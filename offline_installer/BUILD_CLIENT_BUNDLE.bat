@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set REPO=%HERE%..
set OUT=%REPO%\dist\GNSS_Recorder_Dashboard_portable

echo [build] Building portable client bundle...
echo [build] Output: %OUT%
echo [build] This downloads embeddable Python + pip-installs deps. ~1 min.
echo.

set PYEXE=
python --version >nul 2>&1
if not errorlevel 1 set PYEXE=python
if "%PYEXE%"=="" (
  py -3 --version >nul 2>&1
  if not errorlevel 1 set PYEXE=py -3
)
if "%PYEXE%"=="" (
  echo [build] ERROR: Python not found. Install Python 3.11+ first.
  pause
  exit /b 1
)

%PYEXE% "%HERE%BUILD_PORTABLE.py" --out "%OUT%" --zip
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo [build] ============================================
  echo [build]  Bundle ready under: %REPO%\dist\
  echo [build]  Ship the .zip to the client.
  echo [build] ============================================
) else (
  echo [build] BUILD FAILED ^(exit %RC%^)
)
echo.
pause
endlocal