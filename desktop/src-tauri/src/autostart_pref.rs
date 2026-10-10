//! The user's "start at logon" intent, kept in the data home so it outlives the registry value.
//!
//! Why. The logon entry is one registry value (`HKCU\...\Run\OmniAPI`, see autostart.rs). A
//! covering install made by double-clicking the installer first runs the OLD version's
//! uninstaller, and that one deletes the value unless Tauri's own updater started it (`/UPDATE`),
//! so the entry silently disappears on every manual upgrade. The data home (`~/.omniapi`, or
//! `~/.omniapi-test` for the test build) is never touched by an install or an uninstall, so the
//! intent is written there: the installer's post-install hook (nsis-hooks.nsh) puts the value
//! back from it at once, and the shell does the same at start (belt and braces: a value lost for
//! any other reason is also repaired).
//!
//! File: `<data home>\autostart.pref`, ASCII, first line `autostart=on` or `autostart=off`.
//! A line of text and not JSON because NSIS reads it too (one `FileRead` and a string compare;
//! it has no JSON parser). Only the first line counts. Missing, empty, unreadable or anything
//! else = "no preference known": nothing is restored, and the shell records the actual state.
//!
//! A genuine uninstall followed by a reinstall: the file stays, so autostart comes back with the
//! reinstall -- the same as the rest of the settings, which also survive an uninstall. (The
//! 1.4.1 uninstaller cannot tell a real uninstall from the first half of an upgrade in any way
//! that is safe to rely on, and one of the two directions has to be wrong; "settings survive" is
//! the one that matches the data home's meaning.) Removing `autostart.pref` or turning the
//! switch off before uninstalling gives the other behaviour.

use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const FILE_NAME: &str = "autostart.pref";
const KEY: &str = "autostart";

/// Writers (the tray thread, the follow timer, the first-launch flag) never interleave.
static WRITE_LOCK: Mutex<()> = Mutex::new(());

pub fn path(dir: &Path) -> PathBuf {
    dir.join(FILE_NAME)
}

/// The file's content for a preference.
pub fn render(on: bool) -> String {
    format!("{KEY}={}\n", if on { "on" } else { "off" })
}

/// The preference a file's text says, `None` for anything that is not exactly the first line
/// `autostart=on` / `autostart=off` (case and surrounding blanks do not matter; a BOM is skipped).
pub fn parse(text: &str) -> Option<bool> {
    let first = text.trim_start_matches('\u{feff}').lines().next()?;
    let (k, v) = first.split_once('=')?;
    if !k.trim().eq_ignore_ascii_case(KEY) {
        return None;
    }
    match v.trim().to_ascii_lowercase().as_str() {
        "on" => Some(true),
        "off" => Some(false),
        _ => None,
    }
}

/// The stored preference; a missing, unreadable or corrupt file is `None`.
pub fn read(dir: &Path) -> Option<bool> {
    // a few KB at most; never trust the size of a file somebody else may have edited
    let bytes = std::fs::read(path(dir)).ok()?;
    let head = &bytes[..bytes.len().min(256)];
    parse(&String::from_utf8_lossy(head))
}

