@echo off
REM ---------------------------------------------------------------
REM  MCAP to CSV - Transect Extractor - launcher
REM  Double-click this file to open the app.
REM  Requires Python 3.10+ with the packages in requirements.txt.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM Candidates are tested, not trusted. A machine can carry several Pythons, and
REM a partial install still leaves a python.exe on disk that cannot find its own
REM standard library -- picking that one produces a baffling error about
REM _tkinter rather than an obvious "this Python is broken". So each candidate
REM has to prove it can import tkinter and the packages this tool needs before
REM it is used. What is on PATH is tried first, since that is the Python the
REM user has actually been working with.
set "PY="
set "PY_TK="

for /f "delims=" %%P in ('where pythonw 2^>nul') do call :probe "%%~P"
for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%~P"
call :probe "%LOCALAPPDATA%\anaconda3\pythonw.exe"
call :probe "%USERPROFILE%\anaconda3\pythonw.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
call :probe "C:\Program Files\Python313\pythonw.exe"
call :probe "C:\Program Files\Python312\pythonw.exe"

if not defined PY if defined PY_TK (
  echo Found a working Python, but some required packages are missing:
  echo.
  "%PY_TK%" -c "import mcap, zstandard, pandas, numpy, pytz, geopy, scipy, requests" 2>&1
  echo.
  echo Install them with:
  echo     "%PY_TK%" -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
if not defined PY (
  echo Could not find a working Python 3.10+ with tkinter.
  echo.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo ^(tick "tcl/tk and IDLE" during setup^), then run:
  echo     python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

REM PY is a console python.exe; PYW is its windowed twin where one exists, so
REM the app opens without a console behind it.
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"

"%PYW%" -m ccr_m2c.gui
if errorlevel 1 (
  echo.
  echo The app exited with an error. Running again with the console visible:
  echo.
  "%PY%" -m ccr_m2c.gui
  pause
)
endlocal
exit /b

REM ---------------------------------------------------------------
REM  :probe "<path to python.exe or pythonw.exe>"
REM  Records the first candidate that has tkinter (PY_TK, used only to give a
REM  better error message) and the first that has everything (PY, used to run).
REM  Probing always goes through python.exe: pythonw.exe detaches from the
REM  console, so its exit code cannot be relied on here.
REM ---------------------------------------------------------------
:probe
if defined PY exit /b
set "CAND=%~1"
if "%CAND%"=="" exit /b
set "CAND=%CAND:pythonw.exe=python.exe%"
if not exist "%CAND%" exit /b
"%CAND%" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b
if not defined PY_TK set "PY_TK=%CAND%"
"%CAND%" -c "import mcap, zstandard, pandas, numpy, pytz, geopy, scipy, requests" >nul 2>&1
if errorlevel 1 exit /b
set "PY=%CAND%"
exit /b
