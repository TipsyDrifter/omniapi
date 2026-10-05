# Build the desktop installer WITH the Python service:
#   standalone CPython 3.10 + the locked runtime dependencies in its own site-packages
#   + omniapi_mcp itself (not editable, no pyproject.toml next to it = the service's installed layout)
#   + the built GUI + omni.cmd, bundled by Tauri into one NSIS (lzma) installer.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\package.ps1 [options]
#
#   -Stage <dir>       work folder (default %TEMP%\omniapi-proto\m6). Everything big lives there:
#                      uv's Python and cache, the staged payload, the Tauri overlay config, the installer.
#   -SkipPython        reuse the staged python\ (only re-copy the GUI and rebuild the installer)
#   -SkipGui           reuse the staged gui\
#   -NoInstaller       stop after staging (no cargo / tauri)
#   -Version <x.y.z>   build the installer under another version (tests only: tauri.conf.json is
#                      NOT changed, the exe's file version stays Cargo.toml's; never for a release)
#   -ShellConfig <f>   ship this file as shell.config.json instead of config\shell.installed.json
#                      (tests only, e.g. the update experiment bakes in a test port)
#   -Jobs <n>          parallel rustc (default 4; this machine idles near 88% commit charge)
#
# The exe is built with source paths remapped to neutral names (neutral-paths.ps1) and then
# searched for this machine's folders (check-embedded-paths.ps1); any hit fails the build.
#
# Needs: uv (%USERPROFILE%\.local\bin\uv.exe or on PATH), Node + npm, Rust. Network for the first run
# (CPython, wheels, npm packages, the claude-agent-sdk sdist build backend).
# Nothing is written inside the repo except gui\dist and node_modules (both git-ignored).
param(
    [string]$Stage = (Join-Path $env:TEMP 'omniapi-proto\m6'),
    [switch]$SkipPython,
    [switch]$SkipGui,
    [switch]$NoInstaller,
    [string]$Version = '',
    [string]$ShellConfig = '',
    [int]$Jobs = 4,
    [switch]$SkipMemoryCheck
)
$ErrorActionPreference = 'Stop'
$DesktopDir = Split-Path -Parent $PSScriptRoot
$RepoDir = Split-Path -Parent $DesktopDir
$TargetDir = Join-Path $env:TEMP 'omniapi-proto\cargo-target-desktop'
$Payload = Join-Path $Stage 'payload'
$Py = Join-Path $Payload 'python'
$Build = Join-Path $Stage 'build'
$LogFile = Join-Path $Stage 'package.log'
New-Item -ItemType Directory -Force $Stage, $Build | Out-Null

function Log([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}
function Bytes([string]$dir) { if (Test-Path -LiteralPath $dir) { (Get-ChildItem -LiteralPath $dir -Recurse -File -Force | Measure-Object Length -Sum).Sum } else { 0 } }
function Files([string]$dir) { if (Test-Path -LiteralPath $dir) { @(Get-ChildItem -LiteralPath $dir -Recurse -File -Force).Count } else { 0 } }
function MB([double]$b) { "{0:N1} MB" -f ($b / 1MB) }
function Run([string]$what, [scriptblock]$block) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $block
    if ($LASTEXITCODE) { throw "$what failed (exit $LASTEXITCODE)" }
    Log ("{0}: {1:N1}s" -f $what, $sw.Elapsed.TotalSeconds)
}

$total = [Diagnostics.Stopwatch]::StartNew()
Log "==== package.ps1  stage=$Stage  repo=$RepoDir"

