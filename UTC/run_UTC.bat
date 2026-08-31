@echo off
REM ---------------------------------------------------------------------------
REM  Underwater Telemetry Compositing (UTC) - launcher
REM
REM  Double-click this file. On the first run it builds a private Python
REM  environment and installs what it needs, which takes a few minutes; after
REM  that it starts straight away.
REM
REM  You need Python 3.10 or newer installed. Nothing else -- no conda
REM  environment, no manual pip, no PATH setup.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

REM The environment lives outside the repo on purpose. This checkout sits in a
REM OneDrive folder, and a virtualenv there would be thousands of files for the
REM sync client to chew through forever. Both launchers share one environment,
REM so whichever runs first does the work.
set "ENV_ROOT=%LOCALAPPDATA%\CCR_ROV"
set "VENV=%ENV_ROOT%\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

REM ---- 1. a working environment already? --------------------------------
if exist "%VPY%" (
  "%VPY%" -c "import utc.gui.app" >nul 2>&1
  if not errorlevel 1 goto run
)

REM ---- 2. find a Python that actually works -----------------------------
REM Candidates are tested, not trusted. A partial install still leaves a
REM python.exe on disk that cannot find its own standard library, and picking
REM it produces a baffling error about _tkinter rather than an obvious "this
REM Python is broken". What is on PATH is tried first.
set "SYS_PY="
for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%~P"
for /f "delims=" %%P in ('where python3 2^>nul') do call :probe "%%~P"
call :probe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :probe "%LOCALAPPDATA%\anaconda3\python.exe"
call :probe "%USERPROFILE%\anaconda3\python.exe"
call :probe "C:\Program Files\Python313\python.exe"
call :probe "C:\Program Files\Python312\python.exe"
call :probe "C:\Program Files\Python311\python.exe"

if not defined SYS_PY (
  echo.
  echo Could not find a working Python 3.10 or newer with tkinter.
  echo.
  echo Install one from https://www.python.org/downloads/ and tick both
  echo   [x] tcl/tk and IDLE
  echo   [x] Add python.exe to PATH
  echo then run this file again.
  echo.
  pause
  exit /b 1
)

REM ---- 3. build the environment ------------------------------------------
echo.
echo First run: setting up a private Python environment.
echo Using: %SYS_PY%
echo Into : %VENV%
echo.
echo This takes a few minutes and happens only once.
echo.
if not exist "%ENV_ROOT%" mkdir "%ENV_ROOT%"
if not exist "%VPY%" (
  "%SYS_PY%" -m venv "%VENV%"
  if errorlevel 1 goto envfail
)
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
echo Installing UTC...
"%VPY%" -m pip install -e . --quiet --disable-pip-version-check
if errorlevel 1 goto envfail

REM The transect extractor is a sibling in this repo and is not on PyPI, so it
REM is installed from the folder. Without it the "Transects" page cannot run.
if exist "..\mcap_to_csv\pyproject.toml" (
  echo Installing the transect extractor...
  "%VPY%" -m pip install -e "..\mcap_to_csv" --quiet --disable-pip-version-check
)
echo.
echo Setup complete.
echo.

REM ---- 4. run -------------------------------------------------------------
:run
"%VPYW%" -m utc.gui.app
if errorlevel 1 (
  echo.
  echo UTC exited with an error. Running again with the console visible:
  echo.
  "%VPY%" -m utc.gui.app
  echo.
  pause
)
endlocal
exit /b

REM ---------------------------------------------------------------------------
REM  :probe "<path to python.exe>"
REM  Accepts the first candidate that is 3.10+ and can import tkinter.
REM ---------------------------------------------------------------------------
:probe
if defined SYS_PY exit /b
if "%~1"=="" exit /b
if not exist "%~1" exit /b
"%~1" -c "import sys, tkinter, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b
set "SYS_PY=%~1"
exit /b

:envfail
echo.
echo Could not build the environment. The output above says why.
echo.
echo A common cause is no network access the first time this is run --
echo the packages have to be downloaded once.
echo.
pause
exit /b 1
