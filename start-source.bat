@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Run setup-windows.bat first to install the project environment.
  pause
  exit /b 1
)
"%PYTHON%" "%~dp0main.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
