use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use tauri::{AppHandle, Emitter, Manager};

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

static OPENCODE_CHILD: OnceLock<Arc<Mutex<Option<std::process::Child>>>> = OnceLock::new();

fn opencode_child() -> &'static Arc<Mutex<Option<std::process::Child>>> {
    OPENCODE_CHILD.get_or_init(|| Arc::new(Mutex::new(None)))
}

#[tauri::command]
fn opencode_start_background() -> Result<String, String> {
    let child_lock = opencode_child();
    let mut guard = child_lock.lock().map_err(|e| e.to_string())?;
    if let Some(child) = guard.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            return Ok("already running".to_string());
        }
        *guard = None;
    }
    let info = detect_opencode();
    let path = info.path.ok_or("OpenCode not found")?;
    let mut cmd = Command::new(&path);
    cmd.args(["serve", "--hostname", "127.0.0.1", "--port", "4096"]);
    #[cfg(windows)]
    cmd.creation_flags(0x08000000 | 0x00000200); // CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    let child = cmd.spawn().map_err(|e| e.to_string())?;
    *guard = Some(child);
    Ok("started".to_string())
}

#[tauri::command]
fn opencode_stop_background() -> Result<String, String> {
    let child_lock = opencode_child();
    let mut guard = child_lock.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
        Ok("stopped".to_string())
    } else {
        Ok("not running".to_string())
    }
}

