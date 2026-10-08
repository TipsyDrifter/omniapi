//! Shell config: which port the service answers on and how to start it. Nothing about the
//! service is hard-coded in Rust; 1.3-M6 changes where Python lives by editing the installed
//! default (`desktop/config/shell.installed.json`), not this file.
//!
//! Lookup order (first hit wins):
//!   1. env `OMNIAPI_SHELL_CONFIG` — a path to a JSON file. If the variable is set but the file
//!      is missing or broken, that is an error: falling back to the defaults would make a test
//!      shell adopt (and on quit, stop) the owner's real service on 7788.
//!   2. `<exe dir>/shell.config.json` — the installer puts the installed default there.
//!   3. debug builds only (or a release build with the `dev-default` feature): the development
//!      default compiled into the exe (`desktop/config/shell.dev.json`), the service from the
//!      checkout this shell was built from, with that checkout's `mcp/.venv`.
//!      A release build has no built-in default and no build-time path at all: without (1) or (2)
//!      it reports "找不到設定檔 shell.config.json" (tray, window, shell.log) and starts nothing.
//!
//! Placeholders, expanded in every string: `{port}`, `{temp}` (%TEMP%), `{run_id}` (unix secs
//! at shell start), `{exe_dir}` (the folder of the running exe), `{exe}` (the running exe
//! itself, written exactly as the logon entry has it), `{repo}` (the checkout the
//! exe was built from; development builds only, an error elsewhere), `{data_home}` (the
//! service's data folder: `OMNIAPI_HOME` from the service env, else from the shell's env, else
//! `%USERPROFILE%\.omniapi`), `{log_dir}`.
//! An unknown `{name}` is an error (a typo would otherwise reach the service as a literal).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

pub const ENV_VAR: &str = "OMNIAPI_SHELL_CONFIG";
pub const FILE_NAME: &str = "shell.config.json";
/// The development default, only in builds made for development: a release exe must not carry
/// the build machine's checkout path (`{repo}`) nor fall back to a service that is not there.
/// The test build (`test-identity`) never has one: the development default manages port 7788.
#[cfg(all(any(debug_assertions, feature = "dev-default"), not(feature = "test-identity")))]
pub const BUILT_IN: Option<&str> = Some(include_str!("../../config/shell.dev.json"));
#[cfg(not(all(any(debug_assertions, feature = "dev-default"), not(feature = "test-identity"))))]
pub const BUILT_IN: Option<&str> = None;
/// Start of the error when no config file is found (shown as is, no "設定檔有問題" in front).
pub const MISSING: &str = "找不到設定檔";
#[cfg(test)]
const DEV_DEFAULT: &str = include_str!("../../config/shell.dev.json");
/// Shipped next to the exe by the installer (tauri.conf.json `bundle.resources`); compiled in
/// here only so the tests can check it.
#[cfg(test)]
const INSTALLED_DEFAULT: &str = include_str!("../../config/shell.installed.json");
/// What `package.ps1 -TestIdentity` ships instead (tests only here, too).
#[cfg(test)]
const TEST_INSTALLED: &str = include_str!("../../config/shell.test.json");

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    /// Free text for people reading the file.
    #[serde(default)]
    #[allow(dead_code)]
    comment: Option<String>,
    #[serde(default = "default_port")]
    port: u16,
    /// Where shell.log (and, through `{log_dir}`, the service's logs) go. Default `{data_home}\logs`.
    #[serde(default)]
    log_dir: Option<String>,
    service: RawService,
    #[serde(default)]
    watch: Watch,
    #[serde(default)]
    update_check: UpdateCheck,
}

/// The new-version check (D45's fallback: say there is a new version, the user downloads it).
/// One anonymous GET of the public repo's latest release; nothing about the user is sent.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct UpdateCheck {
    pub enabled: bool,
    /// GitHub's "latest release" API (any URL answering the same JSON works, e.g. a local test server).
    pub url: String,
    /// After a successful check, the next one is this much later (also across restarts).
    pub interval_hours: u64,
    /// After a failed one (offline, rate-limited): try again this much later.
    pub retry_hours: u64,
    /// Wait after the shell starts before checking (the service starts first).
    pub delay_secs: u64,
}

