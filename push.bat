@echo off
REM Omniilink ProDesk push - commit manual changes made on this machine
REM and push them to GitHub. Secrets and data (.env, *.db, user_data/)
REM are git-ignored and never leave the server.
setlocal EnableDelayedExpansion
cd /d "C:\Modellink"
if errorlevel 1 (
  echo Cannot find C:\Modellink - is this running on ProDesk?
  pause
  exit /b 1
)
echo === What will be pushed ===
git status --porcelain
git diff --stat
echo.
set MSG=
set /p MSG="Commit message (Enter for default): "
if "!MSG!"=="" set MSG=Manual update from ProDesk
set MSG=!MSG:"=!
git add -A
if errorlevel 1 (
  echo GIT ADD FAILED.
  pause
  exit /b 1
)
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "!MSG!"
  if errorlevel 1 (
    echo GIT COMMIT FAILED.
    pause
    exit /b 1
  )
) else (
  echo Nothing to commit - working tree is clean.
  pause
  exit /b 0
)
git push origin main
if errorlevel 1 (
  echo GIT PUSH FAILED - someone else pushed first? Run update.bat to pull, then try again.
  pause
  exit /b 1
)
echo.
echo Pushed !MSG!.
pause
