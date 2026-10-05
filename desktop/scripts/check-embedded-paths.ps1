# Look for build-machine paths inside built binaries (the shell exe that ships in the installer).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\check-embedded-paths.ps1 [-Path <exe> ...] [-Extra <text> ...]
#
# Each file is read as bytes and searched, ignoring case, for every needle in two encodings:
# 8-bit (ASCII / UTF-8) and UTF-16LE (Windows wide strings). Needles are worked out on the machine
# that runs the check, so nothing about any machine is written here:
#   - this checkout, the main checkout (git's common dir), and the top folder of each (e.g. D:\src);
#   - the bare name of that top folder when it is distinctive (6+ characters, not a common word);
#   - %USERPROFILE%, CARGO_HOME, %TEMP%, the cargo target dir, and any C:\Users\ / C:/Users/ prefix;
#   - "\.cargo\registry" and "/.cargo/registry" (crate sources), plus -Extra.
# Each needle is searched with both \ and / as separators.
# Exit 0 = nothing found; exit 1 = hits (each printed with its offset and surrounding text).
param(
    [string[]]$Path = @(),
    [string[]]$Extra = @(),
    [int]$Show = 12
)
$ErrorActionPreference = 'Stop'
$DesktopDir = Split-Path -Parent $PSScriptRoot
$RepoDir = Split-Path -Parent $DesktopDir
if (-not $Path.Count) { $Path = @(Join-Path $env:TEMP 'omniapi-proto\cargo-target-desktop\release\OmniAPI.exe') }

function Top([string]$p) {
    $full = [IO.Path]::GetFullPath($p).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full)
    $rest = $full.Substring($root.Length)
    if (-not $rest) { return $null }
    return Join-Path $root ($rest.Split('\')[0])
}

$roots = New-Object System.Collections.ArrayList
[void]$roots.Add($RepoDir)
$common = git -C $RepoDir rev-parse --path-format=absolute --git-common-dir
if ($LASTEXITCODE -eq 0 -and $common) { [void]$roots.Add((Split-Path -Parent ($common -replace '/', '\'))) }
$LASTEXITCODE = 0

$needles = New-Object System.Collections.ArrayList
function Add-Needle([string]$s) { if ($s -and $s.Trim().Length -ge 3 -and -not $needles.Contains($s.TrimEnd('\', '/'))) { [void]$needles.Add($s.TrimEnd('\', '/')) } }
foreach ($r in $roots) {
    Add-Needle $r
    $t = Top $r
    if ($t) {
        Add-Needle $t
        $name = Split-Path -Leaf $t
        # a bare folder name like "src" or "Users" would match ordinary text; only distinctive ones
        if ($name.Length -ge 6 -and $name -notin @('Users', 'source', 'Program Files', 'projects', 'Documents', 'Desktop')) { Add-Needle $name }
    }
}
Add-Needle $env:USERPROFILE
Add-Needle $(if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' })
Add-Needle $env:TEMP
Add-Needle (Join-Path $env:TEMP 'omniapi-proto\cargo-target-desktop')
Add-Needle ':\Users\'
Add-Needle '\.cargo\registry'
foreach ($e in $Extra) { Add-Needle $e }
# both separators
$all = New-Object System.Collections.ArrayList
foreach ($n in $needles) {
    foreach ($v in @($n, ($n -replace '\\', '/'))) { if (-not $all.Contains($v)) { [void]$all.Add($v) } }
}

# 8-bit view of the bytes (code page 28591 maps every byte to the same code point)
$latin1 = [Text.Encoding]::GetEncoding(28591)
function Printable([string]$s) { ($s.ToCharArray() | ForEach-Object { if ([int]$_ -ge 32 -and [int]$_ -lt 127) { $_ } else { '.' } }) -join '' }

$failed = $false
foreach ($file in $Path) {
    if (-not (Test-Path -LiteralPath $file)) { Write-Output "MISSING $file"; $failed = $true; continue }
    $bytes = [IO.File]::ReadAllBytes($file)
    $text = $latin1.GetString($bytes)
    $hits = 0
    $shown = 0
    $places = New-Object 'System.Collections.Generic.HashSet[long]'
    $perNeedle = [ordered]@{}
    foreach ($n in $all) {
        foreach ($enc in 'ascii', 'utf16le') {
            $needle = if ($enc -eq 'ascii') { $latin1.GetString([Text.Encoding]::UTF8.GetBytes($n)) } else { $latin1.GetString([Text.Encoding]::Unicode.GetBytes($n)) }
            $i = 0
            $count = 0
            while (($i = $text.IndexOf($needle, $i, [StringComparison]::OrdinalIgnoreCase)) -ge 0) {
                $count++
                [void]$places.Add([long]($i - ($i % 64)))
                if ($shown -lt $Show) {
                    $from = [Math]::Max(0, $i - 40)
                    $ctx = $text.Substring($from, [Math]::Min(160, $text.Length - $from))
                    if ($enc -eq 'utf16le') { $ctx = $ctx -replace "`0", '' }
                    Write-Output ("  [{0}] @0x{1:X}  {2}" -f $enc, $i, (Printable $ctx))
                    $shown++
                }
                $i += $needle.Length
            }
            if ($count) { $perNeedle["$enc  $n"] = $count; $hits += $count }
        }
    }
    Write-Output ("{0}: {1} hit(s) at about {2} distinct places, {3} needles x 2 encodings ({4:N1} MB)" -f $file, $hits, $places.Count, $all.Count, ($bytes.Length / 1MB))
    foreach ($k in $perNeedle.Keys) { Write-Output ("    {0,6}  {1}" -f $perNeedle[$k], $k) }
    if ($hits) { $failed = $true }
}
if ($failed) { exit 1 }
exit 0
