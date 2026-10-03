@echo off
REM Omniilink Companion installer - run once on the computer you want to link.
REM Installs dependencies, creates the projects folder, enables autostart,
REM then pairs with your server. No admin rights needed.
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Cannot reach this script's own folder.
  pause
  exit /b 1
)
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" during setup^), then run this again.
  pause
  exit /b 1
)
echo === Installing companion dependencies ===
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo PIP INSTALL FAILED - check your connection and try again.
  pause
  exit /b 1
)
set PROJDIR=%USERPROFILE%\Documents\OmniilinkProjects
if not exist "%PROJDIR%" mkdir "%PROJDIR%"
echo Projects folder: %PROJDIR%
echo.
echo === Pairing with your server ===
echo Get a code from the web app: Admin ^> Agents ^> + New pairing code
python companion.py pair
if errorlevel 1 (
  echo PAIRING FAILED - check the server URL and code, then run:
  echo   python companion.py pair
  pause
  exit /b 1
)
echo.
echo === Enable start-with-Windows ===
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Omniilink Companion.lnk\");$s.TargetPath=(Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source;$s.Arguments='\"%~dp0companion.py\"';$s.WorkingDirectory='%~dp0';$s.Save()"
if errorlevel 1 (
  echo Autostart shortcut could not be created - the app still works,
  echo just start companion.py by hand after rebooting.
) else (
  echo Autostart enabled.
)
echo.
echo === Starting the companion (tray icon: green = connected) ===
start "" pythonw.exe "%~dp0companion.py"
echo Done. Look for the dot in the system tray.
pause
