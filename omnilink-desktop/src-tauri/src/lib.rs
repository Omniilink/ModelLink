use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use tauri::{AppHandle, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use std::process::Command;

#[derive(serde::Serialize)]
struct OpenCodeInfo {
    found: bool,
    path: Option<String>,
    version: Option<String>,
}

fn candidate_paths() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(appdata) = std::env::var("APPDATA") {
        let npm = PathBuf::from(appdata).join("npm");
        candidates.push(npm.join("opencode.exe"));
        candidates.push(npm.join("opencode.cmd"));
        candidates.push(
            npm.join("node_modules")
                .join("opencode-ai")
                .join("bin")
                .join("opencode.exe"),
        );
        candidates.push(
            npm.join("node_modules")
                .join("opencode-ai")
                .join("bin")
                .join("opencode"),
        );
    }

    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        let bin = PathBuf::from(local).join("Programs");
        candidates.push(bin.join("opencode").join("opencode.exe"));
    }

    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            candidates.push(dir.join("opencode.exe"));
            candidates.push(dir.join("opencode.cmd"));
            candidates.push(dir.join("opencode"));
        }
    }

    candidates
}

fn read_version(path: &Path) -> Option<String> {
    let is_script = path
        .extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| ext.eq_ignore_ascii_case("cmd") || ext.eq_ignore_ascii_case("ps1"))
        .unwrap_or(false);

    let mut command = if is_script {
        let mut cmd = Command::new("cmd");
        cmd.arg("/C").arg(path).arg("--version");
        cmd
    } else {
        let mut cmd = Command::new(path);
        cmd.arg("--version");
        cmd
    };

    #[cfg(windows)]
    command.creation_flags(0x0800_0000);

    let output = command.output().ok()?;
    if !output.status.success() {
        return None;
    }

    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        return None;
    }
    Some(text.lines().next().unwrap_or(&text).trim().to_string())
}

#[tauri::command]
fn detect_opencode() -> OpenCodeInfo {
    for path in candidate_paths() {
        if path.is_file() {
            let version = read_version(&path);
            return OpenCodeInfo {
                found: true,
                path: Some(path.to_string_lossy().to_string()),
                version,
            };
        }
    }

    OpenCodeInfo {
        found: false,
        path: None,
        version: None,
    }
}

// ------------------------------------------------------------------ *
// Storage (SQLite WAL + key/value)
// ------------------------------------------------------------------ *

fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

fn db_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app_data_dir(app)?.join("omnilink.db"))
}

fn open_db(app: &AppHandle) -> Result<rusqlite::Connection, String> {
    let conn = rusqlite::Connection::open(db_path(app)?).map_err(|e| e.to_string())?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| e.to_string())?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| e.to_string())?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    )
    .map_err(|e| e.to_string())?;
    Ok(conn)
}

#[tauri::command]
fn storage_get(app: AppHandle, key: String) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    let mut stmt = conn
        .prepare("SELECT value FROM kv WHERE key = ?1")
        .map_err(|e| e.to_string())?;
    let mut rows = stmt.query([key]).map_err(|e| e.to_string())?;
    if let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let value: String = row.get(0).map_err(|e| e.to_string())?;
        Ok(Some(value))
    } else {
        Ok(None)
    }
}

#[tauri::command]
fn storage_set(app: AppHandle, key: String, value: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn storage_delete(app: AppHandle, key: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM kv WHERE key = ?1", [key])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn storage_keys(app: AppHandle) -> Result<Vec<String>, String> {
    let conn = open_db(&app)?;
    let mut stmt = conn
        .prepare("SELECT key FROM kv")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| e.to_string())?;
    let mut keys = Vec::new();
    for row in rows {
        keys.push(row.map_err(|e| e.to_string())?);
    }
    Ok(keys)
}

// ------------------------------------------------------------------ *
// Secrets (DPAPI on Windows)
// ------------------------------------------------------------------ *

#[cfg(windows)]
mod secrets {
    use super::app_data_dir;
    use tauri::AppHandle;
    use std::fs;
    use std::path::PathBuf;

    #[repr(C)]
    struct DataBlob {
        cb_data: u32,
        pb_data: *mut u8,
    }

