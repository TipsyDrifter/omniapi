# Window screenshots for the record: the local "starting" page during a cold start, then the
# dashboard once the shell has sent the window there. Port 7826 only; quits cleanly at the end.
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\screens-test.ps1 [-Exe <path>]
param([string]$Exe = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Exe) { $Exe = Get-DefaultExe }

$ownerPid = Get-OwnerPid
if (Get-Health) { throw "something already answers on $TestPort" }
if (Get-ShellPids) { throw "a test shell is already running" }
New-TestConfig 'screens' | Out-Null
$from = Get-LogCount

$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Shell $Exe @('--open')
$w = Wait-Until { (Get-ShellWindows).Count -gt 0 } 20
Write-Output ("window visible after {0:N1}s" -f $sw.Elapsed.TotalSeconds)
Start-Sleep -Milliseconds 3500   # let the page lay out (web fonts may still be loading)
$up = [bool](Get-Health)
Write-Output ("starting page: {0} (service already up: {1}, {2:N1}s after launch)" -f (Save-WindowShot (Join-Path $ShotDir 'm5-starting-page.png')), $up, $sw.Elapsed.TotalSeconds)

$r = Wait-Until { Get-LogTail $from | Select-String -Pattern ' window-navigate ' -Quiet } 180
Write-Output ("window sent to the service: {0} after {1:N1}s from launch" -f $r.Ok, $sw.Elapsed.TotalSeconds)
Start-Sleep 5
Write-Output ("dashboard: {0}" -f (Save-WindowShot (Join-Path $ShotDir 'm5-dashboard.png')))

$q = Start-Shell $Exe @('--quit'); [void](Wait-Until { $q.HasExited } 15)
$gone = Wait-Until { -not (Get-ShellPids) } 40
Start-Sleep 1
Write-Output ("quit: shell gone {0}; leftovers {1}" -f $gone.Ok, @(Get-Leftovers).Count)
Assert-OwnerServiceUntouched $ownerPid
Get-LogTail $from | Select-String -Pattern ' (start|config|spawn|phase-\w+|window-\w+|stopped) '
