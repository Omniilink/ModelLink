@echo off
title ModelLink Server
echo ========================================
echo   ModelLink Server
echo ========================================
echo.

:: Activate virtual environment
call C:\Modellink\venv\Scripts\activate.bat

:: Start the API server
echo Starting ModelLink API server...
start /min "ModelLink API" python -m uvicorn modellink.api_server:app --host 127.0.0.1 --port 8080
timeout /t 3 /nobreak >nul

:: Check if server started
curl -s http://localhost:8080/ >nul 2>&1
if %errorlevel% == 0 (
    echo API server is running on http://localhost:8080
) else (
    echo API server may have failed to start. Check for errors.
)

echo.
echo ========================================
echo   Starting Cloudflare Tunnel...
echo ========================================
echo.
echo If this is your first time:
echo   1. A browser window will open
echo   2. Log in to your Cloudflare account
echo   3. Select your domain (or create a free one)
echo   4. Come back here and wait for the URL
echo.

C:\Modellink\cloudflared.exe tunnel --url http://localhost:8080

pause
