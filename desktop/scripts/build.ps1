# Build the desktop shell: release exe + NSIS installer.
#   powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build.ps1 [-NoBundle]
# Big outputs stay outside the repo (the project folder is cloud-synced): CARGO_TARGET_DIR is
# %TEMP%\omniapi-proto\cargo-target-desktop, the installer is copied to %TEMP%\omniapi-proto\m5\installers.
# Needs `npm install` in desktop/ once (the Tauri CLI is a devDependency there).
param(
    [switch]$NoBundle,          # exe only (cargo build --release), no installer
    [switch]$SkipMemoryCheck,
    [int]$Jobs = 4              # fewer parallel rustc = lower peak memory (this machine idles near 88% commit)
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$pct = Get-CommitPct
Write-Output "commit charge $pct%"
if ($pct -ge 90 -and -not $SkipMemoryCheck) { throw "commit charge >= 90%; not building (wait, or lower -Jobs)" }

$env:CARGO_TARGET_DIR = $TargetDir
$env:CARGO_BUILD_JOBS = "$Jobs"
$src = Join-Path $DesktopDir 'src-tauri'
# same rustc flags as package.ps1 (no build-machine paths in the exe; switching flags would rebuild everything)
. (Join-Path $PSScriptRoot 'neutral-paths.ps1')
$dirs = Set-NeutralPathFlags $src
$sw = [Diagnostics.Stopwatch]::StartNew()
if ($NoBundle) {
    Push-Location $src
    try { cargo build --release; if ($LASTEXITCODE) { throw "cargo build failed ($LASTEXITCODE)" } } finally { Pop-Location }
} else {
    $cli = Join-Path $DesktopDir 'node_modules\@tauri-apps\cli\tauri.js'
    if (-not (Test-Path $cli)) { throw "Tauri CLI missing: run npm install in $DesktopDir" }
    Push-Location $DesktopDir
    try { & node $cli build --bundles nsis; if ($LASTEXITCODE) { throw "tauri build failed ($LASTEXITCODE)" } } finally { Pop-Location }
}
Write-Output ("build took {0:N0}s" -f $sw.Elapsed.TotalSeconds)

$exe = Join-Path $TargetDir 'release\omniapi-desktop.exe'
if (Test-Path $exe) { Write-Output ("exe        {0}  {1:N2} MB" -f $exe, ((Get-Item $exe).Length / 1MB)) }
$built = @($exe, (Get-DefaultExe)) | Where-Object { Test-Path $_ } | Sort-Object { (Get-Item $_).LastWriteTime } | Select-Object -Last 1
& (Join-Path $PSScriptRoot 'check-embedded-paths.ps1') -Path $built -Extra @($dirs.BuildDir, $dirs.TargetDir) -Show 5
if ($LASTEXITCODE) { throw "the exe contains build-machine paths (check-embedded-paths.ps1)" }
if (-not $NoBundle) {
    $version = (Get-Content (Join-Path $src 'tauri.conf.json') -Raw | ConvertFrom-Json).version
    $nsisDir = Join-Path $TargetDir 'release\bundle\nsis'
    $setup = Get-ChildItem $nsisDir -Filter "*_${version}_x64-setup.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $setup) { throw "no installer for $version in $nsisDir" }
    $outDir = Join-Path $Work 'installers'
    New-Item -ItemType Directory -Force $outDir | Out-Null
    # ASCII name (GitHub release assets drop non-ASCII characters)
    $dest = Join-Path $outDir ("OmniAPI_{0}_x64-setup.exe" -f $version)
    Copy-Item $setup.FullName $dest -Force
    Write-Output ("installer  {0}  {1:N0} bytes ({2:N2} MB)" -f $dest, (Get-Item $dest).Length, ((Get-Item $dest).Length / 1MB))
}