pub const UPDATE_URL: &str = "https://api.github.com/repos/TipsyDrifter/omniapi/releases/latest";

impl Default for UpdateCheck {
    fn default() -> Self {
        UpdateCheck { enabled: crate::identity::UPDATE_CHECK_DEFAULT, url: UPDATE_URL.into(), interval_hours: 24, retry_hours: 6, delay_secs: 60 }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawService {
    /// false = only show the tray and the window; never start, restart or stop anything.
    #[serde(default = "yes")]
    enabled: bool,
    program: String,
    #[serde(default)]
    args: Vec<String>,
    cwd: String,
    #[serde(default)]
    env: BTreeMap<String, String>,
    /// Names of `env` entries whose values are folders to create before each start.
    #[serde(default)]
    create_dirs: Vec<String>,
}

/// How the service is watched and restarted (see `supervisor.rs` for what each one does).
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct Watch {
    /// Health check interval while the service is up.
    pub poll_secs: u64,
    /// Consecutive missed health checks (while up) before the service is taken down and restarted.
    pub miss_threshold: u32,
    /// A start that has not answered after this long counts as failed. The first start after an
    /// install measured 36 s (prototype one); slow disks and virus scans add more.
    pub startup_timeout_secs: u64,
    /// Up this long = the consecutive-failure count goes back to zero.
    pub stable_secs: u64,
    /// Consecutive failed starts before the shell stops trying (red icon, reason shown).
    pub max_failures: u32,
    /// Wait before retry n (1-based: entry n-1; the last entry repeats).
    pub backoff_secs: Vec<u64>,
    /// On quit / restart: how long the service gets to shut down by itself after
    /// `POST /api/shutdown` before the whole tree is killed. The service itself waits up to 10 s
    /// for stuck requests, so this must stay above that.
    pub shutdown_grace_secs: u64,
}

impl Default for Watch {
    fn default() -> Self {
        Watch {
            poll_secs: 3,
            miss_threshold: 3,
            startup_timeout_secs: 180,
            stable_secs: 60,
            max_failures: 5,
            backoff_secs: vec![0, 5, 15, 30],
            shutdown_grace_secs: 15,
        }
    }
}

impl Watch {
    pub fn poll(&self) -> Duration {
        Duration::from_secs(self.poll_secs.max(1))
    }
}

/// The config after placeholder expansion: everything the shell needs, ready to use.
#[derive(Debug, Clone)]
pub struct ShellConfig {
    pub port: u16,
    pub log_dir: PathBuf,
    pub data_home: PathBuf,
    pub service: ServiceSpec,
    pub watch: Watch,
    pub update_check: UpdateCheck,
}

#[derive(Debug, Clone)]
pub struct ServiceSpec {
    pub enabled: bool,
    pub program: String,
    pub args: Vec<String>,
    pub cwd: String,
    pub env: BTreeMap<String, String>,
    pub create_dirs: Vec<PathBuf>,
}

fn default_port() -> u16 {
    crate::identity::DEFAULT_PORT
}
fn yes() -> bool {
    true
}

/// Where a config came from (logged at start; shown nowhere else).
#[derive(Debug, Clone, PartialEq)]
pub enum Source {
    Env(PathBuf),
    ExeDir(PathBuf),
    BuiltIn,
}

impl std::fmt::Display for Source {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Source::Env(p) => write!(f, "{ENV_VAR}={}", p.display()),
            Source::ExeDir(p) => write!(f, "next to the exe: {}", p.display()),
            Source::BuiltIn => write!(f, "built-in development default"),
        }
    }
}

/// Values the placeholders expand to, gathered from the environment once.
#[derive(Debug, Clone)]
pub struct Context {
    pub temp: String,
    pub run_id: u64,
    pub exe_dir: String,
    /// The running exe, exactly as tauri-plugin-autostart writes it into the logon entry.
    pub exe: String,
    /// The checkout the exe was built from; `None` in release builds (`{repo}` is then an error).
    pub repo: Option<String>,
    /// The shell's own `OMNIAPI_HOME`, if any.
    pub env_home: Option<String>,
    pub user_profile: String,
}

