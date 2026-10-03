#!/usr/bin/env python3
"""Omniilink Companion: links this computer to your Omniilink server.

Runs in the Windows system tray (green = connected, red = offline,
amber = not paired yet). The web app can then run shell commands and
create/edit files inside your local projects folder through a pairing code.

Usage:
  OmniilinkCompanion.exe             # setup window on first run, tray after
  OmniilinkCompanion.exe gui        # setup window (server, code, folder)
  OmniilinkCompanion.exe tray       # tray icon only
  python companion.py pair --server https://YOUR-SERVER --code ABC123

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
import urllib.error
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
# Browsers pass through hosting edge filters; bare script user-agents often
# do not (HTTP 403). Identify plainly as the companion, browser-style.
COMPANION_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "OmniilinkCompanion/1.0")


def api(server, method, path, token=None, payload=None):
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": COMPANION_UA})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise Exception(f"HTTP {e.code}: {detail.strip() or e.reason}")


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
        # Reload config every round while unpaired so pairing from the
        # Setup window (separate process) is picked up live.
        while not state["stop"].is_set():
            c = load_config()
            try:
                os.makedirs(c["projects_root"], exist_ok=True)
            except Exception:
                pass
            if not c["token"] or not c["server"]:
                state["ok"] = None
                state["msg"] = "not paired — right-click > Setup…"
                redraw()
                state["stop"].wait(5)
                continue
            sync_loop(c["server"], c["token"], c["projects_root"], state["stop"], status_cb)

    def on_open(icon, item):
        folder = load_config()["projects_root"]
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 — local folder by design
        except Exception:
            pass

    def on_setup(icon, item):
        try:
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable, "gui"])
            else:
                subprocess.Popen([sys.executable, os.path.abspath(__file__), "gui"])
        except Exception:
            pass

    def on_quit(icon, item):
        state["stop"].set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open projects folder", on_open),
        pystray.MenuItem("Setup…", on_setup),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, _dot_icon((217, 160, 33)), APP_NAME + " — starting…", menu)
    state["icon"] = icon
    threading.Thread(target=worker, daemon=True).start()
    icon.run()


# ---------------- setup gui ----------------
def clean_code(s):
    """ABC-123 / abc 123 -> ABC123."""
    import re as _re
    return _re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def normalize_server(s):
    s = (s or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    return s


def run_gui():
    """Setup window: server + pairing code + name + folder, live status."""
    import queue as _queue
    import tkinter as tk
    from tkinter import filedialog
    cfg = load_config()
    root = tk.Tk()
    root.title("Omniilink Companion Setup")
    root.resizable(False, False)
    BG, PANEL, LINE = "#0e0f12", "#101216", "#24282f"
    TEXT, MUTED = "#e8eaed", "#9aa1ab"
    ACCENT, ACCENT_HI = "#2f6fed", "#5b8def"
    root.configure(bg=BG)
    tk.Label(root, text="Omniilink Companion", fg=TEXT, bg=BG,
             font=("Segoe UI", 13, "bold")).grid(
                 row=0, column=0, columnspan=4, padx=14, pady=(14, 0), sticky="w")

    dots = {"color": "#d9a021"}
    dot_canvas = tk.Canvas(root, width=22, height=22, highlightthickness=0, bg=BG)
    dot_canvas.grid(row=1, column=0, padx=(14, 2), pady=(6, 4), sticky="w")

    def draw_dot():
        dot_canvas.delete("all")
        dot_canvas.create_oval(4, 4, 18, 18, fill=dots["color"], outline="")

    status_var = tk.StringVar(value="Not paired yet.")
    tk.Label(root, textvariable=status_var, fg=MUTED, bg=BG,
             font=("Segoe UI", 9)).grid(row=1, column=1, columnspan=2,
                                        padx=(0, 14), pady=(6, 4), sticky="w")

    def field(row, label, width=44):
        tk.Label(root, text=label, fg=MUTED, bg=BG, font=("Segoe UI", 9)).grid(
            row=row, column=0, padx=(14, 6), pady=4, sticky="e")
        var = tk.StringVar()
        ent = tk.Entry(root, textvariable=var, width=width,
                       font=("Segoe UI", 10), bg=PANEL, fg=TEXT,
                       insertbackground=TEXT, relief="flat",
                       highlightthickness=1, highlightbackground=LINE,
                       highlightcolor=ACCENT, bd=0)
        ent.grid(row=row, column=1, columnspan=2, padx=(0, 14), pady=4,
                 sticky="we")
        return var, ent

    server_var, _ = field(2, "Server URL")
    server_var.set(cfg.get("server") or "")
    code_var, code_ent = field(3, "Pairing code")
    try:
        code_ent.configure(font=("Consolas", 13))
    except Exception:
        pass
    name_var, _ = field(4, "Computer name")
    name_var.set(cfg.get("name") or socket.gethostname())
    proj_var, _ = field(5, "Projects folder")
    proj_var.set(cfg.get("projects_root") or DEFAULT_PROJECTS)

    def browse():
        d = filedialog.askdirectory(title="Projects folder")
        if d:
            proj_var.set(d)

    tk.Button(root, text="Browse…", command=browse, bg="#1b1e24", fg=TEXT,
              activebackground="#262b34", activeforeground=TEXT,
              relief="flat", padx=10, pady=3).grid(
        row=5, column=3, padx=(0, 14), pady=4)
    detail_var = tk.StringVar(value="Get a code from the web app: Admin > Agents > + New pairing code.")
    tk.Label(root, textvariable=detail_var, fg=MUTED, bg=BG, wraplength=400,
             justify="left", font=("Segoe UI", 9)).grid(
                 row=6, column=0, columnspan=4, padx=14, pady=(2, 2), sticky="w")

    pair_btn = tk.Button(root, text="Pair this computer", font=("Segoe UI", 11, "bold"),
                         bg=ACCENT, fg="white", activebackground=ACCENT_HI,
                         activeforeground="white", relief="flat", padx=10, pady=6,
                         cursor="hand2")

    def set_status(text, color):
        dots["color"] = color
        status_var.set(text)
        try:
            draw_dot()
        except Exception:
            pass

    def set_busy(busy):
        try:
            pair_btn.configure(state="disabled" if busy else "normal")
            root.configure(cursor="watch" if busy else "")
        except Exception:
            pass

    results = _queue.Queue()

    def poll():
        try:
            kind, text, color = results.get_nowait()
        except Exception:
            root.after(120, poll)
            return
        if kind == "done":
            set_status(text, color)
            set_busy(False)
        else:
            root.after(120, poll)

    def do_pair():
        server = normalize_server(server_var.get())
        code = clean_code(code_var.get())
        name = name_var.get().strip() or socket.gethostname()
        proot = proj_var.get().strip() or DEFAULT_PROJECTS
        if not server or not code:
            results.put(("done", "Enter the server address and the pairing code.", "#e5534b"))
            return
        try:
            aid, token = pair(server, code, name)
        except Exception as e:
            results.put(("done", "Pairing failed: " + str(e), "#e5534b"))
            return
        try:
            c = load_config()
            c.update({"server": server, "token": token, "agent_id": aid,
                      "name": name, "projects_root": proot})
            save_config(c)
            os.makedirs(proot, exist_ok=True)
        except Exception as e:
            results.put(("done", "Paired, but could not save: " + str(e), "#e5534b"))
            return
        results.put(("done", "Paired as agent " + aid + " — the tray dot turns green.", "#34c77b"))

    def on_pair():
        set_busy(True)
        set_status("Pairing…", "#d9a021")
        import threading as _th
        _th.Thread(target=do_pair, daemon=True).start()
        root.after(120, poll)

    pair_btn.configure(command=on_pair)
    pair_btn.grid(row=7, column=0, columnspan=4, padx=14, pady=(6, 4), sticky="we")

    bottom = tk.Frame(root, bg=BG)
    bottom.grid(row=8, column=0, columnspan=4, padx=14, pady=(0, 14), sticky="we")

    def on_open():
        folder = proj_var.get().strip() or DEFAULT_PROJECTS
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # noqa: S606 -- local folder by design
        except Exception:
            pass

    tk.Button(bottom, text="Open projects folder", bg="#1b1e24", fg=TEXT,
              activebackground="#262b34", activeforeground=TEXT,
              relief="flat", padx=10, pady=4,
              command=on_open).pack(side="left")
    tk.Button(bottom, text="Close", bg="#1b1e24", fg=TEXT,
              activebackground="#262b34", activeforeground=TEXT,
              relief="flat", padx=10, pady=4,
              command=root.destroy).pack(side="right")

    if cfg.get("token") and cfg.get("server"):
        set_status("This computer is paired (agent " + str(cfg.get("agent_id") or "?") +
                   "). Close this window — the tray icon handles the rest.", "#34c77b")
    else:
        draw_dot()
    root.mainloop()


def cmd_pair(args):
    server = normalize_server(args.server or input("Server URL (https://…): ").strip())
    code = clean_code(args.code or input("Pairing code from Admin > Agents: ").strip())
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
    sub.add_parser("tray", help="tray icon only")
    sub.add_parser("gui", help="setup window only")
    args = ap.parse_args(argv)
    if args.cmd == "pair":
        if getattr(args, "projects", ""):
            cfg = load_config()
            cfg["projects_root"] = args.projects
            save_config(cfg)
        return cmd_pair(args)
    if args.cmd == "gui":
        run_gui()
        return 0
    if args.cmd == "tray":
        run_tray()
        return 0
    # No subcommand: tray when paired, otherwise setup window + tray behind it
    # so the first run visibly works instead of hiding in the tray.
    cfg = load_config()
    if cfg.get("token") and cfg.get("server"):
        run_tray()
        return 0
    t = threading.Thread(target=run_tray, daemon=False)
    t.start()
    try:
        run_gui()
    finally:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
