# Omniilink Companion

Links a Windows computer to your Omniilink server so the web app can run
commands and create/edit files on it. Lives in the system tray: a **green**
dot means connected, **red** means it cannot reach the server, **amber**
means it is not paired yet.

Right-click the tray icon for: Open projects folder · Pair with server · Quit.

## Install (on the computer to link)

1. In the web app, go to **Admin → Agents** and download
   **Companion setup (.msi)**.
2. Run it (asks for admin once — it installs to Program Files and enables
   start-with-Windows automatically).
3. Get a code from **Admin → Agents → + New pairing code**, then either:
   - right-click the tray icon → **Pair with server…**, or
   - run `"C:\Program Files\Omniilink Companion\OmniilinkCompanion.exe" pair --server https://YOUR-SERVER --code XXXXXX`
4. Look for the green dot in the system tray (you may need to expand hidden icons).

To uninstall: Windows Settings → Apps → Omniilink Companion → Uninstall.

## Manual install (no admin rights)

Run `install.bat` from this folder instead: it uses plain Python (no EXE),
puts projects in `Documents\OmniilinkProjects`, and sets up autostart for
your user only.

## What it can touch

Only the projects folder (`Documents\OmniilinkProjects` by default).
Shell commands run with your user account; file operations are jailed to
the projects folder — `..` escapes and absolute paths are rejected.

## Web app side

**Admin → Agents**: paired computers, run commands, and a file manager
(browse, create, edit, delete). Every command is audit-logged.

## Rebuilding the installer

See `build/`: `build.bat` re-freezes the EXE (PyInstaller) and rebuilds the
MSI (WiX v3 `candle`/`light`). The MSI in this folder is what the server
hands out on Admin → Agents.
