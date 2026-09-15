@echo off
cd /d "%USERPROFILE%\Desktop\Modellink"
python -m uvicorn modellink.api_server:app --host 127.0.0.1 --port 8080 --reload
pause
