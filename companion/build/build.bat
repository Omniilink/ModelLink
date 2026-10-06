@echo off
REM Rebuilds OmniilinkCompanion.exe and OmniilinkCompanionSetup.msi.
REM Prereqs: Python 3.10+ with PyInstaller + pystray + Pillow installed,
REM and WiX v3 binaries (candle.exe, light.exe, WixUIExtension.dll).
REM Set WIXBIN to the folder holding them, or drop them in build\wix.
setlocal
cd /d "%~dp0\.."
if errorlevel 1 (
  echo Cannot reach the companion folder.
  pause
  exit /b 1
)
if not defined WIXBIN set WIXBIN=%~dp0wix
if not exist "%WIXBIN%\candle.exe" (
  echo WiX v3 binaries not found in %WIXBIN%.
  echo Download wix311-binaries.zip from github.com/wixtoolset/wix3/releases
  echo and extract it there, then run this again.
  pause
  exit /b 1
)
REM NOTE: PyInstaller must run under the same interpreter the packages were
REM installed for ("python -m pip install ..."). A bare pyinstaller.exe from
REM another install on PATH will silently miss modules (e.g. pystray).
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo PyInstaller not found. Run:  python -m pip install pyinstaller pystray pillow
  pause
  exit /b 1
)
echo === Building OmniilinkServer.exe ===
python -m PyInstaller --noconfirm --noconsole --onefile --name OmniilinkServer --clean ^
  --exclude-module tkinter --icon "%~dp0icon.ico" --version-file "%~dp0version.txt" ^
  --distpath "%~dp0dist" --workpath "%~dp0work" --specpath "%~dp0." companion.py
if errorlevel 1 (
  echo PYINSTALLER FAILED.
  pause
  exit /b 1
)
echo === Building OmniilinkServerSetup.msi ===
"%WIXBIN%\candle.exe" -nologo -arch x64 "-dExeSourceDir=%~dp0dist" "-dIconDir=%~dp0." -o "%~dp0msi\companion.wixobj" "%~dp0companion.wxs"
if errorlevel 1 (
  echo CANDLE FAILED.
  pause
  exit /b 1
)
"%WIXBIN%\light.exe" -nologo -o "%~dp0..\OmniilinkServerSetup.msi" "%~dp0msi\companion.wixobj" -ext "%WIXBIN%\WixUIExtension.dll"
if errorlevel 1 (
  echo LIGHT FAILED.
  pause
  exit /b 1
)
echo.
echo Built %~dp0..\OmniilinkServerSetup.msi
pause
