@echo off
setlocal
REM Offline-friendly GUI: pick manifests zip/folder, choose station, view summary.
REM Requires INSTALL_OFFLINE.bat completed (.venv_offline).

set HERE=%~dp0
set ROOT=%HERE%..\..
set VENV=%ROOT%\.venv_offline
set PY=%VENV%\Scripts\python.exe
set SCRIPT=%ROOT%\gnss-recorder-dashboard\analyze_station_manifest.py

if not exist "%PY%" (
  echo [gnss] ERROR: Offline venv not found: %VENV%
  echo [gnss] Run INSTALL_OFFLINE.bat first.
  echo.
  pause
  exit /b 1
)
if not exist "%SCRIPT%" (
  echo [gnss] ERROR: Missing script: %SCRIPT%
  pause
  exit /b 1
)

"%PY%" "%SCRIPT%" --gui
echo.
echo Press any key to close...
pause >nul
exit /b 0
