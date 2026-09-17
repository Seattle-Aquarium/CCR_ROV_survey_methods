@echo off
REM ---------------------------------------------------------------------------
REM  Puts "ROV Flight Operations" on this user's desktop.
REM
REM  Double-click it once. Run it again whenever the repository is moved or
REM  renamed -- it overwrites the shortcut it made last time rather than adding
REM  a second one, so running it twice is the same as running it once.
REM
REM  It needs no administrator rights: everything it touches belongs to the
REM  user running it. Nothing is installed and nothing outside the desktop is
REM  changed.
REM
REM  Three things here are easy to get wrong on Windows and are done properly:
REM
REM    * The repository is found from this file's own location (%~dp0), never
REM      written down. A copy of the repository anywhere -- including one with
REM      spaces in its path, which every Dropbox and OneDrive folder has --
REM      makes a shortcut that points at itself.
REM    * The desktop is asked for rather than assumed. On a machine where
REM      OneDrive has taken the Desktop folder over, "%USERPROFILE%\Desktop"
REM      is an empty folder nobody looks at; the real one is in the shell's
REM      own list of special folders, which is what is read here.
REM    * The shortcut points at the .vbs, so a normal launch has no command
REM      prompt behind it. See launch_rov_flight_ops.vbs for what that does
REM      with a failure.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "TARGET=%HERE%\launch_rov_flight_ops.vbs"
set "ICON=%HERE%\assets\rov_flight_ops.ico"
set "NAME=ROV Flight Operations"

if not exist "%TARGET%" (
  echo.
  echo Could not find:
  echo   %TARGET%
  echo.
  echo This file has to stay in the rov_flight_ops folder of the repository,
  echo beside run_rov_flight_ops.bat.
  echo.
  pause
  exit /b 1
)

echo.
echo Creating a desktop shortcut for %NAME%
echo   from : %HERE%
echo.

REM PowerShell is used for the shortcut itself because the COM object that
REM writes a .lnk has no command-line equivalent. -NoProfile so somebody's
REM profile script cannot change what this does; -ExecutionPolicy Bypass
REM because the command is passed inline rather than run from a file, and the
REM default policy on a fresh Windows blocks the latter.
REM
REM The paths go across in environment variables rather than being pasted into
REM the command text. Pasting works right up until a path contains a quote --
REM and "C:\Users\O'Brien\..." is a perfectly ordinary Windows path.
set "CCR_TARGET=%TARGET%"
set "CCR_HOME=%HERE%"
set "CCR_ICON=%ICON%"
set "CCR_NAME=%NAME%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "if ([string]::IsNullOrWhiteSpace($desktop)) { $desktop = Join-Path $env:USERPROFILE 'Desktop' };" ^
  "if (-not (Test-Path -LiteralPath $desktop)) { New-Item -ItemType Directory -Path $desktop -Force | Out-Null };" ^
  "$link = Join-Path $desktop ($env:CCR_NAME + '.lnk');" ^
  "$shell = New-Object -ComObject WScript.Shell;" ^
  "$s = $shell.CreateShortcut($link);" ^
  "$s.TargetPath = $env:CCR_TARGET;" ^
  "$s.WorkingDirectory = $env:CCR_HOME;" ^
  "$s.Description = 'ROV connectivity, live monitoring, flight logs and BlueOS file management';" ^
  "if (Test-Path -LiteralPath $env:CCR_ICON) { $s.IconLocation = ($env:CCR_ICON + ',0') };" ^
  "$s.Save();" ^
  "Write-Host ('  desktop : ' + $desktop);" ^
  "Write-Host ('  shortcut: ' + $link)"

if errorlevel 1 (
  echo.
  echo The shortcut could not be created. The message above says why.
  echo.
  echo If PowerShell is blocked on this machine, the same thing can be done by
  echo hand: right-click the desktop, New ^> Shortcut, and give it
  echo   %TARGET%
  echo.
  pause
  exit /b 1
)

echo.
echo Done. Before it will start, the program needs its dependencies installed,
echo which happens by itself the first time it runs -- that first run takes a
echo few minutes and needs the internet.
echo.
pause
endlocal
exit /b 0