impl Context {
    pub fn from_env(run_id: u64) -> Context {
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|e| e.parent().map(Path::to_path_buf))
            .map(|p| plain(&p))
            .unwrap_or_default();
        Context {
            temp: plain(&std::env::temp_dir()),
            run_id,
            exe_dir,
            exe: std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default(),
            repo: build_repo(),
            env_home: std::env::var("OMNIAPI_HOME").ok().filter(|v| !v.trim().is_empty()),
            user_profile: std::env::var("USERPROFILE").unwrap_or_default(),
        }
    }
}

/// The checkout this exe was compiled from (`desktop/src-tauri` → two folders up); development
/// builds only, so the path is never compiled into a release exe.
#[cfg(any(debug_assertions, feature = "dev-default"))]
fn build_repo() -> Option<String> {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest.parent().and_then(Path::parent).map(plain)
}
#[cfg(not(any(debug_assertions, feature = "dev-default")))]
fn build_repo() -> Option<String> {
    None
}

/// A path as a plain string: no `\\?\` prefix, no trailing separator.
pub fn plain(p: &Path) -> String {
    let s = p.to_string_lossy();
    let s = s.strip_prefix(r"\\?\").unwrap_or(&s);
    let t = s.trim_end_matches(['\\', '/']);
    // keep "C:\" as is: "C:" alone means "the current folder on drive C"
    if t.len() == 2 && t.ends_with(':') {
        s.to_string()
    } else {
        t.to_string()
    }
}

/// Pick the config text per the lookup order. `env_value` is `OMNIAPI_SHELL_CONFIG`.
pub fn locate(env_value: Option<&str>, exe_dir: Option<&Path>) -> Result<(String, Source), String> {
    locate_with(env_value, exe_dir, BUILT_IN)
}

fn locate_with(env_value: Option<&str>, exe_dir: Option<&Path>, built_in: Option<&str>) -> Result<(String, Source), String> {
    if let Some(v) = env_value.map(str::trim).filter(|v| !v.is_empty()) {
        let p = PathBuf::from(v.trim_matches('"'));
        return std::fs::read_to_string(&p)
            .map(|t| (t, Source::Env(p.clone())))
            .map_err(|e| format!("{ENV_VAR} points at {} but it cannot be read ({e}); not falling back to a default", p.display()));
    }
    if let Some(dir) = exe_dir {
        let p = dir.join(FILE_NAME);
        if p.is_file() {
            return std::fs::read_to_string(&p)
                .map(|t| (t, Source::ExeDir(p.clone())))
                .map_err(|e| format!("cannot read {}: {e}", p.display()));
        }
    }
    match built_in {
        Some(text) => Ok((text.to_string(), Source::BuiltIn)),
        None => {
            let place = exe_dir.map(|d| d.join(FILE_NAME).display().to_string()).unwrap_or_else(|| format!("OmniAPI.exe 旁邊的 {FILE_NAME}"));
            Err(format!(
                "{MISSING} {FILE_NAME}（應該在 {place}）。安裝程式會把它放在 OmniAPI.exe 旁邊，重新安裝可以補回；也可以用環境變數 {ENV_VAR} 指定一個設定檔。"
            ))
        }
    }
}

/// Parse and expand a config text.
pub fn parse(text: &str, ctx: &Context) -> Result<ShellConfig, String> {
    let raw: RawConfig = serde_json::from_str(text).map_err(|e| format!("invalid shell config: {e}"))?;
    if raw.port == 0 {
        return Err("invalid shell config: port must not be 0".into());
    }
    if raw.service.program.trim().is_empty() {
        return Err("invalid shell config: service.program is empty".into());
    }
    if raw.watch.backoff_secs.is_empty() || raw.watch.max_failures == 0 || raw.watch.miss_threshold == 0 {
        return Err("invalid shell config: watch.backoff_secs, max_failures and miss_threshold must not be empty / 0".into());
    }
    let mut vars: Vec<(&str, String)> = vec![
        ("port", raw.port.to_string()),
        ("temp", ctx.temp.clone()),
        ("run_id", ctx.run_id.to_string()),
        ("exe_dir", ctx.exe_dir.clone()),
        ("exe", ctx.exe.clone()),
        // %USERPROFILE%\.omniapi (released build) or \.omniapi-test (test build): lets a config
        // name its build's own default data home, e.g. "OMNIAPI_HOME": "{default_home}"
        ("default_home", crate::identity::default_home(&ctx.user_profile)),
    ];
    if let Some(repo) = &ctx.repo {
        vars.push(("repo", repo.clone()));
    }
    // env values first (they may define OMNIAPI_HOME), then the folders derived from them
    let mut env = BTreeMap::new();
    for (k, v) in &raw.service.env {
        env.insert(k.clone(), expand(v, &vars, &format!("service.env.{k}"))?);
    }
    let data_home = env
        .get("OMNIAPI_HOME")
        .cloned()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| ctx.env_home.clone())
        .unwrap_or_else(|| crate::identity::default_home(&ctx.user_profile));
    // a test build refuses the released app's port and data folder (no-op in the released build)
    crate::identity::check_separate(raw.port, &data_home, &ctx.user_profile)?;
    vars.push(("data_home", data_home.clone()));
    let log_dir = expand(raw.log_dir.as_deref().unwrap_or("{data_home}\\logs"), &vars, "log_dir")?;
    vars.push(("log_dir", log_dir.clone()));

    let s = &raw.service;
    let mut create_dirs = Vec::new();
    for name in &s.create_dirs {
        match env.get(name) {
            Some(v) => create_dirs.push(PathBuf::from(v)),
            None => return Err(format!("invalid shell config: service.create_dirs names {name}, which is not in service.env")),
        }
    }
    let args = s
        .args
        .iter()
        .enumerate()
        .map(|(i, a)| expand(a, &vars, &format!("service.args[{i}]")))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ShellConfig {
        port: raw.port,
        log_dir: PathBuf::from(log_dir),
        data_home: PathBuf::from(data_home),
        service: ServiceSpec {
            enabled: s.enabled,
            program: expand(&s.program, &vars, "service.program")?,
            args,
            cwd: expand(&s.cwd, &vars, "service.cwd")?,
            env,
            create_dirs,
        },
        watch: raw.watch,
        update_check: UpdateCheck { url: expand(&raw.update_check.url, &vars, "update_check.url")?, ..raw.update_check },
    })
}

