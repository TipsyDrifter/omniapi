# OmniAPI desktop shell

A thin Tauri 2 app around the local OmniAPI service: a tray icon, one window that loads the
service's own dashboard (`http://127.0.0.1:<port>/` — the same pages a browser tab shows, so the
front end needs no changes), and the service's life — adopt or start it, watch it, restart it, stop
it on quit. Everything else lives in the service and its web pages. MCP clients keep talking to the
service directly; closing the window, or the shell crashing, does not stop the service.

How to install and use it as a user: the root [README](../README.md) (install) and
[USER_GUIDE](../USER_GUIDE.md) (the "桌面版" chapter). This file is for building and releasing it.

## What it does

- **Tray icon** — the picture says how the service is: paper tile with the overprinted "O" = up,
  yellow = starting, red = not answering (being restarted), red "!" = gave up. Tooltip and the
  first menu line say it in words. Left click opens the dashboard. Menu: 開啟看板 / 重啟服務 /
  開機時啟動 (check) / 開啟紀錄資料夾 / 結束; plus 有新版 vX.Y.Z（開啟下載頁） when there is one.
- **Window** — built when needed, X really closes it (the WebView2 processes go away; the shell
  stays in the tray). Size and position are remembered (`%APPDATA%\com.kosa.omniapi\window.json`);
  minimum width 360. While the service is not up it shows a local "starting" page
  (`ui/index.html`) with the time waited, and the reason plus the log locations if the service
  cannot start. Links that leave the service open in the default browser.
- **Service** — at start: answering → adopt it; silent → start it. Health check every 3 s (1 s
  while starting). Our process exiting, or 3 missed checks in a row → kill the tree, start again,
  with backoff 0 / 5 / 15 / 30 s; after 5 failed starts in a row it stops trying (red "!") until
  you choose 重啟服務. Up for 60 s resets the count. Rules and their tests: `src-tauri/src/supervisor.rs`.
- **Quit / restart** — `POST /api/shutdown` first and wait (15 s) for the service to finish by
  itself; only then kill the whole tree. The service runs in a Job Object without kill-on-close,
  so it survives the shell being killed (the next shell adopts it).
- **Single instance** — a second launch hands its arguments to the running shell and exits.
- **Logon** — 開機時啟動 adds `HKCU\...\Run\OmniAPI = "<exe> --background"` (tray and service, no
  window). `omni autostart status` reports when the desktop app has taken over, and
  `omni autostart install` refuses while it has. An old `omni autostart` launcher (Startup-folder
  `OmniAPI Daemon.vbs` or the `OmniAPI Daemon` task) is detected and pointed out in the menu and
  the log, never removed: `omni autostart remove` removes it.
  The settings page (〈05 服務資訊〉) has the same switch: the installed config passes the shell's
  exe to the service as `OMNIAPI_DESKTOP_EXE` (`{exe}`), and `GET` / `PUT /api/desktop/autostart`
  (`{available, enabled, legacy, other}`; body `{"enabled": bool}`) reads or writes that same Run
  value, byte for byte what the plugin writes (`mcp/omniapi_mcp/desktop_autostart.py`). Without
  the variable (a service from a checkout or the zip, or the development config) `available` is
  false, the page shows no switch and `PUT` answers 409. The shell re-reads the value when the
  pointer reaches the tray icon and every 5 s, and rebuilds the menu when it changed
  (`autostart-followed` in `shell.log`).
  **The choice survives a covering install (1.4.1).** Double-clicking a newer installer first runs the
  old version's uninstaller, which deletes the Run value unless Tauri's own updater started it
  (`/UPDATE`); so the shell also keeps the choice in `<data home>autostart.pref` (`~/.omniapi`, or
  `~/.omniapi-test` for the test build; a place no install touches): one line, `autostart=on` or
  `autostart=off`, written atomically (temp file + rename) by every route that changes the switch —
  the tray, `--autostart-on/off`, and the settings page (the service writes the registry itself, so
  the shell records it when its 5 s check sees the change; `autostart-pref` in `shell.log`). Plain
  text, not JSON, because the installer's NSIS hook reads it too and NSIS has no JSON parser. At
  start (`autostart-reconcile` in `shell.log`): choice on and the value missing, or pointing at an exe
  that no longer exists → written again (`autostart-restored`); a value that points at another exe
  that still exists is left alone; choice off → nothing is touched; no file, or an unreadable one
  (the first start after 1.4.0) → the file is seeded from the actual state, never guessed
  (`autostart-pref-seeded`). The installer's post-install hook (`NSIS_HOOK_POSTINSTALL`) does the same
  at once when the file says on — the same value name (`${MAINBINARYNAME}`) and text the plugin writes —
  so the logon entry works even if the app is not started after installing. A genuine uninstall
  leaves the file like every other setting, so a later reinstall turns the entry on again; turning
  the switch off first (or deleting the file) gives the other behaviour. The one upgrade this cannot
  help is 1.4.0 → 1.4.1: 1.4.0 kept no choice and its uninstaller does the deleting.

