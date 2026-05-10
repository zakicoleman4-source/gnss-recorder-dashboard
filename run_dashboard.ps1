$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host ("[run] " + $msg) -ForegroundColor Cyan }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path ".\\.venv\\Scripts\\python.exe")) {
  Info "Venv missing. Run .\\setup_env.ps1 first."
  exit 1
}

Info "Starting GNSS dashboard (Streamlit)..."
Info "If prompted by Windows Firewall, allow Private networks."
.\.venv\Scripts\python.exe -m streamlit run .\streamlit_app.py

