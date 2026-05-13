@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv
set REQ=%HERE%requirements.txt
set LOG=%HERE%install.log

echo [gnss] GNSS Recorder Dashboard - Installer
echo [gnss] Log: %LOG%
echo. > "%LOG%"

:: ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Python not found on PATH.
  echo [gnss] Download from https://www.python.org/downloads/windows/
  echo [gnss] Tick "Add python.exe to PATH" during install.
  echo.
  pause
  exit /b 1
)

set PYMM=
for /f "tokens=*" %%v in ('python -c "import sys; v=sys.version_info; print(str(v.major)+chr(46)+str(v.minor))" 2^>nul') do set PYMM=%%v

if "%PYMM%"=="" (
  echo [gnss] ERROR: Could not detect Python version.
  pause
  exit /b 1
)
echo [gnss] Python %PYMM% detected.

if "%PYMM%"=="3.6" goto PY_OLD
if "%PYMM%"=="3.7" goto PY_OLD
goto PY_OK

:PY_OLD
echo [gnss] ERROR: Python %PYMM% is too old. Need 3.8 or newer.
echo [gnss] Download Python 3.11 from https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:PY_OK

:: ── Create venv ──────────────────────────────────────────────────────────────
if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] Creating virtual environment...
  python -m venv "%VENV%" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] ERROR: venv creation failed. See install.log
    pause
    exit /b 1
  )
)

:: ── Upgrade pip ──────────────────────────────────────────────────────────────
echo [gnss] Upgrading pip...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >>"%LOG%" 2>&1

:: ── Install packages ─────────────────────────────────────────────────────────
echo [gnss] Installing packages (1-5 min on first run)...
"%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] First attempt failed - retrying...
  "%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] ERROR: pip install failed after retry. See install.log
    pause
    exit /b 1
  )
)

:: ── Verify key packages actually installed ───────────────────────────────────
echo [gnss] Verifying installation...
set VERIFY_FAIL=0

"%VENV%\Scripts\python.exe" -c "import streamlit" >nul 2>&1
if errorlevel 1 (
  echo [gnss] streamlit missing - installing separately...
  "%VENV%\Scripts\python.exe" -m pip install streamlit >>"%LOG%" 2>&1
  "%VENV%\Scripts\python.exe" -c "import streamlit" >nul 2>&1
  if errorlevel 1 set VERIFY_FAIL=1
)

"%VENV%\Scripts\python.exe" -c "import pandas" >nul 2>&1
if errorlevel 1 (
  echo [gnss] pandas missing - installing separately...
  "%VENV%\Scripts\python.exe" -m pip install pandas >>"%LOG%" 2>&1
  "%VENV%\Scripts\python.exe" -c "import pandas" >nul 2>&1
  if errorlevel 1 set VERIFY_FAIL=1
)

"%VENV%\Scripts\python.exe" -c "import plotly" >nul 2>&1
if errorlevel 1 (
  echo [gnss] plotly missing - installing separately...
  "%VENV%\Scripts\python.exe" -m pip install plotly >>"%LOG%" 2>&1
  "%VENV%\Scripts\python.exe" -c "import plotly" >nul 2>&1
  if errorlevel 1 set VERIFY_FAIL=1
)

if "%VERIFY_FAIL%"=="1" (
  echo.
  echo [gnss] ERROR: Some packages could not be installed.
  echo [gnss] Check your internet connection and see install.log for details.
  echo.
  pause
  exit /b 1
)

echo.
echo [gnss] All packages verified OK.
echo [gnss] Done. Double-click RUN_DASHBOARD.bat to launch.
echo.
pause
endlocal