    #[link(name = "crypt32")]
    unsafe extern "system" {
        fn CryptProtectData(
            p_data_in: *const DataBlob,
            sz_data_desc: *const u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *mut std::ffi::c_void,
            p_prompt_struct: *mut std::ffi::c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;
        fn CryptUnprotectData(
            p_data_in: *const DataBlob,
            ppsz_data_desc: *mut *mut u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *mut std::ffi::c_void,
            p_prompt_struct: *mut std::ffi::c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;
        fn LocalFree(h_mem: *mut std::ffi::c_void) -> *mut std::ffi::c_void;
    }

    fn secret_path(app: &AppHandle, key: &str) -> Result<PathBuf, String> {
        let dir = app_data_dir(app)?.join("secrets");
        fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        // Sanitize key for filename.
        let safe: String = key
            .chars()
            .map(|c| {
                if c.is_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        Ok(dir.join(format!("{safe}.bin")))
    }

    pub fn set(app: &AppHandle, key: &str, value: &str) -> Result<(), String> {
        let input = value.as_bytes();
        let blob_in = DataBlob {
            cb_data: input.len() as u32,
            pb_data: input.as_ptr() as *mut u8,
        };
        let mut blob_out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let ok = unsafe {
            CryptProtectData(
                &blob_in,
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                0,
                &mut blob_out,
            )
        };
        if ok == 0 {
            return Err("CryptProtectData failed".to_string());
        }
        let encrypted =
            unsafe { std::slice::from_raw_parts(blob_out.pb_data, blob_out.cb_data as usize) };
        let path = secret_path(app, key)?;
        fs::write(&path, encrypted).map_err(|e| e.to_string())?;
        unsafe {
            LocalFree(blob_out.pb_data as *mut std::ffi::c_void);
        }
        Ok(())
    }

    pub fn get(app: &AppHandle, key: &str) -> Result<Option<String>, String> {
        let path = secret_path(app, key)?;
        if !path.exists() {
            return Ok(None);
        }
        let data = fs::read(&path).map_err(|e| e.to_string())?;
        let blob_in = DataBlob {
            cb_data: data.len() as u32,
            pb_data: data.as_ptr() as *mut u8,
        };
        let mut blob_out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let ok = unsafe {
            CryptUnprotectData(
                &blob_in,
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                0,
                &mut blob_out,
            )
        };
        if ok == 0 {
            return Err("CryptUnprotectData failed".to_string());
        }
        let decrypted =
            unsafe { std::slice::from_raw_parts(blob_out.pb_data, blob_out.cb_data as usize) };
        let value = String::from_utf8(decrypted.to_vec()).map_err(|e| e.to_string())?;
        unsafe {
            LocalFree(blob_out.pb_data as *mut std::ffi::c_void);
        }
        Ok(Some(value))
    }

    pub fn delete(app: &AppHandle, key: &str) -> Result<(), String> {
        let path = secret_path(app, key)?;
        if path.exists() {
            fs::remove_file(&path).map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

#[cfg(not(windows))]
mod secrets {
    use tauri::AppHandle;
    pub fn set(_app: &AppHandle, _key: &str, _value: &str) -> Result<(), String> {
        Err("Secrets are only supported on Windows in this build".to_string())
    }
    pub fn get(_app: &AppHandle, _key: &str) -> Result<Option<String>, String> {
        Err("Secrets are only supported on Windows in this build".to_string())
    }
    pub fn delete(_app: &AppHandle, _key: &str) -> Result<(), String> {
        Err("Secrets are only supported on Windows in this build".to_string())
    }
}

#[tauri::command]
fn secret_set(app: AppHandle, key: String, value: String) -> Result<(), String> {
    secrets::set(&app, &key, &value)
}

#[tauri::command]
fn secret_get(app: AppHandle, key: String) -> Result<Option<String>, String> {
    secrets::get(&app, &key)
}

#[tauri::command]
fn secret_delete(app: AppHandle, key: String) -> Result<(), String> {
    secrets::delete(&app, &key)
}

#[derive(serde::Serialize, Debug)]
pub struct McpTool {
    pub name: String,
    pub description: Option<String>,
}

#[tauri::command]
async fn mcp_probe_stdio(command: String, args: String) -> Result<Vec<McpTool>, String> {
    mcp_probe_stdio_impl(command, args).await
}

pub async fn mcp_probe_stdio_impl(command: String, args: String) -> Result<Vec<McpTool>, String> {
    use rmcp::{ServiceExt, transport::TokioChildProcess};

    let mut cmd = tokio::process::Command::new(&command);
    for a in args.split_whitespace() {
        cmd.arg(a);
    }
    let transport = TokioChildProcess::new(cmd).map_err(|e| e.to_string())?;
    let mut client = rmcp::service::serve_client((), transport)
        .await
        .map_err(|e| e.to_string())?;
    let result = client.list_tools(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result
        .tools
        .into_iter()
        .map(|t| McpTool {
            name: t.name.to_string(),
            description: t.description.map(|d| d.to_string()),
        })
        .collect())
}

#[tauri::command]
async fn mcp_probe_http(url: String) -> Result<Vec<McpTool>, String> {
    mcp_probe_http_impl(url).await
}

pub async fn mcp_probe_http_impl(url: String) -> Result<Vec<McpTool>, String> {
    use rmcp::{ServiceExt, transport::StreamableHttpClientTransport};

    let transport = StreamableHttpClientTransport::from_uri(url);
    let mut client = rmcp::service::serve_client((), transport)
        .await
        .map_err(|e| e.to_string())?;
    let result = client.list_tools(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result
        .tools
        .into_iter()
        .map(|t| McpTool {
            name: t.name.to_string(),
            description: t.description.map(|d| d.to_string()),
        })
        .collect())
}

static OAUTH_TOKENS: OnceLock<Mutex<HashMap<u16, Option<String>>>> = OnceLock::new();

fn oauth_tokens() -> &'static Mutex<HashMap<u16, Option<String>>> {
    OAUTH_TOKENS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[tauri::command]
fn oauth_start(port: u16, server_base: String) -> Result<String, String> {
    oauth_start_impl(port, server_base)
}

pub fn oauth_start_impl(port: u16, server_base: String) -> Result<String, String> {
    let listener = TcpListener::bind(format!("127.0.0.1:{}", port)).map_err(|e| e.to_string())?;
    // store pending
    {
        let mut map = oauth_tokens().lock().map_err(|e| e.to_string())?;
        map.insert(port, None);
    }
    let base = server_base.trim_end_matches('/');
    let url = format!("{}/api/auth/google/start?desktop_port={}", base, port);
    // spawn listener thread
    std::thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut request_line = String::new();
            let _ = reader.read_line(&mut request_line);
            // parse path
            let path = request_line.split_whitespace().nth(1).unwrap_or("/");
            // extract token from query
            let mut token: Option<String> = None;
            if let Some(q) = path.split('?').nth(1) {
                for pair in q.split('&') {
                    let mut kv = pair.splitn(2, '=');
                    if let (Some(k), Some(v)) = (kv.next(), kv.next()) {
                        if k == "token" {
                            token = Some(urldecode(v));
                        }
                    }
                }
            }
            {
                let mut map = oauth_tokens().lock().unwrap();
                map.insert(port, token);
            }
            // respond
            let body = "<html><body><h2>Signed in to OmniLink</h2><p>You can close this tab.</p></body></html>";
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = stream.write_all(resp.as_bytes());
            let _ = stream.flush();
        }
    });
    Ok(url)
}

#[tauri::command]
fn oauth_poll(port: u16) -> Result<Option<String>, String> {
    oauth_poll_impl(port)
}

pub fn oauth_poll_impl(port: u16) -> Result<Option<String>, String> {
    let mut map = oauth_tokens().lock().map_err(|e| e.to_string())?;
    Ok(map.get(&port).cloned().flatten())
}

#[tauri::command]
fn oauth_clear(port: u16) -> Result<(), String> {
    oauth_clear_impl(port)
}

pub fn oauth_clear_impl(port: u16) -> Result<(), String> {
    let mut map = oauth_tokens().lock().map_err(|e| e.to_string())?;
    map.remove(&port);
    Ok(())
}

fn urldecode(s: &str) -> String {
    let mut out = String::new();
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v as char);
                i += 3;
                continue;
            }
        } else if bytes[i] == b'+' {
            out.push(' ');
            i += 1;
            continue;
        }
        out.push(bytes[i] as char);
        i += 1;
    }
    out
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(windows)]
    {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", &url])
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(not(windows))]
    {
        let _ = url;
    }
    Ok(())
}
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(
            tauri_plugin_log::Builder::new()
                .level(if cfg!(debug_assertions) {
                    log::LevelFilter::Info
                } else {
                    log::LevelFilter::Warn
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            detect_opencode,
            storage_get,
            storage_set,
            storage_delete,
            storage_keys,
            secret_set,
            secret_get,
            secret_delete,
            mcp_probe_stdio,
            mcp_probe_http,
            oauth_start,
            oauth_poll,
            oauth_clear,
            open_url
        ])
        .run(tauri::generate_context!())
        .expect("error while building OmniLink");
}
