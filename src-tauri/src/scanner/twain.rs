//! Minimal TWAIN 2.x DSM binding for Windows.
//!
//! TWAIN is a stateful triplet protocol, not a normal function-per-operation
//! API. All DSM calls and the Windows message pump therefore stay on the same
//! blocking thread for the lifetime of a session.

use crate::commands::{PixelType, ScanConfig};
use libloading::Library;
use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::thread;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{GlobalFree, HGLOBAL, HWND};
use windows_sys::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DestroyWindow, DispatchMessageW, PeekMessageW, TranslateMessage, MSG,
    PM_REMOVE,
};

type TwUint16 = u16;
type TwUint32 = u32;
type TwMemRef = *mut c_void;
type DsmEntry = unsafe extern "system" fn(
    *mut TwIdentity,
    *mut TwIdentity,
    TwUint32,
    TwUint16,
    TwUint16,
    TwMemRef,
) -> TwUint16;

const DG_CONTROL: u32 = 0x0001;
const DG_IMAGE: u32 = 0x0002;
const DAT_CAPABILITY: u16 = 0x0001;
const DAT_EVENT: u16 = 0x0002;
const DAT_IDENTITY: u16 = 0x0003;
const DAT_PARENT: u16 = 0x0004;
const DAT_PENDINGXFERS: u16 = 0x0005;
const DAT_SETUPFILEXFER: u16 = 0x0007;
const DAT_STATUS: u16 = 0x0008;
const DAT_USERINTERFACE: u16 = 0x0009;
const DAT_IMAGEFILEXFER: u16 = 0x0105;

const MSG_GET: u16 = 0x0001;
const MSG_GETFIRST: u16 = 0x0004;
const MSG_GETNEXT: u16 = 0x0005;
const MSG_SET: u16 = 0x0006;
const MSG_RESET: u16 = 0x0007;
const MSG_XFERREADY: u16 = 0x0101;
const MSG_CLOSEDSREQ: u16 = 0x0102;
const MSG_CLOSEDSOK: u16 = 0x0103;
const MSG_OPENDSM: u16 = 0x0301;
const MSG_CLOSEDSM: u16 = 0x0302;
const MSG_OPENDS: u16 = 0x0401;
const MSG_CLOSEDS: u16 = 0x0402;
const MSG_DISABLEDS: u16 = 0x0501;
const MSG_ENABLEDS: u16 = 0x0502;
const MSG_PROCESSEVENT: u16 = 0x0601;
const MSG_ENDXFER: u16 = 0x0701;

const TWRC_SUCCESS: u16 = 0;
const TWRC_FAILURE: u16 = 1;
const TWRC_CANCEL: u16 = 3;
const TWRC_DSEVENT: u16 = 4;
const TWRC_NOTDSEVENT: u16 = 5;
const TWRC_XFERDONE: u16 = 6;
const TWRC_ENDOFLIST: u16 = 7;

const CAP_XFERCOUNT: u16 = 0x0001;
const CAP_DUPLEXENABLED: u16 = 0x1013;
const ICAP_PIXELTYPE: u16 = 0x0101;
const ICAP_XFERMECH: u16 = 0x0103;
const ICAP_IMAGEFILEFORMAT: u16 = 0x110c;
const ICAP_XRESOLUTION: u16 = 0x1118;
const ICAP_YRESOLUTION: u16 = 0x1119;

const TWON_ONEVALUE: u16 = 0x0005;
const TWTY_INT16: u16 = 0x0001;
const TWTY_UINT16: u16 = 0x0004;
const TWTY_BOOL: u16 = 0x0006;
const TWTY_FIX32: u16 = 0x0007;
const TWSX_FILE: u32 = 1;
const TWFF_TIFF: u16 = 0;
const ACQUISITION_TIMEOUT: Duration = Duration::from_secs(10 * 60);

