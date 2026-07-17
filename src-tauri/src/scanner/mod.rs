use crate::commands::{ScanConfig, ScannerDriver};
use image::ImageFormat;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(target_os = "windows")]
mod twain;

static SCAN_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub fn list_scanners() -> Result<Vec<String>, String> {
    #[cfg(target_os = "windows")]
    {
        return twain::list_sources();
    }

    #[cfg(not(target_os = "windows"))]
    Err(
        "Native scanner acquisition is currently implemented only for Windows TWAIN drivers."
            .to_string(),
    )
}

pub fn acquire_scan<F>(
    config: ScanConfig,
    cache_root: PathBuf,
    mut on_page: F,
) -> Result<Vec<PathBuf>, String>
where
    F: FnMut(usize) + Send,
{
    let scan_dir = create_scan_directory(&cache_root)?;

    let acquired = match config.driver {
        ScannerDriver::Twain => acquire_twain(&config, &scan_dir, &mut on_page),
        ScannerDriver::Isis => isis::acquire(&config, &scan_dir),
    };

    let raw_pages = match acquired {
        Ok(pages) => pages,
        Err(err) => {
            let _ = std::fs::remove_dir_all(&scan_dir);
            return Err(err);
        }
    };

    let mut normalized = Vec::with_capacity(raw_pages.len());
    for (offset, raw_path) in raw_pages.iter().enumerate() {
        let page_index = offset + 1;
        let png_path = scan_dir.join(format!("page-{page_index:04}.png"));
        if let Err(err) = normalize_to_png(raw_path, &png_path) {
            let _ = std::fs::remove_dir_all(&scan_dir);
            return Err(err);
        }
        if raw_path != &png_path {
            let _ = std::fs::remove_file(raw_path);
        }
        if matches!(config.driver, ScannerDriver::Isis) {
            on_page(page_index);
        }
        normalized.push(png_path);
    }

    if normalized.is_empty() {
        let _ = std::fs::remove_dir_all(&scan_dir);
        return Err("The scanner completed without transferring any pages.".to_string());
    }
    Ok(normalized)
}

fn create_scan_directory(cache_root: &Path) -> Result<PathBuf, String> {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|err| format!("System clock is invalid: {err}"))?
        .as_millis();
    let sequence = SCAN_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let path = cache_root.join(format!("{timestamp}-{sequence}"));
    std::fs::create_dir_all(&path).map_err(|err| {
        format!(
            "Unable to create secure scan cache {}: {err}",
            path.display()
        )
    })?;
    Ok(path)
}

fn normalize_to_png(source: &Path, destination: &Path) -> Result<(), String> {
    let image = image::ImageReader::open(source)
        .map_err(|err| format!("Unable to open scanner output {}: {err}", source.display()))?
        .with_guessed_format()
        .map_err(|err| {
            format!(
                "Unable to identify scanner output {}: {err}",
                source.display()
            )
        })?
        .decode()
        .map_err(|err| {
            format!(
                "Unable to decode scanner output {}: {err}",
                source.display()
            )
        })?;
    image
        .save_with_format(destination, ImageFormat::Png)
        .map_err(|err| {
            format!(
                "Unable to write normalized page {}: {err}",
                destination.display()
            )
        })
}

#[cfg(target_os = "windows")]
fn acquire_twain(
    config: &ScanConfig,
    scan_dir: &Path,
    on_page: &mut impl FnMut(usize),
) -> Result<Vec<PathBuf>, String> {
    twain::acquire(config, scan_dir, on_page)
}

#[cfg(not(target_os = "windows"))]
fn acquire_twain(
    _config: &ScanConfig,
    _scan_dir: &Path,
    _on_page: &mut impl FnMut(usize),
) -> Result<Vec<PathBuf>, String> {
    Err("TWAIN acquisition is available only in the Windows desktop build.".to_string())
}

/// ISIS has no public, stable driver-manager ABI comparable to TWAIN.
///
/// Production integration must implement this boundary against the licensed
/// ISIS SDK installed for the deployment (for example OpenText/EMC Captiva or
/// the scanner vendor's toolkit), link its cabinet/runtime files in build.rs,
/// and translate the SDK's page callback into files under `scan_dir`.
mod isis {
    use super::*;

    pub fn acquire(_config: &ScanConfig, _scan_dir: &Path) -> Result<Vec<PathBuf>, String> {
        Err(
            "ISIS support requires a licensed vendor ISIS SDK and runtime. Configure the \
             deployment-specific adapter in src-tauri/src/scanner/mod.rs; TWAIN remains available."
                .to_string(),
        )
    }
}
