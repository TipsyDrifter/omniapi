# Static check that a built shell is the identity it was built as (package.ps1 runs it after
# tauri build; it can be run alone). Nothing is executed: it reads the exe's bytes, Tauri's
# generated NSIS script, and the shell config / omni.cmd that go into the installer.
#
#   powershell -File desktop\scripts\check-identity.ps1 -Identity released|test -Exe <exe> [-Nsi <installer.nsi>] [-ShellConfig <json>] [-OmniCmd <cmd>]
#
# released: identifier com.kosa.omniapi, product / exe OmniAPI, and NOTHING of the test build in
#           the exe (no "com.kosa.omniapi.test", no "OmniAPI-Test"); shell config and omni.cmd are
#           the repo's config\shell.installed.json and config\omni.cmd byte for byte.
# test:     identifier com.kosa.omniapi.test everywhere the identifier appears, product / exe /
#           uninstall key OmniAPI-Test; the shell config manages port 7939 (not 7788), its own
#           data home (not ~\.omniapi), works under it, logon value OmniAPI-Test, a stand-in
#           Claude config, offline + dev, no new-version check; omni.cmd defaults to the same.
# Exit 0 = all checks passed; 1 = a check failed (each line says which).
param(
    [Parameter(Mandatory = $true)][ValidateSet('released', 'test')][string]$Identity,
    [Parameter(Mandatory = $true)][string]$Exe,
    [string]$Nsi = '',
    [string]$ShellConfig = '',
    [string]$OmniCmd = ''
)
$ErrorActionPreference = 'Stop'
$DesktopDir = Split-Path -Parent $PSScriptRoot
$fail = 0
function Check([bool]$ok, [string]$what) {
    if ($ok) { "ok    $what" } else { "FAIL  $what"; $script:fail++ }
}
function Count([byte[]]$hay, [string]$needle, [Text.Encoding]$enc) {
    $n = $enc.GetBytes($needle); $c = 0
    $first = $n[0]; $last = $hay.Length - $n.Length
    for ($i = [Array]::IndexOf($hay, $first); $i -ge 0 -and $i -le $last; $i = [Array]::IndexOf($hay, $first, $i + 1)) {
        $m = $true
        for ($j = 1; $j -lt $n.Length; $j++) { if ($hay[$i + $j] -ne $n[$j]) { $m = $false; break } }
        if ($m) { $c++ }
    }
    $c
}

$test = $Identity -eq 'test'
$want = if ($test) {
    @{ Id = 'com.kosa.omniapi.test'; Product = 'OmniAPI-Test'; Binary = 'OmniAPI-Test' }
} else {
    @{ Id = 'com.kosa.omniapi'; Product = 'OmniAPI'; Binary = 'OmniAPI' }
}
"check-identity: $Identity  exe=$Exe"

# ---------------------------------------------------------------- the exe
Check (Test-Path -LiteralPath $Exe) "exe exists"
Check ([IO.Path]::GetFileName($Exe) -eq "$($want.Binary).exe") "exe file name is $($want.Binary).exe"
$bytes = [IO.File]::ReadAllBytes($Exe)
$u8 = [Text.Encoding]::UTF8; $u16 = [Text.Encoding]::Unicode
$idAll = (Count $bytes 'com.kosa.omniapi' $u8) + (Count $bytes 'com.kosa.omniapi' $u16)
$idTest = (Count $bytes 'com.kosa.omniapi.test' $u8) + (Count $bytes 'com.kosa.omniapi.test' $u16)
$testName = (Count $bytes 'OmniAPI-Test' $u8) + (Count $bytes 'OmniAPI-Test' $u16)
"      identifier strings in the exe: com.kosa.omniapi* $idAll, of them com.kosa.omniapi.test $idTest; 'OmniAPI-Test' $testName"
if ($test) {
    Check ($idTest -gt 0) "the exe carries the test identifier (single instance mutex, app folders)"
    Check ($idAll -eq $idTest) "every identifier in the exe is the test one (no bare com.kosa.omniapi)"
    Check ($testName -gt 0) "the exe carries the test name (logon value OmniAPI-Test)"
} else {
    Check ($idAll -gt 0) "the exe carries the released identifier"
    Check ($idTest -eq 0) "no test identifier in the released exe"
    Check ($testName -eq 0) "no 'OmniAPI-Test' (test logon value / name) in the released exe"
}

