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

pub const RUN_VALUE: &str = "OmniAPI";
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
    fn run_value_is_what_the_cli_reads() {
        // mcp/omniapi_mcp/cli.py: DESKTOP_RUN_VALUE = "OmniAPI"
        assert_eq!(RUN_VALUE, "OmniAPI");
    }
}
