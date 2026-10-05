//! "Is there a new version?" (decision D45's fallback; 1.3-M6). At most one successful check a
//! day: one anonymous GET of the public repo's latest release (GitHub's API, no token, nothing
//! about the user). A new version adds "有新版 vX.Y.Z" to the tray menu (opens the release page)
//! and is written to `<data home>\desktop-update.json`, which the service shows in `/api/status`
//! (`desktop_update`) for the web pages. Offline / any failure: one log line, try again later,
//! nothing shown to the user. Turned off with `update_check.enabled: false` in the shell config.
//!
//! The state file also carries the schedule across restarts (when the last check was), so
//! restarting the shell does not mean another request.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::config::UpdateCheck;
use crate::log::{epoch_ms, Logger};

pub const STATE_FILE: &str = "desktop-update.json";
const HOUR_MS: u64 = 3_600_000;

/// What the last check found (the JSON in `desktop-update.json`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct State {
    pub comment: String,
    pub enabled: bool,
    /// The app version that wrote this (the installed shell).
    pub current: String,
    /// Last successful check (epoch ms), 0 = never.
    pub checked_at_ms: u64,
    /// Last attempt, successful or not (epoch ms).
    pub attempted_at_ms: u64,
    /// Latest published version, without the leading "v".
    pub latest: Option<String>,
    /// The release page to open.
    pub url: Option<String>,
    /// latest > current
    pub newer: bool,
    /// Why the last attempt failed (None after a success).
    pub error: Option<String>,
    pub source: String,
}

const COMMENT: &str = "Written by the OmniAPI desktop app: the result of the daily new-version check. Safe to delete.";

pub fn state_path(data_home: &Path) -> PathBuf {
    data_home.join(STATE_FILE)
}

pub fn read_state(path: &Path) -> Option<State> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn write_state(path: &Path, s: &State) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(s).unwrap_or_default())?;
    std::fs::rename(&tmp, path)
}

/// `1.2.0`, `v1.3.0`, `1.3.0-rc.1` → comparable parts. A pre-release sorts before its release.
pub fn parse_version(raw: &str) -> Option<(u64, u64, u64, Option<String>)> {
    let s = raw.trim().trim_start_matches(['v', 'V']);
    let s = s.split('+').next()?; // build metadata never matters
    let (core, pre) = match s.split_once('-') {
        Some((c, p)) if !p.is_empty() => (c, Some(p.to_string())),
        Some(_) => return None,
        None => (s, None),
    };
    let mut it = core.split('.');
    let (a, b, c) = (it.next()?.parse().ok()?, it.next()?.parse().ok()?, it.next()?.parse().ok()?);
    if it.next().is_some() {
        return None;
    }
    Some((a, b, c, pre))
}

/// Is `latest` newer than `current`? Unknown spellings are never "newer".
pub fn is_newer(latest: &str, current: &str) -> bool {
    let (Some(l), Some(c)) = (parse_version(latest), parse_version(current)) else { return false };
    if (l.0, l.1, l.2) != (c.0, c.1, c.2) {
        return (l.0, l.1, l.2) > (c.0, c.1, c.2);
    }
    match (&l.3, &c.3) {
        (None, Some(_)) => true, // 1.3.0 after 1.3.0-rc.1
        (Some(lp), Some(cp)) => pre_newer(lp, cp),
        _ => false,
    }
}

fn pre_newer(l: &str, c: &str) -> bool {
    let (lp, cp): (Vec<&str>, Vec<&str>) = (l.split('.').collect(), c.split('.').collect());
    for (a, b) in lp.iter().zip(cp.iter()) {
        let ord = match (a.parse::<u64>(), b.parse::<u64>()) {
            (Ok(x), Ok(y)) => x.cmp(&y),
            (Ok(_), Err(_)) => std::cmp::Ordering::Less,
            (Err(_), Ok(_)) => std::cmp::Ordering::Greater,
            _ => a.cmp(b),
        };
        if ord != std::cmp::Ordering::Equal {
            return ord == std::cmp::Ordering::Greater;
        }
    }
    lp.len() > cp.len()
}

