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
if not defined WATCH_STARTED set FORCE_UPDATE=1
set WATCH_STARTED=1
REM NOTE: :update_once may rewrite this very file (git pull), which desyncs
REM the reader - resuming on the next line would execute garbage (this once
REM stuck FORCE_UPDATE=1 on and rebuilt every 5s). Chain the jump on the
REM SAME line as the call: it is parsed before the mutation runs, and goto
REM rescans the file, so execution always resumes at fresh offsets.
call :update_once && goto :watch_cont || goto :watch_cont
:watch_cont
set FORCE_UPDATE=
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
  if not "%FORCE_UPDATE%"=="1" exit /b 0
  echo First launch - rebuilding all containers anyway.
  goto :rebuild
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
:rebuild
echo.
echo === Checking mounted files ===
call :ensure_file "network_usage.json"
call :ensure_file "bw_usage.json"
call :ensure_file "omnilink.db"
call :ensure_file "tunnel.log"
call :ensure_file ".env"
call :ensure_file "modellink\server.log"
echo.
echo === Rebuilding and restarting containers ===
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set GIT_SHA=%%i
if defined GIT_SHA echo Build marker: %GIT_SHA%
call :compose_up
if errorlevel 1 (
  echo COMPOSE FAILED - nothing was changed. Will retry on the next check.
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

REM ---------------------------------------------------------------------------
REM compose_up: bring the stack up, retrying on the ONE failure that is
REM genuinely transient: Docker's internal DNS failing to resolve
REM registry-1.docker.io while resolving base-image metadata.
REM
REM Symptom seen in the wild:
REM   ERROR [sandbox-image internal] load metadata for
REM     docker.io/library/python:3.12-slim: failed to do request:
REM     Head "https://registry-1.docker.io/...": dialing registry-1.docker.io:443:
REM     dial tcp: lookup registry-1.docker.io:443: no such host
REM
REM That is Docker Desktop's own resolver (not the host's - the host resolves
REM the name fine), and it settles on its own within seconds. Treating it as
REM fatal reported a false "is Docker Desktop running?" and skipped the deploy
REM until the next 5s poll, which often failed identically. So: distinguish
REM it from a real build error, retry with backoff, and only then give up.
REM ---------------------------------------------------------------------------
:compose_up
setlocal EnableDelayedExpansion
set "LOG=%TEMP%\omni-compose.log"
set /a ATTEMPT=0

:compose_attempt
set /a ATTEMPT+=1
if !ATTEMPT! GTR 3 (
  endlocal & exit /b 1
)
if !ATTEMPT! GTR 1 (
  echo.
  echo     Registry DNS was unreachable - retrying !ATTEMPT! of 3 in 6s...
  ping -n 7 127.0.0.1 >nul
)
REM Capture output so we can tell a DNS hiccup from a genuine build failure.
docker compose up -d --build >"%LOG%" 2>&1
if not errorlevel 1 (
  type "%LOG%"
  endlocal & exit /b 0
)

REM Classify the failure from the captured log.
set "IS_DNS="
findstr /i /c:"no such host" "%LOG%" >nul && set "IS_DNS=1"
findstr /i /c:"dial tcp: lookup" "%LOG%" >nul && set "IS_DNS=1"
findstr /i /c:"failed to do request" "%LOG%" >nul && set "IS_DNS=1"
findstr /i /c:"i/o timeout" "%LOG%" >nul && set "IS_DNS=1"

if defined IS_DNS (
  goto compose_attempt
)

echo Build failed. Full output:
type "%LOG%"
echo.
echo --- If this is a registry/DNS problem: Docker Desktop ^> Settings ^> Docker
echo ---   Engine, add  "dns": ["1.1.1.1","8.8.8.8"]  to daemon.json, then
echo ---   "Apply ^& Restart".
echo --- If Docker Desktop itself is down, start it and wait for the whale
echo ---   icon to settle, then run this file again.
endlocal & exit /b 1

:ensure_file
REM %1 = relative file path. If Docker previously created a DIRECTORY here
REM (happens when the file was missing at first start), remove it, then
REM make sure a real (possibly empty) file exists. Never deletes real files.
if exist "%~1\" rmdir "%~1"
if not exist "%~1" type nul > "%~1"
exit /b 0
