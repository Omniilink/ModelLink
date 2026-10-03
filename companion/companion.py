#!/usr/bin/env python3
"""Omniilink Companion: links this computer to your Omniilink server.

Runs in the Windows system tray (green = connected, red = offline,
amber = not paired yet). The web app can then run shell commands and
create/edit files inside your local projects folder through a pairing code.

Usage:
  python companion.py pair --server https://YOUR-SERVER --code ABC123
  python companion.py            # tray icon + background loop

Needs only the standard library to pair and sync; the tray icon needs
`pip install -r requirements.txt` (pystray + Pillow).
"""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

APP_NAME = "Omniilink Companion"
CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OmniilinkCompanion")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_PROJECTS = os.path.join(os.path.expanduser("~"), "Documents", "OmniilinkProjects")
POLL_SECONDS = 5
CMD_TIMEOUT = 300
READ_MAX = 256 * 1024
WRITE_MAX = 1024 * 1024


# ---------------- config ----------------
def default_config():
    return {"server": "", "token": "", "agent_id": "",
            "name": socket.gethostname(), "projects_root": DEFAULT_PROJECTS}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in cfg:
                if isinstance(data.get(k), str) and data[k]:
                    cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------- server api ----------------
def api(server, method, path, token=None, payload=None):
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=body, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def pair(server, code, name):
    """Claim a pairing code; returns (agent_id, token)."""
    d = api(server, "POST", "/api/agent/claim", None,
            {"code": code, "name": name, "hostname": socket.gethostname(),
             "platform": platform.system() + " " + platform.release()})
    return d["agent_id"], d["token"]


# ---------------- file jail ----------------
def safe_join(root, rel):
    """Resolve a project-relative path, rejecting escapes outside root."""
    rel = (rel or "").replace("\\", "/").strip()
    if not rel:
        return os.path.realpath(root)
    if rel.startswith("/") or rel.startswith("~"):
        raise ValueError("absolute paths are not allowed")
    full = os.path.realpath(os.path.join(root, rel))
    root_r = os.path.realpath(root)
    if full != root_r and not full.startswith(root_r + os.sep):
        raise ValueError("path escapes the projects folder")
    return full


def do_file_op(root, payload):
    """Execute one file op inside the jailed projects folder."""
    op = (payload.get("op") or "").strip().lower()
    try:
        if op == "list":
            full = safe_join(root, payload.get("path", ""))
            if not os.path.isdir(full):
                return {"ok": False, "error": "not a folder"}
            entries = []
            with os.scandir(full) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        entries.append({"name": e.name,
                                        "type": "dir" if e.is_dir(follow_symlinks=False) else "file",
                                        "size": st.st_size, "mtime": st.st_mtime})
                    except OSError:
                        continue
            entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
            return {"ok": True, "entries": entries}
        if op == "read":
            full = safe_join(root, payload.get("path", ""))
            if not os.path.isfile(full):
                return {"ok": False, "error": "not a file"}
            if os.path.getsize(full) > READ_MAX:
                return {"ok": False, "error": "file too large to preview"}
            try:
                with open(full, "r", encoding="utf-8") as f:
                    return {"ok": True, "content": f.read()}
            except UnicodeDecodeError:
                return {"ok": False, "error": "binary file cannot be edited as text"}
        if op == "write":
            content = payload.get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "error": "content must be text"}
            if len(content.encode("utf-8")) > WRITE_MAX:
                return {"ok": False, "error": "content over 1MB"}
            full = safe_join(root, payload.get("path", ""))
            if not payload.get("path"):
                return {"ok": False, "error": "missing path"}
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return {"ok": True, "bytes": len(content.encode("utf-8"))}
        if op == "mkdir":
            if not payload.get("path"):
                return {"ok": False, "error": "missing path"}
            os.makedirs(safe_join(root, payload.get("path", "")), exist_ok=True)
            return {"ok": True}
        if op == "delete":
            full = safe_join(root, payload.get("path", ""))
            if not payload.get("path") or full == os.path.realpath(root):
                return {"ok": False, "error": "refusing to delete the projects folder"}
            if os.path.isdir(full) and not os.path.islink(full):
                os.rmdir(full)  # only empty folders, by design
            elif os.path.isfile(full) or os.path.islink(full):
                os.remove(full)
            else:
                return {"ok": False, "error": "not found"}
            return {"ok": True}
        return {"ok": False, "error": "unknown file op"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def do_shell(cmd, timeout=CMD_TIMEOUT):
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-65536:]
    except subprocess.TimeoutExpired:
        return 124, "[timed out]"
    except Exception as e:
        return 1, "agent error: " + str(e)


