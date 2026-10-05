# Installer check in a throwaway folder (never the default location):
#  1 silent install (/S /D=)   2 run the installed shell against the test config (port 7826)
#  3 install again over the running copy (= an update): the hook stops the shell; a service that
#    does not run from the install folder is left alone; the files are replaced
#  4 the new shell adopts the service   5 silent uninstall while it runs: hook, files and
#    registry entry gone, user data untouched   6 stop the test service, check for leftovers
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\install-test.ps1 [-Setup <path>]
param([string]$Setup = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Setup) { $Setup = Get-ChildItem (Join-Path $Work 'installers') -Filter 'OmniAPI_*_x64-setup.exe' | Sort-Object LastWriteTime | Select-Object -Last 1 -ExpandProperty FullName }
$Dir = Join-Path $env:TEMP 'omniapi-proto\install-test-m5'
$UninstKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OmniAPI'

$ownerPid = Get-OwnerPid
if (Get-Health) { throw "something already answers on $TestPort" }
if (Get-ShellPids) { throw "a test shell is already running" }
if (Test-Path $Dir) { throw "$Dir exists; remove it first" }
Write-Output ("installer: {0} ({1:N2} MB)" -f $Setup, ((Get-Item $Setup).Length / 1MB))

function Install {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process $Setup -ArgumentList '/S', "/D=$Dir" -PassThru -Wait
    return ("exit {0} in {1:N1}s" -f $p.ExitCode, $sw.Elapsed.TotalSeconds)
}

Write-Output ""
Write-Output "=== 1. install ==="
Write-Output (Install)
Get-ChildItem $Dir | Select-Object Name, Length | Format-Table -AutoSize | Out-String | Write-Output
$reg = Get-ItemProperty $UninstKey -ErrorAction SilentlyContinue
Write-Output ("uninstall entry: {0} {1}" -f $reg.DisplayName, $reg.DisplayVersion)
$exe = Join-Path $Dir 'OmniAPI.exe'
Write-Output ("exe file version: {0}" -f (Get-Item $exe).VersionInfo.FileVersion)

Write-Output ""
Write-Output "=== 2. run the installed shell (test config) ==="
New-TestConfig 'install' | Out-Null
$from = Get-LogCount
$p = Start-Shell $exe @('--background')
$r = Wait-Until { $h = Get-Health; if ($h -and $h.status -eq 'ok') { $h } } 180
$svcPid = [int]$r.Value.pid
Write-Output ("service up: {0} after {1}s, pid {2}; shell pid {3}" -f $r.Ok, $r.Seconds, $svcPid, (Get-ShellPids -join ','))

Write-Output ""
Write-Output "=== 3. install again over the running copy ==="
$stamp = (Get-Item $exe).LastWriteTimeUtc
Remove-Item (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue
Write-Output (Install)
Write-Output ("shell still running: {0}; service pid {1} still answering: {2}" -f [bool](Get-ShellPids), $svcPid, ([int](Get-Health).pid -eq $svcPid))
Write-Output ("left running under the install folder before the copy: see the hook log; exe timestamp changed: {0} (NSIS keeps the build time, so reinstalling the same build shows False)" -f ((Get-Item $exe).LastWriteTimeUtc -ne $stamp))
Write-Output "hook log:"
Get-Content (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue | ForEach-Object { "  $_" }

Write-Output ""
Write-Output "=== 4. start the new shell: adopts the running service ==="
$from2 = Get-LogCount
$p = Start-Shell $exe @('--background')
[void](Wait-Until { Get-LogTail $from2 | Select-String -Pattern ' adopt ' -Quiet } 20)
Get-LogTail $from2 | Select-String -Pattern ' (start|adopt) ' | ForEach-Object { "  " + $_.Line }

Write-Output ""
Write-Output "=== 5. uninstall while it runs ==="
$homeDirs = @(Get-ChildItem $Work -Directory -Filter 'home-*').Count
Remove-Item (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue
$sw = [Diagnostics.Stopwatch]::StartNew()
$u = Start-Process (Join-Path $Dir 'uninstall.exe') -ArgumentList '/S' -PassThru -Wait
[void](Wait-Until { -not (Test-Path $exe) } 30)
Write-Output ("uninstaller exit {0}; {1:N1}s; folder left: {2}; uninstall entry left: {3}" -f $u.ExitCode, $sw.Elapsed.TotalSeconds, (Test-Path $Dir), (Test-Path $UninstKey))
if (Test-Path $Dir) { Get-ChildItem $Dir -Recurse | Select-Object FullName | Out-String | Write-Output }
Write-Output ("shell running: {0}; test service answering: {1}; data homes kept: {2} -> {3}" -f [bool](Get-ShellPids), [bool](Get-Health), $homeDirs, @(Get-ChildItem $Work -Directory -Filter 'home-*').Count)
Write-Output "hook log:"
Get-Content (Join-Path $env:TEMP 'omniapi-installer-stop.log') -ErrorAction SilentlyContinue | ForEach-Object { "  $_" }

Write-Output ""
Write-Output "=== 6. stop the test service (by its pid: it is not ours to adopt any more) ==="
$h = Get-Health
if ($h) {
    try { Invoke-RestMethod -Method Post "http://127.0.0.1:$TestPort/api/shutdown" -TimeoutSec 5 | Out-Null } catch { }
    [void](Wait-Until { -not (Get-Health) } 20)
}
Start-Sleep 2
$left = @(Get-Leftovers)
Write-Output ("leftovers: {0}" -f $left.Count)
$left | Format-Table -AutoSize | Out-String -Width 250 | Write-Output
if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force; Write-Output "removed what the uninstaller left in $Dir" }
Assert-OwnerServiceUntouched $ownerPid
