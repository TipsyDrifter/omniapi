# Shared guard + helpers for the 1.3-M6 installer tests (dot-source it). ASCII only.
#
# SAFETY. An installed OmniAPI.exe reads shell.config.json next to it, which manages the owner's
# real service on 7788 (its Quit stops it). So nothing here starts an installed shell unless:
#   - OMNIAPI_SHELL_CONFIG points at a test config whose port is 7829 (never 7788) and whose
#     service env has OMNIAPI_HOME (a fresh folder under %TEMP%), OMNIAPI_DEV=1, OMNIAPI_OFFLINE=1;
#   - no OmniAPI.exe runs from anywhere else (a second launch would be handed to that one by
#     the single-instance plugin);
# and, belt and braces, every test install gets its shell.config.json replaced by the test
# config right after installing (Protect-Install), so even a shell started without the variable
# (e.g. relaunched by an installer) stays on 7829.

# 1.4: OMNIAPI_M6_IDENTITY = test (default) | released picks which build is tested. The TEST build
# (package.ps1 -TestIdentity: OmniAPI-Test.exe, its own identifier, uninstall entry, logon value,
# install folder) shares nothing with an installed OmniAPI, so it can be tested while the released
# app runs on this machine. The released identity is refused while any OmniAPI.exe runs.
# Every test config also points the works folder (STORAGE__BASE_PATH), the logon value
# (OMNIAPI_DESKTOP_RUN_VALUE) and the Claude config (OMNIAPI_CLAUDE_CONFIG) into the test folder:
# an installed service otherwise puts works in Documents\OmniAPI.
#
# OMNIAPI_M6_DIR / OMNIAPI_M6_PORT move the test folder and port (e.g. a release check next to an
# earlier run); the folder must stay under %TEMP%, the port in 7800-7949 and never 7788.
$Script:M6 = if ($env:OMNIAPI_M6_DIR) { $env:OMNIAPI_M6_DIR } else { Join-Path $env:TEMP 'omniapi-proto\m6' }
$Script:TestPort = if ($env:OMNIAPI_M6_PORT) { [int]$env:OMNIAPI_M6_PORT } else { 7829 }
$Script:OwnerPort = 7788
if ($TestPort -eq $OwnerPort -or $TestPort -lt 7800 -or $TestPort -gt 7949) { throw "test port $TestPort is not allowed (7800-7949, never 7788)" }
$Script:Identity = if ($env:OMNIAPI_M6_IDENTITY) { $env:OMNIAPI_M6_IDENTITY } else { 'test' }
if ($Identity -notin 'test', 'released') { throw "OMNIAPI_M6_IDENTITY must be test or released" }
$Script:AppName = if ($Identity -eq 'test') { 'OmniAPI-Test' } else { 'OmniAPI' }
$Script:ExeName = "$AppName.exe"
if (-not ([IO.Path]::GetFullPath($M6)).StartsWith([IO.Path]::GetFullPath($env:TEMP), [StringComparison]::OrdinalIgnoreCase)) { throw "test folder $M6 is not under %TEMP%" }
$Script:DesktopDir = Split-Path -Parent $PSScriptRoot
$Script:RepoDir = Split-Path -Parent $DesktopDir
$Script:UninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$Script:RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Script:SystemPath = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
New-Item -ItemType Directory -Force $M6 | Out-Null
$Script:ReportFile = Join-Path $M6 'report.txt'

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $ReportFile -Value $line -Encoding UTF8
}

function Get-Health([int]$Port = $TestPort, [int]$TimeoutSec = 2) {
    try { return Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec $TimeoutSec } catch { return $null }
}
function Get-OwnerPid { $h = Get-Health $OwnerPort 3; if (-not $h) { throw "7788 is NOT answering" }; [int]$h.pid }
function Assert-Owner([int]$Expected, [string]$When) {
    $now = Get-OwnerPid
    if ($now -ne $Expected) { throw "7788 pid changed ($When): $Expected -> $now" }
    Say "7788 pid $now unchanged ($When)"
}

# A fresh data home under %TEMP% (or an existing one to reuse across restarts).
function New-Home([string]$Name) {
    $h = Join-Path $M6 ("home-{0}-{1}" -f $Name, (Get-Date -Format 'HHmmss'))
    New-Item -ItemType Directory -Force $h | Out-Null
    return $h
}

