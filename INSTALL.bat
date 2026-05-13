@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv
set REQ=%HERE%requirements.txt
set LOG=%HERE%install.log
set WHEELS=%HERE%offline_installer

echo [gnss] GNSS Recorder Dashboard - Installer
echo [gnss] Log: %LOG%
echo.
echo Install started: %DATE% %TIME% > "%LOG%"
echo Working dir: %HERE% >> "%LOG%"

:: -- Non-ASCII path check (cmd globbing can break on extended chars) ----------
echo %HERE% | findstr /R /C:"[^ -~]" >nul
if not errorlevel 1 (
  echo [gnss] WARNING: Install path contains non-ASCII characters.
  echo [gnss] Some Python tooling may fail. Move the folder to a path like
  echo [gnss]   C:\gnss-dashboard\ and re-run if install fails.
  echo.
)

:: -- Long path warning ---------------------------------------------------------
set "HERELEN=0"
for /f %%L in ('cmd /c "echo %HERE%"^| find /v /c ""') do rem
set "HERESTR=%HERE%"
call :STRLEN HERESTR HERELEN
if %HERELEN% GTR 100 (
  echo [gnss] WARNING: Install path is %HERELEN% chars. Windows MAX_PATH=260.
  echo [gnss] Long paths can break pip. Consider moving to C:\gnss-dashboard\.
  echo.
)

:: -- Find Python (try python, py -3, py) -- reject Microsoft Store stub ------
set PYEXE=
set PYSRC=
python --version >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%w in ('where python 2^>nul') do (
    if "!PYEXE!"=="" (
      echo %%w | findstr /I "WindowsApps" >nul
      if errorlevel 1 (
        set "PYEXE=python"
        set "PYSRC=%%w"
      )
    )
  )
)
if "%PYEXE%"=="" (
  py -3 --version >nul 2>&1
  if not errorlevel 1 (
    set "PYEXE=py -3"
    for /f "delims=" %%w in ('where py 2^>nul') do if "!PYSRC!"=="" set "PYSRC=%%w (py -3)"
  )
)
if "%PYEXE%"=="" (
  py --version >nul 2>&1
  if not errorlevel 1 (
    set "PYEXE=py"
    for /f "delims=" %%w in ('where py 2^>nul') do if "!PYSRC!"=="" set "PYSRC=%%w (py)"
  )
)
if "%PYEXE%"=="" (
  echo [gnss] ERROR: Python not found, or only Microsoft Store stub is installed.
  echo [gnss] Download from https://www.python.org/downloads/windows/
  echo [gnss] Tick "Add python.exe to PATH" during install, then re-run this.
  echo ERROR: no Python found, MS Store stub rejected >> "%LOG%"
  echo.
  pause
  exit /b 1
)
echo [gnss] Python: %PYSRC%
echo Python: %PYSRC% >> "%LOG%"

:: -- Verify Python actually runs and is not a stub --------------------------
%PYEXE% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Python found but cannot execute -- may be MS Store stub.
  echo [gnss] Uninstall via Settings ^> Apps and install real Python from python.org.
  echo ERROR: Python -c "import sys" failed >> "%LOG%"
  pause
  exit /b 1
)

:: -- Check Python version -----------------------------------------------------
set PYMM=
for /f "tokens=*" %%v in ('%PYEXE% -c "import sys; v=sys.version_info; print(str(v.major)+chr(46)+str(v.minor))" 2^>nul') do set PYMM=%%v
if "%PYMM%"=="" (
  echo [gnss] ERROR: Python found but version probe failed.
  echo ERROR: version probe failed >> "%LOG%"
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
echo ERROR: Python %PYMM% too old >> "%LOG%"
echo.
pause
exit /b 1

:PY_OK

:: -- Always delete and recreate venv (guaranteed clean install) -------------
if exist "%VENV%" (
  echo [gnss] Removing existing environment...
  %PYEXE% -c "import shutil,os,time,sys; p=sys.argv[1]; [shutil.rmtree(p,ignore_errors=True) or time.sleep(0.5) for _ in range(3) if os.path.isdir(p)]; sys.exit(0 if not os.path.isdir(p) else 1)" "%VENV%" >> "%LOG%" 2>&1
  if exist "%VENV%" (
    echo [gnss] WARNING: .venv folder still exists. May be locked by antivirus or open process.
    echo [gnss] Close any open cmd/Explorer windows pointing at it and re-run.
    echo WARN: .venv still exists after rmtree >> "%LOG%"
  )
)

echo [gnss] Creating virtual environment...
%PYEXE% -m venv "%VENV%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: venv creation failed. Common causes:
  echo [gnss]   - Antivirus blocking Python writes, whitelist %HERE%
  echo [gnss]   - Read-only folder, do not install under Program Files
  echo [gnss]   - Disk full
  echo [gnss] See: %LOG%
  pause
  exit /b 1
)
echo [gnss] Virtual environment created.