#[repr(C, packed(2))]
#[derive(Clone, Copy)]
struct TwVersion {
    major_num: u16,
    minor_num: u16,
    language: u16,
    country: u16,
    info: [i8; 34],
}

#[repr(C, packed(2))]
#[derive(Clone, Copy)]
struct TwIdentity {
    id: u32,
    version: TwVersion,
    protocol_major: u16,
    protocol_minor: u16,
    supported_groups: u32,
    manufacturer: [i8; 34],
    product_family: [i8; 34],
    product_name: [i8; 34],
}

#[repr(C, packed(2))]
struct TwCapability {
    cap: u16,
    con_type: u16,
    container: HGLOBAL,
}

#[repr(C, packed(2))]
struct TwOneValue {
    item_type: u16,
    item: u32,
}

#[repr(C, packed(2))]
struct TwStatus {
    condition_code: u16,
    data: u16,
}

#[repr(C, packed(2))]
struct TwUserInterface {
    show_ui: u16,
    modal_ui: u16,
    parent: HWND,
}

#[repr(C, packed(2))]
struct TwEvent {
    event: *mut c_void,
    tw_message: u16,
}

#[repr(C, packed(2))]
struct TwPendingXfers {
    count: u16,
    eoj: u32,
}

#[repr(C, packed(2))]
struct TwSetupFileXfer {
    file_name: [i8; 256],
    format: u16,
    v_ref_num: i16,
}

struct TwainDsm {
    _library: Library,
    entry: DsmEntry,
}

impl TwainDsm {
    fn load() -> Result<Self, String> {
        let windir = std::env::var_os("WINDIR")
            .map(PathBuf::from)
            .ok_or_else(|| "WINDIR is unavailable; cannot locate the TWAIN DSM.".to_string())?;
        let candidates = [
            windir.join("System32").join("TWAINDSM.dll"),
            windir.join("System32").join("twain_32.dll"),
        ];
        let mut errors = Vec::new();
        for path in candidates {
            let library = match unsafe { Library::new(&path) } {
                Ok(value) => value,
                Err(err) => {
                    errors.push(format!("{} ({err})", path.display()));
                    continue;
                }
            };
            let entry = unsafe {
                match library.get::<DsmEntry>(b"DSM_Entry\0") {
                    Ok(symbol) => *symbol,
                    Err(err) => {
                        errors.push(format!("{} has no DSM_Entry ({err})", path.display()));
                        continue;
                    }
                }
            };
            return Ok(Self {
                _library: library,
                entry,
            });
        }
        Err(format!(
            "No compatible TWAIN Data Source Manager was found. Install a {}-bit TWAIN DSM and \
             scanner driver. Attempts: {}",
            usize::BITS,
            errors.join("; ")
        ))
    }
}

struct MessageWindow(HWND);

impl MessageWindow {
    fn create() -> Result<Self, String> {
        let class_name: Vec<u16> = "STATIC\0".encode_utf16().collect();
        let title: Vec<u16> = "DocuVision TWAIN Host\0".encode_utf16().collect();
        let hwnd = unsafe {
            CreateWindowExW(
                0,
                class_name.as_ptr(),
                title.as_ptr(),
                0,
                0,
                0,
                0,
                0,
                null_mut(),
                null_mut(),
                null_mut(),
                null(),
            )
        };
        if hwnd.is_null() {
            Err("Unable to create the hidden TWAIN message window.".to_string())
        } else {
            Ok(Self(hwnd))
        }
    }
}

impl Drop for MessageWindow {
    fn drop(&mut self) {
        unsafe {
            DestroyWindow(self.0);
        }
    }
}

struct TwainSession {
    dsm: TwainDsm,
    app: TwIdentity,
    source: Option<TwIdentity>,
    hwnd: HWND,
    dsm_open: bool,
    source_open: bool,
    source_enabled: bool,
}