# Test config: program/gui from the exe's own folder ({exe_dir}), port 7829, the given home.
function New-TestConfig([string]$Name, [string]$HomeDir, [hashtable]$ExtraEnv = @{}, [hashtable]$Update = $null) {
    $envMap = [ordered]@{ OMNIAPI_HOME = $HomeDir; OMNIAPI_DEV = '1'; OMNIAPI_OFFLINE = '1'; OMNIAPI_GUI_DIST = '{exe_dir}\gui'
        STORAGE__BASE_PATH = (Join-Path $HomeDir 'works'); OMNIAPI_DESKTOP_RUN_VALUE = $AppName; OMNIAPI_CLAUDE_CONFIG = (Join-Path $HomeDir 'claude.json') }
    foreach ($k in $ExtraEnv.Keys) { $envMap[$k] = $ExtraEnv[$k] }
    $u = if ($Update) { $Update } else { @{ enabled = $false } }
    $cfg = [ordered]@{
        comment      = "1.3-M6 test config ($Name): port $TestPort, data home $HomeDir; never 7788"
        port         = $TestPort
        service      = [ordered]@{
            program     = '{exe_dir}\python\pythonw.exe'
            args        = @('-I', '-m', 'omniapi_mcp.cli', 'serve', '--foreground', '--port', '{port}', '--log-file', '{log_dir}\daemon.log')
            cwd         = '{data_home}'
            env         = $envMap
            create_dirs = @('OMNIAPI_HOME')
        }
        watch        = @{ startup_timeout_secs = 240 }
        update_check = $u
    }
    $path = Join-Path $M6 "$Name.config.json"
    [IO.File]::WriteAllText($path, ($cfg | ConvertTo-Json -Depth 6))
    $env:OMNIAPI_SHELL_CONFIG = $path
    return $path
}

function Assert-TestConfig {
    $p = $env:OMNIAPI_SHELL_CONFIG
    if (-not $p -or -not (Test-Path -LiteralPath $p)) { throw "OMNIAPI_SHELL_CONFIG is not set to a test config; refusing to start a shell (the installed default manages 7788)" }
    $c = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    if ([int]$c.port -eq $OwnerPort -or [int]$c.port -ne $TestPort) { throw "test config port is $($c.port); only $TestPort is allowed; refusing" }
    $e = $c.service.env
    if (-not $e.OMNIAPI_HOME -or -not ([string]$e.OMNIAPI_HOME).StartsWith($env:TEMP, [StringComparison]::OrdinalIgnoreCase)) { throw "test config has no OMNIAPI_HOME under %TEMP%; refusing" }
    if ($e.OMNIAPI_DEV -ne '1' -or $e.OMNIAPI_OFFLINE -ne '1') { throw "test config must set OMNIAPI_DEV=1 and OMNIAPI_OFFLINE=1; refusing" }
    if (-not $e.STORAGE__BASE_PATH -or -not ([string]$e.STORAGE__BASE_PATH).StartsWith($env:TEMP, [StringComparison]::OrdinalIgnoreCase)) { throw "test config has no works folder (STORAGE__BASE_PATH) under %TEMP%; refusing (it would be Documents\OmniAPI)" }
    if ($e.OMNIAPI_DESKTOP_RUN_VALUE -ne $AppName) { throw "test config must name the logon value $AppName; refusing" }
}

function Get-ShellProcs { @(Get-CimInstance Win32_Process -Filter "Name='$ExeName' OR Name='omniapi-desktop.exe'" -Property ProcessId, ExecutablePath, CommandLine) }
function Assert-NoForeignShell {
    if ($Identity -eq 'released' -and @(Get-CimInstance Win32_Process -Filter "Name='OmniAPI.exe'").Count) { throw "the released app runs on this machine; only the test build (OMNIAPI_M6_IDENTITY=test) may be tested here. Refusing." }
    $foreign = @(Get-ShellProcs | Where-Object { -not ([string]$_.ExecutablePath).StartsWith($M6, [StringComparison]::OrdinalIgnoreCase) })
    if ($foreign.Count) { throw "an OmniAPI shell runs outside the test folders ($($foreign[0].ExecutablePath)); a test launch would be handed to it. Refusing." }
}