# ---------------------------------------------------------------- uv, isolated from the user's config
$Uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $Uv)) { $Uv = (Get-Command uv.exe -ErrorAction Stop).Source }
$env:UV_NO_CONFIG = '1'                # no user/system uv.toml ... which also drops .python-version: always pass --python 3.10
$env:UV_CACHE_DIR = Join-Path $Stage 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Stage 'uv-python'
$env:UV_PYTHON_BIN_DIR = Join-Path $Stage 'uv-bin'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_LINK_MODE = 'copy'
Remove-Item Env:VIRTUAL_ENV, Env:PYTHONHOME, Env:PYTHONPATH, Env:UV_PYTHON -ErrorAction SilentlyContinue
$PyVersion = (Get-Content (Join-Path $RepoDir 'mcp\.python-version') -Raw).Trim()   # 3.10

# ---------------------------------------------------------------- 0. versions (the seven places agree)
Run "uv python install $PyVersion" { & $Uv python install $PyVersion --quiet }
$BasePy = Get-ChildItem $env:UV_PYTHON_INSTALL_DIR -Directory | Where-Object { $_.Name -like "cpython-$PyVersion*-windows-x86_64-none" -and (Test-Path (Join-Path $_.FullName 'python.exe')) } | Sort-Object Name | Select-Object -Last 1
if (-not $BasePy) { throw "no CPython $PyVersion under $env:UV_PYTHON_INSTALL_DIR" }
Log "CPython: $($BasePy.Name)"
& (Join-Path $BasePy.FullName 'python.exe') (Join-Path $RepoDir 'scripts\build_release.py') --check-versions
if ($LASTEXITCODE) { throw "the seven version numbers do not agree (scripts\build_release.py --check-versions)" }
$tauriConf = Get-Content (Join-Path $DesktopDir 'src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$AppVersion = if ($Version) { $Version } else { $tauriConf.version }
Log "app version: $AppVersion$(if ($Version) { '  (override for tests; not a release build)' })"

# ---------------------------------------------------------------- 1. Python payload
if (-not $SkipPython) {
    if (Test-Path $Py) { Remove-Item -LiteralPath $Py -Recurse -Force }
    New-Item -ItemType Directory -Force $Payload | Out-Null
    & robocopy.exe $BasePy.FullName $Py /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    $LASTEXITCODE = 0
    # uv marks its Pythons as externally managed; this copy is ours and only --target installs go in
    Remove-Item (Join-Path $Py 'Lib\EXTERNALLY-MANAGED') -ErrorAction SilentlyContinue
    $rawBase = Bytes $Py; $rawBaseFiles = Files $Py
    Log "base CPython copied: $(MB $rawBase), $rawBaseFiles files"

    $req = Join-Path $Build 'requirements.txt'
    Run 'uv export (locked runtime deps, no dev)' { & $Uv export --frozen --no-dev --no-emit-project --format requirements-txt --project (Join-Path $RepoDir 'mcp') --python $PyVersion --quiet -o $req }
    $site = Join-Path $Py 'Lib\site-packages'
    $pyExe = Join-Path $Py 'python.exe'
    # claude-agent-sdk from its sdist: no bundled 230 MB claude.exe; the user's own Claude Code is used
    Run 'uv pip install --target (deps)' { & $Uv pip install --python $pyExe --target $site --no-binary claude-agent-sdk --require-hashes -r $req --quiet }
    # the package itself: a real (non-editable) install, so there is no pyproject.toml next to it
    Run 'uv pip install --target (omniapi-mcp)' { & $Uv pip install --python $pyExe --target $site --no-deps (Join-Path $RepoDir 'mcp') --quiet }
    $rawAll = Bytes $Py; $rawAllFiles = Files $Py
    Log "after install: python\ $(MB $rawAll), $rawAllFiles files"

    # --- checks: installed layout, data files, no bundled CLI
    $pkg = Join-Path $site 'omniapi_mcp'
    if (Test-Path (Join-Path $site 'pyproject.toml')) { throw "pyproject.toml next to omniapi_mcp: the service would think it runs from a repo" }
    foreach ($rel in 'catalog\catalog.json', 'prompts\data', 'resources\models', 'daemon\app.py') {
        if (-not (Test-Path (Join-Path $pkg $rel))) { throw "missing from the installed package: omniapi_mcp\$rel" }
    }
    $srcData = @(Get-ChildItem (Join-Path $RepoDir 'mcp\omniapi_mcp') -Recurse -File | Where-Object { $_.Extension -notin '.py', '.pyc' -and $_.FullName -notmatch '\\__pycache__\\' })
    $missing = @($srcData | Where-Object { -not (Test-Path (Join-Path $pkg $_.FullName.Substring((Join-Path $RepoDir 'mcp\omniapi_mcp').Length + 1))) })
    if ($missing.Count) { throw "data files not installed: $(($missing | Select-Object -First 5 | ForEach-Object Name) -join ', ')" }
    Log "omniapi_mcp installed with all $($srcData.Count) data files"
    $bundled = @(Get-ChildItem $site -Recurse -Filter 'claude*.exe' -File)
    if ($bundled.Count) { throw "a bundled Claude CLI got in: $($bundled[0].FullName)" }

    # --- slimming: only what the service can never use (each one re-checked by the install tests)
    $removed = [ordered]@{}
    function Drop([string]$label, [string[]]$paths) {
        $b = 0
        foreach ($p in $paths) {
            if (Test-Path -LiteralPath $p) {
                $b += if ((Get-Item -LiteralPath $p -Force).PSIsContainer) { Bytes $p } else { (Get-Item -LiteralPath $p -Force).Length }
                Remove-Item -LiteralPath $p -Recurse -Force
            }
        }
        if ($b) { $removed[$label] = $b }
    }
    Drop 'stdlib test suite (Lib\test, Lib\*\tests)' (@(Join-Path $Py 'Lib\test') + @(Get-ChildItem (Join-Path $Py 'Lib') -Directory | Where-Object { $_.Name -ne 'site-packages' } | ForEach-Object { Join-Path $_.FullName 'tests'; Join-Path $_.FullName 'test' }))
    Drop 'Tk / IDLE / turtle (Lib\tkinter, idlelib, turtledemo, turtle.py, tcl\, _tkinter, tcl/tk dlls)' (@('Lib\tkinter', 'Lib\idlelib', 'Lib\turtledemo', 'Lib\turtle.py', 'tcl', 'DLLs\_tkinter.pyd', 'DLLs\tcl86t.dll', 'DLLs\tk86t.dll') | ForEach-Object { Join-Path $Py $_ })
    Drop 'pip / ensurepip / Scripts launchers' (@(@('Lib\ensurepip', 'Scripts') | ForEach-Object { Join-Path $Py $_ }) + @(Get-ChildItem $site -Directory | Where-Object { $_.Name -match '^pip(-[\d.]+\.dist-info)?$' } | ForEach-Object FullName))
    Drop 'C headers and import libraries (include\, libs\)' (@('include', 'libs') | ForEach-Object { Join-Path $Py $_ })
    Drop 'console scripts installed by --target (site-packages\bin)' @(Join-Path $site 'bin')
    Drop 'third-party test folders (site-packages\*\tests)' @(Get-ChildItem $site -Directory | ForEach-Object { Join-Path $_.FullName 'tests' } | Where-Object { Test-Path $_ })
    Drop 'old bytecode (all __pycache__; rebuilt below)' @(Get-ChildItem $Py -Recurse -Directory -Filter '__pycache__' -Force | ForEach-Object FullName)
    foreach ($k in $removed.Keys) { Log ("  removed {0,9}  {1}" -f (MB $removed[$k]), $k) }

    # --- bytecode: unchecked-hash = used as is, never compared with the .py's timestamp (the
    #     installer may not keep timestamps), never rewritten in the install folder
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $out = & $pyExe -I -m compileall -q -f -j 0 --invalidation-mode unchecked-hash (Join-Path $Py 'Lib')
    $LASTEXITCODE = 0
    $bad = @($out | Where-Object { "$_" -match '^\*\*\* Error compiling' })
    Log ("compileall (unchecked-hash, parallel): {0:N1}s, {1} reported errors" -f $sw.Elapsed.TotalSeconds, $bad.Count)
    $bad | Select-Object -First 10 | ForEach-Object { Log "    $_" }
    # Parallel workers on Windows occasionally fail to write one .pyc (seen once: Lib\abc.py). Check every
    # .py has an unchecked-hash .pyc (header flags = 1) and compile the missing ones one by one; a file
    # that really cannot compile is listed (it is never imported, or the import probe below fails).
    $check = @'
import importlib.util, pathlib, py_compile, sys
root = pathlib.Path(sys.argv[1]); fixed = []; broken = []
for py in root.rglob("*.py"):
    pyc = pathlib.Path(importlib.util.cache_from_source(str(py)))
    ok = pyc.is_file() and int.from_bytes(pyc.read_bytes()[4:8], "little") == 1
    if ok:
        continue
    try:
        py_compile.compile(str(py), cfile=str(pyc), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        fixed.append(str(py.relative_to(root)))
    except Exception as e:
        broken.append(f"{py.relative_to(root)}: {type(e).__name__}")
print(f"fixed={len(fixed)} broken={len(broken)}")
for f in fixed[:10]: print("fixed", f)
for b in broken[:20]: print("broken", b)
'@
    $checkFile = Join-Path $Build 'check_pyc.py'
    [IO.File]::WriteAllText($checkFile, $check)
    $res = & $pyExe -I -B $checkFile (Join-Path $Py 'Lib')
    if ($LASTEXITCODE) { throw "bytecode check failed: $res" }
    $res | ForEach-Object { Log "  bytecode check: $_" }

    # --- the service imports from here (no PATH, no PYTHON* variables: -I; -B: write no bytecode of its own)
    $probe = & $pyExe -I -B -c "import omniapi_mcp, omniapi_mcp.layout as l, omniapi_mcp.daemon.app, claude_agent_sdk, uvicorn, sys; print(omniapi_mcp.__version__, l.layout_name(), sys.version.split()[0])"
    if ($LASTEXITCODE -or "$probe" -notmatch ' installed ') { throw "import probe failed: $probe" }
    Log "import probe: $probe"
    $slim = Bytes $Py; $slimFiles = Files $Py
    Log ("python\ before slimming {0} ({1} files) -> after {2} ({3} files); base CPython was {4}" -f (MB $rawAll), $rawAllFiles, (MB $slim), $slimFiles, (MB $rawBase))
}
if (-not (Test-Path (Join-Path $Py 'pythonw.exe'))) { throw "no staged Python in $Py (run without -SkipPython)" }

# ---------------------------------------------------------------- 2. GUI
$GuiDir = Join-Path $RepoDir 'gui'
if (-not $SkipGui) {
    Push-Location $GuiDir
    try {
        if (-not (Test-Path 'node_modules')) { Run 'npm ci (gui)' { npm ci --no-audit --no-fund } }
        Run 'npm run build (gui)' { npm run build }
    } finally { Pop-Location }
    $guiOut = Join-Path $Payload 'gui'
    if (Test-Path $guiOut) { Remove-Item -LiteralPath $guiOut -Recurse -Force }
    & robocopy.exe (Join-Path $GuiDir 'dist') $guiOut /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy gui failed ($LASTEXITCODE)" }
    $LASTEXITCODE = 0
}
if (-not (Get-ChildItem (Join-Path $Payload 'gui\assets') -Filter 'index-*.js' -ErrorAction SilentlyContinue)) { throw "staged gui has no assets\index-*.js" }
Log "gui: $(MB (Bytes (Join-Path $Payload 'gui'))), $(Files (Join-Path $Payload 'gui')) files"

# ---------------------------------------------------------------- 3. omni.cmd + payload summary
Copy-Item (Join-Path $DesktopDir 'config\omni.cmd') (Join-Path $Payload 'omni.cmd') -Force
Log "payload total: $(MB (Bytes $Payload)), $(Files $Payload) files"
if ($NoInstaller) { Log ("done (no installer) in {0:N0}s" -f $total.Elapsed.TotalSeconds); return }

# ---------------------------------------------------------------- 4. Tauri bundle (overlay config: the repo's tauri.conf.json is not touched)
$m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$pct = [math]::Round(100.0 * $m.CommittedBytes / $m.CommitLimit, 1)
Log "commit charge $pct%"
if ($pct -ge 90 -and -not $SkipMemoryCheck) { throw "commit charge >= 90%; not building (wait, or lower -Jobs)" }
$cfgFile = if ($ShellConfig) { (Resolve-Path $ShellConfig).Path } else { Join-Path $DesktopDir 'config\shell.installed.json' }
$overlay = [ordered]@{
    bundle = [ordered]@{
        resources = [ordered]@{
            # replace the base entry (JSON merge patch: null removes it) so the config is listed once
            '../config/shell.installed.json'  = $null
            $cfgFile                          = 'shell.config.json'
            (Join-Path $Payload 'python\')    = 'python\'
            (Join-Path $Payload 'gui\')       = 'gui\'
            (Join-Path $Payload 'omni.cmd')   = 'omni.cmd'
        }
        windows   = [ordered]@{ nsis = [ordered]@{ compression = 'lzma' } }
    }
}
if ($Version) { $overlay['version'] = $Version }
$overlayFile = Join-Path $Stage 'tauri.package.json'
[IO.File]::WriteAllText($overlayFile, ($overlay | ConvertTo-Json -Depth 6))

$env:CARGO_TARGET_DIR = $TargetDir
$env:CARGO_BUILD_JOBS = "$Jobs"
# no build-machine paths in the exe: rustc remaps them (neutral-paths.ps1), the check below proves it
. (Join-Path $PSScriptRoot 'neutral-paths.ps1')
$dirs = Set-NeutralPathFlags (Join-Path $DesktopDir 'src-tauri') { param($m) Log $m }
$cli = Join-Path $DesktopDir 'node_modules\@tauri-apps\cli\tauri.js'
if (-not (Test-Path $cli)) { Push-Location $DesktopDir; try { Run 'npm ci (desktop)' { npm ci --no-audit --no-fund } } finally { Pop-Location } }
Push-Location $DesktopDir
try { Run 'tauri build (exe + NSIS lzma)' { & node $cli build --bundles nsis --config $overlayFile } } finally { Pop-Location }

# ---------------------------------------------------------------- 5. the shipped exe names no folder of this machine
$exe = Join-Path $TargetDir 'release\OmniAPI.exe'
$check = & (Join-Path $PSScriptRoot 'check-embedded-paths.ps1') -Path $exe -Extra @($dirs.BuildDir, $dirs.TargetDir)
$checkExit = $LASTEXITCODE
$check | ForEach-Object { Log "  $_" }
if ($checkExit) { throw "the exe contains build-machine paths (check-embedded-paths.ps1 exit $checkExit); not shipping it" }

$nsisDir = Join-Path $TargetDir 'release\bundle\nsis'
$setup = Get-ChildItem $nsisDir -Filter "*_${AppVersion}_x64-setup.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $setup) { throw "no installer for $AppVersion in $nsisDir" }
$outDir = Join-Path $Stage 'installers'
New-Item -ItemType Directory -Force $outDir | Out-Null
$dest = Join-Path $outDir ("OmniAPI_{0}_x64-setup.exe" -f $AppVersion)   # ASCII name (GitHub drops non-ASCII)
Copy-Item $setup.FullName $dest -Force
$hash = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
Log ("installer {0}  {1} ({2:N0} bytes)  sha256 {3}" -f $dest, (MB (Get-Item $dest).Length), (Get-Item $dest).Length, $hash)
Log ("done in {0:N0}s" -f $total.Elapsed.TotalSeconds)
