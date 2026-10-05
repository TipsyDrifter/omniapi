# Dot-source, then call Set-NeutralPathFlags before `cargo build` / `tauri build` (package.ps1, build.ps1).
#
# rustc writes source paths into the exe (panic messages name the file: our own crate, every
# crate from the registry, generated code in OUT_DIR). --remap-path-prefix rewrites them to neutral
# names, so the shipped exe carries nothing about the machine that built it:
#   %USERPROFILE% -> /home, %TEMP% -> /tmp, CARGO_HOME -> /cargo, cargo's build dir -> /build,
#   the target dir -> /target, this checkout -> /omniapi.
# Cargo's profile `trim-paths` would do the same but is not stable in Rust 1.95.
# When several prefixes match, rustc applies the LAST one, so the list goes from broad to specific.
# The flags go through CARGO_ENCODED_RUSTFLAGS (0x1f-separated: paths may contain spaces); any
# RUSTFLAGS already set are kept in front. Changing the flags rebuilds every dependency once.
# scripts\check-embedded-paths.ps1 checks the result.
function Set-NeutralPathFlags([string]$CrateDir, [scriptblock]$Log = { param($m) Write-Output $m }) {
    $repo = Split-Path -Parent (Split-Path -Parent $CrateDir)
    Push-Location $CrateDir
    try { $meta = cargo metadata --format-version 1 --no-deps | ConvertFrom-Json } finally { Pop-Location }
    if ($LASTEXITCODE -or -not $meta) { throw "cargo metadata failed" }
    $cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
    $map = @(
        @($env:USERPROFILE, '/home'),
        @($env:TEMP, '/tmp'),
        @($cargoHome, '/cargo'),
        @($meta.build_directory, '/build'),
        @($meta.target_directory, '/target'),
        @($repo, '/omniapi')
    )
    $flags = New-Object System.Collections.ArrayList
    if ($env:RUSTFLAGS) { foreach ($f in ($env:RUSTFLAGS -split '\s+')) { if ($f) { [void]$flags.Add($f) } } }
    $seen = @{}
    foreach ($m in $map) {
        if (-not $m[0]) { continue }
        foreach ($from in @(([IO.Path]::GetFullPath($m[0])).TrimEnd('\'), ([string]$m[0]).TrimEnd('\', '/'))) {
            if ($seen.ContainsKey($from)) { continue }
            $seen[$from] = $true
            [void]$flags.Add("--remap-path-prefix=$from=$($m[1])")
        }
    }
    $env:CARGO_ENCODED_RUSTFLAGS = ($flags -join [char]0x1f)
    & $Log ("rustc path remaps: {0} (USERPROFILE, TEMP, CARGO_HOME, build dir, target dir, checkout)" -f $flags.Count) | Out-Host
    return [pscustomobject]@{ BuildDir = $meta.build_directory; TargetDir = $meta.target_directory }
}
