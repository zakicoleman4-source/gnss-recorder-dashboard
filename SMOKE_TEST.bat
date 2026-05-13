@echo off
setlocal enabledelayedexpansion

set HERE=%~dp0
set VENV=%HERE%.venv

echo [test] GNSS Recorder Dashboard - Smoke Test
echo.

if not exist "%VENV%\Scripts\python.exe" (
  echo [test] ERROR: Not installed. Run INSTALL.bat first.
  pause
  exit /b 1
)

echo [test] Running all tests under tests\...
"%VENV%\Scripts\python.exe" "%HERE%tests\run_all.py"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo [test] ============================================
  echo [test]  All tests pass.
  echo [test] ============================================
) else (
  echo [test] ============================================
  echo [test]  TESTS FAILED ^(exit %RC%^)
  echo [test]  Share output with support.
  echo [test] ============================================
)
echo.
pause
endlocal
exit /b %RC%