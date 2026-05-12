@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

if not exist "%VENV%\Scripts\python.exe" (
  echo [probe] ERROR: Not installed. Run INSTALL.bat first.
  echo.
  pause
  exit /b 1
)

if not "%~1"=="" (
  set "FOLDER=%~1"
  goto RUN
)

echo [probe] Drag a data folder onto this bat file, or type the path below.
echo.
set /p FOLDER="Data folder path: "

:RUN
if "%FOLDER%"=="" (
  echo [probe] No folder given.
  pause
  exit /b 1
)

echo.
echo [probe] Scanning: %FOLDER%
echo [probe] Output:   %HERE%probe_results.csv
echo.

"%VENV%\Scripts\python.exe" "%HERE%probe_t02_files.py" "%FOLDER%" --out "%HERE%probe_results.csv"

echo.
echo [probe] Done. Open probe_results.csv for full details.
echo.
pause
endlocal