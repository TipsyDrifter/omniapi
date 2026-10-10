//! OmniAPI desktop shell (1.3-M5). A thin layer (1.3-b): the tray icon, one window that loads
//! the local service (1.3-a), and the service's life — adopt or start it, watch it, restart it,
//! stop it on quit. Everything the user works with lives in the service and its web pages.
//!
//! Command line (also what a second launch forwards to the running shell):
//!   (none) / --open        open the window
//!   --background           tray and service only (what the logon entry uses)
//!   --restart-service      restart the service
//!   --quit                 stop the service and exit
//!   --autostart-on / --autostart-off
//!                          same as ticking "開機時啟動" in the tray menu (for scripts and tests)

mod autostart;
mod autostart_pref;
mod config;
mod http;
mod identity;
mod log;
mod proc;
mod service;
mod supervisor;
mod tray;
mod update;
mod window;
mod winhttp;
mod winstate;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::ManagerExt;

use crate::autostart::Legacy;
use crate::log::Logger;
use crate::service::{Service, Snapshot};
use crate::supervisor::Phase;

/// What a launch (or a forwarded second launch) asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Open,
    Background,
    Restart,
    Quit,
    Autostart(bool),
}

pub fn action_from_args(args: &[String]) -> Action {
    // the first entry is the exe itself
    let has = |f: &str| args.iter().skip(1).any(|a| a == f);
    if has("--quit") {
        Action::Quit
    } else if has("--restart-service") {
        Action::Restart
    } else if has("--autostart-on") {
        Action::Autostart(true)
    } else if has("--autostart-off") {
        Action::Autostart(false)
    } else if has("--background") {
        Action::Background
    } else {
        Action::Open
    }
}

struct Shell {
    log: Arc<Logger>,
    service: Option<Service>,
    /// Set when the config could not be used: the window and the tray show it.
    config_error: Option<String>,
    log_dir: PathBuf,
    /// The data home (autostart.pref lives there); `None` when the config could not be used.
    data_home: Option<PathBuf>,
    config_dir: Option<PathBuf>,
    items: Mutex<Option<tray::Items>>,
    legacy: Mutex<Legacy>,
    /// A newer published version and its release page (the daily check, `update.rs`).
    update: Mutex<Option<(String, String)>>,
    quitting: AtomicBool,
}

impl Shell {
    fn snapshot(&self) -> Snapshot {
        match &self.service {
            Some(s) => s.snapshot(),
            None => Snapshot {
                phase: Phase::Failed,
                port: 0,
                pid: None,
                adopted: false,
                failures: 0,
                max_failures: 0,
                misses: 0,
                miss_threshold: 0,
                restarts: 0,
                problem: self.config_error.clone(),
                retry_in_secs: None,
                starting_since_ms: None,
            },
        }
    }

    fn status_json(&self, s: &Snapshot) -> String {
        s.to_json(&config::plain(&self.log_dir)).to_string()
    }
}

fn open_window(app: &AppHandle, via: &str) {
    let shell = app.state::<Shell>();
    if shell.quitting.load(Ordering::SeqCst) {
        return;
    }
    let snap = shell.snapshot();
    let url = shell.service.as_ref().map(|s| s.url()).unwrap_or_else(|| "http://127.0.0.1/".into());
    let o = window::Open {
        port: snap.port,
        service_url: &url,
        service_up: snap.phase == Phase::Up,
        status_json: shell.status_json(&snap),
        config_dir: shell.config_dir.clone(),
    };
    match window::show(app, o) {
        Ok(created) => shell.log.line("window-open", format!("via={via} created={created} service_up={}", snap.phase == Phase::Up)),
        Err(e) => shell.log.line("window-open-failed", format!("via={via} {e}")),
    }
}

fn restart_service(app: &AppHandle, via: &str) {
    let shell = app.state::<Shell>();
    match &shell.service {
        Some(s) => s.restart(via),
        None => shell.log.line("restart-ignored", format!("via={via}: no usable config")),
    }
}

fn quit(app: &AppHandle, via: &str) {
    let shell = app.state::<Shell>();
    if shell.quitting.swap(true, Ordering::SeqCst) {
        return; // already on the way out
    }
    shell.log.line("quit", format!("via={via}"));
    if let (Some(w), Some(dir)) = (app.get_webview_window(window::LABEL), shell.config_dir.as_deref()) {
        window::remember(&w, dir);
        let _ = w.destroy();
    }
    if let Some(s) = &shell.service {
        let ok = s.quit();
        shell.log.line("quit-service", if ok { "service stopped" } else { "gave up waiting for the service runner" });
    }
    shell.log.line("exit", "app.exit(0)");
    app.exit(0);
}

