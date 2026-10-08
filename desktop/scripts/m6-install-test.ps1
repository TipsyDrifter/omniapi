# 1.3-M6 installer verification (in a throwaway folder; the owner's 7788 is checked, never touched).
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\m6-install-test.ps1 -Setup <setup.exe> [-Setup2 <newer setup.exe>] [-Steps a,b,...]
# Steps (default: all, in this order):
#   install   silent install to %TEMP%\omniapi-proto\m6\install, size, exe list
#   first     first start of the fresh files with PATH = Windows folders only; layout, GUI, workdir
#   second    quit, start again (second-start time); the shell's process tree
#   cli       omni.cmd: status / works / autostart status; omni serve == the shell's kind of service
#   autostart --autostart-on: Run value, omni autostart status/install see it; off; Run key restored
#   settings  PATCH /api/settings, quit, start: still there
#   e2e       chat / generate / artifacts / settings end-to-end scripts against the installed service
#   update    -Setup2 over the running copy (graceful stop by the hook, files replaced, data untouched),
#             then -Setup again over it (going back), then start
#   defender  wait, every exe still there
#   uninstall uninstall while it runs: folder gone, data home kept
#   cjk       install to "...\Omni <CJK> App", start, uninstall
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [string]$Setup2 = '',
    [string]$Steps = 'install,first,second,cli,autostart,settings,e2e,update,defender,uninstall,cjk',
    [int]$DefenderWaitSec = 90
)
$ErrorActionPreference = 'Stop'
$StepList = @($Steps -split '[,\s]+' | Where-Object { $_ })
. (Join-Path $PSScriptRoot 'm6-common.ps1')
$Dir = Join-Path $M6 'install'
$Exe = Join-Path $Dir $ExeName
$StateFile = Join-Path $M6 'install-test.state.json'
function Has([string]$s) { $StepList -contains $s }
function Save-State($o) { [IO.File]::WriteAllText($StateFile, ($o | ConvertTo-Json -Depth 4)) }
$state = if (Test-Path $StateFile) { Get-Content $StateFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
function Set-State([string]$k, $v) { $state | Add-Member -NotePropertyName $k -NotePropertyValue $v -Force; Save-State $state }

Say "==== m6-install-test steps=$($StepList -join ',') setup=$Setup"
$ownerPid = Get-OwnerPid
Say "7788 pid at start: $ownerPid"
$runBefore = Get-RunSnapshot
Assert-NoForeignShell

function Show-Status([string]$label) {
    $s = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/status" -TimeoutSec 10
    $p = Get-ServiceProc
    Say ("[{0}] layout={1} gui_dist={2} workdir={3} data_home={4} storage={5}" -f $label, $s.layout.layout, $s.layout.gui_dist, $s.layout.workdir, $s.data_home, $s.storage)
    Say ("[{0}] service pid {1} exe {2}; parent pid {3}" -f $label, $p.ProcessId, $p.ExecutablePath, $p.ParentProcessId)
    Say ("[{0}] service command line: {1}" -f $label, $p.CommandLine)
    $page = (Invoke-WebRequest "http://127.0.0.1:$TestPort/" -UseBasicParsing -TimeoutSec 10).Content
    Say ("[{0}] home page is the dashboard (has /assets/index-): {1}" -f $label, ($page -match '/assets/index-'))
    return $s
}

# ------------------------------------------------------------------ install
if (Has 'install') {
    if (Test-Path $Dir) { throw "$Dir exists; uninstall / remove it first" }
    Say ("installer {0:N1} MB" -f ((Get-Item $Setup).Length / 1MB))
    Say "install: $(Install-Setup $Setup $Dir)"
    $st = Get-DirStats $Dir
    Say "installed folder: $($st.MB) MB, $($st.Files) files"
    Say ("top level: " + ((Get-ChildItem $Dir | ForEach-Object Name) -join ', '))
    $reg = Get-ItemProperty $UninstKey -ErrorAction SilentlyContinue
    Say "uninstall entry: $($reg.DisplayName) $($reg.DisplayVersion)"
    $omniCmd = [IO.File]::ReadAllBytes((Join-Path $Dir 'omni.cmd'))
    Say ("omni.cmd: {0} bytes, ASCII only: {1}, CRLF only: {2}" -f $omniCmd.Length, (-not ($omniCmd | Where-Object { $_ -gt 127 })), (([Text.Encoding]::ASCII.GetString($omniCmd) -replace "`r`n", '') -notmatch "`n"))
    $exes = @(Get-ChildItem $Dir -Recurse -Include *.exe, *.dll, *.pyd -File | ForEach-Object FullName)
    Set-State 'exes' $exes
    Say "executables (exe/dll/pyd) in the install: $($exes.Count)"
    Set-State 'installedAt' (Get-Date).ToString('o')
    Assert-Owner $ownerPid 'after install'
}

# ------------------------------------------------------------------ first start (fresh files, clean PATH)
if (Has 'first') {
    $home1 = New-Home 'main'
    Set-State 'home' $home1
    New-TestConfig 'main' $home1 | Out-Null
    Protect-Install $Dir
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $null = Start-TestShell $Exe @('--background') -CleanPath
    $r = Wait-Up 300
    Say ("FIRST start (fresh install, PATH=Windows folders only): up={0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    if (-not $r.Ok) { throw "service did not come up; see $home1\logs" }
    $s = Show-Status 'first'
    $ok = ($s.layout.layout -eq 'installed') -and ($s.layout.gui_dist -like "$Dir\gui*") -and ((Resolve-Path $s.layout.workdir).Path -eq (Resolve-Path $home1).Path)
    Say "installed layout, GUI from the install folder, workdir = data home: $ok"
    $p = Get-ServiceProc
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
    $envPath = $null
    Say ("service program in the install folder: {0}; parent is the installed shell: {1}" -f ($p.ExecutablePath -eq (Join-Path $Dir 'python\pythonw.exe')), ($parent.ExecutablePath -eq $Exe))
}

# ------------------------------------------------------------------ second start
if (Has 'second') {
    New-TestConfig 'main' $state.home | Out-Null
    Stop-TestShell $Exe
    Say "after quit: shell running $([bool](Get-ShellProcs)), test service answering $([bool](Get-Health))"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $null = Start-TestShell $Exe @('--background') -CleanPath
    $r = Wait-Up 300
    Say ("SECOND start: up={0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    Stop-TestShell $Exe
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $null = Start-TestShell $Exe @('--background') -CleanPath
    $r = Wait-Up 300
    Say ("THIRD start: up={0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    $tree = Get-CimInstance Win32_Process | Where-Object { ([string]$_.ExecutablePath).StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) }
    Say ("processes from the install folder: " + (($tree | ForEach-Object { "$($_.Name)#$($_.ProcessId)" }) -join ', '))
}

# ------------------------------------------------------------------ omni.cmd
if (Has 'cli') {
    New-TestConfig 'main' $state.home | Out-Null
    $omni = Join-Path $Dir 'omni.cmd'
    $env:OMNIAPI_HOME = $state.home
    $env:STORAGE__BASE_PATH = Join-Path $state.home 'works'
    $saved = $env:PATH; $env:PATH = $SystemPath
    try {
        Say "omni.cmd version: $((& $omni version) -join ' ')"
        Say ("omni.cmd status --port {0}: exit {1}" -f $TestPort, $LASTEXITCODE)
        & $omni status --port $TestPort | ForEach-Object { Say "    $_" }
        Say "omni.cmd works --help (first lines):"
        & $omni works --help | Select-Object -First 4 | ForEach-Object { Say "    $_" }
        Say "omni.cmd autostart status:"
        & $omni autostart status | ForEach-Object { Say "    $_" }

        # omni serve (installed mode) next to the shell's: stop the shell, start the service from omni.cmd
        $shellSvc = Get-ServiceProc
        Stop-TestShell $Exe
        $env:OMNIAPI_DEV = '1'; $env:OMNIAPI_OFFLINE = '1'
        Remove-Item Env:OMNIAPI_GUI_DIST -ErrorAction SilentlyContinue
        Push-Location $env:TEMP
        try { & $omni serve --port $TestPort | ForEach-Object { Say "    serve: $_" } } finally { Pop-Location }
        $cliSvc = Get-ServiceProc
        $s = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/status"
        Say ("omni serve: exe same as the shell's: {0}; -I: {1}; workdir {2}; gui_dist {3}; layout {4}" -f ($cliSvc.ExecutablePath -eq $shellSvc.ExecutablePath), ($cliSvc.CommandLine -match ' -I '), $s.layout.workdir, $s.layout.gui_dist, $s.layout.layout)
        Say "  shell's: $($shellSvc.CommandLine)"
        Say "  omni's:  $($cliSvc.CommandLine)"
        & $omni stop --port $TestPort | ForEach-Object { Say "    stop: $_" }
        Say "test service answering after omni stop --port $TestPort : $([bool](Get-Health))"
    } finally {
        $env:PATH = $saved
        Remove-Item Env:OMNIAPI_HOME, Env:OMNIAPI_DEV, Env:OMNIAPI_OFFLINE, Env:STORAGE__BASE_PATH -ErrorAction SilentlyContinue
    }
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
    # omni stop while the desktop app runs: it says the app will start the service again, and it does
    $before = (Get-Health).pid
    $env:OMNIAPI_HOME = $state.home
    try { & $omni stop --port $TestPort | ForEach-Object { Say "    stop (app running): $_" } } finally { Remove-Item Env:OMNIAPI_HOME -ErrorAction SilentlyContinue }
    $r = Wait-Until { $h = Get-Health; if ($h -and $h.status -eq 'ok' -and [int]$h.pid -ne [int]$before) { $h } } 120
    Say "the desktop app started the service again: $($r.Ok) after $($r.Seconds)s (pid $before -> $($r.Value.pid))"
}

# ------------------------------------------------------------------ logon entry (restored afterwards)
if (Has 'autostart') {
    New-TestConfig 'main' $state.home | Out-Null
    if (-not (Get-ShellProcs)) { $null = Start-TestShell $Exe @('--background') -CleanPath; [void](Wait-Up 300) }
    Assert-TestConfig; Assert-NoForeignShell
    $before = Get-RunSnapshot
    $ownerRun = (Get-ItemProperty $RunKey -Name 'OmniAPI' -ErrorAction SilentlyContinue).OmniAPI
    $had = (Get-ItemProperty $RunKey -Name $AppName -ErrorAction SilentlyContinue).$AppName
    Say "Run\$AppName before: $(if ($had) { $had } else { '(none)' })"
    try {
        Start-Process -FilePath $Exe -ArgumentList '--autostart-on' -Wait | Out-Null
        [void](Wait-Until { (Get-ItemProperty $RunKey -Name $AppName -ErrorAction SilentlyContinue).$AppName } 15)
        Say "Run\$AppName after --autostart-on: $((Get-ItemProperty $RunKey -Name $AppName -ErrorAction SilentlyContinue).$AppName)"
        if ($Identity -eq 'test') { Say ("Run\OmniAPI (the released app's value) unchanged: {0}" -f ((Get-ItemProperty $RunKey -Name 'OmniAPI' -ErrorAction SilentlyContinue).OmniAPI -eq $ownerRun)) }
        $env:OMNIAPI_HOME = $state.home
        & (Join-Path $Dir 'omni.cmd') autostart status | ForEach-Object { Say "    omni autostart status: $_" }
        if ($Identity -ne 'test') { & (Join-Path $Dir 'omni.cmd') autostart install | ForEach-Object { Say "    omni autostart install: $_" } }   # the test build refuses it anyway; not even tried
        Say "    (exit ${LASTEXITCODE}: refused while the desktop app starts at logon)"
    } finally {
        Remove-Item Env:OMNIAPI_HOME -ErrorAction SilentlyContinue
        Start-Process -FilePath $Exe -ArgumentList '--autostart-off' -Wait | Out-Null
        [void](Wait-Until { -not (Get-ItemProperty $RunKey -Name $AppName -ErrorAction SilentlyContinue) } 15)
        if (-not $had) { Remove-ItemProperty $RunKey -Name $AppName -ErrorAction SilentlyContinue }
    }
    Say "Run key restored to the state before: $((Get-RunSnapshot) -eq $before)"
}

# ------------------------------------------------------------------ settings survive a restart
if (Has 'settings') {
    New-TestConfig 'main' $state.home | Out-Null
    if (-not (Get-Health)) { $null = Start-TestShell $Exe @('--background') -CleanPath; [void](Wait-Up 300) }
    $body = '{"tiers": {"cheap": "gpt-5-mini"}, "defaults": {"chat": "deepseek-chat"}}'
    $r = Invoke-RestMethod -Method Patch "http://127.0.0.1:$TestPort/api/settings" -Body $body -ContentType 'application/json'
    Say "PATCH /api/settings: changed=$($r.changed -join ',') warnings=$($r.warnings -join ' | ')"
    $settingsFile = Join-Path $state.home 'settings.json'
    Say "settings.json in the data home: $(Test-Path $settingsFile)"
    Stop-TestShell $Exe
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
    $g = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/settings"
    Say ("after restarting the shell: tiers.cheap={0} ({1}); defaults.chat={2} ({3})" -f $g.tiers.cheap.model, $g.tiers.cheap.source, $g.defaults.chat.model, $g.defaults.chat.source)
}

# ------------------------------------------------------------------ e2e scripts against the installed service
if (Has 'e2e') {
    # a fresh home for the e2e run (settings_e2e expects no key and ends with a shutdown)
    $homeE = New-Home 'e2e'
    New-TestConfig 'e2e' $homeE @{ OMNIAPI_FAKE_DELAY = '2' } | Out-Null
    Stop-TestShell $Exe
    Protect-Install $Dir
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
    $py = Join-Path $Dir 'python\python.exe'
    $env:OMNIAPI_HOME = $homeE
    Push-Location (Join-Path $RepoDir 'mcp')
    try {
        foreach ($t in 'chat_e2e', 'generate_e2e', 'artifacts_e2e', 'openrouter_images_e2e', 'settings_e2e') {
            if (-not (Get-Health)) { [void](Wait-Up 120) }
            $sw = [Diagnostics.Stopwatch]::StartNew()
            # the installed Python runs the repo's test script; -I so the installed omniapi_mcp is the one imported
            $o = Join-Path $M6 "e2e-$t.out.txt"; $e = Join-Path $M6 "e2e-$t.err.txt"
            $proc = Start-Process -FilePath $py -ArgumentList '-I', "tests\e2e\$t.py", $TestPort -RedirectStandardOutput $o -RedirectStandardError $e -NoNewWindow -Wait -PassThru
            $code = $proc.ExitCode
            $out = @(Get-Content $o -Encoding UTF8) + @(Get-Content $e -Encoding UTF8)
            $fails = @($out | Where-Object { $_ -match 'FAIL|Traceback|Error' })
            Say ("e2e {0}: exit {1} in {2:N0}s; {3} lines; last: {4}" -f $t, $code, $sw.Elapsed.TotalSeconds, $out.Count, ($out | Select-Object -Last 1))
            $fails | Select-Object -First 8 | ForEach-Object { Say "    $_" }
        }
    } finally { Pop-Location; Remove-Item Env:OMNIAPI_HOME -ErrorAction SilentlyContinue }
    # settings_e2e shut the service down; the shell brings it back
    $r = Wait-Up 300
    Say "after settings_e2e's shutdown the shell brought the service back: $($r.Ok) ($($r.Seconds)s)"
    Stop-TestShell $Exe
    New-TestConfig 'main' $state.home | Out-Null
    Protect-Install $Dir
    $null = Start-TestShell $Exe @('--background') -CleanPath
    [void](Wait-Up 300)
}

# ------------------------------------------------------------------ update over the running copy, then back
if (Has 'update') {
    if (-not $Setup2) { throw "-Setup2 (a newer installer) is needed for the update step" }
    New-TestConfig 'main' $state.home | Out-Null
    if (-not (Get-Health)) { $null = Start-TestShell $Exe @('--background') -CleanPath; [void](Wait-Up 300) }
    $svc = Get-ServiceProc
    Say "before update: service pid $($svc.ProcessId) from $($svc.ExecutablePath)"
    # the database is open while the service runs: compare what the service reports about it, and
    # settings.json byte for byte
    $settingsHash = (Get-FileHash (Join-Path $state.home 'settings.json')).Hash
    $storeBefore = (Invoke-RestMethod "http://127.0.0.1:$TestPort/api/status").store | ConvertTo-Json -Compress
    $dbBefore = Get-Item (Join-Path $state.home 'omniapi.db')
    Say "before: store $storeBefore; omniapi.db $($dbBefore.Length) bytes"
    $marker = Join-Path $Dir 'python\Lib\site-packages\omniapi_mcp\M6-OLD-MARKER.txt'
    Set-Content $marker 'left by the old version' -Encoding ASCII
    Clear-HookLog
    $ver1 = (Get-ItemProperty $UninstKey).DisplayVersion
    Say "update ($ver1 -> setup2) over the running copy: $(Install-Setup $Setup2 $Dir)"
    Get-HookLog | ForEach-Object { Say "    hook: $_" }
    Say ("after update: registry version {0}; shell running {1}; test service answering {2}; old service pid {3} alive {4}" -f (Get-ItemProperty $UninstKey).DisplayVersion, [bool](Get-ShellProcs), [bool](Get-Health), $svc.ProcessId, [bool](Get-Process -Id $svc.ProcessId -ErrorAction SilentlyContinue))
    Say "a file only the old version had (python\...\M6-OLD-MARKER.txt) is gone: $(-not (Test-Path $marker))"
    Say ("settings.json unchanged: {0}; omniapi.db still there: {1}" -f ((Get-FileHash (Join-Path $state.home 'settings.json')).Hash -eq $settingsHash), (Test-Path (Join-Path $state.home 'omniapi.db')))
    Assert-Owner $ownerPid 'after update'
    Protect-Install $Dir
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $null = Start-TestShell $Exe @('--background') -CleanPath
    $r = Wait-Up 300
    Say ("start after the update (fresh files again): up={0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    $g = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/settings"
    Say "settings after the update: tiers.cheap=$($g.tiers.cheap.model) defaults.chat=$($g.defaults.chat.model)"
    $storeAfter = (Invoke-RestMethod "http://127.0.0.1:$TestPort/api/status").store | ConvertTo-Json -Compress
    Say "store after the update: $storeAfter (same as before: $($storeAfter -eq $storeBefore))"

    Clear-HookLog
    Say "back to the older installer over the running copy: $(Install-Setup $Setup $Dir)"
    Get-HookLog | ForEach-Object { Say "    hook: $_" }
    Say ("after going back: registry version {0}; test service answering {1}" -f (Get-ItemProperty $UninstKey).DisplayVersion, [bool](Get-Health))
    Assert-Owner $ownerPid 'after going back'
    Protect-Install $Dir
    $null = Start-TestShell $Exe @('--background') -CleanPath
    $r = Wait-Up 300
    $g = Invoke-RestMethod "http://127.0.0.1:$TestPort/api/settings"
    Say "start after going back: up=$($r.Ok) ($($r.Seconds)s); settings tiers.cheap=$($g.tiers.cheap.model)"
}

# ------------------------------------------------------------------ Defender
if (Has 'defender') {
    $exes = @($state.exes)
    Say "waiting $DefenderWaitSec s, then checking $($exes.Count) exe/dll/pyd files"
    Start-Sleep -Seconds $DefenderWaitSec
    $gone = @($exes | Where-Object { -not (Test-Path -LiteralPath $_) })
    Say "missing after the wait: $($gone.Count) $(($gone | Select-Object -First 5) -join ', ')"
    try { $t = @(Get-MpThreatDetection -ErrorAction Stop); Say "Get-MpThreatDetection: $($t.Count) entries" } catch { Say "Get-MpThreatDetection: not available ($($_.Exception.Message))" }
    Say "service still answering: $([bool](Get-Health))"
}

# ------------------------------------------------------------------ uninstall while running
if (Has 'uninstall') {
    New-TestConfig 'main' $state.home | Out-Null
    if (-not (Get-Health)) { $null = Start-TestShell $Exe @('--background') -CleanPath; [void](Wait-Up 300) }
    $docs = [Environment]::GetFolderPath('MyDocuments')
    Clear-HookLog
    Say "uninstall while running: $(Uninstall-Dir $Dir)"
    Get-HookLog | ForEach-Object { Say "    hook: $_" }
    Say ("install folder left: {0}; uninstall entry left: {1}; shell running: {2}; test service answering: {3}" -f (Test-Path $Dir), (Test-Path $UninstKey), [bool](Get-ShellProcs), [bool](Get-Health))
    if (Test-Path $Dir) { Get-ChildItem $Dir -Recurse -Force | Select-Object -First 20 | ForEach-Object { Say "    left: $($_.FullName)" } }
    Say ("data home kept: {0} ({1} files); Documents\OmniAPI exists: {2}" -f (Test-Path $state.home), @(Get-ChildItem $state.home -Recurse -File).Count, (Test-Path (Join-Path $docs 'OmniAPI')))
    Assert-Owner $ownerPid 'after uninstall'
}

# ------------------------------------------------------------------ path with a space and CJK characters
if (Has 'cjk') {
    $cjk = [string]::Concat([char]0x6E2C, [char]0x8A66)
    $DirC = Join-Path $M6 "Omni $cjk App"
    if (Test-Path -LiteralPath $DirC) { throw "$DirC exists" }
    Say "install to '$DirC': $(Install-Setup $Setup $DirC)"
    $homeC = New-Home 'cjk'
    New-TestConfig 'cjk' $homeC | Out-Null
    Protect-Install $DirC
    $exeC = Join-Path $DirC $ExeName
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $null = Start-TestShell $exeC @('--background') -CleanPath
    $r = Wait-Up 300
    Say ("CJK path start: up={0} after {1:N1}s" -f $r.Ok, $sw.Elapsed.TotalSeconds)
    if ($r.Ok) { $null = Show-Status 'cjk' }
    Clear-HookLog
    Say "uninstall (running): $(Uninstall-Dir $DirC)"
    Get-HookLog | ForEach-Object { Say "    hook: $_" }
    Say "CJK install folder left: $(Test-Path -LiteralPath $DirC); service answering: $([bool](Get-Health))"
    Assert-Owner $ownerPid 'after CJK uninstall'
}

$runAfter = Get-RunSnapshot
Say "HKCU Run unchanged by this run: $($runBefore -eq $runAfter)"
Assert-Owner $ownerPid 'end'