/// Store the preference: written to a temporary file in the same folder and renamed over the
/// real one, so a reader (the installer) sees the old or the new content, never half of it.
/// Nothing is written when the file already says the same; `Ok(true)` = the file changed.
pub fn write(dir: &Path, on: bool) -> std::io::Result<bool> {
    let _guard = WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    if read(dir) == Some(on) {
        return Ok(false);
    }
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join(format!("{FILE_NAME}.{}.tmp", std::process::id()));
    let res: std::io::Result<bool> = (|| {
        let mut f = File::create(&tmp)?;
        f.write_all(render(on).as_bytes())?;
        f.sync_all()?;
        drop(f);
        std::fs::rename(&tmp, path(dir))?; // replaces an existing file on Windows
        Ok(true)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

/// What the logon value in the registry looks like.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunState {
    /// No value (or an empty one).
    Missing,
    /// A value that runs this exe, or another exe that is still there (another install, a
    /// development build): not ours to overwrite.
    Live,
    /// A value that runs an exe which no longer exists (the install folder was moved or removed).
    Stale,
}

/// Classify the registry value. `mine` is exactly what the plugin writes for the running exe
/// (`<exe> --background`); `exists` tells whether a file is there.
pub fn classify(value: Option<&str>, mine: &str, exists: impl Fn(&Path) -> bool) -> RunState {
    let Some(v) = value.map(str::trim).filter(|v| !v.is_empty()) else { return RunState::Missing };
    if v.eq_ignore_ascii_case(mine.trim()) {
        return RunState::Live;
    }
    match exe_of(v) {
        Some(exe) if !exists(Path::new(exe)) => RunState::Stale,
        // anything else, including a value in a form we do not know: leave it alone
        _ => RunState::Live,
    }
}

/// The exe a value of the form `<exe> --background` (exe unquoted, as the plugin writes it, or
/// quoted, as someone else might) runs.
fn exe_of(value: &str) -> Option<&str> {
    let rest = value.strip_suffix(crate::autostart::ARG)?.trim_end();
    let exe = rest.trim_matches('"');
    (!exe.is_empty()).then_some(exe)
}

/// What the shell does about the preference at start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Plan {
    /// Preference and registry agree, or the preference is off: leave everything alone.
    Nothing,
    /// No preference known yet (first run after 1.4.0, or a lost file): record what is actually
    /// the case, and change nothing else.
    Seed(bool),
    /// The preference is on and the value is missing or stale: write it again.
    Restore,
    /// The registry says something else than the preference although a value is there (Task
    /// Manager's switch, or the settings page while no 1.4.1 shell was watching): the registry
    /// is the later word, so the preference follows it.
    Sync(bool),
}

/// `pref`: what the file says. `run`: the registry value's state. `actual`: what the plugin's
/// `is_enabled` says (value there and Task Manager has not switched it off).
pub fn plan(pref: Option<bool>, run: RunState, actual: bool) -> Plan {
    match pref {
        None => Plan::Seed(actual),
        Some(true) if run != RunState::Live => Plan::Restore,
        Some(want) if want != actual => Plan::Sync(actual),
        Some(_) => Plan::Nothing,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINE: &str = r"C:\Apps\OmniAPI\OmniAPI.exe --background";

    fn temp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("omniapi-pref-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn render_and_parse_round_trip() {
        assert_eq!(render(true), "autostart=on\n");
        assert_eq!(render(false), "autostart=off\n");
        assert_eq!(parse(&render(true)), Some(true));
        assert_eq!(parse(&render(false)), Some(false));
    }

    #[test]
    fn parse_is_strict_about_the_first_line_and_loose_about_blanks_and_case() {
        assert_eq!(parse("autostart=on"), Some(true), "no newline");
        assert_eq!(parse("autostart=on\r\n"), Some(true), "CRLF (hand-edited)");
        assert_eq!(parse("\u{feff}autostart=off\n"), Some(false), "a BOM from an editor");
        assert_eq!(parse(" Autostart = ON \n"), Some(true));
        for bad in ["", "\n", "on", "autostart", "autostart=", "autostart=maybe", "autostart: on", "other=on", "{\"autostart\":true}", "\nautostart=on"] {
            assert_eq!(parse(bad), None, "{bad:?}");
        }
        assert_eq!(parse("junk\nautostart=on\n"), None, "only the first line counts (the installer reads only that)");
        assert_eq!(parse("autostart=on\nautostart=off\n"), Some(true));
    }

    #[test]
    fn missing_and_corrupt_files_mean_no_preference() {
        let d = temp("corrupt");
        assert_eq!(read(&d), None, "no folder");
        std::fs::create_dir_all(&d).unwrap();
        assert_eq!(read(&d), None, "no file");
        std::fs::write(path(&d), b"").unwrap();
        assert_eq!(read(&d), None, "empty");
        std::fs::write(path(&d), [0xff, 0xfe, 0x00, 0x80, 0x81]).unwrap();
        assert_eq!(read(&d), None, "binary garbage");
        std::fs::write(path(&d), vec![b'x'; 100_000]).unwrap();
        assert_eq!(read(&d), None, "a huge file");
        std::fs::remove_file(path(&d)).unwrap();
        std::fs::create_dir(path(&d)).unwrap();
        assert_eq!(read(&d), None, "a folder in its place");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn write_creates_the_folder_replaces_the_file_and_leaves_no_temp_file() {
        let d = temp("write").join("nested").join("home");
        assert!(write(&d, true).unwrap());
        assert_eq!(read(&d), Some(true));
        assert_eq!(std::fs::read_to_string(path(&d)).unwrap(), "autostart=on\n");
        write(&d, false).unwrap();
        assert_eq!(read(&d), Some(false));
        assert!(!write(&d, false).unwrap(), "same value: no write");
        std::fs::write(path(&d), b"garbage").unwrap();
        write(&d, true).unwrap();
        assert_eq!(read(&d), Some(true), "a corrupt file is replaced");
        let names: Vec<String> = std::fs::read_dir(&d).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        assert_eq!(names, vec![FILE_NAME.to_string()], "no .tmp left behind");
        let _ = std::fs::remove_dir_all(d.parent().unwrap().parent().unwrap());
    }

    #[test]
    fn write_into_an_unusable_place_is_an_error_not_a_panic() {
        let d = temp("blocked");
        std::fs::create_dir_all(&d).unwrap();
        let file_as_folder = d.join("f");
        std::fs::write(&file_as_folder, b"x").unwrap();
        assert!(write(&file_as_folder.join("home"), true).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn run_value_states() {
        let there = |_: &Path| true;
        let gone = |_: &Path| false;
        assert_eq!(classify(None, MINE, there), RunState::Missing);
        assert_eq!(classify(Some(""), MINE, there), RunState::Missing);
        assert_eq!(classify(Some("   "), MINE, there), RunState::Missing);
        assert_eq!(classify(Some(MINE), MINE, gone), RunState::Live, "ours, whatever the disk says");
        assert_eq!(classify(Some(&MINE.to_uppercase()), MINE, gone), RunState::Live, "paths are case-insensitive");
        let other = r"D:\old\OmniAPI.exe --background";
        assert_eq!(classify(Some(other), MINE, there), RunState::Live, "another install that still exists is left alone");
        assert_eq!(classify(Some(other), MINE, gone), RunState::Stale, "an exe that is gone is stale");
        assert_eq!(classify(Some(r#""D:\old dir\OmniAPI.exe" --background"#), MINE, gone), RunState::Stale, "quoted form");
        assert_eq!(classify(Some(r"D:\Program Files\x\OmniAPI.exe --background"), MINE, gone), RunState::Stale, "a space in the path");
        assert_eq!(classify(Some("something --else"), MINE, gone), RunState::Live, "a form we do not know is not touched");
        assert_eq!(classify(Some(" --background"), MINE, gone), RunState::Live);
    }

    #[test]
    fn what_the_shell_does_at_start() {
        use Plan::*;
        use RunState::*;
        // no preference: only record the facts, never change the registry
        assert_eq!(plan(None, Missing, false), Seed(false));
        assert_eq!(plan(None, Live, true), Seed(true));
        assert_eq!(plan(None, Live, false), Seed(false), "Task Manager switched it off");
        assert_eq!(plan(None, Stale, false), Seed(false), "a stale value is not repaired without an intent");
        // on, value gone: restore (the covering-install case)
        assert_eq!(plan(Some(true), Missing, false), Restore);
        assert_eq!(plan(Some(true), Stale, true), Restore);
        // on and fine
        assert_eq!(plan(Some(true), Live, true), Nothing);
        // on, value there but Task Manager's switch is off, or another live exe owns it: the preference follows the registry
        assert_eq!(plan(Some(true), Live, false), Sync(false));
        // off: never touch the registry
        assert_eq!(plan(Some(false), Missing, false), Nothing);
        assert_eq!(plan(Some(false), Stale, false), Nothing);
        assert_eq!(plan(Some(false), Live, true), Sync(true), "somebody turned it on while no shell watched; the file follows");
    }
}
