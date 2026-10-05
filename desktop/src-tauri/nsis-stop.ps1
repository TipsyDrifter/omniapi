# Run by the OmniAPI installer before it copies or removes files (nsis-hooks.nsh).
# Stops everything running from the install folder, the service gracefully first:
#   1. the shell (OmniAPI.exe from this folder) -- so it does not start the service again;
#   2. the service, if its program is in this folder: POST /api/shutdown, wait for it to exit;
#   3. anything still running from this folder (by path; a python.exe elsewhere is never touched).
# A log goes to %TEMP%\omniapi-installer-stop.log. Always exits 0: a failure here must not
# block an uninstall; the installer reports files it could not replace by itself.
param([Parameter(Mandatory = $true)][string]$InstDir)

$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'omniapi-installer-stop.log'
function Say([string]$m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8 }

$dir = [IO.Path]::GetFullPath($InstDir).TrimEnd('\') + '\'
Say "---- stop processes under $dir"

function Under([string]$path) { $path -and $path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) }
# never the installer / uninstaller that runs this script (NSIS normally runs the uninstaller
# from a copy in %TEMP%, but not when started with _?=)
$me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
$spare = @([int]$PID, [int]$me.ParentProcessId)
function Ours { @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, ExecutablePath, Name | Where-Object { (Under $_.ExecutablePath) -and ($spare -notcontains [int]$_.ProcessId) -and ($_.Name -notlike 'uninstall*') }) }

# 1. the shell
foreach ($p in (Ours | Where-Object { $_.Name -ieq 'OmniAPI.exe' })) {
    Say "stop shell pid $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 2. the service, gracefully -- only a service whose own program is in the install folder (1.3-M6).
#    It is found from the process side: every python under the install folder, the ports it
#    listens on, and /api/health on that port must name that same pid. So whatever port it runs on
#    (the shell config may say another one) it is asked, and a service anywhere else (another
#    install, a checkout's venv) is never asked, never even queried.
foreach ($p in (Ours | Where-Object { $_.Name -match '^pythonw?\.exe$' })) {
    $svcPid = [int]$p.ProcessId
    $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $svcPid -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -in '127.0.0.1', '0.0.0.0', '::1', '::' } | ForEach-Object { [int]$_.LocalPort } | Sort-Object -Unique)
    foreach ($port in $ports) {
        $h = $null
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3 } catch { }
        if (-not $h -or [int]$h.pid -ne $svcPid) { Say "pid $svcPid listens on :$port but that is not its OmniAPI health; skipped"; continue }
        Say "service pid $svcPid on :$port runs from the install folder ($($p.ExecutablePath)); POST /api/shutdown"
        try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/shutdown" -TimeoutSec 5 | Out-Null } catch { Say "shutdown request failed: $_" }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt 20 -and (Get-Process -Id $svcPid -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 300 }
        Say ("service gone after {0:N1}s: {1}" -f $sw.Elapsed.TotalSeconds, -not (Get-Process -Id $svcPid -ErrorAction SilentlyContinue))
        break
    }
}

# 3. whatever is left
foreach ($p in (Ours)) {
    Say "stop leftover pid $($p.ProcessId) $($p.ExecutablePath)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500
$left = Ours
Say ("left running under the install folder: {0}" -f $left.Count)
exit 0
