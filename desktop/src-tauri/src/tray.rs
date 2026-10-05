//! The tray icon: its picture says how the service is (paper "O" = up, yellow = starting,
//! red = not answering, red "!" = gave up), its tooltip and first menu line say it in words.
//! Icons are drawn by `desktop/scripts/make-icons.mjs` and compiled in.

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::{AppHandle, Wry};

use crate::autostart::Legacy;
use crate::log::epoch_ms;
use crate::service::Snapshot;
use crate::supervisor::Phase;

pub const TRAY_ID: &str = "omniapi-tray";

pub mod ids {
    pub const STATUS: &str = "status";
    pub const OPEN: &str = "open";
    pub const RESTART: &str = "restart";
    pub const AUTOSTART: &str = "autostart";
    pub const LEGACY: &str = "legacy-autostart";
    pub const LOGS: &str = "logs";
    pub const QUIT: &str = "quit";
    pub const UPDATE: &str = "update";
}

/// The menu line for a newer version (1.3-M6, D45 fallback): clicking it opens the release page.
pub fn update_text(version: &str) -> String {
    format!("有新版 v{version}（開啟下載頁）")
}

static UP: &[u8] = include_bytes!("../icons/tray/up.png");
static STARTING: &[u8] = include_bytes!("../icons/tray/starting.png");
static DOWN: &[u8] = include_bytes!("../icons/tray/down.png");
static FAILED: &[u8] = include_bytes!("../icons/tray/failed.png");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Look {
    Up,
    Starting,
    Down,
    Failed,
}

pub fn look(phase: Phase) -> Look {
    match phase {
        Phase::Up => Look::Up,
        Phase::Starting | Phase::Stopping | Phase::Disabled => Look::Starting,
        Phase::Down => Look::Down,
        Phase::Failed => Look::Failed,
    }
}

pub fn icon(l: Look) -> Image<'static> {
    let bytes = match l {
        Look::Up => UP,
        Look::Starting => STARTING,
        Look::Down => DOWN,
        Look::Failed => FAILED,
    };
    Image::from_bytes(bytes).expect("tray icon png")
}

/// The status in words: the menu's first line (and the tooltip after "OmniAPI — ").
pub fn status_text(s: &Snapshot, now_ms: u64) -> String {
    match s.phase {
        Phase::Up => {
            let how = if s.adopted { "接手既有的服務" } else { "由桌面版啟動" };
            format!("服務運作中（埠 {}，{how}）", s.port)
        }
        Phase::Starting => match s.starting_since_ms {
            Some(t) => format!("服務啟動中…已等 {} 秒", now_ms.saturating_sub(t) / 1000),
            None => "服務啟動中…".into(),
        },
        Phase::Down => match s.retry_in_secs {
            Some(0) | None if s.misses == 0 => "服務沒有回應，正在重新啟動".into(),
            Some(n) if n > 0 => format!("服務沒有回應，{n} 秒後重新啟動（第 {} 次）", s.failures),
            _ => format!("服務沒有回應（{}/{}）", s.misses, s.miss_threshold),
        },
        Phase::Failed => format!("服務起不來，已停止重試：{}", s.problem.as_deref().unwrap_or("原因不明")),
        Phase::Stopping => "正在停止服務…".into(),
        Phase::Disabled => format!("不管理服務（設定檔停用）；埠 {}", s.port),
    }
}

/// Windows cuts tray tooltips at 127 characters.
pub fn tooltip(status: &str) -> String {
    let t = format!("OmniAPI — {status}");
    if t.chars().count() <= 120 {
        t
    } else {
        t.chars().take(119).collect::<String>() + "…"
    }
}

/// Menu items that change after the menu is built. `autostart_on` is what the check mark was
/// built with: the settings page can switch the logon entry too, so the shell compares it with
/// the registry (pointer on the tray icon, and every few seconds) and rebuilds when they differ.
pub struct Items {
    pub status: MenuItem<Wry>,
    pub autostart_on: bool,
}

/// Does the menu need rebuilding to show the logon entry as it is now?
pub fn autostart_stale(shown: Option<bool>, actual: bool) -> bool {
    shown != Some(actual)
}