/// Replace `{name}` with its value; an unknown `{name}` (letters and `_` only) is an error,
/// other braces are left alone.
fn expand(s: &str, vars: &[(&str, String)], field: &str) -> Result<String, String> {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(open) = rest.find('{') {
        out.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        match after.find('}') {
            Some(close) if !after[..close].is_empty() && after[..close].chars().all(|c| c.is_ascii_lowercase() || c == '_') => {
                let name = &after[..close];
                match vars.iter().find(|(k, _)| *k == name) {
                    Some((_, v)) => out.push_str(v),
                    None if name == "repo" => {
                        return Err(format!(
                            "invalid shell config: {field} uses {{repo}}, which only a development build knows (the checkout it was built from); use {{exe_dir}} or a full path"
                        ))
                    }
                    None => return Err(format!("invalid shell config: {field} uses an unknown placeholder {{{name}}}")),
                }
                rest = &after[close + 1..];
            }
            _ => {
                out.push('{');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> Context {
        Context {
            temp: r"C:\T".into(),
            run_id: 42,
            exe_dir: r"C:\Apps\OmniAPI".into(),
            exe: r"C:\Apps\OmniAPI\OmniAPI.exe".into(),
            repo: Some(r"D:\src\OmniAPI".into()),
            env_home: None,
            user_profile: r"C:\Users\u".into(),
        }
    }

    const MINIMAL: &str = r#"{ "service": { "program": "p.exe", "cwd": "." } }"#;

    #[test]
    fn minimal_config_gets_the_defaults() {
        let c = parse(MINIMAL, &ctx()).unwrap();
        assert_eq!(c.port, crate::identity::DEFAULT_PORT);
        assert_eq!(c.watch, Watch::default());
        assert!(c.service.enabled);
        let home = format!(r"C:\Users\u\{}", crate::identity::HOME_DIR);
        assert_eq!(c.data_home, PathBuf::from(&home));
        assert_eq!(c.log_dir, PathBuf::from(&home).join("logs"));
    }

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn released_build_defaults_are_unchanged() {
        let c = parse(MINIMAL, &ctx()).unwrap();
        assert_eq!(c.port, 7788);
        assert_eq!(c.data_home, PathBuf::from(r"C:\Users\u\.omniapi"));
        assert!(c.update_check.enabled);
        let t = r#"{ "service": { "program": "p.exe", "cwd": "{default_home}" } }"#;
        assert_eq!(parse(t, &ctx()).unwrap().service.cwd, r"C:\Users\u\.omniapi");
    }

    #[cfg(feature = "test-identity")]
    #[test]
    fn test_build_refuses_the_released_port_and_home() {
        let c = parse(MINIMAL, &ctx()).unwrap();
        assert_eq!((c.port, c.data_home.clone()), (7939, PathBuf::from(r"C:\Users\u\.omniapi-test")));
        assert!(!c.update_check.enabled, "no new-version check by default");
        let p = r#"{ "port": 7788, "service": { "program": "p.exe", "cwd": "." } }"#;
        assert!(parse(p, &ctx()).unwrap_err().contains("7788"));
        let h = r#"{ "service": { "program": "p.exe", "cwd": ".", "env": { "OMNIAPI_HOME": "C:\\Users\\u\\.omniapi" } } }"#;
        assert!(parse(h, &ctx()).is_err());
        let mut x = ctx();
        x.env_home = Some(r"C:\Users\u\.omniapi\".into());
        assert!(parse(MINIMAL, &x).is_err(), "the shell's own OMNIAPI_HOME pointing at the released home is refused too");
        assert_eq!(BUILT_IN, None, "no development default (it manages 7788)");
    }

    #[test]
    fn placeholders_expand_everywhere() {
        let text = r#"{
            "port": 7826,
            "log_dir": "{temp}\\logs-{run_id}",
            "service": {
                "program": "{exe_dir}\\python\\pythonw.exe",
                "args": ["serve", "--port", "{port}", "--log-file", "{log_dir}\\daemon.log"],
                "cwd": "{data_home}",
                "env": { "OMNIAPI_HOME": "{temp}\\home-{run_id}", "SRC": "{repo}\\mcp" },
                "create_dirs": ["OMNIAPI_HOME"]
            }
        }"#;
        let c = parse(text, &ctx()).unwrap();
        assert_eq!(c.port, 7826);
        assert_eq!(c.service.program, r"C:\Apps\OmniAPI\python\pythonw.exe");
        assert_eq!(c.service.args, vec!["serve", "--port", "7826", "--log-file", r"C:\T\logs-42\daemon.log"]);
        assert_eq!(c.service.cwd, r"C:\T\home-42", "data_home follows the service's own OMNIAPI_HOME");
        assert_eq!(c.service.env["SRC"], r"D:\src\OmniAPI\mcp");
        assert_eq!(c.service.create_dirs, vec![PathBuf::from(r"C:\T\home-42")]);
    }

    #[test]
    fn shell_env_home_is_used_when_the_config_sets_none() {
        let mut x = ctx();
        x.env_home = Some(r"E:\omni".into());
        assert_eq!(parse(MINIMAL, &x).unwrap().log_dir, PathBuf::from(r"E:\omni\logs"));
    }

    #[test]
    fn unknown_placeholder_and_unknown_field_are_errors() {
        let typo = r#"{ "service": { "program": "{exe_dri}\\p.exe", "cwd": "." } }"#;
        assert!(parse(typo, &ctx()).unwrap_err().contains("{exe_dri}"));
        let field = r#"{ "prot": 7826, "service": { "program": "p.exe", "cwd": "." } }"#;
        assert!(parse(field, &ctx()).unwrap_err().contains("prot"));
        let watch = r#"{ "service": { "program": "p.exe", "cwd": "." }, "watch": { "max_failure": 2 } }"#;
        assert!(parse(watch, &ctx()).is_err());
    }

    #[test]
    fn other_braces_are_left_alone() {
        let vars = vec![("port", "1".to_string())];
        assert_eq!(expand(r#"{"a": {port}} {} {X}"#, &vars, "f").unwrap(), r#"{"a": 1} {} {X}"#);
    }

    #[test]
    fn create_dirs_must_name_an_env_entry() {
        let t = r#"{ "service": { "program": "p.exe", "cwd": ".", "create_dirs": ["NOPE"] } }"#;
        assert!(parse(t, &ctx()).unwrap_err().contains("NOPE"));
    }

    #[test]
    fn bad_watch_values_are_refused() {
        let t = r#"{ "service": { "program": "p.exe", "cwd": "." }, "watch": { "backoff_secs": [] } }"#;
        assert!(parse(t, &ctx()).is_err());
        let t = r#"{ "port": 0, "service": { "program": "p.exe", "cwd": "." } }"#;
        assert!(parse(t, &ctx()).is_err());
    }

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn dev_default_runs_the_checkouts_venv() {
        let c = parse(DEV_DEFAULT, &ctx()).unwrap();
        assert_eq!(c.port, 7788);
        assert_eq!(c.service.program, r"D:\src\OmniAPI\mcp\.venv\Scripts\pythonw.exe");
        assert_eq!(c.service.cwd, r"D:\src\OmniAPI\mcp");
        assert!(c.service.args.windows(2).any(|w| w == ["--port", "7788"]));
        // a shell run from the checkout does not offer the logon switch on the settings page
        assert!(!c.service.env.contains_key("OMNIAPI_DESKTOP_EXE"));
    }

    #[test]
    fn exe_placeholder_is_the_running_exe() {
        let t = r#"{ "service": { "program": "p.exe", "cwd": ".", "env": { "X": "{exe}" } } }"#;
        assert_eq!(parse(t, &ctx()).unwrap().service.env["X"], r"C:\Apps\OmniAPI\OmniAPI.exe");
        // same text as tauri-plugin-autostart's app_path: current_exe().display()
        let real = Context::from_env(1);
        assert_eq!(real.exe, std::env::current_exe().unwrap().display().to_string());
    }

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn installed_default_runs_python_from_the_install_folder() {
        // 1.3-M6 ships Python in <install>\python; the service must not run inside the install folder
        let c = parse(INSTALLED_DEFAULT, &ctx()).unwrap();
        assert!(c.service.program.starts_with(r"C:\Apps\OmniAPI\python\"), "{}", c.service.program);
        assert_eq!(c.service.cwd, r"C:\Users\u\.omniapi");
        assert_eq!(c.port, 7788);
        // -I: PYTHONPATH / PYTHONHOME on the user's machine must not reach the bundled Python
        assert_eq!(c.service.args.first().map(String::as_str), Some("-I"));
        assert_eq!(c.service.env["OMNIAPI_GUI_DIST"], r"C:\Apps\OmniAPI\gui");
        // the settings page's 開機時啟動 switch: the service learns which exe the logon entry runs
        assert_eq!(c.service.env["OMNIAPI_DESKTOP_EXE"], r"C:\Apps\OmniAPI\OmniAPI.exe");
        assert!(c.update_check.enabled && c.update_check.url == UPDATE_URL);
    }

    #[test]
    fn test_installed_config_is_separate_from_the_released_one() {
        // package.ps1 -TestIdentity ships config/shell.test.json next to OmniAPI-Test.exe
        let c = parse(TEST_INSTALLED, &ctx()).unwrap();
        assert_eq!(c.port, 7939);
        assert_eq!(c.data_home, PathBuf::from(crate::identity::default_home(r"C:\Users\u")));
        assert_eq!(c.service.env["OMNIAPI_HOME"], crate::identity::default_home(r"C:\Users\u"));
        assert!(!c.update_check.enabled);
        assert!(c.service.env["STORAGE__BASE_PATH"].ends_with(r"\works"), "works never go to Documents\\OmniAPI");
        assert_eq!(c.service.env["OMNIAPI_DESKTOP_RUN_VALUE"], "OmniAPI-Test");
        assert_eq!(c.service.env["OMNIAPI_DEV"], "1");
        assert_eq!(c.service.env["OMNIAPI_OFFLINE"], "1");
    }

    #[test]
    fn update_check_defaults_and_overrides() {
        let c = parse(MINIMAL, &ctx()).unwrap();
        assert_eq!(c.update_check, UpdateCheck::default());
        assert_eq!((c.update_check.interval_hours, c.update_check.enabled), (24, crate::identity::UPDATE_CHECK_DEFAULT));
        let t = r#"{ "service": { "program": "p.exe", "cwd": "." }, "update_check": { "enabled": false, "url": "http://127.0.0.1:{port}/x" } }"#;
        let c = parse(t, &ctx()).unwrap();
        assert!(!c.update_check.enabled);
        assert_eq!(c.update_check.url, format!("http://127.0.0.1:{}/x", crate::identity::DEFAULT_PORT));
        let bad = r#"{ "service": { "program": "p.exe", "cwd": "." }, "update_check": { "enable": false } }"#;
        assert!(parse(bad, &ctx()).is_err());
    }

    #[cfg(all(any(debug_assertions, feature = "dev-default"), not(feature = "test-identity")))]
    #[test]
    fn built_in_repo_is_this_checkout() {
        let repo = PathBuf::from(build_repo().expect("a development build knows its checkout"));
        assert!(repo.join("desktop").join("src-tauri").join("Cargo.toml").is_file(), "{}", repo.display());
        assert_eq!(BUILT_IN, Some(DEV_DEFAULT));
    }

    #[cfg(not(any(debug_assertions, feature = "dev-default")))]
    #[test]
    fn release_build_has_no_built_in_default_nor_checkout() {
        assert_eq!(BUILT_IN, None);
        assert_eq!(build_repo(), None);
    }

    #[cfg(not(feature = "test-identity"))]
    #[test]
    fn repo_placeholder_without_a_checkout_is_an_error() {
        let mut x = ctx();
        x.repo = None;
        let err = parse(DEV_DEFAULT, &x).unwrap_err();
        assert!(err.contains("{repo}") && err.contains("development build"), "{err}");
        // configs that do not use it are fine
        assert!(parse(INSTALLED_DEFAULT, &x).is_ok());
    }

    #[test]
    fn lookup_order_env_then_exe_dir_then_built_in() {
        let dir = std::env::temp_dir().join(format!("omni-cfg-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let env_file = dir.join("env.json");
        std::fs::write(&env_file, "ENV").unwrap();
        std::fs::write(dir.join(FILE_NAME), "EXE").unwrap();

        let (t, s) = locate_with(Some(env_file.to_str().unwrap()), Some(&dir), Some(DEV_DEFAULT)).unwrap();
        assert_eq!((t.as_str(), s), ("ENV", Source::Env(env_file.clone())));
        let (t, s) = locate_with(None, Some(&dir), Some(DEV_DEFAULT)).unwrap();
        assert_eq!((t.as_str(), s), ("EXE", Source::ExeDir(dir.join(FILE_NAME))));
        std::fs::remove_file(dir.join(FILE_NAME)).unwrap();
        let (t, s) = locate_with(Some("  "), Some(&dir), Some(DEV_DEFAULT)).unwrap();
        assert_eq!((t, s), (DEV_DEFAULT.to_string(), Source::BuiltIn));
        // a set but missing env file is an error, never a silent fallback to the defaults
        let err = locate_with(Some(dir.join("missing.json").to_str().unwrap()), Some(&dir), Some(DEV_DEFAULT)).unwrap_err();
        assert!(err.contains("not falling back"), "{err}");
        // a release build (no built-in default): no file = a clear error naming the file, no fallback
        let err = locate_with(None, Some(&dir), None).unwrap_err();
        assert!(err.starts_with(MISSING) && err.contains(FILE_NAME) && err.contains(&dir.join(FILE_NAME).display().to_string()), "{err}");
        assert!(locate_with(None, None, None).unwrap_err().contains(FILE_NAME));
        // the exe-dir file still wins when there is one
        std::fs::write(dir.join(FILE_NAME), "EXE").unwrap();
        assert_eq!(locate_with(None, Some(&dir), None).unwrap().0, "EXE");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn plain_paths() {
        assert_eq!(plain(Path::new(r"\\?\C:\a\b\")), r"C:\a\b");
        assert_eq!(plain(Path::new(r"C:\")), r"C:\");
    }
}