/// Turn the logon entry on or off (`None` = flip it, as the menu's check mark does).
fn set_autostart(app: &AppHandle, want: Option<bool>, via: &str) {
    let shell = app.state::<Shell>();
    let al = app.autolaunch();
    let was = al.is_enabled().unwrap_or(false);
    let on = want.unwrap_or(!was);
    let res = if on == was { Ok(()) } else if on { al.enable() } else { al.disable() };
    let now = al.is_enabled().unwrap_or(was);
    match res {
        Ok(()) => {
            shell.log.line("autostart", format!("via={via} {was} -> {now}"));
            // the user's intent, kept where an install cannot erase it (autostart_pref.rs)
            remember_autostart(&shell.log, shell.data_home.as_deref(), now, via);
        }
        Err(e) => shell.log.line("autostart-failed", format!("via={via} {was} -> {on}: {e}")),
    }
    let legacy = autostart::detect();
    if now && legacy.any() {
        shell.log.line("autostart-legacy", format!("the old `omni autostart` launcher is also on ({}); left alone — `omni autostart remove` removes it", legacy.describe()));
    }
    *shell.legacy.lock().unwrap_or_else(|e| e.into_inner()) = legacy.clone();
    rebuild_menu(app, now, &legacy);
}

/// Keep the user's "start at logon" choice in the data home (autostart_pref.rs): the installer
/// and the next start put the registry value back from it.
fn remember_autostart(log: &Logger, data_home: Option<&Path>, on: bool, via: &str) {
    let Some(dir) = data_home else { return };
    match autostart_pref::write(dir, on) {
        Ok(true) => log.line("autostart-pref", format!("via={via} {}", if on { "on" } else { "off" })),
        Ok(false) => {}
        Err(e) => log.line("autostart-pref-failed", format!("via={via} {}: {e}", dir.display())),
    }
}

/// At start, before the tray shows the check mark: compare the stored choice with the registry.
///  - choice on, value missing (a covering install removed it) or pointing at an exe that is gone:
///    write it again (`autostart-restored`);
///  - no choice stored yet (first run after 1.4.0, or the file was lost): record the actual state,
///    change nothing (`autostart-pref-seeded`);
///  - choice off: leave everything alone.
/// A development build (debug) never writes the registry here: its exe is not the installed one.
fn reconcile_autostart<R: tauri::Runtime>(app: &impl Manager<R>, log: &Logger, data_home: &Path) {
    use crate::autostart_pref::{classify, plan, Plan};
    let al = app.autolaunch();
    let actual = al.is_enabled().unwrap_or(false);
    let pref = autostart_pref::read(data_home);
    let mine = autostart::my_command().unwrap_or_default();
    let value = autostart::read_run_value();
    let state = classify(value.as_deref(), &mine, |p| p.is_file());
    let action = plan(pref, state, actual);
    log.line("autostart-reconcile", format!("pref={pref:?} run={state:?} enabled={actual} -> {action:?}"));
    match action {
        Plan::Nothing => {}
        Plan::Seed(v) => {
            remember_autostart(log, Some(data_home), v, "seed");
            log.line("autostart-pref-seeded", format!("no stored choice; recorded the actual state: {}", if v { "on" } else { "off" }));
        }
        Plan::Sync(v) => {
            remember_autostart(log, Some(data_home), v, "start");
        }
        Plan::Restore if cfg!(debug_assertions) => {
            log.line("autostart-restore-skipped", "development build: the logon entry is not written from here");
        }
        Plan::Restore => match al.enable() {
            Ok(()) => log.line("autostart-restored", format!("choice is on and the entry was {state:?} (was {:?}); wrote {mine:?}", value.unwrap_or_default())),
            Err(e) => log.line("autostart-restore-failed", e.to_string()),
        },
    }
}

