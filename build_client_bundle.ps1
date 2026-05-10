param(
  [ValidateSet("lite","full")]
  [string]$Mode = "full",
  [string]$DestPath = ""
)

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host ("[bundle] " + $msg) -ForegroundColor Cyan }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspace = (Resolve-Path (Join-Path $root "..")).Path
$src = (Resolve-Path $root).Path
$dest = if ($DestPath -and $DestPath.Trim()) { $DestPath } else { (Join-Path $workspace "client_bundle_build") }

Info "Mode: $Mode"
Info "Source: $src"
Info "Dest: $dest"

if (Test-Path $dest) { Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $dest | Out-Null
New-Item -ItemType Directory -Force (Join-Path $dest "gnss-recorder-dashboard") | Out-Null

$appOut = Join-Path $dest "gnss-recorder-dashboard"

function CopyIfExists([string]$srcPath, [string]$dstFolder, [bool]$recurse = $false) {
    if (Test-Path $srcPath) {
        if ($recurse) {
            Copy-Item -Recurse -Force $srcPath $dstFolder
        } else {
            Copy-Item -Force $srcPath $dstFolder
        }
    } else {
        Info ("Skipping (not found): " + $srcPath)
    }
}

# Required: must exist or the bundle is broken.
$required = @(
    "dashboard.py",
    "scan_gnss_folder.py",
    "to2_pipeline.py",
    "analyze_station_manifest.py",
    "PRODUCT_SELF_TEST.py",
    "requirements.txt"
)
foreach ($f in $required) {
    $p = Join-Path $src $f
    if (-not (Test-Path $p)) {
        throw ("Required file missing in source: " + $p)
    }
    Copy-Item -Force $p $appOut
}

# offline_installer folder is required.
$installerSrc = Join-Path $src "offline_installer"
if (-not (Test-Path $installerSrc)) {
    throw ("Required folder missing: " + $installerSrc)
}
Copy-Item -Recurse -Force $installerSrc $appOut

# Optional: do not crash the bundle if these are absent.
CopyIfExists (Join-Path $src "streamlit_app.py") $appOut
CopyIfExists (Join-Path $src "tools") $appOut $true
CopyIfExists (Join-Path $src "INSTRUCTIONS.md") $appOut
CopyIfExists (Join-Path $src "README.md") $appOut

# If precomputed probe manifests exist, include them so the client can open instantly.
$probeManifests = Join-Path $src "._cache_geonet_probe\\exported\\_manifests"
if (Test-Path $probeManifests) {
  Info "Including precomputed manifests from: $probeManifests"
  New-Item -ItemType Directory -Force (Join-Path $appOut "_precomputed") | Out-Null
  Copy-Item -Recurse -Force $probeManifests (Join-Path $appOut "_precomputed")
}

if ($Mode -eq "lite") {
  Info "Removing wheelhouse for LITE bundle..."
  Get-ChildItem (Join-Path $appOut "offline_installer") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "wheelhouse*" } |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName; New-Item -ItemType Directory -Force $_.FullName | Out-Null }
}

Info "Done."
Info "Send this folder: $dest"
