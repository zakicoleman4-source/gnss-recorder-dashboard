$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host ("[setup] " + $msg) -ForegroundColor Cyan }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Info "Creating venv (if missing)..."
if (-not (Test-Path ".\\.venv\\Scripts\\python.exe")) {
  python -m venv .venv
}

Info "Installing dashboard dependencies..."
.\.venv\Scripts\python.exe -m pip install -r .\requirements_streamlit.txt | Out-Host

Info "Done. Next: run .\\run_dashboard.ps1"

