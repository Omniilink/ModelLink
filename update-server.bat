@echo off
title OmniLink Server Update
echo ========================================
echo   OmniLink Server Update
echo ========================================
echo.

:: Check if we're in the right directory
if not exist "C:\Modellink\run-server.py" (
    echo ERROR: C:\Modellink not found
    pause
    exit /b 1
)

cd C:\Modellink

echo Pulling latest code...
git pull origin main
if errorlevel 1 (
    echo git pull failed. Trying to reset...
    git fetch origin
    git reset --hard origin/main
)

echo.
echo Installing/updating dependencies...
C:\Modellink\venv\Scripts\pip.exe install -r requirements.txt -q

echo.
echo Update complete!
echo.
echo To start the server, run: start.bat
echo.
pause
