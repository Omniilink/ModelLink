@echo off
title ModelLink - Background Mode
echo Starting ModelLink in background mode...
echo.

:: Activate virtual environment
call C:\Modellink\venv\Scripts\activate.bat

:: Start API server in background
start /min "ModelLink API" python -m uvicorn modellink.api_server:app --host 127.0.0.1 --port 8080

:: Start Cloudflare Tunnel in background
start /min "Cloudflare Tunnel" C:\Modellink\cloudflared.exe tunnel --url http://localhost:8080

timeout /t 5 /nobreak >nul
echo ModelLink is running in the background!
echo Close this window - the server keeps running.