#[tauri::command]
fn opencode_status_background() -> Result<bool, String> {
    let child_lock = opencode_child();
    let mut guard = child_lock.lock().map_err(|e| e.to_string())?;
    if let Some(child) = guard.as_mut() {
        match child.try_wait() {
            Ok(Some(_)) => {
                *guard = None;
                Ok(false)
            }
            Ok(None) => Ok(true),
            Err(_) => Ok(false),
        }
    } else {
        Ok(false)
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
#[derive(serde::Serialize, Debug)]
pub struct McpResource {
    pub uri: String,
    pub name: Option<String>,
    pub description: Option<String>,
    pub mime_type: Option<String>,
}

#[tauri::command]
async fn mcp_call_tool_stdio(command: String, args: String, tool_name: String, tool_args: String) -> Result<String, String> {
    use rmcp::{ServiceExt, transport::TokioChildProcess};

    let mut cmd = tokio::process::Command::new(&command);
    for a in args.split_whitespace() {
        cmd.arg(a);
    }
    let transport = TokioChildProcess::new(cmd).map_err(|e| e.to_string())?;
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let parsed_args: Option<serde_json::Value> = if tool_args.trim().is_empty() { None } else { serde_json::from_str(&tool_args).ok() };
    let mut params = rmcp::model::CallToolRequestParams::new(tool_name);
    if let Some(obj) = parsed_args.and_then(|v| v.as_object().cloned()) {
        params = params.with_arguments(obj);
    }
    let result = client.call_tool(params).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    let mut out = String::new();
    for c in &result.content {
        if let Some(t) = c.as_text() {
            out.push_str(&t.text);
            out.push('\n');
        }
    }
    if out.is_empty() {
        out = serde_json::to_string(&result).unwrap_or_default();
    }
    Ok(out)
}

#[tauri::command]
async fn mcp_call_tool_http(url: String, tool_name: String, tool_args: String) -> Result<String, String> {
    use rmcp::{ServiceExt, transport::StreamableHttpClientTransport};

    let transport = StreamableHttpClientTransport::from_uri(url);
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let parsed_args: Option<serde_json::Value> = if tool_args.trim().is_empty() { None } else { serde_json::from_str(&tool_args).ok() };
    let mut params = rmcp::model::CallToolRequestParams::new(tool_name);
    if let Some(obj) = parsed_args.and_then(|v| v.as_object().cloned()) {
        params = params.with_arguments(obj);
    }
    let result = client.call_tool(params).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    let mut out = String::new();
    for c in &result.content {
        if let Some(t) = c.as_text() {
            out.push_str(&t.text);
            out.push('\n');
        }
    }
    if out.is_empty() {
        out = serde_json::to_string(&result).unwrap_or_default();
    }
    Ok(out)
}

#[tauri::command]
async fn mcp_list_resources_stdio(command: String, args: String) -> Result<Vec<McpResource>, String> {
    use rmcp::{ServiceExt, transport::TokioChildProcess};

    let mut cmd = tokio::process::Command::new(&command);
    for a in args.split_whitespace() {
        cmd.arg(a);
    }
    let transport = TokioChildProcess::new(cmd).map_err(|e| e.to_string())?;
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let result = client.list_resources(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result.resources.into_iter().map(|r| McpResource {
        uri: r.uri.to_string(),
        name: Some(r.name.to_string()),
        description: r.description.map(|d| d.to_string()),
        mime_type: r.mime_type.map(|m| m.to_string()),
    }).collect())
}

#[tauri::command]
async fn mcp_list_resources_http(url: String) -> Result<Vec<McpResource>, String> {
    use rmcp::{ServiceExt, transport::StreamableHttpClientTransport};

    let transport = StreamableHttpClientTransport::from_uri(url);
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let result = client.list_resources(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result.resources.into_iter().map(|r| McpResource {
        uri: r.uri.to_string(),
        name: Some(r.name.to_string()),
        description: r.description.map(|d| d.to_string()),
        mime_type: r.mime_type.map(|m| m.to_string()),
    }).collect())
}

#[tauri::command]
fn run_shell_command(command: String) -> Result<String, String> {
    #[cfg(windows)]
    {
        let output = std::process::Command::new("cmd")
            .args(["/C", &command])
            .output()
            .map_err(|e| e.to_string())?;
        let stdout = String::from_utf8_lossy(&output.stdout).to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        let mut out = stdout;
        if !stderr.is_empty() {
            out.push_str("\n--- stderr ---\n");
            out.push_str(&stderr);
        }
        out.push_str(&format!("\n--- exit: {} ---", output.status));
        Ok(out)
    }
    #[cfg(not(windows))]
    {
        let output = std::process::Command::new("sh")
            .args(["-c", &command])
            .output()
            .map_err(|e| e.to_string())?;
        let stdout = String::from_utf8_lossy(&output.stdout).to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        let mut out = stdout;
        if !stderr.is_empty() {
            out.push_str("\n--- stderr ---\n");
            out.push_str(&stderr);
        }
        out.push_str(&format!("\n--- exit: {} ---", output.status));
        Ok(out)
    }
}

#[tauri::command]
async fn http_request(method: String, url: String, body: String) -> Result<String, String> {
    let client = reqwest::Client::new();
    let req = match method.to_uppercase().as_str() {
        "GET" => client.get(&url),
        "POST" => client.post(&url).body(body),
        "PUT" => client.put(&url).body(body),
        "DELETE" => client.delete(&url),
        _ => return Err(format!("Unsupported method: {}", method)),
    };
    let resp = req.send().await.map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    Ok(format!("HTTP {}\n{}", status, text))
}

#[tauri::command]
async fn mcp_list_prompts_stdio(command: String, args: String) -> Result<Vec<String>, String> {
    use rmcp::{ServiceExt, transport::TokioChildProcess};

    let mut cmd = tokio::process::Command::new(&command);
    for a in args.split_whitespace() {
        cmd.arg(a);
    }
    let transport = TokioChildProcess::new(cmd).map_err(|e| e.to_string())?;
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let result = client.list_prompts(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result.prompts.into_iter().map(|p| p.name.to_string()).collect())
}

#[tauri::command]
async fn mcp_list_prompts_http(url: String) -> Result<Vec<String>, String> {
    use rmcp::{ServiceExt, transport::StreamableHttpClientTransport};

    let transport = StreamableHttpClientTransport::from_uri(url);
    let mut client = rmcp::service::serve_client((), transport).await.map_err(|e| e.to_string())?;
    let result = client.list_prompts(None).await.map_err(|e| e.to_string())?;
    let _ = client.cancel().await;
    Ok(result.prompts.into_iter().map(|p| p.name.to_string()).collect())
}
#[derive(serde::Serialize, Clone, Debug)]
pub struct WatchdogStatus {
    pub running: bool,
    pub restarts: u32,
    pub last_error: Option<String>,
}

static WATCHDOGS: OnceLock<Mutex<HashMap<String, WatchdogHandle>>> = OnceLock::new();

struct WatchdogHandle {
    stop: Arc<AtomicBool>,
    status: Arc<Mutex<WatchdogStatus>>,
}

fn watchdogs() -> &'static Mutex<HashMap<String, WatchdogHandle>> {
    WATCHDOGS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[tauri::command]
fn watchdog_start(id: String, command: String, args: String) -> Result<(), String> {
    watchdog_stop(id.clone()).ok();
    let stop = Arc::new(AtomicBool::new(false));
    let status = Arc::new(Mutex::new(WatchdogStatus {
        running: true,
        restarts: 0,
        last_error: None,
    }));
    let stop_clone = stop.clone();
    let status_clone = status.clone();
    std::thread::spawn(move || {
        let mut restarts = 0u32;
        while !stop_clone.load(Ordering::Relaxed) {
            let mut cmd = std::process::Command::new(&command);
            for a in args.split_whitespace() {
                cmd.arg(a);
            }
            let result = cmd.output();
            match result {
                Ok(output) if output.status.success() => break,
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
                    restarts += 1;
                    if let Ok(mut s) = status_clone.lock() {
                        s.restarts = restarts;
                        s.last_error = Some(format!("exit {}: {}", output.status, stderr));
                        s.running = true;
                    }
                }
                Err(e) => {
                    restarts += 1;
                    if let Ok(mut s) = status_clone.lock() {
                        s.restarts = restarts;
                        s.last_error = Some(e.to_string());
                        s.running = true;
                    }
                }
            }
            if stop_clone.load(Ordering::Relaxed) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_secs(2));
        }
        if let Ok(mut s) = status_clone.lock() {
            s.running = false;
        }
    });
    let mut map = watchdogs().lock().map_err(|e| e.to_string())?;
    map.insert(
        id,
        WatchdogHandle {
            stop,
            status,
        },
    );
    Ok(())
}

