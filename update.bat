@echo off
setlocal
cd /d "%~dp0"
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%b
echo Updating %BRANCH% from origin...
git fetch origin %BRANCH%
if errorlevel 1 (echo Fetch failed; pause; exit /b 1)
git reset --hard origin/%BRANCH%
if errorlevel 1 (echo Reset failed; pause; exit /b 1)
echo Done.
pause
