@echo off
setlocal enabledelayedexpansion

REM Offline one-click installer. Requires: python on PATH.
REM Installs deps from offline_installer\wheelhouse (no internet).

set HERE=%~dp0
set ROOT=%HERE%..\..
set PROJECT=%HERE%..
set VENV=%ROOT%\.venv_offline
set WHEELHOUSE=%HERE%wheelhouse
set REQ=%PROJECT%\requirements.txt
set LOG=%HERE%install_offline.log

echo [gnss] Writing log to: %LOG%
echo. > "%LOG%"

echo [gnss] Root: %ROOT%
echo [gnss] Project: %PROJECT%
echo [gnss] Venv: %VENV%
echo [gnss] Wheelhouse: %WHEELHOUSE%
echo [gnss] Requirements: %REQ%
echo [gnss] Root: %ROOT%>>"%LOG%"
echo [gnss] Project: %PROJECT%>>"%LOG%"
echo [gnss] Venv: %VENV%>>"%LOG%"
echo [gnss] Wheelhouse: %WHEELHOUSE%>>"%LOG%"
echo [gnss] Requirements: %REQ%>>"%LOG%"

REM Detect python and abort with a CLEAR message if it's missing / is the
REM Microsoft Store stub. Previously a missing python silently produced an
REM empty PYTAG, then we tripped much later with a confusing "wheelhouse
REM missing" error. Run python --version first; non-zero / empty output here
REM means there's no real interpreter on PATH.
python --version >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: 'python' is not on PATH or points at the Microsoft Store stub.
  echo [gnss] Install Python 3.10+ from https://www.python.org/downloads/windows/
  echo [gnss] During install, check the box "Add python.exe to PATH".
  echo [gnss] Then re-run INSTALL_OFFLINE.bat.
  echo [gnss] ERROR: python missing on PATH>>"%LOG%"
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)

REM Detect python version and wheel tag (escape % for cmd.exe)
set PYTAG=
set PYMM=
for /f %%v in ('python -c "import sys; print('cp%%d%%d'%%(sys.version_info.major,sys.version_info.minor))" 2^>^&1') do set PYTAG=%%v
for /f %%v in ('python -c "import sys; print('%%d.%%d'%%(sys.version_info.major,sys.version_info.minor))" 2^>^&1') do set PYMM=%%v
REM Trim potential whitespace
for /f "tokens=* delims= " %%v in ("%PYTAG%") do set PYTAG=%%v
for /f "tokens=* delims= " %%v in ("%PYMM%") do set PYMM=%%v

REM If detection still produced nothing, bail with a helpful error.
if "%PYTAG%"=="" (
  echo [gnss] ERROR: Could not determine Python version from 'python' command.
  echo [gnss] Try running:  python -c "import sys; print(sys.version)"
  echo [gnss] If that prints nothing, install Python 3.10+ and retry.
  echo [gnss] ERROR: python version detection returned empty>>"%LOG%"
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)
echo [gnss] Detected: %PYTAG% (Python %PYMM%)
echo [gnss] Detected: %PYTAG% (Python %PYMM%)>>"%LOG%"

REM Enforce Python 3.10+
if "%PYMM%"=="3.8" goto PY_TOO_OLD
if "%PYMM%"=="3.9" goto PY_TOO_OLD
goto PY_OK

:PY_TOO_OLD
echo [gnss] ERROR: Python %PYMM% detected. This bundle requires Python 3.10 or newer.
echo [gnss] Install Python 3.10+ and re-run INSTALL_OFFLINE.bat
echo [gnss] ERROR: Python too old (%PYMM%)>>"%LOG%"
echo.
echo Press any key to close this window...
pause >nul
exit /b 1

:PY_OK

REM If a version-specific wheelhouse exists (wheelhouse_cp311, etc), prefer it.
if exist "%HERE%wheelhouse_%PYTAG%\" (
  set WHEELHOUSE=%HERE%wheelhouse_%PYTAG%
  echo [gnss] Using version-specific wheelhouse: %WHEELHOUSE%
  echo [gnss] Using version-specific wheelhouse: %WHEELHOUSE%>>"%LOG%"
  goto WHEELHOUSE_OK
)
REM Back-compat: some bundles may include wheelhouse_cp311 style (no underscore)
if exist "%HERE%wheelhouse%PYTAG%\" (
  set WHEELHOUSE=%HERE%wheelhouse%PYTAG%
  echo [gnss] Using version-specific wheelhouse: %WHEELHOUSE%
  echo [gnss] Using version-specific wheelhouse: %WHEELHOUSE%>>"%LOG%"
  goto WHEELHOUSE_OK
)

REM If wheelhouse exists but no version-specific match, keep generic wheelhouse.
REM (This still works for pure-Python deps, but may fail for compiled wheels.)

:WHEELHOUSE_OK
if not exist "%WHEELHOUSE%" (
  echo [gnss] ERROR: wheelhouse folder missing: %WHEELHOUSE%
  echo [gnss] Run DOWNLOAD_WHEELS.py on an ONLINE machine first.
  echo [gnss] ERROR: wheelhouse folder missing: %WHEELHOUSE%>>"%LOG%"
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)

REM Quick compatibility check: ensure there is at least one wheel matching the python tag.
dir /b "%WHEELHOUSE%\*%PYTAG%*.whl" >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: wheelhouse does not contain %PYTAG% wheels for this Python.
  echo [gnss] This bundle was likely built for a different Python version.
  echo [gnss] Fix: on an ONLINE PC with Python matching this PC, run:
  echo [gnss]   offline_installer\DOWNLOAD_WHEELS.py
  echo [gnss] Then re-send the updated wheelhouse.
  echo [gnss] ERROR: wheelhouse missing %PYTAG% wheels>>"%LOG%"
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)

REM Create venv if missing
if not exist "%VENV%\Scripts\python.exe" (
  echo [gnss] Creating virtual environment...
  echo [gnss] Creating virtual environment...>>"%LOG%"
  python -m venv "%VENV%" >>"%LOG%" 2>&1
)

echo [gnss] Installing dependencies from wheelhouse (offline)...
"%VENV%\Scripts\python.exe" -m pip install --no-index --find-links "%WHEELHOUSE%" -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: pip install failed.
  echo [gnss] See log: %LOG%
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 1
)

echo.
echo [gnss] DONE. Install succeeded.
echo [gnss] To launch the dashboard, double-click:
echo [gnss]   offline_installer\RUN_DASHBOARD_OFFLINE.bat
echo.
echo Press any key to close this window...
pause >nul

endlocal
