#!/usr/bin/env python3
"""Omniilink Server: links this computer to your Omniilink server.

Runs in the Windows system tray (green = connected, red = offline,
amber = not paired yet). The web app can then run shell commands and
create/edit files inside your local projects folder through a pairing code.

Usage:
  OmniilinkServer.exe                # setup page on first run, tray after
  OmniilinkServer.exe gui           # setup page (server, code, folder)
  OmniilinkServer.exe tray          # tray icon only
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

APP_NAME = "Omniilink Server"
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
                "OmniilinkServer/1.0")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}" if body else f"HTTP {status}")
        self.status = status
        self.body = body


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
        try:
            parsed = json.loads(detail or "{}")
            detail = parsed.get("error", "") or detail
        except Exception:
            pass
        raise ApiError(e.code, (detail or e.reason or "").strip())


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
    Returns 'idle' | 'ran' | 'error' | 'unauthorized' (dead/removed token —
    the caller should reload config instead of retrying blindly)."""
    try:
        p = api(server, "GET", "/api/agent/pending", token)
    except ApiError as e:
        if status_cb:
            status_cb(False, str(e))
        return "unauthorized" if e.status == 401 else "error"
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
        api(server, "POST", "/api/agent/result", token,
            {"command_id": cmd["id"], "exit_code": code, "output": out[-65536:]})
    except Exception as e:
        try:
            api(server, "POST", "/api/agent/result", token,
                {"command_id": cmd.get("id"), "exit_code": 1,
                 "output": "agent error: " + str(e)})
        except Exception:
            pass
    if status_cb:
        status_cb(True, "")
    return "ran"


def sync_loop(server, token, root, stop, status_cb=None):
    """Poll until stopped. Returns 'unauthorized' when the token is dead so
    the worker reloads config (re-pairing heals); 'ok' when stopped."""
    while not stop.is_set():
        try:
            res = poll_once(server, token, root, status_cb)
        except Exception:
            res = "error"
        if res == "unauthorized":
            return "unauthorized"
        if res != "error":
            # Only successful rounds count as activity: errors must not
            # masquerade as syncing anywhere status is shown.
            _touch_poll()
        stop.wait(POLL_SECONDS)
    return "ok"


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
    try:
        start_setup_server(SETUP_PORT)
    except OSError:
        pass

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
            res = sync_loop(c["server"], c["token"], c["projects_root"], state["stop"], status_cb)
            if res == "unauthorized" and not state["stop"].is_set():
                # Dead token (agent removed, or re-paired elsewhere): back off
                # so we don't hammer, then reload config — a fresh pairing
                # heals automatically without restarting.
                state["stop"].wait(30)

    def on_open(icon, item):
        folder = load_config()["projects_root"]
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 — local folder by design
        except Exception:
            pass

    def on_setup(icon, item):
        _ensure_setup_page(False)

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


SETUP_PORT = 53681
LASTPOLL_NAME = "lastpoll"
PORTFILE_NAME = "setup-port"
_tray_lock_fd = None

import http.server as _http_server


