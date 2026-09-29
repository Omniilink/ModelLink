@echo off
REM Omniilink ProDesk updater - double-click to update now, then leave the
REM window open: it re-checks origin every 5 seconds and updates itself.
REM Ctrl+C stops the watcher.
setlocal
cd /d "C:\Modellink"
if errorlevel 1 (
  echo Cannot find C:\Modellink - is this running on ProDesk?
  pause
  exit /b 1
)

:watchloop
call :update_once
echo.
echo --- Watching for updates every 5s (Ctrl+C to stop) ---
timeout /t 5 /nobreak >nul 2>&1 || ping -n 6 127.0.0.1 >nul
if errorlevel 1 exit /b 1
goto watchloop

:update_once
git fetch origin >nul 2>&1
set LOCAL=
set REMOTE=
for /f %%i in ('git rev-parse HEAD') do set LOCAL=%%i
for /f %%j in ('git rev-parse origin/main') do set REMOTE=%%j
if "%REMOTE%"=="" (
  echo [%date% %time%] Cannot reach origin - retrying on the next check.
  exit /b 0
)
if "%LOCAL%"=="%REMOTE%" (
  echo [%date% %time%] Up to date %LOCAL:~0,7%.
  exit /b 0
)
echo.
echo === Update found (%LOCAL:~0,7% -^> %REMOTE:~0,7%) - pulling ===
REM The prod checkout doubles as a deploy target (scp'd files + cobaltv18/),
REM so the tree is usually dirty and plain `git pull` would refuse. Snapshot
REM prod-side changes into the stash (see them with `git stash list`) first.
if exist cobaltv18 git -c user.name="update.bat" -c user.email="update@localhost" stash push -m "update.bat snapshot" --include-untracked -- cobaltv18
git -c user.name="update.bat" -c user.email="update@localhost" stash push -m "update.bat snapshot"
git pull --ff-only && goto :pull_ok || goto :pull_fail
:pull_fail
echo GIT PULL FAILED - will retry on the next check.
exit /b 0
:pull_ok
REM NOTE: the pull above rewrites this very file mid-run, which desyncs the
REM reader - the next line read would be garbage. Always jump (goto rescans
REM the file) right after pulling, never read straight on.
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
  echo COMPOSE FAILED - is Docker Desktop running? Will retry on the next check.
  exit /b 0
)
REM Tunnel ingress comes from a mounted file, which compose does not watch:
REM restart it so new routes (e.g. the converter) always go live on update.
docker compose restart tunnel
echo.
echo === Status ===
docker compose ps
echo.
echo Done. Hard-refresh the site with Ctrl+Shift+R.
exit /b 0

:ensure_file
REM %1 = relative file path. If Docker previously created a DIRECTORY here
REM (happens when the file was missing at first start), remove it, then
REM make sure a real (possibly empty) file exists. Never deletes real files.
if exist "%~1\" rmdir "%~1"
if not exist "%~1" type nul > "%~1"
exit /b 0
