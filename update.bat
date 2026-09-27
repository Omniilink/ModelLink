@echo off
REM Omniilink ProDesk updater - double-click to pull latest code and restart.
setlocal
cd /d "C:\Modellink"
if errorlevel 1 (
  echo Cannot find C:\Modellink - is this running on ProDesk?
  pause
  exit /b 1
)
echo === Pulling latest code ===
git pull
if errorlevel 1 (
  echo GIT PULL FAILED - fix the error above and try again.
  pause
  exit /b 1
)
echo.
echo === Checking mounted files ===
call :ensure_file "network_usage.json"
call :ensure_file "omnilink.db"
call :ensure_file "tunnel.log"
call :ensure_file ".env"
call :ensure_file "modellink\server.log"
echo.
echo === Rebuilding and restarting containers ===
docker compose up -d --build
if errorlevel 1 (
  echo COMPOSE FAILED - is Docker Desktop running?
  pause
  exit /b 1
)
REM Tunnel ingress comes from a mounted file, which compose does not watch:
REM restart it so new routes (e.g. the converter) always go live on update.
docker compose restart tunnel
echo.
echo === Status ===
docker compose ps
echo.
echo Done. Hard-refresh the site with Ctrl+Shift+R.
pause
exit /b 0

:ensure_file
REM %1 = relative file path. If Docker previously created a DIRECTORY here
REM (happens when the file was missing at first start), remove it, then
REM make sure a real (possibly empty) file exists. Never deletes real files.
if exist "%~1\" rmdir "%~1"
if not exist "%~1" type nul > "%~1"
exit /b 0
