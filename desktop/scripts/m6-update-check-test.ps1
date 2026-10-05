# 1.3-M6: the daily new-version check of the installed shell (port 7829, the m6 guard).
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\m6-update-check-test.ps1 -Setup <setup.exe>
# A  local server says v9.9.9: tray line (shell.log update-available), /api/status desktop_update;
#    restart the shell: no second request within the day (the server's request log)
# B  nothing listening: one failure line, nothing offered, retry later
# C  the real GitHub API (public, anonymous): latest = the published version
# D  update_check.enabled = false: no request, status says off
param([Parameter(Mandatory = $true)][string]$Setup)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'm6-common.ps1')
$Dir = Join-Path $M6 'install-upd'
$Exe = Join-Path $Dir 'OmniAPI.exe'
$Www = Join-Path $M6 'upd-check-www'
$SrvPort = 7839
$ownerPid = Get-OwnerPid
Say "==== update-check test (7788 pid $ownerPid)"
if (Test-Path $Dir) { throw "$Dir exists" }
Say "install: $(Install-Setup $Setup $Dir)"

New-Item -ItemType Directory -Force $Www | Out-Null
'{"tag_name":"v9.9.9","html_url":"https://github.com/TipsyDrifter/omniapi/releases/tag/v9.9.9","draft":false,"prerelease":false}' | Set-Content (Join-Path $Www 'latest.json') -Encoding ASCII
$srvLog = Join-Path $M6 'upd-check-server.log'
Remove-Item $srvLog -ErrorAction SilentlyContinue
$srv = Start-Process -FilePath (Join-Path $Dir 'python\python.exe') -ArgumentList '-I', '-m', 'http.server', $SrvPort, '--bind', '127.0.0.1', '--directory', $Www -WindowStyle Hidden -RedirectStandardError $srvLog -PassThru
[void](Wait-Until { Get-NetTCPConnection -State Listen -LocalPort $SrvPort -ErrorAction SilentlyContinue } 15)

function Run-Case([string]$name, [hashtable]$update, [int]$waitSec = 40) {
    $h = New-Home "upd-$name"
    New-TestConfig "upd-$name" $h -Update $update | Out-Null
    Protect-Install $Dir | Out-Host
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
    $log = Join-Path $h 'logs\shell.log'
    [void](Wait-Until { Select-String -Path $log -Pattern ' update-check(-failed|-off)? ' -Quiet -ErrorAction SilentlyContinue } $waitSec)
    Start-Sleep -Milliseconds 500
    Get-Content $log -Encoding UTF8 | Where-Object { $_ -match ' update-' } | ForEach-Object { Say "  [$name] shell.log: $_" | Out-Host }
    $s = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/status"
    Say "  [$name] /api/status desktop_update: $($s.desktop_update | ConvertTo-Json -Compress)" | Out-Host
    return $h
}

try {
    $hA = Run-Case 'A' @{ enabled = $true; url = "http://127.0.0.1:$SrvPort/latest.json"; delay_secs = 1 }
    Stop-TestShell $Exe
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
    Start-Sleep -Seconds 5
    Get-Content (Join-Path $hA 'logs\shell.log') -Encoding UTF8 | Where-Object { $_ -match ' update-' } | Select-Object -Last 2 | ForEach-Object { Say "  [A restart] shell.log: $_" }
    $reqs = @(Get-Content $srvLog -ErrorAction SilentlyContinue | Where-Object { $_ -match 'GET /latest.json' })
    Say "  [A] requests the server got over two shell starts: $($reqs.Count)"
    $reqs | ForEach-Object { Say "      $_" }
    Stop-TestShell $Exe

    $null = Run-Case 'B' @{ enabled = $true; url = 'http://127.0.0.1:1/latest.json'; delay_secs = 1 }
    Stop-TestShell $Exe
    $null = Run-Case 'C' @{ enabled = $true; delay_secs = 1 }
    Stop-TestShell $Exe
    $hD = Run-Case 'D' @{ enabled = $false } 10
    Say "  [D] state file: $(Get-Content (Join-Path $hD 'desktop-update.json') -Raw -ErrorAction SilentlyContinue)"
    Stop-TestShell $Exe
} finally {
    Stop-TestService
    if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
    if (Test-Path (Join-Path $Dir 'uninstall.exe')) { Say "uninstall: $(Uninstall-Dir $Dir)" }
    Say "install folder left: $(Test-Path $Dir)"
    Assert-Owner $ownerPid 'end of update-check test'
}