:: -- Pip flags: retries + timeout + trusted-host for corporate proxies -------
set PF=--retries 5 --timeout 60 --no-cache-dir --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org

:: -- Network probe (so we can suggest offline path on failure) ---------------
set HAS_NET=1
"%VENV%\Scripts\python.exe" -c "import urllib.request as u; u.urlopen('https://pypi.org/simple/', timeout=10).close()" >nul 2>&1
if errorlevel 1 set HAS_NET=0
echo Network: HAS_NET=%HAS_NET% >> "%LOG%"

:: -- Upgrade pip --------------------------------------------------------------
echo [gnss] Upgrading pip...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip %PF% >>"%LOG%" 2>&1

:: -- Install all requirements (online) ----------------------------------------
if "%HAS_NET%"=="1" (
  echo [gnss] Installing packages from PyPI ^(1-5 min first run^)...
  "%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" %PF% >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [gnss] First attempt failed - retrying...
    "%VENV%\Scripts\python.exe" -m pip install -r "%REQ%" %PF% >>"%LOG%" 2>&1
    if errorlevel 1 (
      echo [gnss] Bulk install errored - will verify each package individually...
    )
  )
) else (
  echo [gnss] No internet detected.
  goto OFFLINE_TRY
)

:: -- Verify + individually install each critical package --------------------
:VERIFY
echo [gnss] Verifying packages...
set ALLFAIL=0
call :CHECK streamlit streamlit
call :CHECK pandas pandas
call :CHECK plotly plotly
call :CHECK numpy numpy
call :CHECK requests requests
if "%ALLFAIL%"=="1" goto OFFLINE_TRY

goto SMOKE_TEST

:: -- Offline wheelhouse fallback (if shipped) -------------------------------
:OFFLINE_TRY
if not exist "%WHEELS%" (
  echo [gnss] ERROR: Online install failed and no offline wheelhouse present.
  echo [gnss] Check your internet connection / proxy / firewall and re-run.
  echo [gnss] See: %LOG%
  pause
  exit /b 1
)
echo [gnss] Trying offline wheelhouse: %WHEELS%
set WHEEL_DIR=%WHEELS%\wheelhouse_cp%PYMAJ%%PYMIN%
if not exist "%WHEEL_DIR%" set WHEEL_DIR=%WHEELS%\wheelhouse
if not exist "%WHEEL_DIR%" (
  echo [gnss] ERROR: No wheelhouse for Python %PYMM% under %WHEELS%
  echo ERROR: no wheelhouse_cp%PYMAJ%%PYMIN% >> "%LOG%"
  pause
  exit /b 1
)
echo [gnss] Using wheels from: %WHEEL_DIR%
echo wheelhouse: %WHEEL_DIR% >> "%LOG%"
"%VENV%\Scripts\python.exe" -m pip install --no-index --find-links "%WHEEL_DIR%" -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Offline install failed. See: %LOG%
  pause
  exit /b 1
)
set ALLFAIL=0
call :CHECK streamlit streamlit
call :CHECK pandas pandas
call :CHECK plotly plotly
call :CHECK numpy numpy
call :CHECK requests requests
if "%ALLFAIL%"=="1" (
  echo [gnss] ERROR: Critical packages still missing after offline install. See: %LOG%
  pause
  exit /b 1
)

:: -- Smoke test ---------------------------------------------------------------
:SMOKE_TEST
echo [gnss] Running smoke test...
"%VENV%\Scripts\python.exe" -c "import streamlit,pandas,numpy,plotly,requests,sqlite3" >nul 2>&1
if errorlevel 1 (
  echo [gnss] ERROR: Smoke test failed - see %LOG%
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

:: -- Subroutine: verify import, install individually if missing ---------------
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

:: -- String length helper -----------------------------------------------------
:STRLEN
setlocal enabledelayedexpansion
set "s=!%~1!#"
set "len=0"
for %%P in (4096 2048 1024 512 256 128 64 32 16 8 4 2 1) do (
  if "!s:~%%P,1!" NEQ "" (
    set /a "len+=%%P"
    set "s=!s:~%%P!"
  )
)
endlocal & set "%~2=%len%"
exit /b 0