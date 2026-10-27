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
import tempfile
import threading
import time
import urllib.error
import urllib.request
APP_NAME = "Omniilink Server"
COMPANION_VERSION = "1.0.17"
CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OmniilinkCompanion")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_PROJECTS = os.path.join(os.path.expanduser("~"), "Documents", "OmniilinkProjects")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
POLL_SECONDS = 0.25
CMD_TIMEOUT = 300
_TREE_MAX_ENTRIES = 20000  # tree op stops here (huge folders list instead)
READ_MAX = 256 * 1024
WRITE_MAX = 1024 * 1024


# ---------------- config ----------------
def default_config():
    return {"server": "", "token": "", "agent_id": "",
            "name": socket.gethostname(), "projects_root": DEFAULT_PROJECTS,
            "ollama_url": DEFAULT_OLLAMA_URL}


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


class ApiTimeout(Exception):
    """The socket read deadline elapsed. Almost always transient: the server
    long-polls /api/agent/pending for up to ~25s, so a timeout that fires here
    means the network was slow, not that the pairing is broken."""


# The server holds GET /api/agent/pending open for up to 25s waiting for work.
# The client deadline must clear that with real headroom, or a healthy long-poll
# trips the socket read timeout on a slow link and the tray shows a scary red
# "The read operation timed out" for a connection that is actually fine.
HTTP_TIMEOUT = 75
LONG_TIMEOUT = 120


def api(server, method, path, token=None, payload=None, timeout=None):
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": COMPANION_UA})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=body,
                                    timeout=timeout or HTTP_TIMEOUT) as r:
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
    except (socket.timeout, TimeoutError) as e:
        # socket.timeout IS TimeoutError on 3.10+; both spellings covered for
        # older builds. urllib wraps some of these in URLError.
        raise ApiTimeout(str(e) or "socket read timed out")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise ApiTimeout("socket read timed out")
        raise


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
            return {"ok": True, "root": os.path.realpath(root), "entries": entries}
        if op == "tree":
            # One recursive walk instead of a round trip per folder: the file
            # tree for a project arrives in a single command. Mirrors the web
            # client's agentBuildTree exactly (same skips, same depth cap).
            base = safe_join(root, payload.get("path", ""))
            if not os.path.isdir(base):
                return {"ok": False, "error": "not a folder"}
            skip = {"node_modules", ".git", ".venv", "venv", "__pycache__",
                    ".next", ".turbo", ".cache", ".pytest_cache", ".idea",
                    ".vscode"}
            count = [0]
            truncated = [False]

            def walk(dirpath, depth):
                try:
                    with os.scandir(dirpath) as it:
                        items = sorted(it, key=lambda e: (
                            not e.is_dir(follow_symlinks=False),
                            e.name.lower()))
                except OSError:
                    return []
                out = []
                for e in items:
                    if count[0] >= _TREE_MAX_ENTRIES:
                        truncated[0] = True
                        break
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and (depth >= 4 or e.name in skip):
                        continue
                    count[0] += 1
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    node = {"name": e.name,
                            "type": "dir" if is_dir else "file",
                            "size": size}
                    if is_dir and not e.is_symlink():
                        node["children"] = walk(
                            os.path.join(dirpath, e.name), depth + 1)
                    out.append(node)
                return out

            return {"ok": True, "tree": walk(base, 0),
                    "truncated": truncated[0]}
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
        if op == "readb64":
            import base64 as _b64
            full = safe_join(root, payload.get("path", ""))
            if not os.path.isfile(full):
                return {"ok": False, "error": "not a file"}
            if os.path.getsize(full) > 8 * 1024 * 1024:
                return {"ok": False, "error": "file too large"}
            with open(full, "rb") as f:
                data = f.read()
            return {"ok": True, "content": _b64.b64encode(data).decode("ascii"),
                    "bytes": len(data)}
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
            return {"ok": True, "bytes": len(content.encode("utf-8")),
                    "path": os.path.relpath(full, root).replace("\\", "/")}
        if op == "mkdir":
            if not payload.get("path"):
                return {"ok": False, "error": "missing path"}
            full = safe_join(root, payload.get("path", ""))
            os.makedirs(full, exist_ok=True)
            return {"ok": True, "path": os.path.relpath(full, root).replace("\\", "/")}
        if op == "delete":
            full = safe_join(root, payload.get("path", ""))
            if not payload.get("path") or full == os.path.realpath(root):
                return {"ok": False, "error": "refusing to delete the projects folder"}
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=False)
            elif os.path.isfile(full) or os.path.islink(full):
                os.remove(full)
            else:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "recursive": True}
        return {"ok": False, "error": "unknown file op"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def _rewrite_inline_python(cmd, workdir):
    """Mirror of the server's rewrite: a multi-line `python -c` script never
    survives cmd.exe quoting, so run it from a temp file in the workdir."""
    import re as _re
    import tempfile as _tf
    m = _re.match(r"(?i)^\s*(python3?|py)(?:\.exe)?\s+-c\s+(.*)$", cmd or "", _re.DOTALL)
    if not m or "\n" not in m.group(2):
        return cmd, None
    prog, script = m.group(1), m.group(2).strip()
    if len(script) >= 2 and script[0] == script[-1] and script[0] in "\"'":
        script = script[1:-1]
    try:
        fd, tmp = _tf.mkstemp(prefix="omni_inline_", suffix=".py",
                              dir=workdir or PROJECTS_ROOT_DEFAULT)
    except Exception:
        return cmd, None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return cmd, None
    return prog + ' "' + os.path.basename(tmp) + '"', tmp


def do_shell(cmd, root=None, timeout=CMD_TIMEOUT, cwd=None):
    try:
        workdir = None
        if cwd:
            workdir = safe_join(root or PROJECTS_ROOT_DEFAULT, cwd)
        cmd, inline_tmp = _rewrite_inline_python(cmd, workdir)
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  timeout=timeout, cwd=workdir)
        finally:
            if inline_tmp:
                try:
                    os.remove(inline_tmp)
                except OSError:
                    pass
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-65536:]
    except subprocess.TimeoutExpired:
        return 124, "[timed out]"
    except Exception as e:
        return 1, "agent error: " + str(e)


