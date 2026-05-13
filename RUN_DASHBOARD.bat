@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] ERROR: Not installed. Run INSTALL.bat first.
  echo.
  pause
  exit /b 1
)

set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
set STREAMLIT_GLOBAL_DISABLE_WIDGET_STATE_DUPLICATION_WARNING=true

set "STREAMLIT_CFG_DIR=%USERPROFILE%\.streamlit"
if not exist "%STREAMLIT_CFG_DIR%" mkdir "%STREAMLIT_CFG_DIR%" >nul 2>&1
if not exist "%STREAMLIT_CFG_DIR%\credentials.toml" (
  > "%STREAMLIT_CFG_DIR%\credentials.toml" echo [general]
  >> "%STREAMLIT_CFG_DIR%\credentials.toml" echo email = ""
)

if exist "%HERE%tools\runpkr00\runpkr00.exe"                     set "GNSS_RUNPKR00=%HERE%tools\runpkr00\runpkr00.exe"
if exist "%HERE%tools\rtklib\convbin.exe"                        set "GNSS_CONVBIN=%HERE%tools\rtklib\convbin.exe"
if exist "%HERE%tools\rtklib\rnx2rtkp.exe"                      set "GNSS_RNX2RTKP=%HERE%tools\rtklib\rnx2rtkp.exe"
if exist "%HERE%tools\convert_to_rinex\convertToRinex_cli.exe"  set "GNSS_CTR=%HERE%tools\convert_to_rinex\convertToRinex_cli.exe"

set "PYTHONPATH=%HERE%;%PYTHONPATH%"

set "PORT="
for /L %%P in (8501,1,8520) do (
  if not defined PORT (
    "%VENV%\Scripts\python.exe" -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',%%P)); s.close()" >nul 2>&1
    if not errorlevel 1 set "PORT=%%P"
  )
)
if not defined PORT set "PORT=8501"

echo [gnss] Starting dashboard at http://localhost:!PORT!
echo [gnss] Press Ctrl+C to stop.
echo.

"%VENV%\Scripts\python.exe" -m streamlit run "%HERE%dashboard.py" ^
  --server.headless=true ^
  --browser.gatherUsageStats=false ^
  --server.port=!PORT!

echo.
echo [gnss] Dashboard stopped.
pause
endlocal