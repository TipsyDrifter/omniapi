//! Start at logon, without starting the service twice.
//!
//! The shell's own entry comes from tauri-plugin-autostart: a value named `OmniAPI` under
//! `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` running `<exe> --background` (tray and
//! service only, no window). `omni autostart status` reads the same value and reports that the
//! desktop app has taken over; `omni autostart install` refuses while it is on.
//!
//! The older launchers that `omni autostart install` made — `OmniAPI Daemon.vbs` in the Startup
//! folder, or the scheduled task `OmniAPI Daemon` — are only detected, never removed: they are
//! the user's, and with both on nothing breaks (whichever starts second finds the service
//! answering and adopts it), it is just one launcher too many. So the shell says so in the log
//! and in the tray menu, and points at `omni autostart remove`.

use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// `OmniAPI` (the test build: `OmniAPI-Test`, see identity.rs).
pub const RUN_VALUE: &str = crate::identity::RUN_VALUE;
/// What the logon entry passes: start in the tray, do not open the window.
pub const ARG: &str = "--background";
pub const LEGACY_VBS: &str = "OmniAPI Daemon.vbs";
pub const LEGACY_TASK: &str = "OmniAPI Daemon";

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Legacy {
    /// The Startup-folder launcher, when it exists.
    pub startup_vbs: Option<PathBuf>,
    /// Whether the scheduled task exists.
    pub task: bool,
}

impl Legacy {
    pub fn any(&self) -> bool {
        self.startup_vbs.is_some() || self.task
    }

    /// One line for the tray menu (empty when there is nothing).
    pub fn menu_text(&self) -> String {
        match (&self.startup_vbs, self.task) {
            (Some(_), true) => "⚠ 舊的開機啟動也還在（啟動資料夾與工作排程）：可用 omni autostart remove 移除".into(),
            (Some(_), false) => "⚠ 舊的開機啟動也還在（啟動資料夾的 OmniAPI Daemon.vbs）：點此打開資料夾".into(),
            (None, true) => "⚠ 舊的開機啟動也還在（工作排程 OmniAPI Daemon）：可用 omni autostart remove 移除".into(),
            (None, false) => String::new(),
        }
    }

    pub fn describe(&self) -> String {
        let mut parts = Vec::new();
        if let Some(p) = &self.startup_vbs {
            parts.push(format!("startup-folder launcher {}", p.display()));
        }
        if self.task {
            parts.push(format!("scheduled task '{LEGACY_TASK}'"));
        }
        if parts.is_empty() {
            "none".into()
        } else {
            parts.join(" + ")
        }
    }
}

/// `HKCU\...\Run`, the key the plugin writes to.
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

/// Exactly the text tauri-plugin-autostart 2.5.1 (auto-launch 0.5.0) writes into the Run value
/// for the running exe: `format!("{} {}", current_exe().display(), args.join(" "))` -- the exe
/// path unquoted, one space, `--background`. The installer's post-install hook writes the same
/// text for `$INSTDIR\<exe>`.
pub fn my_command() -> Option<String> {
    std::env::current_exe().ok().map(|e| format!("{} {}", e.display(), ARG))
}

/// The Run value of this identity (`OmniAPI` / `OmniAPI-Test`) as it is now, `None` when there is
/// none. The plugin only says enabled / not enabled; the restore logic also needs to know whether
/// a value that is there points at an exe that still exists.
pub fn read_run_value() -> Option<String> {
    use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_CURRENT_USER, RRF_NOEXPAND, RRF_RT_REG_EXPAND_SZ, RRF_RT_REG_SZ};
    let wide = |s: &str| -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() };
    let (key, name) = (wide(RUN_KEY), wide(RUN_VALUE));
    let flags = RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND;
    let mut bytes: u32 = 0;
    // first call: how big is it
    let rc = unsafe { RegGetValueW(HKEY_CURRENT_USER, key.as_ptr(), name.as_ptr(), flags, std::ptr::null_mut(), std::ptr::null_mut(), &mut bytes) };
    if rc != 0 || bytes == 0 {
        return None;
    }
    let mut buf = vec![0u16; bytes as usize / 2 + 2];
    let mut size = (buf.len() * 2) as u32;
    let rc = unsafe { RegGetValueW(HKEY_CURRENT_USER, key.as_ptr(), name.as_ptr(), flags, std::ptr::null_mut(), buf.as_mut_ptr().cast(), &mut size) };
    if rc != 0 {
        return None;
    }
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    Some(String::from_utf16_lossy(&buf[..len]))
}

/// The user's Startup folder, from `%APPDATA%` (the same rule `omni autostart` uses).
pub fn startup_dir(appdata: &str) -> PathBuf {
    Path::new(appdata).join("Microsoft").join("Windows").join("Start Menu").join("Programs").join("Startup")
}

/// Look for the old launchers. Runs `schtasks /Query` (hidden, ~0.1 s).
pub fn detect() -> Legacy {
    let vbs = std::env::var("APPDATA").ok().map(|a| startup_dir(&a).join(LEGACY_VBS)).filter(|p| p.is_file());
    let task = Command::new("schtasks")
        .args(["/Query", "/TN", LEGACY_TASK])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(0x0800_0000 /* CREATE_NO_WINDOW */)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    Legacy { startup_vbs: vbs, task }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_folder_matches_omni_autostart() {
        // mcp/omniapi_mcp/cli.py: _startup_dir() = APPDATA\Microsoft\Windows\Start Menu\Programs\Startup
        assert_eq!(
            startup_dir(r"C:\Users\u\AppData\Roaming"),
            PathBuf::from(r"C:\Users\u\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup")
        );
    }

    #[test]
    fn menu_text_names_what_was_found() {
        assert_eq!(Legacy::default().menu_text(), "");
        assert!(!Legacy::default().any());
        let vbs = Legacy { startup_vbs: Some(PathBuf::from("x.vbs")), task: false };
        assert!(vbs.any() && vbs.menu_text().contains("OmniAPI Daemon.vbs"));
        let task = Legacy { startup_vbs: None, task: true };
        assert!(task.menu_text().contains("omni autostart remove"));
        assert!(Legacy { startup_vbs: Some(PathBuf::from("x.vbs")), task: true }.describe().contains(" + "));
    }

    #[test]
    fn my_command_is_the_text_the_plugin_writes() {
        // tauri-plugin-autostart 2.5.1: set_app_path(&current_exe()?.display().to_string()), then
        // auto-launch 0.5.0 enable(): format!("{} {}", app_path, args.join(" "))
        let exe = std::env::current_exe().unwrap();
        assert_eq!(my_command().unwrap(), format!("{} --background", exe.display()));
        assert!(!my_command().unwrap().starts_with('"'), "unquoted, as the plugin writes it");
    }

    #[test]
    fn reading_the_run_value_does_not_panic_whatever_is_there() {
        // the value may or may not exist on the machine running the tests
        let _ = read_run_value();
    }

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn run_value_is_what_the_cli_reads() {
        // mcp/omniapi_mcp/cli.py: DESKTOP_RUN_VALUE = "OmniAPI"
        assert_eq!(RUN_VALUE, "OmniAPI");
    }
}