def _touch_poll():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(os.path.join(CONFIG_DIR, LASTPOLL_NAME), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _poll_age():
    try:
        with open(os.path.join(CONFIG_DIR, LASTPOLL_NAME)) as f:
            return time.time() - float(f.read().strip())
    except Exception:
        return None


def _write_port_file(port):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(os.path.join(CONFIG_DIR, PORTFILE_NAME), "w") as f:
            f.write(str(int(port)))
    except Exception:
        pass


def _read_port_file():
    try:
        with open(os.path.join(CONFIG_DIR, PORTFILE_NAME)) as f:
            return int(f.read().strip())
    except Exception:
        return SETUP_PORT


def tray_lock():
    """Single-instance guard for the tray/polling loop. True when acquired.

    Time-based only: no pid signals (unreliable across platforms and
    privilege levels). A lock older than 60s is treated as stale and
    reclaimed; anything fresher means a live tray, so stand down."""
    global _tray_lock_fd
    path = os.path.join(CONFIG_DIR, "tray.lock")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except Exception:
        pass

    def _try():
        global _tray_lock_fd
        try:
            _tray_lock_fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            try:
                os.write(_tray_lock_fd, ("%d %d" % (os.getpid(), int(time.time()))).encode())
            except Exception:
                pass
            return True
        except FileExistsError:
            return None
        except OSError:
            return False

    r = _try()
    if r is not None:
        return r
    try:
        with open(path) as f:
            parts = (f.read() or "").split()
        age = time.time() - float(parts[1]) if len(parts) > 1 else 1e9
    except Exception:
        age = 1e9
    if age < 60:
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return bool(_try())


SETUP_CSS = ("*{box-sizing:border-box;margin:0;padding:0}"
 "body{background:#0e0f12;color:#e8eaed;font-family:-apple-system,BlinkMacSystemFont,"
 "\"Segoe UI\",Inter,Roboto,sans-serif;min-height:100vh;display:flex;"
 "align-items:flex-start;justify-content:center;padding:48px 20px;-webkit-font-smoothing:antialiased}"
 ".card{background:#14161a;border:1px solid #24282f;border-radius:12px;"
 "padding:28px;width:100%;max-width:460px}"
 ".brand{font-size:17px;font-weight:700;letter-spacing:-.01em;display:flex;"
 "align-items:center;gap:10px;margin-bottom:4px}"
 ".dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}")
SETUP_CSS += (".sub{font-size:13px;color:#9aa1ab;margin-bottom:20px}"
 "label{display:block;font-size:12px;color:#9aa1ab;margin:0 0 6px}"
 "input{width:100%;background:#101216;border:1px solid #24282f;border-radius:8px;"
 "padding:10px 12px;color:#e8eaed;font-size:14px;margin-bottom:14px;outline:none;font-family:inherit}"
 "input:focus{border-color:#2f6fed}"
 "input.code{font-family:Consolas,monospace;font-size:16px;letter-spacing:3px;text-transform:uppercase}"
 ".btn{display:block;width:100%;text-align:center;font-size:14px;font-weight:600;"
 "padding:12px;border-radius:8px;border:1px solid #24282f;color:#e8eaed;"
 "background:#2f6fed;border-color:#2f6fed;cursor:pointer;font-family:inherit}"
 ".btn:hover{background:#5b8def}"
 ".btn:disabled{opacity:.5;cursor:default}"
 ".btnrow{display:flex;gap:8px;margin-top:10px}"
 ".btnrow .btn{background:#1b1e24;border-color:#24282f;font-size:13px;font-weight:500;padding:9px}"
 "#msg{display:none;font-size:13px;border-radius:8px;padding:10px 13px;margin-bottom:14px}"
 "#msg.ok{display:block;background:rgba(52,199,123,.1);border:1px solid rgba(52,199,123,.4);color:#7ddba4}"
 "#msg.err{display:block;background:rgba(229,83,75,.1);border:1px solid rgba(229,83,75,.4);color:#f19994}"
 ".hint{margin-top:16px;font-size:12.5px;color:#646c78;text-align:center}")


def _setup_page():
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    cfg = load_config()
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            "<title>Omniilink Server Setup</title><style>" + SETUP_CSS + "</style></head><body>"
            "<main class=\"card\"><div class=\"brand\">"
            "<span id=\"dot\" class=\"dot\" style=\"background:#d9a021\"></span>"
            "Omniilink Server</div>"
            "<p class=\"sub\" id=\"sub\">Link this computer to your Omniilink server.</p>"
            "<div id=\"msg\"></div>"
            "<label for=\"server\">Server URL</label>"
            "<input id=\"server\" value=\"" + esc(cfg.get("server", "")) + "\" "
            "placeholder=\"https://your-server-address\" autocomplete=\"off\" />"
            "<label for=\"code\">Pairing code</label>"
            "<input id=\"code\" class=\"code\" maxlength=\"16\" placeholder=\"ABC123\" autocomplete=\"off\" />"
            "<label for=\"name\">Computer name</label>"
            "<input id=\"name\" value=\"" + esc(cfg.get("name", "") or socket.gethostname()) + "\" />"
            "<label for=\"projects\">Projects folder</label>"
            "<input id=\"projects\" value=\"" + esc(cfg.get("projects_root", "") or DEFAULT_PROJECTS) + "\" />"
            "<button class=\"btn\" id=\"pair\">Pair this computer</button>"
            "<div class=\"btnrow\"><button class=\"btn\" id=\"open\">Open projects folder</button>"
            "<button class=\"btn\" id=\"diag\">Run diagnostics</button></div>"
            "<div id=\"diagout\" style=\"margin-top:4px\"></div>"
            "<p class=\"hint\">Get a code from the web app: Admin &gt; Agents &gt; + New pairing code.<br>"
            "Green dot = connected. This page only ever runs on your own machine.</p>"
            "</main><script>(function(){"
            "var dot=document.getElementById('dot'),sub=document.getElementById('sub'),"
            "msg=document.getElementById('msg'),out=document.getElementById('diagout');"
            "function show(t,ok){msg.textContent=t;msg.className=ok?'ok':'err';}"
            "function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}"
            "fetch('api/status').then(function(r){return r.json()}).then(function(s){"
            "if(s.paired){dot.style.background='#34c77b';"
            "sub.textContent='Paired'+(s.agent_id?' as agent '+s.agent_id:'')+'. '+(s.connection==='connected'?'Server link is syncing.':'Not syncing right now — start the tray app.');}"
            "}).catch(function(){});"
            "document.getElementById('pair').addEventListener('click',function(){"
            "var b=this;b.disabled=true;msg.className='';"
            "fetch('api/pair',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({server:document.getElementById('server').value,"
            "code:document.getElementById('code').value,"
            "name:document.getElementById('name').value,"
            "projects:document.getElementById('projects').value})})"
            ".then(function(r){return r.json().then(function(d){return{ok:r.ok,d:d}})})"
            ".then(function(x){b.disabled=false;"
            "if(x.ok&&x.d.ok){dot.style.background='#34c77b';"
            "show('Paired as agent '+x.d.agent_id+' — the tray dot turns green.',1);}"
            "else{show((x.d&&x.d.error)||'Pairing failed',0);}})"
            ".catch(function(e){b.disabled=false;show('Pairing failed: '+e,0);});});"
            "document.getElementById('code').addEventListener('input',function(){"
            "this.value=this.value.toUpperCase();});"
            "document.getElementById('open').addEventListener('click',function(){"
            "fetch('api/open-folder',{method:'POST'}).catch(function(){});});"
            "document.getElementById('diag').addEventListener('click',function(){"
            "var b=this;out.innerHTML='';b.disabled=true;"
            "fetch('api/doctor').then(function(r){return r.json()}).then(function(d){"
            "b.disabled=false;"
            "out.innerHTML=(d.checks||[]).map(function(c){"
            "return '<div style=\"padding:6px 0;border-top:1px solid #24282f;font-size:13px\">'"
            "+'<span style=\"display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;background:'"
            "+(c.ok?'#34c77b':'#e5534b')+'\"></span><b>'+esc(c.name)+'</b><br>'"
            "+'<span style=\"color:#9aa1ab\">'+esc(c.detail)+'</span></div>'}).join('');"
            "}).catch(function(e){b.disabled=false;out.textContent='Diagnostics failed: '+e;});});"
            "})();</script></body></html>")


