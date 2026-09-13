@echo off
REM ---------------------------------------------------------------------------
REM  ROV Flight Operations (working title) - topside network check
REM
REM  Double-click this on the ROV laptop, with the tether connected, BEFORE a
REM  dive. It reports every network adapter, which one carries the tether,
REM  whether that one is a Windows bridge with a real adapter underneath it,
REM  and whether Windows is allowed to power any of them down to save energy.
REM
REM  That last one is the reason this exists as its own file. It is a setting,
REM  not a measurement: no log written during a flight can recover it
REM  afterwards, so it has to be read before.
REM
REM  Pass --measure to also watch the counters for four seconds against a live
REM  tether and report what the sampling actually resolves on this laptop.
REM
REM  The report is printed and written beside this file as network_check.txt,
REM  so it can be sent on.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

set "ENV_ROOT=%LOCALAPPDATA%\CCR_ROV\rov_flight_ops"
set "VENV=%ENV_ROOT%\venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
  echo.
  echo No environment yet. Run run_rov_flight_ops.bat once first -- it builds the
  echo environment this shares.
  echo.
  pause
  exit /b 1
)

"%VPY%" -m rov_flight_ops.netdiag --netcheck "%~dp0network_check.txt" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="2" (
  echo ^>^> NOTHING on this laptop holds a subnet containing the vehicle.
  echo    The tether adapter has no address, or the wrong one.
)
if "%RC%"=="1" (
  echo ^>^> The adapter is there but ARP could not resolve the vehicle.
  echo    Layer 2 is not reaching it -- check the tether and the Fathom-X pair.
)
if "%RC%"=="0" (
  echo ^>^> The vehicle answered. Read the adapter settings above before flying.
)
echo.
pause
exit /b %RC%
