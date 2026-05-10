param(
  [switch]$SkipScan,
  [bool]$UseV2 = $true
)

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host ("[gnss] " + $msg) -ForegroundColor Cyan }
function Warn($msg) { Write-Host ("[gnss] " + $msg) -ForegroundColor Yellow }
function Die($msg) { Write-Host ("[gnss] " + $msg) -ForegroundColor Red; exit 1 }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Info "GNSS public dashboard (Cloudflare Tunnel) quickstart"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  Info "Creating venv..."
  python -m venv .venv
}

Info "Installing/updating dependencies..."
.\.venv\Scripts\python.exe -m pip install -r requirements.txt | Out-Host

if (-not $env:GNSS_DATA_ROOT) {
  $env:GNSS_DATA_ROOT = Read-Host 'Enter FULL path to your 2026 folder (example: D:\GNSS\2026)'
}
$env:GNSS_DATA_ROOT = $env:GNSS_DATA_ROOT.Trim()
if (($env:GNSS_DATA_ROOT.StartsWith('"') -and $env:GNSS_DATA_ROOT.EndsWith('"')) -or
    ($env:GNSS_DATA_ROOT.StartsWith("'") -and $env:GNSS_DATA_ROOT.EndsWith("'"))) {
  $env:GNSS_DATA_ROOT = $env:GNSS_DATA_ROOT.Substring(1, $env:GNSS_DATA_ROOT.Length - 2)
}
if (-not (Test-Path $env:GNSS_DATA_ROOT)) {
  Die "GNSS_DATA_ROOT path not found: $($env:GNSS_DATA_ROOT)"
}

$dbPath = Join-Path $here "gnss.db"
if (-not $SkipScan) {
  Info "Scanning data into DB (this can take a while on first run)..."
  .\.venv\Scripts\python.exe .\scan_to_db.py --data-root "$env:GNSS_DATA_ROOT" --db "$dbPath" | Out-Host
} else {
  Info "SkipScan enabled (not updating DB)."
}

if (-not $env:GNSS_DASH_USER) { $env:GNSS_DASH_USER = "admin" }
if (-not $env:GNSS_DASH_PASS) {
  Warn "GNSS_DASH_PASS not set. You should set a strong password."
  $env:GNSS_DASH_PASS = Read-Host "Enter password for the dashboard user '$($env:GNSS_DASH_USER)'"
}

$env:GNSS_HOST = "127.0.0.1"
$env:GNSS_PORT = "8501"
$env:GNSS_DB_PATH = $dbPath

# Ensure a receivers.csv exists so Map/VRS doesn't look "broken" (user can edit later)
$receiversCsv = Join-Path $here "receivers.csv"
if (-not (Test-Path $receiversCsv)) {
  $tmpl = Join-Path $here "receivers_template.csv"
  if (Test-Path $tmpl) {
    Info "Creating receivers.csv from template (edit later for real lat/lon)..."
    Copy-Item $tmpl $receiversCsv -Force
  }
}

# Import receivers.csv if present (safe to run repeatedly)
if (Test-Path $receiversCsv) {
  Info "Importing receiver locations/VRS flags from receivers.csv (if any)..."
  .\.venv\Scripts\python.exe .\import_receivers_csv.py --csv "$receiversCsv" --db "$dbPath" | Out-Host
}

Info "Attempting to scrape coordinates from logs (NMEA GGA best-effort)..."
.\.venv\Scripts\python.exe .\extract_coords_nmea.py --db "$dbPath" | Out-Host

Info "Attempting to auto-fill station coordinates from GeoNet (best-effort)..."
.\.venv\Scripts\python.exe .\autofill_geonet_coords.py --db "$dbPath" | Out-Host

Info "Ensuring cloudflared exists..."
$cloudflared = $null
$cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
if ($cmd) { $cloudflared = $cmd.Source }
if (-not $cloudflared) {
  $toolsDir = Join-Path $here "tools"
  New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
  $cloudflared = Join-Path $toolsDir "cloudflared.exe"
  if (-not (Test-Path $cloudflared)) {
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Info "Downloading cloudflared from $url"
    Invoke-WebRequest -Uri $url -OutFile $cloudflared
  }
}

if ($UseV2) {
  $serverScript = ".\server_v2.py"
  Info "Starting v2 dashboard server on http://127.0.0.1:8501"
} else {
  $serverScript = ".\server.py"
  Info "Starting dashboard server on http://127.0.0.1:8501"
}

Start-Process -WindowStyle Minimized -FilePath ".\.venv\Scripts\python.exe" -ArgumentList @($serverScript) | Out-Null
Start-Sleep -Seconds 2

# Basic health check (helps avoid "website doesn't work" confusion)
try {
  $metaUrl = "http://127.0.0.1:8501/api/meta"
  Info "Local health check: $metaUrl"
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 -Uri $metaUrl | Out-Null
  Info "Local server is responding."
} catch {
  Warn "Local server did not respond at http://127.0.0.1:8501 (firewall/port conflict/server crash)."
  Warn "Try: netstat -ano | findstr :8501  (then stop the conflicting process) and rerun."
}

Info "Starting Cloudflare Tunnel (public HTTPS URL will appear below)..."
Info "PM access: share the https://*.trycloudflare.com URL and the username/password."
& $cloudflared tunnel --url http://127.0.0.1:8501

