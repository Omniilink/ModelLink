@echo off
REM Omniilink log wipe - clears ALL server-side chat/coding history for ALL users.
REM   - usage_events table (every chat + coding request record)
REM   - modellink\server.log (terminal history)
REM   - network_usage.json (bandwidth stats, recreated empty)
REM Never touched: users, sessions, API keys, provider keys, projects, files.
REM NOTE: message TEXT lives in each browser's own storage; clear chats
REM inside the app (trash icons) to remove those per device.
setlocal
cd /d "C:\Modellink"
if errorlevel 1 (
  echo Cannot find C:\Modellink - is this running on ProDesk?
  pause
  exit /b 1
)
echo WARNING: this deletes ALL usage history, terminal logs and network
echo stats for EVERY user on this server. Accounts, keys and files stay.
echo.
set /p CONFIRM="Type CLEAR in capitals to continue: "
if /i not "%CONFIRM%"=="CLEAR" (
  echo Cancelled. Nothing was deleted.
  pause
  exit /b 1
)
echo.
echo --- Deleting usage records for all users ---
py -c "from modellink.auth import get_db; c=get_db(); n=c.execute('DELETE FROM usage_events').rowcount; c.execute(\"DELETE FROM sqlite_sequence WHERE name='usage_events'\"); c.commit(); c.close(); print(f'usage events deleted: {n}')"
if errorlevel 1 (
  echo FAILED to clear usage events - is Python working? Try: py --version
  pause
  exit /b 1
)
echo.
echo --- Clearing terminal log ---
if exist "modellink\server.log" (
  del /f /q "modellink\server.log"
  echo server.log deleted.
) else (
  echo server.log not present - nothing to do.
)
echo.
echo --- Resetting network stats ---
if exist "network_usage.json" (
  del /f /q "network_usage.json"
  echo network_usage.json deleted - recreated empty on next request.
) else (
  echo network_usage.json not present - nothing to do.
)
echo.
echo Done. Restart the server so the in-memory log buffer clears too:
echo   native: Ctrl+C, then py run-server.py
echo   docker: docker compose restart api
echo Remember: message text in each browser must be cleared inside the app.
pause