impl TwainSession {
    fn open(hwnd: HWND) -> Result<Self, String> {
        let mut session = Self {
            dsm: TwainDsm::load()?,
            app: application_identity(),
            source: None,
            hwnd,
            dsm_open: false,
            source_open: false,
            source_enabled: false,
        };
        let mut parent = hwnd;
        session.call_manager(
            DG_CONTROL,
            DAT_PARENT,
            MSG_OPENDSM,
            (&mut parent as *mut HWND).cast(),
            "open the TWAIN Data Source Manager",
        )?;
        session.dsm_open = true;
        Ok(session)
    }

    fn source_identities(&mut self) -> Result<Vec<TwIdentity>, String> {
        let mut sources = Vec::new();
        let mut source: TwIdentity = unsafe { zeroed() };
        let mut message = MSG_GETFIRST;
        loop {
            let rc = unsafe {
                (self.dsm.entry)(
                    &mut self.app,
                    null_mut(),
                    DG_CONTROL,
                    DAT_IDENTITY,
                    message,
                    (&mut source as *mut TwIdentity).cast(),
                )
            };
            match rc {
                TWRC_SUCCESS => {
                    sources.push(source);
                    source = unsafe { zeroed() };
                    message = MSG_GETNEXT;
                }
                TWRC_ENDOFLIST => break,
                _ => return Err(self.error_for("enumerate TWAIN scanner sources", rc, false)),
            }
        }
        Ok(sources)
    }

    fn open_source(&mut self, mut source: TwIdentity) -> Result<(), String> {
        self.call_manager(
            DG_CONTROL,
            DAT_IDENTITY,
            MSG_OPENDS,
            (&mut source as *mut TwIdentity).cast(),
            "open the selected TWAIN scanner",
        )?;
        self.source = Some(source);
        self.source_open = true;
        Ok(())
    }

    fn set_capability(&mut self, cap_id: u16, item_type: u16, item: u32) -> Result<(), String> {
        let handle = unsafe { GlobalAlloc(GMEM_MOVEABLE, size_of::<TwOneValue>()) };
        if handle.is_null() {
            return Err("Windows could not allocate a TWAIN capability container.".to_string());
        }
        let pointer = unsafe { GlobalLock(handle) } as *mut TwOneValue;
        if pointer.is_null() {
            unsafe {
                GlobalFree(handle);
            }
            return Err("Windows could not lock a TWAIN capability container.".to_string());
        }
        unsafe {
            pointer.write_unaligned(TwOneValue { item_type, item });
            let _ = GlobalUnlock(handle);
        }
        let mut capability = TwCapability {
            cap: cap_id,
            con_type: TWON_ONEVALUE,
            container: handle,
        };
        let result = self.call_source(
            DG_CONTROL,
            DAT_CAPABILITY,
            MSG_SET,
            (&mut capability as *mut TwCapability).cast(),
            &format!("set scanner capability 0x{cap_id:04x}"),
        );
        unsafe {
            GlobalFree(handle);
        }
        result
    }

    fn enable_source(&mut self) -> Result<(), String> {
        let mut ui = TwUserInterface {
            show_ui: 0,
            modal_ui: 0,
            parent: self.hwnd,
        };
        self.call_source(
            DG_CONTROL,
            DAT_USERINTERFACE,
            MSG_ENABLEDS,
            (&mut ui as *mut TwUserInterface).cast(),
            "enable the selected TWAIN scanner",
        )?;
        self.source_enabled = true;
        Ok(())
    }

    fn setup_file_transfer(&mut self, path: &Path) -> Result<(), String> {
        let encoded = path.to_string_lossy();
        if !encoded.is_ascii() || encoded.len() >= 256 {
            return Err(format!(
                "TWAIN file-transfer paths must be ANSI and shorter than 256 bytes: {}",
                path.display()
            ));
        }
        let mut transfer = TwSetupFileXfer {
            file_name: [0; 256],
            format: TWFF_TIFF,
            v_ref_num: 0,
        };
        for (destination, source) in transfer.file_name.iter_mut().zip(encoded.bytes()) {
            *destination = source as i8;
        }
        self.call_source(
            DG_CONTROL,
            DAT_SETUPFILEXFER,
            MSG_SET,
            (&mut transfer as *mut TwSetupFileXfer).cast(),
            "configure the TWAIN page output file",
        )
    }

