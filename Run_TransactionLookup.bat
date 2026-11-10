@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0python\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: Portable Python not found at:
  echo %PYTHON%
  pause
  exit /b 1
)
"%PYTHON%" TransactionLookup.py
if errorlevel 1 (
  echo.
  echo TransactionLookup ended with an error. Check Results\errors.log.
)
pause
endlocal
