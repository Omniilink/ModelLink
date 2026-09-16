@echo off
title OmniLink Server Update
echo ========================================
echo   OmniLink Server Update
echo ========================================
echo.

cd C:\Modellink

echo [1/3] Pulling latest code...
git pull origin main

echo.
echo [2/3] Updating Python packages...
call venv\Scripts\activate.bat
pip install -r requirements.txt --quiet

echo.
echo [3/3] Restarting server...
taskkill /f /im python.exe 2>nul
timeout /t 2 /nobreak >nul
start /min "OmniLink API" python -m uvicorn modellink.api_server:app --host 127.0.0.1 --port 8080

timeout /t 3 /nobreak >nul
curl -s http://localhost:8080/ >nul 2>&1
if %errorlevel% == 0 (
    echo.
    echo Server is running!
) else (
    echo.
    echo Server may need a moment to start...
)

echo.
echo Restart Cloudflare Tunnel if needed.
echo ========================================
pause