    fn transfer_pages(
        &mut self,
        scan_dir: &Path,
        max_pages: u16,
        on_page: &mut impl FnMut(usize),
    ) -> Result<Vec<PathBuf>, String> {
        let mut pages = Vec::new();
        loop {
            if pages.len() >= usize::from(max_pages) {
                let mut pending: TwPendingXfers = unsafe { zeroed() };
                self.call_source(
                    DG_CONTROL,
                    DAT_PENDINGXFERS,
                    MSG_RESET,
                    (&mut pending as *mut TwPendingXfers).cast(),
                    "stop the TWAIN feeder at the configured page limit",
                )?;
                break;
            }

            let path = scan_dir.join(format!("raw-page-{:04}.tif", pages.len() + 1));
            self.setup_file_transfer(&path)?;
            let rc = unsafe {
                let source = self.source.as_mut().expect("source is open");
                (self.dsm.entry)(
                    &mut self.app,
                    source,
                    DG_IMAGE,
                    DAT_IMAGEFILEXFER,
                    MSG_GET,
                    null_mut(),
                )
            };
            if rc == TWRC_CANCEL {
                break;
            }
            if rc != TWRC_XFERDONE {
                return Err(self.error_for("transfer a page from the TWAIN scanner", rc, true));
            }
            if !path.is_file() {
                return Err(format!(
                    "The TWAIN driver reported a completed transfer but did not create {}.",
                    path.display()
                ));
            }
            pages.push(path);

            let mut pending: TwPendingXfers = unsafe { zeroed() };
            self.call_source(
                DG_CONTROL,
                DAT_PENDINGXFERS,
                MSG_ENDXFER,
                (&mut pending as *mut TwPendingXfers).cast(),
                "finish the TWAIN page transfer",
            )?;
            on_page(pages.len());
            let count = pending.count;
            if count == 0 {
                break;
            }
        }
        Ok(pages)
    }

