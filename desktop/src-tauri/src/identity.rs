//! Which app this build is: the released OmniAPI, or the test build (cargo feature
//! `test-identity`, `package.ps1 -TestIdentity`).
//!
//! The two must never meet on one machine. A test shell with the released identity hands its
//! arguments to the owner's running shell (single instance is keyed on the Tauri identifier; a
//! test `--quit` would stop the owner's app), its installer stops the running `OmniAPI.exe` by
//! name, and its logon switch writes the owner's `Run\OmniAPI` value. So the test build differs
//! in everything that is shared state on the machine:
//!
//! | | released | test |
//! |---|---|---|
//! | Tauri identifier (single instance, `%APPDATA%` / WebView2 folders) | `com.kosa.omniapi` | `com.kosa.omniapi.test` (`package.ps1` overlay) |
//! | product name, exe, install folder, uninstall entry, Start menu | `OmniAPI` | `OmniAPI-Test` (overlay) |
//! | logon entry (`HKCU\...\Run` value) | `OmniAPI` | `OmniAPI-Test` (here) |
//! | default port | 7788 | 7939, and 7788 is refused (here) |
//! | default data home | `%USERPROFILE%\.omniapi` | `%USERPROFILE%\.omniapi-test`, `.omniapi` refused (here) |
//! | new-version check by default | on | off (here) |
//!
//! The released values below are the ones the shell always had; nothing about the released build
//! changes.

#[cfg(not(feature = "test-identity"))]
mod values {
    pub const TEST: bool = false;
    pub const NAME: &str = "OmniAPI";
    pub const RUN_VALUE: &str = "OmniAPI";
    pub const DEFAULT_PORT: u16 = 7788;
    pub const HOME_DIR: &str = ".omniapi";
    pub const UPDATE_CHECK_DEFAULT: bool = true;
}

#[cfg(feature = "test-identity")]
mod values {
    pub const TEST: bool = true;
    pub const NAME: &str = "OmniAPI-Test";
    pub const RUN_VALUE: &str = "OmniAPI-Test";
    pub const DEFAULT_PORT: u16 = 7939;
    pub const HOME_DIR: &str = ".omniapi-test";
    pub const UPDATE_CHECK_DEFAULT: bool = false;
}

pub use values::*;

/// The released app's port and data folder: a test build refuses to manage either.
pub const RELEASED_PORT: u16 = 7788;
pub const RELEASED_HOME_DIR: &str = ".omniapi";

/// The display name in the tray tooltip and the window title.
pub fn title() -> &'static str {
    if TEST {
        "OmniAPI 測試版"
    } else {
        "OmniAPI"
    }
}

/// `%USERPROFILE%\<HOME_DIR>` — the data home when neither the config nor the shell sets one.
pub fn default_home(user_profile: &str) -> String {
    format!("{}\\{}", user_profile.trim_end_matches('\\'), HOME_DIR)
}

/// A test build refuses the released app's port and data folder (`Ok` in the released build).
pub fn check_separate(port: u16, data_home: &str, user_profile: &str) -> Result<(), String> {
    if !TEST {
        return Ok(());
    }
    if port == RELEASED_PORT {
        return Err(format!("invalid shell config: this is the test build ({NAME}); port {RELEASED_PORT} belongs to the released app"));
    }
    let released = format!("{}\\{}", user_profile.trim_end_matches('\\'), RELEASED_HOME_DIR);
    let norm = |s: &str| s.trim_end_matches(['\\', '/']).replace('/', "\\").to_lowercase();
    if !user_profile.is_empty() && norm(data_home) == norm(&released) {
        return Err(format!("invalid shell config: this is the test build ({NAME}); {released} belongs to the released app"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn released_identity_is_what_it_always_was() {
        assert!(!TEST);
        assert_eq!((NAME, RUN_VALUE, DEFAULT_PORT, HOME_DIR), ("OmniAPI", "OmniAPI", 7788, ".omniapi"));
        assert!(UPDATE_CHECK_DEFAULT);
        assert_eq!(title(), "OmniAPI");
        assert_eq!(default_home(r"C:\Users\u"), r"C:\Users\u\.omniapi");
        assert!(check_separate(7788, r"C:\Users\u\.omniapi", r"C:\Users\u").is_ok(), "the released build accepts its own port and home");
    }

    #[cfg(feature = "test-identity")]
    #[test]
    fn test_identity_shares_nothing_with_the_released_app() {
        assert!(TEST);
        assert_ne!(RUN_VALUE, "OmniAPI");
        assert_ne!(DEFAULT_PORT, RELEASED_PORT);
        assert!((7900..=7949).contains(&DEFAULT_PORT), "sandbox port range");
        assert_ne!(HOME_DIR, RELEASED_HOME_DIR);
        assert!(!UPDATE_CHECK_DEFAULT);
        assert_eq!(default_home(r"C:\Users\u\"), r"C:\Users\u\.omniapi-test");
        assert!(check_separate(7788, r"C:\T\h", r"C:\Users\u").unwrap_err().contains("7788"));
        assert!(check_separate(7939, r"C:\Users\u\.omniapi", r"C:\Users\u").is_err());
        assert!(check_separate(7939, r"c:/users/U/.OmniAPI/", r"C:\Users\u").is_err(), "case and slashes do not matter");
        assert!(check_separate(7939, r"C:\Users\u\.omniapi-test", r"C:\Users\u").is_ok());
    }
}
