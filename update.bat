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
echo === Rebuilding and restarting containers ===
docker compose up -d --build
if errorlevel 1 (
  echo COMPOSE FAILED - is Docker Desktop running?
  pause
  exit /b 1
)
echo.
echo === Status ===
docker compose ps
echo.
echo Done. Hard-refresh the site with Ctrl+Shift+R.
pause