Command line (scripts and tests; also what a second launch forwards):

| | |
|---|---|
| (none), `--open` | open the window |
| `--background` | tray and service only |
| `--restart-service` | restart the service |
| `--quit` | stop the service, exit |
| `--autostart-on`, `--autostart-off` | same as the 開機時啟動 check mark |

## Config: how the shell starts the service

The shell knows nothing about Python; a JSON file says what to run. Looked up in this order:

1. `OMNIAPI_SHELL_CONFIG` = path of a JSON file. **If it is set but unreadable, the shell does not
   fall back** — a test shell falling back to the defaults would adopt (and on quit, stop) the
   service you use every day on 7788.
2. `shell.config.json` next to the exe — the installer puts `config/shell.installed.json` there.
3. Debug builds only (`cargo run`, `tauri dev`; or a release build with `--features dev-default`):
   built in `config/shell.dev.json` — the service from the checkout the exe was built from, with
   that checkout's `mcp/.venv`, on **port 7788**. A worktree has no `.venv`, so from a worktree
   always use (1). A release build (what the installer ships) has no built-in default and no
   build-time path: without (1) or (2) it says **找不到設定檔 shell.config.json** (where it looked,
   how to get it back) in the tray, the window and `shell.log`, and starts nothing.

```json
{
  "comment": "free text",
  "port": 7788,
  "log_dir": "{data_home}\\logs",
  "service": {
    "enabled": true,
    "program": "{repo}\\mcp\\.venv\\Scripts\\pythonw.exe",
    "args": ["-m", "omniapi_mcp.cli", "serve", "--foreground", "--port", "{port}", "--log-file", "{log_dir}\\daemon.log"],
    "cwd": "{repo}\\mcp",
    "env": { "OMNIAPI_HOME": "..." },
    "create_dirs": ["OMNIAPI_HOME"]
  },
  "watch": { "poll_secs": 3, "miss_threshold": 3, "startup_timeout_secs": 180, "stable_secs": 60,
             "max_failures": 5, "backoff_secs": [0, 5, 15, 30], "shutdown_grace_secs": 15 }
}
```

Placeholders: `{port}`, `{temp}`, `{run_id}` (start time), `{exe_dir}`, `{exe}` (the running exe, as the logon entry writes it), `{repo}` (build-time
checkout; debug builds only, an error in a release build), `{data_home}` (`OMNIAPI_HOME` from `service.env`, else the shell's, else
`%USERPROFILE%\.omniapi`), `{log_dir}`. Unknown fields and unknown placeholders are errors (shown
in the tray and the window). `service.enabled: false` = tray and window only, nothing is started,
restarted or stopped.

