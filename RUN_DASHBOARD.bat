@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] ERROR: Not installed yet. Run INSTALL.bat first.
  echo.
  pause >nul
  exit /b 1
)

REM ── Skip Streamlit's first-run email prompt ────────────────────────────────
set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
set STREAMLIT_GLOBAL_DISABLE_WIDGET_STATE_DUPLICATION_WARNING=true
set "STREAMLIT_CFG_DIR=%USERPROFILE%\.streamlit"
if not exist "%STREAMLIT_CFG_DIR%" mkdir "%STREAMLIT_CFG_DIR%" >nul 2>&1
if not exist "%STREAMLIT_CFG_DIR%\credentials.toml" (
  > "%STREAMLIT_CFG_DIR%\credentials.toml" echo [general]
  >> "%STREAMLIT_CFG_DIR%\credentials.toml" echo email = ""
)

REM ── Pre-fill tool paths if bundled alongside the app ─────────────────────
if exist "%HERE%tools\runpkr00\runpkr00.exe" set "GNSS_RUNPKR00=%HERE%tools\runpkr00\runpkr00.exe"
if exist "%HERE%tools\rtklib\convbin.exe"    set "GNSS_CONVBIN=%HERE%tools\rtklib\convbin.exe"
if exist "%HERE%tools\rtklib\rnx2rtkp.exe"  set "GNSS_RNX2RTKP=%HERE%tools\rtklib\rnx2rtkp.exe"

set "PYTHONPATH=%HERE%;%PYTHONPATH%"

REM ── Find a free port ──────────────────────────────────────────────────────
set "PORT="
for /L %%P in (8501,1,8520) do (
  if not defined PORT (
    "%VENV%\Scripts\python.exe" -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',%%P)); s.close()" >nul 2>&1
    if not errorlevel 1 set "PORT=%%P"
  )
)
if not defined PORT set "PORT=8501"

echo [gnss] Starting dashboard on http://localhost:!PORT! ...
echo [gnss] Press Ctrl+C in this window to stop.
echo.

"%VENV%\Scripts\python.exe" -m streamlit run "%HERE%dashboard.py" ^
  --server.headless=true ^
  --browser.gatherUsageStats=false ^
  --server.port=!PORT!

echo.
echo [gnss] Dashboard stopped (exit code %ERRORLEVEL%).
echo Press any key to close...
pause >nul
endlocal
