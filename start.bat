@echo off
title OmniLink Server
echo ========================================
echo   OmniLink Server
echo ========================================
echo.
cd C:\Modellink
call venv\Scripts\activate.bat
python run-server.py
pause
