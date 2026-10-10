# Unit test for the installer's post-install hook (src-tauri\nsis-hooks.nsh, OMNI_RESTORE_AUTOSTART):
# compiles a tiny installer that does nothing but run the hook, and runs it against pref files in
# temp folders. Seconds, no Tauri build. ASCII only (Windows PowerShell 5.1).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\nsis-hook-test.ps1
#
# Registry: it writes HKCU Run values named OmniAPI-Test (the test identity's own; the previous
# value, if any, is put back at the end) and OmniAPI-HookCheck (a made-up name that selects the
# released build's data folder branch without ever naming the released app's value OmniAPI).
# It never writes Run\OmniAPI. 7788 is not involved.
$ErrorActionPreference = 'Stop'
$Hooks = Join-Path (Split-Path -Parent $PSScriptRoot) 'src-tauri\nsis-hooks.nsh'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$ApprovedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$Work = Join-Path $env:TEMP ('omniapi-hook-test-' + $PID)
New-Item -ItemType Directory -Force $Work | Out-Null

$makensis = @("$env:LOCALAPPDATA\tauri\NSIS\makensis.exe", "${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$env:ProgramFiles\NSIS\makensis.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $makensis) { throw 'makensis.exe not found (Tauri downloads it to %LOCALAPPDATA%\tauri\NSIS on its first bundle)' }

$fail = 0
function Check([bool]$ok, [string]$what) { if ($ok) { "  ok   $what" } else { "  FAIL $what"; $script:fail++ } }
function Get-Val([string]$key, [string]$name) { try { (Get-ItemProperty -Path $key -Name $name -ErrorAction Stop).$name } catch { $null } }
function Hex($b) { if ($null -eq $b) { '(none)' } else { ($b | ForEach-Object { '{0:x2}' -f $_ }) -join '' } }

# one stub installer per name: the hook text is inserted exactly as Tauri's installer inserts it
function New-Stub([string]$Name) {
    $inst = Join-Path $Work "inst-$Name"
    $nsi = Join-Path $Work "stub-$Name.nsi"
    $exe = Join-Path $Work "stub-$Name.exe"
    $text = @"
Unicode true
RequestExecutionLevel user
SilentInstall silent
!define MAINBINARYNAME "$Name"
OutFile "$exe"
InstallDir "$inst"
!include "$Hooks"
Section
  !insertmacro NSIS_HOOK_POSTINSTALL
SectionEnd
"@
    [IO.File]::WriteAllText($nsi, $text)
    $out = & $makensis /V2 $nsi 2>&1
    if ($LASTEXITCODE) { $out | ForEach-Object { "    makensis: $_" }; throw "makensis failed for $Name" }
    [pscustomobject]@{ Exe = $exe; Inst = $inst; Name = $Name; Command = "$inst\$Name.exe --background" }
}

# run a stub with the given environment (OMNIAPI_HOME / USERPROFILE), then return the Run value it left
function Invoke-Stub($stub, [hashtable]$Env) {
    Remove-ItemProperty $RunKey -Name $stub.Name -ErrorAction SilentlyContinue
    $saved = @{}
    foreach ($k in 'OMNIAPI_HOME', 'USERPROFILE') { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process') }
    try {
        foreach ($k in 'OMNIAPI_HOME', 'USERPROFILE') { [Environment]::SetEnvironmentVariable($k, $(if ($Env.ContainsKey($k)) { $Env[$k] } else { $saved[$k] }), 'Process') }
        if (-not $Env.ContainsKey('OMNIAPI_HOME')) { [Environment]::SetEnvironmentVariable('OMNIAPI_HOME', $null, 'Process') }
        $p = Start-Process $stub.Exe -ArgumentList '/S' -Wait -PassThru
        if ($p.ExitCode) { throw "stub exit $($p.ExitCode)" }
    } finally { foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') } }
    Get-Val $RunKey $stub.Name
}
function New-Pref([string]$Dir, $Bytes) { New-Item -ItemType Directory -Force $Dir | Out-Null; [IO.File]::WriteAllBytes((Join-Path $Dir 'autostart.pref'), [byte[]]$Bytes) }
function Bytes([string]$s) { [Text.Encoding]::ASCII.GetBytes($s) }

# put back what was there (the test identity's value and its Task Manager flag)
$savedRun = Get-Val $RunKey 'OmniAPI-Test'
$savedApproved = Get-Val $ApprovedKey 'OmniAPI-Test'
$approvedKeyExisted = Test-Path $ApprovedKey
$ownerRun = Get-Val $RunKey 'OmniAPI'
try {
    $test = New-Stub 'OmniAPI-Test'
    $hookcheck = New-Stub 'OmniAPI-HookCheck'
    "makensis: $makensis"

    "OMNIAPI_HOME set (what the install tests use):"
    $h = Join-Path $Work 'home1'
    foreach ($case in @(
        @{ n = 'autostart=on + LF';      b = (Bytes "autostart=on`n");       on = $true },
        @{ n = 'autostart=on + CRLF';    b = (Bytes "autostart=on`r`n");     on = $true },
        @{ n = 'autostart=on, no EOL';   b = (Bytes 'autostart=on');         on = $true },
        @{ n = 'AUTOSTART=ON (case)';    b = (Bytes "AUTOSTART=ON`n");       on = $true },
        @{ n = 'autostart=off';          b = (Bytes "autostart=off`n");      on = $false },
        @{ n = 'empty file';             b = @();                            on = $false },
        @{ n = 'garbage';                b = @(0xff, 0xfe, 0x00, 0x81);      on = $false },
        @{ n = 'JSON';                   b = (Bytes '{"autostart":true}');   on = $false },
        @{ n = 'second line only';       b = (Bytes "x`nautostart=on`n");    on = $false })) {
        Remove-Item -Recurse -Force $h -ErrorAction SilentlyContinue
        New-Pref $h $case.b
        $v = Invoke-Stub $test @{ OMNIAPI_HOME = $h }
        if ($case.on) { Check ($v -eq $test.Command) "$($case.n): Run\OmniAPI-Test = '$($test.Command)' (got '$v')" } else { Check ($null -eq $v) "$($case.n): nothing written" }
    }
    Remove-Item -Recurse -Force $h -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $h | Out-Null
    Check ($null -eq (Invoke-Stub $test @{ OMNIAPI_HOME = $h })) 'no pref file: nothing written'
    Remove-Item -Recurse -Force $h
    Check ($null -eq (Invoke-Stub $test @{ OMNIAPI_HOME = $h })) 'no data folder at all: nothing written'
    New-Pref $h (Bytes "autostart=on`n"); Remove-Item (Join-Path $h 'autostart.pref'); New-Item -ItemType Directory (Join-Path $h 'autostart.pref') | Out-Null
    Check ($null -eq (Invoke-Stub $test @{ OMNIAPI_HOME = $h })) 'autostart.pref is a folder: nothing written, no error'

    "default data folder (OMNIAPI_HOME not set; USERPROFILE = a temp folder):"
    $u = Join-Path $Work 'profile'
    Remove-Item -Recurse -Force $u -ErrorAction SilentlyContinue
    New-Pref (Join-Path $u '.omniapi') (Bytes "autostart=on`n")
    Check ($null -eq (Invoke-Stub $test @{ USERPROFILE = $u })) "test identity ignores the released app's .omniapi\autostart.pref"
    New-Pref (Join-Path $u '.omniapi-test') (Bytes "autostart=on`n")
    Check ((Invoke-Stub $test @{ USERPROFILE = $u }) -eq $test.Command) 'test identity reads .omniapi-test\autostart.pref'
    Remove-Item -Recurse -Force $u
    New-Pref (Join-Path $u '.omniapi-test') (Bytes "autostart=on`n")
    Check ($null -eq (Invoke-Stub $hookcheck @{ USERPROFILE = $u })) "released-branch name ignores .omniapi-test\autostart.pref"
    New-Pref (Join-Path $u '.omniapi') (Bytes "autostart=on`n")
    Check ((Invoke-Stub $hookcheck @{ USERPROFILE = $u }) -eq $hookcheck.Command) 'released-branch name reads .omniapi\autostart.pref and writes its own value name'

    "Task Manager flag (StartupApproved\Run):"
    if (Test-Path $ApprovedKey) {
        Remove-ItemProperty $ApprovedKey -Name 'OmniAPI-Test' -ErrorAction SilentlyContinue
        $hasOthers = @((Get-Item $ApprovedKey).GetValueNames()).Count -gt 0
        Remove-Item -Recurse -Force $h -ErrorAction SilentlyContinue; New-Pref $h (Bytes "autostart=on`n")
        [void](Invoke-Stub $test @{ OMNIAPI_HOME = $h })
        $a = Get-Val $ApprovedKey 'OmniAPI-Test'
        if ($hasOthers) { Check ((Hex $a) -eq '020000000000000000000000') "marked enabled for Task Manager (02 00 .. 00), as the plugin does: $(Hex $a)" } else { "  (key has no values; skipped)" }
    } else { "  (key does not exist on this machine; the hook leaves it alone: $(-not (Test-Path $ApprovedKey)))" }

    Check ((Get-Val $RunKey 'OmniAPI') -eq $ownerRun) 'Run\OmniAPI (the released app''s value) was never touched'
} finally {
    Remove-ItemProperty $RunKey -Name 'OmniAPI-HookCheck' -ErrorAction SilentlyContinue
    if ($null -ne $savedRun) { Set-ItemProperty $RunKey -Name 'OmniAPI-Test' -Value $savedRun } else { Remove-ItemProperty $RunKey -Name 'OmniAPI-Test' -ErrorAction SilentlyContinue }
    if (Test-Path $ApprovedKey) {
        Remove-ItemProperty $ApprovedKey -Name 'OmniAPI-HookCheck' -ErrorAction SilentlyContinue
        if ($null -ne $savedApproved) { Set-ItemProperty $ApprovedKey -Name 'OmniAPI-Test' -Value ([byte[]]$savedApproved) -Type Binary } else { Remove-ItemProperty $ApprovedKey -Name 'OmniAPI-Test' -ErrorAction SilentlyContinue }
    }
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
if ($fail) { "nsis-hook-test: $fail check(s) FAILED"; exit 1 }
'nsis-hook-test: all checks passed'
exit 0
