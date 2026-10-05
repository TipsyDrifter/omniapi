# Shared helpers for the desktop shell's build and verification scripts (dot-source this file).
# ASCII only (Windows PowerShell 5.1 reads a BOM-less file as the ANSI code page).
#
# Safety: a debug build's built-in default (release builds have none) manages the owner's real service on 7788 (and its Quit
# stops it). Every test here runs the shell with OMNIAPI_SHELL_CONFIG pointing at a test config
# on port 7826 with a fresh OMNIAPI_HOME, and refuses to start if that is not the case.

$Script:DesktopDir = Split-Path -Parent $PSScriptRoot
$Script:RepoDir = Split-Path -Parent $DesktopDir
$Script:Work = Join-Path $env:TEMP 'omniapi-proto\m5'
$Script:TargetDir = Join-Path $env:TEMP 'omniapi-proto\cargo-target-desktop'
$Script:TestPort = 7826
$Script:OwnerPort = 7788
$Script:ShellNames = @('omniapi-desktop.exe', 'OmniAPI.exe')
$Script:LogDir = Join-Path $Work 'logs'
$Script:ShellLog = Join-Path $LogDir 'shell.log'
# the main checkout (a worktree has no .venv): its venv runs the service (read-only use; nothing is
# installed there). OMNIAPI_MAIN_CHECKOUT overrides; otherwise git's common dir names it.
$Script:MainCheckout = $env:OMNIAPI_MAIN_CHECKOUT
if (-not $MainCheckout) {
    $common = git -C $RepoDir rev-parse --path-format=absolute --git-common-dir
    $MainCheckout = if ($LASTEXITCODE -eq 0 -and $common) { Split-Path -Parent $common } else { $RepoDir }
}
$Script:MainVenvPythonw = Join-Path $MainCheckout 'mcp\.venv\Scripts\pythonw.exe'
$Script:MainGuiDist = Join-Path $MainCheckout 'gui\dist'

# `tauri build` renames the cargo output (omniapi-desktop.exe) to the product's name
function Get-DefaultExe { Join-Path $TargetDir 'release\OmniAPI.exe' }

function Get-Health([int]$Port = $TestPort, [int]$TimeoutSec = 2) {
    try { return Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec $TimeoutSec } catch { return $null }
}

function Get-OwnerPid {
    $h = Get-Health $OwnerPort 3
    if (-not $h) { throw "7788 is NOT answering" }
    return [int]$h.pid
}

function Assert-OwnerServiceUntouched([int]$ExpectedPid) {
    $now = Get-OwnerPid
    if ($now -ne $ExpectedPid) { throw "7788 pid changed: $ExpectedPid -> $now" }
    Write-Output "7788 ok, pid $now unchanged"
}

# Write a test config and point OMNIAPI_SHELL_CONFIG at it (this process and its children).
#   -Program / -Arguments replace the service command (e.g. a program that fails at once).
function New-TestConfig([string]$Name = 'test', [string]$Program = $MainVenvPythonw, [string[]]$Arguments = $null, [hashtable]$Watch = $null) {
    New-Item -ItemType Directory -Force $Work, $LogDir | Out-Null
    if (-not $Arguments) {
        $Arguments = @('-m', 'omniapi_mcp.cli', 'serve', '--foreground', '--port', '{port}', '--log-file', '{log_dir}\daemon.log')
    }
    $w = @{ poll_secs = 2; miss_threshold = 3; startup_timeout_secs = 120; stable_secs = 30; max_failures = 3; backoff_secs = @(0, 2, 4); shutdown_grace_secs = 15 }
    if ($Watch) { foreach ($k in $Watch.Keys) { $w[$k] = $Watch[$k] } }
    $cfg = [ordered]@{
        comment = "verification config ($Name): port $TestPort, fresh data home per run; never 7788"
        port    = $TestPort
        log_dir = $LogDir
        service = [ordered]@{
            program     = $Program
            args        = $Arguments
            cwd         = (Join-Path $RepoDir 'mcp')
            env         = [ordered]@{
                OMNIAPI_HOME     = (Join-Path $Work 'home-{run_id}')
                OMNIAPI_DEV      = '1'
                OMNIAPI_OFFLINE  = '1'
                OMNIAPI_GUI_DIST = $MainGuiDist
            }
            create_dirs = @('OMNIAPI_HOME')
        }
        watch   = $w
    }
    $path = Join-Path $Work "$Name.config.json"
    [IO.File]::WriteAllText($path, ($cfg | ConvertTo-Json -Depth 6))
    $env:OMNIAPI_SHELL_CONFIG = $path
    return $path
}

function Assert-TestConfig {
    $p = $env:OMNIAPI_SHELL_CONFIG
    if (-not $p -or -not (Test-Path $p)) { throw "OMNIAPI_SHELL_CONFIG is not set to a test config; refusing to start a shell (the default would manage 7788)" }
    $c = Get-Content $p -Raw | ConvertFrom-Json
    if ([int]$c.port -ne $TestPort) { throw "test config port is $($c.port), expected $TestPort; refusing" }
}