class _SetupHandler(_http_server.BaseHTTPRequestHandler):
    server_version = "OmniilinkServer/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except Exception:
            n = 0
        try:
            return json.loads(self.rfile.read(max(0, n)).decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        from urllib.parse import urlparse
        if urlparse(self.path).path == "/api/doctor":
            try:
                checks = [{"name": n, "ok": bool(o), "detail": str(d or "")}
                          for n, o, d in run_doctor()]
                return self._json({"checks": checks})
            except Exception as e:
                return self._json({"checks": [{"name": "diagnostics", "ok": False,
                                               "detail": str(e)}]})
        if urlparse(self.path).path == "/api/status":
            cfg = load_config()
            paired = bool(cfg.get("token") and cfg.get("server"))
            age = _poll_age()
            if not paired:
                conn = "unpaired"
            elif age is None:
                conn = "idle"
            elif age < 20:
                conn = "connected"
            else:
                conn = "stale"
            return self._json({"paired": paired, "agent_id": cfg.get("agent_id", ""),
                               "server": cfg.get("server", ""), "name": cfg.get("name", ""),
                               "projects_root": cfg.get("projects_root", ""),
                               "connection": conn, "last_poll_seconds_ago": age})
        return self._send(200, _setup_page())

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/api/pair":
            data = self._body_json()
            server = normalize_server(data.get("server", ""))
            code = clean_code(data.get("code", ""))
            name = (data.get("name", "") or "").strip() or socket.gethostname()
            proot = (data.get("projects", "") or "").strip() or DEFAULT_PROJECTS
            if not server or not code:
                return self._json({"ok": False,
                                   "error": "Enter the server address and the pairing code."}, 400)
            try:
                aid, token = pair(server, code, name)
            except Exception as e:
                return self._json({"ok": False, "error": "Pairing failed: " + str(e)}, 502)
            try:
                c = load_config()
                c.update({"server": server, "token": token, "agent_id": aid,
                          "name": name, "projects_root": proot})
                save_config(c)
                os.makedirs(proot, exist_ok=True)
            except Exception as e:
                return self._json({"ok": False,
                                   "error": "Paired, but could not save: " + str(e)}, 500)
            return self._json({"ok": True, "agent_id": aid})
        if path == "/api/open-folder":
            folder = load_config()["projects_root"]
            try:
                os.makedirs(folder, exist_ok=True)
                os.startfile(folder)  # noqa: S606 -- local folder by design
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"ok": False, "error": "not found"}, 404)


def start_setup_server(port=SETUP_PORT):
    """Localhost-only setup panel. Tries the fixed port, falls back to an
    ephemeral one so two copies never fight. Returns (server, thread, port)
    with the ACTUAL bound port — open that URL, never guess from the file."""
    import http.server as _hs
    import socketserver as _ss

    class _Threaded(_ss.ThreadingMixIn, _hs.HTTPServer):
        daemon_threads = True
    try:
        srv = _Threaded(("127.0.0.1", port), _SetupHandler)
    except OSError:
        srv = _Threaded(("127.0.0.1", 0), _SetupHandler)
    actual = srv.server_address[1]
    _write_port_file(actual)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, actual