#[tauri::command]
fn watchdog_stop(id: String) -> Result<(), String> {
    let mut map = watchdogs().lock().map_err(|e| e.to_string())?;
    if let Some(handle) = map.remove(&id) {
        handle.stop.store(true, Ordering::Relaxed);
        if let Ok(mut s) = handle.status.lock() {
            s.running = false;
        }
    }
    Ok(())
}

#[tauri::command]
fn watchdog_status(id: String) -> Result<WatchdogStatus, String> {
    let map = watchdogs().lock().map_err(|e| e.to_string())?;
    if let Some(handle) = map.get(&id) {
        let s = handle.status.lock().map_err(|e| e.to_string())?.clone();
        Ok(s)
    } else {
        Ok(WatchdogStatus {
            running: false,
            restarts: 0,
            last_error: None,
        })
    }
}

#[tauri::command]
fn workspace_encrypt(app: AppHandle) -> Result<(), String> {
    use std::fs;
    let db = app_data_dir(&app)?.join("omnilink.db");
    if !db.exists() {
        return Ok(());
    }
    let data = fs::read(&db).map_err(|e| e.to_string())?;
    // Encrypt with DPAPI.
    #[cfg(windows)]
    {
        // Use the secrets module's DPAPI.
        // Reuse CryptProtectData via a local call.
        #[repr(C)]
        struct DataBlob {
            cb_data: u32,
            pb_data: *mut u8,
        }
        #[link(name = "crypt32")]
        unsafe extern "C" {
            fn CryptProtectData(
                p_data_in: *const DataBlob,
                sz_data_desc: *const u16,
                p_optional_entropy: *const DataBlob,
                pv_reserved: *mut std::ffi::c_void,
                p_prompt_struct: *mut std::ffi::c_void,
                dw_flags: u32,
                p_data_out: *mut DataBlob,
            ) -> i32;
            fn LocalFree(h_mem: *mut std::ffi::c_void) -> *mut std::ffi::c_void;
        }
        let blob_in = DataBlob {
            cb_data: data.len() as u32,
            pb_data: data.as_ptr() as *mut u8,
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
        let encrypted = unsafe {
            std::slice::from_raw_parts(blob_out.pb_data, blob_out.cb_data as usize)
        };
        let enc_path = app_data_dir(&app)?.join("omnilink.db.enc");
        fs::write(&enc_path, encrypted).map_err(|e| e.to_string())?;
        unsafe {
            LocalFree(blob_out.pb_data as *mut std::ffi::c_void);
        }
        // Remove unencrypted DB and WAL files.
        let _ = fs::remove_file(&db);
        let _ = fs::remove_file(app_data_dir(&app)?.join("omnilink.db-wal"));
        let _ = fs::remove_file(app_data_dir(&app)?.join("omnilink.db-shm"));
    }
    #[cfg(not(windows))]
    {
        let _ = data;
    }
    Ok(())
}

#[tauri::command]
fn workspace_decrypt(app: AppHandle) -> Result<(), String> {
    use std::fs;
    let enc_path = app_data_dir(&app)?.join("omnilink.db.enc");
    if !enc_path.exists() {
        return Ok(());
    }
    let data = fs::read(&enc_path).map_err(|e| e.to_string())?;
    #[cfg(windows)]
    {
        #[repr(C)]
        struct DataBlob {
            cb_data: u32,
            pb_data: *mut u8,
        }
        #[link(name = "crypt32")]
        unsafe extern "C" {
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
        let decrypted = unsafe {
            std::slice::from_raw_parts(blob_out.pb_data, blob_out.cb_data as usize)
        };
        let db = app_data_dir(&app)?.join("omnilink.db");
        fs::write(&db, decrypted).map_err(|e| e.to_string())?;
        unsafe {
            LocalFree(blob_out.pb_data as *mut std::ffi::c_void);
        }
        let _ = fs::remove_file(&enc_path);
    }
    #[cfg(not(windows))]
    {
        let _ = data;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
// ---------------------------------------------------------------------------
// Model transport
//
// The webview runs on http://tauri.localhost in release builds, and Ollama
// rejects that origin with 403. All model traffic therefore goes through Rust,
// which has no CORS restrictions. This also keeps provider keys out of JS.
// ---------------------------------------------------------------------------

fn opencode_auth_path() -> Option<PathBuf> {
    if let Ok(xdg) = std::env::var("XDG_DATA_HOME") {
        let p = PathBuf::from(xdg).join("opencode").join("auth.json");
        if p.exists() {
            return Some(p);
        }
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .ok()?;
    let p = PathBuf::from(home)
        .join(".local")
        .join("share")
        .join("opencode")
        .join("auth.json");
    if p.exists() {
        Some(p)
    } else {
        None
    }
}

fn opencode_auth_json() -> Option<serde_json::Value> {
    let path = opencode_auth_path()?;
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn opencode_key(provider: &str) -> Option<String> {
    let json = opencode_auth_json()?;
    json.get(provider)?
        .get("key")?
        .as_str()
        .map(|value| value.to_string())
        .filter(|value| !value.is_empty())
}

#[tauri::command]
fn opencode_auth_status() -> Result<Vec<String>, String> {
    let mut found = Vec::new();
    if opencode_key("opencode").is_some() {
        found.push("opencode".to_string());
    }
    if opencode_key("opencode-go").is_some() {
        found.push("opencode-go".to_string());
    }
    Ok(found)
}

#[derive(serde::Serialize)]
struct ModelHttpResponse {
    status: u16,
    body: String,
}

fn apply_auth(
    mut request: reqwest::RequestBuilder,
    auth_source: Option<&str>,
) -> Result<reqwest::RequestBuilder, String> {
    if let Some(source) = auth_source {
        if !source.is_empty() {
            let key = opencode_key(source).ok_or_else(|| {
                format!(
                    "No OpenCode credential for '{source}'. Sign in with the OpenCode CLI, or paste an API key."
                )
            })?;
            request = request.header("Authorization", format!("Bearer {key}"));
        }
    }
    Ok(request)
}

#[tauri::command]
async fn model_http(
    method: String,
    url: String,
    headers: HashMap<String, String>,
    body: Option<String>,
    auth_source: Option<String>,
) -> Result<ModelHttpResponse, String> {
    let client = reqwest::Client::builder()
        .user_agent("OmniLink/0.1.1")
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())?;

    let mut request = client.request(
        reqwest::Method::from_bytes(method.to_uppercase().as_bytes())
            .map_err(|e| e.to_string())?,
        &url,
    );
    for (key, value) in headers {
        request = request.header(key, value);
    }
    request = apply_auth(request, auth_source.as_deref())?;
    if let Some(payload) = body {
        request = request.body(payload);
    }

    let response = request.send().await.map_err(|e| e.to_string())?;
    let status = response.status().as_u16();
    let text = response.text().await.unwrap_or_default();
    Ok(ModelHttpResponse { status, body: text })
}

fn model_cancels() -> &'static Mutex<HashMap<String, Arc<AtomicBool>>> {
    static CANCELS: OnceLock<Mutex<HashMap<String, Arc<AtomicBool>>>> = OnceLock::new();
    CANCELS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[tauri::command]
async fn model_stream(
    app: AppHandle,
    request_id: String,
    url: String,
    headers: HashMap<String, String>,
    body: String,
    auth_source: Option<String>,
) -> Result<(), String> {
    let cancel = Arc::new(AtomicBool::new(false));
    {
        let mut guard = model_cancels().lock().map_err(|e| e.to_string())?;
        guard.insert(request_id.clone(), cancel.clone());
    }

    let result = model_stream_inner(url, headers, body, auth_source, cancel, |text| {
        let _ = app.emit(&format!("model:chunk:{request_id}"), text.to_string());
    })
    .await;

    if let Ok(mut guard) = model_cancels().lock() {
        guard.remove(&request_id);
    }

    match result {
        Ok(()) => {
            let _ = app.emit(&format!("model:done:{request_id}"), ());
            Ok(())
        }
        Err(error) => {
            let _ = app.emit(&format!("model:error:{request_id}"), error.clone());
            Err(error)
        }
    }
}

async fn model_stream_inner<F>(
    url: String,
    headers: HashMap<String, String>,
    body: String,
    auth_source: Option<String>,
    cancel: Arc<AtomicBool>,
    mut on_chunk: F,
) -> Result<(), String>
where
    F: FnMut(&str),
{
    let client = reqwest::Client::builder()
        .user_agent("OmniLink/0.1.1")
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;

    let mut request = client.post(&url);
    for (key, value) in headers {
        request = request.header(key, value);
    }
    request = apply_auth(request, auth_source.as_deref())?;

    let mut response = request.body(body).send().await.map_err(|e| e.to_string())?;

    if !response.status().is_success() {
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        return Err(format!("HTTP {status}: {text}"));
    }

    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        if cancel.load(Ordering::SeqCst) {
            break;
        }
        let text = String::from_utf8_lossy(&chunk).to_string();
        on_chunk(&text);
    }

    Ok(())
}

#[tauri::command]
fn model_abort(request_id: String) -> Result<(), String> {
    if let Ok(guard) = model_cancels().lock() {
        if let Some(flag) = guard.get(&request_id) {
            flag.store(true, Ordering::SeqCst);
        }
    }
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Decrypt workspace if an encrypted file exists.
            let handle = app.handle();
            let _ = workspace_decrypt(handle.clone());
            Ok(())
        })
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
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
            opencode_start_background,
            opencode_stop_background,
            opencode_status_background,
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
            open_url,
            mcp_call_tool_stdio,
            mcp_call_tool_http,
            mcp_list_resources_stdio,
            mcp_list_resources_http,
            mcp_list_prompts_stdio,
            mcp_list_prompts_http,
            watchdog_start,
            watchdog_stop,
            watchdog_status,
            workspace_encrypt,
            workspace_decrypt,
            run_shell_command,
            http_request,
            opencode_auth_status,
            model_http,
            model_stream,
            model_abort
        ])
        .run(tauri::generate_context!())
        .expect("error while building OmniLink");
}

