//! The one window, built when needed and destroyed by X (every live window keeps a WebView2
//! renderer alive; toolchain.md Tauri #4). It loads the service itself (1.3-a); while the service
//! is not up it shows the local starting page (`desktop/ui/index.html`), which the shell keeps
//! informed with `eval` and sends on to the service once it answers.
//!
//! Every entry point (tray click, menu, `--open` from a second launch) goes through [`show`].
//! Callers must NOT be on the main thread's event callback — spawn a thread first, or the
//! WebView2 creation deadlocks. Creation is serialised with a lock (two quick clicks must not
//! both try to create the same label).

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{AppHandle, Manager,PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use crate::winstate::{self, Area, Geometry};

pub const LABEL: &str = "main";
pub const TITLE: &str = "OmniAPI";
/// Smallest window: phone width (the site becomes responsive in 1.3-M3; until then it scrolls).
pub const MIN_SIZE: (f64, f64) = (360.0, 480.0);
/// The dashboard is laid out for 1440 px.
const DEFAULT_SIZE: (f64, f64) = (1440.0, 900.0);

/// Replaces wry's defaults wholesale, so they are copied in; `--disable-gpu` saves the GPU
/// process (ClaudeUsageMonitor D92). Every webview of the app must use the identical string.
pub const BROWSER_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required --disable-gpu";

static CREATE_LOCK: Mutex<()> = Mutex::new(());

pub struct Open<'a> {
    pub port: u16,
    pub service_url: &'a str,
    pub service_up: bool,
    /// The service status as JSON, handed to the starting page before its scripts run.
    pub status_json: String,
    pub config_dir: Option<PathBuf>,
}

/// Bring the window up (create it when there is none). Returns true when it was created.
pub fn show(app: &AppHandle, o: Open) -> tauri::Result<bool> {
    let (w, created) = {
        let _g = CREATE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        match app.get_webview_window(LABEL) {
            Some(w) => (w, false),
            None => (create(app, &o)?, true),
        }
    };
    w.show()?;
    let _ = w.unminimize();
    let _ = w.set_focus();
    Ok(created)
}

fn create(app: &AppHandle, o: &Open) -> tauri::Result<WebviewWindow> {
    let url = if o.service_up {
        WebviewUrl::External(o.service_url.parse().expect("service url"))
    } else {
        WebviewUrl::App("index.html".into())
    };
    let (w0, h0) = default_size(app);
    let port = o.port;
    let w = WebviewWindowBuilder::new(app, LABEL, url)
        .title(TITLE)
        .inner_size(w0, h0)
        .min_inner_size(MIN_SIZE.0, MIN_SIZE.1)
        .additional_browser_args(BROWSER_ARGS)
        .initialization_script(&format!("window.__OMNI_SHELL__ = {};", o.status_json))
        // The window shows the service and the starting page, nothing else: other sites go to
        // the user's browser, and so does anything a page opens in a new window.
        .on_navigation(move |u| {
            let ours = is_service_url(u, port) || u.host_str() == Some("tauri.localhost");
            if !ours {
                open_in_browser(u);
            }
            ours
        })
        .on_new_window(|u, _features| {
            open_in_browser(&u);
            tauri::webview::NewWindowResponse::Deny
        })
        .center()
        .visible(false)
        .build()?;
    if let Some(saved) = o.config_dir.as_deref().and_then(winstate::load) {
        let monitors: Vec<Area> = w
            .available_monitors()
            .unwrap_or_default()
            .iter()
            .map(|m| {
                let a = m.work_area();
                Area { x: a.position.x, y: a.position.y, width: a.size.width, height: a.size.height }
            })
            .collect();
        let sf = w.scale_factor().unwrap_or(1.0);
        let min = ((MIN_SIZE.0 * sf) as u32, (MIN_SIZE.1 * sf) as u32);
        if let Some(g) = winstate::usable(saved, &monitors, min) {
            let _ = w.set_size(PhysicalSize::new(g.width, g.height));
            let _ = w.set_position(PhysicalPosition::new(g.x, g.y));
            if g.maximized {
                let _ = w.maximize();
            }
        }
    }
    Ok(w)
}