/// `(version, page)` from GitHub's release JSON (`tag_name`, `html_url`). Drafts and
/// pre-releases are ignored (`/releases/latest` never returns them; a test server might).
pub fn parse_release(body: &str) -> Result<(String, String), String> {
    let v: serde_json::Value = serde_json::from_str(body).map_err(|e| format!("not JSON: {e}"))?;
    if v.get("draft").and_then(|d| d.as_bool()) == Some(true) || v.get("prerelease").and_then(|d| d.as_bool()) == Some(true) {
        return Err("the latest release is a draft or pre-release".into());
    }
    let tag = v.get("tag_name").and_then(|t| t.as_str()).ok_or("no tag_name")?;
    let (a, b, c, pre) = parse_version(tag).ok_or_else(|| format!("tag {tag:?} is not a version"))?;
    let version = match pre {
        Some(p) => format!("{a}.{b}.{c}-{p}"),
        None => format!("{a}.{b}.{c}"),
    };
    let url = v.get("html_url").and_then(|t| t.as_str()).unwrap_or_default().to_string();
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err(format!("html_url {url:?} is not a web page"));
    }
    Ok((version, url))
}

/// How long until the next check is due (0 = now).
pub fn due_in(state: Option<&State>, cfg: &UpdateCheck, now_ms: u64) -> Duration {
    let Some(s) = state else { return Duration::ZERO };
    let next = if s.error.is_some() || s.checked_at_ms == 0 {
        s.attempted_at_ms + cfg.retry_hours.max(1) * HOUR_MS
    } else {
        s.checked_at_ms + cfg.interval_hours.max(1) * HOUR_MS
    };
    Duration::from_millis(next.saturating_sub(now_ms))
}

/// One check: fetch, compare, write the state file. Returns the new state.
pub fn check_once(cfg: &UpdateCheck, current: &str, path: &Path, previous: Option<&State>, log: &Logger) -> State {
    let now = epoch_ms();
    let mut s = previous.cloned().unwrap_or_default();
    s.comment = COMMENT.into();
    s.enabled = true;
    s.current = current.to_string();
    s.source = cfg.url.clone();
    s.attempted_at_ms = now;
    let ua = format!("OmniAPI-desktop/{current}");
    match crate::winhttp::get(&cfg.url, &ua, Duration::from_secs(15)) {
        Ok((200, body)) => match parse_release(&body) {
            Ok((latest, url)) => {
                s.newer = is_newer(&latest, current);
                s.latest = Some(latest.clone());
                s.url = Some(url);
                s.checked_at_ms = now;
                s.error = None;
                log.line("update-check", format!("latest={latest} current={current} newer={}", s.newer));
            }
            Err(e) => {
                s.error = Some(e.clone());
                log.line("update-check-failed", format!("{}: {e}", cfg.url));
            }
        },
        Ok((code, _)) => {
            let e = format!("HTTP {code}");
            s.error = Some(e.clone());
            log.line("update-check-failed", format!("{}: {e}", cfg.url));
        }
        Err(e) => {
            s.error = Some(e.clone());
            log.line("update-check-failed", format!("{}: {e}", cfg.url));
        }
    }
    if let Err(e) = write_state(path, &s) {
        log.line("update-state-failed", format!("{}: {e}", path.display()));
    }
    s
}

/// What the tray shows: `Some((version, page))` when a newer version is known.
pub fn available(s: &State, current: &str) -> Option<(String, String)> {
    let latest = s.latest.as_deref()?;
    let url = s.url.as_deref()?;
    is_newer(latest, current).then(|| (latest.to_string(), url.to_string()))
}

pub type OnState = Arc<dyn Fn(&State) + Send + Sync>;

