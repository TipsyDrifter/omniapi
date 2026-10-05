//! Window size and position, remembered between openings (and between runs) in
//! `<app config dir>/window.json`. Physical pixels, because the monitors may differ in scale.
//! A saved place that is no longer on any monitor (a screen was unplugged) is not used.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Geometry {
    pub x: i32,
    pub y: i32,
    /// Inner (client area) size.
    pub width: u32,
    pub height: u32,
    #[serde(default)]
    pub maximized: bool,
}

/// A monitor's work area in physical pixels.
#[derive(Clone, Copy, Debug)]
pub struct Area {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

/// How much of the window's title bar must be on a monitor for the saved place to be used:
/// enough to grab and move it.
const GRAB_W: i64 = 120;
const GRAB_H: i64 = 32;

/// The saved geometry if its top strip is reachable on one of `monitors`, with the size grown
/// to at least `min` (the minimum may have changed since it was saved).
pub fn usable(saved: Geometry, monitors: &[Area], min: (u32, u32)) -> Option<Geometry> {
    if saved.width == 0 || saved.height == 0 {
        return None;
    }
    let (x0, y0) = (saved.x as i64, saved.y as i64);
    let x1 = x0 + saved.width as i64;
    let reachable = monitors.iter().any(|m| {
        let (mx0, my0) = (m.x as i64, m.y as i64);
        let (mx1, my1) = (mx0 + m.width as i64, my0 + m.height as i64);
        let overlap_w = x1.min(mx1) - x0.max(mx0);
        let top_inside = y0 >= my0 && y0 + GRAB_H <= my1;
        overlap_w >= GRAB_W && top_inside
    });
    reachable.then(|| Geometry { width: saved.width.max(min.0), height: saved.height.max(min.1), ..saved })
}

pub fn file(config_dir: &Path) -> PathBuf {
    config_dir.join("window.json")
}

pub fn load(config_dir: &Path) -> Option<Geometry> {
    let text = std::fs::read_to_string(file(config_dir)).ok()?;
    serde_json::from_str(&text).ok()
}

pub fn save(config_dir: &Path, g: &Geometry) {
    let _ = std::fs::create_dir_all(config_dir);
    if let Ok(text) = serde_json::to_string(g) {
        // write-then-rename so a crash mid-write never leaves half a file
        let tmp = config_dir.join("window.json.tmp");
        if std::fs::write(&tmp, text).is_ok() {
            let _ = std::fs::rename(&tmp, file(config_dir));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MAIN: Area = Area { x: 0, y: 0, width: 1920, height: 1040 };
    const LEFT: Area = Area { x: -2560, y: 0, width: 2560, height: 1400 };

    fn g(x: i32, y: i32) -> Geometry {
        Geometry { x, y, width: 1200, height: 800, maximized: false }
    }

    #[test]
    fn a_place_on_screen_is_kept() {
        assert_eq!(usable(g(100, 100), &[MAIN], (360, 480)), Some(g(100, 100)));
        assert_eq!(usable(g(-2000, 50), &[MAIN, LEFT], (360, 480)), Some(g(-2000, 50)), "second monitor on the left");
    }

    #[test]
    fn a_place_on_an_unplugged_monitor_is_dropped() {
        assert_eq!(usable(g(-2000, 50), &[MAIN], (360, 480)), None);
    }

    #[test]
    fn title_bar_must_be_grabbable() {
        assert_eq!(usable(g(1850, 100), &[MAIN], (360, 480)), None, "only 70 px left on screen");
        assert_eq!(usable(g(100, -20), &[MAIN], (360, 480)), None, "title bar above the screen");
        assert_eq!(usable(g(100, 1030), &[MAIN], (360, 480)), None, "title bar below the work area");
        assert!(usable(g(1700, 100), &[MAIN], (360, 480)).is_some());
    }

    #[test]
    fn size_is_grown_to_the_minimum() {
        let small = Geometry { width: 200, height: 100, ..g(10, 10) };
        assert_eq!(usable(small, &[MAIN], (360, 480)).map(|x| (x.width, x.height)), Some((360, 480)));
        assert_eq!(usable(Geometry { width: 0, ..g(10, 10) }, &[MAIN], (360, 480)), None);
    }

    #[test]
    fn save_and_load_round_trip() {
        let dir = std::env::temp_dir().join(format!("omni-win-test-{}", std::process::id()));
        let geo = Geometry { maximized: true, ..g(5, 6) };
        save(&dir, &geo);
        assert_eq!(load(&dir), Some(geo));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
