use crate::scanner;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager, State};

#[derive(Default)]
pub struct ScannerState {
    busy: Arc<AtomicBool>,
}

struct BusyGuard(Arc<AtomicBool>);

impl Drop for BusyGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

impl ScannerState {
    fn acquire(&self) -> Result<BusyGuard, String> {
        self.busy
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| BusyGuard(Arc::clone(&self.busy)))
            .map_err(|_| "A scanner operation is already in progress.".to_string())
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ScanConfig {
    pub scanner_name: String,
    #[serde(default)]
    pub driver: ScannerDriver,
    #[serde(default = "default_dpi")]
    pub dpi: u16,
    #[serde(default)]
    pub duplex: bool,
    #[serde(default)]
    pub pixel_type: PixelType,
    #[serde(default = "default_max_pages")]
    pub max_pages: u16,
}

fn default_dpi() -> u16 {
    300
}

fn default_max_pages() -> u16 {
    100
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScannerDriver {
    #[default]
    Twain,
    Isis,
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PixelType {
    BlackAndWhite,
    #[default]
    Grayscale,
    Rgb,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScanPage {
    pub index: usize,
    pub mime_type: String,
    pub absolute_path: String,
    pub file_url: String,
    pub byte_length: u64,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScanResult {
    pub scanner_name: String,
    pub pages: Vec<ScanPage>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PageAcquiredEvent {
    page_index: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScanCompletedEvent {
    page_count: usize,
}

#[derive(Clone, Debug, Serialize)]
struct ScanErrorEvent {
    message: String,
}

/// Enumerate native scanner data sources without blocking the WebView thread.
#[tauri::command]
pub async fn list_scanners(state: State<'_, ScannerState>) -> Result<Vec<String>, String> {
    let guard = state.acquire()?;
    tokio::task::spawn_blocking(move || {
        let _guard = guard;
        scanner::list_scanners()
    })
    .await
    .map_err(|err| format!("Scanner enumeration task failed: {err}"))?
}

/// Acquire pages on a blocking native thread and emit progress to the frontend.
#[tauri::command]
pub async fn acquire_scan(
    app: AppHandle,
    state: State<'_, ScannerState>,
    config: ScanConfig,
) -> Result<ScanResult, String> {
    validate_config(&config)?;
    let guard = state.acquire()?;
    let cache_root = app
        .path()
        .app_cache_dir()
        .map_err(|err| format!("Unable to resolve the application cache directory: {err}"))?
        .join("scans");
    let scanner_name = config.scanner_name.clone();
    let event_app = app.clone();

    let task = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        let progress_app = event_app.clone();
        scanner::acquire_scan(config, cache_root, move |page_index| {
            let payload = PageAcquiredEvent { page_index };
            let _ = progress_app.emit("scan:page-acquired", payload);
        })
    })
    .await
    .map_err(|err| format!("Scanner acquisition task failed: {err}"))?;

    match task {
        Ok(paths) => {
            let pages = paths
                .iter()
                .enumerate()
                .map(|(index, path)| page_result(index + 1, path))
                .collect::<Result<Vec<_>, _>>()?;
            let _ = app.emit(
                "scan:completed",
                ScanCompletedEvent {
                    page_count: pages.len(),
                },
            );
            Ok(ScanResult {
                scanner_name,
                pages,
            })
        }
        Err(message) => {
            let _ = app.emit(
                "scan:error",
                ScanErrorEvent {
                    message: message.clone(),
                },
            );
            Err(message)
        }
    }
}

fn validate_config(config: &ScanConfig) -> Result<(), String> {
    if config.scanner_name.trim().is_empty() {
        return Err("Select a scanner before starting acquisition.".to_string());
    }
    if !(75..=1200).contains(&config.dpi) {
        return Err("Scanner DPI must be between 75 and 1200.".to_string());
    }
    if !(1..=500).contains(&config.max_pages) {
        return Err("maxPages must be between 1 and 500.".to_string());
    }
    Ok(())
}

fn page_result(index: usize, path: &PathBuf) -> Result<ScanPage, String> {
    let metadata = std::fs::metadata(path)
        .map_err(|err| format!("Unable to inspect scanned page {}: {err}", path.display()))?;
    Ok(ScanPage {
        index,
        mime_type: "image/png".to_string(),
        absolute_path: path.to_string_lossy().into_owned(),
        file_url: path_to_file_url(path),
        byte_length: metadata.len(),
    })
}

fn path_to_file_url(path: &std::path::Path) -> String {
    let normalized = path.to_string_lossy().replace('\\', "/");
    format!("file:///{}", normalized.trim_start_matches('/'))
}