# Replace the installed shell.config.json by the current test config (after checking the shipped one).
function Protect-Install([string]$Dir) {
    $cfg = Join-Path $Dir 'shell.config.json'
    $shipped = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json
    Copy-Item -LiteralPath $cfg (Join-Path $M6 'last-shipped-shell.config.json') -Force
    Assert-TestConfig
    Copy-Item -LiteralPath $env:OMNIAPI_SHELL_CONFIG $cfg -Force
    Say "installed shell.config.json (shipped: port $($shipped.port), program $($shipped.service.program)) replaced by the test config"
}

# Start the installed shell. -CleanPath: PATH = Windows folders only (no uv, Python, Node, Git).
function Start-TestShell([string]$Exe, [string[]]$ShellArgs = @('--background'), [switch]$CleanPath) {
    Assert-TestConfig
    Assert-NoForeignShell
    if (-not ([string]$Exe).StartsWith($M6, [StringComparison]::OrdinalIgnoreCase)) { throw "not a test install: $Exe" }
    if ([IO.Path]::GetFileName($Exe) -ne $ExeName) { throw "not the $Identity build's exe ($ExeName): $Exe" }
    $saved = $env:PATH
    try {
        if ($CleanPath) { $env:PATH = $SystemPath }
        return Start-Process -FilePath $Exe -ArgumentList $ShellArgs -PassThru
    } finally { $env:PATH = $saved }
}

function Wait-Until([scriptblock]$Cond, [double]$TimeoutSec = 60, [int]$StepMs = 250) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $r = & $Cond
        if ($r) { return [pscustomobject]@{ Ok = $true; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); Value = $r } }
        Start-Sleep -Milliseconds $StepMs
    }
    [pscustomobject]@{ Ok = $false; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); Value = $null }
}
function Wait-Up([double]$TimeoutSec = 300) { Wait-Until { $h = Get-Health; if ($h -and $h.status -eq 'ok') { $h } } $TimeoutSec }

# Quit the test shell (forwarded to the running test shell by the single-instance plugin).
function Stop-TestShell([string]$Exe) {
    if (Get-ShellProcs) {
        Assert-TestConfig
        Assert-NoForeignShell
        Start-Process -FilePath $Exe -ArgumentList '--quit' -Wait | Out-Null
        [void](Wait-Until { -not (Get-ShellProcs) } 40)
    }
    [void](Wait-Until { -not (Get-Health) } 20)
}

# Stop whatever still serves the test port, by the pid its health names (never 7788).
function Stop-TestService {
    $h = Get-Health
    if (-not $h) { return }
    try { Invoke-RestMethod -Method Post "http://127.0.0.1:$TestPort/api/shutdown" -TimeoutSec 5 | Out-Null } catch { }
    $r = Wait-Until { -not (Get-Process -Id ([int]$h.pid) -ErrorAction SilentlyContinue) } 20
    if (-not $r.Ok) { Stop-Process -Id ([int]$h.pid) -Force -ErrorAction SilentlyContinue }
}

function Install-Setup([string]$Setup, [string]$Dir) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process $Setup -ArgumentList '/S', "/D=$Dir" -PassThru -Wait
    "exit {0} in {1:N1}s" -f $p.ExitCode, $sw.Elapsed.TotalSeconds
}
function Uninstall-Dir([string]$Dir) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $u = Start-Process (Join-Path $Dir 'uninstall.exe') -ArgumentList '/S' -PassThru -Wait
    [void](Wait-Until { -not (Test-Path -LiteralPath (Join-Path $Dir $ExeName)) } 60)
    [void](Wait-Until { -not (Test-Path -LiteralPath $Dir) } 15)
    "exit {0} in {1:N1}s" -f $u.ExitCode, $sw.Elapsed.TotalSeconds
}

function Get-DirStats([string]$Dir) {
    $f = @(Get-ChildItem -LiteralPath $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)
    [pscustomobject]@{ Files = $f.Count; MB = [math]::Round((($f | Measure-Object Length -Sum).Sum) / 1MB, 1) }
}

function Get-HookLog { Get-Content (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue }
function Clear-HookLog { Remove-Item (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue }

function Get-RunSnapshot { (Get-ItemProperty $RunKey | Select-Object * -ExcludeProperty PS* | ConvertTo-Json -Compress) }

# Who serves the test port: pid, program, command line, parent.
function Get-ServiceProc {
    $h = Get-Health
    if (-not $h) { return $null }
    Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$h.pid)" -Property ProcessId, ParentProcessId, ExecutablePath, CommandLine
}

function Get-CommitPct {
    $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    [math]::Round(100.0 * $m.CommittedBytes / $m.CommitLimit, 1)
}
