@echo off
setlocal enabledelayedexpansion

REM GNSS Dashboard installer — requires Python 3.8+ and internet access.

set HERE=%~dp0
set VENV=%HERE%.venv
set REQ=%HERE%requirements.txt
set LOG=%HERE%install.log

echo [gnss] GNSS Recorder Dashboard installer
echo [gnss] Log: %LOG%
echo. > "%LOG%"

REM ── Check Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Python not found on PATH.
  echo [gnss] Download from https://www.python.org/downloads/windows/
  echo [gnss] During install check "Add python.exe to PATH".
  pause >nul
  exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))" 2^>^&1') do set PYMM=%%v

echo [gnss] Python %PYMM% detected.
echo [gnss] Python %PYMM%>>"%LOG%"

if "%PYMM%"=="3.6" goto PY_OLD
if "%PYMM%"=="3.7" goto PY_OLD
goto PY_OK

:PY_OLD
echo [gnss] ERROR: Python %PYMM% is too old. Requires 3.8 or newer.
echo [gnss] Download Python 3.11+ from https://www.python.org/downloads/windows/
pause >nul
exit /b 1

:PY_OK

REM ── Create venv ───────────────────────────────────────────────────────────
if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] Creating virtual environment...
  python -m venv "%VENV%" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] ERROR: Could not create virtual environment. See install.log
    pause >nul
    exit /b 1
  )
)

REM ── Install dependencies ──────────────────────────────────────────────────
echo [gnss] Installing dependencies (this may take 1-3 minutes)...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >>"%LOG%" 2>&1
"%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: pip install failed. See install.log for details.
  pause >nul
  exit /b 1
)

echo.
echo [gnss] Install complete.
echo [gnss] Run the dashboard by double-clicking RUN_DASHBOARD.bat
echo.
pause >nul
endlocal
