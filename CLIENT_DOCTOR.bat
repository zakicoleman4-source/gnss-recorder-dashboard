@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

echo [doctor] GNSS Recorder Dashboard - Diagnostics
echo [doctor] Folder: %HERE%
echo.

echo [doctor] System:
echo   OS: %OS%
ver
echo.

echo [doctor] Python on PATH:
where python 2>nul
echo.

echo [doctor] Python launcher (py):
where py 2>nul
py --version 2>nul
echo.

if exist "%VENV%\Scripts\python.exe" (
  echo [doctor] Virtual environment: OK
  echo   path: %VENV%
  "%VENV%\Scripts\python.exe" --version
  echo.
  echo [doctor] Installed package versions:
  "%VENV%\Scripts\python.exe" -c "import streamlit,pandas,numpy,plotly,requests,sqlite3; print('  streamlit', streamlit.__version__); print('  pandas   ', pandas.__version__); print('  numpy    ', numpy.__version__); print('  plotly   ', plotly.__version__); print('  requests ', requests.__version__); print('  sqlite3  ', sqlite3.sqlite_version)" 2>nul
  if errorlevel 1 (
    echo   ERROR: import smoke test failed.
    echo   Run INSTALL.bat to repair.
  )
) else (
  echo [doctor] Virtual environment: MISSING
  echo   Expected at: %VENV%
  echo   Run INSTALL.bat first.
)
echo.

echo [doctor] Bundled tools:
if exist "%HERE%tools\runpkr00\runpkr00.exe" (echo   runpkr00.exe        OK) else (echo   runpkr00.exe        MISSING)
if exist "%HERE%tools\rtklib\convbin.exe" (echo   convbin.exe         OK) else (echo   convbin.exe         MISSING)
if exist "%HERE%tools\rtklib\rnx2rtkp.exe" (echo   rnx2rtkp.exe        OK) else (echo   rnx2rtkp.exe        MISSING)
if exist "%HERE%tools\convert_to_rinex\convertToRinex_cli.exe" (echo   convertToRinex_cli  OK) else (echo   convertToRinex_cli  MISSING)
echo.

echo [doctor] Offline wheelhouse:
if exist "%HERE%offline_installer\wheelhouse_cp310" echo   wheelhouse_cp310 present
if exist "%HERE%offline_installer\wheelhouse_cp311" echo   wheelhouse_cp311 present
if exist "%HERE%offline_installer\wheelhouse_cp312" echo   wheelhouse_cp312 present
if exist "%HERE%offline_installer\wheelhouse_cp313" echo   wheelhouse_cp313 present
echo.

echo [doctor] Network check:
if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import urllib.request as u; r=u.urlopen('https://pypi.org/simple/', timeout=10); r.close(); print('  pypi.org reachable')" 2>nul
  if errorlevel 1 echo   pypi.org NOT reachable - use offline install path
)
echo.

echo [doctor] Done. Share install.log with support if dashboard does not start.
pause
endlocal