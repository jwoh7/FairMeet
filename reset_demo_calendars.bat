@echo off
setlocal
chcp 65001 >nul

rem This utility works even when Windows starts it from C:\Windows\System32.
set "FAIRMEET_ROOT=C:\Users\JW\Downloads\fairmeet\fairmeet"
if exist "%~dp0.venv\Scripts\python.exe" set "FAIRMEET_ROOT=%~dp0"
if "%FAIRMEET_ROOT:~-1%"=="\" set "FAIRMEET_ROOT=%FAIRMEET_ROOT:~0,-1%"
set "PYTHON=%FAIRMEET_ROOT%\.venv\Scripts\python.exe"
set "RESET_SCRIPT=%FAIRMEET_ROOT%\scripts\reset_demo_calendars.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON%" (
  echo [ERROR] FairMeet Python was not found:
  echo %PYTHON%
  set "RESULT=1"
  goto :finish
)
if not exist "%RESET_SCRIPT%" (
  echo [ERROR] Demo reset tool was not found:
  echo %RESET_SCRIPT%
  set "RESULT=1"
  goto :finish
)

cd /d "%FAIRMEET_ROOT%"

echo Restoring FairMeet demo calendars for all three Google accounts...
echo Existing events in next week's primary calendars will be cleared first.
"%PYTHON%" "%RESET_SCRIPT%"
if errorlevel 1 (
  echo.
  echo [FAILED] Demo calendar restore did not finish.
  set "RESULT=1"
  goto :finish
)

echo.
echo [DONE] Demo calendar state restored.
echo You can now rehearse FairMeet. No server restart is required.
set "RESULT=0"

:finish
if /I not "%~1"=="--no-pause" pause
exit /b %RESULT%
endlocal