def _setup_alive(port):
    """True when OUR setup panel answers on a localhost port."""
    import urllib.request as _url
    try:
        with _url.urlopen("http://127.0.0.1:%d/api/status" % int(port), timeout=3) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8") or "{}")
            return isinstance(body, dict) and "paired" in body
    except Exception:
        return False


def _setup_base():
    """Base URL of a live setup panel, or None."""
    try:
        port = _read_port_file()
    except Exception:
        return None
    if port and _setup_alive(port):
        return "http://127.0.0.1:%d" % port
    return None


def tray_unlock():
    """Release a lock WE hold (used to probe-then-release in diagnostics)."""
    global _tray_lock_fd
    try:
        if _tray_lock_fd is not None:
            try:
                os.close(_tray_lock_fd)
            except Exception:
                pass
            _tray_lock_fd = None
        try:
            os.remove(os.path.join(CONFIG_DIR, "tray.lock"))
        except OSError:
            pass
    except Exception:
        pass


def run_doctor():
    """Check everything between this computer and the server. Returns a list
    of (name, ok, detail). Never raises."""
    out = []
    cfg = load_config()
    if os.path.isfile(CONFIG_PATH):
        out.append(("config file", True, CONFIG_PATH))
    else:
        out.append(("config file", True, "missing (defaults used)"))
    if not cfg.get("server") or not cfg.get("token"):
        out.append(("pairing", False, "not paired — enter a pairing code first"))
        return out
    out.append(("pairing", True, "agent " + str(cfg.get("agent_id") or "?")))
    try:
        h = api(cfg["server"], "GET", "/api/health")
        out.append(("server reachable", True,
                    "build " + str(h.get("build", "?")) + ", model " + str(h.get("model", "?"))))
    except Exception as e:
        return out + [("server reachable", False, str(e))]
    try:
        p = api(cfg["server"], "GET", "/api/agent/pending", cfg["token"])
        out.append(("token accepted", True,
                    "queue idle" if not p.get("command") else "a command is waiting"))
    except ApiError as e:
        if e.status == 401:
            out.append(("token accepted", False,
                        "server rejected the token — re-pair this computer (remove it under "
                        "Admin > Agents first if it is still listed)"))
        else:
            out.append(("token accepted", False, str(e)))
    except Exception as e:
        out.append(("token accepted", False, str(e)))
    proot = cfg.get("projects_root") or DEFAULT_PROJECTS
    try:
        os.makedirs(proot, exist_ok=True)
        probe = os.path.join(proot, ".doctor-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        out.append(("projects folder writable", True, proot))
    except Exception as e:
        out.append(("projects folder writable", False, proot + ": " + str(e)))
    if tray_lock():
        tray_unlock()
        out.append(("tray running", False, "tray is not running — start Omniilink Server"))
    else:
        out.append(("tray running", True, "tray process holds the lock"))
    return out


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


def _ensure_setup_page(block):
    """Open a live setup panel, starting one if none answers. When block is
    true, stay up serving it (Ctrl+C ends); otherwise return once opened."""
    import webbrowser
    base = _setup_base()
    if not base:
        _srv, _t, _port = start_setup_server(SETUP_PORT)
        base = "http://127.0.0.1:%d" % _port
    try:
        webbrowser.open(base + "/")
    except Exception:
        pass
    if block:
        print("Setup panel serving at " + base + ". Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
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
    sub.add_parser("doctor", help="check server, token, folder and tray")
    sub.add_parser("setup", help="browser setup panel only")
    sub.add_parser("gui", help="browser setup panel only (alias)")
    args = ap.parse_args(argv)
    if args.cmd == "pair":
        if getattr(args, "projects", ""):
            cfg = load_config()
            cfg["projects_root"] = args.projects
            save_config(cfg)
        return cmd_pair(args)
    if args.cmd in ("gui", "setup"):
        return _ensure_setup_page(True)
    if args.cmd == "tray":
        if not tray_lock():
            return 0
        run_tray()
        return 0
    if args.cmd == "doctor":
        bad = False
        for name, ok, detail in run_doctor():
            if not ok:
                bad = True
            print(("OK   " if ok else "FAIL ") + name + ((" — " + detail) if detail else ""))
        return 1 if bad else 0
    # No subcommand: another copy already running -> show its setup panel.
    if not tray_lock():
        return _ensure_setup_page(False)
    cfg = load_config()
    if cfg.get("token") and cfg.get("server"):
        run_tray()
        return 0
    # First run: open the setup panel, then run the tray blocking so both
    # stay alive in this process (tray picks up the pairing live).
    _ensure_setup_page(False)
    run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
