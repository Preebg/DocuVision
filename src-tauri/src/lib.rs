use std::fs;
use std::path::PathBuf;

mod commands;
mod scanner;

const OPERATOR_SESSION_SERVICE: &str = "dev.preebg.docuvision";
const OPERATOR_SESSION_ACCOUNT: &str = "operator-session";
const MAX_SESSION_BYTES: usize = 64 * 1024;

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

fn operator_session_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(OPERATOR_SESSION_SERVICE, OPERATOR_SESSION_ACCOUNT)
        .map_err(|err| format!("Unable to open OS credential store: {err}"))
}

/// Store the Supabase session in the current user's OS credential manager.
#[tauri::command]
fn write_operator_session(session: String) -> Result<(), String> {
    if session.is_empty() || session.len() > MAX_SESSION_BYTES {
        return Err("Operator session has an invalid size.".to_string());
    }
    operator_session_entry()?
        .set_password(&session)
        .map_err(|err| format!("Unable to store operator session: {err}"))
}

/// Restore the active operator session without exposing any filesystem path.
#[tauri::command]
fn read_operator_session() -> Result<Option<String>, String> {
    match operator_session_entry()?.get_password() {
        Ok(session) => Ok(Some(session)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(format!("Unable to read operator session: {err}")),
    }
}

/// Remove persisted operator credentials during sign-out.
#[tauri::command]
fn delete_operator_session() -> Result<(), String> {
    match operator_session_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => Err(format!("Unable to delete operator session: {err}")),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(commands::ScannerState::default())
        .plugin(tauri_plugin_http::init())
        .invoke_handler(tauri::generate_handler![
            read_local_api_token,
            write_operator_session,
            read_operator_session,
            delete_operator_session,
            commands::list_scanners,
            commands::acquire_scan
        ])
        .run(tauri::generate_context!())
        .expect("error while running DocuVision");
}
