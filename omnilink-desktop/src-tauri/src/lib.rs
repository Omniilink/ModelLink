use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
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
        .invoke_handler(tauri::generate_handler![detect_opencode])
        .run(tauri::generate_context!())
        .expect("error while building OmniLink");
}
