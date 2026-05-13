@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv
set REQ=%HERE%requirements.txt
set LOG=%HERE%install.log

echo [gnss] GNSS Recorder Dashboard - Installer
echo [gnss] Log: %LOG%
echo.
echo. > "%LOG%"
echo Install started: %DATE% %TIME% >> "%LOG%"

:: ── Find Python (try python, py -3, py) ──────────────────────────────────────
set PYEXE=
python --version >nul 2>&1
if not errorlevel 1 set PYEXE=python
if "%PYEXE%"=="" (
  py -3 --version >nul 2>&1
  if not errorlevel 1 set PYEXE=py -3
)
if "%PYEXE%"=="" (
  py --version >nul 2>&1
  if not errorlevel 1 set PYEXE=py
)
if "%PYEXE%"=="" (
  echo [gnss] ERROR: Python not found.
  echo [gnss] Download from https://www.python.org/downloads/windows/
  echo [gnss] Tick "Add python.exe to PATH" during install, then re-run this.
  echo.
  pause
  exit /b 1
)

:: ── Check Python version ─────────────────────────────────────────────────────
set PYMM=
for /f "tokens=*" %%v in ('%PYEXE% -c "import sys; v=sys.version_info; print(str(v.major)+chr(46)+str(v.minor))" 2^>nul') do set PYMM=%%v
if "%PYMM%"=="" (
  echo [gnss] ERROR: Python found but not working correctly.
  pause
  exit /b 1
)
echo [gnss] Python %PYMM% detected.
echo Python %PYMM% >> "%LOG%"

for /f "tokens=1 delims=." %%a in ("%PYMM%") do set PYMAJ=%%a
for /f "tokens=2 delims=." %%b in ("%PYMM%") do set PYMIN=%%b
if %PYMAJ% LSS 3 goto PY_OLD
if %PYMAJ% EQU 3 if %PYMIN% LSS 8 goto PY_OLD
goto PY_OK

:PY_OLD
echo [gnss] ERROR: Python %PYMM% is too old. Need 3.8 or newer.
echo [gnss] Download Python 3.11 from https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:PY_OK

:: ── Create or repair venv ────────────────────────────────────────────────────
if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo [gnss] Existing venv is broken - deleting and recreating...
    %PYEXE% -c "import shutil; shutil.rmtree(r'%VENV%')" >nul 2>&1
  )
)
if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] Creating virtual environment...
  %PYEXE% -m venv "%VENV%" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] ERROR: venv creation failed. See install.log
    pause
    exit /b 1
  )
)
echo [gnss] Virtual environment OK.

:: ── Pip flags: retries + timeout + trusted-host for corporate proxies ─────────
set PF=--retries 5 --timeout 60 --trusted-host pypi.org --trusted-host files.pythonhosted.org

:: ── Upgrade pip ──────────────────────────────────────────────────────────────
echo [gnss] Upgrading pip...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip %PF% >>"%LOG%" 2>&1

:: ── Install all requirements ─────────────────────────────────────────────────
echo [gnss] Installing packages (1-5 min first run, needs internet)...
"%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" %PF% >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] First attempt failed - retrying...
  "%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" %PF% >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] Bulk install had errors - checking each package individually...
  )
)

:: ── Verify + individually install each critical package ──────────────────────
echo [gnss] Verifying packages...
set ALLFAIL=0

call :CHECK streamlit streamlit
call :CHECK pandas pandas
call :CHECK plotly plotly
call :CHECK numpy numpy
call :CHECK requests requests

if "%ALLFAIL%"=="1" (
  echo.
  echo [gnss] ERROR: One or more packages could not be installed.
  echo [gnss] Check your internet connection and see install.log for details.
  echo.
  pause
  exit /b 1
)

:: ── Smoke test ───────────────────────────────────────────────────────────────
echo [gnss] Running smoke test...
"%VENV%\Scripts\python.exe" -c "import streamlit,pandas,numpy,plotly,requests,sqlite3" >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Smoke test failed - see install.log
  "%VENV%\Scripts\python.exe" -c "import streamlit,pandas,numpy,plotly,requests,sqlite3" >>"%LOG%" 2>&1
  pause
  exit /b 1
)

echo.
echo [gnss] ============================================
echo [gnss]  All packages OK.
echo [gnss]  Done - double-click RUN_DASHBOARD.bat
echo [gnss] ============================================
echo.
pause
endlocal
exit /b 0

:: ── Subroutine: verify import, install individually if missing ────────────────
:CHECK
set _M=%1
set _P=%2
"%VENV%\Scripts\python.exe" -c "import %_M%" >nul 2>&1
if errorlevel 1 (
  echo [gnss]   %_P% missing - installing separately...
  "%VENV%\Scripts\python.exe" -m pip install %_P% %PF% >>"%LOG%" 2>&1
  "%VENV%\Scripts\python.exe" -c "import %_M%" >nul 2>&1
  if errorlevel 1 (
    echo [gnss]   ERROR: %_P% still missing after install.
    set ALLFAIL=1
  ) else (
    echo [gnss]   %_P% OK.
  )
) else (
  echo [gnss]   %_P% OK.
)
exit /b 0
