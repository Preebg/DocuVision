use std::fs;
use std::path::PathBuf;

/// Resolve the per-user DocuVision ``token.lock`` path written by ``run_api.py``.
fn token_lock_path() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        let local = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let mut home = dirs_fallback_home();
                home.push("AppData");
                home.push("Local");
                home
            });
        return local.join("DocuVision").join("token.lock");
    }

    #[cfg(target_os = "macos")]
    {
        let mut path = dirs_fallback_home();
        path.push("Library");
        path.push("Application Support");
        path.push("DocuVision");
        path.push("token.lock");
        return path;
    }

    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let mut path = std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let mut home = dirs_fallback_home();
                home.push(".local");
                home.push("state");
                home
            });
        path.push("DocuVision");
        path.push("token.lock");
        return path;
    }
}

fn dirs_fallback_home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Read the FastAPI session handshake key from the local lockfile.
#[tauri::command]
fn read_local_api_token() -> Result<String, String> {
    let path = token_lock_path();
    let raw = fs::read_to_string(&path).map_err(|err| {
        format!(
            "Unable to read API token lockfile at {}: {}",
            path.display(),
            err
        )
    })?;
    let token = raw.trim().to_string();
    if token.is_empty() {
        return Err(format!(
            "API token lockfile at {} is empty. Is run_api.py running?",
            path.display()
        ));
    }
    Ok(token)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .invoke_handler(tauri::generate_handler![read_local_api_token])
        .run(tauri::generate_context!())
        .expect("error while running DocuVision");
}