/// Start the background checker (a no-op that only records "off" when disabled). `on_state`
/// runs with the cached result at once and after every check.
pub fn start(cfg: UpdateCheck, current: String, data_home: &Path, log: Arc<Logger>, on_state: OnState) {
    let path = state_path(data_home);
    let cached = read_state(&path);
    if !cfg.enabled {
        log.line("update-check-off", "update_check.enabled = false");
        let off = State { comment: COMMENT.into(), enabled: false, current, source: cfg.url, ..Default::default() };
        let _ = write_state(&path, &off);
        return;
    }
    if let Some(s) = &cached {
        // a version found before this app was updated is not "new" any more
        let shown = State { newer: s.latest.as_deref().map(|l| is_newer(l, &current)).unwrap_or(false), current: current.clone(), ..s.clone() };
        if shown != *s {
            let _ = write_state(&path, &shown);
        }
        on_state(&shown);
    }
    std::thread::spawn(move || {
        let mut state = cached;
        std::thread::sleep(Duration::from_secs(cfg.delay_secs));
        loop {
            let wait = due_in(state.as_ref(), &cfg, epoch_ms());
            if !wait.is_zero() {
                log.line("update-check-next", format!("in {} min", wait.as_secs() / 60));
                // sleep in slices so a clock change (sleep / hibernate) is noticed within the hour
                std::thread::sleep(wait.min(Duration::from_secs(3600)));
                continue;
            }
            let s = check_once(&cfg, &current, &path, state.as_ref(), &log);
            on_state(&s);
            state = Some(s);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare() {
        assert!(is_newer("1.3.0", "1.2.0"));
        assert!(is_newer("v1.2.1", "1.2.0"));
        assert!(is_newer("2.0.0", "1.99.99"));
        assert!(!is_newer("1.2.0", "1.2.0"));
        assert!(!is_newer("1.1.9", "1.2.0"), "an older release is never offered");
        assert!(is_newer("1.3.0", "1.3.0-rc.1"));
        assert!(!is_newer("1.3.0-rc.1", "1.3.0"));
        assert!(is_newer("1.3.0-rc.2", "1.3.0-rc.1"));
        assert!(is_newer("1.3.0-rc.1", "1.3.0-alpha.9"));
        assert!(is_newer("1.3.0-alpha.1.1", "1.3.0-alpha.1"));
        assert!(!is_newer("nightly", "1.2.0"));
        assert!(!is_newer("1.3", "1.2.0"));
        assert_eq!(parse_version("v1.2.3+build.5"), Some((1, 2, 3, None)));
    }

    #[test]
    fn github_release_json() {
        let body = r#"{"tag_name":"v1.3.0","html_url":"https://github.com/TipsyDrifter/omniapi/releases/tag/v1.3.0","draft":false,"prerelease":false,"assets":[]}"#;
        assert_eq!(parse_release(body).unwrap(), ("1.3.0".into(), "https://github.com/TipsyDrifter/omniapi/releases/tag/v1.3.0".into()));
        assert!(parse_release(r#"{"tag_name":"v1.3.0","html_url":"javascript:alert(1)"}"#).is_err());
        assert!(parse_release(r#"{"tag_name":"latest","html_url":"https://x"}"#).is_err());
        assert!(parse_release(r#"{"tag_name":"v2.0.0","html_url":"https://x","prerelease":true}"#).is_err());
        assert!(parse_release(r#"{"message":"API rate limit exceeded"}"#).is_err());
        assert!(parse_release("<html>").is_err());
    }

    #[test]
    fn schedule() {
        let cfg = UpdateCheck::default(); // 24 h / 6 h
        let now = 100 * HOUR_MS;
        assert_eq!(due_in(None, &cfg, now), Duration::ZERO, "never checked: now");
        let ok = State { checked_at_ms: now - 2 * HOUR_MS, attempted_at_ms: now - 2 * HOUR_MS, ..Default::default() };
        assert_eq!(due_in(Some(&ok), &cfg, now), Duration::from_millis(22 * HOUR_MS), "a day after the last success");
        let failed = State { error: Some("offline".into()), ..ok.clone() };
        assert_eq!(due_in(Some(&failed), &cfg, now), Duration::from_millis(4 * HOUR_MS), "6 h after a failure");
        let old = State { checked_at_ms: now - 30 * HOUR_MS, ..ok };
        assert_eq!(due_in(Some(&old), &cfg, now), Duration::ZERO);
    }

    #[test]
    fn available_follows_the_running_version() {
        let s = State { latest: Some("1.3.0".into()), url: Some("https://x/r".into()), newer: true, ..Default::default() };
        assert_eq!(available(&s, "1.2.0"), Some(("1.3.0".into(), "https://x/r".into())));
        assert_eq!(available(&s, "1.3.0"), None, "after the update it is not new any more");
    }

    #[test]
    fn state_file_round_trip_and_offline_check() {
        let dir = std::env::temp_dir().join(format!("omni-update-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let log = Logger::open(&dir);
        // nothing listens on port 1: a failed check is recorded, not shown
        let cfg = UpdateCheck { url: "http://127.0.0.1:1/latest".into(), ..Default::default() };
        let path = state_path(&dir);
        let s = check_once(&cfg, "1.2.0", &path, None, &log);
        assert!(s.error.is_some() && s.latest.is_none() && !s.newer && s.checked_at_ms == 0);
        assert_eq!(read_state(&path).unwrap(), s);
        let text = std::fs::read_to_string(dir.join("shell.log")).unwrap();
        assert!(text.contains("update-check-failed"), "{text}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