# ---------------- sync loop ----------------
def poll_once(server, token, root, status_cb=None):
    """One heartbeat: fetch a queued command, run it, post the result.
    Returns 'idle' | 'ran' | 'error'."""
    try:
        p = api("GET", "/api/agent/pending", token)
    except Exception as e:
        if status_cb:
            status_cb(False, str(e))
        return "error"
    cmd = p.get("command")
    if not cmd:
        if status_cb:
            status_cb(True, "")
        return "idle"
    kind = cmd.get("kind") or "shell"
    try:
        if kind == "file":
            res = do_file_op(root, cmd.get("payload") or {})
            code = 0 if res.get("ok") else 1
            out = json.dumps(res)
        else:
            code, out = do_shell(cmd.get("command") or "", int(cmd.get("timeout") or CMD_TIMEOUT))
        api("POST", "/api/agent/result", token,
            {"command_id": cmd["id"], "exit_code": code, "output": out[-65536:]})
    except Exception as e:
        try:
            api("POST", "/api/agent/result", token,
                {"command_id": cmd.get("id"), "exit_code": 1,
                 "output": "agent error: " + str(e)})
        except Exception:
            pass
    if status_cb:
        status_cb(True, "")
    return "ran"


def sync_loop(server, token, root, stop, status_cb=None):
    while not stop.is_set():
        try:
            poll_once(server, token, root, status_cb)
        except Exception:
            pass
        stop.wait(POLL_SECONDS)


# ---------------- tray ----------------
def _dot_icon(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, 56, 56], fill=color + (255,), outline=(255, 255, 255, 220), width=4)
    return img


def run_tray():
    """System-tray icon with green/red/amber connection indicator."""
    import pystray
    cfg = load_config()
    os.makedirs(cfg["projects_root"], exist_ok=True)
    state = {"icon": None, "ok": None, "msg": "starting…", "stop": threading.Event()}

    def redraw():
        color = (52, 199, 123) if state["ok"] is True else ((217, 160, 33) if state["ok"] is None else (229, 83, 75))
        if state["icon"] is not None:
            state["icon"].icon = _dot_icon(color)
            state["icon"].title = APP_NAME + " — " + state["msg"]

    def status_cb(ok, msg):
        state["ok"] = bool(ok)
        state["msg"] = "connected" if ok and not msg else ("error: " + msg if msg else "connected")
        redraw()

    def worker():
        c = load_config()
        if not c["token"] or not c["server"]:
            state["ok"] = None
            state["msg"] = "not paired — right-click > Pair…"
            redraw()
            state["stop"].wait()
            return
        os.makedirs(c["projects_root"], exist_ok=True)
        sync_loop(c["server"], c["token"], c["projects_root"], state["stop"], status_cb)

    def on_open(icon, item):
        folder = load_config()["projects_root"]
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 — local folder by design
        except Exception:
            pass

    def on_pair(icon, item):
        try:
            import tkinter
            from tkinter import simpledialog
            root = tkinter.Tk()
            root.withdraw()
            server = simpledialog.askstring(APP_NAME, "Server URL (https://…):") or ""
            code = simpledialog.askstring(APP_NAME, "Pairing code from Admin > Agents:") or ""
            root.destroy()
        except Exception:
            return
        if not server.strip() or not code.strip():
            return
        try:
            aid, token = pair(server.strip(), code.strip(),
                              load_config()["name"])
            c = load_config()
            c.update({"server": server.strip(), "token": token, "agent_id": aid})
            save_config(c)
            state["stop"].clear()
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            state["ok"] = False
            state["msg"] = "pairing failed"
            redraw()
            try:
                import tkinter.messagebox as mb
                r = tkinter.Tk()
                r.withdraw()
                mb.showerror(APP_NAME, "Pairing failed:\n" + str(e))
                r.destroy()
            except Exception:
                pass

    def on_quit(icon, item):
        state["stop"].set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open projects folder", on_open),
        pystray.MenuItem("Pair with server…", on_pair),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, _dot_icon((217, 160, 33)), APP_NAME + " — starting…", menu)
    state["icon"] = icon
    threading.Thread(target=worker, daemon=True).start()
    icon.run()


def cmd_pair(args):
    server = args.server or input("Server URL (https://…): ").strip()
    code = args.code or input("Pairing code from Admin > Agents: ").strip()
    if not server or not code:
        print("need both --server and --code")
        return 2
    try:
        aid, token = pair(server, code, args.name or socket.gethostname())
    except Exception as e:
        print("pairing failed:", e)
        return 1
    cfg = load_config()
    cfg.update({"server": server, "token": token, "agent_id": aid,
                "name": args.name or socket.gethostname()})
    save_config(cfg)
    os.makedirs(cfg["projects_root"], exist_ok=True)
    print("paired as agent", aid, "- projects folder:", cfg["projects_root"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_NAME)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pair", help="pair with a server using a code")
    p.add_argument("--server", default="")
    p.add_argument("--code", default="")
    p.add_argument("--name", default="")
    p.add_argument("--projects", default="",
                   help="projects folder (default: Documents/OmniilinkProjects)")
    args = ap.parse_args(argv)
    if args.cmd == "pair":
        if getattr(args, "projects", ""):
            cfg = load_config()
            cfg["projects_root"] = args.projects
            save_config(cfg)
        return cmd_pair(args)
    run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