# ---------------------------------------------------------------- Tauri's NSIS script
if ($Nsi) {
    $text = Get-Content -LiteralPath $Nsi -Raw -Encoding UTF8
    function Define([string]$name) { if ($text -match ('!define\s+' + $name + '\s+"([^"]*)"')) { $Matches[1] } else { $null } }
    $d = @{ PRODUCTNAME = (Define 'PRODUCTNAME'); MAINBINARYNAME = (Define 'MAINBINARYNAME'); BUNDLEID = (Define 'BUNDLEID'); UNINSTKEY = (Define 'UNINSTKEY') }
    "      nsis: PRODUCTNAME=$($d.PRODUCTNAME) MAINBINARYNAME=$($d.MAINBINARYNAME) BUNDLEID=$($d.BUNDLEID) UNINSTKEY=$($d.UNINSTKEY)"
    Check ($d.PRODUCTNAME -eq $want.Product) "installer product name (install folder %LOCALAPPDATA%\$($want.Product), Start menu, uninstall entry) is $($want.Product)"
    Check ($d.MAINBINARYNAME -eq $want.Binary) "installer's running-app check and shortcuts use $($want.Binary).exe"
    Check ($d.BUNDLEID -eq $want.Id) "installer bundle id is $($want.Id)"
    Check ($d.UNINSTKEY -match '\\Uninstall\\\$\{PRODUCTNAME\}$') "uninstall key is ...\Uninstall\`${PRODUCTNAME}"
    Check ($text -match 'StrCpy \$INSTDIR "\$LOCALAPPDATA\\\$\{PRODUCTNAME\}"') "default per-user install folder is `$LOCALAPPDATA\`${PRODUCTNAME}"
    $hooks = Join-Path $DesktopDir 'src-tauri\nsis-hooks.nsh'
    $h = Get-Content -LiteralPath $hooks -Raw
    Check (($h -notmatch '\$INSTDIR\\OmniAPI\.exe') -and ($h -match '\$INSTDIR\\\$\{MAINBINARYNAME\}\.exe')) "our installer hooks name the exe only as `${MAINBINARYNAME}.exe"
    if ($test) {
        Check ($text -notmatch '\\OmniAPI\.exe') "no literal OmniAPI.exe in the test installer script"
        Check ($text -notmatch '"OmniAPI"') "no literal ""OmniAPI"" name in the test installer script"
    }
}

# ---------------------------------------------------------------- what goes next to the exe
if ($ShellConfig) {
    if ($test) {
        $c = Get-Content -LiteralPath $ShellConfig -Raw | ConvertFrom-Json
        $e = $c.service.env
        Check ([int]$c.port -ne 7788 -and [int]$c.port -ge 7900 -and [int]$c.port -le 7949) "test shell config port $($c.port) (not 7788)"
        Check ([string]$e.OMNIAPI_HOME -and [string]$e.OMNIAPI_HOME -notmatch '\\\.omniapi\\?$') "test shell config data home $($e.OMNIAPI_HOME) (not ~\.omniapi)"
        Check ([string]$e.STORAGE__BASE_PATH -and [string]$e.STORAGE__BASE_PATH -notmatch 'Documents') "works folder $($e.STORAGE__BASE_PATH) (never Documents\OmniAPI)"
        Check ($e.OMNIAPI_DESKTOP_RUN_VALUE -eq 'OmniAPI-Test') "settings page's logon switch writes Run\OmniAPI-Test"
        Check ([string]$e.OMNIAPI_CLAUDE_CONFIG -and [string]$e.OMNIAPI_CLAUDE_CONFIG -notmatch '\.claude\.json$') "settings page's 'connect Claude Code' writes a stand-in, not ~/.claude.json"
        Check ($e.OMNIAPI_OFFLINE -eq '1' -and $e.OMNIAPI_DEV -eq '1') "offline sandbox with fake providers"
        Check ($c.update_check.enabled -eq $false) "no new-version check"
    } else {
        $repoCfg = Join-Path $DesktopDir 'config\shell.installed.json'
        Check ((Get-FileHash -LiteralPath $ShellConfig).Hash -eq (Get-FileHash -LiteralPath $repoCfg).Hash) "released shell config is config\shell.installed.json byte for byte"
        $c = Get-Content -LiteralPath $ShellConfig -Raw | ConvertFrom-Json
        Check ([int]$c.port -eq 7788 -and -not $c.service.env.OMNIAPI_DESKTOP_RUN_VALUE) "released shell config: port 7788, default logon value"
    }
}
if ($OmniCmd) {
    $src = Join-Path $DesktopDir $(if ($test) { 'config\omni.test.cmd' } else { 'config\omni.cmd' })
    Check ((Get-FileHash -LiteralPath $OmniCmd).Hash -eq (Get-FileHash -LiteralPath $src).Hash) "omni.cmd is $([IO.Path]::GetFileName($src)) byte for byte"
    if ($test) {
        $t = Get-Content -LiteralPath $OmniCmd -Raw
        Check ($t -match 'OMNIAPI_DEFAULT_PORT=7939' -and $t -match '\.omniapi-test' -and $t -match 'OMNIAPI_DESKTOP_RUN_VALUE=OmniAPI-Test') "test omni.cmd defaults to port 7939, ~\.omniapi-test, Run\OmniAPI-Test"
    }
}

if ($fail) { "check-identity: $fail check(s) FAILED"; exit 1 }
"check-identity: all checks passed"
exit 0