#[cfg(test)]
mod model_transport_tests {
    use super::*;

    fn block_on<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    #[test]
    fn opencode_auth_is_discoverable() {
        let status = opencode_auth_status().unwrap();
        println!("opencode auth providers: {status:?}");
    }

    #[test]
    fn ollama_lists_models_through_rust() {
        let result = block_on(model_http(
            "GET".into(),
            "http://127.0.0.1:11434/api/tags".into(),
            HashMap::new(),
            None,
            None,
        ));
        match result {
            Ok(response) => {
                assert_eq!(response.status, 200, "ollama tags status");
                let json: serde_json::Value = serde_json::from_str(&response.body).unwrap();
                let count = json["models"].as_array().map(|m| m.len()).unwrap_or(0);
                println!("ollama models found: {count}");
                assert!(count > 0, "expected at least one local model");
            }
            Err(error) => println!("ollama not reachable in this environment: {error}"),
        }
    }

    #[test]
    fn ollama_streams_real_tokens_through_rust() {
        let body = serde_json::json!({
            "model": "merge-gemma-2b:latest",
            "messages": [{ "role": "user", "content": "Say OK" }],
            "stream": true
        })
        .to_string();

        let mut collected = String::new();
        let result = block_on(model_stream_inner(
            "http://127.0.0.1:11434/api/chat".into(),
            HashMap::new(),
            body,
            None,
            Arc::new(AtomicBool::new(false)),
            |chunk| collected.push_str(chunk),
        ));

        match result {
            Ok(()) => {
                println!("ollama stream bytes: {}", collected.len());
                assert!(collected.contains("message"), "expected NDJSON message frames");
            }
            Err(error) => println!("ollama not reachable in this environment: {error}"),
        }
    }

    #[test]
    fn opencode_go_streams_real_tokens_through_rust() {
        if opencode_key("opencode-go").is_none() {
            println!("skipping: no opencode-go credential");
            return;
        }

        let mut headers = HashMap::new();
        headers.insert("Content-Type".to_string(), "application/json".to_string());
        headers.insert("x-opencode-session".to_string(), "omnilink-test".to_string());

        let body = serde_json::json!({
            "model": "iceman-contributor-free",
            "messages": [{ "role": "user", "content": "Say OMNI OK" }],
            "stream": true
        })
        .to_string();

        let mut collected = String::new();
        let result = block_on(model_stream_inner(
            "https://opencode.ai/inference/go/openai/v1/chat/completions".into(),
            headers,
            body,
            Some("opencode-go".to_string()),
            Arc::new(AtomicBool::new(false)),
            |chunk| collected.push_str(chunk),
        ));

        match result {
            Ok(()) => {
                println!("opencode stream bytes: {}", collected.len());
                assert!(
                    collected.contains("chat.completion.chunk"),
                    "expected SSE chunks, got: {}",
                    &collected[..collected.len().min(300)]
                );
            }
            Err(error) => panic!("opencode go stream failed: {error}"),
        }
    }
}