    fn wait_for_transfer(
        &mut self,
        scan_dir: &Path,
        max_pages: u16,
        on_page: &mut impl FnMut(usize),
    ) -> Result<Vec<PathBuf>, String> {
        let started = Instant::now();
        loop {
            if started.elapsed() > ACQUISITION_TIMEOUT {
                return Err("Timed out waiting for the scanner to acquire a page.".to_string());
            }
            let mut message: MSG = unsafe { zeroed() };
            let has_message =
                unsafe { PeekMessageW(&mut message, null_mut(), 0, 0, PM_REMOVE) } != 0;
            if !has_message {
                thread::sleep(Duration::from_millis(10));
                continue;
            }

            let mut event = TwEvent {
                event: (&mut message as *mut MSG).cast(),
                tw_message: 0,
            };
            let rc = unsafe {
                let source = self.source.as_mut().expect("source is open");
                (self.dsm.entry)(
                    &mut self.app,
                    source,
                    DG_CONTROL,
                    DAT_EVENT,
                    MSG_PROCESSEVENT,
                    (&mut event as *mut TwEvent).cast(),
                )
            };
            if rc == TWRC_DSEVENT {
                let tw_message = event.tw_message;
                match tw_message {
                    MSG_XFERREADY => {
                        return self.transfer_pages(scan_dir, max_pages, on_page);
                    }
                    MSG_CLOSEDSREQ | MSG_CLOSEDSOK => {
                        return Err("The scanner closed before transferring a page.".to_string())
                    }
                    _ => {}
                }
            } else if rc == TWRC_NOTDSEVENT {
                unsafe {
                    TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            } else if rc == TWRC_FAILURE {
                return Err(self.error_for("process a TWAIN scanner event", rc, true));
            }
        }
    }

    fn call_manager(
        &mut self,
        dg: u32,
        dat: u16,
        message: u16,
        data: *mut c_void,
        action: &str,
    ) -> Result<(), String> {
        let rc = unsafe { (self.dsm.entry)(&mut self.app, null_mut(), dg, dat, message, data) };
        if rc == TWRC_SUCCESS {
            Ok(())
        } else {
            Err(self.error_for(action, rc, false))
        }
    }

    fn call_source(
        &mut self,
        dg: u32,
        dat: u16,
        message: u16,
        data: *mut c_void,
        action: &str,
    ) -> Result<(), String> {
        let rc = unsafe {
            let source = self.source.as_mut().expect("source is open");
            (self.dsm.entry)(&mut self.app, source, dg, dat, message, data)
        };
        if rc == TWRC_SUCCESS {
            Ok(())
        } else {
            Err(self.error_for(action, rc, true))
        }
    }

    fn error_for(&mut self, action: &str, rc: u16, with_source: bool) -> String {
        let mut status = TwStatus {
            condition_code: 0,
            data: 0,
        };
        let destination = if with_source {
            self.source
                .as_mut()
                .map(|source| source as *mut TwIdentity)
                .unwrap_or(null_mut())
        } else {
            null_mut()
        };
        let status_rc = unsafe {
            (self.dsm.entry)(
                &mut self.app,
                destination,
                DG_CONTROL,
                DAT_STATUS,
                MSG_GET,
                (&mut status as *mut TwStatus).cast(),
            )
        };
        if status_rc == TWRC_SUCCESS {
            let condition = status.condition_code;
            format!(
                "Unable to {action} (TWAIN return {rc}, condition {condition}: {}).",
                condition_name(condition)
            )
        } else {
            format!("Unable to {action} (TWAIN return {rc}).")
        }
    }
}

impl Drop for TwainSession {
    fn drop(&mut self) {
        unsafe {
            if self.source_enabled {
                let mut ui = TwUserInterface {
                    show_ui: 0,
                    modal_ui: 0,
                    parent: self.hwnd,
                };
                if let Some(source) = self.source.as_mut() {
                    let _ = (self.dsm.entry)(
                        &mut self.app,
                        source,
                        DG_CONTROL,
                        DAT_USERINTERFACE,
                        MSG_DISABLEDS,
                        (&mut ui as *mut TwUserInterface).cast(),
                    );
                }
            }
            if self.source_open {
                if let Some(source) = self.source.as_mut() {
                    let _ = (self.dsm.entry)(
                        &mut self.app,
                        null_mut(),
                        DG_CONTROL,
                        DAT_IDENTITY,
                        MSG_CLOSEDS,
                        (source as *mut TwIdentity).cast(),
                    );
                }
            }
            if self.dsm_open {
                let mut parent = self.hwnd;
                let _ = (self.dsm.entry)(
                    &mut self.app,
                    null_mut(),
                    DG_CONTROL,
                    DAT_PARENT,
                    MSG_CLOSEDSM,
                    (&mut parent as *mut HWND).cast(),
                );
            }
        }
    }
}

pub fn list_sources() -> Result<Vec<String>, String> {
    let window = MessageWindow::create()?;
    let mut session = TwainSession::open(window.0)?;
    session
        .source_identities()?
        .iter()
        .map(|identity| twain_string(&identity.product_name))
        .filter(|name| !name.is_empty())
        .collect::<Vec<_>>()
        .pipe(Ok)
}

pub fn acquire(
    config: &ScanConfig,
    scan_dir: &Path,
    on_page: &mut impl FnMut(usize),
) -> Result<Vec<PathBuf>, String> {
    let window = MessageWindow::create()?;
    let mut session = TwainSession::open(window.0)?;
    let selected = session
        .source_identities()?
        .into_iter()
        .find(|identity| twain_string(&identity.product_name) == config.scanner_name)
        .ok_or_else(|| {
            format!(
                "Scanner '{}' is no longer available. Refresh the scanner list.",
                config.scanner_name
            )
        })?;
    session.open_source(selected)?;

    session.set_capability(CAP_XFERCOUNT, TWTY_INT16, u32::from(config.max_pages))?;
    session.set_capability(ICAP_XFERMECH, TWTY_UINT16, TWSX_FILE)?;
    session.set_capability(ICAP_IMAGEFILEFORMAT, TWTY_UINT16, u32::from(TWFF_TIFF))?;
    session.set_capability(
        ICAP_PIXELTYPE,
        TWTY_UINT16,
        match config.pixel_type {
            PixelType::BlackAndWhite => 0,
            PixelType::Grayscale => 1,
            PixelType::Rgb => 2,
        },
    )?;
    session.set_capability(ICAP_XRESOLUTION, TWTY_FIX32, u32::from(config.dpi))?;
    session.set_capability(ICAP_YRESOLUTION, TWTY_FIX32, u32::from(config.dpi))?;
    session.set_capability(
        CAP_DUPLEXENABLED,
        TWTY_BOOL,
        if config.duplex { 1 } else { 0 },
    )?;
    session.enable_source()?;
    session.wait_for_transfer(scan_dir, config.max_pages, on_page)
}

fn application_identity() -> TwIdentity {
    let mut identity: TwIdentity = unsafe { zeroed() };
    identity.version.major_num = 1;
    identity.version.minor_num = 0;
    identity.version.language = 13; // TWLG_USA
    identity.version.country = 1; // TWCY_USA
    identity.protocol_major = 2;
    identity.protocol_minor = 4;
    identity.supported_groups = DG_CONTROL | DG_IMAGE;
    write_twain_string(&mut identity.version.info, "DocuVision 0.1");
    write_twain_string(&mut identity.manufacturer, "DocuVision");
    write_twain_string(&mut identity.product_family, "Document ingestion");
    write_twain_string(&mut identity.product_name, "DocuVision");
    identity
}

fn write_twain_string(destination: &mut [i8], value: &str) {
    let maximum_length = destination.len().saturating_sub(1);
    for (slot, byte) in destination
        .iter_mut()
        .take(maximum_length)
        .zip(value.bytes())
    {
        *slot = byte as i8;
    }
}

fn twain_string(value: &[i8]) -> String {
    let bytes = value
        .iter()
        .take_while(|byte| **byte != 0)
        .map(|byte| *byte as u8)
        .collect::<Vec<_>>();
    String::from_utf8_lossy(&bytes).trim().to_string()
}

fn condition_name(condition: u16) -> &'static str {
    match condition {
        0 => "success",
        1 => "unspecified driver error",
        2 => "insufficient memory",
        3 => "scanner not found or disconnected",
        4 => "scanner connection limit reached",
        5 => "scanner operation error",
        6 => "invalid capability",
        9 => "unsupported protocol operation",
        10 => "unsupported capability value",
        11 => "invalid TWAIN state sequence",
        12 => "invalid destination",
        13 => "capability unsupported by this scanner",
        14 => "capability operation unsupported",
        15 => "capability cannot be changed in the current state",
        16 => "access denied",
        17 => "output file already exists",
        18 => "output file not found",
        19 => "output directory is not empty",
        20 => "paper jam",
        21 => "double feed detected",
        22 => "output file write error",
        23 => "scanner offline or disconnected",
        24 => "scanner cover or interlock is open",
        25 => "damaged page corner detected",
        26 => "focus error",
        27 => "document is too light",
        28 => "document is too dark",
        29 => "feeder has no paper",
        _ => "unknown TWAIN condition",
    }
}

trait Pipe: Sized {
    fn pipe<T>(self, function: impl FnOnce(Self) -> T) -> T {
        function(self)
    }
}

impl<T> Pipe for T {}