The installed default (`config/shell.installed.json`) runs `{exe_dir}\python\pythonw.exe -I`
(the bundled Python; `-I` keeps `PYTHONPATH`/`PYTHONHOME` and the current folder out) in the data
home, with `OMNIAPI_GUI_DIST={exe_dir}\gui` and `OMNIAPI_DESKTOP_EXE={exe}` (the settings page's 開機時啟動 switch).

`update_check` (optional): `{ "enabled": true, "url": "<GitHub latest-release API>", "interval_hours": 24,
"retry_hours": 6, "delay_secs": 60 }` — see "New versions" below. The installed default has it on,
the built-in development default (debug builds) off.

## New versions

The app tells you about a new version; it does not update itself. At most once a day (the last
check's time is kept in `{data_home}\desktop-update.json`, so restarts do not add requests) the shell
makes one anonymous `GET` of `https://api.github.com/repos/TipsyDrifter/omniapi/releases/latest` through
Windows' own WinHTTP (system proxy and certificates; no token, nothing about the user). When
`tag_name` is newer than the running version the tray menu gets **有新版 vX.Y.Z（開啟下載頁）**, which
opens the release page; the user downloads the new installer and runs it (it stops the running copy
first, see Installer). The same result is in `/api/status` → `desktop_update` (`{enabled, current,
latest, newer, url, checked_at_ms, attempted_at_ms, error}`; `null` without the desktop app), which the
settings page shows under 服務資訊. Offline, rate-limited or any other failure: one
`update-check-failed` line in `shell.log`, nothing shown, next try 6 hours later.
`"update_check": {"enabled": false}` in the shell config turns it off.

One-click updating with Tauri's updater plugin was tried and left out: it needs a signing key kept
for every installed copy, and a failed or blocked update leaves no message on screen.

## Logs

All in the log folder (`{data_home}\logs`, i.e. `%USERPROFILE%\.omniapi\logs` by default; the
tray's 開啟紀錄資料夾 opens it):

- `shell.log` — the shell, one line per event (`adopt`, `spawn`, `phase-*`, `failed`, `give-up`,
  `shutdown-requested`, `stopped how=graceful|forced`, `window-*`, `autostart*`); kept under 2 MB
  with one old copy.
- `daemon.log` — the service's own log (rotated by the service).
- `service.console.log` — whatever the service printed before its log was set up (import errors).

## Develop and build

Needs Rust (stable), Node, and WebView2 (part of Windows 11).

```powershell
cd desktop
npm install                       # the Tauri CLI (devDependency); node_modules is git-ignored
cd src-tauri; cargo test          # unit tests: config, supervisor rules, http parsing, paths, window place
```

Build the shell alone (exe + NSIS installer; outputs go to `%TEMP%`, keep big build output out of
the repo folder):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build.ps1
```

That installer has no Python: the installed app says the service cannot start. **The real
installer, with the service:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\package.ps1
```

From a clean checkout it: checks the seven version numbers (`scripts/build_release.py --check-versions`)
→ `uv python install 3.10` (uv's standalone CPython, copied) → installs `uv export --frozen --no-dev`
into that Python's own `Lib\site-packages` with `--require-hashes`, `claude-agent-sdk` from its sdist
(no bundled 230 MB `claude.exe`; agent runs on Claude use the user's own Claude Code) → installs
`omniapi_mcp` itself there (a real install: no `pyproject.toml` beside it = the service's installed
layout) and checks every data file arrived → removes what the service never uses (Tk/IDLE/turtle,
pip/ensurepip/`Scripts`, C headers and import libraries, `--target`'s console scripts, third-party
`tests` folders) → compiles every `.py` to `unchecked-hash` bytecode (never compared with timestamps,
never rewritten in the install folder) → `npm ci && npm run build` in `gui/` → `tauri build` with an
overlay config (`<stage>\tauri.package.json`: `python\`, `gui\`, `omni.cmd`, NSIS lzma;
`tauri.conf.json` is not changed). Everything big stays in the stage folder (`-Stage`, default
`%TEMP%\omniapi-proto\m6\`: uv's Python and cache, `payload\`, `installers\OmniAPI_<version>_x64-setup.exe`),
the log in `package.log`. `-SkipPython` / `-SkipGui` reuse the staged parts, `-NoInstaller` stops
before cargo. First run about 14 minutes, then about 9–12.

**The test build** (`package.ps1 -TestIdentity`, stage `%TEMP%\omniapi-proto\m6-test`, its own cargo target
folder): `OmniAPI-Test_<version>_x64-setup.exe` installs `OmniAPI-Test.exe` with nothing in common with the
released app — Tauri identifier `com.kosa.omniapi.test` (single-instance mutex, `%APPDATA%` / WebView2 folders),
product, exe, install folder, Start menu entry and uninstall key `OmniAPI-Test` (the installer's "stop the
running app" looks for `OmniAPI-Test.exe` only), logon value `Run\OmniAPI-Test`, default port 7939 (a config
naming 7788 is refused), default data home `%USERPROFILE%\.omniapi-test` (`.omniapi` is refused), no
new-version check; it ships `config\shell.test.json` (offline, works under its data home, a stand-in Claude
config, `OMNIAPI_DESKTOP_RUN_VALUE=OmniAPI-Test`) and `config\omni.test.cmd` (the same defaults for the command
line; `omni autostart install/remove` refused). The differences live in `src-tauri/src/identity.rs` (cargo
feature `test-identity`) and the overlay config; the released values are unchanged. After every build
`scripts/check-identity.ps1` reads the exe's bytes, Tauri's generated `installer.nsi` and the shipped config and
`omni.cmd`, and the build fails unless they are the identity asked for. Use the test build to test installers
on a machine where the released app runs; `m6-install-test.ps1` defaults to it.

Installed (per user, no admin, `%LOCALAPPDATA%\OmniAPI` by default): `OmniAPI.exe`, `shell.config.json`,
`nsis-stop.ps1`, `uninstall.exe`, `python\`, `gui\`, and `omni.cmd` — the command line for people who
want it (`"<install>\omni.cmd" status`, `works`, `chat`…; not on PATH). It runs
`python -I -m omniapi_mcp.cli`; `omni serve` started from it is the same service the shell starts
(same Python with `-I`, data home as working folder, `gui\` found next to `python\`, `daemon.log` in
the data home). `omni stop` says so when the desktop app is running (the app starts the service
again; 結束 in its tray menu stops both). `omni autostart status` knows the app's logon entry and
`omni autostart install` refuses while it is on.

Both scripts build with rustc's `--remap-path-prefix` (`scripts/neutral-paths.ps1`: the user folder,
`%TEMP%`, `CARGO_HOME`, cargo's build and target folders and the checkout become `/home`, `/tmp`,
`/cargo`, `/build`, `/target`, `/omniapi`; Cargo's `trim-paths` is not stable yet), so the source paths
in panic messages name no folder of the build machine. Afterwards `scripts/check-embedded-paths.ps1`
searches the exe's bytes (8-bit and UTF-16LE) for this checkout, the main checkout, the user folder,
`CARGO_HOME`, `%TEMP%` and any `C:\Users\` path, and the build fails on a hit. Run it alone on any exe:
`powershell -File desktop\scripts\check-embedded-paths.ps1 -Path <exe>`. New flags = every dependency is
compiled once more.

`build.ps1` and `package.ps1` refuse to start at ≥ 90 % commit charge and compile 4 at a time
(`-Jobs`). The exe is `%TEMP%\omniapi-proto\cargo-target-desktop\release\OmniAPI.exe`.

Versions: `src-tauri/tauri.conf.json` (installer, registry, what the shell reports) and
`src-tauri/Cargo.toml` (the exe's file version) both equal the project version;
`scripts/build_release.py` checks them with the other five places (`mcp/pyproject.toml`,
`mcp/omniapi_mcp/__init__.py`, `mcp/manifest.json`, `mcp/uv.lock`, `gui/package.json`).

Icons: `node scripts/make-icons.mjs` draws the tray icons (`src-tauri/icons/tray/`) and the app
icon source from the dashboard's D3 colours, checks their contrast on light and dark taskbars and
writes a preview to `%TEMP%\omniapi-tray-preview.png`; then
`npx tauri icon src-tauri/icons/icon-source.png -o src-tauri/icons` and keep only the four files
`tauri.conf.json` lists.

## Releasing

A release has five files, all with ASCII names (GitHub drops other characters from asset names):

| file | what |
|---|---|
| `OmniAPI_<version>_x64-setup.exe` | the desktop installer (this folder) |
| `omniapi-v<version>.zip` | the source tree with the built GUI, for the zip + `uv` install |
| `omniapi-mcp.dxt` | the stdio MCP server as a Claude Desktop extension |
| `omniapi-skill.zip` | the skill for Claude |
| `SHA256SUMS.txt` | SHA-256 of the four above (the installer is unsigned; this is what users can check) |

Order (from a clean checkout of the tagged commit; nothing here pushes anything until the last step):

1. Bump the seven version numbers, `uv lock` in `mcp/`, commit; `python scripts/build_release.py --check-versions`
   (any Python 3.10+, e.g. `uv run python` in `mcp/`).
2. `git tag -a v<version> -m "<one sentence for the public commit>"` — the public mirror's commit message
   is the tag's message, so no internal wording.
3. `powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\package.ps1` →
   `<stage>\installers\OmniAPI_<version>_x64-setup.exe`.
4. `npm run build --prefix gui` if `gui/dist` is older than `gui/src` (`package.ps1` already built it).
5. `cd mcp && uv run python ../scripts/build_release.py --desktop-installer <stage>\installers\OmniAPI_<version>_x64-setup.exe`
   → `dist/` with the five files (it checks the installer's file name carries the same version).
6. Install check: unzip `dist/omniapi-v<version>.zip` to a temp folder, `uv sync` there, start an offline
   sandbox on another port and run `scripts/verify_install.py`; build the same commit with `-TestIdentity` and
   install that one into a temp folder with `desktop/scripts/m6-install-test.ps1` (it never touches 7788; the
   released installer is never run on a machine where the released app is installed).
7. `bash scripts/mirror-publish.sh --tag v<version> --dry-run` — the four safety nets must pass.
8. `bash scripts/mirror-publish.sh --tag v<version> --release --notes scripts/release-notes/v<version>.md`
   pushes the public mirror and creates its Release with the five files.

## Verification scripts

All run the shell against a test config with a fresh `OMNIAPI_HOME`, refuse to start without one,
and check that the service on 7788 kept its pid.

| script | what |
|---|---|
| `scripts/lifecycle-test.ps1` | (port 7826) cold start, single instance, X, kill service / tree / adopted service → brought back, shell crash → adopt, restart, quit (graceful, zero leftovers) |
| `scripts/giveup-test.ps1` | a service that exits at once / a missing program: attempts, backoff, gives up, stays idle, manual restart, quit |
| `scripts/autostart-test.ps1` | logon entry on/off, `omni autostart status`, the old-launcher warning (faked APPDATA); restores the registry |
| `scripts/screens-test.ps1` | screenshots of the starting page and the dashboard |
| `scripts/install-test.ps1` | (shell-only installer) silent install to a temp folder, run, update over a running copy, uninstall, clean up |
| `scripts/m6-install-test.ps1` | (the real installer, by default the test build: `OMNIAPI_M6_IDENTITY=test`; `released` is refused while any `OmniAPI.exe` runs; port **7829**, or `OMNIAPI_M6_PORT` 7800–7949; folder `%TEMP%\omniapi-proto\m6`, or `OMNIAPI_M6_DIR` under `%TEMP%`; guard in `m6-common.ps1`, which also overwrites each test install's `shell.config.json` with the test config) install, first / second start with PATH = Windows folders only, layout, `omni.cmd`, settings across a restart, the e2e scripts, update over the running copy and back, **`autostart-upgrade`** (the logon entry across covering installs: needs `-Setup2`; see Installer), Defender, uninstall while running, a path with a space and CJK characters |
| `scripts/nsis-hook-test.ps1` | (seconds, no Tauri build) the post-install hook alone: a tiny installer made with `makensis` runs `NSIS_HOOK_POSTINSTALL` against `autostart.pref` files in temp folders (on / off / CRLF / garbage / missing / a folder; `OMNIAPI_HOME` or the default `.omniapi` / `.omniapi-test` folder per identity; Task Manager flag); writes only `RunOmniAPI-Test` (put back) and a made-up name, never `RunOmniAPI` |
| `scripts/m6-update-check-test.ps1` | the daily new-version check against a local stand-in for the release API |

The lifecycle scripts run the service from the main checkout's `mcp/.venv` (found through git, or
`OMNIAPI_MAIN_CHECKOUT`), read-only.

## Installer

`tauri.conf.json` → NSIS, `installMode: currentUser`. `nsis-hooks.nsh` runs before files are
copied and before they are removed: `nsis-stop.ps1` (shipped in the install folder) stops the
shell, asks the service to shut down (`POST /api/shutdown`) — found from the process side: a python
whose program is in the install folder, the port it listens on, and `/api/health` there naming that
same pid, so whatever port it was configured for it is asked, and a service anywhere else is never
asked or even queried — then stops whatever still runs from there, by path, never by name. Then
`python\` and `gui\` are removed whole (only those two, only when `OmniAPI.exe` is beside them), so an
update replaces every file and nothing is left behind by an uninstall. Uninstall leaves `~/.omniapi`
and `Documents\OmniAPI` alone. After an update over a running copy the app is not started again by
itself (the finish page offers to start it).

`NSIS_HOOK_POSTINSTALL` puts the logon entry back after a covering install when `autostart.pref` in the
data home says on (see Logon above; data home = `OMNIAPI_HOME` if set in the installer's environment,
else `%USERPROFILE%.omniapi`, `.omniapi-test` for the test build), and appends a line `autostart-restore: …`
to `%TEMP%omniapi-installer-stop.log`. `m6-install-test.ps1 -Steps autostart-upgrade -Setup <A> -Setup2 <B>`
shows it with two builds. A silent install (`/S`) skips the installer's "already installed" page and never
runs the old uninstaller, so it cannot show the bug; the step therefore runs the sequence that page runs
(the old `uninstall.exe /S _?=<folder>`, no `/UPDATE`, then the new installer). With no stored choice the
covering install clears the entry (the 1.4.0 behaviour, as a control); with the choice on the value is there right after the install, before the app
runs; the shell repairs a deleted or stale value at its start; off stays off; seeding; and the settings
page's `PUT` is followed. Build the second installer with `package.ps1 -TestIdentity -Version 1.4.1
-SkipPython -SkipGui` (tests only; the version is only the installer's), `-CargoTargetName` keeps a
private cargo target when other builds run at the same time.

Known limits: the logon entry is the exe path unquoted (tauri-plugin-autostart writes it that way);
the per-user install folder has no spaces unless the Windows user name does.