/// The settings page can switch the logon entry too (the service writes the same registry
/// value). When the registry no longer matches the check mark, rebuild the menu (check mark and
/// old-launcher line). Runs when the pointer reaches the tray icon and every few seconds.
fn follow_autostart(app: &AppHandle, via: &str) {
    let Some(shell) = app.try_state::<Shell>() else { return };
    if shell.quitting.load(Ordering::SeqCst) {
        return;
    }
    let Ok(actual) = app.autolaunch().is_enabled() else { return };
    let shown = shell.items.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|i| i.autostart_on);
    if !tray::autostart_stale(shown, actual) {
        return;
    }
    shell.log.line("autostart-followed", format!("via={via} {} -> {actual} (changed outside the tray menu)", shown.map(|b| b.to_string()).unwrap_or_else(|| "?".into())));
    // the settings page (the service writes the registry itself) or Task Manager changed it: that is the user's intent now
    remember_autostart(&shell.log, shell.data_home.as_deref(), actual, via);
    let legacy = autostart::detect();
    if actual && legacy.any() {
        shell.log.line("autostart-legacy", format!("the old `omni autostart` launcher is also on ({}); left alone — `omni autostart remove` removes it", legacy.describe()));
    }
    *shell.legacy.lock().unwrap_or_else(|e| e.into_inner()) = legacy.clone();
    rebuild_menu(app, actual, &legacy);
}

/// How often the check mark is compared with the registry when nobody points at the icon.
const AUTOSTART_FOLLOW_SECS: u64 = 5;

fn rebuild_menu(app: &AppHandle, autostart_on: bool, legacy: &Legacy) {
    let shell = app.state::<Shell>();
    let hint = (autostart_on && legacy.any()).then_some(legacy);
    let update = shell.update.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|(v, _)| v.clone());
    match tray::build_menu(app, autostart_on, hint, update.as_deref()) {
        Ok((menu, items)) => {
            if let Some(t) = app.tray_by_id(tray::TRAY_ID) {
                let _ = t.set_menu(Some(menu));
            }
            tray::refresh(app, &items, &shell.snapshot());
            *shell.items.lock().unwrap_or_else(|e| e.into_inner()) = Some(items);
        }
        Err(e) => shell.log.line("menu-failed", e.to_string()),
    }
}

fn open_logs(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let _ = std::fs::create_dir_all(&shell.log_dir);
    if let Err(e) = proc::open_folder(&shell.log_dir) {
        shell.log.line("open-logs-failed", e.to_string());
    }
}

fn open_release_page(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let page = shell.update.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|(_, u)| u.clone());
    if let Some(url) = page.and_then(|u| tauri::Url::parse(&u).ok()) {
        shell.log.line("update-open", url.as_str());
        window::open_in_browser(&url);
    }
}

/// A new check result: remember a newer version and rebuild the menu when that changed.
fn on_update_state(app: &AppHandle, s: &update::State) {
    let Some(shell) = app.try_state::<Shell>() else { return };
    let now = update::available(s, &app.package_info().version.to_string());
    let changed = {
        let mut cur = shell.update.lock().unwrap_or_else(|e| e.into_inner());
        let changed = *cur != now;
        *cur = now.clone();
        changed
    };
    if changed {
        if let Some((v, _)) = &now {
            shell.log.line("update-available", format!("v{v}"));
        }
        let legacy = shell.legacy.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let on = app.autolaunch().is_enabled().unwrap_or(false);
        rebuild_menu(app, on, &legacy);
    }
}

fn open_legacy_folder(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let legacy = shell.legacy.lock().unwrap_or_else(|e| e.into_inner()).clone();
    if let Some(dir) = legacy.startup_vbs.as_deref().and_then(|p| p.parent()) {
        let _ = proc::open_folder(dir);
    }
}

/// Every action runs on its own thread: they are reached from main-thread callbacks (tray,
/// menu, single-instance), and building a webview there deadlocks; quit blocks for seconds.
fn dispatch(app: &AppHandle, action: Action, via: &str) {
    let app = app.clone();
    let via = via.to_string();
    std::thread::spawn(move || match action {
        Action::Open => open_window(&app, &via),
        Action::Restart => restart_service(&app, &via),
        Action::Quit => quit(&app, &via),
        Action::Autostart(on) => set_autostart(&app, Some(on), &via),
        Action::Background => {}
    });
}

/// Only the local starting page may call the shell (not the dashboard or anything it links to).
fn from_starting_page(webview: &tauri::Webview) -> bool {
    webview.url().map(|u| u.host_str() == Some("tauri.localhost")).unwrap_or(false)
}

#[tauri::command]
fn retry_service(app: AppHandle, webview: tauri::Webview) {
    if from_starting_page(&webview) {
        dispatch(&app, Action::Restart, "starting-page");
    }
}

