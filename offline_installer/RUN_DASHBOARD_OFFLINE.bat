@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set ROOT=%HERE%..\..
set VENV=%ROOT%\.venv_gnss

if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] ERROR: venv not found: %VENV%
  echo [gnss] Run INSTALL_OFFLINE.bat first.
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)

set GNSS_OFFLINE=1

REM Suppress Streamlit's interactive "enter your email" welcome prompt on
REM first launch -- on a client desktop it pauses startup waiting for input
REM that the operator may not see. Also disable usage stats (we are offline).
set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
set STREAMLIT_GLOBAL_DISABLE_WIDGET_STATE_DUPLICATION_WARNING=true

REM Pre-create the streamlit credentials file so the welcome flow is skipped
REM even if env vars are stripped by group policy.
set "STREAMLIT_CFG_DIR=%USERPROFILE%\.streamlit"
if not exist "%STREAMLIT_CFG_DIR%" mkdir "%STREAMLIT_CFG_DIR%" >nul 2>&1
if not exist "%STREAMLIT_CFG_DIR%\credentials.toml" (
  > "%STREAMLIT_CFG_DIR%\credentials.toml" echo [general]
  >> "%STREAMLIT_CFG_DIR%\credentials.toml" echo email = ""
)

REM runpkr00 -- the dashboard reads GNSS_RUNPKR00 to pre-fill the
REM tool path in the UI, saving the operator from typing it manually.
if exist "%ROOT%\gnss-recorder-dashboard\tools\runpkr00\runpkr00.exe" (
  set "GNSS_RUNPKR00=%ROOT%\gnss-recorder-dashboard\tools\runpkr00\runpkr00.exe"
)

REM Make local modules importable regardless of working directory.
set "PYTHONPATH=%ROOT%\gnss-recorder-dashboard;%PYTHONPATH%"

REM Where _dbg writes debug-c48812.log -- pin to the app folder so we can
REM always tell the client "send me <app>\debug-c48812.log".
set "GNSS_DEBUG_DIR=%ROOT%\gnss-recorder-dashboard"

REM Find a free TCP port starting at 8501. Streamlit's default is 8501, but
REM that's also Slack's localhost dev port and many other tools', so trying a
REM small range here saves a "address already in use" support call.
set "PORT="
for /L %%P in (8501,1,8520) do (
  if not defined PORT (
    netstat -an | findstr /R /C:":%%P  *LISTENING" >nul 2>&1
    if errorlevel 1 (
      set "PORT=%%P"
    )
  )
)
if not defined PORT set "PORT=8501"

echo [gnss] Starting dashboard (offline mode) on port !PORT! ...
echo [gnss] If your browser does not open automatically, navigate to:
echo [gnss]   http://localhost:!PORT!
echo.
cd /d "%ROOT%"
if errorlevel 1 (
  echo [gnss] ERROR: could not change to root directory: %ROOT%
  echo [gnss] The install directory may have moved or the drive is offline.
  pause >nul
  exit /b 1
)
"%VENV%\Scripts\python.exe" -m streamlit run gnss-recorder-dashboard\dashboard.py --server.headless=true --browser.gatherUsageStats=false --server.port=!PORT!

REM If we get here, Streamlit either exited cleanly or crashed. Either way
REM keep the window open so the client can see the message instead of the
REM cmd window vanishing the instant something goes wrong.
echo.
echo [gnss] Streamlit exited (code %ERRORLEVEL%).
echo.
echo Press any key to close this window...
pause >nul

endlocal
