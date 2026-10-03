# Omniilink Companion

Links a Windows computer to your Omniilink server so the web app can run
commands and create/edit files on it. Lives in the system tray: a **green**
dot means connected, **red** means it cannot reach the server, **amber**
means it is not paired yet.

Right-click the tray icon for: Open projects folder · Pair with server · Quit.

## Install (on the computer to link)

1. Install Python 3 from https://www.python.org/downloads/
   (tick **"Add python.exe to PATH"**).
2. Copy this `companion` folder anywhere (e.g. `Documents`).
3. Double-click **`install.bat`**. It installs dependencies, creates
   `Documents\OmniilinkProjects`, starts with Windows, and asks for:
   - the server URL (`https://…`)
   - a pairing code from the web app: **Admin → Agents → + New pairing code**
4. Look for the dot in the system tray (you may need to expand hidden icons).

No admin rights needed. To uninstall: Quit from the tray menu, delete this
folder, and remove `Omniilink Companion.lnk` from the Startup folder.

## What it can touch

Only the projects folder (`Documents\OmniilinkProjects` by default).
Shell commands run with your user account; file operations are jailed to
the projects folder — `..` escapes and absolute paths are rejected.

## Web app side

**Admin → Agents**: paired computers, run commands, and a file manager
(browse, create, edit, delete). Every command is audit-logged.
