@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

:: Single-click entry. Installs if needed, then launches dashboard.

if not exist "%VENV%\Scripts\python.exe" goto NEED_INSTALL
"%VENV%\Scripts\python.exe" -c "import streamlit" >nul 2>&1
if errorlevel 1 goto NEED_INSTALL
goto LAUNCH

:NEED_INSTALL
echo [gnss] Setup not complete -- running installer first.
echo.
call "%HERE%INSTALL.bat"
if errorlevel 1 (
  echo.
  echo [gnss] ERROR: install failed. See install.log.
  pause
  exit /b 1
)
echo.

:LAUNCH
call "%HERE%RUN_DASHBOARD.bat"
endlocal