/// Build the menu. The legacy-autostart line is only there when both the shell's own autostart
/// and an old launcher are on (`legacy_hint`), so the menu is rebuilt when that changes.
pub fn build_menu(app: &AppHandle, autostart_on: bool, legacy_hint: Option<&Legacy>, update: Option<&str>) -> tauri::Result<(Menu<Wry>, Items)> {
    let status = MenuItem::with_id(app, ids::STATUS, "服務狀態…", false, None::<&str>)?;
    let menu = Menu::new(app)?;
    menu.append(&status)?;
    if let Some(v) = update {
        let item = MenuItem::with_id(app, ids::UPDATE, update_text(v), true, None::<&str>)?;
        menu.append(&item)?;
    }
    let open = MenuItem::with_id(app, ids::OPEN, "開啟看板", true, None::<&str>)?;
    let restart = MenuItem::with_id(app, ids::RESTART, "重啟服務", true, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(app, ids::AUTOSTART, "開機時啟動", true, autostart_on, None::<&str>)?;
    let logs = MenuItem::with_id(app, ids::LOGS, "開啟紀錄資料夾", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, ids::QUIT, "結束", true, None::<&str>)?;
    let s1 = PredefinedMenuItem::separator(app)?;
    let s2 = PredefinedMenuItem::separator(app)?;
    let s3 = PredefinedMenuItem::separator(app)?;
    menu.append_items(&[&s1, &open, &restart, &s2, &autostart])?;
    if let Some(l) = legacy_hint.filter(|l| l.any()) {
        let hint = MenuItem::with_id(app, ids::LEGACY, l.menu_text(), l.startup_vbs.is_some(), None::<&str>)?;
        menu.append(&hint)?;
    }
    menu.append_items(&[&logs, &s3, &quit])?;
    Ok((menu, Items { status, autostart_on }))
}

pub fn refresh(app: &AppHandle, items: &Items, s: &Snapshot) {
    let text = status_text(s, epoch_ms());
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_icon(Some(icon(look(s.phase))));
        let _ = tray.set_tooltip(Some(tooltip(&text)));
    }
    let _ = items.status.set_text(&text);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(phase: Phase) -> Snapshot {
        Snapshot {
            phase,
            port: 7826,
            pid: Some(1),
            adopted: false,
            failures: 0,
            max_failures: 5,
            misses: 0,
            miss_threshold: 3,
            restarts: 0,
            problem: None,
            retry_in_secs: None,
            starting_since_ms: None,
        }
    }

    #[test]
    fn every_state_has_words() {
        assert_eq!(status_text(&snap(Phase::Up), 0), "服務運作中（埠 7826，由桌面版啟動）");
        assert!(status_text(&Snapshot { adopted: true, ..snap(Phase::Up) }, 0).contains("接手"));
        assert_eq!(status_text(&Snapshot { starting_since_ms: Some(1_000), ..snap(Phase::Starting) }, 13_500), "服務啟動中…已等 12 秒");
        assert_eq!(status_text(&Snapshot { misses: 1, ..snap(Phase::Down) }, 0), "服務沒有回應（1/3）");
        assert_eq!(status_text(&Snapshot { failures: 2, retry_in_secs: Some(5), ..snap(Phase::Down) }, 0), "服務沒有回應，5 秒後重新啟動（第 2 次）");
        assert_eq!(status_text(&Snapshot { failures: 1, retry_in_secs: Some(0), ..snap(Phase::Down) }, 0), "服務沒有回應，正在重新啟動");
        assert!(status_text(&Snapshot { problem: Some("找不到檔案".into()), ..snap(Phase::Failed) }, 0).ends_with("找不到檔案"));
        assert!(status_text(&snap(Phase::Stopping), 0).contains("停止"));
        assert!(status_text(&snap(Phase::Disabled), 0).contains("不管理"));
    }

    #[test]
    fn looks_follow_the_phase() {
        assert_eq!(look(Phase::Up), Look::Up);
        assert_eq!(look(Phase::Starting), Look::Starting);
        assert_eq!(look(Phase::Down), Look::Down);
        assert_eq!(look(Phase::Failed), Look::Failed);
    }

    #[test]
    fn icons_decode() {
        for l in [Look::Up, Look::Starting, Look::Down, Look::Failed] {
            let i = icon(l);
            assert_eq!((i.width(), i.height()), (32, 32));
        }
    }

    #[test]
    fn autostart_check_mark_follows_the_registry() {
        assert!(!autostart_stale(Some(true), true));
        assert!(!autostart_stale(Some(false), false));
        assert!(autostart_stale(Some(false), true), "turned on from the settings page");
        assert!(autostart_stale(Some(true), false), "turned off from the settings page");
        assert!(autostart_stale(None, false), "no menu yet");
    }

    #[test]
    fn update_line() {
        assert_eq!(update_text("1.3.0"), "有新版 v1.3.0（開啟下載頁）");
    }

    #[test]
    fn tooltip_fits_windows_limit() {
        let long = "很".repeat(300);
        assert!(tooltip(&long).chars().count() <= 120);
        assert_eq!(tooltip("ok"), "OmniAPI — ok");
    }
}
