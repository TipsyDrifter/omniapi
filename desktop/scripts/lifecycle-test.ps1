# End-to-end check of the shell, driven from outside (no tray clicks), on port 7826 only:
#  1 cold start in the background (start the service)    2 second launch --open (single instance)
#  3 WM_CLOSE (= X): window gone, shell + service stay     4 kill the service python -> brought back
#  5 kill the whole service tree -> brought back           6 shell crash -> service survives -> adopted
#  7 kill the adopted service -> brought back              8 --restart-service (graceful)
#  9 --quit: POST /api/shutdown, clean exit in daemon.log, zero leftovers; 7788 untouched throughout
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\lifecycle-test.ps1 [-Exe <path>]
param([string]$Exe = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Exe) { $Exe = Get-DefaultExe }

function Step($t) { Write-Output ""; Write-Output "=== $t ===" }
function NewPid([int]$Old, [int]$Max = 180) { Wait-Until { $h = Get-Health $TestPort 1; if ($h -and $h.status -eq 'ok' -and [int]$h.pid -ne $Old) { $h } } $Max }

$ownerPid = Get-OwnerPid
Write-Output "7788 pid at start: $ownerPid"
if (Get-Health) { throw "something already answers on $TestPort; stop it first" }
if (Get-ShellPids) { throw "a test shell is already running" }
$cfg = New-TestConfig 'lifecycle'
Write-Output "config: $cfg"
$logFrom = Get-LogCount

Step "1. cold start with --background (no window)"
$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Shell $Exe @('--background')
$r = Wait-Until { $h = Get-Health; if ($h -and $h.status -eq 'ok') { $h } } 180
Write-Output ("service answered after {0:N1}s (launch to health ok): pid {1}" -f $sw.Elapsed.TotalSeconds, $r.Value.pid)
Write-Output ("visible shell windows: {0}" -f (Get-ShellWindows).Count)

Step "2. second launch with --open (forwarded, exits by itself)"
$p2 = Start-Shell $Exe @('--open')
$exit2 = Wait-Until { $p2.HasExited } 15
$w = Wait-Until { (Get-ShellWindows).Count -gt 0 } 20
Write-Output ("second instance exited: {0} after {1}s; shells now: {2}; window visible: {3} after {4}s" -f $exit2.Ok, $exit2.Seconds, (Get-ShellPids).Count, $w.Ok, $w.Seconds)
Start-Sleep 3
$p3 = Start-Shell $Exe @(); [void](Wait-Until { $p3.HasExited } 15)
Write-Output ("third launch (no args) exited: {0}; shells: {1}; windows: {2}" -f $p3.HasExited, (Get-ShellPids).Count, (Get-ShellWindows).Count)

Step "3. close the window (WM_CLOSE = clicking X)"
Write-Output ("closed {0} window(s)" -f (Close-ShellWindow))
Start-Sleep 4
$wv = @(Get-Leftovers | Where-Object { $_.Name -ieq 'msedgewebview2.exe' })
Write-Output ("shells: {0}; visible windows: {1}; webview2 processes: {2}; service answering: {3}" -f (Get-ShellPids).Count, (Get-ShellWindows).Count, $wv.Count, [bool](Get-Health))

Step "4. kill only the service python (the health pid)"
$old = [int](Get-Health).pid
taskkill /PID $old /F | Out-Null
$t = NewPid $old
Write-Output ("back: {0} after {1}s, new pid {2}" -f $t.Ok, $t.Seconds, $t.Value.pid)

Step "5. kill the whole service tree (launcher + python)"
$old = [int](Get-Health).pid
$launcher = [int](Get-CimInstance Win32_Process -Filter "ProcessId=$old").ParentProcessId
Get-Family @($launcher) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$t = NewPid $old
Write-Output ("back: {0} after {1}s, new pid {2}" -f $t.Ok, $t.Seconds, $t.Value.pid)

Step "6. shell crash -> the service survives -> a new shell adopts it"
$svcPid = [int](Get-Health).pid
Get-ShellPids | ForEach-Object { Stop-Process -Id $_ -Force }
Start-Sleep 3
Write-Output ("after the crash: service answering {0}, pid {1} (was {2})" -f [bool](Get-Health), (Get-Health).pid, $svcPid)
$p = Start-Shell $Exe @('--background')
Start-Sleep 5
Write-Output ("new shell running: {0}; service pid unchanged: {1}" -f [bool](Get-ShellPids), ([int](Get-Health).pid -eq $svcPid))

Step "7. kill the adopted service (no job, no launcher handle)"
$old = [int](Get-Health).pid
taskkill /PID $old /F | Out-Null
$t = NewPid $old
Write-Output ("back: {0} after {1}s, new pid {2}" -f $t.Ok, $t.Seconds, $t.Value.pid)

Step "8. --restart-service (graceful stop, then a new start)"
$old = [int](Get-Health).pid
$p4 = Start-Shell $Exe @('--restart-service'); [void](Wait-Until { $p4.HasExited } 15)
$t = NewPid $old
Write-Output ("restarted: {0} after {1}s, new pid {2}" -f $t.Ok, $t.Seconds, $t.Value.pid)

Step "9. --quit, then look for leftovers"
$svcPid = [int](Get-Health).pid
$p5 = Start-Shell $Exe @('--quit'); [void](Wait-Until { $p5.HasExited } 15)
$gone = Wait-Until { -not (Get-ShellPids) } 40
Write-Output ("shell exited: {0} after {1}s" -f $gone.Ok, $gone.Seconds)
Start-Sleep 2
$left = @(Get-Leftovers)
Write-Output ("leftover processes: {0}" -f $left.Count)
$left | Format-Table -AutoSize | Out-String -Width 250 | Write-Output
Write-Output ("$TestPort answering after quit: {0}" -f [bool](Get-Health))
$daemonLog = Join-Path $LogDir 'daemon.log'
Write-Output "daemon.log, the last service's shutdown (pid $svcPid):"
Get-Content $daemonLog -Encoding UTF8 | Select-String -Pattern "shutdown requested over /api/shutdown \(pid=$svcPid\)|daemon shutting down \(pid=$svcPid\)" | ForEach-Object { "  " + $_.Line }
Assert-OwnerServiceUntouched $ownerPid

Step "shell.log (this run)"
Get-LogTail $logFrom
