@echo off
REM ---------------------------------------------------------------------------
REM  ROV Flight Operations (working title) - launcher
REM
REM  Double-click this file. On the first run it builds a private Python
REM  environment and installs what it needs, which takes a few minutes and
REM  needs the internet; after that it starts straight away.
REM
REM  You need Python 3.10 or newer installed. Nothing else.
REM
REM  This program has its own environment, separate from UTC's and from ROV
REM  Imagery Processing's, so updating one can never break another.
REM
REM  Switches:
REM    --repair    reinstall into the existing environment, whatever state
REM                it is in
REM    --silent    do not pause, and do not re-run on failure -- just return
REM                the exit code. This is how launch_rov_flight_ops.vbs runs
REM                it for the desktop shortcut, with the output captured to a
REM                log; pausing there would wait forever on a window nobody
REM                can see.
REM    --console   run with the console Python and leave the window open, so
REM                anything the program prints on its way down can be read.
REM                This is the debugging way in.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

set "SILENT="
set "CONSOLE="
set "REPAIR="
for %%A in (%*) do (
  if /i "%%~A"=="--silent" set "SILENT=1"
  if /i "%%~A"=="--console" set "CONSOLE=1"
  if /i "%%~A"=="--repair" set "REPAIR=1"
)

REM Outside the repo on purpose: a virtualenv inside a Dropbox folder is
REM thousands of files for the sync client to chew through forever.
set "ENV_ROOT=%LOCALAPPDATA%\CCR_ROV\rov_flight_ops"
set "VENV=%ENV_ROOT%\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

REM ---- 1. a working environment already? --------------------------------
REM "run_rov_flight_ops.bat --repair" reinstalls into the existing environment
REM whatever state it is in.
REM
REM The check covers the transect extractor too. It is imported only when the
REM Analyze transects tab runs, so an environment missing it would otherwise
REM pass this check forever and never get the install that fixes it.
if defined REPAIR goto setup
if exist "%VPY%" (
  "%VPY%" -c "import rov_flight_ops.gui.app" >nul 2>&1
  if not errorlevel 1 (
    if not exist "..\mcap_to_csv\pyproject.toml" goto run
    "%VPY%" -c "import ccr_m2c" >nul 2>&1
    if not errorlevel 1 goto run
  )
)

:setup

REM ---- 2. find a Python that actually works -----------------------------
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
  if not defined SILENT pause
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
REM constraints.txt pins the dependency versions this was last tested with,
REM so a new laptop does not get whatever happens to be newest that day.
set "PINS="
if exist "constraints.txt" set "PINS=-c constraints.txt"
echo Installing ROV Flight Operations...
"%VPY%" -m pip install -e . %PINS% --quiet --disable-pip-version-check
if errorlevel 1 goto envfail

REM The transect extractor is a sibling in this repo and is not on PyPI. The
REM Analyze transects tab needs it. A failed install stops setup here, rather
REM than leaving an environment that starts but cannot extract transects.
if exist "..\mcap_to_csv\pyproject.toml" (
  echo Installing the transect extractor...
  "%VPY%" -m pip install -e "..\mcap_to_csv" %PINS% --quiet --disable-pip-version-check
  if errorlevel 1 goto envfail
  "%VPY%" -c "import ccr_m2c" >nul 2>&1
  if errorlevel 1 goto envfail
)
echo.
echo Setup complete.
echo.

REM ---- 4. run -------------------------------------------------------------
REM  pythonw has no console, which is what keeps a command prompt off the
REM  taskbar for the whole survey day. It also has nowhere to print to, so
REM  anything that goes wrong before the window exists would vanish -- which
REM  is why diagnostics.setup() runs first inside the program, and why a
REM  failure here is re-run visibly rather than swallowed.
:run
if defined CONSOLE goto runconsole
"%VPYW%" -m rov_flight_ops
if not errorlevel 1 goto done
if defined SILENT (
  echo.
  echo ROV Flight Operations exited with an error. Run
  echo   run_rov_flight_ops.bat --console
  echo to see what it printed, and look in the diagnostics folder:
  echo   %%LOCALAPPDATA%%\CCR_ROV\rov_flight_ops\diagnostics
  exit /b 1
)
echo.
echo ROV Flight Operations exited with an error. Running again with the
echo console visible:
echo.

:runconsole
"%VPY%" -m rov_flight_ops
set "CODE=%ERRORLEVEL%"
if not defined SILENT (
  echo.
  pause
)
endlocal & exit /b %CODE%

:done
endlocal
exit /b 0

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
if not defined SILENT pause
exit /b 1
