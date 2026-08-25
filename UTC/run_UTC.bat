@echo off
REM ---------------------------------------------------------------
REM  Underwater Telemetry Compositing (UTC) - launcher
REM  Double-click this file to open the app.
REM  Requires Python 3.10+ with the packages in requirements.txt.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "C:\Program Files\Python313\pythonw.exe"
  "C:\Program Files\Python312\pythonw.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY (
  for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PY set "PY=%%P"
)
if not defined PY (
  echo Could not find Python. Install Python 3.10+ and run:
  echo     python -m pip install -r requirements.txt
  pause
  exit /b 1
)

"%PY%" -m utc.gui.app
if errorlevel 1 (
  echo.
  echo The app exited with an error. Running again with the console visible:
  echo.
  "%PY:pythonw.exe=python.exe%" -m utc.gui.app
  pause
)
endlocal
