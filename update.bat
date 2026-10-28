@echo off
setlocal
cd /d "%~dp0"
git fetch origin main
if errorlevel 1 (echo Fetch failed; pause; exit /b 1)
git reset --hard origin/main
if errorlevel 1 (echo Reset failed; pause; exit /b 1)
echo Updated to main.
pause
