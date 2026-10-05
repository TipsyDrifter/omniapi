//! shell.log: one line per event, `<local time> +<secs since start> pid=<shell pid> <event> <details>`.
//! A plain file so people and the verification scripts can read and grep it. Kept small: when
//! it is past `MAX_BYTES` at start it becomes `shell.log.1` (one old copy).

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;
use std::time::Instant;

const MAX_BYTES: u64 = 2 * 1024 * 1024;

pub struct Logger {
    file: Mutex<Option<File>>,
    start: Instant,
}

impl Logger {
    pub fn open(dir: &Path) -> Logger {
        let _ = std::fs::create_dir_all(dir);
        let path = dir.join("shell.log");
        if std::fs::metadata(&path).map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
            let _ = std::fs::rename(&path, dir.join("shell.log.1"));
        }
        let file = OpenOptions::new().create(true).append(true).open(&path).ok();
        Logger { file: Mutex::new(file), start: Instant::now() }
    }

    pub fn line(&self, event: &str, details: impl AsRef<str>) {
        let up = self.start.elapsed().as_secs_f64();
        let text = format!("{} +{up:.3}s pid={} {event} {}\n", local_time(), std::process::id(), details.as_ref());
        if let Some(f) = self.file.lock().unwrap_or_else(|e| e.into_inner()).as_mut() {
            let _ = f.write_all(text.as_bytes());
            let _ = f.flush();
        }
        #[cfg(debug_assertions)]
        eprint!("{text}");
    }
}

/// `2026-10-04 13:05:09.123` in the machine's time zone.
#[cfg(windows)]
pub fn local_time() -> String {
    use windows_sys::Win32::System::SystemInformation::GetLocalTime;
    let mut t = unsafe { std::mem::zeroed() };
    unsafe { GetLocalTime(&mut t) };
    format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, t.wMilliseconds)
}

#[cfg(not(windows))]
pub fn local_time() -> String {
    let ms = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    format!("unix-ms {ms}")
}

/// Milliseconds since the Unix epoch (what the starting page counts from).
pub fn epoch_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_lines_and_rotates_a_big_log() {
        let dir = std::env::temp_dir().join(format!("omni-log-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("shell.log"), vec![b'x'; (MAX_BYTES + 1) as usize]).unwrap();
        let log = Logger::open(&dir);
        log.line("start", "hello");
        drop(log);
        let text = std::fs::read_to_string(dir.join("shell.log")).unwrap();
        assert!(text.contains(" start hello\n") && text.lines().count() == 1, "{text}");
        assert_eq!(std::fs::metadata(dir.join("shell.log.1")).unwrap().len(), MAX_BYTES + 1);
        assert_eq!(local_time().len(), 23);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
