@echo off
setlocal enabledelayedexpansion

REM GNSS Dashboard installer. Requires: Python 3.10+ on PATH, internet access.

set HERE=%~dp0
set ROOT=%HERE%..\..
set PROJECT=%HERE%..
set VENV=%ROOT%\.venv_gnss
set REQ=%PROJECT%\requirements.txt
set LOG=%HERE%install.log

echo [gnss] Writing log to: %LOG%
echo. > "%LOG%"

echo [gnss] Root: %ROOT%
echo [gnss] Project: %PROJECT%
echo [gnss] Venv: %VENV%
echo [gnss] Requirements: %REQ%
echo [gnss] Root: %ROOT%>>"%LOG%"
echo [gnss] Project: %PROJECT%>>"%LOG%"
echo [gnss] Venv: %VENV%>>"%LOG%"
echo [gnss] Requirements: %REQ%>>"%LOG%"

REM Check python is real (not the Microsoft Store stub).
python --version >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: 'python' is not on PATH or points at the Microsoft Store stub.
  echo [gnss] Install Python 3.10+ from https://www.python.org/downloads/windows/
  echo [gnss] During install, check "Add python.exe to PATH".
  echo [gnss] ERROR: python missing on PATH>>"%LOG%"
  echo.
  pause >nul
  exit /b 1
)

REM Detect version string (e.g. 3.12)
set PYMM=
for /f %%v in ('python -c "import sys; print('%%d.%%d'%%(sys.version_info.major,sys.version_info.minor))" 2^>^&1') do set PYMM=%%v
for /f "tokens=* delims= " %%v in ("%PYMM%") do set PYMM=%%v

if "%PYMM%"=="" (
  echo [gnss] ERROR: Could not detect Python version.
  echo [gnss] ERROR: version detection failed>>"%LOG%"
  pause >nul
  exit /b 1
)
echo [gnss] Detected: Python %PYMM%
echo [gnss] Detected: Python %PYMM%>>"%LOG%"

REM pandas>=2.2 / numpy>=1.26 require Python 3.9.
REM Current latest packages (pandas 3.x, numpy 2.x, streamlit 1.57) require Python 3.11.
REM pip resolves the right version automatically -- just block truly unsupported Pythons.
if "%PYMM%"=="3.6" goto PY_TOO_OLD
if "%PYMM%"=="3.7" goto PY_TOO_OLD
if "%PYMM%"=="3.8" goto PY_TOO_OLD
goto PY_OK

:PY_TOO_OLD
echo [gnss] ERROR: Python %PYMM% too old. Requires 3.9 or newer (3.11+ recommended).
echo [gnss] Install Python 3.11+ from https://www.python.org/downloads/windows/
echo [gnss] ERROR: Python too old (%PYMM%)>>"%LOG%"
pause >nul
exit /b 1

:PY_OK

REM Check requirements.txt is present.
if not exist "%REQ%" (
  echo [gnss] ERROR: requirements.txt not found at: %REQ%
  echo [gnss] ERROR: requirements.txt missing>>"%LOG%"
  pause >nul
  exit /b 1
)

REM Create venv if missing.
if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] Creating virtual environment...
  echo [gnss] Creating virtual environment...>>"%LOG%"
  python -m venv "%VENV%" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] ERROR: venv creation failed. See: %LOG%
    pause >nul
    exit /b 1
  )
)

REM Upgrade pip silently first.
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >>"%LOG%" 2>&1

echo [gnss] Installing dependencies (pip install -r requirements.txt)...
echo [gnss] Installing dependencies...>>"%LOG%"
"%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: pip install failed. See log: %LOG%
  pause >nul
  exit /b 1
)

echo.
echo [gnss] DONE. Install succeeded.
echo [gnss] To launch the dashboard, double-click:
echo [gnss]   offline_installer\RUN_DASHBOARD_OFFLINE.bat
echo.
pause >nul

endlocal