function Get-ShellProcs { @(Get-CimInstance Win32_Process -Property ProcessId, Name, ExecutablePath, CommandLine | Where-Object { $ShellNames -contains $_.Name -and ($_.ExecutablePath -like "$TargetDir*" -or $_.ExecutablePath -like "$env:TEMP\omniapi-proto\install-test-m5*") }) }
function Get-ShellPids { @(Get-ShellProcs | ForEach-Object { [int]$_.ProcessId }) }

function Start-Shell([string]$Exe, [string[]]$ShellArgs) {
    Assert-TestConfig
    if ($ShellArgs -and $ShellArgs.Count) { return Start-Process -FilePath $Exe -ArgumentList $ShellArgs -PassThru }
    return Start-Process -FilePath $Exe -PassThru
}

# All processes descended from $RootPids (by ParentProcessId), roots included.
function Get-Family([int[]]$RootPids) {
    $all = Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name, CommandLine, ExecutablePath, CreationDate
    $byParent = @{}
    foreach ($p in $all) {
        $k = [int]$p.ParentProcessId
        if (-not $byParent.ContainsKey($k)) { $byParent[$k] = New-Object System.Collections.ArrayList }
        [void]$byParent[$k].Add($p)
    }
    $out = New-Object System.Collections.ArrayList
    $seen = @{}
    $queue = New-Object System.Collections.Queue
    foreach ($r in ($all | Where-Object { $RootPids -contains [int]$_.ProcessId })) { $queue.Enqueue($r) }
    while ($queue.Count -gt 0) {
        $p = $queue.Dequeue()
        if ($seen.ContainsKey([int]$p.ProcessId)) { continue }
        $seen[[int]$p.ProcessId] = $true
        [void]$out.Add($p)
        $kids = $byParent[[int]$p.ProcessId]
        if ($kids) { foreach ($k in $kids) { if ($k.CreationDate -ge $p.CreationDate) { $queue.Enqueue($k) } } }
    }
    return $out
}

# Anything that could be ours: the shell, a python serving the test port, WebView2 with our data dir.
function Get-Leftovers {
    Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name, CommandLine, ExecutablePath | Where-Object {
        ($ShellNames -contains $_.Name -and ($_.ExecutablePath -like "$TargetDir*" -or $_.ExecutablePath -like "$env:TEMP\omniapi-proto\install-test-m5*")) -or
        ($_.Name -match '^pythonw?\.exe$' -and [string]$_.CommandLine -match "--port\s+$TestPort") -or
        ($_.Name -ieq 'msedgewebview2.exe' -and [string]$_.CommandLine -match 'com\.kosa\.omniapi\\')
    } | Select-Object ProcessId, ParentProcessId, Name, CommandLine
}

function Wait-Until([scriptblock]$Cond, [double]$TimeoutSec = 60, [int]$StepMs = 250) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $r = & $Cond
        if ($r) { return [pscustomobject]@{ Ok = $true; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); Value = $r } }
        Start-Sleep -Milliseconds $StepMs
    }
    return [pscustomobject]@{ Ok = $false; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); Value = $null }
}

function Get-LogTail([int]$From) { if (Test-Path $ShellLog) { Get-Content $ShellLog -Encoding UTF8 | Select-Object -Skip $From } }
function Get-LogCount { if (Test-Path $ShellLog) { (Get-Content $ShellLog -Encoding UTF8).Count } else { 0 } }

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class OmniWin {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
    public static List<IntPtr> Find(uint pid, string title) {
        var r = new List<IntPtr>();
        EnumWindows((h, l) => {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != pid) return true;
            var sb = new StringBuilder(256); GetWindowText(h, sb, 256);
            if (sb.ToString() == title && IsWindowVisible(h)) r.Add(h);
            return true;
        }, IntPtr.Zero);
        return r;
    }
}
"@

function Get-ShellWindows {
    $out = @()
    foreach ($p in Get-ShellPids) { $out += [OmniWin]::Find([uint32]$p, 'OmniAPI') }
    return $out
}

# Same as clicking the window's X.
function Close-ShellWindow {
    $w = Get-ShellWindows
    foreach ($h in $w) { [void][OmniWin]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    return $w.Count
}

$Script:ShotDir = Join-Path $RepoDir 'prototypes\desktop-shell\verification'

Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public static class OmniShot {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    // PW_RENDERFULLCONTENT (2): asks DWM for the composed content, which WebView2 needs
    public static Bitmap Capture(IntPtr h) {
        RECT r; GetWindowRect(h, out r);
        var bmp = new Bitmap(Math.Max(1, r.R - r.L), Math.Max(1, r.B - r.T));
        using (var g = Graphics.FromImage(bmp)) { var dc = g.GetHdc(); PrintWindow(h, dc, 2); g.ReleaseHdc(dc); }
        return bmp;
    }
}
"@

# Save the shell window (the first visible one) as a PNG; returns the path or a reason.
function Save-WindowShot([string]$Path) {
    $w = @(Get-ShellWindows)
    if (-not $w.Count) { return "no window to capture" }
    New-Item -ItemType Directory -Force (Split-Path $Path) | Out-Null
    $bmp = [OmniShot]::Capture($w[0])
    try { $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png) } finally { $bmp.Dispose() }
    return $Path
}

function Get-CommitPct {
    $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    return [math]::Round(100.0 * $m.CommittedBytes / $m.CommitLimit, 1)
}