# ---------------- ollama relay ----------------
# The host queues kind=ollama commands; we perform the local HTTP call
# against our own Ollama (localhost only — no firewall changes needed) and
# stream seq-numbered batches back to POST /api/agent/relay. Runs in worker
# threads so the poll loop keeps serving shell/file commands meanwhile.
_RELAY_PATHS = ("/api/tags", "/api/show", "/api/chat", "/v1/chat/completions",
                "/api/version", "/api/pull")
_RELAY_BODY_MAX = 8 * 1024 * 1024
_RELAY_FLUSH_BYTES = 32768
_RELAY_FLUSH_SECONDS = 0.05
_RELAY_WORKERS = 4
_relay_exec = None
_relay_exec_lock = threading.Lock()


def relay_executor():
    global _relay_exec
    with _relay_exec_lock:
        if _relay_exec is None:
            from concurrent.futures import ThreadPoolExecutor
            _relay_exec = ThreadPoolExecutor(max_workers=_RELAY_WORKERS,
                                             thread_name_prefix="ollama-relay")
        return _relay_exec


class RelayGone(Exception):
    """Host dropped the stream (410): stop the local Ollama read."""


_relay_conns = {}
_relay_conns_lock = threading.Lock()


def _relay_post(server, token, fields, timeout=None):
    """POST one relay batch over a pooled keep-alive connection.

    A streamed reply is dozens of small batches; a fresh TCP+TLS handshake
    per batch (what api() does) dominates the round trip. Reusing one
    HTTP/1.1 connection per host removes it. Raises ApiError exactly like
    api() so the sender's retry/410 handling is unchanged. A server that
    closes idle connections just triggers one transparent reconnect.
    """
    import http.client
    from urllib.parse import urlparse
    u = urlparse(server.rstrip("/") + "/api/agent/relay")
    port = u.port or (443 if u.scheme == "https" else 80)
    key = (u.scheme, (u.hostname or "").lower(), port)
    path = u.path or "/api/agent/relay"
    body = json.dumps(fields).encode()
    headers = {"Content-Type": "application/json", "User-Agent": COMPANION_UA,
               "Content-Length": str(len(body)), "Connection": "keep-alive"}
    if token:
        headers["Authorization"] = "Bearer " + token
    with _relay_conns_lock:
        for attempt in (0, 1):
            conn = _relay_conns.get(key)
            if conn is None:
                cls = (http.client.HTTPSConnection if u.scheme == "https"
                       else http.client.HTTPConnection)
                conn = cls(u.hostname or "", port, timeout=timeout or HTTP_TIMEOUT)
                _relay_conns[key] = conn
            try:
                conn.request("POST", path, body=body, headers=headers)
                r = conn.getresponse()
                raw = r.read()
                status = r.status
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                _relay_conns.pop(key, None)
                if attempt == 0:
                    continue
                raise
            break
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = ""
    if status in (404, 410):
        raise ApiError(status, (text or "gone").strip()[:300])
    if status < 200 or status >= 300:
        try:
            parsed = json.loads(text or "{}")
            detail = parsed.get("error", "") or text
        except Exception:
            detail = text
        raise ApiError(status, (detail or "relay post failed").strip()[:300])
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


