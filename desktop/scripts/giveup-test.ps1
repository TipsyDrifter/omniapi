# The shell must stop retrying a service that cannot start (no endless restart loop):
#  A. the service command exits at once (cmd /c exit 3)   B. the program does not exist
# For each: count the start attempts and the waits between them, check that nothing more is
# started after the shell gives up, that the shell is idle, that a manual --restart-service
# gets a fresh set of attempts, and that --quit leaves nothing behind. Port 7826 only.
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\giveup-test.ps1 [-Exe <path>] [-Open]
param([string]$Exe = '', [switch]$Open)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Exe) { $Exe = Get-DefaultExe }

$ownerPid = Get-OwnerPid
Write-Output "7788 pid at start: $ownerPid"
if (Get-Health) { throw "something already answers on $TestPort" }
if (Get-ShellPids) { throw "a test shell is already running" }

function Run-Case([string]$Name, [string]$Program, [string[]]$Arguments) {
    Write-Output ""
    Write-Output "=== $Name ==="
    $watch = @{ max_failures = 3; backoff_secs = @(0, 2, 4) }
    New-TestConfig "giveup-$($Name.Substring(0,1))" $Program $Arguments $watch | Out-Null
    $from = Get-LogCount
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $args0 = if ($Open) { @('--open') } else { @('--background') }
    $p = Start-Shell $Exe $args0
    $r = Wait-Until { Get-LogTail $from | Select-String -Pattern ' give-up ' -Quiet } 60
    Write-Output ("gave up: {0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    $lines = @(Get-LogTail $from)
    $spawns = @($lines | Select-String -Pattern ' (spawn|spawn-failed) ')
    Write-Output ("start attempts before giving up: {0}" -f $spawns.Count)
    $lines | Select-String -Pattern ' (spawn|spawn-failed|failed|give-up|phase-\w+) ' | ForEach-Object { "  " + $_.Line }
    # nothing more for a while, and the shell is idle
    $shellPid = (Get-ShellPids)[0]
    $cpu0 = (Get-Process -Id $shellPid).TotalProcessorTime.TotalMilliseconds
    Start-Sleep 15
    $cpu1 = (Get-Process -Id $shellPid).TotalProcessorTime.TotalMilliseconds
    $after = @(Get-LogTail $from | Select-String -Pattern ' (spawn|spawn-failed) ').Count
    Write-Output ("15 s later: attempts {0} (unchanged: {1}); shell CPU in those 15 s: {2:N0} ms" -f $after, ($after -eq $spawns.Count), ($cpu1 - $cpu0))
    if ($Open -and $Name.StartsWith('A')) {
        $png = Join-Path $ShotDir 'm5-failed-page.png'
        Write-Output ("failed page screenshot: {0}" -f (Save-WindowShot $png))
    }
    # a manual restart gets a fresh set of attempts
    $from2 = Get-LogCount
    $q = Start-Shell $Exe @('--restart-service'); [void](Wait-Until { $q.HasExited } 15)
    $r2 = Wait-Until { Get-LogTail $from2 | Select-String -Pattern ' give-up ' -Quiet } 60
    $n2 = @(Get-LogTail $from2 | Select-String -Pattern ' (spawn|spawn-failed) ').Count
    Write-Output ("after --restart-service: gave up again {0}, attempts {1}" -f $r2.Ok, $n2)
    $q = Start-Shell $Exe @('--quit'); [void](Wait-Until { $q.HasExited } 15)
    $gone = Wait-Until { -not (Get-ShellPids) } 40
    Start-Sleep 1
    Write-Output ("quit: shell gone {0} after {1}s; leftovers {2}" -f $gone.Ok, $gone.Seconds, @(Get-Leftovers).Count)
}

Run-Case 'A-exits-at-once' "$env:SystemRoot\System32\cmd.exe" @('/c', 'exit 3')
Run-Case 'B-missing-program' (Join-Path $Work 'no-such-python\pythonw.exe') @('-m', 'omniapi_mcp.cli')
Assert-OwnerServiceUntouched $ownerPid