#[tauri::command(rename = "open_logs")]
fn open_logs_cmd(app: AppHandle, webview: tauri::Webview) {
    if from_starting_page(&webview) {
        open_logs(&app);
    }
}

fn run_id() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let first_args: Vec<String> = std::env::args().collect();
    // No early return for --quit here: the single-instance plugin must run first so a second
    // launch can forward --quit to the running shell.

    let app = tauri::Builder::default()
        // Must be the first plugin. A second launch hands its args to us and exits there.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            let action = match action_from_args(&args) {
                Action::Background => Action::Open, // launching it again means "show me"
                a => a,
            };
            if let Some(shell) = app.try_state::<Shell>() {
                shell.log.line("second-instance", format!("args={args:?} -> {action:?}"));
            }
            dispatch(app, action, "second-instance");
        }))
        .plugin(tauri_plugin_autostart::Builder::new().app_name(autostart::RUN_VALUE).arg(autostart::ARG).build())
        .invoke_handler(tauri::generate_handler![retry_service, open_logs_cmd])
        .setup(move |app| {
            let handle = app.handle().clone();
            let first = action_from_args(&first_args);
            if first == Action::Quit {
                // --quit and no shell was running: nothing to stop.
                handle.exit(0);
                return Ok(());
            }

            // config → log dir → logger
            let ctx = config::Context::from_env(run_id());
            let exe_dir = std::env::current_exe().ok().and_then(|e| e.parent().map(PathBuf::from));
            let env_cfg = std::env::var(config::ENV_VAR).ok();
            let loaded = config::locate(env_cfg.as_deref(), exe_dir.as_deref())
                .and_then(|(text, source)| config::parse(&text, &ctx).map(|c| (c, source)));
            let log_dir = match &loaded {
                Ok((c, _)) => c.log_dir.clone(),
                // no usable config: the default data home's logs (OMNIAPI_HOME if the shell has one)
                Err(_) => match &ctx.env_home {
                    Some(h) => PathBuf::from(h).join("logs"),
                    None => PathBuf::from(identity::default_home(&ctx.user_profile)).join("logs"),
                },
            };
            let log = Arc::new(Logger::open(&log_dir));
            log.line(
                "start",
                format!("version={} exe={:?} args={first_args:?}", app.package_info().version, std::env::current_exe().ok()),
            );
            let config_dir = app.path().app_config_dir().ok();
            let data_home = loaded.as_ref().ok().map(|(c, _)| c.data_home.clone());
            if let Some(dir) = &data_home {
                reconcile_autostart(&handle, &log, dir);
            }

            // tray first (with a neutral status), so even a broken config is visible
            let al_on = app.autolaunch().is_enabled().unwrap_or(false);
            let legacy = autostart::detect();
            if al_on && legacy.any() {
                log.line("autostart-legacy", format!("both this app and the old `omni autostart` launcher start at logon ({}); left alone — `omni autostart remove` removes it", legacy.describe()));
            }
            log.line("autostart-state", format!("enabled={al_on} legacy={}", legacy.describe()));
            let hint = (al_on && legacy.any()).then_some(&legacy);
            let (menu, items) = tray::build_menu(&handle, al_on, hint, None)?;
            TrayIconBuilder::with_id(tray::TRAY_ID)
                .icon(tray::icon(tray::Look::Starting))
                .tooltip(identity::title())
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, ev| match ev.id.0.as_str() {
                    tray::ids::OPEN => dispatch(app, Action::Open, "tray-menu"),
                    tray::ids::RESTART => dispatch(app, Action::Restart, "tray-menu"),
                    tray::ids::QUIT => dispatch(app, Action::Quit, "tray-menu"),
                    tray::ids::LOGS => open_logs(app),
                    tray::ids::LEGACY => open_legacy_folder(app),
                    tray::ids::UPDATE => open_release_page(app),
                    tray::ids::AUTOSTART => {
                        let app = app.clone();
                        std::thread::spawn(move || set_autostart(&app, None, "tray-menu"));
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, ev| match ev {
                    TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } => {
                        dispatch(tray.app_handle(), Action::Open, "tray-click");
                    }
                    // the pointer reached the icon: the menu may open next, so show the logon
                    // entry as it is now (the settings page may have switched it)
                    TrayIconEvent::Enter { .. } => {
                        let app = tray.app_handle().clone();
                        std::thread::spawn(move || follow_autostart(&app, "tray-hover"));
                    }
                    _ => {}
                })
                .build(app)?;

            let update_plan = loaded.as_ref().ok().map(|(c, _)| (c.update_check.clone(), c.data_home.clone()));
            let (service, config_error) = match loaded {
                Ok((cfg, source)) => {
                    log.line(
                        "config",
                        format!("source=[{source}] port={} program={:?} cwd={:?} log_dir={:?} watch={:?}", cfg.port, cfg.service.program, cfg.service.cwd, cfg.log_dir, cfg.watch),
                    );
                    let h2 = handle.clone();
                    let on_change: service::OnChange = Arc::new(move |snap: &Snapshot| {
                        let Some(shell) = h2.try_state::<Shell>() else { return };
                        if let Some(items) = shell.items.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
                            tray::refresh(&h2, items, snap);
                        }
                        if snap.phase == Phase::Up {
                            if let Some(s) = &shell.service {
                                if window::go_to_service_if_waiting(&h2, &s.url(), snap.port) {
                                    shell.log.line("window-navigate", "starting page -> service");
                                }
                            }
                        } else {
                            window::push_status(&h2, &shell.status_json(snap), snap.port);
                        }
                    });
                    (Some(Service::start(cfg, Arc::clone(&log), on_change)), None)
                }
                Err(e) => {
                    log.line("config-error", &e);
                    let shown = if e.starts_with(config::MISSING) { e } else { format!("設定檔有問題：{e}") };
                    (None, Some(shown))
                }
            };
            app.manage(Shell {
                log: Arc::clone(&log),
                service,
                config_error,
                log_dir,
                data_home,
                config_dir,
                items: Mutex::new(Some(items)),
                legacy: Mutex::new(legacy),
                update: Mutex::new(None),
                quitting: AtomicBool::new(false),
            });
            {
                let shell = app.state::<Shell>();
                if let Some(items) = shell.items.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
                    tray::refresh(&handle, items, &shell.snapshot());
                };
            }
            {
                let h4 = handle.clone();
                std::thread::spawn(move || loop {
                    std::thread::sleep(std::time::Duration::from_secs(AUTOSTART_FOLLOW_SECS));
                    follow_autostart(&h4, "timer");
                });
            }
            if let Some((uc, data_home)) = update_plan {
                let h3 = handle.clone();
                let on_state: update::OnState = Arc::new(move |s: &update::State| on_update_state(&h3, s));
                update::start(uc, app.package_info().version.to_string(), &data_home, Arc::clone(&log), on_state);
            }

            if matches!(first, Action::Open | Action::Restart | Action::Autostart(_)) {
                dispatch(&handle, first, "first-launch-args");
            }
            Ok(())
        })
        .on_window_event(|w, ev| match ev {
            WindowEvent::CloseRequested { .. } => {
                if let Some(shell) = w.app_handle().try_state::<Shell>() {
                    if let (Some(dir), Some(ww)) = (shell.config_dir.as_deref(), w.app_handle().get_webview_window(w.label())) {
                        window::remember(&ww, dir);
                    }
                }
            }
            WindowEvent::Destroyed => {
                if let Some(shell) = w.app_handle().try_state::<Shell>() {
                    shell.log.line("window-destroyed", w.label());
                }
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app, ev| {
        // The last window closed → code None → stay in the tray. app.exit(0) → Some(0) → exit.
        if let RunEvent::ExitRequested { code: None, api, .. } = ev {
            api.prevent_exit();
            if let Some(shell) = app.try_state::<Shell>() {
                shell.log.line("exit-prevented", "last window closed; staying in the tray");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(args: &[&str]) -> Action {
        let mut v = vec!["OmniAPI.exe".to_string()];
        v.extend(args.iter().map(|s| s.to_string()));
        action_from_args(&v)
    }

    #[test]
    fn command_line() {
        assert_eq!(a(&[]), Action::Open);
        assert_eq!(a(&["--open"]), Action::Open);
        assert_eq!(a(&["--background"]), Action::Background);
        assert_eq!(a(&["--restart-service"]), Action::Restart);
        assert_eq!(a(&["--background", "--quit"]), Action::Quit, "quit wins");
        assert_eq!(a(&["--autostart-on"]), Action::Autostart(true));
        assert_eq!(a(&["--autostart-off"]), Action::Autostart(false));
        assert_eq!(action_from_args(&["--quit".to_string()]), Action::Open, "the exe path itself is not a flag");
    }
}