class _RelaySender(object):
    """Seq-numbered batch poster; retries transient failures, treats 410
    as the host saying 'this stream no longer exists'."""

    def __init__(self, server, token, cid):
        self.server = server
        self.token = token
        self.cid = cid
        self.seq = 0

    def send(self, **fields):
        fields["command_id"] = self.cid
        fields["seq"] = self.seq
        self.seq += 1
        last = None
        for attempt in range(3):
            try:
                _relay_post(self.server, self.token, fields)
                return
            except ApiError as e:
                if e.status in (404, 410):
                    raise RelayGone()
                last = e
                time.sleep(0.5 * (attempt + 1))
            except Exception as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("relay post failed: " + str(last))


def do_ollama_relay(server, token, cmd):
    """Service one kind=ollama command against the local Ollama."""
    cid = cmd.get("id")
    payload = cmd.get("payload") or {}
    path = str(payload.get("path") or "")
    body = payload.get("body")
    sender = _RelaySender(server, token, cid)
    try:
        if path not in _RELAY_PATHS:
            sender.send(error="relay path not allowed: " + path)
            return
        cfg = load_config()
        base = (cfg.get("ollama_url") or DEFAULT_OLLAMA_URL).strip().rstrip("/")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json", "Accept": "*/*"})
        try:
            # Generous ceiling: a deep-reasoning model can take tens of
            # minutes for one (especially non-streaming) response.
            resp = urllib.request.urlopen(req, timeout=3600)
        except urllib.error.HTTPError as he:
            # Ollama answers errors as one JSON body even when stream=true.
            try:
                raw = he.read(_RELAY_BODY_MAX)
            except Exception:
                raw = b""
            headers = {"content-type":
                       he.headers.get("Content-Type") or "application/json"}
            sender.send(status=he.code, headers=headers,
                        body=raw.decode("utf-8", "replace"), done=True)
            return
        except Exception as e:
            sender.send(error="Ollama is not reachable on this computer ("
                        + str(e)[:240] + ")")
            return
        try:
            status = getattr(resp, "status", None) or resp.getcode() or 200
            headers = {"content-type":
                       resp.headers.get("Content-Type") or "application/json"}
            stream_intent = isinstance(body, dict) and body.get("stream") is True
            if status != 200 or not stream_intent:
                raw = resp.read(_RELAY_BODY_MAX + 1)
                if len(raw) > _RELAY_BODY_MAX:
                    sender.send(error="Ollama response too large")
                    return
                sender.send(status=status, headers=headers,
                            body=raw.decode("utf-8", "replace"), done=True)
                return
            sender.send(status=status, headers=headers)
            buf = []
            size = 0
            last_flush = time.time()
            while True:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                buf.append(text)
                size += len(text)
                if (size >= _RELAY_FLUSH_BYTES
                        or time.time() - last_flush >= _RELAY_FLUSH_SECONDS):
                    sender.send(lines=buf)
                    buf, size = [], 0
                    last_flush = time.time()
            if buf:
                sender.send(lines=buf)
            sender.send(done=True)
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except RelayGone:
        pass
    except Exception as e:
        try:
            sender.send(error=str(e)[:300])
        except Exception:
            pass


# ---------------- sync loop ----------------
def poll_once(server, token, root, status_cb=None):
    """One heartbeat: fetch a queued command, run it, post the result.
    Returns 'idle' | 'ran' | 'error' | 'unauthorized' (dead/removed token —
    the caller should reload config instead of retrying blindly)."""
    try:
        # The server long-polls this endpoint for ~25s waiting for work, so it
        # needs a deadline well clear of that; a short one reports a healthy
        # idle connection as a read timeout.
        p = api(server, "GET", "/api/agent/pending?caps=ollama", token,
                timeout=LONG_TIMEOUT)
    except ApiError as e:
        if status_cb:
            status_cb(False, str(e))
        return "unauthorized" if e.status == 401 else "error"
    except ApiTimeout:
        # Transient: the link stalled. Stay green-ish and retry on the next
        # tick instead of painting a permanent error the user cannot clear.
        if status_cb:
            status_cb(True, "reconnecting…")
        return "idle"
    except Exception as e:
        if status_cb:
            status_cb(False, str(e))
        return "error"
    # Sync MCP config from server
    try:
        mcp_sync(p.get("mcp_servers") or [])
    except Exception:
        pass
    cmd = p.get("command")
    if not cmd:
        if status_cb:
            status_cb(True, "")
        return "idle"
    kind = cmd.get("kind") or "shell"
    if kind == "ollama":
        # Model relay runs in a worker: streams can take minutes, and the
        # poll loop must keep picking up shell/file commands meanwhile.
        # The worker posts batches directly; no result entry is written
        # here (the relay endpoint tracks the stream lifecycle).
        try:
            relay_executor().submit(do_ollama_relay, server, token, cmd)
        except Exception:
            if status_cb:
                status_cb(False, "could not start relay worker")
            return "error"
        if status_cb:
            status_cb(True, "")
        return "ran"
    try:
        if kind == "file":
            res = do_file_op(root, cmd.get("payload") or {})
            code = 0 if res.get("ok") else 1
            out = json.dumps(res)
        else:
            payload = cmd.get("payload") or {}
            code, out = do_shell(cmd.get("command") or "", root,
                                 int(cmd.get("timeout") or CMD_TIMEOUT),
                                 payload.get("cwd"))
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


# ---------------- self-update ----------------
_UPDATE = {"latest": "", "msi": ""}


def _ver_tuple(v):
    parts = []
    for bit in str(v or "").strip().split("."):
        digits = "".join(ch for ch in bit if ch.isdigit())
        try:
            parts.append(int(digits or 0))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def newer_available(current, latest):
    try:
        return _ver_tuple(latest) > _ver_tuple(current)
    except Exception:
        return False


def _download_file(server, token, path, dest):
    req = urllib.request.Request(server.rstrip("/") + path, method="GET",
                                 headers={"User-Agent": COMPANION_UA})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=65536)
    return dest


def fetch_update(server, token):
    """Check for a newer build; download its MSI. Returns (latest, msi_path
    or 'error: ...' or ''). Never raises."""
    try:
        info = api(server, "GET", "/api/agents/companion-version", token)
    except Exception as e:
        return "", "error: " + str(e)
    latest = str((info or {}).get("version") or "")
    if not latest:
        return "", "error: empty version"
    if not newer_available(COMPANION_VERSION, latest):
        return latest, ""
    safe = "".join(ch for ch in latest if ch.isalnum() or ch in ".-_")[:32] or "latest"
    dest = os.path.join(tempfile.gettempdir(), "OmniilinkServerSetup-%s.msi" % safe)
    try:
        _download_file(server, token, "/api/agents/companion", dest)
    except Exception as e:
        return latest, "error: " + str(e)
    return latest, dest


def _find_product_code():
    """Find the installed product code from the registry."""
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                key = winreg.OpenKey(hive, sub)
            except Exception:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                    i += 1
                except OSError:
                    break
                try:
                    sk = winreg.OpenKey(key, name)
                    dn = winreg.QueryValueEx(sk, "DisplayName")[0]
                    if "Omniilink" in str(dn):
                        winreg.CloseKey(sk)
                        winreg.CloseKey(key)
                        return name
                    winreg.CloseKey(sk)
                except Exception:
                    pass
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return None

def install_update_and_quit(msi_path, quit_fn):
    """Uninstall the old version silently, then install the new one.
    Returns False only if msiexec itself could not start."""
    try:
        code = _find_product_code()
        if code:
            # uninstall old
            subprocess.run(["msiexec", "/x", code, "/qn", "/norestart"], timeout=120)
            time.sleep(2)
        # install new
        subprocess.Popen(["msiexec", "/i", msi_path, "/qn", "/norestart"])
    except Exception:
        try:
            subprocess.Popen(["msiexec", "/i", msi_path, "/qn", "/norestart"])
        except Exception:
            return False
    try:
        quit_fn()
    except Exception:
        pass
    return True



# ---- Zen health + MCP (Model Context Protocol) client ----
import subprocess as _mcp_subprocess
_MCP_SERVERS = {}
_MCP_LOCK = threading.Lock()
_MCP_ID = [0]
_RELAY_PROC = None
_RELAY_PASSWORD = ""
_RELAY_PORT = 4096

def _mcp_next_id():
    _MCP_ID[0] += 1
    return _MCP_ID[0]

def _mcp_rpc(proc, method, params=None, timeout=30):
    req = {"jsonrpc": "2.0", "id": _mcp_next_id(), "method": method}
    if params is not None:
        req["params"] = params
    try:
        proc.stdin.write((json.dumps(req) + "\n").encode())
        proc.stdin.flush()
    except Exception as e:
        return None, str(e)
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if r:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                for line in buf.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("id") == req["id"]:
                            return obj, None
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            return None, str(e)
    return None, "timeout"

def mcp_sync(servers):
    if not isinstance(servers, list):
        servers = []
    # Write to mcp.json (opencode format) for manual editing
    try:
        mcp_cfg = {"mcp": {}}
        for srv in servers:
            name = srv.get("name")
            if not name:
                continue
            if srv.get("type") == "stdio":
                mcp_cfg["mcp"][name] = {"type": "local", "command": [srv.get("command", "")] + (srv.get("args") or []), "enabled": True}
            else:
                entry = {"type": "remote", "url": srv.get("url", "")}
                if srv.get("username"):
                    import base64 as _b64
                    entry["headers"] = {"Authorization": "Basic " + _b64.b64encode((srv["username"] + ":" + srv.get("password", "")).encode()).decode()}
                mcp_cfg["mcp"][name] = entry
        pth = os.path.join(CONFIG_DIR, "mcp.json")
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(pth, "w", encoding="utf-8") as f:
            json.dump(mcp_cfg, f, indent=2)
    except Exception:
        pass
    with _MCP_LOCK:
        new_names = {s.get("name") for s in servers if s.get("name")}
        for name in list(_MCP_SERVERS.keys()):
            if name not in new_names:
                mcp_stop(name)
        for srv in servers:
            name = srv.get("name")
            if not name or name in _MCP_SERVERS:
                continue
            mcp_start(srv)

def mcp_start(srv):
    name = srv.get("name")
    stype = srv.get("type", "stdio")
    if stype == "stdio":
        cmd = srv.get("command", "")
        args = srv.get("args", [])
        env = dict(os.environ)
        env.update(srv.get("env", {}) or {})
        try:
            proc = subprocess.Popen([cmd] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, bufsize=0)
        except Exception as e:
            _MCP_SERVERS[name] = {"cfg": srv, "proc": None, "tools": [], "status": "error: " + str(e)}
            return
        resp, err = _mcp_rpc(proc, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                  "clientInfo": {"name": "omniilink", "version": "1.0"}}, 15)
        if err or not resp:
            _MCP_SERVERS[name] = {"cfg": srv, "proc": proc, "tools": [], "status": "error: " + (err or "no response")}
            return
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
            proc.stdin.flush()
        except Exception:
            pass
        resp, err = _mcp_rpc(proc, "tools/list", {}, 15)
        tools = []
        if resp and isinstance(resp.get("result"), dict):
            tools = resp["result"].get("tools", []) or []
        _MCP_SERVERS[name] = {"cfg": srv, "proc": proc, "tools": tools, "status": "ready"}
    elif stype == "sse":
        _MCP_SERVERS[name] = {"cfg": srv, "proc": None, "tools": [], "status": "sse: " + srv.get("url", "")}

def mcp_stop(name):
    entry = _MCP_SERVERS.pop(name, None)
    if entry and entry.get("proc"):
        try:
            entry["proc"].terminate()
            entry["proc"].wait(timeout=5)
        except Exception:
            try:
                entry["proc"].kill()
            except Exception:
                pass

def mcp_list_tools():
    out = []
    with _MCP_LOCK:
        for name, entry in _MCP_SERVERS.items():
            for t in entry.get("tools", []):
                tool = dict(t)
                tool["_server"] = name
                out.append(tool)
    return out

def mcp_call_tool(server_name, tool_name, arguments):
    with _MCP_LOCK:
        entry = _MCP_SERVERS.get(server_name)
    if not entry:
        return {"error": "server not found"}
    proc = entry.get("proc")
    if not proc:
        return {"error": "server not running"}
    resp, err = _mcp_rpc(proc, "tools/call", {"name": tool_name, "arguments": arguments or {}}, 60)
    if err:
        return {"error": err}
    if resp and "result" in resp:
        return {"result": resp["result"]}
    if resp and "error" in resp:
        return {"error": resp["error"]}
    return {"error": "no response"}

def mcp_status():
    out = []
    with _MCP_LOCK:
        for name, entry in _MCP_SERVERS.items():
            out.append({"name": name, "type": entry["cfg"].get("type"), "status": entry["status"],
                        "tool_count": len(entry.get("tools", []))})
    return out

def relay_status():
    global _RELAY_PROC
    import urllib.request
    try:
        r = urllib.request.urlopen("http://127.0.0.1:%d/global/health" % _RELAY_PORT, timeout=3)
        data = json.loads(r.read().decode())
        return {"ok": True, "running": True, "authed": True, "url": "http://127.0.0.1:%d" % _RELAY_PORT,
                "version": data.get("version", ""), "connected": [], "free_ready": True}
    except Exception:
        return {"ok": True, "running": False, "authed": False, "url": "http://127.0.0.1:%d" % _RELAY_PORT,
                "error": "OpenCode not running", "free_ready": False}

def relay_start():
    global _RELAY_PROC
    import shutil
    binary = shutil.which("opencode") or ""
    if not binary:
        for p in [os.path.join(os.environ.get("APPDATA",""), "npm", "opencode.CMD"),
                  os.path.join(os.environ.get("APPDATA",""), "npm", "opencode.cmd")]:
            if os.path.isfile(p):
                binary = p; break
    if not binary:
        return {"ok": False, "running": False, "error": "opencode not found"}
    try:
        _RELAY_PROC = subprocess.Popen([binary, "serve", "--port", str(_RELAY_PORT), "--hostname", "127.0.0.1"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        return relay_status()
    except Exception as e:
        return {"ok": False, "running": False, "error": str(e)}


def mcp_load_file():
    """Read mcp.json (opencode format) and return list of server configs."""
    p = os.path.join(CONFIG_DIR, "mcp.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    mcp = data.get("mcp", data.get("servers", {}))
    if not isinstance(mcp, dict):
        return []
    out = []
    for name, cfg in mcp.items():
        if not isinstance(cfg, dict):
            continue
        entry = {"name": name}
        ctype = cfg.get("type", "")
        if ctype == "local" or "command" in cfg:
            entry["type"] = "stdio"
            cmd = cfg.get("command", [])
            if isinstance(cmd, list):
                entry["command"] = cmd[0] if cmd else ""
                entry["args"] = cmd[1:] if len(cmd) > 1 else []
            else:
                entry["command"] = str(cmd)
                entry["args"] = cfg.get("args", [])
            entry["env"] = cfg.get("env", {}) or {}
        elif ctype == "remote" or "url" in cfg:
            entry["type"] = "sse"
            entry["url"] = cfg.get("url", "")
            if cfg.get("headers"):
                entry["username"] = cfg["headers"].get("Authorization", "").replace("Bearer ", "") if isinstance(cfg["headers"], dict) else ""
        if cfg.get("enabled", True) is False:
            continue
        out.append(entry)
    return out

_MCP_FILE_MTIME = [0.0]

def mcp_file_watcher(stop):
    """Watch mcp.json for changes and sync MCP servers."""
    while not stop.is_set():
        p = os.path.join(CONFIG_DIR, "mcp.json")
        try:
            if os.path.exists(p):
                mt = os.path.getmtime(p)
                if mt != _MCP_FILE_MTIME[0]:
                    _MCP_FILE_MTIME[0] = mt
                    servers = mcp_load_file()
                    mcp_sync(servers)
        except Exception:
            pass
        stop.wait(3)

def run_tray():
    """System-tray icon with green/red/amber connection indicator."""
    import pystray
    cfg = load_config()
    os.makedirs(cfg["projects_root"], exist_ok=True)
    try:
        mcp_sync(mcp_load_file())
    except Exception:
        pass
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
        # A trailing "…" marks a TRANSIENT state (reconnecting, backoff):
        # show amber, never the red dot the user cannot clear. A hard error is
        # a real failure and stays red.
        transient = bool(ok and msg and msg.endswith("…"))
        state["ok"] = None if transient else bool(ok)
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
                # The server rejected our token: the agent was removed or the
                # machine got a NEW agent (fresh pairing) while this process
                # held the old one. Clear it so Setup shows "not paired" and
                # open the Setup page once — retrying a dead token forever is
                # what left the tray red with no way out.
                c["token"] = ""
                c["agent_id"] = ""
                save_config(c)
                state["ok"] = None
                state["msg"] = "not paired — token rejected, re-pair"
                redraw()
                if not state.get("reopened_setup"):
                    state["reopened_setup"] = True
                    try:
                        _ensure_setup_page(False)
                    except Exception:
                        pass
                state["stop"].wait(5)
                continue
            state["reopened_setup"] = False

    def on_open(icon, item):
        folder = load_config()["projects_root"]
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 — local folder by design
        except Exception:
            pass

    def on_setup(icon, item):
        _ensure_setup_page(False)

    def on_mcp_config(icon, item):
        p = os.path.join(CONFIG_DIR, "mcp.json")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"mcp": {}}, f, indent=2)
        except Exception:
            pass
        try:
            os.startfile(p)
        except Exception:
            pass

    def do_quit():
        state["stop"].set()
        try:
            if state["icon"] is not None:
                state["icon"].stop()
        except Exception:
            pass

    def on_update(icon, item):
        c = load_config()
        if not c["token"] or not c["server"]:
            state["msg"] = "pair this computer first, then check for updates"
            redraw()
            return
        msi = _UPDATE.get("msi")
        if msi and os.path.isfile(msi):
            state["msg"] = "installing update " + _UPDATE.get("latest", "")
            redraw()
            install_update_and_quit(msi, do_quit)
            return
        state["msg"] = "checking for updates…"
        redraw()

        def _go():
            latest, got = fetch_update(c["server"], c["token"])
            if got.startswith("error:"):
                state["msg"] = "update check failed"
            elif got:
                _UPDATE["latest"] = latest
                _UPDATE["msi"] = got
                state["msg"] = "update " + latest + " ready — click again to install"
            else:
                state["msg"] = "up to date (" + COMPANION_VERSION + ")"
            redraw()

        threading.Thread(target=_go, daemon=True).start()

    def update_label(item):
        if _UPDATE.get("msi"):
            return "Install update (%s)…" % _UPDATE.get("latest", "")
        return "Check for updates…"

    def on_quit(icon, item):
        do_quit()

    def auto_update():
        try:
            c = load_config()
            if not c["token"] or not c["server"]:
                return
            latest, got = fetch_update(c["server"], c["token"])
            if got and not got.startswith("error:"):
                _UPDATE["latest"] = latest
                _UPDATE["msi"] = got
                state["msg"] = "update " + latest + " ready — right-click > Install"
                redraw()
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Open projects folder", on_open),
        pystray.MenuItem("Open MCP config", on_mcp_config),
        pystray.MenuItem("Setup…", on_setup),
        pystray.MenuItem(update_label, on_update),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, _dot_icon((217, 160, 33)), APP_NAME + " — starting…", menu)
    state["icon"] = icon
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=mcp_file_watcher, args=(state["stop"],), daemon=True).start()
    threading.Thread(target=auto_update, daemon=True).start()
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
            "<label for=\"ollama\">Ollama URL (shared models)</label>"
            "<input id=\"ollama\" value=\"" + esc(cfg.get("ollama_url", "") or DEFAULT_OLLAMA_URL) + "\" "
            "placeholder=\"http://127.0.0.1:11434\" autocomplete=\"off\" />"
            "<button class=\"btn\" id=\"pair\">Pair this computer</button>"
            "<div class=\"btnrow\"><button class=\"btn\" id=\"open\">Open projects folder</button>"
            "<button class=\"btn\" id=\"ollamatest\">Test Ollama</button>"
            "<button class=\"btn\" id=\"diag\">Run diagnostics</button></div>"
            "<div id=\"diagout\" style=\"margin-top:4px\"></div>"
            "<p class=\"hint\">Get a code from the web app: Admin &gt; Agents &gt; + New pairing code.<br>"
            "Only use a code created for the account that should own this computer. "
            "Pairing gives that account full command and file access inside the projects folder.<br>"
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
            "projects:document.getElementById('projects').value,"
            "ollama_url:document.getElementById('ollama').value})})"
            ".then(function(r){return r.json().then(function(d){return{ok:r.ok,d:d}})})"
            ".then(function(x){b.disabled=false;"
            "if(x.ok&&x.d.ok){dot.style.background='#34c77b';msg.className='';"
            "sub.textContent='Paired as agent '+x.d.agent_id+(x.d.owner?(' for '+x.d.owner):'')+'. Close this window — the tray icon handles the rest.';}"
            "else{show((x.d&&x.d.error)||'Pairing failed',0);}})"
            ".catch(function(e){b.disabled=false;show('Pairing failed: '+e,0);});});"
            "document.getElementById('ollamatest').addEventListener('click',function(){"
            "var b=this;b.disabled=true;msg.className='';"
            "fetch('api/ollama',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({url:document.getElementById('ollama').value})})"
            ".then(function(r){return r.json().then(function(d){return{ok:r.ok,d:d}})})"
            ".then(function(x){b.disabled=false;"
            "if(x.d&&x.d.ok){show('Ollama reachable at '+x.d.url+' \\u2014 '+x.d.models.length+' model(s).',1);}"
            "else{show((x.d&&x.d.error)||'Ollama not reachable',0);}})"
            ".catch(function(e){b.disabled=false;show('Test failed: '+e,0);});});"
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
        if urlparse(self.path).path == "/api/zen/health":
            return self._json({"ok": True, "zen": True, "port": self.server.server_address[1],
                               "mcp": mcp_status(), "mcp_tools": mcp_list_tools()})
        if urlparse(self.path).path == "/api/mcp/status":
            return self._json({"servers": mcp_status()})
        if urlparse(self.path).path == "/api/mcp/tools":
            return self._json({"tools": mcp_list_tools()})
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
        if path == "/api/mcp/call":
            data = self._body_json()
            result = mcp_call_tool(str(data.get("server") or ""), str(data.get("tool") or ""), data.get("arguments") or {})
            return self._json(result)
        if path == "/api/relay/start":
            return self._json(relay_start())
        if path == "/api/relay/status":
            return self._json(relay_status())
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
                ou = (data.get("ollama_url", "") or "").strip().rstrip("/")
                c.update({"server": server, "token": token, "agent_id": aid,
                          "name": name, "projects_root": proot})
                if ou:
                    c["ollama_url"] = ou
                save_config(c)
                os.makedirs(proot, exist_ok=True)
            except Exception as e:
                return self._json({"ok": False,
                                   "error": "Paired, but could not save: " + str(e)}, 500)
            return self._json({"ok": True, "agent_id": aid})
        if path == "/api/ollama":
            data = self._body_json()
            url = (data.get("url", "") or "").strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                return self._json({"ok": False,
                                   "error": "URL must start with http:// or https://"}, 400)
            try:
                if url:
                    c = load_config()
                    c["ollama_url"] = url
                    save_config(c)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            target = url or (load_config().get("ollama_url")
                             or DEFAULT_OLLAMA_URL).rstrip("/")
            try:
                req = urllib.request.Request(target + "/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=4) as r:
                    tags = json.loads(r.read().decode() or "{}")
                models = [m.get("name") for m in (tags.get("models") or [])
                          if isinstance(m, dict) and m.get("name")]
                return self._json({"ok": True, "url": target, "models": models})
            except Exception as e:
                return self._json({"ok": False, "url": target,
                                   "error": "Ollama not reachable: " + str(e)[:200]})
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
    ollama_url = (cfg.get("ollama_url") or DEFAULT_OLLAMA_URL).rstrip("/")
    try:
        req = urllib.request.Request(ollama_url + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=4) as r:
            tags = json.loads(r.read().decode() or "{}")
        models = [m for m in (tags.get("models") or []) if isinstance(m, dict)]
        out.append(("ollama reachable", True,
                    ollama_url + " — " + str(len(models)) + " model(s)"))
    except Exception as e:
        out.append(("ollama reachable", False,
                    ollama_url + " — " + str(e)[:120]))
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
