# Logon autostart on and off through the shell (same code path as the tray's check mark),
# what `omni autostart status` says about it, and the warning about an old `omni autostart`
# launcher. The old launcher is FAKED: the shell is started with APPDATA pointing at a temp
# folder holding a dummy "OmniAPI Daemon.vbs", so the real Startup folder is never written.
# The registry values the plugin writes (HKCU ...\Run\OmniAPI and StartupApproved\Run\OmniAPI)
# are recorded first and put back exactly as they were at the end.
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\autostart-test.ps1 [-Exe <path>]
param([string]$Exe = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Exe) { $Exe = Get-DefaultExe }

$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$ApprovedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
function Read-Value([string]$Key, [string]$Name) { try { (Get-ItemProperty -Path $Key -Name $Name -ErrorAction Stop).$Name } catch { $null } }
function Omni-Status {
    Push-Location (Join-Path $RepoDir 'mcp')
    try { & ($MainVenvPythonw -replace 'pythonw\.exe$', 'python.exe') -m omniapi_mcp.cli autostart status } finally { Pop-Location }
}

$ownerPid = Get-OwnerPid
if (Get-Health) { throw "something already answers on $TestPort" }
if (Get-ShellPids) { throw "a test shell is already running" }

# 0. record what is there now
$before = @{ run = (Read-Value $RunKey 'OmniAPI'); approved = (Read-Value $ApprovedKey 'OmniAPI') }
Write-Output ("before: Run\OmniAPI = {0}; StartupApproved\Run\OmniAPI = {1}" -f $(if ($null -eq $before.run) { '(none)' } else { $before.run }), $(if ($null -eq $before.approved) { '(none)' } else { ($before.approved | ForEach-Object { '{0:x2}' -f $_ }) -join ' ' }))
Write-Output "omni autostart status (before):"
Omni-Status | ForEach-Object { "  $_" }

try {
    New-TestConfig 'autostart' | Out-Null
    $fakeAppData = Join-Path $Work 'fake-appdata'
    $fakeStartup = Join-Path $fakeAppData 'Microsoft\Windows\Start Menu\Programs\Startup'
    New-Item -ItemType Directory -Force $fakeStartup | Out-Null
    Set-Content -Path (Join-Path $fakeStartup 'OmniAPI Daemon.vbs') -Value "' dummy for autostart-test.ps1; never run" -Encoding ASCII
    $from = Get-LogCount
    $realAppData = $env:APPDATA
    $env:APPDATA = $fakeAppData
    try { $p = Start-Shell $Exe @('--background') } finally { $env:APPDATA = $realAppData }
    [void](Wait-Until { Get-Health } 180)

    Write-Output ""
    Write-Output "=== on ==="
    $q = Start-Shell $Exe @('--autostart-on'); [void](Wait-Until { $q.HasExited } 15)
    [void](Wait-Until { Get-LogTail $from | Select-String -Pattern ' autostart via=' -Quiet } 15)
    Start-Sleep 1
    Write-Output ("Run\OmniAPI = {0}" -f (Read-Value $RunKey 'OmniAPI'))
    Get-LogTail $from | Select-String -Pattern ' autostart' | ForEach-Object { "  " + $_.Line }
    Write-Output "omni autostart status:"
    Omni-Status | ForEach-Object { "  $_" }

    Write-Output ""
    Write-Output "=== off ==="
    $from2 = Get-LogCount
    $q = Start-Shell $Exe @('--autostart-off'); [void](Wait-Until { $q.HasExited } 15)
    [void](Wait-Until { Get-LogTail $from2 | Select-String -Pattern ' autostart via=' -Quiet } 15)
    Start-Sleep 1
    Write-Output ("Run\OmniAPI = {0}" -f $(if ($null -eq (Read-Value $RunKey 'OmniAPI')) { '(none)' } else { Read-Value $RunKey 'OmniAPI' }))
    Get-LogTail $from2 | Select-String -Pattern ' autostart' | ForEach-Object { "  " + $_.Line }
    Write-Output "omni autostart status:"
    Omni-Status | ForEach-Object { "  $_" }

    $q = Start-Shell $Exe @('--quit'); [void](Wait-Until { $q.HasExited } 15)
    [void](Wait-Until { -not (Get-ShellPids) } 40)
} finally {
    # put the registry back exactly as it was
    if ($null -eq $before.run) { Remove-ItemProperty -Path $RunKey -Name 'OmniAPI' -ErrorAction SilentlyContinue }
    else { Set-ItemProperty -Path $RunKey -Name 'OmniAPI' -Value $before.run }
    if ($null -eq $before.approved) { Remove-ItemProperty -Path $ApprovedKey -Name 'OmniAPI' -ErrorAction SilentlyContinue }
    else { Set-ItemProperty -Path $ApprovedKey -Name 'OmniAPI' -Value ([byte[]]$before.approved) -Type Binary }
    $after = @{ run = (Read-Value $RunKey 'OmniAPI'); approved = (Read-Value $ApprovedKey 'OmniAPI') }
    $same = ("$($after.run)" -eq "$($before.run)") -and ((@($after.approved) -join ',') -eq (@($before.approved) -join ','))
    Write-Output ""
    Write-Output ("registry restored to the recorded state: {0}" -f $same)
}
Start-Sleep 1
Write-Output ("leftovers: {0}" -f @(Get-Leftovers).Count)
Assert-OwnerServiceUntouched $ownerPid
