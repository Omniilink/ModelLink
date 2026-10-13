@echo off
setlocal
REM ---------------------------------------------------------------------------
REM OmniLink Server - one-step workspace-merge model setup.
REM Run this ONCE on the machine that runs OmniLink Server (needs Ollama).
REM It downloads the small local model used to merge concurrent workspace
REM edits, then tells you the env vars to set.
REM ---------------------------------------------------------------------------
set "MODEL=%OMNIILINK_MERGE_MODEL%"
if "%MODEL%"=="" set "MODEL=qwen2.5-coder:3b"

echo.
echo === OmniLink workspace-merge model setup ===
echo Model: %MODEL%
echo.

where ollama >nul 2>nul
if errorlevel 1 (
  echo [X] Ollama is not installed or not on PATH.
  echo     Install it from https://ollama.com/download/windows then re-run this file.
  echo     (After installing, open a NEW terminal so PATH updates.)
  goto :end
)

echo [1/2] Checking Ollama is running...
ollama list >nul 2>nul
if errorlevel 1 (
  echo     Starting Ollama...
  start "" ollama serve
  timeout /t 3 /nobreak >nul
)

echo [2/2] Pulling %MODEL% ^(about 2 GB, one time^)...
ollama pull %MODEL%
if errorlevel 1 (
  echo [X] Pull failed. Check your internet connection and that "ollama serve" is running, then retry.
  goto :end
)

echo.
echo [OK] Model ready. Confirm with:  ollama list
echo.
echo Next: put these lines in the server's ".env" (next to run-server.py), then restart OmniLink Server:
echo.
echo     OMNIILINK_OLLAMA_URL=http://localhost:11434
echo     OMNIILINK_MERGE_MODEL=%MODEL%
echo     OMNIILINK_MERGE_ENABLED=1
echo.
echo For CPU servers, also set these Windows environment variables and restart Ollama:
echo     OLLAMA_KEEP_ALIVE=30m
echo     OLLAMA_NUM_PARALLEL=1
echo     OLLAMA_MAX_LOADED_MODELS=1
echo.
echo Tip: to keep merges on one of your paired computers instead of the server,
echo open Admin ^> Agents and press "Merge AI: off" next to that computer (it
echo installs the model there automatically).

:end
echo.
pause
endlocal