/// 1440×900, or less on a small screen.
fn default_size(app: &AppHandle) -> (f64, f64) {
    let Ok(Some(m)) = app.primary_monitor() else { return DEFAULT_SIZE };
    let sf = m.scale_factor();
    let a = m.work_area().size;
    let (mw, mh) = (a.width as f64 / sf, a.height as f64 / sf);
    (DEFAULT_SIZE.0.min(mw * 0.92).max(MIN_SIZE.0), DEFAULT_SIZE.1.min(mh * 0.9).max(MIN_SIZE.1))
}

/// Remember where the window is (called when it is about to close, and on quit).
pub fn remember(w: &WebviewWindow, config_dir: &std::path::Path) {
    let maximized = w.is_maximized().unwrap_or(false);
    let g = if maximized {
        // a maximised window's own rect is the screen; keep the last normal one under it
        let prev = winstate::load(config_dir);
        match prev {
            Some(p) => Geometry { maximized: true, ..p },
            None => return,
        }
    } else {
        let (Ok(pos), Ok(size)) = (w.outer_position(), w.inner_size()) else { return };
        if w.is_minimized().unwrap_or(false) {
            return; // minimised windows report a far-off position
        }
        Geometry { x: pos.x, y: pos.y, width: size.width, height: size.height, maximized: false }
    };
    winstate::save(config_dir, &g);
}

/// Open an http(s) link in the user's default browser; other schemes are dropped.
pub fn open_in_browser(url: &tauri::Url) {
    if matches!(url.scheme(), "http" | "https") {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("rundll32.exe")
            .args(["url.dll,FileProtocolHandler", url.as_str()])
            .creation_flags(0x0800_0000 /* CREATE_NO_WINDOW */)
            .spawn();
    }
}

/// Whether a window URL is the service (as opposed to the local starting page, which is
/// `http://tauri.localhost/...` — also `http`, so the scheme does not tell them apart).
pub fn is_service_url(url: &tauri::Url, port: u16) -> bool {
    url.host_str() == Some("127.0.0.1") && url.port() == Some(port)
}

fn on_starting_page(w: &WebviewWindow, port: u16) -> bool {
    w.url().map(|u| !is_service_url(&u, port)).unwrap_or(false)
}

/// The service came up: if the window is still on the starting page, send it to the service.
pub fn go_to_service_if_waiting(app: &AppHandle, service_url: &str, port: u16) -> bool {
    let Some(w) = app.get_webview_window(LABEL) else { return false };
    if !on_starting_page(&w, port) {
        return false;
    }
    match service_url.parse() {
        Ok(u) => w.navigate(u).is_ok(),
        Err(_) => false,
    }
}

/// Tell the starting page (only that page — never the dashboard) the latest status.
pub fn push_status(app: &AppHandle, status_json: &str, port: u16) {
    let Some(w) = app.get_webview_window(LABEL) else { return };
    if on_starting_page(&w, port) {
        let _ = w.eval(&format!("window.omniShell && window.omniShell.update({status_json});"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_url_is_told_apart_from_the_starting_page() {
        let u = |s: &str| s.parse::<tauri::Url>().unwrap();
        assert!(is_service_url(&u("http://127.0.0.1:7826/runs/1"), 7826));
        assert!(!is_service_url(&u("http://127.0.0.1:7788/"), 7826), "another port is not our service");
        assert!(!is_service_url(&u("http://tauri.localhost/index.html"), 7826));
        assert!(!is_service_url(&u("http://localhost:7826/"), 7826), "the shell always loads 127.0.0.1");
    }

    #[test]
    fn browser_args_keep_wrys_defaults() {
        for part in ["msWebOOUI", "msPdfOOUI", "msSmartScreenProtection", "--autoplay-policy=no-user-gesture-required"] {
            assert!(BROWSER_ARGS.contains(part), "{part}");
        }
    }

    #[test]
    fn minimum_width_is_phone_width() {
        assert_eq!(MIN_SIZE.0, 360.0);
    }
}
