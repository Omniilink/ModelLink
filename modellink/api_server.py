import sys
import os
import re
import io
import base64
import datetime
import logging
import asyncio
import hmac
import hashlib
import queue
import secrets
import shlex
import subprocess
import shutil
import tempfile
import tarfile
import zipfile
import threading
import gzip
import collections


# ==========================================

# PyInstaller path resolution

# ==========================================
def _base_path():
    """Return the base directory for resource files.
    When frozen by PyInstaller, files are next to the exe.
    When running normally, it's the parent of the modellink package."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_DIR = _base_path()
sys.path.insert(0, APP_DIR)

def _build_id():
    """Short id of the running code (shown in /api/health and the app
    footer) so a stale deployment is obvious instead of mysterious.
    Prefers OMNIILINK_BUILD baked into the image, then git, then a hash
    of the shipped source files (changes with any backend or frontend
    edit). Newlines are normalized so Windows and Linux checkouts of the
    same commit hash identically."""
    try:
        env = (os.environ.get("OMNIILINK_BUILD") or "").strip()
        if env and env != "unknown":
            return env
    except Exception:
        pass
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=APP_DIR,
                             capture_output=True, text=True, timeout=10)
        sha = (out.stdout or "").strip()
        if sha:
            return sha
    except Exception:
        pass
    try:
        h = hashlib.sha1()
        for rel in ("modellink/api_server.py", "modellink/splitter.py",
                    "modellink/auth.py", "web/index.html", "web/landing.html"):
            p = os.path.join(APP_DIR, rel)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk.replace(b"\r\n", b"\n"))
        return "code-" + h.hexdigest()[:7]
    except Exception:
        pass
    return "unknown"

BUILD = _build_id()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("Omniilink")

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse, RedirectResponse, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import time
import math
import json
import socket
import ipaddress
import urllib.request
import urllib.parse

from modellink.splitter import (
    generate, PROVIDERS, MODEL_NAME, lb,
    load_all_keys, MODEL_RATE_LIMITS,
    scan_ollama_models, get_backup_model, OLLAMA_BASE_URL, set_ollama_base_url,
    ollama_model_capabilities,
    get_language, set_language, normalize_level, EFFORT_LEVELS,
    provider_health,
    web_search, WEB_SEARCH_TOOL, _add_tools_to_body, _extract_tool_calls,
    _execute_tool_call, _response_to_openai_message, run_tool_loop
)
from modellink import relay
from modellink.auth import (
    init_db, get_current_user, require_auth, create_user, authenticate_user,
    create_session, destroy_session, set_session_cookie, get_user_by_id,
    update_user_name, update_user_password, change_username, generate_api_key, get_user_api_keys,
    revoke_api_key, get_user_provider_keys, save_user_provider_key,
    delete_user_provider_key, get_custom_providers, get_custom_provider,
    save_custom_provider, delete_custom_provider, get_user_by_google_sub,
    username_taken, set_user_google_sub, get_db, get_user_from_session,
    revoke_user_sessions, record_usage, get_usage, get_api_key_id_by_key,
    all_usernames, validate_api_key
)

def _ollama_url_from_dotenv() -> str:
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(os.path.join(APP_DIR, ".env"))
        return ((vals or {}).get("OMNIILINK_OLLAMA_URL", "") or "").strip()
    except Exception:
        return ""


def _resolve_ollama_url() -> str:
    return (os.environ.get("OMNIILINK_OLLAMA_URL", "") or "").strip() or _ollama_url_from_dotenv()



# Apply the server-wide Ollama address at boot (.env/env), so the fallback and

# /api/discover use it before any admin interaction.
try:
    set_ollama_base_url(_resolve_ollama_url())
except Exception:
    pass

app = FastAPI(title="Omniilink API", version="3.0.0")
MAX_BODY_BYTES = 15 * 1024 * 1024  # 15 MB request body cap

# The one endpoint allowed past the cap: coding file uploads (the endpoint

# itself enforces a hard 50 MB while streaming to disk).
BODY_LIMIT_EXEMPT_PREFIXES = ("/api/coding/file-upload", "/api/tools/convert")
UPLOAD_LIMIT_MB = float(os.environ.get("OMNIILINK_UPLOAD_LIMIT_MB", "50") or 50)
UPLOAD_LIMIT_BYTES = int(UPLOAD_LIMIT_MB * 1024 * 1024)
UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024       # frontend chunk size for big files
UPLOAD_CHUNK_MAX_BYTES = 16 * 1024 * 1024  # server-side per-chunk ceiling
UPLOAD_PART_TTL_SECONDS = 24 * 3600        # stale chunk sessions pruned at init
UPLOAD_MAX_CHUNKS = 65536
ZIP_EXTRACT_MAX_FILES = 20000
ZIP_EXTRACT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2GB uncompressed (zip-bomb guard)


class BodyLimitMiddleware:
    """Reject request bodies over MAX_BODY_BYTES (works for Content-Length and chunked)."""
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").startswith(BODY_LIMIT_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        cl = headers.get("content-length", "")
        if cl.isdigit() and int(cl) > self.max_bytes:
            await self._send_413(send)
            return


        # Pre-read the body (if any) so the cap is enforced even when the

        # endpoint returns early (e.g. 401) without reading the body.
        has_body = (cl not in ("", "0")) or "chunked" in headers.get("transfer-encoding", "").lower()
        if not has_body:
            await self.app(scope, receive, send)
            return

        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                await self._send_413(send)
                return

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            # Body already replayed: delegate upstream and wait for the REAL

            # client disconnect. Returning a fake http.disconnect here makes

            # Starlette's StreamingResponse (spec < 2.4 path) believe the

            # client is gone, so listen_for_disconnect wins the race against

            # stream_response, the response start is never sent, and

            # BaseHTTPMiddleware raises RuntimeError("No response returned.").
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _send_413(self, send):
        body = b'{"error":"Request body too large"}'
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"connection", b"close")],
        })
        await send({"type": "http.response.body", "body": body})



# ---- Per-user bandwidth limits: 10 Mbit/s pacing + 1 GB per 6h window ----
BW_RATE_MBPS = float(os.environ.get("OMNIILINK_BW_RATE_MBPS", "100") or 100)
BW_WINDOW_MB = float(os.environ.get("OMNIILINK_BW_WINDOW_MB", "1000") or 1000)
BW_WINDOW_HOURS = float(os.environ.get("OMNIILINK_BW_WINDOW_HOURS", "6") or 6)
BW_ENABLED = (os.environ.get("OMNIILINK_BW_ENABLED", "1").strip().lower()
              not in ("0", "false", "no", "off", "disabled"))
BW_RATE_BPS = BW_RATE_MBPS * 1024 * 1024 / 8.0      # bytes per second
BW_WINDOW_BYTES = int(BW_WINDOW_MB * 1024 * 1024)
BW_WINDOW_SECONDS = BW_WINDOW_HOURS * 3600.0
BW_BURST_BYTES = max(1024 * 1024, int(BW_RATE_BPS * 2))   # ~2s of burst
BW_SPEED_SECONDS = 1.5                               # rolling window for "current speed"

# Bandwidth is accounted (and limited) on file transfers only — page loads,

# API polling and chat streaming must not eat the quota.
BW_TRANSFER_PREFIXES = ("/api/coding/file-upload", "/api/coding/download-project",
                          "/api/tools/")

_bw_lock = threading.Lock()
_bw_users = {}        # user_id -> [bucket, last_ts, deque[(ts, bytes)]]
_bw_user_cache = {}  # session token -> (user, expires monotonic)


def _bandwidth_state(user_id):
    with _bw_lock:
        st = _bw_users.get(user_id)
        if st is None:
            st = [float(BW_BURST_BYTES), time.monotonic(), collections.deque()]
            _bw_users[user_id] = st
        return st


def _bandwidth_window_used(st, now):
    dq = st[2]
    cutoff = now - BW_WINDOW_SECONDS
    while dq and dq[0][0] < cutoff:
        dq.popleft()
    return sum(b for _t, b in dq)


def _bandwidth_speed(st, now):
    """Bytes/second over the trailing BW_SPEED_SECONDS (newest entries first)."""
    dq = st[2]
    cutoff = now - BW_SPEED_SECONDS
    total = 0
    for t, b in reversed(dq):
        if t < cutoff:
            break
        total += b
    return total / BW_SPEED_SECONDS


def _bandwidth_gate(user):
    """None when the user may proceed, else a 429 payload dict. Admins are
    exempt from the cap (their usage is still tracked)."""
    if not BW_ENABLED or not user or user.get("is_admin"):
        return None
    st = _bandwidth_state(user["id"])
    with _bw_lock:
        if _bandwidth_window_used(st, time.monotonic()) >= BW_WINDOW_BYTES:
            return {"error": f"Bandwidth cap reached: {BW_WINDOW_MB:.0f} MB per "
                             f"{BW_WINDOW_HOURS:.0f} hours. Try again later."}
    return None


def _bandwidth_flush(user, n_in, n_out):
    """Record transferred bytes against the user's rolling window. Called per
    chunk (and once more at the end) so the speed readout stays live."""
    if not user or not BW_ENABLED or (not n_in and not n_out):
        return
    st = _bandwidth_state(user["id"])
    with _bw_lock:
        dq = st[2]
        dq.append((time.monotonic(), int(n_in) + int(n_out)))
        if len(dq) > 32768:

            # Coalesce adjacent entries (sum bytes, keep the older ts) so a

            # long transfer cannot grow the deque without bound. The 6h sum

            # and the age cutoff both stay exact.
            merged = collections.deque()
            while dq:
                t1, b1 = dq.popleft()
                if dq:
                    _t2, b2 = dq.popleft()
                    merged.append((t1, b1 + b2))
                else:
                    merged.append((t1, b1))
            st[2] = merged
    _bw_record_disk(user["id"], int(n_in) + int(n_out))


_BW_USAGE_PATH = os.path.join(APP_DIR, "bw_usage.json")
_BW_PERSIST_SECONDS = 5.0
_BW_MAX_SAVED = 10000
_bw_saved = {}          # user_id(int) -> [[wall_ts, bytes], ...] (dirty mirror)
_bw_dirty = False       # True once THIS process has usage the file lacks
_bw_last_persist = 0.0


def _bw_record_disk(uid, n):
    global _bw_dirty
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return
    with _bw_lock:
        _bw_saved.setdefault(uid, []).append([time.time(), n])
        _bw_dirty = True
    _bw_persist()


def _bw_persist(force=False):
    """Mirror recent usage to disk (wall-clock) so restarts keep the window.

    Only records made by THIS process mark the mirror dirty: a process that
    merely restored the file has nothing newer than what is already on disk,
    and rewriting from its stale copy would clobber rows another process
    wrote since (the last writer wins, so the freshest rows could vanish)."""
    global _bw_last_persist, _bw_dirty
    now_wall = time.time()
    if not force and (not _bw_dirty
                      or now_wall - _bw_last_persist < _BW_PERSIST_SECONDS):
        return
    _bw_last_persist = now_wall
    cutoff = now_wall - BW_WINDOW_SECONDS
    with _bw_lock:
        for uid in list(_bw_saved.keys()):
            clean = []
            for row in _bw_saved[uid]:
                try:
                    t, b = float(row[0]), int(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if t >= cutoff:
                    clean.append([t, b])
            rows = clean
            while len(rows) > _BW_MAX_SAVED:
                merged = []
                for i in range(0, len(rows), 2):
                    if i + 1 < len(rows):
                        merged.append([rows[i][0], rows[i][1] + rows[i + 1][1]])
                    else:
                        merged.append(rows[i])
                rows = merged
            if rows:
                _bw_saved[uid] = rows
            else:
                del _bw_saved[uid]
        snapshot = {str(uid): rows for uid, rows in _bw_saved.items()}

        # Cleared under the lock: rows recorded from here on re-set it.
        _bw_dirty = False
    try:
        tmp = _BW_USAGE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": snapshot}, f)
        os.replace(tmp, _BW_USAGE_PATH)
    except OSError:
        with _bw_lock:
            _bw_dirty = True


def _bw_restore():
    """Rebuild in-memory windows from disk after a restart."""
    try:
        with open(_BW_USAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return
    mono_now = time.monotonic()
    wall_now = time.time()
    with _bw_lock:
        for uid_s, rows in entries.items():
            try:
                uid = int(uid_s)
            except (TypeError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            valid = []
            for row in rows:
                try:
                    t, b = float(row[0]), int(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                age = wall_now - t
                if age < 0 or age > BW_WINDOW_SECONDS:
                    continue
                valid.append([t, b])
            if not valid:
                continue
            _bw_saved[uid] = valid
            st = _bw_users.get(uid)
            if st is None:
                st = [float(BW_BURST_BYTES), mono_now, collections.deque()]
                _bw_users[uid] = st
            dq = st[2]
            for t, b in valid:
                dq.append((mono_now - (wall_now - t), b))


try:
    _bw_restore()
except Exception:
    pass


def _bw_flush_loop():
    """Background flusher: rows recorded since the last throttled write would
    otherwise sit in memory until the NEXT transfer, and a restart in between
    loses them (usage reported before a restart but not after)."""
    while True:
        time.sleep(1.0)
        try:
            with _bw_lock:
                dirty = _bw_dirty
            if dirty:
                _bw_persist()
        except Exception:
            pass


try:
    _bw_thread = threading.Thread(target=_bw_flush_loop, daemon=True)
    _bw_thread.start()
except Exception:
    pass


def _bw_pace_seconds(st, n):
    """Token-bucket math: seconds the caller must wait before n bytes go out."""
    now = time.monotonic()
    st[0] = min(float(BW_BURST_BYTES), st[0] + (now - st[1]) * BW_RATE_BPS)
    st[1] = now
    if st[0] >= n:
        st[0] -= n
        return 0.0
    need = (n - st[0]) / BW_RATE_BPS
    st[0] = 0.0
    return need


async def _bandwidth_pace(user, n):
    """Admins are exempt from pacing (their speed is still measured)."""
    if not BW_ENABLED or not user or user.get("is_admin") or n <= 0:
        return
    st = _bandwidth_state(user["id"])
    with _bw_lock:
        need = _bw_pace_seconds(st, n)
    if need > 0:
        await asyncio.sleep(need)


def _bw_resolve_user(headers):
    """Session cookie (or API key) -> user, cached for 60s. None if anonymous."""
    cookie = headers.get("cookie", "") or ""
    token = None
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("omnilink_token="):
            token = part.split("=", 1)[1].strip()
            break
    api_key_id = None
    if not token:
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ml-"):
            api_key_id = validate_api_key(auth[7:])
    if not token and api_key_id is None:
        return None
    now = time.monotonic()
    with _bw_lock:
        if len(_bw_user_cache) > 5000:
            _bw_user_cache.clear()
        hit = _bw_user_cache.get(token) if token else None
        if hit and hit[1] > now:
            return hit[0]
    user = get_user_from_session(token) if token else None
    if user is None and api_key_id is not None:
        user = get_user_by_id(api_key_id)
    if user is not None:
        with _bw_lock:
            _bw_user_cache[token if token else f"ak:{user['id']}"] = (user, now + 60.0)
    return user

class BandwidthLimitMiddleware:
    """Paces each user's file transfers to BW_RATE_MBPS and 429s them once they
    have moved BW_WINDOW_MB inside the rolling BW_WINDOW_HOURS window.

    Only upload/download endpoints are accounted — page loads and API calls
    never touch the quota. Admins are unlimited but still measured (so the
    indicator can show their usage and current speed). Registered inside
    CORS/SecurityHeaders so the 429 still carries those headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        transfer = path.startswith(BW_TRANSFER_PREFIXES) and not path.startswith("/api/tools/youtube/download")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}

        # Resolving the user is only needed for transfers; a page refresh

        # must not touch bandwidth state at all.
        user = _bw_resolve_user(headers) if transfer else None
        limited = bool(user) and not user.get("is_admin")

        if limited:
            deny = _bandwidth_gate(user)
            if deny is not None:
                await self._send_429(send, deny)
                return

        n_in = 0
        n_out = 0

        async def recv_wrapper():
            nonlocal n_in
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"") or b""
                try:
                    n_in += len(chunk)
                except Exception:
                    pass
                if chunk and limited:

                    # Pace uploads too, then flush so the speed readout is live.
                    await _bandwidth_pace(user, len(chunk))
                    _bandwidth_flush(user, len(chunk), 0)
                    n_in = 0
            return message

        async def send_wrapper(message):
            nonlocal n_out
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"") or b""
                try:
                    n_out += len(chunk)
                except Exception:
                    pass
                if chunk and user is not None:
                    if limited:
                        await _bandwidth_pace(user, len(chunk))
                    _bandwidth_flush(user, 0, len(chunk))
                    n_out = 0
            await send(message)

        try:
            await self.app(scope, recv_wrapper, send_wrapper)
        finally:
            if user is not None and (n_in or n_out):
                _bandwidth_flush(user, n_in, n_out)

    async def _send_429(self, send, payload):
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start", "status": 429,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"connection", b"close")],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BandwidthLimitMiddleware)

app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://omniilink.dpdns.org",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Converter origin for connect-src, refreshed slowly so .env edits take

# effect without a restart and responses don't pay a file read each time.
_csp_extra = {"value": "", "ts": 0.0}


def _csp_connect_extra():
    now = time.monotonic()
    cur = _csp_extra
    if now - cur["ts"] < 60.0:
        return cur["value"]
    origin = ""
    try:
        url = _converter_url(
            (os.environ.get("OMNIILINK_CONVERTER_API", "") or "").strip(),
            _converter_url_from_dotenv())
        parts = urllib.parse.urlparse(url)
        if parts.scheme in ("http", "https") and parts.hostname:
            origin = f"{parts.scheme}://{parts.hostname}"
            if parts.port and parts.port not in (80, 443):
                origin += f":{parts.port}"
            origin.encode("ascii")  # IDN/unicode origins can't go in a header
    except Exception:
        origin = ""
    _csp_extra["value"] = origin
    _csp_extra["ts"] = now
    return origin


def _csp_policy():
    extra = _csp_connect_extra()

    # blob: is required by the Models viewport: three.js decodes embedded

    # GLB textures into blob: URLs and fetch()es them, which connect-src

    # blocks without it (that surfaced as "Could not load model: Failed to fetch").
    connect = "connect-src 'self' cloudflareinsights.com" + (f" {extra}" if extra else "") + " blob:"
    return ("default-src 'self'; script-src 'self' 'unsafe-inline' static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: *.ytimg.com blob:; "
            f"{connect}; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeadersMiddleware:
    """Raw ASGI middleware — avoids nested BaseHTTPMiddleware + StreamingResponse bug in Starlette 1.6."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
        is_https = request_headers.get("x-forwarded-proto", "").lower() == "https"

        # The HTML preview endpoint is MEANT to be framed (sandboxed iframe

        # with an opaque origin, so it cannot touch app storage). Framing

        # guards must not apply to it or the browser refuses to render it.
        is_preview = scope.get("path", "") in ("/api/coding/preview", "/api/coding/remote-preview")
        try:
            _qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        except Exception:
            _qs = {}

        # ?tab=1 is the real-tab preview: it must run EXACTLY like the file

        # opened from disk (pointer lock, keys, fullscreen), so neither the

        # sandbox nor the app-wide framing ban may apply to it.
        is_preview_tab = is_preview and _qs.get("tab") == ["1"]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.decode("latin-1").lower() for k, _ in headers}
                if "x-content-type-options" not in existing:
                    headers.append((b"x-content-type-options", b"nosniff"))
                if not is_preview and "x-frame-options" not in existing:
                    headers.append((b"x-frame-options", b"DENY"))
                if "referrer-policy" not in existing:
                    headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                if "permissions-policy" not in existing:
                    headers.append((b"permissions-policy", b"camera=(), microphone=(), geolocation=()"))
                if "content-security-policy" not in existing:
                    if is_preview_tab:
                        pass
                    elif is_preview:

                        # Tight sandbox instead of the app-wide framing ban
                        headers.append((b"content-security-policy", b"sandbox allow-scripts"))
                    else:

                        # static.cloudflareinsights.com: Cloudflare's own RUM

                        # beacon auto-injected by the proxy; harmless analytics.

                        # connect-src also allows the configured converter API

                        # (in-browser YouTube downloads) and img-src the

                        # YouTube thumbnail CDN.
                        headers.append((b"content-security-policy",
                                        _csp_policy().encode("ascii")))
                if is_https and "strict-transport-security" not in existing:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

app.add_middleware(SecurityHeadersMiddleware)


# /api/logs streams the log; /api/logs/clear manages it - neither should

# log itself, or clearing would instantly re-add the clear request's line.
_SKIP_LOG_PATHS = {"/api/health", "/api/logs", "/api/logs/clear", "/api/bandwidth", "/favicon.svg", "/favicon.ico"}

@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    path = request.url.path
    if path in _SKIP_LOG_PATHS or path.startswith("/static") or path == "/":
        return await call_next(request)
    start = time.time()
    response = await call_next(request)
    dur_ms = int((time.time() - start) * 1000)
    method = request.method
    status = response.status_code
    user = getattr(request.state, "user", None)
    uname = user.get("username", "-") if user else "-"
    if status >= 500:
        level = "ERROR"
    elif status >= 400:
        level = "WARNING"
    else:
        level = "INFO"
    msg = f"{method} {path} {status} {dur_ms}ms user={uname}"

    # Emulate logger format so frontend level detection works
    log_broadcaster.emit(f"{time.strftime('%H:%M:%S')} [{level}] [HTTP] {msg}")
    return response

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[UNHANDLED] {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(status_code=404, content={"error": "Not found"})


# ==========================================

# Network Usage Tracking (persisted JSON)

# ==========================================
NETWORK_USAGE_PATH = os.path.join(APP_DIR, "network_usage.json")

class _NetworkUsage:
    """Counts HTTP bytes in/out per path. Best-effort, never raises."""
    def __init__(self, path: str):
        self.path = path
        self.data = {
            "updated_at": "",
            "total_bytes_in": 0,
            "total_bytes_out": 0,
            "total_requests": 0,
            "by_path": {},
        }
        self._last_flush = 0.0
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k in ("total_bytes_in", "total_bytes_out", "total_requests"):
                        v = saved.get(k, 0)
                        if isinstance(v, (int, float)):
                            self.data[k] = int(v)
                    bp = saved.get("by_path")
                    if isinstance(bp, dict):
                        self.data["by_path"] = {
                            str(p): {
                                "in": int(v.get("in", 0)),
                                "out": int(v.get("out", 0)),
                                "requests": int(v.get("requests", 0)),
                            }
                            for p, v in list(bp.items())[:500]
                            if isinstance(v, dict)
                        }
        except Exception:
            pass

    def record(self, path: str, n_in: int, n_out: int):
        try:
            d = self.data
            d["total_bytes_in"] += max(0, int(n_in))
            d["total_bytes_out"] += max(0, int(n_out))
            d["total_requests"] += 1
            entry = d["by_path"].get(path)
            if entry is None:
                if len(d["by_path"]) >= 500:

                    # Drop the smallest entry to stay bounded
                    victim = min(d["by_path"].items(),
                                 key=lambda kv: kv[1].get("requests", 0))[0]
                    del d["by_path"][victim]
                entry = d["by_path"][path] = {"in": 0, "out": 0, "requests": 0}
            entry["in"] += max(0, int(n_in))
            entry["out"] += max(0, int(n_out))
            entry["requests"] += 1
            now = time.time()
            if now - self._last_flush >= 5:
                self.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._last_flush = time.time()
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

network_usage = _NetworkUsage(NETWORK_USAGE_PATH)


class NetworkUsageMiddleware:
    """Raw ASGI middleware — counts request/response body bytes per path."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "?") or "?"
        n_in = 0
        n_out = 0

        async def recv_wrapper():
            nonlocal n_in
            message = await receive()
            if message["type"] == "http.request":
                try:
                    n_in += len(message.get("body", b"") or b"")
                except Exception:
                    pass
            return message

        async def send_wrapper(message):
            nonlocal n_out
            if message["type"] == "http.response.body":
                try:
                    n_out += len(message.get("body", b"") or b"")
                except Exception:
                    pass
            await send(message)

        try:
            await self.app(scope, recv_wrapper, send_wrapper)
        finally:
            network_usage.record(path, n_in, n_out)

app.add_middleware(NetworkUsageMiddleware)


# ==========================================

# Per-User Data Isolation

# ==========================================
def _safe_username(username: str) -> str:
    """Filesystem-safe folder name for a username ([a-z0-9_-] only, 1-32 chars)."""
    name = re.sub(r"[^a-z0-9_-]", "_", (username or "").lower())[:32].strip("_-")
    return name or "user"

def _is_within(base: str, target: str) -> bool:
    """True if target (after realpath) is base itself or inside base."""
    base_real = os.path.normcase(os.path.realpath(base))
    target_real = os.path.normcase(os.path.realpath(target))
    if target_real == base_real:
        return True
    return target_real.startswith(base_real.rstrip(os.sep) + os.sep)

def _require_auth(request: Request):
    """Return (user, None) or (None, 401 JSONResponse)."""
    user = get_current_user(request)
    if not user:
        return None, JSONResponse(status_code=401, content={"error": "Authentication required"})
    request.state.user = user
    return user, None

def _require_admin(request: Request):
    """Return (user, None) or (None, 401/403 JSONResponse)."""
    user, err = _require_auth(request)
    if err:
        return None, err
    if not user.get("is_admin"):
        return None, JSONResponse(status_code=403, content={"error": "Admin access required"})
    return user, None

def _request_is_https(request: Request) -> bool:
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    if '"scheme":"https"' in request.headers.get("cf-visitor", ""):
        return True
    return request.url.scheme == "https"

def _bad_path(path: str) -> bool:
    """Reject null bytes, absurd lengths and `..` traversal in filesystem paths."""
    if not path or len(path) > 1024:
        return True
    if "\x00" in path:
        return True
    if any(seg == ".." for seg in path.replace("\\", "/").split("/")):
        return True
    return False

async def _safe_json(request: Request):
    """Parse JSON body; returns (data, None) or (None, 400 response)."""
    try:
        data = await request.json()
    except Exception:
        return None, JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    return data, None

async def _optional_json_body(request: Request):
    """Parse an optional JSON body; an empty body means {}.

    Pairing invitations are created from a button that historically sent no
    body at all. Requiring a JSON document would break those callers, while
    still accepting an explicit owner_username when present.
    """
    raw = b""
    body_fn = getattr(request, "body", None)
    if callable(body_fn):
        try:
            raw = await body_fn()
        except Exception:
            raw = b""
    if not (raw or b"").strip():
        # No raw body (or a test double without one): fall back to parsed
        # JSON when available, otherwise treat the invitation as self-owned.
        try:
            data = await request.json()
        except Exception:
            data = {}
        return (data or {}), None
    return await _safe_json(request)


def _request_ip(request: Request) -> str:
    """Best-effort client IP for rate limits. Tolerates request doubles."""
    try:
        header_ip = request.headers.get("cf-connecting-ip", "")
    except Exception:
        header_ip = ""
    if header_ip:
        return header_ip
    try:
        client = getattr(request, "client", None)
        return client.host if client else ""
    except Exception:
        return ""

def _user_quota_bytes() -> int:
    try:
        return int(os.environ.get("OMNIILINK_USER_QUOTA", str(1 * 1024 * 1024 * 1024)))
    except ValueError:
        return 1 * 1024 * 1024 * 1024

def _dir_size(path: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total

def _check_quota(user, user_id: str, incoming_bytes: int = 0):
    """Non-admin disk quota check. Returns JSONResponse error or None."""
    if user.get("is_admin"):
        return None
    used = _dir_size(_user_data_dir(user_id))
    if used + incoming_bytes > _user_quota_bytes():
        return JSONResponse(status_code=413, content={"error": "Storage quota exceeded"})
    return None

def _clear_path_entry(path: str) -> bool:
    """Delete a file/symlink blocking a directory path. If deletion is refused
    (e.g. a locked file), move it aside instead. Returns True once it's gone."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        pass
    try:
        os.rename(path, path + ".stray-" + str(int(time.time())))
        return True
    except Exception:
        return False


def _ensure_dir(path: str) -> str:
    """mkdir -p that never raises because a file sits where a directory goes.

    os.makedirs(path, exist_ok=True) raises FileExistsError when `path` exists
    as a file/symlink, and NotADirectoryError when an ancestor component is a
    file. A file anywhere on that path is never valid user data — a user's
    files always live inside a directory named after them — so it is removed
    (or moved aside) and the folder is created. Without this, one stray file
    500s every endpoint that touches the account (GET /api/projects,
    POST /api/projects/create, ...).
    """
    for _ in range(6):
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except FileExistsError:

            # `path` itself is a file/symlink, not a directory.
            if not _clear_path_entry(path):
                return path
        except NotADirectoryError:

            # An ancestor component is a file; clear the nearest one.
            offender = None
            p = path
            while p and p != os.path.dirname(p):
                if os.path.lexists(p) and not os.path.isdir(p):
                    offender = p
                    break
                p = os.path.dirname(p)
            if not offender or not _clear_path_entry(offender):
                return path
        except OSError:
            return path
    return path


def _heal_user_dirs():
    """Startup repair: a stray file/symlink sitting at user_data/<username>
    blocks that account's folder (and every endpoint that touches it). Clear
    it once so the folder can be created. Only non-directories are touched —
    a real user folder is always a directory."""
    base = os.path.join(APP_DIR, "user_data")
    if not os.path.isdir(base):
        return
    try:
        conn = get_db()
        names = [r["username"] for r in conn.execute("SELECT username FROM users").fetchall()]
        conn.close()
    except Exception:
        return
    for uname in names:
        d = os.path.join(base, _safe_username(uname))
        if os.path.lexists(d) and not os.path.isdir(d):
            if _clear_path_entry(d):
                logger.warning(f"[HEAL] cleared stray file blocking {d}")


def _user_data_dir(user_id: str) -> str:
    """Get or create per-user data directory (user_id must already be sanitized)."""
    safe = _safe_username(user_id)
    return _ensure_dir(os.path.join(APP_DIR, "user_data", safe))


def _user_projects_dir(user_id: str) -> str:
    """Get per-user projects directory."""
    return _ensure_dir(os.path.join(_user_data_dir(user_id), "projects"))

def _migrate_user_data_dirs():
    """Rename legacy numeric user_data/<id> folders to user_data/<username>."""
    base = os.path.join(APP_DIR, "user_data")
    if not os.path.isdir(base):
        return
    conn = get_db()
    try:
        rows = [(r["id"], r["username"]) for r in conn.execute("SELECT id, username FROM users").fetchall()]
    finally:
        conn.close()

    # Phase 1: numeric id folders -> temp names (avoids clashes with numeric usernames)
    for uid, _uname in rows:
        old = os.path.join(base, str(uid))
        tmp = os.path.join(base, f"_tmpuid_{uid}")
        if uid != "default" and os.path.isdir(old) and not os.path.exists(tmp):
            try:
                os.rename(old, tmp)
            except OSError as e:
                logger.warning(f"[MIGRATE] Could not rename {old}: {e}")

    # Phase 2: temp names -> sanitized usernames
    for uid, uname in rows:
        tmp = os.path.join(base, f"_tmpuid_{uid}")
        new = os.path.join(base, _safe_username(uname))
        if not os.path.isdir(tmp):
            continue
        if os.path.exists(new):
            try:
                for entry in os.listdir(tmp):
                    os.rename(os.path.join(tmp, entry), os.path.join(new, entry))
                os.rmdir(tmp)
            except OSError:
                pass
        else:
            try:
                os.rename(tmp, new)
            except OSError as e:
                logger.warning(f"[MIGRATE] Could not rename {tmp}: {e}")

_migrate_user_data_dirs()
_heal_user_dirs()


# Per-user coding states: {user_key: {"folder": ..., "settings": {...}}}
_coding_states = {}

def _coding_settings_path(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "coding_settings.json")

def _load_coding_settings(user_id: str, defaults: dict) -> dict:
    """Best-effort load of persisted per-user coding settings."""
    try:
        p = _coding_settings_path(user_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if k in defaults:
                        defaults[k] = v
    except Exception:
        pass
    return defaults

def _save_coding_settings(user_id: str, settings: dict):
    """Best-effort persist of per-user coding settings."""
    try:
        with open(_coding_settings_path(user_id), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except Exception:
        pass

def _coding_folder_path(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "coding_folder.txt")

def _load_coding_folder(user_id: str) -> Optional[str]:
    """Restore the open project folder across server restarts.
    Returns None unless the stored folder still exists (stale = closed)."""
    try:
        p = _coding_folder_path(user_id)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            folder = f.read().strip()
        if folder and os.path.isdir(folder):
            return folder
    except Exception:
        pass
    return None

def _save_coding_folder(user_id: str, folder: Optional[str]):
    """Best-effort persist (or clear, when None) the open project folder."""
    try:
        p = _coding_folder_path(user_id)
        if not folder:
            if os.path.exists(p):
                os.remove(p)
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(folder)
    except Exception:
        pass



# ---- File version history: every coding write/patch snapshots the previous

# content first, so a bad edit can be undone from the file's history. ----
_CODING_VERSION_CAP = 25                     # versions kept per file
_CODING_VERSION_DIR_CAP = 20 * 1024 * 1024   # 20MB total per user


def _coding_versions_dir(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "coding_versions")


def _coding_version_dir(user_id: str, path: str, folder: str = "") -> str:
    """One directory per tracked file in one project folder.

    The key hashes both the absolute project folder and the project-relative
    path, so the same filename in two projects never shares history.
    """
    import hashlib
    scope = os.path.abspath(folder or "") if folder else ""
    key = hashlib.sha1((scope + "\n" + (path or "")).encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(_coding_versions_dir(user_id), key)


def _coding_list_versions(user_id: str, path: str, folder: str = "") -> list:
    out = []
    try:
        d = _coding_version_dir(user_id, path, folder)
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if not name.endswith(".json") or not os.path.isfile(full):
                continue
            try:
                with open(full, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and "content" in rec and "ts" in rec:
                    out.append({"ts": rec.get("ts"), "size": rec.get("size", 0)})
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def _coding_get_version(user_id: str, path: str, ts: float, folder: str = "") -> Optional[str]:
    try:
        prefix = f"{float(ts):.6f}_"
        for name in os.listdir(_coding_version_dir(user_id, path, folder)):
            if not name.startswith(prefix):
                continue
            full = os.path.join(_coding_version_dir(user_id, path, folder), name)
            with open(full, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if isinstance(rec, dict) and "content" in rec:
                return rec["content"]
    except Exception:
        pass
    return None


def _coding_prune_versions(user_id: str, path: str, folder: str = "") -> None:
    """Keep the newest N per file, then the newest overall under the size cap."""
    d = _coding_version_dir(user_id, path, folder)
    files = []
    try:
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if name.endswith(".json") and os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        ts = json.load(f).get("ts", 0)
                except Exception:
                    ts = 0
                files.append((ts, full))
    except Exception:
        return
    files.sort(key=lambda x: x[0], reverse=True)
    for _ts, full in files[_CODING_VERSION_CAP:]:
        try:
            os.remove(full)
        except OSError:
            pass
    root = _coding_versions_dir(user_id)
    all_files = []
    total = 0
    try:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                full = os.path.join(dirpath, name)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    continue
                total += sz
                all_files.append((os.path.getmtime(full), full))
    except Exception:
        return
    all_files.sort(key=lambda x: x[0])
    for _mt, full in all_files:
        if total <= _CODING_VERSION_DIR_CAP:
            break
        try:
            sz = os.path.getsize(full)
            os.remove(full)
            total -= sz
        except OSError:
            continue


def _coding_save_version(user_id: str, path: str, content: str, folder: str = ""):
    """Snapshot content BEFORE an edit. No-ops are skipped; oldest versions are
    pruned once the per-file or total cap is hit. Returns the version ts (for
    the undo log) or None."""
    if not content:
        return None
    try:
        d = _coding_version_dir(user_id, path, folder)
        os.makedirs(d, exist_ok=True)
        ts = time.time()
        rec = {"ts": ts, "size": len(content.encode("utf-8", "ignore")), "content": content}
        with open(os.path.join(d, f"{ts:.6f}_{secrets.token_hex(4)}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f)
        _coding_prune_versions(user_id, path, folder)
        return ts
    except Exception:
        return None


# ---- Project-wide versions: a whole-project snapshot (files + chat) taken
# after a coding turn that changed code, named by the AI's own summary. Stored
# gzipped under user_data/<user>/project_versions/, so it counts against the
# cloud quota. Restoring reverts the files AND the coding chat log.
_PROJECT_VERSION_MAX_KEEP = 50
_PROJECT_VERSION_DEFAULT_KEEP = 5
_PROJECT_VERSION_MAX_FILE_BYTES = 512 * 1024
_PROJECT_VERSION_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".hg", ".svn", ".venv", "venv",
    "env", ".next", ".nuxt", ".turbo", ".cache", ".pytest_cache",
    ".mypy_cache", "dist", "build", "out", ".omniilink", "target", "vendor",
    "coverage", ".idea", ".vscode", "bower_components", ".eggs", ".gradle",
}


def _project_versions_dir(user_id: str, folder: str) -> str:
    import hashlib
    key = hashlib.sha1(os.path.abspath(folder or "").encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(_user_data_dir(user_id), "project_versions", key)


def _project_snapshot(folder: str) -> dict:
    """{relpath: text} for every text file, skipping heavy dirs and binaries."""
    out = {}
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs
                       if d not in _PROJECT_VERSION_SKIP_DIRS and not d.startswith(".git")]
            for name in files:
                full = os.path.join(root, name)
                try:
                    if os.path.getsize(full) > _PROJECT_VERSION_MAX_FILE_BYTES:
                        continue
                    with open(full, "rb") as f:
                        raw = f.read()
                    if b"\x00" in raw[:8000]:
                        continue
                    out[os.path.relpath(full, folder).replace("\\", "/")] = \
                        raw.decode("utf-8", "replace")
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _project_version_stats(prev: dict, cur: dict) -> tuple:
    """Per-file character additions/removals vs the previous snapshot."""
    import difflib
    paths = sorted(set(prev) | set(cur))
    stats = []
    tot_add = tot_rem = 0
    for p in paths:
        a = prev.get(p, "")
        b = cur.get(p, "")
        if a == b:
            continue
        dl = list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))
        added = sum(len(l) for l in dl if l.startswith("+") and not l.startswith("+++"))
        removed = sum(len(l) for l in dl if l.startswith("-") and not l.startswith("---"))
        status = "added" if p not in prev else ("deleted" if p not in cur else "modified")
        stats.append({"path": p, "added": added, "removed": removed, "status": status})
        tot_add += added
        tot_rem += removed
    return stats, tot_add, tot_rem


def _unified_diff(a: str, b: str, path: str, context: int = 3) -> str:
    """Unified diff of two texts, labelled a/<path> → b/<path>."""
    import difflib
    lines = difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(),
                                 fromfile=f"a/{path}", tofile=f"b/{path}",
                                 n=context, lineterm="")
    return "\n".join(lines)


def _project_version_load(user_id: str, folder: str, vid: str):
    try:
        p = os.path.join(_project_versions_dir(user_id, folder),
                         os.path.basename(vid or "") + ".json.gz")
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _project_version_list(user_id: str, folder: str) -> list:
    out = []
    d = _project_versions_dir(user_id, folder)
    try:
        for name in os.listdir(d):
            if not name.endswith(".json.gz"):
                continue
            try:
                with gzip.open(os.path.join(d, name), "rt", encoding="utf-8") as f:
                    rec = json.load(f)
                out.append({
                    "id": rec.get("id", name[:-8]),
                    "ts": rec.get("ts", 0),
                    "short": rec.get("short", ""),
                    "paragraph": rec.get("paragraph", ""),
                    "files": len(rec.get("files", {})),
                    "added": rec.get("added", 0),
                    "removed": rec.get("removed", 0),
                })
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def _project_version_prune(user_id: str, folder: str, keep: int) -> None:
    d = _project_versions_dir(user_id, folder)
    try:
        names = sorted((n for n in os.listdir(d) if n.endswith(".json.gz")), reverse=True)
    except Exception:
        return
    for n in names[max(1, int(keep)):]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def _project_version_save(user_id: str, folder: str, short: str, paragraph: str, chat=None):
    """Snapshot the project now. Returns (record, compressed_bytes) — or
    (None, 0) when the snapshot is identical to the newest version, so
    liberal record-after-every-message callers never spam the store."""
    cur = _project_snapshot(folder)
    existing = _project_version_list(user_id, folder)
    prev = {}
    if existing:
        rec0 = _project_version_load(user_id, folder, existing[0]["id"])
        if rec0:
            prev = rec0.get("files", {}) or {}
        if prev == cur:
            return None, 0
    stats, tot_add, tot_rem = _project_version_stats(prev, cur)
    ts = time.time()
    vid = f"{int(ts * 1000):013d}-{secrets.token_hex(3)}"
    rec = {
        "id": vid,
        "ts": ts,
        "short": (short or "").strip()[:200] or "Code changed",
        "paragraph": (paragraph or "").strip()[:2000],
        "files": cur,
        "stats": stats,
        "added": tot_add,
        "removed": tot_rem,
        "chat": chat if isinstance(chat, list) else [],
        "project": os.path.basename(os.path.normpath(folder)),
    }
    d = _project_versions_dir(user_id, folder)
    os.makedirs(d, exist_ok=True)
    raw = json.dumps(rec, ensure_ascii=False).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=6)
    with open(os.path.join(d, vid + ".json.gz"), "wb") as f:
        f.write(blob)
    return rec, len(blob)


def _project_versions_keep(user_id: str) -> int:
    try:
        st = _get_coding_state(user_id)
        k = int(st.get("settings", {}).get("versions_to_keep", _PROJECT_VERSION_DEFAULT_KEEP))
    except (TypeError, ValueError):
        k = _PROJECT_VERSION_DEFAULT_KEEP
    return max(1, min(_PROJECT_VERSION_MAX_KEEP, k))


def _user_prefs_path(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "prefs.json")

def _load_user_prefs(user_id: str) -> dict:
    """Best-effort load of per-user preferences (language, ...)."""
    try:
        p = _user_prefs_path(user_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}

def _save_user_pref(user_id: str, key: str, value):
    """Best-effort persist of a single per-user preference."""
    try:
        prefs = _load_user_prefs(user_id)
        prefs[key] = value
        with open(_user_prefs_path(user_id), "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception:
        pass



# ---- cloud chat history (per account, syncs across devices) ----
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,80}")
_CHAT_MAX_BYTES = 3_000_000

def _user_chats_path(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "chats.json")

def _load_user_chats(user_id: str) -> dict:
    try:
        p = _user_chats_path(user_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}

def _save_user_chats(user_id: str, chats: dict):
    try:
        p = _user_chats_path(user_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(chats, f)
        os.replace(tmp, p)
    except Exception:
        pass

def _prune_chats(chats: dict, limit: int = 300) -> dict:
    items = sorted(chats.items(),
                   key=lambda kv: float((kv[1] or {}).get("updated") or 0),
                   reverse=True)
    return dict(items[:limit])


# The language value is interpolated into the system prompt, so it is

# validated against a strict alphabet instead of being trusted.
_LANGUAGE_RE = re.compile(r"[A-Za-z]{3,20}")

def _user_language(user_id: str) -> str:
    lang = _load_user_prefs(user_id).get("language")
    if isinstance(lang, str) and _LANGUAGE_RE.fullmatch(lang.strip()):
        return lang.strip()
    return get_language()


def _zoneinfo(name):
    """ZoneInfo for an IANA name, or None when unknown/unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return None


def _valid_timezone(name) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if len(name) > 64 or any(c in name for c in "\n\r\0"):
        return False
    return _zoneinfo(name.strip()) is not None


def _user_timezone(user_id: str) -> str:
    """Saved IANA timezone for the account, or '' (use the server clock)."""
    tz = _load_user_prefs(user_id).get("timezone")
    if isinstance(tz, str) and tz.strip() and _valid_timezone(tz):
        return tz.strip()
    return ""


def _localized_now(user_id: str):
    """(formatted, iso) local date/time for the account's timezone."""
    tz = _user_timezone(user_id)
    zone = _zoneinfo(tz) if tz else None
    now = datetime.datetime.now(zone) if zone is not None else datetime.datetime.now()
    label = tz or "server local time"
    return now.strftime("%A, %d %B %Y, %H:%M") + f" ({label})", now.isoformat()


def _time_line(user_id: str) -> str:
    """Fresh 'Current date and time' line injected into prompts so the model
    always knows the real time (computed per request, never cached)."""
    return "Current date and time: " + _localized_now(user_id)[0] + "."

def _coding_state_evict(user_id: str) -> None:
    """Drop one cached coding session.

    Usernames can be recycled. Without eviction, a new account taking a
    vacated username would inherit the previous account's open folder and
    settings from this in-process cache.
    """
    _coding_states.pop(user_id, None)


def _get_coding_state(user_id: str) -> dict:
    if user_id not in _coding_states:
        settings = _load_coding_settings(user_id, {
            "auto_create": False,
            "auto_edit": False,
            "auto_command": False,
            "auto_attach": False,
            "plan_mode": False,
            "versions_to_keep": 5,
            "max_file_size": 100000,
            "system_prompt": DEFAULT_CODING_PROMPT
        })
        _coding_states[user_id] = {
            "folder": _load_coding_folder(user_id),
            "settings": settings
        }
    return _coding_states[user_id]


# ==========================================

# Provider Key Resolution

# ==========================================
def health_scope(request):
    """Health bucket for the caller's key set: admin shares the server keys,
    everyone else gets their own so one dead key never tints another user."""
    user = get_current_user(request)
    if not user:
        return "anon"
    return "server" if user.get("is_admin") else f"u{user['id']}"


def get_provider_keys(request):
    """Get provider keys: own DB keys for users; server .env keys for admin only."""
    user = get_current_user(request)
    if user:
        db_keys = get_user_provider_keys(user["id"])
        if db_keys:
            result = {}
            for provider, key_value in db_keys.items():
                if key_value and isinstance(key_value, str) and len(key_value) > 10:
                    result[provider] = [{"env_key": f"{provider.upper()}_API_KEY", "key": key_value}]
            if result:
                return result
        if user.get("is_admin"):
            return load_all_keys()
        return {}
    return {}


# ==========================================

# Log Broadcasting via SSE

# ==========================================
def _log_visible_to(msg: str, username: Optional[str]) -> bool:
    """Per-user terminal filter. Admins (username None) see everything.
    Others only see lines tagged with their own `user=<name>` — untagged
    system/server lines stay admin-only so internals never leak out."""
    if not username:
        return True
    i = msg.find("user=")
    if i == -1:
        return False
    rest = msg[i + 5:]
    return rest == username or rest.startswith(username + " ")


class LogBroadcaster:
    def __init__(self):

        # Entries are (queue, username-or-None-for-admin) tuples.
        self._queues: List[tuple] = []
        self._lock = asyncio.Lock()
        self._history: List[str] = []
        self._history_max = 500
        self._file_lock = threading.Lock()
        self._log_file = os.path.join(APP_DIR, "modellink", "server.log")
        self._log_file_max_bytes = 2 * 1024 * 1024
        self._trim_log_file()

    def _trim_log_file(self):
        """Best-effort: keep the persisted log file bounded (last ~1500 lines)."""
        try:
            if not os.path.exists(self._log_file):
                return
            if os.path.getsize(self._log_file) <= self._log_file_max_bytes:
                return
            with open(self._log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(self._log_file, "w", encoding="utf-8") as f:
                f.writelines(lines[-1500:])
        except Exception:
            pass

    def _append_to_file(self, message: str):
        try:
            with self._file_lock:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(message + "\n")
        except Exception:
            pass

    def _load_file_tail(self, n: int = 200) -> List[str]:
        try:
            if not os.path.exists(self._log_file):
                return []
            with open(self._log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return [ln.rstrip("\n") for ln in lines[-n:] if ln.strip()]
        except Exception:
            return []

    def subscribe(self, username: Optional[str] = None) -> queue.Queue:
        if len(self._queues) >= 64:
            raise RuntimeError("Too many log subscriptions")
        q = queue.Queue(maxsize=200)

        # Seed from the persisted log file so the terminal survives restarts
        if not self._history:
            self._history = self._load_file_tail(500)[-self._history_max:]

        # Replay recent history so the terminal isn't empty on connect
        for msg in self._history:
            if not _log_visible_to(msg, username):
                continue
            try:
                q.put_nowait(msg)
            except queue.Full:
                break
        self._queues.append((q, username))
        return q

    def unsubscribe(self, q: queue.Queue):
        self._queues = [(qq, u) for qq, u in self._queues if qq is not q]

    def emit(self, message: str):
        self._history.append(message)
        if len(self._history) > self._history_max:
            del self._history[: len(self._history) - self._history_max]
        self._append_to_file(message)
        dead = []
        for q, username in self._queues:
            if not _log_visible_to(message, username):
                continue
            try:
                q.put_nowait(message)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._queues = [(qq, u) for qq, u in self._queues if qq is not q]

    def clear_user(self, username: str) -> int:
        """Drop one user's tagged lines from memory and the log file.

        Only lines carrying this user's own tag go away - everyone else's
        lines (and untagged system lines) stay. Returns lines removed.
        """
        pat = re.compile(r"user=" + re.escape(username) + r"(?=\s|$)")
        kept_mem = [m for m in self._history if not pat.search(m)]
        removed = len(self._history) - len(kept_mem)
        self._history = kept_mem
        try:
            with self._file_lock:
                try:
                    with open(self._log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except FileNotFoundError:
                    lines = []
                kept = [ln for ln in lines if not pat.search(ln.rstrip("\n"))]
                removed += len(lines) - len(kept)
                tmp = self._log_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                os.replace(tmp, self._log_file)
        except OSError:
            pass
        return removed

log_broadcaster = LogBroadcaster()

class WebLogHandler(logging.Handler):
    def __init__(self, broadcaster: LogBroadcaster):
        super().__init__()
        self.broadcaster = broadcaster

    def emit(self, record):
        try:
            msg = self.format(record)
            self.broadcaster.emit(msg)
        except Exception:
            pass

web_handler = WebLogHandler(log_broadcaster)
web_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger("Omniilink").addHandler(web_handler)
logging.getLogger().addHandler(web_handler)


# ==========================================

# Models

# ==========================================
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1000
    stream: Optional[bool] = False

class SettingsRequest(BaseModel):
    provider: str
    key_name: str
    key_value: str

class ClearKeyRequest(BaseModel):
    key_name: str

class CodingOpenFolder(BaseModel):
    path: str

class CodingFileWrite(BaseModel):
    path: str
    content: str

class CodingFilePatch(BaseModel):
    path: str
    old_text: str
    new_text: str

class CodingRunCommand(BaseModel):
    command: str
    cwd: Optional[str] = None

class CodingFileRestore(BaseModel):
    path: str
    ts: float

class ProjectVersionCreate(BaseModel):
    short: str = ""
    paragraph: str = ""
    chat: list = []

class ProjectVersionId(BaseModel):
    id: str

class CodingGitCommit(BaseModel):
    message: str
    add: str = "all"


# ==========================================

# API Endpoints

# ==========================================
@app.get("/api/logs")
async def stream_logs(request: Request):
    user, err = _require_auth(request)
    if err:
        return err

    # Admins see the full firehose; everyone else only their own tagged lines.
    username = None if user.get("is_admin") else user.get("username")
    try:
        q = log_broadcaster.subscribe(username)
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})
    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.get_event_loop().run_in_executor(None, q.get, True, 0.5)
                    yield f"data: {json.dumps({'log': msg})}\n\n"
                except queue.Empty:
                    yield f": keepalive {time.time()}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            log_broadcaster.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.post("/api/logs/clear")
async def clear_logs(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    n = log_broadcaster.clear_user(user.get("username", ""))
    return {"status": "ok", "cleared": n}

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "3.0.0", "model": MODEL_NAME, "build": BUILD}


# ==========================================

# Authentication Endpoints

# ==========================================
class SignupRequest(BaseModel):
    username: str
    password: str
    name: Optional[str] = ""

class SigninRequest(BaseModel):
    username: str
    password: str

class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    new_password: Optional[str] = None

class ProviderKeyRequest(BaseModel):
    provider: str
    api_key: str

class CustomProviderRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""

class ProviderKeyDeleteRequest(BaseModel):
    key_id: int


# Simple login brute-force throttle: 5 failures per IP+username -> 15 min lockout
_login_failures = {}

# Signup throttle: max 5 signups per IP per hour
_signup_attempts = {}

def _login_allowed(key: str) -> bool:
    now = time.time()
    entry = _login_failures.get(key)
    if not entry:
        return True
    count, first = entry
    if now - first > 15 * 60:
        _login_failures.pop(key, None)
        return True
    return count < 5

def _record_login_failure(key: str):
    now = time.time()
    count, first = _login_failures.get(key, (0, now))
    if now - first > 15 * 60:
        count, first = 0, now
    _login_failures[key] = (count + 1, first)

def _signup_allowed(ip: str) -> bool:
    now = time.time()
    entry = _signup_attempts.get(ip)
    if not entry:
        return True
    count, first = entry
    if now - first > 3600:
        _signup_attempts.pop(ip, None)
        return True
    return count < 5

def _record_signup(ip: str):
    now = time.time()
    count, first = _signup_attempts.get(ip, (0, now))
    if now - first > 3600:
        count, first = 0, now
    _signup_attempts[ip] = (count + 1, first)

@app.post("/api/auth/signup")
async def auth_signup(request: Request, req: SignupRequest):
    if _google_only():
        return JSONResponse(status_code=403, content={"error": "Sign-up is Google-only on this server — use Sign up with Google"})
    ip = request.headers.get("cf-connecting-ip", "") or (request.client.host if request.client else "")
    if not _signup_allowed(ip):
        return JSONResponse(status_code=429, content={"error": "Too many signups. Try again later."})
    username = req.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", username):
        return JSONResponse(status_code=400, content={"error": "Username must be 3-32 chars: letters, numbers, - and _ only"})
    if len(req.password) < 6:
        return JSONResponse(status_code=400, content={"error": "Password must be at least 6 characters"})

    # Reserved: allowlisted admin handles can never be registered by anyone

    # else. A brand-new database is exempt so the deployer can still claim

    # their own handle on first run.
    names = all_usernames()
    if names and _allowlisted(username):
        return JSONResponse(status_code=409, content={"error": "That username is reserved"})

    # Two accounts must never share one filesystem folder: "bob" and "bob_"

    # both sanitize to "bob", which would merge their private data.
    safe = _safe_username(username)
    if any(_safe_username(n) == safe for n in names):
        return JSONResponse(status_code=409, content={"error": "Username already taken"})
    result = create_user(username, req.password, req.name)
    if "error" in result:
        return JSONResponse(status_code=409, content={"error": result["error"]})
    _user_projects_dir(username)
    _record_signup(ip)
    token = create_session(result["id"])
    user = get_user_by_id(result["id"])
    logger.info(f"[AUTH] Signup user={username} ip={ip}")
    resp = JSONResponse(content={"status": "ok", "user": user})
    set_session_cookie(resp, token, secure=_request_is_https(request))
    return resp

@app.post("/api/auth/signin")
async def auth_signin(request: Request, req: SigninRequest):
    if _google_only():
        return JSONResponse(status_code=403, content={"error": "Sign-in is Google-only on this server — use Sign in with Google"})
    ip = request.headers.get("cf-connecting-ip", "") or (request.client.host if request.client else "")
    throttle_key = f"{ip}:{req.username.strip().lower()}"
    if not _login_allowed(throttle_key):
        return JSONResponse(status_code=429, content={"error": "Too many failed attempts. Try again in 15 minutes."})
    user = authenticate_user(req.username.strip().lower(), req.password)
    if not user:
        _record_login_failure(throttle_key)
        logger.warning(f"[AUTH] Failed signin user={req.username.strip().lower()} ip={ip}")
        return JSONResponse(status_code=401, content={"error": "Invalid username or password"})
    _login_failures.pop(throttle_key, None)
    token = create_session(user["id"])
    logger.info(f"[AUTH] Signin user={user.get('username','-')} ip={ip}")
    resp = JSONResponse(content={"status": "ok", "user": user})
    set_session_cookie(resp, token, secure=_request_is_https(request))
    return resp

@app.post("/api/auth/signout")
async def auth_signout(request: Request):
    token = request.cookies.get("omnilink_token")
    if token:
        destroy_session(token)
    resp = JSONResponse(content={"status": "ok"})
    resp.delete_cookie("omnilink_token", path="/")
    return resp

def _google_oauth_configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

def _google_only() -> bool:
    """Password sign-in/signup is disabled while Google sign-in is the only way.

    Active whenever Google OAuth is configured, unless OMNIILINK_ALLOW_PASSWORD
    is truthy — that escape hatch keeps the server owner from ever being locked
    out if Google is misconfigured."""
    if os.environ.get("OMNIILINK_ALLOW_PASSWORD", "").strip().lower() in ("1", "true", "yes"):
        return False
    return _google_oauth_configured()

def _google_redirect_uri() -> str:
    return os.environ.get("GOOGLE_REDIRECT_URI", "https://omniilink.dpdns.org/api/auth/google/callback")

@app.get("/api/auth/oauth-status")
async def oauth_status():
    return {"google": _google_oauth_configured(), "google_only": _google_only()}

@app.get("/api/auth/google/start")
async def google_oauth_start(request: Request):
    if not _google_oauth_configured():
        return JSONResponse(status_code=501, content={"error": "Google sign-in is not set up on this server"})
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + params, status_code=302)
    resp.set_cookie(key="g_oauth_state", value=state, httponly=True, samesite="lax",
                    secure=_request_is_https(request), max_age=600, path="/")
    return resp

def _google_post(url: str, data: dict):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def _google_get(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

@app.get("/api/auth/google/callback")
async def google_oauth_callback(request: Request):
    def fail(msg: str):
        return RedirectResponse("/signin?error=" + urllib.parse.quote(msg), status_code=302)
    if request.query_params.get("error"):
        return fail(request.query_params.get("error_description") or request.query_params.get("error"))
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    want = request.cookies.get("g_oauth_state", "")
    if not code or not state or not want or not hmac.compare_digest(state, want):
        return fail("Sign-in expired - please try again")
    if not _google_oauth_configured():
        return fail("Google sign-in is not set up on this server")
    try:
        tok = _google_post("https://oauth2.googleapis.com/token", {
            "code": code,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        })
        access = tok.get("access_token", "")
        if not access:
            return fail("Google did not return a token")
        info = _google_get("https://openidconnect.googleapis.com/v1/userinfo", access)
    except Exception as e:
        logger.warning(f"[AUTH] Google token exchange failed: {e}")
        return fail("Could not finish Google sign-in - try again")
    sub = str(info.get("sub") or "")
    email = str(info.get("email") or "")
    if not sub or not email or info.get("email_verified") is not True:
        return fail("Google did not verify an email address")
    user = get_user_by_google_sub(sub)
    if not user:
        ip = request.headers.get("cf-connecting-ip", "") or (request.client.host if request.client else "")
        if not _signup_allowed(ip):
            return fail("Too many signups. Try again later.")
        base = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower()).strip("_") or "user"
        if len(base) < 3:
            base = (base + "user")[:24]
        base = base[:24]
        names = all_usernames()
        if names and _allowlisted(base):
            return fail("That username is reserved - sign up with a password instead")
        username = base
        n = 0
        while username_taken(username) or any(_safe_username(x) == _safe_username(username) for x in names):
            n += 1
            if n > 60:
                return fail("Could not pick a username - sign up with a password instead")
            username = (base[:20] + str(n))[:24]
        created = create_user(username, secrets.token_urlsafe(32), email.split("@")[0])
        if "error" in created:
            return fail(created["error"])
        set_user_google_sub(created["id"], sub)
        try:
            _user_projects_dir(username)
        except Exception:
            pass
        _record_signup(ip)
        user = get_user_by_id(created["id"])
        logger.info(f"[AUTH] Google signup user={username}")
    else:
        logger.info(f"[AUTH] Google signin user={user.get('username','-')}")
    token = create_session(user["id"])
    resp = RedirectResponse("/chat", status_code=302)
    set_session_cookie(resp, token, secure=_request_is_https(request))
    resp.delete_cookie("g_oauth_state", path="/")
    return resp

@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    return {"user": user}

@app.put("/api/auth/profile")
async def auth_update_profile(request: Request, req: UpdateProfileRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    if req.name is not None:
        update_user_name(user["id"], req.name.strip()[:100])
    if req.new_password:
        if len(req.new_password) < 6:
            return JSONResponse(status_code=400, content={"error": "Password must be at least 6 characters"})
        update_user_password(user["id"], req.new_password)

        # Password change invalidates every other session
        current_token = request.cookies.get("omnilink_token")
        revoke_user_sessions(user["id"], keep_token=current_token)
    updated = get_user_by_id(user["id"])
    return {"status": "ok", "user": updated}

@app.post("/api/auth/change-username")
async def auth_change_username(request: Request):
    """Rename the signed-in user without dropping their Google link or data."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    new = str((data or {}).get("username", "")).strip().lower()
    if not new:
        return JSONResponse(status_code=400, content={"error": "Username is required"})

    # Reserved admin handles can never be claimed by another account (mirrors signup).
    if all_usernames() and _allowlisted(new) and new != (user.get("username") or "").lower():
        return JSONResponse(status_code=409, content={"error": "That username is reserved"})
    result = change_username(user["id"], new)
    if result.get("error"):
        return JSONResponse(status_code=400, content={"error": result["error"]})
    updated = get_user_by_id(user["id"])
    _coding_state_evict(_safe_username(user.get("username", "")))
    _coding_state_evict(_safe_username(updated.get("username", "")))
    _audit("auth.change_username", updated.get("username", ""), new)
    out = {"status": "ok", "user": updated}
    if result.get("warning"):
        out["warning"] = result["warning"]
    return out


# Provider API Key Management (per-user, stored in DB)
@app.get("/api/provider-keys")
async def list_provider_keys(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    db_keys = get_user_provider_keys(user["id"])
    keys_list = []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, provider, api_key, created_at, updated_at FROM user_api_keys WHERE user_id = ?",
            (user["id"],)
        ).fetchall()
        for r in rows:
            keys_list.append({
                "id": r["id"],
                "provider": r["provider"],
                "api_key_preview": r["api_key"][:8] + "..." + r["api_key"][-4:] if len(r["api_key"]) > 12 else "***",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
    finally:
        conn.close()
    return {"keys": keys_list}

@app.post("/api/provider-keys")
async def save_provider_key(request: Request, req: ProviderKeyRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    if not req.provider or not req.api_key:
        return JSONResponse(status_code=400, content={"error": "Provider and API key required"})
    if not re.fullmatch(r"[a-z0-9]{1,32}", req.provider):
        return JSONResponse(status_code=400, content={"error": "Invalid provider name"})
    if len(req.api_key) > 500 or any(ord(c) < 32 for c in req.api_key):
        return JSONResponse(status_code=400, content={"error": "Invalid API key"})
    save_user_provider_key(user["id"], req.provider.strip(), req.api_key.strip())
    logger.info(f"[KEYS] Provider key saved user={user.get('username','-')} provider={req.provider.strip()}")
    return {"status": "ok"}

@app.delete("/api/provider-keys/{key_id}")
async def delete_provider_key(request: Request, key_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    delete_user_provider_key(user["id"], key_id)
    logger.info(f"[KEYS] Provider key deleted user={user.get('username','-')} key_id={key_id}")
    return {"status": "ok"}

def _mask_key(api_key: str) -> str:
    if len(api_key) > 12:
        return api_key[:8] + "..." + api_key[-4:]
    return "***"

@app.get("/api/custom-providers")
async def list_custom_providers(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    out = []
    for r in get_custom_providers(user["id"]):
        out.append({
            "id": r["id"],
            "name": r["name"],
            "base_url": r["base_url"],
            "model": r["model"],
            "api_key_preview": _mask_key(r["api_key"] or ""),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return {"providers": out}

@app.post("/api/custom-providers")
async def save_custom_provider_ep(request: Request, req: CustomProviderRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    name = (req.name or "").strip().lower()
    base_url = (req.base_url or "").strip().rstrip("/")
    api_key = (req.api_key or "").strip()
    model = (req.model or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-_]{0,31}", name):
        return JSONResponse(status_code=400, content={"error": "Name must be 1-32 chars: lowercase letters, digits, - _"})
    if not re.fullmatch(r"https?://[^\s]{1,200}", base_url):
        return JSONResponse(status_code=400, content={"error": "API link must start with http:// or https://"})
    if not api_key or len(api_key) > 500 or any(ord(c) < 32 for c in api_key):
        return JSONResponse(status_code=400, content={"error": "Invalid API key"})
    if not model or len(model) > 128 or any(c.isspace() or ord(c) < 32 for c in model):
        return JSONResponse(status_code=400, content={"error": "Model must be 1-128 chars with no spaces"})
    row_id = save_custom_provider(user["id"], name, base_url, api_key, model)
    if row_id is None:
        return JSONResponse(status_code=500, content={"error": "Could not save provider"})
    logger.info(f"[KEYS] Custom provider saved user={user.get('username','-')} name={name}")
    return {"status": "ok", "id": row_id}

@app.delete("/api/custom-providers/{row_id}")
async def delete_custom_provider_ep(request: Request, row_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    delete_custom_provider(user["id"], row_id)
    logger.info(f"[KEYS] Custom provider deleted user={user.get('username','-')} id={row_id}")
    return {"status": "ok"}

@app.get("/api/status")
async def get_status(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    try:
        provider_keys = get_provider_keys(request)
        active = {}
        for prov_name, keys in provider_keys.items():
            active[prov_name] = {
                "key_count": len(keys),
                "status": "active"
            }
        return {
            "providers": active,
            "rate_limits": lb.rate_limiter.get_status(),
            "model_name": MODEL_NAME,
        }
    except Exception as e:
        return {"providers": {}, "rate_limits": {}, "model_name": MODEL_NAME, "error": str(e)}

def _device_model_entries(user):
    """Model entries for Ollama on paired devices the caller may use.

    Access is owner-or-explicit-share only (server admins included): even
    admins don't get another account's device models unless the owner shares
    the computer or the support escape hatch is enabled. Offline devices keep
    their last-known tags with online:false so the menu never jumps around.
    Contains relay I/O — call via asyncio.to_thread, never directly on the
    event loop."""
    out = []
    try:
        uid = int(user.get("id") or 0)
    except (TypeError, ValueError):
        return out
    with _AGENT_LOCK:
        st = _agents_load()
        agents = [dict(a) for a in st["agents"].values()]
    for a in agents:
        if not _agent_visible(user, a):
            continue
        if "ollama" not in (a.get("caps") or ()):
            continue
        aid = str(a.get("id") or "")
        if not aid:
            continue
        name = str(a.get("name") or a.get("hostname") or aid)
        online = _agent_online(a)
        try:
            tags = scan_ollama_models(agent_id=aid)
        except Exception:
            tags = []
        if not tags:
            continue
        try:
            vision = ollama_model_capabilities(agent_id=aid)
        except Exception:
            vision = {}
        for tag in tags:
            out.append({"id": f"device/{aid}/{tag}", "provider": "device",
                        "group": f"device/{aid}", "group_label": f"Ollama · {name}",
                        "source": "device", "online": bool(online),
                        "vision": bool((vision.get(tag) or {}).get("vision"))})
    return out


@app.get("/api/discover")
async def discover_models(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    provider_keys = get_provider_keys(request)
    discovered = {}
    all_models = [{"id": MODEL_NAME, "provider": "omniilink", "source": "default"}]
    for prov_name, keys in provider_keys.items():
        discovered[prov_name] = []
        for key_idx, api_key in enumerate(keys):
            models = lb._get_models(prov_name, api_key)
            for m in models:
                entry = {"id": m, "provider": prov_name, "key_index": key_idx}
                discovered[prov_name].append(m)
                if not any(x["id"] == m for x in all_models):
                    all_models.append(entry)
    ollama_models = scan_ollama_models()
    if ollama_models:
        discovered["ollama"] = ollama_models
        try:
            _ollama_caps = ollama_model_capabilities()
        except Exception:
            _ollama_caps = {}
        for m in ollama_models:
            mid = "ollama/" + m
            if not any(x["id"] == mid for x in all_models):
                all_models.append({"id": mid, "provider": "ollama", "source": "local",
                                   "vision": bool((_ollama_caps.get(m) or {}).get("vision"))})
    try:
        for entry in await asyncio.to_thread(_device_model_entries, _user):
            if not any(x["id"] == entry["id"] for x in all_models):
                all_models.append(entry)
    except Exception as e:
        logger.warning(f"[DISCOVER] device scan failed: {e}")
    for cp in get_custom_providers(_user["id"]):
        mid = "custom/" + cp["name"]
        discovered.setdefault("custom", []).append(mid)
        if not any(x["id"] == mid for x in all_models):
            all_models.append({"id": mid, "provider": "custom", "source": "custom"})
    return {"models": all_models, "by_provider": discovered, "backup_model": get_backup_model(),
            "health": provider_health(health_scope(request))}

@app.get("/api/provider-health")
async def get_provider_health(request: Request):
    """Roll-up of live request outcomes per provider (in-memory, no I/O)."""
    _user, err = _require_auth(request)
    if err:
        return err
    return {"health": provider_health(health_scope(request))}

def _ollama_current_url() -> str:
    """The live server-wide Ollama address (updated at runtime by the admin)."""
    return getattr(lb, "OLLAMA_BASE_URL", OLLAMA_BASE_URL)


@app.get("/api/ollama/models")
async def ollama_models(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    models = scan_ollama_models()
    try:
        caps = ollama_model_capabilities()
    except Exception:
        caps = {}
    return {"models": ["ollama/" + m for m in models],
            "tags": models,
            "vision": {m: bool((caps.get(m) or {}).get("vision")) for m in models},
            "available": len(models) > 0,
            "base_url": _ollama_current_url()}



# ---- Ollama library browser + hardware-aware recommendations ----
_OLLAMA_CATALOG = {"ts": 0.0, "models": []}

def _load_ollama_catalog():
    """Curated catalog of official Ollama library models (family/sizes/use).
    Cached for an hour; ships in the repo so browsing works offline."""
    now = time.time()
    if _OLLAMA_CATALOG["models"] and now - _OLLAMA_CATALOG["ts"] < 3600:
        return _OLLAMA_CATALOG["models"]
    models = []
    try:
        p = os.path.join(APP_DIR, "modellink", "ollama_catalog.json")
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for m in raw:
            sizes = []
            for s in m.get("sizes", []):
                size_gb = float(s.get("size_gb") or 0)
                tag = str(s.get("tag") or "latest")
                sizes.append({
                    "tag": tag,
                    "params_b": float(s.get("params_b") or 0),
                    "size_gb": size_gb,
                    "ram_gb": round(size_gb * 1.2 + 2, 1),
                    "full": f"{m['name']}:{tag}",
                })
            if not sizes:
                continue
            models.append({
                "name": m["name"],
                "family": m.get("family") or "other",
                "use": m.get("use") or ["chat"],
                "vision": bool(m.get("vision")),
                "description": m.get("description") or "",
                "sizes": sizes,
                "min_gb": min(s["size_gb"] for s in sizes),
                "max_gb": max(s["size_gb"] for s in sizes),
                "min_params": min(s["params_b"] for s in sizes),
                "max_params": max(s["params_b"] for s in sizes),
            })
    except Exception as e:
        logger.warning(f"[OLLAMA] catalog load failed: {e}")
    _OLLAMA_CATALOG.update(ts=now, models=models)
    return models


@app.get("/api/ollama/library")
async def ollama_library(request: Request, family: str = "", use: str = "",
                         q: str = "", max_params: float = 0):
    """Every catalogued Ollama model, filterable by family / use / size / text."""
    _user, err = _require_auth(request)
    if err:
        return err
    models = _load_ollama_catalog()
    families = sorted({m["family"] for m in models})
    uses = sorted({u for m in models for u in m["use"]})
    ql = (q or "").strip().lower()
    out = []
    for m in models:
        if family and m["family"] != family:
            continue
        if use and use not in m["use"]:
            continue
        if ql and ql not in m["name"].lower() and ql not in m["description"].lower():
            continue
        if max_params and m["min_params"] > max_params:
            continue
        out.append(m)
    return {"models": out, "families": families, "uses": uses, "count": len(out)}


def _pick_best(models, want_use, budget_gb, exclude=None):
    """The largest single size that fits `budget_gb` for a use-case."""
    exclude = exclude or set()
    best = None
    for m in models:
        if want_use not in m["use"] or m["name"] in exclude:
            continue
        for s in m["sizes"]:
            if s["ram_gb"] <= budget_gb and (best is None or s["ram_gb"] > best["ram_gb"]):
                best = dict(s)
                best.update(name=m["name"], family=m["family"], use=m["use"],
                            vision=m["vision"], description=m["description"])
    return best


def _gpu_is_dedicated(name):
    """True for a discrete GPU. Integrated graphics share system RAM, so they
    do not get a VRAM budget of their own."""
    n = (name or "").lower()
    if not n:
        return False
    if any(k in n for k in ("nvidia", "geforce", "quadro", "rtx", "gtx", "tesla",
                            "radeon rx", "radeon pro", "radeon vii", "instinct",
                            "arc a", "arc b", "arc pro")):
        return True
    return False



# task key, label, candidate uses
_OLLAMA_TASKS = [
    ("coding", "Best for coding", ["coding"]),
    ("chat", "Best for chat", ["chat", "general"]),
    ("reasoning", "Best for reasoning", ["reasoning"]),
    ("vision", "Best for images", ["vision"]),
    ("3d", "Best for 3D / scene code", ["general"]),
]


def _pick_top(models, uses, budget_gb, n=2):
    """Up to `n` distinct models (largest first) whose on-disk size fits the
    budget, for any of the given uses."""
    cands = []
    for m in models:
        if not any(u in m["use"] for u in uses):
            continue
        for s in m["sizes"]:
            if s["size_gb"] <= budget_gb:
                cands.append((m, s))
    cands.sort(key=lambda ms: ms[1]["size_gb"], reverse=True)
    out, seen = [], set()
    for m, s in cands:
        if m["name"] in seen:
            continue
        seen.add(m["name"])
        out.append((m, s))
        if len(out) >= n:
            break
    return out


@app.post("/api/ollama/recommend")
async def ollama_recommend(request: Request):
    """Top-2 model picks per task for a machine's specs.

    A discrete GPU is budgeted against its VRAM (~70%, leaving room for the
    context window and the desktop). With no dedicated VRAM, only half the
    system RAM is used. Very small machines get a slow-run warning."""
    _user, err = _require_auth(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    body = body or {}
    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    ram_gb, vram_gb = _num(body.get("ram_gb")), _num(body.get("vram_gb"))
    gpu = str(body.get("gpu") or "").strip()
    models = _load_ollama_catalog()
    dedicated = bool(gpu) and vram_gb > 0 and _gpu_is_dedicated(gpu)
    if dedicated:
        budget = round(vram_gb * 0.7, 1)
        source = "GPU VRAM (%g GB)" % vram_gb
        basis = "70%% of your %g GB VRAM, leaving room for context + display" % vram_gb
    else:
        budget = round(max(0.0, ram_gb) * 0.5, 1)
        source = "system RAM (%g GB)" % ram_gb
        basis = "half of your %g GB RAM" % ram_gb
    tasks = []
    for key, label, uses in _OLLAMA_TASKS:
        picks = []
        for m, s in _pick_top(models, uses, budget, 2):
            picks.append({"tag": s["full"], "name": m["name"], "family": m["family"],
                          "size_gb": s["size_gb"], "params_b": s["params_b"],
                          "vision": m["vision"],
                          "reason": "%.1f GB — fits in %g GB (%s)" % (s["size_gb"], budget, basis)})
        if picks:
            tasks.append({"key": key, "label": label, "use": uses[0], "picks": picks})
    if budget < 4.0:
        warning = ("This machine has very little memory for AI models (%g GB usable). "
                   "Only the smallest models fit and they will run slowly — expect short, "
                   "basic answers." % budget)
    elif not tasks:
        warning = "No catalogued model fits this machine's memory budget."
    else:
        warning = ""
    flat = [dict(t["picks"][0], label=t["label"], use=t["use"]) for t in tasks if t["picks"]]
    specs_out = {
        "cpu": str(body.get("cpu") or ""),
        "cpu_cores": int(_num(body.get("cpu_cores"))),
        "cpu_threads": int(_num(body.get("cpu_threads"))),
        "cpu_mhz": int(_num(body.get("cpu_mhz"))),
        "ram_gb": ram_gb, "gpu": gpu, "vram_gb": vram_gb,
        "gpu_driver": str(body.get("gpu_driver") or ""),
        "os": str(body.get("os") or ""),
    }
    return {"budget_gb": budget, "source": source, "basis": basis,
            "dedicated_gpu": dedicated, "tasks": tasks, "picks": flat, "warning": warning,
            "specs": specs_out}


def _sysinfo_command(platform_str):
    """Shell command that prints OMNI_* hardware lines for the device's OS."""
    p = (platform_str or "").lower()

    # Check macOS first: "darwin" contains "win" and must not hit Windows.
    if "darwin" in p or "mac" in p:
        return ("echo OMNI_CPU=$(sysctl -n machdep.cpu.brand_string 2>/dev/null);"
                "echo OMNI_CPU_CORES=$(sysctl -n hw.physicalcpu 2>/dev/null);"
                "echo OMNI_CPU_THREADS=$(sysctl -n hw.logicalcpu 2>/dev/null);"
                "echo OMNI_CPU_MHZ=$(( $(sysctl -n hw.cpufrequency 2>/dev/null) / 1000000 ));"
                "echo OMNI_RAM_GB=$(( $(sysctl -n hw.memsize 2>/dev/null) / 1073741824 ));"
                "echo OMNI_GPU=$(system_profiler SPDisplaysDataType 2>/dev/null | grep -m1 'Chipset Model' | cut -d: -f2);"
                "echo OMNI_OS=$(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)")
    if "win" in p:

        # Prefer nvidia-smi (accurate VRAM + driver); Win32 AdapterRAM is a

        # 32-bit field that caps at 4 GB. Fall back to WMI otherwise.
        return ('powershell -NoProfile -Command "'
                '$p=Get-CimInstance Win32_Processor | Select-Object -First 1;'
                '$cs=Get-CimInstance Win32_ComputerSystem;'
                '$os=Get-CimInstance Win32_OperatingSystem;'
                '$gpu=\'\';$vram=0;$drv=\'\';'
                '$smi=(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null | Select-Object -First 1);'
                'if($smi){$a=$smi.Split(\',\');$gpu=$a[0].Trim();$vram=[math]::Round([double]$a[1]/1024,1);$drv=$a[2].Trim()}'
                'else{$v=Get-CimInstance Win32_VideoController | Select-Object -First 1;$gpu=$v.Name;$vram=[math]::Round($v.AdapterRAM/1GB,1);$drv=$v.DriverVersion};'
                'Write-Output (\'OMNI_CPU=\'+$p.Name);'
                'Write-Output (\'OMNI_CPU_CORES=\'+$p.NumberOfCores);'
                'Write-Output (\'OMNI_CPU_THREADS=\'+$p.NumberOfLogicalProcessors);'
                'Write-Output (\'OMNI_CPU_MHZ=\'+$p.MaxClockSpeed);'
                'Write-Output (\'OMNI_RAM_GB=\'+[math]::Round($cs.TotalPhysicalMemory/1GB));'
                'Write-Output (\'OMNI_GPU=\'+$gpu);Write-Output (\'OMNI_VRAM_GB=\'+$vram);'
                'Write-Output (\'OMNI_GPU_DRIVER=\'+$drv);'
                'Write-Output (\'OMNI_OS=\'+$os.Caption+\' \'+$os.Version)"')
    return ("echo OMNI_CPU=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2);"
            "echo OMNI_CPU_CORES=$(grep -c ^processor /proc/cpuinfo 2>/dev/null);"
            "echo OMNI_CPU_THREADS=$(nproc 2>/dev/null);"
            "echo OMNI_CPU_MHZ=$(grep -m1 'cpu MHz' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | cut -d. -f1);"
            "echo OMNI_RAM_GB=$(awk '/MemTotal/{printf \"%d\", $2/1048576}' /proc/meminfo 2>/dev/null);"
            "echo OMNI_GPU=$( (nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null "
            "|| lspci 2>/dev/null | grep -i -m1 vga) | head -1);"
            "echo OMNI_VRAM_GB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null "
            "| head -1 | awk '{printf \"%.1f\", $1/1024}');"
            "echo OMNI_GPU_DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1);"
            "echo OMNI_OS=$(uname -sr 2>/dev/null)")


def _parse_sysinfo(output):
    """Parse the OMNI_* lines into a specs dict (best-effort)."""
    specs = {"cpu": "", "ram_gb": 0.0, "gpu": "", "vram_gb": 0.0, "os": "",
             "gpu_driver": "", "cpu_cores": 0, "cpu_threads": 0, "cpu_mhz": 0}
    str_keys = {"OMNI_CPU": "cpu", "OMNI_GPU": "gpu", "OMNI_OS": "os",
                "OMNI_GPU_DRIVER": "gpu_driver"}
    num_keys = {"OMNI_RAM_GB": ("ram_gb", float), "OMNI_VRAM_GB": ("vram_gb", float),
                "OMNI_CPU_CORES": ("cpu_cores", int), "OMNI_CPU_THREADS": ("cpu_threads", int),
                "OMNI_CPU_MHZ": ("cpu_mhz", int)}
    for line in str(output or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().upper(), v.strip()
        if k in str_keys:
            specs[str_keys[k]] = v[:160]
        elif k in num_keys:
            field, cast = num_keys[k]
            try:
                specs[field] = cast(float(re.sub(r"[^0-9.]", "", v) or 0))
            except (ValueError, TypeError):
                pass
    return specs


@app.post("/api/agents/{agent_id}/sysinfo")
async def agent_sysinfo(request: Request, agent_id: str):
    """Run a hardware scan on a paired computer and return cpu/ram/gpu."""
    user, err = _require_auth(request)
    if err:
        return err
    with _AGENT_LOCK:
        a = _agents_load()["agents"].get(str(agent_id))
    if a is None or not _agent_visible(user, a):
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    platform_str = a.get("platform") or ""
    cid, err2 = _queue_agent_command(user, agent_id, _sysinfo_command(platform_str), "shell", None)
    if err2:
        return err2
    _agent_notify(agent_id)
    out = ""
    deadline = time.time() + 25
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        with _AGENT_LOCK:
            c = _agents_load()["commands"].get(str(cid))
        if c and c.get("status") == "done":
            out = c.get("output") or ""
            break
    specs = _parse_sysinfo(out)
    specs["os"] = platform_str
    if not (specs["ram_gb"] or specs["cpu"]):
        return JSONResponse(status_code=502, content={
            "error": "Could not read hardware specs from that computer", "raw": out[:400]})
    return {"status": "ok", "specs": specs, "raw": out[:800]}


@app.post("/api/agents/{agent_id}/ollama/pull")
async def agent_ollama_pull(request: Request, agent_id: str):
    """Download a model onto a paired computer, streaming live progress.

    Proxies Ollama's own `/api/pull` NDJSON (status/total/completed per layer)
    through the relay so the browser can show a real progress bar, speed and
    total size instead of waiting blindly for a shell command to finish."""
    user, err = _require_auth(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    tag = str((body or {}).get("tag") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:\-/]{1,120}", tag):
        return JSONResponse(status_code=400, content={"error": "Invalid model tag"})
    with _AGENT_LOCK:
        a = _agents_load()["agents"].get(str(agent_id))
    if a is None or not _agent_visible(user, a):
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    if "ollama" not in (a.get("caps") or []):
        return JSONResponse(status_code=400, content={
            "error": "That computer does not share Ollama models — update its companion app."})
    try:
        resp = relay.open_response(str(agent_id), "/api/pull",
                                   {"name": tag, "stream": True},
                                   read_timeout=3600.0, first_timeout=60.0)
    except relay.RelayError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})

    async def gen():
        _it = resp.iter_lines()
        _sentinel = object()
        try:
            while True:
                line = await asyncio.to_thread(next, _it, _sentinel)
                if line is _sentinel:
                    break
                if not line:
                    continue
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                text = text.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                yield json.dumps(obj) + "\n"
        except (relay.RelayError, relay.RelayTimeout) as e:
            yield json.dumps({"error": str(e)}) + "\n"
        finally:
            try:
                resp.close()
            except Exception:
                pass

    _audit("agent.ollama_pull", user["username"], f"agent={agent_id} tag={tag}")
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/ollama/config")
async def ollama_config_get(request: Request):
    """Server-wide Ollama status (configured in the admin panel, not per user)."""
    _user, err = _require_auth(request)
    if err:
        return err
    models = scan_ollama_models()
    try:
        caps = ollama_model_capabilities()
    except Exception:
        caps = {}
    return {"base_url": _ollama_current_url(), "available": len(models) > 0,
            "models": models,
            "vision": {m: bool((caps.get(m) or {}).get("vision")) for m in models},
            "is_admin": bool(_user.get("is_admin"))}


def _set_env_var(key_name: str, key_value: str):
    """Persist KEY=value into the server .env and the live environment."""
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    target = f"{key_name}="
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(target):
            lines[i] = f"{key_name}={key_value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key_name}={key_value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    if key_value:
        os.environ[key_name] = key_value
    else:
        os.environ.pop(key_name, None)


@app.post("/api/ollama/config")
async def ollama_config_set(request: Request):
    """Admin-only: set the SERVER's Ollama URL (the server app's setting)."""
    _user, err = _require_admin(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    url = str((body or {}).get("base_url", "")).strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        return JSONResponse(status_code=400, content={"error": "URL must start with http:// or https://"})
    _set_env_var("OMNIILINK_OLLAMA_URL", url)
    set_ollama_base_url(url)
    models = scan_ollama_models()
    return {"status": "ok", "base_url": _ollama_current_url(),
            "available": len(models) > 0, "models": models}


@app.get("/api/devices/{agent_id}/ollama")
async def device_ollama_status(request: Request, agent_id: str):
    """Relayed Ollama status for one paired device (owner or admin view —
    management info; using the models stays owner-only in chat)."""
    user, err = _require_auth(request)
    if err:
        return err
    with _AGENT_LOCK:
        st = _agents_load()
        a = st["agents"].get(str(agent_id))
    if not a or not _agent_visible(user, a):
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    caps = a.get("caps") or []
    out = {"id": str(a.get("id")), "name": str(a.get("name") or ""),
           "online": _agent_online(a), "caps": caps, "ollama": None}
    if "ollama" in caps and out["online"]:
        try:
            tags = await asyncio.to_thread(scan_ollama_models, None, str(agent_id), True)
            try:
                vision = await asyncio.to_thread(
                    lambda: ollama_model_capabilities(force=True, agent_id=str(agent_id)))
            except Exception:
                vision = {}
            out["ollama"] = {"ok": True, "models": tags,
                             "vision": {m: bool((vision.get(m) or {}).get("vision"))
                                        for m in tags}}
        except Exception as e:
            out["ollama"] = {"ok": False, "error": str(e)}
    elif "ollama" in caps:
        out["ollama"] = {"ok": False, "error": f"{out['name']} is offline"}
    else:
        out["ollama"] = {"ok": False, "supported": False,
                         "error": "companion app needs updating"}
    return out


@app.post("/api/error-reports")
async def create_error_report(request: Request):
    """File an error report for admins (failed model, dead provider, ...)."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO error_reports (user_id, username, context, provider, model, error, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], user.get("username", ""),
             str(data.get("context", "chat"))[:32],
             str(data.get("provider", ""))[:64],
             str(data.get("model", ""))[:128],
             str(data.get("error", ""))[:2000],
             str(data.get("note", ""))[:2000]))
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


_FINDINGS_COLS = ("id, user_id, username, kind, title, problem, solution,"
                  " why, model, provider, language, code, created_at")
_FINDINGS_CAP = 1000


def _findings_where(kind, model, provider, language, q):
    """Shared filter builder for the findings list endpoints."""
    conds, params = [], []
    if kind in ("error", "code"):
        conds.append("kind = ?")
        params.append(kind)
    for col, val in (("model", model), ("provider", provider), ("language", language)):
        v = (val or "").strip()
        if v:
            conds.append(col + " LIKE ?")
            params.append("%" + v[:64] + "%")
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if len(w) >= 4][:8]
    for w in words:
        conds.append("(lower(title) LIKE ? OR lower(problem) LIKE ? OR lower(solution) LIKE ?)")
        params.extend(["%" + w + "%"] * 3)
    return (" WHERE " + " AND ".join(conds)) if conds else "", params


@app.get("/api/coding/findings")
async def coding_findings_list(request: Request, kind: str = "", model: str = "",
                               provider: str = "", language: str = "", q: str = ""):
    """Troubleshooting knowledge base for the coding loop: what tripped before,
    on which model/provider/language, and what fixed it. Readable by any
    signed-in user (no file contents, only lessons)."""
    user, err = _require_auth(request)
    if err:
        return err
    where, params = _findings_where(kind, model, provider, language, q)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT " + _FINDINGS_COLS + " FROM findings"
            + where + " ORDER BY id DESC LIMIT 100", params).fetchall()
        return {"findings": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/coding/findings")
async def coding_findings_create(request: Request):
    """Log one lesson learned (called by the coding loop after it overcomes a
    repeated failure, or from the admin page). Any signed-in user may add;
    only admins can delete."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    kind = str(data.get("kind", "error")).strip().lower()
    if kind not in ("error", "code"):
        return JSONResponse(status_code=400, content={"error": "kind must be error or code"})
    title = str(data.get("title", "")).strip()[:200]
    problem = str(data.get("problem", "")).strip()[:2000]
    solution = str(data.get("solution", "")).strip()[:2000]
    if not title or (not problem and not solution):
        return JSONResponse(status_code=400, content={"error": "title plus problem/solution required"})
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO findings (user_id, username, kind, title, problem, solution,"
            " why, model, provider, language, code)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], user.get("username", ""), kind, title, problem, solution,
             str(data.get("why", "")).strip()[:2000],
             str(data.get("model", ""))[:128], str(data.get("provider", ""))[:64],
             str(data.get("language", ""))[:32], str(data.get("code", ""))[:8000]))
        conn.execute("DELETE FROM findings WHERE id NOT IN"
                     " (SELECT id FROM findings ORDER BY id DESC LIMIT ?)",
                     (_FINDINGS_CAP,))
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/admin/findings")
async def admin_findings_list(request: Request, kind: str = "", model: str = "",
                              provider: str = "", language: str = "", q: str = ""):
    user, err = _require_superadmin(request)
    if err:
        return err
    where, params = _findings_where(kind, model, provider, language, q)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT " + _FINDINGS_COLS + " FROM findings"
            + where + " ORDER BY id DESC LIMIT 200", params).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM findings").fetchone()["c"]
        return {"findings": [dict(r) for r in rows], "total": total}
    finally:
        conn.close()


@app.delete("/api/admin/findings/{finding_id}")
async def admin_findings_delete(request: Request, finding_id: int):
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "Finding not found"})
        return {"status": "ok", "id": finding_id}
    finally:
        conn.close()


# ==========================================
# Trade Me valuation & flipping pipeline (admin only)
# One-shot category scrape (plain HTTP first, Playwright hook reserved),
# thumbnails stored server-side, human-in-the-loop verify console.
# ==========================================
_TRADEME_STATUS = ("pending", "approved", "rejected", "adjusted")
_TRADEME_CAP = 50
_TRADEME_LIMIT_OPTIONS = (20, 50, 100, 200)
# Trade Me success fee for most computer/electronics categories. Configurable;
# used to turn "estimated − ask" into a real profit after fees + shipping.
_TRADEME_FEE_PCT = 7.9
_TRADEME_IMG_MAX = 300 * 1024
_TRADEME_IMG_CAP = 10
_TRADEME_GALLERY_CAP = 12


def _trademe_thumb_dir() -> str:
    return _ensure_dir(os.path.join(APP_DIR, "user_data", "trademe_thumbs"))


def _trademe_config_path() -> str:
    return os.path.join(APP_DIR, "user_data", "trademe_config.json")


def _trademe_config_normalize(data: dict) -> dict:
    """Clamp limit, normalise the exclusion list, and read the cost model
    (success-fee % and flat shipping) used for true-profit maths."""
    out = {"limit": _TRADEME_CAP, "exclude": [], "fee_pct": _TRADEME_FEE_PCT,
           "shipping": 0.0}
    try:
        raw = (data or {}).get("limit")
        if raw not in (None, ""):
            out["limit"] = max(1, min(200, int(float(raw))))
    except (TypeError, ValueError):
        pass
    ex = (data or {}).get("exclude")
    if isinstance(ex, str):
        ex = re.split(r"[,\n]", ex)
    keys, seen = [], set()
    for k in (ex or []):
        k = str(k).strip().lower()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    out["exclude"] = keys
    try:
        fp = (data or {}).get("fee_pct")
        if fp not in (None, ""):
            out["fee_pct"] = max(0.0, min(50.0, float(fp)))
    except (TypeError, ValueError):
        pass
    try:
        sh = (data or {}).get("shipping")
        if sh not in (None, ""):
            out["shipping"] = max(0.0, float(sh))
    except (TypeError, ValueError):
        pass
    return out


def _trademe_config_load() -> dict:
    try:
        with open(_trademe_config_path(), "r", encoding="utf-8") as f:
            return _trademe_config_normalize(json.load(f))
    except Exception:
        return {"limit": _TRADEME_CAP, "exclude": [], "fee_pct": _TRADEME_FEE_PCT,
                "shipping": 0.0}


def _trademe_config_save(cfg: dict) -> dict:
    # Merge over what is already stored so a partial save never resets the
    # other settings to defaults.
    cfg = _trademe_config_normalize({**_trademe_config_load(), **(cfg or {})})
    try:
        with open(_trademe_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass
    return cfg


def _trademe_excluded(title: str, exclude) -> bool:
    """Case-insensitive keyword exclusion on a listing title."""
    t = (title or "").lower()
    return any(k and k in t for k in (exclude or []))


# Spec tokens worth surfacing: sizes, RAM generations, cores, refresh, screen.
_TRADEME_SPEC_RES = (
    ("size", re.compile(r"(?<![A-Za-z0-9])(\d{1,4}(?:\.\d+)?)\s?(gb|tb|mb)\b", re.I)),
    ("ddr", re.compile(r"(?<![A-Za-z0-9])ddr\s?([2-5])\b", re.I)),
    ("cores", re.compile(r"(?<![A-Za-z0-9])(\d{1,3})\s?-?\s?cores?\b", re.I)),
    ("hz", re.compile(r"(?<![A-Za-z0-9])(\d{2,3})\s?hz\b", re.I)),
    ("inch", re.compile(r"(?<![A-Za-z0-9])(\d{1,2}(?:\.\d)?)\s?-?\s?(?:inch|inches|\")\b", re.I)),
)
_RAM_WORDS = ("ram", "ddr", "memory", "mem ")
_STORAGE_WORDS = ("ssd", "hdd", "nvme", "storage", "hard drive", "harddisk",
                  "hard disk", "drive", "emmc")


def _trademe_spec_kind(title: str, start: int, end: int, unit: str = "") -> str:
    """Classify a size token as RAM or storage from the adjacent word. The
    category word usually follows the size ("64GB RAM", "1TB SSD"), so the
    suffix is checked first, then the prefix."""
    low = (title or "").lower()
    suf, pre = low[end:end + 14], low[max(0, start - 14):start]

    def hit(seg):
        best = None
        for kind, words in (("ram", _RAM_WORDS), ("storage", _STORAGE_WORDS)):
            for w in words:
                i = seg.find(w)
                if i != -1 and (best is None or i < best[0]):
                    best = (i, kind)
        return best[1] if best else None

    kind = hit(suf) or hit(pre)
    if kind:
        return kind
    return "storage" if (unit or "").upper() == "TB" else "other"


def _trademe_detect_patterns(titles, min_count: int = 2, limit: int = 60) -> list:
    """Spot specs that repeat across listing titles (64GB RAM, 1TB SSD, 8-core,
    DDR4, 144Hz, 15.6") so the UI can surface them as one-click facets."""
    counts = {}
    for title in (titles or []):
        title = title or ""
        seen = set()
        for name, rx in _TRADEME_SPEC_RES:
            for m in rx.finditer(title):
                if name == "size":
                    num, unit = m.group(1), m.group(2)
                    key = f"{num}{unit.lower()}"
                    label = f"{num}{unit.upper()}"
                    kind = _trademe_spec_kind(title, m.start(), m.end(), unit)
                elif name == "ddr":
                    key, label, kind = f"ddr{m.group(1)}", f"DDR{m.group(1)}", "ram"
                elif name == "cores":
                    key, label, kind = f"{m.group(1)}core", f"{m.group(1)}-Core", "other"
                elif name == "hz":
                    key, label, kind = f"{m.group(1)}hz", f"{m.group(1)}Hz", "other"
                else:
                    key, label, kind = f"{m.group(1)}inch", f'{m.group(1)}"', "other"
                if key in seen:
                    continue
                seen.add(key)
                rec = counts.get(key)
                if rec is None:
                    counts[key] = {"key": key, "label": label, "kind": kind, "count": 1}
                else:
                    rec["count"] += 1
    out = [r for r in counts.values() if r["count"] >= max(1, min_count)]
    order = {"ram": 0, "storage": 1, "other": 2}
    out.sort(key=lambda r: (order.get(r["kind"], 3), -r["count"], r["label"]))
    return out[:limit]


def _trademe_has_listings(html: str) -> bool:
    """True when the HTML actually contains item links (numeric /listing/
    ids). Trade Me category pages are a JS app shell — plain HTTP returns
    chrome with zero listings, which must escalate, not save as empty."""
    if not html:
        return False
    for am in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html,
                          re.IGNORECASE | re.DOTALL):
        if not re.search(r"(?:/listing/|/item/)\d", am.group(1) or "", re.IGNORECASE):
            continue
        title = re.sub(r"<[^>]+>", " ", am.group(2) or "")
        if len(re.sub(r"\s+", " ", title).strip()) >= 3:
            return True
    return False


def _trademe_browser_html(url: str, wait_css: str = "a[href*='listing']",
                          scroll: int = 0, want: int = 0) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Trade Me renders with JavaScript — install Playwright"
                           " on the server (pip install playwright && playwright install chromium;"
                           " Docker rebuilds include it) and try again")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=ua)
            try:
                page.route("**/*.{png,jpg,jpeg,webp,gif,svg,woff,woff2,ttf,mp4}",
                           lambda r: r.abort())
            except Exception:
                pass
            page.goto(url, timeout=45000)
            if wait_css:
                try:
                    page.wait_for_selector(wait_css, timeout=30000)
                except Exception:
                    pass
            # Category pages lazy-load cards on scroll. Keep scrolling until
            # there are comfortably more cards than the quota (excluded ones
            # still cost cards) or the page stops growing.
            same = 0
            for _ in range(max(0, scroll)):
                try:
                    before = page.evaluate("document.body.scrollHeight")
                    have = page.locator("a[href*='listing']").count()
                    if want and have >= want * 2:
                        break
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    after = page.evaluate("document.body.scrollHeight")
                    if after == before:
                        same += 1
                        if same >= 2:
                            break
                    else:
                        same = 0
                except Exception:
                    break
            return page.content()
        finally:
            browser.close()


def _trademe_playwright_html(url: str, scroll: int = 0, want: int = 0) -> str:
    return _trademe_browser_html(url, scroll=scroll, want=want)


# Chrome shown to JS-less / logged-out clients must never be stored as a
# "description" (it is what you get back from the app shell over plain HTTP).
_TRADEME_JUNK = ("enable javascript", "requires javascript", "not logged in member",
                 "profile image for not logged in", "please enable javascript",
                 "refresh the page to try again")


def _trademe_is_junk(text: str) -> bool:
    low = (text or "").lower()
    return any(j in low for j in _TRADEME_JUNK)


def _trademe_meta_description(page_html: str) -> str:
    from html import unescape as _unescape
    for pat in (r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
                r'<meta[^>]+content="([^"]+)"[^>]+property="og:description"',
                r'<meta[^>]+name="description"[^>]+content="([^"]+)"'):
        m = re.search(pat, page_html or "", re.IGNORECASE)
        if m and m.group(1).strip():
            txt = _unescape(m.group(1).strip())
            if not _trademe_is_junk(txt):
                return txt[:3000]
    return ""


def _trademe_visible_text(page_html: str) -> str:
    from html import unescape as _unescape
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ",
                  page_html or "", flags=re.IGNORECASE)
    text = _unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def _trademe_extract_description(page_html: str, rendered: bool = False) -> str:
    """Description from meta tags, or (only for a JavaScript-rendered page)
    the visible text — never the raw app-shell chrome."""
    meta = _trademe_meta_description(page_html)
    if meta:
        return meta
    if not rendered:
        return ""
    text = _trademe_visible_text(page_html)
    if len(text) < 40 or _trademe_is_junk(text):
        return ""
    return text[:2000]


def _trademe_fetch_description(tm_url: str):
    """(description, engine) for one listing. Plain HTTP first (og:description
    is often server-rendered for share bots), then the browser."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0 Safari/537.36"}
    try:
        req = urllib.request.Request(tm_url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            page = r.read(1 * 1024 * 1024).decode("utf-8", "replace")
        # Plain HTTP only carries the meta description; the body is an app
        # shell, so visible text there is chrome and must be ignored.
        desc = _trademe_extract_description(page)
        if desc:
            return desc, "http"
    except Exception:
        pass
    page = _trademe_browser_html(tm_url, wait_css="")
    desc = _trademe_extract_description(page, rendered=True)
    if not desc:
        raise RuntimeError("No description found on the listing page")
    return desc, "playwright"


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _trademe_srcset_first(v: str) -> str:
    return re.split(r"[\s,]", (v or "").strip())[0] if v else ""


def _trademe_extract_images(page_html: str) -> list:
    """Candidate photo URLs on an item page: og:image plus gallery <img>s
    (src / data-src / srcset), filtered to Trade Me's photo host."""
    from html import unescape as _unescape
    urls, seen = [], set()

    def add(u):
        u = _unescape((u or "").strip())
        if not u.lower().startswith(("http://", "https://")):
            return
        base = u.split("?")[0].lower()
        if not base.endswith(_IMG_EXT):
            return
        host = urllib.parse.urlparse(u).netloc.lower()
        if not any(h in host for h in ("tmcdn", "trademe", "photoserver")):
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for m in re.finditer(
            r'<meta[^>]+(?:property|name)="(?:og:image|twitter:image)"[^>]+content="([^"]+)"',
            page_html or "", re.IGNORECASE):
        add(m.group(1))
    for m in re.finditer(
            r'<img[^>]+(?:data-src|data-original|data-lazy-src|src)="([^"]+)"',
            page_html or "", re.IGNORECASE):
        add(m.group(1))
    for m in re.finditer(r'(?:data-)?srcset="([^"]+)"', page_html or "", re.IGNORECASE):
        add(_trademe_srcset_first(m.group(1)))
    return urls


def _trademe_fetch_gallery(tm_url: str) -> list:
    """Photo URLs for one listing. Plain HTTP often carries og:image; a real
    browser is needed for the full gallery."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0 Safari/537.36"}
    try:
        req = urllib.request.Request(tm_url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            page = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        urls = _trademe_extract_images(page)
        if len(urls) > 1:
            return urls
    except Exception:
        urls = []
    try:
        page = _trademe_browser_html(tm_url, wait_css="")
        browser_urls = _trademe_extract_images(page)
        if browser_urls:
            return browser_urls
    except Exception:
        pass
    return urls


def _trademe_fetch_html(url: str, want: int = _TRADEME_CAP):
    """Returns (html, engine). Plain HTTP first (cheap when it works), then
    headless Chromium — Trade Me category pages are a JS app shell whose raw
    HTML holds no listings at all. `want` is the scrape quota: the browser
    scrolls to lazy-load comfortably more cards than that, so keyword
    exclusions filter down to `want` kept listings instead of `want` scanned."""
    html = _trademe_fetch_http(url)
    if _trademe_has_listings(html):
        return html, "http"
    scrolls = max(3, min(10, (int(want or _TRADEME_CAP) // 20) + 2))
    html = _trademe_playwright_html(url, scroll=scrolls, want=int(want or _TRADEME_CAP))
    if not _trademe_has_listings(html):
        raise RuntimeError("No listings rendered even in a real browser — Trade Me may be"
                           " blocking automation, or the category is empty")
    return html, "playwright"


def _trademe_abs(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:")):
        return ""
    return urllib.parse.urljoin(base, href).split("#")[0]


def _trademe_parse_listings(html: str, base_url: str,
                            cap: int = _TRADEME_CAP, exclude=None,
                            stats: dict = None) -> list:
    """Best-effort item extraction (title/url/price/nearby image). The BERT
    parser plugs in later; for now this stores raw text metadata. `cap` bounds
    how many items are KEPT — excluded titles never consume the quota, so the
    scan continues past them until `cap` keepers (or the page runs out).
    `stats`, when given, is filled with scanned/excluded/kept counts."""
    from html import unescape as _unescape
    cap = max(1, int(cap or _TRADEME_CAP))
    out, seen = [], set()
    spans = []
    scanned, excluded = 0, 0
    for am in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html or "",
                          re.IGNORECASE | re.DOTALL):
        href, inner, at = am.group(1), am.group(2), am.start()
        url = _trademe_abs(base_url, href)
        if not url or url in seen:
            continue
        # Detail pages only (numeric ids) — bare /marketplace/ category and
        # nav links are not listings.
        if not re.search(r"(?:/listing/|/item/)\d", url, re.IGNORECASE):
            continue
        title = _unescape(re.sub(r"<[^>]+>", " ", inner or ""))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 3:
            continue
        scanned += 1
        if _trademe_excluded(title, exclude):
            excluded += 1
            seen.add(url)
            continue
        seen.add(url)
        out.append({"tm_url": url[:500], "title": title[:200],
                    "tm_price": None, "image": ""})
        spans.append((at, am.end()))
        if len(out) >= cap:
            break
    if stats is not None:
        stats.update(scanned=scanned, excluded=excluded, kept=len(out))
    # Prices + thumbnails from the neighbourhood of each link: first $ amount
    # after the anchor; thumbnail inside the anchor first, else the nearest
    # <img> before it.
    for (rec, (at, end)) in zip(out, spans):
        window = html[end:end + 2000]
        m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", window)
        if m:
            try:
                rec["tm_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        img_rx = r'<img[^>]+(?:data-src|data-original|data-lazy-src|src)="([^"]+)"'
        inner_imgs = re.findall(img_rx, html[at:end], re.IGNORECASE)
        if inner_imgs:
            rec["image"] = _trademe_abs(base_url, inner_imgs[0])[:500]
        else:
            before = re.findall(img_rx, html[max(0, at - 1500):at], re.IGNORECASE)
            if before:
                rec["image"] = _trademe_abs(base_url, before[-1])[:500]
    return out


def _trademe_thumb_save(image_url: str):
    """Download one thumbnail (capped). Returns the stored filename or ''."""
    if not image_url or not image_url.lower().startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(
            image_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return ""
            raw = r.read(_TRADEME_IMG_MAX + 1)
        if not raw or len(raw) > _TRADEME_IMG_MAX:
            return ""
        ext = os.path.splitext(urllib.parse.urlparse(image_url).path)[1].lower()
        if ext and ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return ""
        if not ext:
            ext = ".jpg"
        name = f"{int(time.time() * 1000):013d}-{secrets.token_hex(4)}{ext}"
        with open(os.path.join(_trademe_thumb_dir(), name), "wb") as f:
            f.write(raw)
        return name
    except Exception:
        return ""


def _trademe_merge_keep(keep: dict, items: list, known: set) -> int:
    """Merge one parsed batch into the keeper dict, skipping already-known
    URLs. Returns how many were newly added."""
    new = 0
    for it in items or []:
        u = (it or {}).get("tm_url")
        if u and u not in keep and u not in (known or set()):
            keep[u] = it
            new += 1
    return new


def _trademe_collect_browser(category_url: str, limit: int, exclude,
                             known: set, log) -> tuple:
    """Batched collection inside one browser session. Each round parses the
    current DOM (~20-30 fresh cards per scroll), merges keepers, then scrolls
    for more — until `limit` NEW keepers or the page stops growing. Returns
    (items, excluded_total). Excluded titles never consume the quota."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Trade Me renders listings with JavaScript — install Playwright"
                           " on the server (pip install playwright && playwright install chromium;"
                           " Docker rebuilds include it) and scrape again")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    keep, excluded_total = {}, 0
    max_rounds = max(6, limit // 20 + 4)
    stall = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=ua)
            try:
                page.route("**/*.{png,jpg,jpeg,webp,gif,svg,woff,woff2,ttf,mp4}",
                           lambda r: r.abort())
            except Exception:
                pass
            page.goto(category_url, timeout=45000)
            try:
                page.wait_for_selector("a[href*='listing']", timeout=30000)
            except Exception:
                pass
            if not _trademe_has_listings(page.content()):
                raise RuntimeError("No listings rendered even in a real browser — Trade Me may be"
                                   " blocking automation, or the category is empty")
            for rnd in range(1, max_rounds + 1):
                try:
                    html = page.content()
                except Exception:
                    break
                stats = {}
                items = _trademe_parse_listings(html, category_url, cap=limit,
                                                exclude=exclude, stats=stats)
                new = _trademe_merge_keep(keep, items, known)
                excluded_total = max(excluded_total, stats.get("excluded", 0))
                log(f"batch {rnd}: +{new} new ({len(keep)}/{limit} kept,"
                    f" {stats.get('scanned', 0)} cards on page)")
                if len(keep) >= limit:
                    break
                try:
                    before = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    after = page.evaluate("document.body.scrollHeight")
                except Exception:
                    break
                if new == 0 and after == before:
                    stall += 1
                    if stall >= 2:
                        log("page stopped growing — collected all it holds")
                        break
                else:
                    stall = 0
        finally:
            browser.close()
    return list(keep.values())[:limit], excluded_total


def _trademe_fetch_http(url: str) -> str:
    """Plain-HTTP fetch; returns "" on any failure."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0 Safari/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return ""


def _trademe_run_scrape(job_id: str, category_url: str,
                        limit: int = _TRADEME_CAP, exclude=None):
    job = _ADMIN_JOBS.get(job_id)
    if job is None:
        return
    log = job["lines"].append
    try:
        log(f"Fetching {category_url} (quota {limit}, ~20-30 per batch)")
        if exclude:
            log("Excluding: " + ", ".join(exclude))
        conn = get_db()
        try:
            known = {r["tm_url"] for r in conn.execute(
                "SELECT tm_url FROM trademe_listings").fetchall()}
        finally:
            conn.close()
        items, excluded, engine = [], 0, "http"
        html = _trademe_fetch_http(category_url)
        if _trademe_has_listings(html):
            stats = {}
            raw = _trademe_parse_listings(html, category_url, cap=limit,
                                          exclude=exclude, stats=stats)
            keep = {}
            _trademe_merge_keep(keep, raw, known)
            items, excluded = list(keep.values()), stats.get("excluded", 0)
            log(f"http gave {len(items)}/{limit} new keepers — batching in the browser for the rest")
        if len(items) < limit:
            items, excluded = _trademe_collect_browser(
                category_url, limit, exclude, known, log)
            engine = "playwright"
        log(f"Kept {len(items)} new listings ({excluded} excluded, quota {limit})")
        conn = get_db()
        try:
            saved, dupes = 0, 0
            for it in items:
                exists = conn.execute(
                    "SELECT id FROM trademe_listings WHERE tm_url = ?",
                    (it["tm_url"],)).fetchone()
                if exists:
                    dupes += 1
                    continue
                # Thumbnails are NOT downloaded during scrape (faster, less
                # bandwidth) — only the source URL is kept and fetched lazily
                # when the admin opens the listing.
                conn.execute(
                    "INSERT INTO trademe_listings (tm_url, title, tm_price, status,"
                    " image_url, source_category) VALUES (?, ?, ?, 'pending', ?, ?)",
                    (it["tm_url"], it["title"], it.get("tm_price"),
                     (it.get("image") or "")[:500], category_url[:500]))
                saved += 1
            conn.commit()
        finally:
            conn.close()
        job["status"] = "done"
        job["exit_code"] = 0
        log(f"Saved {saved} new listings ({dupes} already known;"
            " thumbnails load lazily when opened).")
    except Exception as e:
        job["status"] = "failed"
        job["exit_code"] = 1
        log(f"error: {e}")
    finally:
        job["finished"] = time.time()
        job.pop("_proc", None)


@app.post("/api/trademe/scrape")
async def trademe_scrape(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    url = str(data.get("category_url", "") or "").strip()
    if not url or len(url) > 500 or not url.lower().startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "A valid http(s) category URL is required"})
    cfg = _trademe_config_load()
    if data.get("limit") not in (None, ""):
        cfg["limit"] = data.get("limit")
    if "exclude" in data:
        cfg["exclude"] = data.get("exclude")
    cfg = _trademe_config_normalize(cfg)
    job_id = secrets.token_hex(5)
    job = {"id": job_id, "kind": "trademe-scrape", "command": url,
           "status": "running", "lines": [], "exit_code": None,
           "started": time.time(), "finished": None}
    with _ADMIN_JOBS_LOCK:
        for jid in list(_ADMIN_JOBS):
            if len(_ADMIN_JOBS) > 20 and _ADMIN_JOBS[jid].get("status") != "running":
                _ADMIN_JOBS.pop(jid, None)
        _ADMIN_JOBS[job_id] = job
    threading.Thread(target=_trademe_run_scrape,
                     args=(job_id, url, cfg["limit"], cfg["exclude"]),
                     daemon=True).start()
    return {"status": "started", "job_id": job_id,
            "limit": cfg["limit"], "exclude": cfg["exclude"]}


def _trademe_margin(row: dict):
    """Upside: best-case profit (estimated_max − ask)."""
    try:
        mx = row.get("estimated_max")
        px = row.get("tm_price")
        if mx is None or px is None:
            return None
        return float(mx) - float(px)
    except (TypeError, ValueError):
        return None


def _trademe_margin_lo(row: dict):
    """Downside: worst-case outcome (estimated_min − ask). Negative means a
    potential loss if the item only fetches the low end."""
    try:
        mn = row.get("estimated_min")
        px = row.get("tm_price")
        if mn is None or px is None:
            return None
        return float(mn) - float(px)
    except (TypeError, ValueError):
        return None


def _trademe_net_profit(row: dict, fee_pct: float, shipping: float, high: bool = True):
    """Real profit: sale price, minus success fee on the sale, minus shipping
    you pay, minus what you paid. `high` uses estimated_max (upside); otherwise
    estimated_min (downside)."""
    try:
        sale = row.get("estimated_max") if high else row.get("estimated_min")
        px = row.get("tm_price")
        if sale is None or px is None:
            return None
        sale = float(sale)
        fee = sale * (float(fee_pct) / 100.0)
        return sale - fee - float(shipping) - float(px)
    except (TypeError, ValueError):
        return None


@app.get("/api/trademe/listings")
async def trademe_listings(request: Request, status: str = "", sort: str = ""):
    user, err = _require_superadmin(request)
    if err:
        return err
    status = (status or "").strip().lower()
    conds, params = [], []
    if status:
        if status not in _TRADEME_STATUS:
            return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
        conds.append("status = ?")
        params.append(status)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, tm_url, title, tm_price, estimated_min, estimated_max,"
            " status, image_path, source_category, description, predicted_min,"
            " predicted_max, image_url, images, created_at, updated_at"
            " FROM trademe_listings"
            + (" WHERE " + " AND ".join(conds) if conds else "")
            + " ORDER BY id DESC LIMIT 500", params).fetchall()
    finally:
        conn.close()
    cfg = _trademe_config_load()
    fee_pct, shipping = cfg.get("fee_pct", _TRADEME_FEE_PCT), cfg.get("shipping", 0.0)
    out = []
    for r in rows:
        rec = dict(r)
        rec["margin"] = _trademe_margin(rec)
        rec["margin_lo"] = _trademe_margin_lo(rec)
        rec["net"] = _trademe_net_profit(rec, fee_pct, shipping, high=True)
        rec["net_lo"] = _trademe_net_profit(rec, fee_pct, shipping, high=False)
        rec["image_url"] = f"/api/trademe/image/{rec['id']}" if rec.get("image_path") else ""
        out.append(rec)
    if (sort or "").strip().lower() == "margin":
        out.sort(key=lambda r: (r["net"] is None, -(r["net"] or 0)))
    return {"listings": out, "fee_pct": fee_pct, "shipping": shipping}


@app.get("/api/trademe/patterns")
async def trademe_patterns(request: Request, status: str = ""):
    """Spec facets (64GB, 1TB, DDR4, 8-core, 144Hz…) that repeat across the
    titles in view — the admin UI pins them above the list as quick filters."""
    user, err = _require_superadmin(request)
    if err:
        return err
    status = (status or "").strip().lower()
    conds, params = [], []
    if status:
        if status not in _TRADEME_STATUS:
            return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
        conds.append("status = ?")
        params.append(status)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT title FROM trademe_listings"
            + (" WHERE " + " AND ".join(conds) if conds else "")
            + " ORDER BY id DESC LIMIT 1000", params).fetchall()
    finally:
        conn.close()
    return {"patterns": _trademe_detect_patterns([r["title"] for r in rows])}


@app.post("/api/trademe/verify/{listing_id}")
async def trademe_verify(request: Request, listing_id: int):
    """Human-in-the-loop verdict. An adjusted_min/max always flips the row to
    'adjusted'; otherwise an explicit approved/rejected(/pending) applies."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Listing not found"})
        sets, params = [], []
        has_range = ("adjusted_min" in data) or ("adjusted_max" in data)
        if has_range:
            try:
                mn = float(data.get("adjusted_min")) if data.get("adjusted_min") not in (None, "") else None
                mx = float(data.get("adjusted_max")) if data.get("adjusted_max") not in (None, "") else None
            except (TypeError, ValueError):
                return JSONResponse(status_code=400, content={"error": "adjusted_min/max must be numbers"})
            if mn is not None and mn < 0:
                return JSONResponse(status_code=400, content={"error": "adjusted_min must be >= 0"})
            if mx is not None and mx < 0:
                return JSONResponse(status_code=400, content={"error": "adjusted_max must be >= 0"})
            if mn is not None and mx is not None and mn > mx:
                return JSONResponse(status_code=400, content={"error": "adjusted_min cannot exceed adjusted_max"})
            sets.append("estimated_min = ?")
            params.append(mn)
            sets.append("estimated_max = ?")
            params.append(mx)
            sets.append("status = 'adjusted'")
        else:
            want = str(data.get("status", "") or "").strip().lower()
            if want not in _TRADEME_STATUS:
                return JSONResponse(status_code=400, content={"error": "status must be pending, approved, rejected or adjusted"})
            sets.append("status = ?")
            params.append(want)
        sets.append("updated_at = datetime('now')")
        params.append(listing_id)
        conn.execute("UPDATE trademe_listings SET " + ", ".join(sets) + " WHERE id = ?", params)
        conn.commit()
        rec = dict(conn.execute(
            "SELECT id, tm_url, title, tm_price, estimated_min, estimated_max,"
            " status, image_path, source_category, description, predicted_min,"
            " predicted_max, image_url, images, created_at, updated_at"
            " FROM trademe_listings WHERE id = ?", (listing_id,)).fetchone())
        rec["margin"] = _trademe_margin(rec)
        rec["margin_lo"] = _trademe_margin_lo(rec)
        rec["image_url"] = f"/api/trademe/image/{rec['id']}" if rec.get("image_path") else ""
        return rec
    finally:
        conn.close()


@app.post("/api/trademe/description/{listing_id}")
async def trademe_description(request: Request, listing_id: int):
    """Lazy description fetch for one listing (stored on success). The
    category scrape skips item pages; the detail view calls this on open."""
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT id, tm_url, description FROM trademe_listings"
                           " WHERE id = ?", (listing_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Listing not found"})
    if row["description"]:
        return {"id": row["id"], "description": row["description"], "engine": "stored"}
    try:
        # Blocking network + sync Playwright must stay off the event loop
        # (sync API raises if called inside it).
        desc, engine = await asyncio.to_thread(_trademe_fetch_description, row["tm_url"])
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    conn = get_db()
    try:
        conn.execute("UPDATE trademe_listings SET description = ?,"
                     " updated_at = datetime('now') WHERE id = ?",
                     (desc, listing_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": listing_id, "description": desc, "engine": engine}


_TRADEME_VALUE_CHUNK = 25
_TRADEME_VALUE_MAX = 200


def _trademe_value_providers(user_id=None) -> list:
    """Models offered for bulk valuation: [{provider, model, grounded}].
    With a user id, cloud providers are limited to ones that user can actually
    call (saved key or server env key) — no dead options. Installed Ollama
    tags are always included (local, keyless) and flagged grounded, since
    every Ollama chunk gets a live market web search fed into the prompt."""
    out = []
    try:
        for pname, pcfg in (PROVIDERS or {}).items():
            if user_id is not None and _trademe_provider_key(pname, user_id) is None:
                continue
            for m in (pcfg or {}).get("models") or []:
                out.append({"provider": pname, "model": m, "grounded": False})
    except Exception:
        pass
    try:
        for tag in scan_ollama_models() or []:
            if tag and not any(o["provider"] == "ollama" and o["model"] == tag for o in out):
                out.append({"provider": "ollama", "model": tag, "grounded": True})
    except Exception:
        pass
    return out


def _trademe_provider_key(provider: str, user_id: int):
    """User's saved key for a provider, else a server env key, else None.
    Ollama is local and needs no key."""
    if provider == "ollama":
        return ""
    try:
        keys = get_user_provider_keys(user_id) or {}
        if keys.get(provider):
            return keys[provider]
    except Exception:
        pass
    try:
        for entry in (load_all_keys().get(provider) or []):
            if (entry or {}).get("key"):
                return entry["key"]
    except Exception:
        pass
    return None


def _trademe_market_context(items: list) -> str:
    """One live web search per chunk so knowledge-cutoff models (local
    Ollama especially) value against today's NZ second-hand market."""
    first = ""
    for it in items or []:
        t = str((it or {}).get("title") or "").strip()
        if len(t) >= 8:
            first = t
            break
    if not first:
        return ""
    try:
        hits = web_search(f"{first} second hand price New Zealand", limit=5) or []
    except Exception:
        return ""
    lines = []
    for h in hits:
        t = str((h or {}).get("title") or "").strip()
        s = str((h or {}).get("snippet") or "").strip()
        if t:
            lines.append(f"- {t}" + (f": {s[:220]}" if s else ""))
    if not lines:
        return ""
    out = ("CURRENT NZ SECOND-HAND MARKET SNAPSHOT (live web search — trust this "
           "over training memory):\n" + "\n".join(lines))
    hist = _trademe_sold_context(items)
    if hist:
        out += "\n\n" + hist
    return out


def _trademe_spec_key(title: str) -> str:
    """Compact spec signature for grouping sold prices (e.g. '64gb-1tb-ssd')."""
    try:
        keys = sorted(p["key"] for p in _trademe_detect_patterns([title], min_count=1))
        words = [w for w in ("ssd", "hdd", "nvme", "server", "laptop", "desktop", "gpu")
                 if w in (title or "").lower()]
        return "-".join(keys + words)[:120]
    except Exception:
        return ""


def _trademe_sold_stats(rows: list) -> dict:
    """Count/avg/median/min/max over a list of {price} rows."""
    prices = sorted(float(r["price"]) for r in (rows or [])
                    if r.get("price") is not None)
    if not prices:
        return {"count": 0}
    n = len(prices)
    mid = n // 2
    median = prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2
    return {"count": n, "avg": round(sum(prices) / n, 2), "median": round(median, 2),
            "min": prices[0], "max": prices[-1]}


def _trademe_sold_context(items: list) -> str:
    """Your own recorded sold prices that match the chunk's specs — the most
    trustworthy grounding there is, because you logged the real sale."""
    if not items:
        return ""
    keys = {_trademe_spec_key(str((it or {}).get("title") or "")) for it in items}
    keys.discard("")
    if not keys:
        return ""
    conn = get_db()
    try:
        rows = conn.execute("SELECT spec_key, price FROM trademe_sold"
                            " ORDER BY sold_at DESC LIMIT 2000").fetchall()
    finally:
        conn.close()
    matched = [dict(r) for r in rows if r["spec_key"] in keys]
    st = _trademe_sold_stats(matched)
    if not st.get("count"):
        return ""
    return ("YOUR OWN RECORDED SOLD PRICES FOR THESE SPECS (real sales — highest "
            f"trust): {st['count']} sales, median ${st['median']}, "
            f"range ${st['min']}–${st['max']}.")


def _trademe_value_system() -> str:
    return (
        "You are a strict second-hand computer hardware valuer for the New Zealand "
        "Trade Me market. All prices in NZD.\n"
        "For EACH numbered item, heavily scrutinise the title and description: pin down "
        "exact specs (RAM size/type, storage size/type, CPU, age/generation), discount "
        "hard for missing specs, vague condition, dead/for-parts wording, or crypto-mining "
        "gear (worth little). Give the realistic resale range you would confidently defend "
        "to a buyer — conservative, never optimistic. The asking price is context only; "
        "do not anchor to it.\n"
        "Reply with ONLY a JSON array, no prose, no code fences, one object per item, "
        'e.g. [{"i":0,"min":120,"max":180}]. Use nulls only when an item is truly unvaluable.'
    )


def _trademe_value_user(items: list) -> str:
    lines = []
    for n, it in enumerate(items or []):
        ask = (it or {}).get("tm_price")
        ask_s = f"${ask:,.2f}" if isinstance(ask, (int, float)) else "unknown"
        desc = str((it or {}).get("description") or "").strip().replace("\n", " ")
        if len(desc) > 500:
            desc = desc[:500] + "…"
        lines.append(f"[{n}] {(it or {}).get('title') or '(no title)'}"
                     f" | asking {ask_s} | {desc or 'no description'}")
    return "Value each item:\n" + "\n".join(lines)


def _trademe_parse_value_response(text: str, count: int) -> dict:
    """{item index: (min, max)} — skips nulls, negatives and garbage.
    Tolerates prose/fences around the JSON array."""
    out = {}
    try:
        s = text or ""
        a, b = s.find("["), s.rfind("]")
        if a < 0 or b <= a:
            return out
        data = json.loads(s[a:b + 1])
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out

    def num(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    for row in data:
        try:
            i = int((row or {}).get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= i < count):
            continue
        mn, mx = num((row or {}).get("min")), num((row or {}).get("max"))
        if mn is None and mx is None:
            continue
        if mn is not None and mx is not None and mn > mx:
            mn, mx = mx, mn
        out[i] = (mn, mx)
    return out


def _trademe_llm_value(provider: str, model: str, user_keys: dict,
                       items: list, market: str = "") -> tuple:
    """One model call valuing a chunk of items through the app's own provider
    routing (any cloud provider, or a local ollama/<tag>). Returns
    ({index: (min, max)}, actually_used_model)."""
    user_text = _trademe_value_user(items)
    if market:
        user_text += "\n\n" + market
    messages = [{"role": "system", "content": _trademe_value_system()},
                {"role": "user", "content": user_text}]
    model_ref = f"ollama/{model}" if provider == "ollama" else model
    try:
        resp, used = generate(
            messages, model=model_ref, temperature=0.2, max_tokens=8192,
            stream=False, user_keys=user_keys or {},
            opts={"temperature": 0.2, "max_tokens": 8192, "health_scope": "trademe"})
        text = ((resp or {}).get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as e:
        raise RuntimeError(f"{provider}/{model}: {e}")
    vals = _trademe_parse_value_response(text, len(items))
    if not vals:
        raise RuntimeError(f"{provider}/{model} returned no usable valuations")
    return vals, used


def _trademe_run_value(job_id: str, ids: list, provider: str, model: str,
                       user_keys: dict, chunk: int, ground: bool):
    job = _ADMIN_JOBS.get(job_id)
    if job is None:
        return
    log = job["lines"].append
    try:
        total = 0
        ids = [int(x) for x in (ids or [])]
        for s in range(0, len(ids), max(1, chunk)):
            batch = ids[s:s + max(1, chunk)]
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id, title, tm_price, description FROM trademe_listings"
                    f" WHERE id IN ({','.join('?' * len(batch))})", batch).fetchall()
            finally:
                conn.close()
            items = [dict(r) for r in rows]
            if not items:
                continue
            market = ""
            if ground:
                log(f"chunk {s // max(1, chunk) + 1}: grounding on live market prices…")
                market = _trademe_market_context(items)
            log(f"valuing {s + 1}-{s + len(items)} of {len(ids)} via {provider}/{model}…")
            vals, used = _trademe_llm_value(provider, model, user_keys, items, market)
            conn = get_db()
            try:
                n = 0
                for i, it in enumerate(items):
                    if i in vals:
                        mn, mx = vals[i]
                        conn.execute("UPDATE trademe_listings SET estimated_min = ?,"
                                     " estimated_max = ?, updated_at = datetime('now')"
                                     " WHERE id = ?", (mn, mx, it["id"]))
                        n += 1
                conn.commit()
            finally:
                conn.close()
            total += n
            log(f"chunk done: {n}/{len(items)} valued via {used} ({total} total)")
            time.sleep(2)
        job["status"] = "done"
        job["exit_code"] = 0
        log(f"Valued {total} of {len(ids)} listings with {provider}/{model}.")
    except Exception as e:
        job["status"] = "failed"
        job["exit_code"] = 1
        log(f"error: {e}")
    finally:
        job["finished"] = time.time()
        job.pop("_proc", None)


@app.get("/api/trademe/models")
async def trademe_models(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    options = _trademe_value_providers(user["id"])
    default = next((o for o in options if o["provider"] == "gemini"), options[0] if options else None)
    return {"options": options,
            "default_provider": (default or {}).get("provider", ""),
            "default_model": (default or {}).get("model", ""),
            "chunk_default": _TRADEME_VALUE_CHUNK}


@app.post("/api/trademe/value")
async def trademe_value(request: Request):
    """Bulk valuation with ANY configured model (one background job, chunked
    requests). Each strict min/max is written straight into the row's ESTIMATED
    value; local Ollama picks get one live web search per chunk so they value
    against today's market instead of stale training memory. Review, then
    Apply to approve everything at once."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    options = _trademe_value_providers()
    provider = str(data.get("provider") or "").strip().lower()
    model = str(data.get("model") or "").strip()
    if not provider and model:
        # Back-compat: bare model name → find its provider.
        hit = next((o for o in options if o["model"] == model), None)
        if hit:
            provider, model = hit["provider"], hit["model"]
    if not provider and not model:
        gem = next((o for o in options if o["provider"] == "gemini"), None)
        if gem:
            provider, model = gem["provider"], gem["model"]
    valid = [o["model"] for o in options if o["provider"] == provider] if provider else []
    if provider == "ollama":
        try:
            live = scan_ollama_models() or []
        except Exception:
            live = []
        if model not in live:
            return JSONResponse(status_code=400, content={"error": f"Ollama model '{model}' is not installed"})
    elif not provider or not model or model not in valid:
        return JSONResponse(status_code=400, content={"error": "Unknown provider/model"})
    try:
        chunk = max(5, min(50, int(data.get("chunk") or _TRADEME_VALUE_CHUNK)))
        maxn = max(1, min(_TRADEME_VALUE_MAX, int(data.get("max") or _TRADEME_VALUE_MAX)))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "chunk/max must be numbers"})
    overwrite = bool(data.get("overwrite"))
    api_key = _trademe_provider_key(provider, user["id"])
    if api_key is None:
        return JSONResponse(status_code=400, content={
            "error": f"No {provider} API key — add one under Providers (or set it on the server)"})
    user_keys = {} if provider == "ollama" else {provider: api_key}
    conn = get_db()
    try:
        if data.get("ids") is not None:
            try:
                wanted = [int(x) for x in data.get("ids")][:maxn]
            except (TypeError, ValueError):
                return JSONResponse(status_code=400, content={"error": "ids must be numbers"})
            if not wanted:
                return JSONResponse(status_code=400, content={"error": "No listings selected"})
            rows = conn.execute(
                "SELECT id FROM trademe_listings"
                f" WHERE id IN ({','.join('?' * len(wanted))})", wanted).fetchall()
            ids = [r["id"] for r in rows]
        else:
            status = str(data.get("status") or "").strip().lower()
            conds, params = [], []
            if status:
                if status not in _TRADEME_STATUS:
                    return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
                conds.append("status = ?")
                params.append(status)
            if not overwrite:
                # Never clobber human estimates — only fill blanks.
                conds.append("(estimated_min IS NULL AND estimated_max IS NULL)")
            rows = conn.execute(
                "SELECT id FROM trademe_listings"
                + (" WHERE " + " AND ".join(conds) if conds else "")
                + " ORDER BY id LIMIT ?", params + [maxn]).fetchall()
            ids = [r["id"] for r in rows]
    finally:
        conn.close()
    if not ids:
        return JSONResponse(status_code=400, content={"error": "Nothing to value"})
    ground = provider == "ollama" or bool(data.get("ground"))
    job_id = secrets.token_hex(5)
    job = {"id": job_id, "kind": "trademe-value",
           "command": f"{len(ids)} via {provider}/{model}",
           "status": "running", "lines": [], "exit_code": None,
           "started": time.time(), "finished": None}
    with _ADMIN_JOBS_LOCK:
        for jid in list(_ADMIN_JOBS):
            if len(_ADMIN_JOBS) > 20 and _ADMIN_JOBS[jid].get("status") != "running":
                _ADMIN_JOBS.pop(jid, None)
        _ADMIN_JOBS[job_id] = job
    threading.Thread(target=_trademe_run_value,
                     args=(job_id, ids, provider, model, user_keys, chunk, ground),
                     daemon=True).start()
    return {"status": "started", "job_id": job_id, "count": len(ids),
            "provider": provider, "model": model, "grounded": ground}


@app.post("/api/trademe/approve-all")
async def trademe_approve_all(request: Request):
    """Apply button: approve every row in the filter that already carries an
    estimated value (bulk-valued or hand-valued). Rows without any estimate
    are left alone."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    status = str((data or {}).get("status") or "pending").strip().lower()
    if status not in _TRADEME_STATUS:
        return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE trademe_listings SET status = 'approved', updated_at = datetime('now')"
            " WHERE status = ? AND (estimated_min IS NOT NULL OR estimated_max IS NOT NULL)",
            (status,))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "approved": n or 0}


@app.post("/api/trademe/clear-values")
async def trademe_clear_values(request: Request):
    """Reset valuations so a filter can be re-valued from scratch. Clears all
    four value columns (estimated + predicted); scope 'estimated' clears only
    the estimated pair. Statuses are untouched."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    status = str(data.get("status") or "").strip().lower()
    scope = str(data.get("scope") or "all").strip().lower()
    if status and status not in _TRADEME_STATUS:
        return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
    if scope not in ("estimated", "all"):
        return JSONResponse(status_code=400, content={"error": "scope must be 'estimated' or 'all'"})
    sets = "estimated_min = NULL, estimated_max = NULL"
    if scope == "all":
        sets += ", predicted_min = NULL, predicted_max = NULL"
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE trademe_listings SET {sets}, updated_at = datetime('now') " + where,
            params)
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "cleared": n or 0, "scope": scope}


@app.post("/api/trademe/sold")
async def trademe_sold_add(request: Request):
    """Record a real sale (yours or a confirmed sold price). This is the
    ground-truth signal — sold prices, unlike asking prices, don't lie — and it
    feeds back into future valuations."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    title = str(data.get("title") or "").strip()
    listing_id = data.get("listing_id")
    conn = get_db()
    try:
        if not title and listing_id:
            r = conn.execute("SELECT title, source_category FROM trademe_listings"
                             " WHERE id = ?", (listing_id,)).fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "Listing not found"})
            title = r["title"] or ""
            data.setdefault("category", r["source_category"] or "")
        if not title:
            return JSONResponse(status_code=400, content={"error": "A title is required"})
        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "price must be a number"})
        if price < 0:
            return JSONResponse(status_code=400, content={"error": "price must be >= 0"})
        cur = conn.execute(
            "INSERT INTO trademe_sold (listing_id, title, category, price, spec_key, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(listing_id) if listing_id else None, title[:200],
             str(data.get("category") or "")[:200], price,
             _trademe_spec_key(title), str(data.get("note") or "")[:500]))
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return {"status": "ok", "id": new_id}


@app.get("/api/trademe/sold")
async def trademe_sold_list(request: Request, limit: int = 100):
    user, err = _require_superadmin(request)
    if err:
        return err
    limit = max(1, min(1000, int(limit or 100)))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, listing_id, title, category, price, spec_key, note, sold_at"
            " FROM trademe_sold ORDER BY sold_at DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
        allrows = conn.execute("SELECT price FROM trademe_sold").fetchall()
    finally:
        conn.close()
    recent = [dict(r) for r in rows]
    return {"sold": recent, "stats": _trademe_sold_stats([dict(r) for r in allrows])}


@app.delete("/api/trademe/sold/{sold_id}")
async def trademe_sold_delete(request: Request, sold_id: int):
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM trademe_sold WHERE id = ?", (sold_id,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if not n:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"status": "ok", "id": sold_id}


@app.post("/api/trademe/predict/{listing_id}")
async def trademe_predict(request: Request, listing_id: int):
    """Store a model's valuation for later comparison against the human's
    adjusted range. This is the write side of the train → predict → correct
    loop; see /api/trademe/export for the read side."""
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    try:
        mn = float(data.get("predicted_min")) if data.get("predicted_min") not in (None, "") else None
        mx = float(data.get("predicted_max")) if data.get("predicted_max") not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "predicted_min/max must be numbers"})
    if (mn is not None and mn < 0) or (mx is not None and mx < 0):
        return JSONResponse(status_code=400, content={"error": "predictions must be >= 0"})
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Listing not found"})
        conn.execute("UPDATE trademe_listings SET predicted_min = ?, predicted_max = ?,"
                     " updated_at = datetime('now') WHERE id = ?", (mn, mx, listing_id))
        conn.commit()
        rec = dict(conn.execute(
            "SELECT id, tm_url, title, tm_price, estimated_min, estimated_max,"
            " status, image_path, source_category, description, predicted_min,"
            " predicted_max, image_url, images, created_at, updated_at"
            " FROM trademe_listings WHERE id = ?", (listing_id,)).fetchone())
        rec["margin"] = _trademe_margin(rec)
        rec["margin_lo"] = _trademe_margin_lo(rec)
        rec["image_url"] = f"/api/trademe/image/{rec['id']}" if rec.get("image_path") else ""
        return rec
    finally:
        conn.close()


@app.delete("/api/trademe/listings/{listing_id}")
async def trademe_delete(request: Request, listing_id: int):
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT image_path FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Listing not found"})
        conn.execute("DELETE FROM trademe_listings WHERE id = ?", (listing_id,))
        conn.commit()
    finally:
        conn.close()
    if row["image_path"]:
        try:
            full = os.path.join(_trademe_thumb_dir(), os.path.basename(row["image_path"]))
            if _is_within(_trademe_thumb_dir(), full):
                os.remove(full)
        except OSError:
            pass
    return {"status": "ok", "id": listing_id}


@app.delete("/api/trademe/listings")
async def trademe_delete_all(request: Request, status: str = ""):
    """Clear the store. Optional ?status= wipes just one bucket; default is
    everything. Downloaded thumbnails are removed alongside the rows."""
    user, err = _require_superadmin(request)
    if err:
        return err
    status = (status or "").strip().lower()
    if status and status not in _TRADEME_STATUS:
        return JSONResponse(status_code=400, content={"error": "Unknown status filter"})
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT image_path, images FROM trademe_listings " + where, params).fetchall()
        cur = conn.execute("DELETE FROM trademe_listings " + where, params)
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    thumb_dir = _trademe_thumb_dir()
    if not status:
        # Full clear: wipe every cached thumbnail, including orphans.
        names = None
    else:
        names = []
        for r in rows:
            if r["image_path"]:
                names.append(r["image_path"])
            if r["images"]:
                try:
                    names.extend(json.loads(r["images"]))
                except (TypeError, ValueError):
                    pass
    try:
        if names is None:
            # Full clear: drop every cached image, including orphans — but
            # never touch non-image files sharing the directory.
            for fn in os.listdir(thumb_dir):
                if os.path.splitext(fn)[1].lower() not in (
                        ".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    continue
                try:
                    os.remove(os.path.join(thumb_dir, fn))
                except OSError:
                    pass
        else:
            for n in names:
                full = os.path.join(thumb_dir, os.path.basename(n))
                if _is_within(thumb_dir, full) and os.path.isfile(full):
                    os.remove(full)
    except OSError:
        pass
    return {"status": "ok", "deleted": deleted or 0}


@app.get("/api/trademe/settings")
async def trademe_settings(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    cfg = _trademe_config_load()
    return {"limit": cfg["limit"], "exclude": cfg["exclude"],
            "fee_pct": cfg["fee_pct"], "shipping": cfg["shipping"],
            "limit_options": list(_TRADEME_LIMIT_OPTIONS)}


@app.post("/api/trademe/settings")
async def trademe_settings_save(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    # Merge over the stored config so a partial save (e.g. only fee_pct)
    # never wipes the limit/exclusions.
    cfg = _trademe_config_save({**_trademe_config_load(), **(data or {})})
    return {"status": "ok", "limit": cfg["limit"], "exclude": cfg["exclude"],
            "fee_pct": cfg["fee_pct"], "shipping": cfg["shipping"],
            "limit_options": list(_TRADEME_LIMIT_OPTIONS)}


@app.post("/api/trademe/thumbnail/{listing_id}")
async def trademe_thumbnail(request: Request, listing_id: int):
    """Download one listing's thumbnail on demand (first time it is viewed),
    then serve it from disk. Scraping never downloads images."""
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT id, image_url, image_path FROM trademe_listings"
                           " WHERE id = ?", (listing_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Listing not found"})
    if row["image_path"]:
        return {"id": listing_id, "image_url": f"/api/trademe/image/{listing_id}"}
    if not row["image_url"]:
        return {"id": listing_id, "image_url": ""}
    name = _trademe_thumb_save(row["image_url"])
    if not name:
        return {"id": listing_id, "image_url": ""}
    conn = get_db()
    try:
        conn.execute("UPDATE trademe_listings SET image_path = ?,"
                     " updated_at = datetime('now') WHERE id = ?", (name, listing_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": listing_id, "image_url": f"/api/trademe/image/{listing_id}"}


@app.post("/api/trademe/gallery/{listing_id}")
async def trademe_gallery(request: Request, listing_id: int):
    """All images for one listing, downloaded lazily on first open and cached.
    The category page only offers one thumbnail; the item page holds the set."""
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT id, tm_url, image_url, image_path, images"
                           " FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Listing not found"})
    stored = []
    if row["images"]:
        try:
            stored = json.loads(row["images"])
        except (TypeError, ValueError):
            stored = []
    if not stored:
        urls = []
        try:
            urls = await asyncio.to_thread(_trademe_fetch_gallery, row["tm_url"])
        except Exception:
            urls = []
        if row["image_url"] and row["image_url"] not in urls:
            urls.insert(0, row["image_url"])
        for u in urls[:_TRADEME_GALLERY_CAP]:
            name = _trademe_thumb_save(u)
            if name:
                stored.append(name)
        if stored:
            conn = get_db()
            try:
                first = row["image_path"] or stored[0]
                conn.execute("UPDATE trademe_listings SET images = ?, image_path = ?,"
                             " updated_at = datetime('now') WHERE id = ?",
                             (json.dumps(stored), first, listing_id))
                conn.commit()
            finally:
                conn.close()
    return {"id": listing_id,
            "images": [f"/api/trademe/image/{listing_id}/{i}" for i in range(len(stored))]}


@app.get("/api/trademe/image/{listing_id}/{idx}", include_in_schema=False)
async def trademe_image_at(request: Request, listing_id: int, idx: int):
    from fastapi.responses import Response as _Response
    import mimetypes
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT images FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
    finally:
        conn.close()
    names = []
    if row and row["images"]:
        try:
            names = json.loads(row["images"])
        except (TypeError, ValueError):
            names = []
    if not (0 <= idx < len(names)):
        return JSONResponse(status_code=404, content={"error": "Image not found"})
    thumb_dir = _trademe_thumb_dir()
    full = os.path.join(thumb_dir, os.path.basename(names[idx]))
    if not _is_within(thumb_dir, full) or not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "Image not found"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        return _Response(content=content,
                         media_type=mimetypes.guess_type(full)[0] or "image/jpeg",
                         headers={"Cache-Control": "private, max-age=86400"})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/trademe/export")
async def trademe_export(request: Request):
    """Training data download: one JSON object per human-labelled row
    (approved/rejected/adjusted) for the BERT value model. Re-export any
    time after correcting predictions — that file IS the training set."""
    from fastapi.responses import Response as _Response
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, tm_url, title, description, tm_price, estimated_min,"
            " estimated_max, predicted_min, predicted_max, status"
            " FROM trademe_listings WHERE status IN ('approved','rejected','adjusted')"
            " ORDER BY id").fetchall()
    finally:
        conn.close()
    lines = []
    for r in rows:
        d = dict(r)
        lines.append(json.dumps({
            "title": d.get("title") or "", "description": d.get("description") or "",
            "tm_price": d.get("tm_price"), "estimated_min": d.get("estimated_min"),
            "estimated_max": d.get("estimated_max"),
            "predicted_min": d.get("predicted_min"),
            "predicted_max": d.get("predicted_max"),
            "status": d.get("status"), "tm_url": d.get("tm_url")}))
    return _Response(content=("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                     media_type="application/x-ndjson",
                     headers={"Content-Disposition":
                              "attachment; filename=trademe_labels.jsonl"})


@app.get("/api/trademe/image/{listing_id}")
async def trademe_image(request: Request, listing_id: int):
    from fastapi.responses import Response as _Response
    import mimetypes
    user, err = _require_superadmin(request)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT image_path FROM trademe_listings WHERE id = ?",
                           (listing_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["image_path"]:
        return JSONResponse(status_code=404, content={"error": "No image for this listing"})
    full = os.path.join(_trademe_thumb_dir(), os.path.basename(row["image_path"]))
    if not _is_within(_trademe_thumb_dir(), full) or not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "Image not found"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        return _Response(content=content,
                         media_type=mimetypes.guess_type(full)[0] or "image/jpeg",
                         headers={"Cache-Control": "private, max-age=86400"})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ==========================================================================
# Phase 5 — model intelligence: usage costs + budgets.
# ==========================================================================





# ---- usage costs + budgets (money control over API spend) ----
# Rough USD per 1M tokens (prompt, completion). Estimates only — local Ollama
# is free, unknown cloud models fall back to a conservative default.
_COST_PER_M = {
    "gpt-4o-mini": (0.15, 0.6), "gpt-4o": (2.5, 10.0), "gpt-4-turbo": (10.0, 30.0),
    "gpt-3.5": (0.5, 1.5), "claude": (3.0, 15.0), "gemini": (0.1, 0.4),
    "gemma": (0.1, 0.4), "deepseek": (0.27, 1.1), "mistral": (0.2, 0.6),
    "command": (0.15, 0.6), "llama": (0.1, 0.3), "qwen": (0.1, 0.3),
    "mixtral": (0.24, 0.24), "gpt-oss": (0.1, 0.4), "nemotron": (0.1, 0.4),
}


def _model_price(provider, model):
    if (provider or "").lower() == "ollama":
        return (0.0, 0.0)
    m = (model or "").lower()
    for key, price in _COST_PER_M.items():
        if key in m:
            return price
    return (0.5, 1.5)


def _cost_usd(provider, model, ptok, ctok) -> float:
    p, c = _model_price(provider, model)
    return round(ptok / 1e6 * p + ctok / 1e6 * c, 6)


def _usage_range_modifier(range_: str) -> str:
    r = (range_ or "24h").strip().lower()
    return {"24h": "-24 hours", "7d": "-7 days", "30d": "-30 days",
            "month": "start of month"}.get(r, "-24 hours")


def _budget_get(user_id) -> dict:
    conn = get_db()
    try:
        row = conn.execute("SELECT monthly_usd, warn_pct, enforce FROM budgets"
                           " WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"monthly_usd": 0.0, "warn_pct": 80, "enforce": False}
    return {"monthly_usd": float(row["monthly_usd"] or 0),
            "warn_pct": int(row["warn_pct"] or 80), "enforce": bool(row["enforce"])}


def _month_spend(user_id) -> float:
    conn = get_db()
    try:
        rows = conn.execute("SELECT provider, model, prompt_tokens, completion_tokens"
                            " FROM usage_events WHERE user_id = ?"
                            " AND ts >= datetime('now','start of month')", (user_id,)).fetchall()
    finally:
        conn.close()
    return round(sum(_cost_usd(r["provider"], r["model"],
                               r["prompt_tokens"] or 0, r["completion_tokens"] or 0)
                     for r in rows), 6)


def _budget_status(user_id) -> dict:
    b = _budget_get(user_id)
    spent = _month_spend(user_id)
    cap = b["monthly_usd"]
    pct = round(100.0 * spent / cap, 1) if cap > 0 else 0.0
    return {"monthly_usd": cap, "spent_usd": spent, "percent": pct,
            "warn_pct": b["warn_pct"], "enforce": b["enforce"],
            "over": bool(cap > 0 and spent >= cap),
            "warn": bool(cap > 0 and pct >= b["warn_pct"])}


@app.get("/api/usage/costs")
async def usage_costs(request: Request, range: str = "24h"):
    user, err = _require_auth(request)
    if err:
        return err
    mod = _usage_range_modifier(range)
    cond = ("ts >= datetime('now','start of month')" if mod == "start of month"
            else "ts >= datetime('now', ?)")
    params = [user["id"]] if mod == "start of month" else [user["id"], mod]
    conn = get_db()
    try:
        rows = conn.execute("SELECT provider, model, COUNT(*) AS calls,"
                            " SUM(prompt_tokens) AS ptok, SUM(completion_tokens) AS ctok"
                            " FROM usage_events WHERE user_id = ? AND " + cond +
                            " GROUP BY provider, model", params).fetchall()
    finally:
        conn.close()
    by_model, total = [], 0.0
    for r in rows:
        cost = _cost_usd(r["provider"], r["model"], r["ptok"] or 0, r["ctok"] or 0)
        total += cost
        by_model.append({"provider": r["provider"], "model": r["model"], "calls": r["calls"],
                         "prompt_tokens": r["ptok"] or 0, "completion_tokens": r["ctok"] or 0,
                         "cost_usd": round(cost, 4)})
    by_model.sort(key=lambda x: -x["cost_usd"])
    return {"range": range, "total_usd": round(total, 4), "by_model": by_model,
            "budget": _budget_status(user["id"])}


@app.get("/api/usage/budget")
async def usage_budget_get(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    return _budget_status(user["id"])


@app.post("/api/usage/budget")
async def usage_budget_set(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    try:
        cap = max(0.0, float(data.get("monthly_usd", 0) or 0))
        warn = max(0, min(100, int(data.get("warn_pct", 80) or 80)))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "monthly_usd/warn_pct must be numbers"})
    enforce = 1 if data.get("enforce") else 0
    conn = get_db()
    try:
        conn.execute("INSERT INTO budgets (user_id, monthly_usd, warn_pct, enforce)"
                     " VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET"
                     " monthly_usd = excluded.monthly_usd, warn_pct = excluded.warn_pct,"
                     " enforce = excluded.enforce", (user["id"], cap, warn, enforce))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", **_budget_status(user["id"])}


@app.get("/api/settings")
async def get_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    db_provider_keys = get_user_provider_keys(user["id"])
    settings = {
        "backup_model": get_backup_model(),
        "ollama_url": _ollama_current_url(),
        "language": _user_language(_safe_username(user["username"])),
        "timezone": _user_timezone(_safe_username(user["username"])),
        "is_admin": bool(user.get("is_admin")),
    }
    for prov_name, prov_cfg in PROVIDERS.items():
        prefix = prov_cfg["env_prefix"]
        settings[prov_name] = {
            "env_prefix": prefix,
            "keys": [],
        }
        if db_provider_keys and prov_name in db_provider_keys:
            key_val = db_provider_keys[prov_name]
            masked = key_val[:4] + "..." + key_val[-4:] if len(key_val) > 4 else "***"
            settings[prov_name]["keys"].append({
                "env_name": f"{prefix}_API_KEY",
                "masked": masked,
                "has_value": True,
            })
        elif user.get("is_admin"):

            # Server .env keys are only visible to the admin
            for i in range(1, 6):
                key_env = f"{prefix}_{i}"
                value = os.environ.get(key_env, "")
                if value and len(value) > 4:
                    masked = value[:4] + "..." + value[-4:]
                elif value:
                    masked = "***"
                else:
                    masked = ""
                settings[prov_name]["keys"].append({
                    "env_name": key_env,
                    "masked": masked,
                    "has_value": bool(value and len(value) > 4),
                })
    return settings

def _valid_env_assignment(key_name: str, key_value: str) -> Optional[str]:
    """Validate a server-key write. Returns an error string or None if OK."""
    if not key_name or not re.fullmatch(r"[A-Z0-9_]{1,64}", key_name):
        return "Invalid key name"
    if not key_value or len(key_value) > 500 or any(ord(c) < 32 for c in key_value):
        return "Invalid key value"
    return None


@app.post("/api/settings")
async def update_settings(request: Request, req: SettingsRequest):
    _user, err = _require_admin(request)
    if err:
        return err
    bad = _valid_env_assignment(req.key_name, req.key_value)
    if bad:
        return JSONResponse(status_code=400, content={"error": bad})
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    target = f"{req.key_name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(target):
            lines[i] = f"{req.key_name}={req.key_value}\n"
            found = True
            break
    if not found:
        lines.append(f"\n{req.key_name}={req.key_value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ[req.key_name] = req.key_value
    logger.info(f"Settings updated: {req.key_name}")
    return {"status": "ok", "message": f"Updated {req.key_name}"}

@app.post("/api/settings/bulk")
async def update_settings_bulk(request: Request):
    _user, err = _require_admin(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    updates = body.get("updates", [])
    for update in updates:
        key_name = update.get("key_name", "")
        key_value = update.get("key_value", "")
        if not key_name:
            continue
        if _valid_env_assignment(key_name, key_value):
            continue
        target = f"{key_name}="
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(target):
                lines[i] = f"{key_name}={key_value}\n"
                found = True
                break
        if not found:
            lines.append(f"{key_name}={key_value}\n")
        os.environ[key_name] = key_value
    with open(env_path, "w") as f:
        f.writelines(lines)
    logger.info(f"Bulk settings updated: {len(updates)} keys")
    return {"status": "ok", "message": f"Updated {len(updates)} keys"}

@app.post("/api/settings/clear")
async def clear_key(request: Request, req: ClearKeyRequest):
    _user, err = _require_admin(request)
    if err:
        return err
    if not req.key_name or not re.fullmatch(r"[A-Z0-9_]{1,64}", req.key_name):
        return JSONResponse(status_code=400, content={"error": "Invalid key name"})
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    target = f"{req.key_name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(target):
            lines[i] = f"{req.key_name}=\n"
            break
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ.pop(req.key_name, None)
    logger.info(f"Key cleared: {req.key_name}")
    return {"status": "ok", "message": f"Cleared {req.key_name}"}

@app.get("/api/language")
async def get_language_setting(http_request: Request):
    user, err = _require_auth(http_request)
    if err:
        return err
    return {"language": _user_language(_safe_username(user["username"]))}

class LanguageRequest(BaseModel):
    language: str

@app.post("/api/language")
async def set_language_setting(http_request: Request, request: LanguageRequest):
    """Language is stored per account: one user's choice can no longer rewrite
    the process-wide setting (and the system prompt) for everybody else."""
    user, err = _require_auth(http_request)
    if err:
        return err
    lang = (request.language or "").strip()
    if not _LANGUAGE_RE.fullmatch(lang):
        return JSONResponse(status_code=400, content={"error": "Invalid language"})
    _save_user_pref(_safe_username(user["username"]), "language", lang)
    logger.info(f"Language set to: {lang} user={user['username']}")
    return {"status": "ok", "language": lang}


@app.get("/api/chats")
async def list_chats(request: Request):
    """All saved conversations for the account (cloud-synced)."""
    user, err = _require_auth(request)
    if err:
        return err
    return {"chats": _load_user_chats(_safe_username(user["username"]))}

class ChatSaveRequest(BaseModel):
    id: str
    title: str = ""
    history: list = []
    model: str = ""
    updated: float = 0.0

@app.post("/api/chats")
async def save_chat(request: Request, req: ChatSaveRequest):
    """Upsert one conversation so it follows the account across devices."""
    user, err = _require_auth(request)
    if err:
        return err
    if not _CHAT_ID_RE.fullmatch(req.id or ""):
        return JSONResponse(status_code=400, content={"error": "Invalid chat id"})
    if not isinstance(req.history, list) or len(req.history) > 1000:
        return JSONResponse(status_code=400, content={"error": "Invalid history"})
    uid = _safe_username(user["username"])
    entry = {"title": str(req.title or "")[:200],
             "history": req.history,
             "model": str(req.model or "")[:80],
             "updated": float(req.updated or time.time())}
    if len(json.dumps(entry)) > _CHAT_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "Chat too large to sync"})
    chats = _load_user_chats(uid)
    chats[req.id] = entry
    _save_user_chats(uid, _prune_chats(chats))
    return {"status": "ok", "id": req.id, "updated": entry["updated"]}

@app.delete("/api/chats/{chat_id}")
async def delete_chat(request: Request, chat_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    if not _CHAT_ID_RE.fullmatch(chat_id or ""):
        return JSONResponse(status_code=400, content={"error": "Invalid chat id"})
    uid = _safe_username(user["username"])
    chats = _load_user_chats(uid)
    if chat_id in chats:
        del chats[chat_id]
        _save_user_chats(uid, chats)
    return {"status": "ok"}


@app.get("/api/timezone")
async def get_timezone_setting(http_request: Request):
    """The account's saved IANA timezone plus the live local time it maps to."""
    user, err = _require_auth(http_request)
    if err:
        return err
    uid = _safe_username(user["username"])
    localized, iso = _localized_now(uid)
    return {"timezone": _user_timezone(uid), "local": localized, "iso": iso,
            "server": datetime.datetime.now().strftime("%A, %d %B %Y, %H:%M")}

class TimezoneRequest(BaseModel):
    timezone: str

@app.post("/api/timezone")
async def set_timezone_setting(http_request: Request, request: TimezoneRequest):
    """Region/timezone is stored per account and used to give the AI the real
    local date and time. Empty string clears it (fall back to server clock)."""
    user, err = _require_auth(http_request)
    if err:
        return err
    tz = (request.timezone or "").strip()
    if tz and not _valid_timezone(tz):
        return JSONResponse(status_code=400, content={"error": "Unknown timezone — use an IANA name like Europe/London"})
    uid = _safe_username(user["username"])
    _save_user_pref(uid, "timezone", tz)
    localized, iso = _localized_now(uid)
    logger.info(f"Timezone set to: {tz or '(server)'} user={user['username']}")
    return {"status": "ok", "timezone": tz, "local": localized, "iso": iso}

def _split_used_model(used_model: str, viewer=None):
    """'groq/openai/gpt-oss-120b' -> ('groq', 'openai/gpt-oss-120b').

    The provider is always the first path segment; everything after it is the
    model name (which may itself contain slashes, as groq's do). Device ids
    ('device/<agent>/<tag>') resolve to the paired device's display name so
    usage rows read 'MSI/llama3.1:8b' instead of '1/llama3.1:8b' — but only
    when the viewer may access that computer. Otherwise the opaque id stays.
    """
    if used_model and used_model.startswith("device/"):
        parts = used_model.split("/", 2)
        if len(parts) == 3:
            return "device", f"{_device_display_name(parts[1], viewer)}/{parts[2]}"
    if used_model and "/" in used_model:
        provider, _, model = used_model.partition("/")
        return provider or "unknown", model or used_model
    return "unknown", used_model or "unknown"


def _device_display_name(agent_id, viewer=None):
    """Paired device display name for usage/attribution, else its id."""
    try:
        with _AGENT_LOCK:
            st = _agents_load()
            a = st["agents"].get(str(agent_id))
        if a and (viewer is None or _agent_visible(viewer, a)):
            return str(a.get("name") or a.get("hostname") or agent_id)
    except Exception:
        pass
    return str(agent_id)


def _resolve_request_model(req_model, viewer=None):
    """Best-effort (provider, model) for a requested model name.

    Direct selections like 'openai/gpt-oss-120b' resolve to the provider that
    actually serves them (groq, here). The auto-router ('omniilink') has no
    single provider, so it stays unknown. Used to attribute failed requests
    to the right provider/model instead of lumping them into 'unknown'.
    """
    if not req_model or req_model in (MODEL_NAME, "omnilink"):
        return "unknown", req_model or "unknown"
    if req_model.startswith("ollama/"):
        return "ollama", req_model.split("/", 1)[1]
    if req_model.startswith("device/"):
        parts = req_model.split("/", 2)
        if len(parts) == 3:
            return "device", f"{_device_display_name(parts[1], viewer)}/{parts[2]}"
        return "device", req_model
    for prov_name, prov_cfg in PROVIDERS.items():
        if req_model in prov_cfg.get("models", []):
            return prov_name, req_model
    return "unknown", req_model


@app.get("/api/websearch")
async def api_websearch(request: Request, q: str = "", limit: int = 8):
    user, err = _require_auth(request)
    if err:
        return err
    query = (q or "").strip()
    if not query:
        return {"query": "", "results": []}
    try:
        limit = max(1, min(10, int(limit)))
    except (TypeError, ValueError):
        limit = 8
    try:
        return {"query": query, "results": web_search(query, limit)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/usage")
async def api_usage(request: Request, range: str = "24h"):
    user, err = _require_auth(request)
    if err:
        return err
    hours = {"1h": 1, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(range, 24)
    # Never 500 the whole page for one failing section: return partial data
    # with a degraded flag so the UI renders what it has and says what broke.
    degraded = None
    try:
        data = get_usage(user_id=user["id"], range_hours=hours)
    except Exception as e:
        logger.error(f"[USAGE] aggregate failed: {e}", exc_info=True)
        degraded = f"usage data unavailable ({e})"
        data = {
            "range_hours": hours, "bucket_fmt": "%Y-%m-%d %H:%M",
            "summary": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "avg_duration_ms": 0,
                        "max_duration_ms": 0, "active_days": 0},
            "all_time": {"requests": 0, "tokens": 0},
            "timeseries": [], "by_model": [], "by_provider": [],
            "by_key": [], "recent": [],
        }
    try:
        data["rate_limits"] = lb.rate_limiter.get_status()
    except Exception as e:
        logger.error(f"[USAGE] rate-limit status failed: {e}", exc_info=True)
        data["rate_limits"] = {}
        degraded = degraded or "rate-limit status unavailable"
    data["model_name"] = MODEL_NAME
    if degraded:
        data["degraded"] = degraded
    return data

@app.get("/api/network")
async def api_network(request: Request):

    # Traffic totals describe every account on the box — admin only.
    user, err = _require_admin(request)
    if err:
        return err
    _audit("network.view", user, {"ip": request.client.host if request.client else ""})
    return network_usage.snapshot()

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list) or len(messages) > 200:
        return JSONResponse(status_code=400, content={"error": "Invalid messages"})
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant"):
            return JSONResponse(status_code=400, content={"error": "Invalid message entry"})
        content = m.get("content")
        if isinstance(content, str):
            if len(content) > 500000:
                return JSONResponse(status_code=400, content={"error": "Invalid message entry"})
        elif isinstance(content, list):

            # Multimodal content (text + image_url parts) — must be non-empty.
            if not content:
                return JSONResponse(status_code=400, content={"error": "Invalid message entry"})
        else:
            return JSONResponse(status_code=400, content={"error": "Invalid message entry"})

    temp = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 1000)
    is_stream = body.get("stream", False)
    req_model = body.get("model", MODEL_NAME)
    if not isinstance(req_model, str) or not re.fullmatch(r"[a-zA-Z0-9._/:-]{1,80}", req_model):
        return JSONResponse(status_code=400, content={"error": "Invalid model name"})
    try:
        temp = max(0.0, min(2.0, float(temp)))
        max_tokens = max(1, min(40000, int(max_tokens)))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "Invalid temperature or max_tokens"})
    is_stream = bool(is_stream)


    # Paired-device models are private: the owning account, an explicitly
    # shared account, or (only with the support escape hatch enabled) an
    # admin may use them. Reject before anything runs.
    if req_model.startswith("device/"):
        parts = req_model.split("/", 2)
        agent = None
        if len(parts) == 3 and parts[1].isdigit():
            with _AGENT_LOCK:
                st = _agents_load()
                agent = st["agents"].get(parts[1])
        if not agent:
            return JSONResponse(status_code=400, content={"error": "Invalid model name"})
        if not _agent_visible(user, agent):
            return JSONResponse(status_code=403, content={
                "error": "This device's models belong to the account that paired it."})


    # Optional generation controls from the UI menus: invalid values are

    # coerced to "auto" (provider default) instead of rejecting the request.
    effort = normalize_level(body.get("effort"), "auto")
    thinking = normalize_level(body.get("thinking"), "auto")
    req_opts = {"temperature": temp, "max_tokens": max_tokens, "effort": effort, "thinking": thinking,
                "language": _user_language(_safe_username(user["username"])),
                "health_scope": health_scope(request)}
    if body.get("response_modalities"):
        req_opts["response_modalities"] = body.get("response_modalities")
    if body.get("tools"):
        req_opts["tools"] = body.get("tools")


    # Key policy: admin uses the server's .env keys; everyone else must use their own keys.
    user_keys = None
    if not user.get("is_admin"):
        db_keys = get_user_provider_keys(user["id"])
        db_customs = get_custom_providers(user["id"])
        if not db_keys and not db_customs and not req_model.startswith(("ollama/", "device/")):
            return JSONResponse(status_code=403, content={"error": "No API keys configured. Add your providers under Providers."})
        user_keys = db_keys

    auth_header = request.headers.get("Authorization", "")
    api_key_id = None
    if auth_header.startswith("Bearer ml-"):
        api_key_id = get_api_key_id_by_key(auth_header[7:])


    # House system prompt (identity + web_search/DuckDuckGo contract) when

    # the caller did not supply its own system message. Coding turns always

    # carry one, so they are unaffected.
    _sys_uid = _safe_username(user["username"])
    # Budget enforcement (opt-in per user): block once the monthly cap is hit.
    try:
        _bs = _budget_status(user["id"])
        if _bs.get("enforce") and _bs.get("over"):
            return JSONResponse(status_code=402, content={
                "error": f"Monthly budget of ${_bs['monthly_usd']:.2f} reached "
                         f"(spent ${_bs['spent_usd']:.2f}). Raise it in Usage → Costs."})
    except Exception:
        pass
    _has_system = any(isinstance(m, dict) and m.get("role") == "system" and m.get("content")
                      for m in messages)
    if not _has_system:
        messages = [{"role": "system", "content": _chat_system_prompt(_sys_uid)}] + list(messages)
    else:

        # A caller-supplied prompt (coding, or a custom system message): append

        # the live clock so the model always knows the exact time and date.
        messages = list(messages)
        for _i, _m in enumerate(messages):
            if isinstance(_m, dict) and _m.get("role") == "system" and _m.get("content"):
                _m = dict(_m)
                _m["content"] = str(_m["content"]).rstrip() + "\n" + _time_line(_sys_uid)
                messages[_i] = _m
                break
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    est_prompt_tokens = max(1, prompt_chars // 4)
    start_ts = time.time()
    logger.info(
        f"[CHAT] Request from user={user.get('username', '-')} "
        f"model={req_model} stream={is_stream} "
        f"messages={len(messages)} prompt_chars={prompt_chars} "
        f"est_tokens~{est_prompt_tokens} temp={temp} max_tokens={max_tokens} "
        f"effort={effort} thinking={thinking}"
    )

    def _finish_usage(used_model: str, completion_tokens: int = 0, status: int = 200):
        provider, model_name = _split_used_model(used_model, user)
        dur_ms = int((time.time() - start_ts) * 1000)
        record_usage(
            user_id=user["id"],
            model=model_name,
            provider=provider,
            prompt_tokens=est_prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=dur_ms,
            source="chat",
            status=status,
            api_key_id=api_key_id,
        )
        logger.info(
            f"[CHAT] {provider}/{model_name} {dur_ms}ms "
            f"prompt~{est_prompt_tokens} completion~{completion_tokens} user={user.get('username', '-')}"
        )

    custom_row = None
    if req_model.startswith("custom/"):
        custom_row = get_custom_provider(user["id"], req_model.split("/", 1)[1].lower())
        if not custom_row:
            return JSONResponse(status_code=404, content={"error": "Custom provider not found. Re-add it under Providers."})

    if is_stream:
        req_tools = body.get("tools")
        if req_tools or body.get("websearch"):
            tool_opts = dict(req_opts or {})
            tool_opts["tools"] = req_tools or [WEB_SEARCH_TOOL]
            try:
                tool_response, tool_used = await asyncio.to_thread(
                    run_tool_loop, messages, req_model, temp, max_tokens,
                    user_keys, tool_opts,
                )
            except Exception as e:
                logger.error(f"[CHAT] tool loop failed: {e}", exc_info=True)
                prov, mname = _resolve_request_model(req_model, user)
                _finish_usage(f"{prov}/{mname}", completion_tokens=0, status=500)
                return JSONResponse(status_code=500, content={"error": str(e)})
            tool_content = ""
            try:
                tool_content = tool_response["choices"][0]["message"].get("content") or ""
            except Exception:
                pass
            chat_id = "chatcmpl-" + str(int(time.time()))
            created_time = int(time.time())
            async def tool_event_gen():
                base = {"id": chat_id, "object": "chat.completion.chunk",
                        "created": created_time, "model": tool_used}
                if tool_content:
                    chunk = dict(base, choices=[{"index": 0, "delta": {"content": tool_content}, "finish_reason": None}])
                    yield "data: " + json.dumps(chunk) + "\n\n"
                finish = dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
                yield "data: " + json.dumps(finish) + "\n\n"
                yield "data: [DONE]\n\n"
            _finish_usage(tool_used, completion_tokens=max(1, len(tool_content) // 4))
            return StreamingResponse(tool_event_gen(), media_type="text/event-stream")
        try:
            if custom_row is not None:
                stream, used_model = await asyncio.to_thread(
                    lb.generate_custom,
                    messages,
                    custom_row["name"],
                    custom_row["base_url"],
                    custom_row["api_key"],
                    custom_row["model"],
                    temp,
                    max_tokens,
                    True,
                    req_opts,
                )
            else:
                stream, used_model = await asyncio.to_thread(
                    generate,
                    messages=messages,
                    model=req_model,
                    temperature=temp,
                    max_tokens=max_tokens,
                    stream=True,
                    user_keys=user_keys,
                    opts=req_opts,
                )
        except Exception as e:
            logger.error(f"[CHAT] generate() failed: {e}", exc_info=True)
            prov, mname = _resolve_request_model(req_model)
            _finish_usage(f"{prov}/{mname}", completion_tokens=0, status=500)

            # NOTE: capture the text NOW — `except ... as e` deletes `e`

            # when the block exits, so a closure reading str(e) later would

            # raise NameError mid-stream (empty 200, connection killed).
            err_text = str(e)
            async def error_event_gen():
                chat_id = "chatcmpl-" + str(int(time.time()))
                created_time = int(time.time())
                error_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created_time, "model": req_model,
                    "choices": [{"index": 0, "delta": {"content": f"Error: {err_text}"}, "finish_reason": "stop"}]
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                finish_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created_time, "model": req_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                }
                yield f"data: {json.dumps(finish_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(error_event_gen(), media_type="text/event-stream")

        async def event_generator():
            chat_id = "chatcmpl-" + str(int(time.time()))
            created_time = int(time.time())
            out_chars = 0
            finished = False
            try:
                if isinstance(stream, dict):
                    text_chunk = stream.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if text_chunk:
                        out_chars += len(text_chunk)
                        chunk_data = {
                            "id": chat_id, "object": "chat.completion.chunk",
                            "created": created_time, "model": used_model,
                            "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk_data)}\n\n"
                else:

                    # Pump the blocking chunk iterator on ONE worker thread and
                    # poll it from the loop. The relay's first batch can take
                    # minutes (a deep-reasoning model thinks before its first
                    # token), and proxies answer 524 when a response sends
                    # nothing for ~100s — while the loop itself must stay free
                    # (device-relay batches arrive over HTTP on this same
                    # server; blocking here would starve them until the
                    # first-timeout fires). An SSE comment keeps the
                    # connection alive; every client ignores non-"data:" lines.
                    # Polling (never wait_for) means no abandoned thread can
                    # swallow a chunk.
                    _stream_iter = iter(stream)
                    _chunk_q = queue.Queue()

                    def _pump_stream():
                        try:
                            for _c in _stream_iter:
                                _chunk_q.put(("chunk", _c))
                        except Exception as _pe:
                            _chunk_q.put(("error", _pe))
                        finally:
                            _chunk_q.put(("done", None))

                    _pump_thread = threading.Thread(target=_pump_stream, daemon=True)
                    _pump_thread.start()
                    yield ": connected\n\n"
                    _idle_ticks = 0
                    while True:
                        try:
                            _kind, _payload = _chunk_q.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(1)
                            _idle_ticks += 1
                            if _idle_ticks >= 25:
                                _idle_ticks = 0
                                yield ": ping\n\n"
                            continue
                        _idle_ticks = 0
                        if _kind == "done":
                            break
                        if _kind == "error":
                            raise _payload
                        chunk = _payload
                        text_chunk = ""
                        if hasattr(chunk, 'text'):
                            text_chunk = chunk.text
                        elif isinstance(chunk, str):
                            text_chunk = chunk
                        if text_chunk:
                            out_chars += len(text_chunk)
                            chunk_data = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": used_model,
                                "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(chunk_data)}\n\n"
            except Exception as e:
                logger.error(f"[CHAT] stream iteration error: {e}", exc_info=True)
                error_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created_time, "model": used_model,
                    "choices": [{"index": 0, "delta": {"content": f"\n[Error: {str(e)}]"}, "finish_reason": "stop"}]
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                _finish_usage(used_model, completion_tokens=max(1, out_chars // 4), status=500)
                finished = True
            finally:
                if not finished:
                    _finish_usage(used_model, completion_tokens=max(1, out_chars // 4))

            finish_chunk = {
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created_time, "model": used_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(finish_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        try:
            if custom_row is not None:
                response, used_model = await asyncio.to_thread(
                    lb.generate_custom,
                    messages,
                    custom_row["name"],
                    custom_row["base_url"],
                    custom_row["api_key"],
                    custom_row["model"],
                    temp,
                    max_tokens,
                    False,
                    req_opts,
                )
            else:
                req_tools = body.get("tools")
                if req_tools or body.get("websearch"):
                    tool_opts = dict(req_opts or {})
                    tool_opts["tools"] = req_tools or [WEB_SEARCH_TOOL]
                    response, used_model = await asyncio.to_thread(
                        run_tool_loop, messages, req_model, temp, max_tokens,
                        user_keys, tool_opts,
                    )
                else:
                    response, used_model = await asyncio.to_thread(
                        generate,
                        messages=messages,
                        model=req_model,
                        temperature=temp,
                        max_tokens=max_tokens,
                        stream=False,
                        user_keys=user_keys,
                        opts=req_opts,
                    )
        except Exception as e:
            prov, mname = _resolve_request_model(req_model)
            _finish_usage(f"{prov}/{mname}", status=500)
            return JSONResponse(status_code=500, content={"error": str(e)})
        usage = (response or {}).get("usage") or {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if completion_tokens <= 0:
            content = ""
            try:
                content = response["choices"][0]["message"].get("content") or ""
            except Exception:
                pass
            completion_tokens = max(1, len(content) // 4)
        if usage.get("prompt_tokens"):
            pass  # keep est_prompt_tokens from request side
        _finish_usage(used_model, completion_tokens=completion_tokens)
        return JSONResponse(content=response)

@app.get("/v1/models")
async def list_models(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    provider_keys = get_provider_keys(request)
    models = [{"id": MODEL_NAME}]
    for prov_name, prov_cfg in PROVIDERS.items():
        if prov_name in provider_keys:
            for m in prov_cfg.get("models", []):
                if not any(existing["id"] == m for existing in models):
                    models.append({"id": m})
            for m in prov_cfg.get("fallback_models", []):
                if not any(existing["id"] == m for existing in models):
                    models.append({"id": m})
    try:
        for m in scan_ollama_models():
            mid = "ollama/" + m
            if not any(existing["id"] == mid for existing in models):
                models.append({"id": mid})
    except Exception:
        pass
    try:
        for entry in await asyncio.to_thread(_device_model_entries, _user):
            if not any(existing["id"] == entry["id"] for existing in models):
                models.append({"id": entry["id"]})
    except Exception:
        pass
    return {"data": models}


# ==========================================

# Coding Mode

# ==========================================
IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", "bin", "obj",
    ".idea", ".vscode", ".vs", "coverage", ".cache", "tmp", "temp",
    ".tox", "eggs", "*.egg-info", ".mypy_cache", ".pytest_cache",
    "vendor", ".gradle", ".terraform", ".vagrant",
}
IGNORE_EXTS = {
    ".pyc", ".pyo", ".class", ".o", ".obj", ".dll", ".exe", ".so",
    ".dylib", ".pdb", ".ilk", ".exp", ".lib", ".dll",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flv",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot",
    ".min.js", ".min.css",
}

def _build_file_tree(root_path, prefix="", patterns=None):
    entries = []
    try:
        items = sorted(os.listdir(root_path))
    except PermissionError:
        return entries
    dirs = []
    files = []
    for name in items:
        full = os.path.join(root_path, name)
        # Symlinks can point outside the project (including another user's
        # files). Never follow or expose them in the project tree.
        if os.path.islink(full):
            continue
        if os.path.isdir(full):
            if name not in IGNORE_DIRS and not name.startswith("."):
                dirs.append(name)
        else:

            # Every file shows in the tree (archives, media, binaries included);

            # only dotfiles are hidden. Directories above still skip the big

            # generated trees (node_modules, .git, ...).
            if not name.startswith("."):
                files.append(name)
    for d in dirs:
        sub_path = os.path.join(root_path, d)
        children = _build_file_tree(sub_path, prefix + d + "/", patterns)
        rel = prefix + d
        ignored = _omniiignore_ignored(rel, patterns or [])
        entries.append({"name": d, "type": "dir", "path": rel, "children": children, "ignored": ignored})
    for f in files:
        rel = prefix + f
        size = os.path.getsize(os.path.join(root_path, f))
        entries.append({"name": f, "type": "file", "path": rel, "size": size,
                        "ignored": _omniiignore_ignored(rel, patterns or [])})
    return entries


OMNIIIGNORE_NAME = ".omniiignore"

# Coding conversation lives inside the project under this hidden folder (the

# file tree and auto-attach skip dotfiles, so it never shows up or gets sent

# to the model). Storing it in the project makes it travel with the project.
CODING_CHAT_DIR = ".omniilink"
CODING_CHAT_FILE = "coding-chat.json"


def _read_omniiignore(folder):
    """Read .omniiignore patterns from the project root (gitignore-style)."""
    path = os.path.join(folder, OMNIIIGNORE_NAME)
    patterns = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                patterns.append(line)
    except Exception:
        pass
    return patterns


def _omniiignore_ignored(rel_path, patterns):
    """True when a project-relative path matches an .omniiignore pattern.

    Supports exact names, trailing-slash dirs, and glob wildcards (*.log).
    A directory pattern ignores everything beneath it. The tree still lists
    ignored items (so the model sees folder names like node_modules) — only
    their contents are kept out of the attached context.
    """
    if not patterns:
        return False
    import fnmatch
    rel = rel_path.replace("\\", "/")
    parts = [p for p in rel.split("/") if p]
    name = parts[-1] if parts else ""
    for pat in patterns:
        p = pat.strip().rstrip("/")
        if not p:
            continue
        if any(c in p for c in "*?["):
            if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p):
                return True
            if any(fnmatch.fnmatch(part, p) for part in parts):
                return True
        else:
            if rel == p or rel.startswith(p + "/") or name == p:
                return True
            if any(part == p for part in parts):
                return True
    return False


def _count_tree_files(entries):
    """Count file entries in a _build_file_tree result (matches the tree)."""
    n = 0
    for e in entries or []:
        if e.get("type") == "dir":
            n += _count_tree_files(e.get("children") or [])
        else:
            n += 1
    return n

def _chat_system_prompt(user_id=""):
    """Default system prompt for /v1/chat/completions, used only when the
    caller did not supply its own system message. Short on purpose — small
    local models degrade with long preambles. Teaches the web_search tool
    contract (name + query argument) and how to treat DuckDuckGo results."""
    now = _localized_now(user_id)[0] if user_id else \
        datetime.datetime.now().strftime("%A, %d %B %Y, %H:%M")
    return (
        "You are Omniilink, a helpful AI assistant. Answer directly and concisely.\n"
        "Always write in proper, grammatically correct English: start every sentence "
        "with a capital letter and end it with correct punctuation (full stop, question "
        "mark or exclamation mark). Never reply in all-lowercase or without punctuation.\n"
        f"Current date and time: {now}. Use this as the exact present moment — "
        "your training data is older, so never assume the date from memory.\n"
        "If a web_search tool is offered and the question needs current or external "
        "facts (news, prices, recent events, anything past your training), call it as "
        "{\"query\": \"<search terms>\"} and answer from the returned results. "
        "Never claim to have browsed when you have not.\n"
        "Web results (when provided) come from the DuckDuckGo instant-answer API as "
        "title/snippet lines: synthesize them in your own words, prefer recent sources "
        "for time-sensitive questions, and say plainly when the results do not answer it.\n"
        "With no web tool offered, answer from your own knowledge; when unsure or the "
        "facts may have changed, say so instead of guessing."
    )


# --- System prompt: OWNED BY THE SERVER, never editable by users ---------
#
# The prompt used to be one blob with "The server runs on WINDOWS" baked in,
# and the client then appended an "authoritative" EXECUTION ENVIRONMENT block
# for the real OS. On a Docker/cloud host that produced a prompt that said
# WINDOWS in the body and Linux in the block, so models mixed the two and
# emitted `ls`/`/work/...` against a Windows project.
#
# It is now split so there is exactly ONE source of truth: the server decides
# which profile applies (hosting mode), serves that complete prompt, and the
# client renders it verbatim. Two full heads, one shared tail.

_CODING_PROMPT_INTRO = """You are an expert coding assistant inside the Omniilink Coding Mode. You can create, edit, delete files and run commands. The user sees your explanations; a parser executes your blocks.
Write in proper, grammatically correct English: capitalise the first word of every sentence and end with correct punctuation. Never reply in all-lowercase.
"""

_CODING_PROMPT_ENV_SERVER = """
ENVIRONMENT (memorize this — AUTHORITATIVE, it overrides anything else you were told):
- HOSTING: OmniLink Server, running natively on the user's own WINDOWS machine.
- Shell: cmd.exe, which is ALREADY a shell.
- Working directory is the open project folder. All paths are relative to it.
- NEVER prefix commands with `cmd /c` or `cmd /k` — it is already implied and breaks parsing.
- NEVER wrap a command in quotes. Write it bare on one single line.
- One command per RUN_COMMAND block. Chain with `&&` if needed.
- Forward slashes work in paths (e.g. `type src/app.js`).
- WRONG, never use these here: `ls`, `cat`, `rm`, `python3`, `which`, `mkdir -p`, `sh -c`, and NEVER write `/work/...` paths or `/work` — that is a container path that does not exist on a Windows host.

COMMAND REFERENCE (use exactly these forms):
- List files: dir /b
- List with sizes: dir
- Show a file: type path\\to\\file
- Make a folder: mkdir foldername
- Delete a file: del filename
- Run Python: python filename.py  (check first: python --version)
- Run JavaScript: node filename.js  (check first: node --version)
- Browser JavaScript is different: if a .js file is loaded by an HTML page, or
  uses window/document/canvas or a CDN global such as THREE, validate it with
  `node --check filename.js` only. Never execute it with node; verify it by
  previewing the HTML page.
- Packages: pip install name  /  npm install
- Git: git status / git add -A / git commit -m "msg"
- Fetch a web page: WEB_FETCH:https://example.com (below)
"""

_CODING_PROMPT_ENV_CLOUD = """
ENVIRONMENT (memorize this — AUTHORITATIVE, it overrides anything else you were told):
- HOSTING: OmniLink Cloud. Commands do NOT run on a Windows machine — they run inside a LINUX container.
- Shell: sh, which is ALREADY a shell.
- Working directory inside the container is /work, which maps to the open project folder. Paths are relative to it.
- NEVER prefix commands with `sh -c` or `bash -c` — the runner already wraps them.
- NEVER wrap a command in quotes. Write it bare on one single line.
- One command per RUN_COMMAND block. Chain with `&&` if needed.
- WRONG, never use these here: `dir`, `type`, `del`, `python` (without 3), `where`, and NEVER write Windows paths like `C:\\` or `src\\app.js`.
- In FILE_CREATE / FILE_EDIT / FILE_PATCH / FILE_DELETE blocks always use plain relative paths such as `index.html` or `src/app.js`. The parser writes straight to the host folder — putting `/work/` in a block path breaks it.

COMMAND REFERENCE (use exactly these forms):
- List files: ls
- List with sizes: ls -la
- Show a file: cat path/to/file
- Make a folder: mkdir -p name
- Delete a file: rm name
- Run Python: python3 file.py  (check first: python3 --version)
- Run JavaScript: node file.js  (check first: node --version)
- Browser JavaScript is different: if a .js file is loaded by an HTML page, or
  uses window/document/canvas or a CDN global such as THREE, validate it with
  `node --check file.js` only. Never execute it with node; verify it by
  previewing the HTML page.
- Packages: pip3 install name  /  npm install
- Git: git status / git add -A / git commit -m "msg"
- Fetch a web page: WEB_FETCH:https://example.com (below)
"""

CODING_PROMPT_TAIL = """BLOCK SYNTAX (exact — the parser is strict):
- Each block starts on its own line. No code fences around blocks. No quotes around the command.
- FILE_CREATE:path/to/file.ext — full file content between markers, then END_FILE_CREATE.
- FILE_EDIT:path/to/file.ext — COMPLETE new file content, then END_FILE_EDIT.
- FILE_DELETE:path/to/file.ext then END_FILE_DELETE on the next line.
- RUN_COMMAND:your command here (same line, or on the next line), then END_RUN_COMMAND on its own line.
- FILE_PATCH:path/to/file.ext — SURGICAL fix, preferred for repairs.
  Copy the exact lines to change under OLD:, new lines under NEW::
FILE_PATCH:styles.css
OLD:
  border-radius: 50
NEW:
  border-radius: 50px;
END_FILE_PATCH
  Use PATCH for fixes (never rewrite a whole working file to fix a few
  lines). Use FILE_CREATE only for brand-new files.
- WEB_FETCH:https://example.com — fetch a web page as text, then
  END_WEB_FETCH on the next line. Use it to read docs or look something up.

WORKFLOW:
1. First inspect: RUN_COMMAND:dir /b (so you know what exists).
   READING FILES: do NOT read project files with `type`, `cat`, or Python
   one-liners in RUN_COMMAND — command output is TRUNCATED and you will burn
   your whole budget re-reading in chunks. The files the user attached (or
   the project's source) are already provided to you. If you truly need a
   file that is not attached, ask the user once to attach it with @name.
   Then go straight to the edits.
2. For any task with 2+ steps, start with a TODO_LIST block and mark items done as you go.
   Split the work into SMALL steps: one file, one feature, or one check per
   item. NEVER write one giant item covering the whole task — "Create a 3D
   scene with cabin, trees, terrain, fog, lighting and controls" must become
   an item per part, not a single item:
TODO_LIST:
- [ ] Inspect project files
- [ ] Create cabin in index.html
- [ ] Add pine trees
- [ ] Add terrain and fog
- [ ] Add lighting and controls
- [ ] Verify file and syntax
END_TODO_LIST
Update it in later replies (repeat the full list with [x] on finished items).
3. Explain briefly, then emit blocks.
4. After creating code, verify it and report the output. Run server-side code
   with python/node. For browser JavaScript, use `node --check` only, then
   preview the HTML page. Never rerun a passing validation command on a file
   you have not changed since that validation.
5. Deliverables (code files) MUST go in FILE_CREATE/FILE_EDIT blocks — never
bare fenced code alone, or the system cannot save them.
6. NEVER describe a task as done/finished/complete while any [ ] item
remains. The system independently verifies that every file you name
actually exists AND is non-empty — false completion claims are rejected
and sent back.
7. Keep chat text SHORT (2-4 sentences). NEVER paste file contents into
   chat text — code lives in FILE_CREATE/FILE_EDIT blocks only.
8. Start every reply with AT MOST ONE short status sentence, and never
   announce the same step twice in different words. If you show a code
   fence without a FILE_* block, write its filename on the line above it
   like `File: engine.js`. Always explain what you are doing.

STEP PLANNING (how this run is budgeted):
- At the START of a task, before any tool block, write ONE line declaring how
  many tool rounds YOU judge the task needs: PLAN-STEPS: N
  (one round = one reply that runs tool blocks).
- PLAN-STEPS is ONLY a round counter — it does NOT replace your TODO_LIST.
  For any task with 2+ steps you still start with a TODO_LIST block naming
  every file you will create, so progress can be verified file by file.
  Split it SMALL: one file, one feature, or one check per item - never one
  giant item for the whole task.
- Count GENEROUSLY: inspection + one round per file + a verification round +
  a round per likely fix (a 3-file website is roughly 6-8 rounds). A plan of
  1-2 steps for a multi-file task is wrong; under-declaring costs a round.
- You get exactly N rounds — there is no other cap. Never stop early because
  of a counter; finish when the work is verified.
- PACK every round: emit all independent actions in ONE reply — several
  FILE_CREATE/FILE_EDIT blocks and several RUN_COMMANDs together. Spending a
  whole round on one action wastes the budget you declared.
- Never end a round with prose only while steps remain and any [ ] todo is
  open: either send the next tool blocks or say plainly what blocks you.
- If mid-run you realize you need more, write a new line PLAN-STEPS: M with a
  higher number and the harness grants it (you may recount up to 3 times).

Example:
I will check the project contents first.

RUN_COMMAND:dir /b
END_RUN_COMMAND

Then I will create the page.

FILE_CREATE:index.html
<!DOCTYPE html>
<html><body><h1>Hello</h1></body></html>
END_FILE_CREATE

Rules:
- For FILE_EDIT, include the COMPLETE file content, not just changes.
- Always put the file path right after the colon, no spaces before the path.
- When you genuinely need the user to decide something (a name, a color,
  whether to add a feature), ask with EXACTLY this format (max 3 questions
  per turn, only blocking decisions — never ask what you can decide):
QUESTION:What background color should the site use?
OPTION:A warm cream (#F5EFE6)
OPTION:Deep charcoal (#141414)
END_QUESTION
- You are an expert in Python, JavaScript, TypeScript, HTML, CSS, Java, C, C++, Go, Rust, Ruby, PHP, and all other programming languages.

INTERACTIVE COMPLETENESS (games, sims, anything the user runs):
- Every control the user asked for MUST exist and be wired on first
  delivery: keyboard (WASD/arrows/space via keydown/keyup state maps),
  mouse look (pointer lock requested on canvas click), touch buttons on
  mobile, and a working START/PLAY button that actually begins the
  experience. A start button that does nothing is a failed delivery —
  wire every button to a real handler with a visible effect.
- Prefer battle-tested patterns over invention: browser games get a
  <canvas> render loop with requestAnimationFrame, pointer-lock mouse
  look, key state maps, and resize handling. 3D via three.js from CDN
  with a pinned version (confirm the version exists with WEB_FETCH if
  unsure); 2D via plain canvas. No build step, no modules the preview
  cannot load.
- Textures/models: procedural canvas textures and low-poly geometry
  beat hotlinked assets (which often 404). If you reference an external
  asset, WEB_FETCH the URL first to confirm it exists; otherwise
  generate it in code.
- ENTRY POINT: the main file (index.html) must run standalone when
  opened or previewed. If there are multiple pages, index.html links
  them all and every link target exists (verify with dir /b).
- VERIFY INTERACTIVITY: after writing, re-read the file and trace every
  user-facing control end to end (button -> handler -> visible effect).
  If a control cannot work (missing API, dead URL), say so plainly and
  offer the closest working alternative instead of shipping it broken.
"""


# Prompt profiles. `server` = native Windows host (OmniLink Server), `cloud` =
# Docker container (OmniLink Cloud). Users never choose; the server reports
# what commands will ACTUALLY run and serves the matching prompt.
_CODING_PROMPT_BY_HOSTING = {
    "server": _CODING_PROMPT_INTRO + _CODING_PROMPT_ENV_SERVER + CODING_PROMPT_TAIL,
    "cloud": _CODING_PROMPT_INTRO + _CODING_PROMPT_ENV_CLOUD + CODING_PROMPT_TAIL,
}

# Back-compat alias for existing imports/tests. Resolved per request by
# _coding_exec_env() so it never lies about the live environment.
DEFAULT_CODING_PROMPT = _CODING_PROMPT_BY_HOSTING["server"]


def _coding_exec_env() -> dict:
    """Authoritative description of where RUN_COMMAND actually executes.

    The coding prompt is built from this and nothing else, so the prompt can
    never describe an OS different from the one the runner uses. Mirrors the
    branch order in coding_run_command(): sandbox -> container, else host.
    """
    sb = _sandbox_config()
    if sb is not None:
        # Both docker (fresh per command) and project (persistent) modes run
        # inside the Linux sandbox image with /work as the cwd.
        return {
            "hosting": "cloud",
            "exec_os": "linux",
            "exec_shell": "sh",
            "sandbox_mode": sb["mode"],
            "sandbox": True,
            "container_workdir": "/work",
            "persistent": sb["mode"] == "project",
        }
    if sys.platform == "win32":
        return {"hosting": "server", "exec_os": "windows", "exec_shell": "cmd",
                "sandbox_mode": "", "sandbox": False,
                "container_workdir": "", "persistent": False}
    return {"hosting": "server", "exec_os": "linux", "exec_shell": "sh",
            "sandbox_mode": "", "sandbox": False,
            "container_workdir": "", "persistent": False}


def _coding_system_prompt(user_id: str = "") -> str:
    """The one true coding system prompt for this server.

    Ignores anything stored in the user's settings: the prompt is server-owned
    so a user (or a stale save from another machine) cannot pin the wrong OS.
    """
    env = _coding_exec_env()
    return _CODING_PROMPT_BY_HOSTING.get(env["hosting"], DEFAULT_CODING_PROMPT)


# Per-user coding states handled by _get_coding_state()

# ---- Collaborative workspaces -----------------------------------------
_WS_LOCK_TTL = 90
_WS_EVENT_CAP = 200
_WS_MAX_MEMBERS = 5
_ws_locks = {}
_ws_events_mutex = threading.Lock()
_ws_pubsub = {}


def _workspace_row(folder):
    """Workspace row (or None) when `folder` has a workspace.json marker."""
    try:
        marker = os.path.join(os.path.abspath(folder or ""), ".omniilink", "workspace.json")
        if not os.path.isfile(marker):
            return None
        with open(marker, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        ws_id = int(data.get("workspace_id") or 0)
        if not ws_id:
            return None
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return dict(row)
    except Exception:
        return None


def _workspace_by_id(ws_id):
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (int(ws_id),)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return dict(row)
    except Exception:
        return None


def _workspace_role(user, folder):
    """'view'/'edit' when `user` is a member of the workspace for `folder`."""
    try:
        ws = _workspace_row(folder)
        if not ws:
            return None
        conn = get_db()
        try:
            if ws["owner_id"] == user["id"] or user.get("is_admin"):
                return "edit"
            r = conn.execute("SELECT role FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                             (ws["id"], user["id"])).fetchone()
        finally:
            conn.close()
        if not r:
            return None
        return (r["role"] or "edit")
    except Exception:
        return None


def _ws_members(ws_id):
    conn = get_db()
    try:
        rows = conn.execute("SELECT user_id, role FROM workspace_members WHERE workspace_id = ?",
                            (ws_id,)).fetchall()
        return [(r["user_id"], (r["role"] or "edit")) for r in rows]
    finally:
        conn.close()


def _ws_events_file(folder):
    return os.path.join(folder, ".omniilink", "workspace-events.json")


def _ws_read_events(folder):
    try:
        with open(_ws_events_file(folder), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _ws_write_events(folder, events):
    with _ws_events_mutex:
        try:
            os.makedirs(os.path.join(folder, ".omniilink"), exist_ok=True)
            with open(_ws_events_file(folder), "w", encoding="utf-8") as fh:
                json.dump(events[-_WS_EVENT_CAP:], fh)
        except Exception:
            pass


def _ws_append_event(folder, ev):
    """Record a change event + bump the pubsub token so pollers learn fast."""
    ev = dict(ev)
    ev.setdefault("at", time.time())
    events = _ws_read_events(folder)
    events.append(ev)
    _ws_write_events(folder, events)
    try:
        t = time.time()
        _ws_pubsub[os.path.abspath(folder)] = t
        for key in list(_ws_pubsub.keys()):
            if t - _ws_pubsub[key] > 1800:
                _ws_pubsub.pop(key, None)
    except Exception:
        pass


def _ws_chat_file(folder, user_id):
    return os.path.join(folder, ".omniilink", f"chat-{user_id}.json")


def _ws_lock_key(folder, relpath):
    return (os.path.abspath(folder), os.path.normpath(relpath or "").lstrip("\\/"))


def _ws_take_lock(user, folder, relpath):
    """Claim a per-file lock. Returns an error dict or None on success."""
    key = _ws_lock_key(folder, relpath)
    now = time.time()
    cur = _ws_locks.get(key)
    if cur and now - cur["at"] < _WS_LOCK_TTL and cur["user_id"] != user["id"]:
        return {"error": "File is locked by another editor", "locked_by": cur["username"], "locked_by_id": cur["user_id"]}
    _ws_locks[key] = {"user_id": user["id"], "username": user.get("username", ""), "at": now}
    _ws_append_event(folder, {"type": "lock", "path": relpath,
                              "user": user.get("username", ""), "user_id": user["id"]})
    return None


def _ws_release_lock(user, folder, relpath):
    key = _ws_lock_key(folder, relpath)
    cur = _ws_locks.get(key)
    if cur and cur["user_id"] == user["id"]:
        del _ws_locks[key]
        _ws_append_event(folder, {"type": "unlock", "path": relpath,
                                  "user": user.get("username", ""), "user_id": user["id"]})


def _ws_cleanup_locks(folder=None):
    now = time.time()
    for key in list(_ws_locks.keys()):
        if folder and key[0] != os.path.abspath(folder):
            continue
        if now - _ws_locks[key]["at"] >= _WS_LOCK_TTL:
            _ws_locks.pop(key, None)


def _ws_live_locks(folder):
    """Current non-stale locks for a folder (for the poll response)."""
    _ws_cleanup_locks(folder)
    out = []
    for key, cur in _ws_locks.items():
        if key[0] == os.path.abspath(folder):
            out.append({"path": key[1], "user": cur["username"], "user_id": cur["user_id"], "at": cur["at"]})
    return out


def _ws_enforce_lock(user, folder, relpath):
    """Server-side file lock for collaborator workspaces.

    In a workspace folder, mutating `relpath` requires the caller to hold its
    lock (client acquires it at send time). Returns an error JSONResponse
    when another member currently holds it, else None (refreshing an existing
    lock of the same user so long AI actions keep it alive)."""
    if not _workspace_row(folder):
        return None
    _ws_cleanup_locks(folder)
    key = _ws_lock_key(folder, relpath)
    cur = _ws_locks.get(key)
    if cur and cur["user_id"] != user["id"]:
        return JSONResponse(status_code=409, content={
            "error": "File is locked by another editor",
            "locked_by": cur["username"], "locked_by_id": cur["user_id"]})
    if cur:
        cur["at"] = time.time()
    else:
        _ws_take_lock(user, folder, relpath)
    return None


def _ws_log_change(folder, evtype, relpath, user, user_id, extra=None):
    """Record a workspace change event for the live feed, if folder is a workspace."""
    if not folder or not os.path.isdir(folder):
        return
    if not os.path.isfile(os.path.join(folder, ".omniilink", "workspace.json")):
        return
    ev = {"type": evtype, "path": relpath, "user": user.get("username", ""), "user_id": user_id}
    if extra:
        ev.update(extra)
    _ws_append_event(folder, ev)


def _folder_shared_role(folder, user_id):
    """'view'/'edit' when `user_id` has a share on this exact folder, else None."""
    try:
        norm = os.path.abspath(folder or "")
        conn = get_db()
        try:
            rows = conn.execute("SELECT folder, role FROM project_shares"
                                " WHERE shared_with_id = ?", (user_id,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            if os.path.abspath(r["folder"] or "") == norm:
                return (r["role"] or "edit")
    except Exception:
        pass
    return None


def _can_access_folder(user, folder) -> bool:
    """True when the user owns/administers the folder, it is shared with them,
    or they are a member of its collaborative workspace."""
    if user.get("is_admin"):
        return True
    if _is_within(_user_data_dir(_safe_username(user["username"])), folder):
        return True
    if _folder_shared_role(folder, user["id"]) is not None:
        return True
    return _workspace_role(user, folder) is not None


def _shared_write_block(user, folder):
    """Read-only shared projects / workspaces: block mutating endpoints."""
    if user.get("is_admin"):
        return None
    if _workspace_role(user, folder) == "view":
        return JSONResponse(status_code=403, content={
            "error": "This workspace is read-only for you (view role)"})
    if _folder_shared_role(folder, user["id"]) == "view":
        return JSONResponse(status_code=403, content={
            "error": "This shared project is read-only (view role)"})
    return None


def _folder_access_error(user, folder, write=False):
    """Return a JSONResponse error if the user may not access folder, else None."""
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    if not _can_access_folder(user, folder):
        return JSONResponse(status_code=403, content={"error": "Access denied: folder is outside your data directory"})
    if write:
        return _shared_write_block(user, folder)
    return None

# ---- AI/file actions log (one-click undo) --------------------------------
def _action_log(owner: str, path: str, kind: str, prev_ts, added=0, removed=0) -> None:
    """Record a file mutation so it can be undone later. `owner` is the
    filesystem-safe username (matches the per-file version store)."""
    try:
        conn = get_db()
        try:
            conn.execute("INSERT INTO file_actions (user_id, path, kind, prev_ts,"
                         " added, removed) VALUES (?, ?, ?, ?, ?, ?)",
                         (owner, path[:500], kind, prev_ts, int(added or 0), int(removed or 0)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _action_undo(action_id: int, owner: str):
    """Revert one logged action using the per-file version store."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM file_actions WHERE id = ? AND user_id = ?",
                           (action_id, owner)).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Action not found"})
    if row["undone"]:
        return JSONResponse(status_code=400, content={"error": "Already undone"})
    folder = _get_coding_state(owner).get("folder")
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    full = os.path.join(folder, row["path"])
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    try:
        if row["prev_ts"] is None:
            # The action created a file that did not exist before → undo = delete.
            if os.path.isfile(full):
                os.remove(full)
            result = "deleted"
        else:
            content = _coding_get_version(owner, row["path"], row["prev_ts"], folder)
            if content is None:
                return JSONResponse(status_code=404, content={
                    "error": "The previous version has been pruned and can no longer be restored"})
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            result = "restored"
        conn = get_db()
        try:
            conn.execute("UPDATE file_actions SET undone = 1 WHERE id = ?", (action_id,))
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "path": row["path"], "result": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/coding/actions")
async def coding_actions(request: Request, limit: int = 50):
    user, err = _require_auth(request)
    if err:
        return err
    owner = _safe_username(user["username"])
    limit = max(1, min(200, int(limit or 50)))
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, path, kind, prev_ts, added, removed, undone,"
                            " created_at FROM file_actions WHERE user_id = ?"
                            " ORDER BY id DESC LIMIT ?", (owner, limit)).fetchall()
    finally:
        conn.close()
    return {"actions": [dict(r) for r in rows]}


@app.post("/api/coding/actions/{action_id}/undo")
async def coding_action_undo(request: Request, action_id: int):
    user, err = _require_auth(request)
    if err:
        return err
    owner = _safe_username(user["username"])
    folder = _get_coding_state(owner).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    blk = _shared_write_block(user, folder)
    if blk:
        return blk
    return await asyncio.to_thread(_action_undo, action_id, owner)


@app.delete("/api/coding/actions")
async def coding_actions_clear(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    owner = _safe_username(user["username"])
    conn = get_db()
    try:
        conn.execute("DELETE FROM file_actions WHERE user_id = ?", (owner,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


# ---- shared workspaces (grant another user access to a project folder) ----
def _share_target_user(username: str):
    conn = get_db()
    try:
        return conn.execute("SELECT id, username FROM users WHERE username = ?",
                            (username,)).fetchone()
    finally:
        conn.close()


@app.get("/api/coding/shares")
async def coding_shares(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        owned = conn.execute("SELECT id, folder, shared_with_id, role, created_at"
                             " FROM project_shares WHERE owner_id = ? ORDER BY id DESC",
                             (user["id"],)).fetchall()
        shared = conn.execute(
            "SELECT s.id, s.folder, s.role, s.created_at, u.username AS owner"
            " FROM project_shares s LEFT JOIN users u ON u.id = s.owner_id"
            " WHERE s.shared_with_id = ? ORDER BY s.id DESC", (user["id"],)).fetchall()
    finally:
        conn.close()
    return {"owned": [dict(r) for r in owned], "shared_with_me": [dict(r) for r in shared]}


@app.post("/api/coding/shares")
async def coding_share_create(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    username = str(data.get("username") or "").strip()
    if not username:
        return JSONResponse(status_code=400, content={"error": "username is required"})
    role = str(data.get("role") or "edit").strip().lower()
    if role not in ("view", "edit"):
        return JSONResponse(status_code=400, content={"error": "role must be view or edit"})
    folder = str(data.get("folder") or "").strip()
    if not folder:
        folder = _get_coding_state(_safe_username(user["username"])).get("folder") or ""
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No project folder to share"})
    if not _can_access_folder(user, folder):
        return JSONResponse(status_code=403, content={"error": "You do not control that folder"})
    target = _share_target_user(username)
    if not target:
        return JSONResponse(status_code=404, content={"error": "No such user"})
    if int(target["id"]) == int(user["id"]):
        return JSONResponse(status_code=400, content={"error": "You already own this project"})
    conn = get_db()
    try:
        conn.execute("INSERT INTO project_shares (owner_id, folder, shared_with_id, role)"
                     " VALUES (?, ?, ?, ?) ON CONFLICT(owner_id, folder, shared_with_id)"
                     " DO UPDATE SET role = excluded.role",
                     (user["id"], folder, target["id"], role))
        conn.commit()
        row = conn.execute("SELECT id, folder, shared_with_id, role FROM project_shares"
                           " WHERE owner_id = ? AND folder = ? AND shared_with_id = ?",
                           (user["id"], folder, target["id"])).fetchone()
    finally:
        conn.close()
    return {"status": "ok", "share": dict(row)}


@app.delete("/api/coding/shares/{share_id}")
async def coding_share_delete(request: Request, share_id: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM project_shares WHERE id = ? AND owner_id = ?",
                           (share_id, user["id"]))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if not n:
        return JSONResponse(status_code=404, content={"error": "Share not found"})
    return {"status": "ok"}


@app.post("/api/coding/open-folder")
async def coding_open_folder(request: Request, req: CodingOpenFolder):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    coding_state = _get_coding_state(user_id)
    folder = req.path.strip()
    if _bad_path(folder):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    if not _can_access_folder(user, folder):

        # Containment before existence: no oracle for probing other paths
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only open folders inside your own data directory or shared with you"})
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {folder}"})
    coding_state["folder"] = folder
    _save_coding_folder(user_id, folder)
    tree = _build_file_tree(folder)
    file_count = _count_tree_files(tree)
    meta = {"public_fork": None, "workspace": _workspace_row(folder)}
    marker = os.path.join(folder, ".omniilink", "public-fork.json")
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as fh:
                meta["public_fork"] = json.load(fh)
        except Exception:
            pass
    if meta["public_fork"]:
        fork = _public_fork_for_folder(folder)
        if fork:
            meta["public_fork"]["submitted"] = bool(fork["submitted"])
            meta["public_fork"]["fork_id"] = fork["id"]
    logger.info(f"[CODING] Opened folder: {folder} ({file_count} files) user={user.get('username','-')}")
    return {"status": "ok", "folder": folder, "tree": tree, "file_count": file_count, "meta": meta}

@app.post("/api/coding/close-folder")
async def coding_close_folder(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    coding_state = _get_coding_state(user_id)
    coding_state["folder"] = None
    _save_coding_folder(user_id, None)
    return {"status": "ok"}


# ---- Collaborative workspace endpoints ----------------------------------
class WorkspaceCreate(BaseModel):
    name: str = ""
    folder: str = ""
    members: list = []


@app.post("/api/workspaces")
async def workspace_create(request: Request, req: WorkspaceCreate):
    """Promote a folder the user owns into a collaborative workspace."""
    user, err = _require_auth(request)
    if err:
        return err
    folder = (req.folder or "").strip()
    if _bad_path(folder):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    if not _is_within(_user_data_dir(_safe_username(user["username"])), folder):
        return JSONResponse(status_code=403, content={"error": "You can only turn your own folders into a workspace"})
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "Folder not found"})
    name = (req.name or os.path.basename(folder.rstrip("\\/"))).strip()[:120]
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM workspaces WHERE folder = ?", (folder,)).fetchone()
        if existing:
            ws = existing
        else:
            cur = conn.execute("INSERT INTO workspaces (folder, owner_id, name) VALUES (?, ?, ?)",
                               (folder, user["id"], name))
            ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    try:
        os.makedirs(os.path.join(folder, ".omniilink"), exist_ok=True)
        with open(os.path.join(folder, ".omniilink", "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"workspace_id": ws["id"], "name": name}, f)
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    _ws_append_event(folder, {"type": "workspace_created",
                              "user": user.get("username", ""), "user_id": user["id"]})
    return {"status": "ok", "workspace_id": ws["id"], "folder": folder,
            "name": name, "members": _ws_members(ws["id"])}


@app.get("/api/workspaces")
async def workspace_list(request: Request):
    """Workspaces I own or belong to (for the projects pickers)."""
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT w.*, u.username AS owner_name FROM workspaces w"
            " LEFT JOIN users u ON u.id = w.owner_id"
            " WHERE w.owner_id = ? OR EXISTS (SELECT 1 FROM workspace_members m"
            "       WHERE m.workspace_id = w.id AND m.user_id = ?)"
            " ORDER BY w.created_at DESC", (user["id"], user["id"])).fetchall()
        out = []
        for w in rows:
            try:
                if not os.path.isdir(w["folder"] or ""):
                    continue
                file_count, dir_size = _scan_project_dir(w["folder"])
            except Exception:
                continue
            out.append({"workspace_id": w["id"], "name": w["name"] or "",
                        "folder": w["folder"], "owner_id": w["owner_id"],
                        "owner_name": (w["owner_name"] or ""),
                        "role": "owner" if w["owner_id"] == user["id"] else
                                (_workspace_role(user, w["folder"]) or "view"),
                        "file_count": file_count, "size_bytes": dir_size,
                        "modified": os.path.getmtime(w["folder"]),
                        "member_count": len(_ws_members(w["id"]))})
        return {"workspaces": out}
    finally:
        conn.close()


@app.get("/api/workspaces/{wid}/members")
async def workspace_members_list(request: Request, wid: int):
    """Member roster (usernames) for the workspace-modal UI."""
    user, err = _require_auth(request)
    if err:
        return err
    ws = _workspace_by_id(wid)
    if not ws:
        return JSONResponse(status_code=404, content={"error": "Workspace not found"})
    if not (_workspace_role(user, ws["folder"]) or ws["owner_id"] == user["id"]):
        return JSONResponse(status_code=403, content={"error": "Not a member"})
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT m.user_id, m.role, u.username FROM workspace_members m"
            " JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ?", (wid,)).fetchall()
        owner = conn.execute("SELECT username FROM users WHERE id = ?", (ws["owner_id"],)).fetchone()
    finally:
        conn.close()
    return {"workspace_id": wid, "name": ws.get("name") or "",
            "owner": ws["owner_id"], "owner_name": (owner["username"] if owner else ""),
            "members": [{"user_id": r["user_id"], "username": r["username"] or "",
                         "role": (r["role"] or "edit")} for r in rows]}


@app.post("/api/workspaces/{wid}/share")
async def workspace_share(request: Request, wid: int, role: str = "edit"):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    target = str((data or {}).get("username", "")).strip()
    role = (role or "edit").strip().lower()
    if role not in ("view", "edit"):
        return JSONResponse(status_code=400, content={"error": "role must be view or edit"})
    if not target:
        return JSONResponse(status_code=400, content={"error": "username required"})
    conn = get_db()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if not ws:
            return JSONResponse(status_code=404, content={"error": "Workspace not found"})
        if ws["owner_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Only the owner can share a workspace"})
        target_user = conn.execute("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
                                   (target,)).fetchone()
        if not target_user:
            return JSONResponse(status_code=404, content={"error": "No such user"})
        if target_user["id"] == ws["owner_id"]:
            return JSONResponse(status_code=400, content={"error": "The owner is already a member"})
        members = _ws_members(ws["id"])
        if target_user["id"] in [m[0] for m in members]:
            conn.execute("UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?",
                         (role, wid, target_user["id"]))
        else:
            if len(members) >= _WS_MAX_MEMBERS:
                return JSONResponse(status_code=400, content={
                    "error": f"A workspace can have at most {_WS_MAX_MEMBERS} members besides the owner"})
            conn.execute("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)",
                         (wid, target_user["id"], role))
        conn.commit()
        folder = ws["folder"] or ""
    finally:
        conn.close()
    _ws_append_event(folder, {"type": "member_added", "path": "",
                              "user": user.get("username", ""), "user_id": user["id"],
                              "extra": {"member": target_user["username"], "role": role}})
    return {"status": "ok", "members": _ws_members(wid)}


@app.post("/api/workspaces/{wid}/members/{username}/role")
async def workspace_set_role(request: Request, wid: int, username: str, role: str = "edit"):
    user, err = _require_auth(request)
    if err:
        return err
    role = (role or "edit").strip().lower()
    if role not in ("view", "edit"):
        return JSONResponse(status_code=400, content={"error": "role must be view or edit"})
    conn = get_db()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if not ws:
            return JSONResponse(status_code=404, content={"error": "Workspace not found"})
        if ws["owner_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Only the owner can change roles"})
        tu = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if not tu:
            return JSONResponse(status_code=404, content={"error": "No such user"})
        n = conn.execute("UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?",
                         (role, wid, tu["id"])).rowcount
        conn.commit()
        if not n:
            return JSONResponse(status_code=404, content={"error": "Not a member"})
        folder = ws["folder"] or ""
    finally:
        conn.close()
    _ws_append_event(folder, {"type": "member_role", "path": "",
                              "user": user.get("username", ""), "user_id": user["id"],
                              "extra": {"member": username, "role": role}})
    return {"status": "ok", "members": _ws_members(wid)}


@app.delete("/api/workspaces/{wid}/members/{username}")
async def workspace_remove_member(request: Request, wid: int, username: str):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if not ws:
            return JSONResponse(status_code=404, content={"error": "Workspace not found"})
        if ws["owner_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Only the owner can remove members"})
        tu = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if not tu:
            return JSONResponse(status_code=404, content={"error": "No such user"})
        if tu["id"] == ws["owner_id"]:
            return JSONResponse(status_code=400, content={"error": "The owner cannot be removed"})
        n = conn.execute("DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                         (wid, tu["id"])).rowcount
        conn.commit()
        if not n:
            return JSONResponse(status_code=404, content={"error": "Not a member"})
        folder = ws["folder"] or ""
    finally:
        conn.close()
    _ws_append_event(folder, {"type": "member_removed", "path": "",
                              "user": user.get("username", ""), "user_id": user["id"],
                              "extra": {"member": username}})
    return {"status": "ok", "members": _ws_members(wid)}


@app.delete("/api/workspaces/{wid}")
async def workspace_delete(request: Request, wid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if not ws:
            return JSONResponse(status_code=404, content={"error": "Workspace not found"})
        if ws["owner_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Only the owner can delete the workspace"})
        folder = ws["folder"] or ""
        conn.execute("DELETE FROM workspaces WHERE id = ?", (wid,))
        conn.commit()
    finally:
        conn.close()
    try:
        marker = os.path.join(folder, ".omniilink", "workspace.json")
        if os.path.isfile(marker):
            os.remove(marker)
    except OSError:
        pass
    _ws_append_event(folder, {"type": "workspace_deleted",
                              "user": user.get("username", ""), "user_id": user["id"]})
    _ws_pubsub.pop(os.path.abspath(folder), None)
    return {"status": "ok"}


@app.get("/api/coding/workspace/poll")
async def workspace_poll(request: Request, since: float = 0.0):
    """Live feed for the open workspace: recent change events, active file
    locks and the current file tree (mtimes compared by the client)."""
    user, err = _require_auth(request)
    if err:
        return err
    folder = _get_coding_state(_safe_username(user["username"])).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    ws = _workspace_row(folder)
    if not ws:
        return {"active": False, "events": [], "locks": [], "tree": None, "since": since}
    try:
        since = float(since or 0)
    except Exception:
        since = 0.0
    events = [e for e in _ws_read_events(folder) if float(e.get("at") or 0) > since]
    tree = _build_file_tree(folder, patterns=_read_omniiignore(folder))
    return {"active": True, "events": events, "locks": _ws_live_locks(folder),
            "tree": tree, "since": since,
            "token": _ws_pubsub.get(os.path.abspath(folder), 0)}


@app.get("/api/coding/workspace/context")
async def workspace_context(request: Request):
    """Recent workspace activity, injected into the AI's coding prompt so the
    second AI knows what collaborators (or an earlier AI session) just did."""
    user, err = _require_auth(request)
    if err:
        return err
    folder = _get_coding_state(_safe_username(user["username"])).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    ws = _workspace_row(folder)
    if not ws:
        return {"active": False, "summary": "", "events": []}
    events = _ws_read_events(folder)[-_WS_EVENT_CAP:]
    events = [e for e in events if e.get("type") not in ("lock",)]
    lines = []
    for e in events[-40:]:
        t = e.get("type")
        who = e.get("user") or "someone"
        rel = e.get("path") or ""
        if t == "write":
            lines.append(f"{who} wrote {rel}")
        elif t == "patch":
            lines.append(f"{who} edited {rel}")
        elif t == "create":
            lines.append(f"{who} created {rel}")
        elif t == "delete":
            lines.append(f"{who} deleted {rel}")
        elif t == "rename":
            lines.append(f"{who} renamed {rel} to {e.get('to') or '?'}")
        elif t == "upload":
            lines.append(f"{who} uploaded {rel}")
        elif t == "member_added":
            lines.append(f"{who} added {e.get('extra', {}).get('member')} as {e.get('extra', {}).get('role')}")
        elif t == "member_removed":
            lines.append(f"{who} removed {e.get('extra', {}).get('member')}")
        elif t == "workspace_created":
            lines.append(f"{who} created this workspace")
    return {"active": True, "summary": "\n".join(lines[-20:]), "events": events,
            "owner": ws.get("owner_id"), "name": ws.get("name") or ""}


class WsLockBody(BaseModel):
    path: str = ""
    unlock: bool = False


@app.post("/api/coding/workspace/lock")
async def workspace_lock(request: Request, req: WsLockBody):
    """Explicitly claim the lock for a file the caller is about to touch."""
    user, err = _require_auth(request)
    if err:
        return err
    folder = _get_coding_state(_safe_username(user["username"])).get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    if not _workspace_row(folder):
        return JSONResponse(status_code=400, content={"error": "Not a workspace"})
    if req.unlock:
        _ws_release_lock(user, folder, req.path)
        return {"status": "ok", "locked": False, "path": req.path}
    errd = _ws_take_lock(user, folder, req.path)
    if errd:
        return JSONResponse(status_code=409, content=errd)
    return {"status": "ok", "locked": True, "path": req.path}


@app.post("/api/coding/workspace/unlock")
async def workspace_unlock(request: Request, req: WsLockBody):
    """Release the file lock the caller holds (called when the AI finishes)."""
    user, err = _require_auth(request)
    if err:
        return err
    folder = _get_coding_state(_safe_username(user["username"])).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    _ws_release_lock(user, folder, req.path)
    return {"status": "ok"}


@app.get("/api/coding/files")
async def coding_list_files(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    tree = _build_file_tree(folder, patterns=_read_omniiignore(folder))
    return {"folder": folder, "tree": tree}


@app.get("/api/coding/chat")
async def coding_get_chat(request: Request):
    """Coding conversation for the open project, stored INSIDE the project
    folder (.omniilink/coding-chat.json) so it follows the project across
    devices. Dotfiles are hidden from the file tree and auto-attach. In a
    collaborative workspace each member gets their own private chat file."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    if _folder_access_error(user, folder) or not folder or not os.path.isdir(folder):
        return {"history": [], "stored": False}
    path = os.path.join(folder, CODING_CHAT_DIR, CODING_CHAT_FILE)
    if _workspace_row(folder):
        path = _ws_chat_file(folder, user["id"])
    if not _is_within(folder, path):
        return {"history": [], "stored": False}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            hist = d.get("history") if isinstance(d, dict) else None
            if isinstance(hist, list):
                return {"history": hist, "stored": True}
    except Exception:
        pass
    return {"history": [], "stored": False}

class CodingChatSave(BaseModel):
    history: list = []

@app.post("/api/coding/chat")
async def coding_save_chat(request: Request, req: CodingChatSave):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    if not isinstance(req.history, list) or len(req.history) > 500:
        return JSONResponse(status_code=400, content={"error": "Invalid history"})
    raw = json.dumps({"updated": time.time(), "history": req.history})
    if len(raw) > _CHAT_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "Chat too large"})
    path = os.path.join(folder, CODING_CHAT_DIR, CODING_CHAT_FILE)
    if _workspace_row(folder):
        path = _ws_chat_file(folder, user["id"])
    if not _is_within(folder, path):
        return JSONResponse(status_code=403, content={"error": "Path outside project"})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, path)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"status": "ok"}


# ---- Chat image attachments: persisted per user, referenced by chats ----
_CHAT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_CHAT_IMAGE_MIME = {"image/png": "png", "image/jpeg": "jpg",
                    "image/webp": "webp", "image/gif": "gif"}


def _chat_images_dir(user_id: str) -> str:
    return _ensure_dir(os.path.join(_user_data_dir(user_id), "chat_images"))


def _chat_image_id_ok(iid: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{13}-[0-9a-f]{8}", iid or ""))


def _chat_image_find(user_id: str, iid: str):
    if not _chat_image_id_ok(iid):
        return None
    d = _chat_images_dir(user_id)
    try:
        for name in os.listdir(d):
            if name.startswith(iid + "."):
                full = os.path.join(d, name)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


@app.post("/api/chat-images")
async def chat_image_upload(request: Request):
    """Persist one chat-attached image. Chat bubbles reference the returned
    URL (tiny) instead of a data: URL (huge), so history survives reloads
    and localStorage quotas; the entry lives until deleted in Assets."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    durl = str((data or {}).get("data_url", "") or "")
    m = re.match(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$", durl)
    if not m:
        return JSONResponse(status_code=400, content={"error": "Expected a PNG/JPEG/WebP/GIF data URL"})
    import base64 as _b64
    try:
        raw = _b64.b64decode(m.group(2))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Bad image data"})
    if not raw or len(raw) > _CHAT_IMAGE_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "Image must be under 5MB"})
    uid = _safe_username(user["username"])
    qerr = _check_quota(user, uid, len(raw))
    if qerr:
        return qerr
    iid = f"{int(time.time() * 1000):013d}-{secrets.token_hex(4)}"
    name = f"{iid}.{_CHAT_IMAGE_MIME[m.group(1).lower()]}"
    try:
        with open(os.path.join(_chat_images_dir(uid), name), "wb") as f:
            f.write(raw)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    label = str((data or {}).get("name") or "image").strip()[:80] or "image"
    return {"status": "ok", "id": iid, "name": label, "size": len(raw),
            "url": f"/api/chat-images/{iid}"}


@app.get("/api/chat-images")
async def chat_images_list(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    d = _chat_images_dir(_safe_username(user["username"]))
    out = []
    try:
        for name in sorted(os.listdir(d), reverse=True):
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            iid = name.rsplit(".", 1)[0]
            if not _chat_image_id_ok(iid):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"id": iid, "name": name, "size": st.st_size,
                        "created": int(st.st_mtime * 1000),
                        "url": f"/api/chat-images/{iid}"})
    except OSError:
        pass
    return {"images": out}


@app.get("/api/chat-images/{image_id}")
async def chat_image_get(request: Request, image_id: str):
    from fastapi.responses import Response as _Response
    import mimetypes
    user, err = _require_auth(request)
    if err:
        return err
    full = _chat_image_find(_safe_username(user["username"]), image_id)
    if not full:
        return JSONResponse(status_code=404, content={"error": "Image not found"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        media = mimetypes.guess_type(full)[0] or "application/octet-stream"
        return _Response(content=content, media_type=media,
                         headers={"Cache-Control": "private, max-age=86400"})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/chat-images/{image_id}")
async def chat_image_delete(request: Request, image_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    full = _chat_image_find(_safe_username(user["username"]), image_id)
    if not full:
        return JSONResponse(status_code=404, content={"error": "Image not found"})
    try:
        os.remove(full)
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"status": "ok"}


@app.get("/api/coding/omniiignore")
async def coding_get_omniiignore(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    return {"patterns": _read_omniiignore(folder)}


@app.post("/api/coding/omniiignore")
async def coding_set_omniiignore(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    patterns = body.get("patterns", [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return JSONResponse(status_code=400, content={"error": "patterns must be a list of strings"})
    path = os.path.join(folder, OMNIIIGNORE_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(p.strip() for p in patterns if p.strip()) + "\n")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"status": "ok", "patterns": _read_omniiignore(folder)}

@app.get("/api/coding/file-content")
async def coding_file_content(request: Request, path: str):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    full = os.path.join(folder, path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    max_size = coding_state["settings"].get("max_file_size", 100000)
    if os.path.getsize(full) > max_size:
        return JSONResponse(status_code=400, content={"error": f"File too large ({os.path.getsize(full)} bytes)"})
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "size": len(content)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------

# Project archive download (the whole open project, compressed)

# ---------------------------------------------------------------------------
_ARCHIVE_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".next", ".turbo", ".cache", ".pytest_cache", ".mypy_cache",
    ".eggs", ".gradle", "bower_components",
}
_ARCHIVE_SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
_ARCHIVE_SKIP_EXT = {".pyc", ".pyo", ".class"}
_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
_ARCHIVE_MAX_FILES = 200000


def _collect_project_files(folder):
    """Walk folder for archiving: [(abs, rel)] with no symlinks and no junk.

    Returns (files, total_bytes, error_message). error_message is None on
    success. Runs in a worker thread (blocking walk + stat)."""
    files, total = [], 0
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if d not in _ARCHIVE_SKIP_DIRS
            and not os.path.islink(os.path.join(root, d))
        )
        for name in sorted(names):
            if name in _ARCHIVE_SKIP_FILES:
                continue
            if os.path.splitext(name)[1].lower() in _ARCHIVE_SKIP_EXT:
                continue
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            files.append((full, os.path.relpath(full, folder).replace(os.sep, "/")))
            total += size
            if total > _ARCHIVE_MAX_BYTES:
                return None, 0, "Project is too large to archive (over 512 MB)"
            if len(files) > _ARCHIVE_MAX_FILES:
                return None, 0, "Too many files in project (over 200,000)"
    return files, total, None


def _write_project_archive(kind, files, path):
    """Fill the temp file at path with a .zip or .tar.gz of files."""
    if kind == "zip":
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for full, rel in files:
                zf.write(full, rel)
    else:
        with tarfile.open(path, "w:gz") as tf:
            for full, rel in files:
                tf.add(full, arcname=rel, recursive=False)


def _remove_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


@app.get("/api/coding/download-project")
async def coding_download_project(request: Request, format: str = "zip"):
    """Compress the whole open project folder and hand it to the browser."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    fmt = (format or "zip").strip().lower()
    if fmt in ("zip",):
        kind, suffix, media = "zip", ".zip", "application/zip"
    elif fmt in ("targz", "tar.gz", "tgz"):
        kind, suffix, media = "targz", ".tar.gz", "application/gzip"
    else:
        return JSONResponse(status_code=400,
                            content={"error": "Unsupported format (use zip or targz)"})
    files, _total, err_msg = await asyncio.to_thread(_collect_project_files, folder)
    if err_msg:
        return JSONResponse(status_code=400, content={"error": err_msg})
    if not files:
        return JSONResponse(status_code=400, content={"error": "Project folder is empty"})
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    try:
        await asyncio.to_thread(_write_project_archive, kind, files, tmp.name)
    except Exception as e:
        _remove_quiet(tmp.name)
        logger.warning(f"[ARCHIVE] Failed for {folder}: {e}")
        return JSONResponse(status_code=500, content={"error": f"Archive failed: {e}"})
    base = re.sub(r"[^A-Za-z0-9._-]+", "_",
                  os.path.basename(os.path.normpath(folder))) or "project"
    filename = f"{base}-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
    return FileResponse(tmp.name, media_type=media, filename=filename,
                        background=BackgroundTask(_remove_quiet, tmp.name))

@app.get("/api/coding/preview")
async def coding_preview(request: Request, path: str):
    """Serve a project HTML file for preview.

    Two modes, same file:
    - iframe (?tab absent): opaque origin (CSP sandbox + iframe sandbox
      attr, no allow-same-origin), so scripts inside cannot touch app
      cookies, localStorage, or the parent page.
    - real tab (?tab=1): NO sandbox directive, so the page runs EXACTLY
      like the same file opened from disk — pointer lock, keyboard,
      fullscreen all work. Only the user's own project files, gated by
      session or a short-lived HMAC token bound to one user + one path.
    Only .html/.htm, size-capped. Scan via /api/coding/scan before
    previewing untrusted content.
    """
    from fastapi.responses import Response as _Response
    user, err = _require_auth(request)
    if err:

        # Same cookie-free path as assets: browsers may withhold Lax cookies

        # from sandboxed-frame navigations, so the frontend appends `t`.
        user = _user_from_preview_token(
            request.query_params.get("t"), request.query_params.get("path"))
        if user is None:
            return err
        request.state.user = user
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err

    # Opening a preview brings the project's (idle-shut-down) container back up.
    _maybe_warm_project(user, folder)
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    if os.path.splitext(path)[1].lower() not in (".html", ".htm"):
        return JSONResponse(status_code=400, content={"error": "Preview supports HTML files only"})
    full = os.path.join(folder, path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    if os.path.getsize(full) > 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "File too large to preview (max 1MB)"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        from urllib.parse import quote as _quote
        _tab = request.query_params.get("tab") == "1"
        def _page_url(rel):
            url = "/api/coding/preview?path=" + _quote(rel, safe="/")
            url += "&t=" + _mint_preview_token(user["id"], rel)
            if _tab:
                url += "&tab=1"
            return url
        content = _rewrite_preview_assets(content, path, user["id"],
                                          page_url_for=_page_url)
        return _Response(
            content=content,
            media_type="text/html",
            headers={

                # Navigation token: lets the frame/tab load this document even

                # when the browser withholds cookies (sandboxed frame) or the

                # tab was opened without a session context.
                "X-Preview-Token": _mint_preview_token(user["id"], path),

                # Real-tab mode drops the sandbox entirely (see docstring);

                # the iframe keeps "sandbox allow-scripts".
                **({} if request.query_params.get("tab") == "1"
                   else {"Content-Security-Policy": "sandbox allow-scripts"}),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

_ASSET_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}


# ==========================================

# Preview asset tokens

# ==========================================

# The preview page runs in a sandboxed, opaque-origin iframe (CSP sandbox +

# iframe sandbox attr, deliberately without allow-same-origin). Browsers treat

# requests from such documents as cross-site/third-party and withhold the

# SameSite=Lax session cookie, so CSS/JS/img subrequests get 401 and the page

# renders unstyled. Rather than weaken the sandbox or the cookie policy,

# rewritten asset URLs carry a short-lived HMAC token bound to one user +

# one exact path; the asset endpoint accepts either a session or a token.
_PREVIEW_TOKEN_TTL = 3600
_preview_secret_cache = None
_preview_secret_lock = threading.Lock()


def _preview_secret() -> bytes:
    """Persistent per-install HMAC key for preview tokens.

    Stored under user_data (a mounted Docker volume) so tokens survive
    restarts; falls back to a per-process key if the file is unusable."""
    global _preview_secret_cache
    if _preview_secret_cache:
        return _preview_secret_cache
    with _preview_secret_lock:
        if _preview_secret_cache:
            return _preview_secret_cache
        path = os.path.join(APP_DIR, "user_data", ".preview_secret")
        data = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    existing = f.read().strip()
                if len(existing) >= 32:
                    data = existing
            if data is None:
                data = secrets.token_bytes(32)
                with open(path, "wb") as f:
                    f.write(data)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except Exception:
            data = secrets.token_bytes(32)
        _preview_secret_cache = data
        return data


def _mint_preview_token(user_id, path: str, ttl: int = _PREVIEW_TOKEN_TTL) -> str:
    """Mint `exp.user_id.nonce.sig` for one asset path (read-only, 1h default).

    The nonce makes every mint unique, so each preview click produces a
    fresh URL that cannot be correlated with (or replayed from) an older one.
    """
    exp = int(time.time()) + int(ttl)
    nonce = secrets.token_hex(8)
    sig = hmac.new(
        _preview_secret(),
        f"{user_id}:{path}:{exp}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{exp}.{user_id}.{nonce}.{sig}"


def _user_from_preview_token(token, path):
    """Validate a preview token for `path`; return the user dict or None.

    Binds user id + exact path + expiry; signature compared in constant time.
    Tampering with any field (including swapping paths) invalidates it."""
    if not token or not path or len(token) > 192:
        return None
    parts = token.split(".")
    if len(parts) == 4:
        exp_s, uid_s, nonce, sig = parts
        msg_tail = f":{nonce}"
    elif len(parts) == 3:

        # Legacy pre-nonce tokens: still honored until they expire.
        exp_s, uid_s, sig = parts
        msg_tail = ""
    else:
        return None
    try:
        exp = int(exp_s)
        uid = int(uid_s)
    except ValueError:
        return None
    now = int(time.time())
    if exp < now or exp > now + 86400:
        return None
    expected = hmac.new(
        _preview_secret(),
        f"{uid}:{path}:{exp}{msg_tail}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8")):
        return None
    try:
        return get_user_by_id(uid)
    except Exception:
        return None


def _preview_asset_url(rel_path: str, user_id=None) -> str:
    from urllib.parse import quote
    url = "/api/coding/preview-asset?path=" + quote(rel_path, safe="/")
    if user_id is not None:
        url += "&t=" + _mint_preview_token(user_id, rel_path)
    return url


_QUOTED_ASSET_RE = re.compile(
    r"(<(?:link|script|img|source|video|audio|track|embed|object|input|iframe)"
    r"\b[^>]*?\b(?:href|src|poster)\s*=\s*)([\"'])(.*?)\2",
    re.IGNORECASE | re.DOTALL)
_UNQUOTED_ASSET_RE = re.compile(
    r"(<(?:link|script|img|source|video|audio|track|embed|object|input|iframe)"
    r"\b[^>]*?\b(?:href|src|poster)\s*=\s*)([^\s\"'>/=]+)",
    re.IGNORECASE)
_SRCSET_RE = re.compile(
    r"(<(?:img|source)\b[^>]*?\bsrcset\s*=\s*)([\"'])(.*?)\2",
    re.IGNORECASE | re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)",
                             re.IGNORECASE | re.DOTALL)
_PAGE_LINK_RE = re.compile(
    r"(<a\b[^>]*?\bhref\s*=\s*)([\"'])(.*?)\2",
    re.IGNORECASE | re.DOTALL)


def _rewrite_preview_assets(content: bytes, page_path: str, user_id=None, url_for=None, page_url_for=None, base_root="") -> bytes:
    """Rewrite relative asset URLs in a previewed HTML page to absolute
    preview-asset URLs (with a signed token), so linked CSS/JS/images actually
    load inside the opaque-origin iframe. Covers quoted, unquoted and srcset
    attributes plus inline <style> url()/@import. External, data:, fragment
    and javascript: URLs are left untouched.

    Pass url_for(rel_path)->url to route assets elsewhere (e.g. shared
    preview links, which stay valid as long as the share does)."""
    try:
        text = content.decode("utf-8")
    except Exception:
        return content
    import posixpath
    page_dir = posixpath.dirname(page_path.replace("\\", "/"))

    def resolve(ref: str):
        ref = (ref or "").strip()
        if not ref:
            return None
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:)", ref, re.IGNORECASE):
            return None

        # Cache-busting queries/fragments are meaningless to a filesystem

        # lookup (img.jpg?v=2 would 404); responses are no-store anyway.
        ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
        if not ref:
            return None

        # normpath clamps ".." at the project root; the asset endpoint

        # re-enforces containment, so anything truly outside 403s there.

        # A root-absolute ref (/style.css) means the SITE root, which for a

        # project page is the project folder (base_root), not the server root.
        if ref.startswith("/"):
            norm = posixpath.normpath(posixpath.join("/" + (base_root or ""), ref.lstrip("/"))).lstrip("/")
        else:
            norm = posixpath.normpath(posixpath.join("/" + page_dir, ref)).lstrip("/")
        if not norm or norm == ".":
            return None
        if url_for is not None:
            return url_for(norm)
        return _preview_asset_url(norm, user_id)

    def repl_quoted(m):
        target = resolve(m.group(3))
        if target is None:
            return m.group(0)
        return m.group(1) + m.group(2) + target + m.group(2)

    def repl_unquoted(m):
        target = resolve(m.group(2))
        if target is None:
            return m.group(0)
        return m.group(1) + target

    def repl_srcset(m):
        val = m.group(3)
        if "data:" in val:
            return m.group(0)
        out = []
        for chunk in val.split(","):
            s = " ".join(chunk.split())
            if not s:
                continue
            bits = s.split(None, 1)
            url = resolve(bits[0]) or bits[0]
            out.append(url + ((" " + bits[1]) if len(bits) > 1 else ""))
        if not out:
            return m.group(0)
        return m.group(1) + m.group(2) + ", ".join(out) + m.group(2)

    def repl_style(m):
        return (m.group(1)
                + _rewrite_css_urls(m.group(2), page_dir, user_id, url_for, base_root)
                + m.group(3))

    def repl_page(m):

        # In-page navigation: <a href="other.html"> keeps working inside

        # previews by pointing at the preview endpoint (own signed token).

        # Anything else (external, fragments, non-HTML) is left untouched.
        if page_url_for is None:
            return m.group(0)
        ref = (m.group(3) or "").strip()
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:|javascript:)", ref, re.IGNORECASE):
            return m.group(0)
        clean = ref.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean:
            return m.group(0)
        if os.path.splitext(clean)[1].lower() not in (".html", ".htm"):
            return m.group(0)
        if clean.startswith("/"):
            norm = posixpath.normpath(posixpath.join("/" + (base_root or ""), clean.lstrip("/"))).lstrip("/")
        else:
            norm = posixpath.normpath(posixpath.join("/" + page_dir, clean)).lstrip("/")
        if not norm or norm == ".":
            return m.group(0)
        return m.group(1) + m.group(2) + page_url_for(norm) + m.group(2)

    text = _QUOTED_ASSET_RE.sub(repl_quoted, text)
    text = _UNQUOTED_ASSET_RE.sub(repl_unquoted, text)
    text = _SRCSET_RE.sub(repl_srcset, text)
    text = _STYLE_BLOCK_RE.sub(repl_style, text)
    text = _PAGE_LINK_RE.sub(repl_page, text)
    return text.encode("utf-8")


def _rewrite_css_urls(css_text: str, css_dir: str, user_id=None, url_for=None, base_root="") -> str:
    """Rewrite relative url(...) and @import "..." in a served stylesheet to
    preview-asset URLs so fonts/backgrounds resolve (external/data untouched)."""
    import posixpath

    def resolve(ref: str):
        ref = (ref or "").strip()
        if not ref:
            return None
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:)", ref, re.IGNORECASE):
            return None
        ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
        if not ref:
            return None
        if ref.startswith("/"):
            norm = posixpath.normpath(posixpath.join("/" + (base_root or ""), ref.lstrip("/"))).lstrip("/")
        else:
            norm = posixpath.normpath(posixpath.join("/" + css_dir, ref)).lstrip("/")
        if not norm or norm == ".":
            return None
        if url_for is not None:
            return url_for(norm)
        return _preview_asset_url(norm, user_id)

    def repl_url(m):
        target = resolve(m.group(2))
        if target is None:
            return m.group(0)
        return "url(" + m.group(1) + target + m.group(1) + ")"

    def repl_import(m):
        target = resolve(m.group(2))
        if target is None:
            return m.group(0)
        return "@import " + m.group(1) + target + m.group(1)

    text = re.sub(r"url\(\s*([\"']?)([^)\"']+)\1\s*\)", repl_url, css_text,
                  flags=re.IGNORECASE)
    text = re.sub(r"@import\s+([\"'])([^\"']+)\1", repl_import, text,
                  flags=re.IGNORECASE)
    return text


@app.get("/api/coding/preview-asset")
async def coding_preview_asset(request: Request, path: str):
    """Serve a project file referenced by a previewed page (CSS/JS/images).
    Auth via session cookie/header OR a signed `t` token: the sandboxed
    preview iframe has an opaque origin, so browsers withhold SameSite=Lax
    cookies from its subrequests. Same containment; correct MIME types
    (required under nosniff); size-capped."""
    from fastapi.responses import Response as _Response
    import mimetypes
    user, err = _require_auth(request)
    if err:

        # Cookie-free path: short-lived token minted during HTML rewriting.
        user = _user_from_preview_token(
            request.query_params.get("t"), request.query_params.get("path"))
        if user is None:
            return err
        request.state.user = user
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    full = os.path.join(folder, path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    if os.path.getsize(full) > 5 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "File too large (max 5MB)"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        ext = os.path.splitext(path)[1].lower()
        media_type = _ASSET_MIME_OVERRIDES.get(ext)
        if media_type is None:
            media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ext == ".css":
            import posixpath
            css_dir = posixpath.dirname(path.replace("\\", "/"))
            try:
                content = _rewrite_css_urls(content.decode("utf-8"), css_dir,
                                            user.get("id")).encode("utf-8")
            except Exception:
                pass
        return _Response(
            content=content,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ==========================================

# Shared previews: copyable links that stay live forever until 10 minutes

# after the first open. Token-gated (unguessable), path- and user-bound at

# creation; assets ride the same share so they die with it.

# ==========================================
_SHARE_TTL_AFTER_OPEN = 600
_SHARE_MAX_BYTES = 1024 * 1024


def _share_lookup(token):
    if not token or len(token) > 128:
        return None
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM shared_previews WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()


def _share_consume(token):
    """Mark a share opened and return its row, or None when dead/expired."""
    row = _share_lookup(token)
    if row is None:
        return None
    now = time.time()
    first = row["first_opened_at"]
    conn = get_db()
    try:
        if first is None:
            conn.execute("UPDATE shared_previews SET first_opened_at = ?, open_count = open_count + 1"
                         " WHERE id = ?", (now, row["id"]))
            conn.commit()
            return row
        try:
            age = now - float(first)
        except (TypeError, ValueError):
            return None
        if age > _SHARE_TTL_AFTER_OPEN:
            return None
        conn.execute("UPDATE shared_previews SET open_count = open_count + 1 WHERE id = ?",
                     (row["id"],))
        conn.commit()
        return row
    finally:
        conn.close()


def _share_alive(token):
    """Like _share_consume but read-only (asset subrequests must not reset
    the clock or inflate the counter)."""
    row = _share_lookup(token)
    if row is None:
        return None
    first = row["first_opened_at"]
    if first is None:
        return row
    try:
        if time.time() - float(first) > _SHARE_TTL_AFTER_OPEN:
            return None
    except (TypeError, ValueError):
        return None
    return row


def _share_asset_url(token, rel_path):
    from urllib.parse import quote
    return "/api/s/" + token + "/asset?path=" + quote(rel_path, safe="/")


def _share_gone(expired):
    from fastapi.responses import Response as _Response
    msg = ("This shared preview has expired (links stay live until 10 minutes "
           "after the first open)." if expired else "This shared preview link is invalid.")
    return _Response(
        content=("<!doctype html><meta charset='utf-8'><title>Preview unavailable</title>"
                 "<body style='font-family:system-ui;background:#0b0d10;color:#e6e9ef;"
                 "display:flex;align-items:center;justify-content:center;min-height:90vh'>"
                 "<p>" + msg + "</p>"),
        media_type="text/html", status_code=410 if expired else 404)


@app.post("/api/coding/preview-share")
async def coding_preview_share(request: Request):
    """Create a shareable preview link. Server files are served live from the
    project; browser-local files can be shared by uploading their content."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    folder, path, content = "", "", None
    inline = data.get("content")
    if inline is not None:
        if not isinstance(inline, str) or len(inline) > _SHARE_MAX_BYTES:
            return JSONResponse(status_code=400, content={"error": "Shared content too large (max 1MB)"})
        name = str(data.get("name", "") or "shared.html")
        name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name.split("/")[-1].split("\\")[-1])[:128] or "shared.html"
        if os.path.splitext(name)[1].lower() not in (".html", ".htm"):
            name += ".html"
        path, content = name, inline
    else:
        path = str(data.get("path", "") or "").strip()
        if not path:
            return JSONResponse(status_code=400, content={"error": "Missing path"})
        coding_state = _get_coding_state(_safe_username(user["username"]))
        folder = coding_state.get("folder")
        access_err = _folder_access_error(user, folder)
        if access_err:
            return access_err
        if _bad_path(path):
            return JSONResponse(status_code=400, content={"error": "Invalid path"})
        if os.path.splitext(path)[1].lower() not in (".html", ".htm"):
            return JSONResponse(status_code=400, content={"error": "Sharing supports HTML files only"})
        full = os.path.join(folder, path)
        if not _is_within(folder, full) or not os.path.isfile(full):
            return JSONResponse(status_code=404, content={"error": "File not found"})
        if os.path.getsize(full) > _SHARE_MAX_BYTES:
            return JSONResponse(status_code=400, content={"error": "File too large to share (max 1MB)"})
    token = secrets.token_urlsafe(24)
    conn = get_db()
    try:
        conn.execute("INSERT INTO shared_previews (token, user_id, username, folder, path, content)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (token, user["id"], user.get("username", ""), folder, path, content))
        conn.commit()
    finally:
        conn.close()
    return {"url": "/api/s/" + token, "token": token}


@app.get("/api/s/{token}")
async def shared_preview(request: Request, token: str):
    """Serve a shared page: sandbox allow-scripts + allow-pointer-lock (opaque
    origin, so it cannot touch viewer storage — but games get full input)."""
    from fastapi.responses import Response as _Response
    row = _share_consume(token)
    if row is None:
        return _share_gone(_share_lookup(token) is not None)
    if row["content"] is not None:
        content = row["content"].encode("utf-8")
        page_path = row["path"] or "shared.html"
    else:
        full = os.path.join(row["folder"], row["path"])
        if not row["folder"] or not _is_within(row["folder"], full) or not os.path.isfile(full):
            return _share_gone(False)
        try:
            with open(full, "rb") as f:
                content = f.read()
        except OSError:
            return _share_gone(False)
        if len(content) > _SHARE_MAX_BYTES:
            return _share_gone(False)
        page_path = row["path"]
    url_for = lambda rel: _share_asset_url(token, rel)  # noqa: E731
    content = _rewrite_preview_assets(content, page_path, url_for=url_for)
    return _Response(
        content=content,
        media_type="text/html",
        headers={
            "Content-Security-Policy": "sandbox allow-scripts allow-pointer-lock",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/s/{token}/asset")
async def shared_preview_asset(request: Request, token: str, path: str):
    """Serve a file referenced by a shared page. Read-only liveness check:
    never resets the expiry clock."""
    from fastapi.responses import Response as _Response
    import mimetypes
    row = _share_alive(token)
    if row is None:
        return _share_gone(_share_lookup(token) is not None)
    if row["content"] is not None or not row["folder"]:
        return JSONResponse(status_code=404, content={"error": "No bundled assets"})
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    full = os.path.join(row["folder"], path)
    if not _is_within(row["folder"], full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    if os.path.getsize(full) > 5 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "File too large (max 5MB)"})
    try:
        with open(full, "rb") as f:
            content = f.read()
        ext = os.path.splitext(path)[1].lower()
        media_type = _ASSET_MIME_OVERRIDES.get(ext)
        if media_type is None:
            media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ext == ".css":
            import posixpath
            css_dir = posixpath.dirname(path.replace("\\", "/"))
            url_for = lambda rel: _share_asset_url(token, rel)  # noqa: E731
            try:
                content = _rewrite_css_urls(content.decode("utf-8"), css_dir,
                                            url_for=url_for).encode("utf-8")
            except Exception:
                pass
        return _Response(
            content=content,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ==========================================

# Remote preview: serve a project file from a paired computer. The server

# reads it through the agent (read/readb64), rewrites its relative asset and

# page links to tokenised remote endpoints, and serves it exactly like a

# server-side preview (real tab or sandboxed frame).

# ==========================================
async def _agent_run_file_op(user, agent_id, payload, timeout=25.0):
    """Queue a file op and wait for the laptop's reply. Returns (parsed, None)
    or (None, JSONResponse error)."""
    cid, err = _queue_agent_command(user, agent_id, "", "file", payload)
    if err:
        return None, err
    try:
        _agent_notify(agent_id)
    except Exception:
        pass
    cid = str(cid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.15)
        with _AGENT_LOCK:
            st = _agents_load()
            c = st["commands"].get(cid)
        if c and c.get("status") == "done":
            try:
                return json.loads(c.get("output") or "null"), None
            except Exception:
                return None, JSONResponse(status_code=500, content={"error": "Bad reply from computer"})
        if c is None:
            return None, JSONResponse(status_code=404, content={"error": "Command vanished"})
    return None, JSONResponse(status_code=504, content={"error": "Timed out waiting for the computer"})


def _remote_tokkey(agent, rel):
    return f"r{agent}:{rel}"


@app.get("/api/coding/remote-preview")
async def coding_remote_preview(request: Request, agent: str = "", path: str = ""):
    from fastapi.responses import Response as _Response
    from urllib.parse import quote as _quote
    tab = request.query_params.get("tab") == "1"
    tokkey = _remote_tokkey(agent, path)
    user, err = _require_auth(request)
    if err:
        user = _user_from_preview_token(request.query_params.get("t"), tokkey)
        if user is None:
            return err
        request.state.user = user
    if not agent:
        return JSONResponse(status_code=400, content={"error": "Missing computer"})
    if _bad_path(path) or os.path.splitext(path)[1].lower() not in (".html", ".htm"):
        return JSONResponse(status_code=400, content={"error": "Preview supports HTML files only"})
    out, err2 = await _agent_run_file_op(user, agent, {"op": "read", "path": path})
    if err2:
        return err2
    if not out or not out.get("ok"):
        return JSONResponse(status_code=400, content={"error": (out or {}).get("error", "Could not read file")})
    content = str(out.get("content") or "").encode("utf-8")
    uid = user["id"]
    proj = (request.query_params.get("proj", "") or "").replace("\\", "/").strip("/")

    def url_for(rel):
        return ("/api/coding/remote-asset?agent=" + _quote(agent, safe="") +
                "&path=" + _quote(rel, safe="/") +
                (("&proj=" + _quote(proj, safe="/")) if proj else "") +
                "&t=" + _mint_preview_token(uid, _remote_tokkey(agent, rel)))

    def page_url_for(rel):
        u = ("/api/coding/remote-preview?agent=" + _quote(agent, safe="") +
             "&path=" + _quote(rel, safe="/") +
             (("&proj=" + _quote(proj, safe="/")) if proj else "") +
             "&t=" + _mint_preview_token(uid, _remote_tokkey(agent, rel)))
        return u + ("&tab=1" if tab else "")

    content = _rewrite_preview_assets(content, path, user_id=uid, url_for=url_for,
                                      page_url_for=page_url_for, base_root=proj)
    return _Response(
        content=content,
        media_type="text/html",
        headers={
            "X-Preview-Token": _mint_preview_token(uid, tokkey),
            **({} if tab else {"Content-Security-Policy": "sandbox allow-scripts"}),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/coding/remote-asset")
async def coding_remote_asset(request: Request, agent: str = "", path: str = ""):
    from fastapi.responses import Response as _Response
    from urllib.parse import quote as _quote
    import base64 as _b64
    import mimetypes
    tokkey = _remote_tokkey(agent, path)
    user, err = _require_auth(request)
    if err:
        user = _user_from_preview_token(request.query_params.get("t"), tokkey)
        if user is None:
            return err
    if not agent or _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid request"})
    out, err2 = await _agent_run_file_op(user, agent, {"op": "readb64", "path": path})
    data = None
    if not err2 and out and out.get("ok"):
        try:
            data = _b64.b64decode(out.get("content") or "")
        except Exception:
            data = None
    if data is None:

        # Older companion without readb64: CSS/JS/etc are text, so the normal

        # read op still delivers them (this is why a page could render its

        # HTML but look unstyled until the agent was updated).
        out2, err3 = await _agent_run_file_op(user, agent, {"op": "read", "path": path})
        if err3:
            return err3
        if not out2 or not out2.get("ok"):
            return JSONResponse(status_code=404, content={"error": (out2 or {}).get("error", "Not found")})
        data = str(out2.get("content") or "").encode("utf-8")
    ext = os.path.splitext(path)[1].lower()
    media = _ASSET_MIME_OVERRIDES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    proj = (request.query_params.get("proj", "") or "").replace("\\", "/").strip("/")
    if ext == ".css":
        import posixpath
        css_dir = posixpath.dirname(path.replace("\\", "/"))

        def url_for(rel):
            return ("/api/coding/remote-asset?agent=" + _quote(agent, safe="") +
                    "&path=" + _quote(rel, safe="/") +
                    (("&proj=" + _quote(proj, safe="/")) if proj else "") +
                    "&t=" + _mint_preview_token(user["id"], _remote_tokkey(agent, rel)))
        try:
            data = _rewrite_css_urls(data.decode("utf-8"), css_dir, url_for=url_for,
                                     base_root=proj).encode("utf-8")
        except Exception:
            pass
    return _Response(content=data, media_type=media,
                     headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})



# ==========================================

# Generated 3D models: metadata + asset store per user.

# Assets live under user_data/<user>/models/ with a models.json index, so a

# generated model survives restarts and is fetched by the Model Gallery.

# ==========================================
_MODEL_MIME = {
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".obj": "text/plain",
    ".mtl": "text/plain",
    ".stl": "model/stl",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_MODEL_MAX_BYTES = 64 * 1024 * 1024


def _models_dir(user):
    d = os.path.join(_user_data_dir(_safe_username(user["username"])), "models")
    os.makedirs(d, exist_ok=True)
    return d


def _models_index_path(user):
    return os.path.join(_models_dir(user), "models.json")


def _models_load(user):
    try:
        with open(_models_index_path(user), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"models": []}


def _models_save(user, data):
    p = _models_index_path(user)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def _model_public(m):
    return {k: m.get(k) for k in
            ("id", "name", "prompt", "format", "generationMethod", "quality",
             "polygonCount", "materialCount", "textureCount", "fileSize",
             "dimensions", "createdAt", "status", "thumbnail",
             "model", "shape")}


@app.get("/api/models")
async def models_list(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data = _models_load(user)
    models = [_model_public(m) for m in data["models"]]
    models.sort(key=lambda m: str(m.get("createdAt") or ""), reverse=True)
    return {"models": models}


@app.post("/api/models")
async def models_create(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    fmt = str(data.get("format", "") or "").lower().lstrip(".")
    if fmt not in ("glb", "gltf", "obj", "stl"):
        return JSONResponse(status_code=400, content={"error": "Unsupported model format"})
    mid = "m" + secrets.token_hex(8)
    store = _models_load(user)
    rel = os.path.join("models", mid + "." + fmt).replace("\\", "/")
    full = os.path.join(_user_data_dir(_safe_username(user["username"])), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    try:
        b64 = data.get("asset_b64")
        if b64:
            raw = base64.b64decode(b64)
            if len(raw) > _MODEL_MAX_BYTES:
                return JSONResponse(status_code=400, content={"error": "Model too large (max 64MB)"})
            with open(full, "wb") as f:
                f.write(raw)
        else:
            return JSONResponse(status_code=400, content={"error": "No asset data"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    entry = {
        "id": mid,
        "name": str(data.get("name", "") or "Untitled model")[:128],
        "prompt": str(data.get("prompt", "") or "")[:2000],
        "format": fmt,
        "path": rel,
        "generationMethod": str(data.get("generationMethod", "") or "procedural")[:64],
        "quality": str(data.get("quality", "") or "")[:32],
        "polygonCount": int(data.get("polygonCount") or 0),
        "materialCount": int(data.get("materialCount") or 0),
        "textureCount": int(data.get("textureCount") or 0),
        "fileSize": os.path.getsize(full),
        "dimensions": data.get("dimensions") or None,
        "thumbnail": data.get("thumbnail") or None,
        "model": str(data.get("model", "") or "")[:128],
        "shape": str(data.get("shape", "") or "")[:32],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "ready",
    }
    store["models"].append(entry)
    _models_save(user, store)
    _audit("model.create", user["username"], f"{mid} {entry['format']} {entry['polygonCount']} tris")
    return {"status": "ok", "model": _model_public(entry)}


@app.get("/api/models/{model_id}/asset")
async def models_asset(request: Request, model_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    store = _models_load(user)
    entry = None
    for m in store["models"]:
        if m.get("id") == model_id:
            entry = m
            break
    if entry is None:
        return JSONResponse(status_code=404, content={"error": "Model not found"})
    base = _user_data_dir(_safe_username(user["username"]))
    full = os.path.join(base, str(entry.get("path", "")))
    if not _is_within(base, full) or not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "Asset missing"})
    ext = os.path.splitext(full)[1].lower()
    return FileResponse(full, media_type=_MODEL_MIME.get(ext, "application/octet-stream"),
                        filename=entry.get("name", "model") + ext,
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/models/{model_id}")
async def models_delete(request: Request, model_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    store = _models_load(user)
    keep, gone = [], None
    for m in store["models"]:
        if m.get("id") == model_id:
            gone = m
        else:
            keep.append(m)
    if gone is None:
        return JSONResponse(status_code=404, content={"error": "Model not found"})
    store["models"] = keep
    _models_save(user, store)
    try:
        base = _user_data_dir(_safe_username(user["username"]))
        full = os.path.join(base, str(gone.get("path", "")))
        if _is_within(base, full) and os.path.isfile(full):
            os.remove(full)
    except OSError:
        pass
    _audit("model.delete", user["username"], model_id)
    return {"status": "ok"}



# ==========================================

# Public sharing (submit -> admin review -> release)

# ==========================================
PUBLIC_SUBMIT_MAX_BYTES = 2 * 1024 * 1024
PUBLIC_FILE_MAX_BYTES = 200 * 1024
PUBLIC_KINDS = ("project", "model", "asset")


def _public_slug(conn, user_id, name: str, exclude_id=None) -> str:
    """Per-user-unique URL slug for /public/<username>/<slug> share links."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60].strip("-") or "project"
    slug = base
    n = 2
    while True:
        q = "SELECT id FROM public_projects WHERE user_id = ? AND slug = ?"
        args = [user_id, slug]
        if exclude_id:
            q += " AND id != ?"
            args.append(exclude_id)
        if not conn.execute(q, args).fetchone():
            return slug
        slug = (base[:55].rstrip("-") + "-" + str(n))
        n += 1


def _pub_row(r):
    show = r["show_author"] if "show_author" in r.keys() else 1
    mf = ""
    try:
        mf = r["main_file"] or "" if "main_file" in r.keys() else ""
    except Exception:
        mf = ""
    slug = ""
    try:
        slug = (r["slug"] or "") if "slug" in r.keys() else ""
    except Exception:
        slug = ""
    src_proj = src_folder = ""
    try:
        src_proj = (r["source_project"] or "") if "source_project" in r.keys() else ""
        src_folder = (r["source_folder"] or "") if "source_folder" in r.keys() else ""
    except Exception:
        src_proj = src_folder = ""
    uname = r["username"] or ""
    return {
        "id": r["id"],
        "name": r["name"] or "Untitled",
        "description": r["description"] or "",
        "kind": r["kind"] or "project",
        "author": uname if show else "",
        "username": uname,
        "slug": slug,
        "can_update": bool(src_proj or src_folder),
        "source_project": src_proj,
        "allow_download": bool(r["allow_download"]),
        "allow_edits": bool(1 if ("allow_edits" in r.keys() and r["allow_edits"]) else 0),
        "status": r["status"],
        "favorites": int(r["fav_count"] or 0),
        "main_file": mf,
        "created_at": r["created_at"],
    }


@app.post("/api/public/submit")
async def public_submit(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    name = str(data.get("name", ""))[:120].strip() or "Untitled"
    desc = str(data.get("description", ""))[:2000]
    kind = str(data.get("kind", "project")).lower()
    if kind not in PUBLIC_KINDS:
        kind = "project"

    # The author's username is required: shared links are /public/<username>/<slug>

    # and the submitter is always credited.
    show_author = 1
    allow_download = 1 if data.get("allow_download", True) else 0
    allow_edits = 1 if (data.get("allow_edits") and allow_download) else 0
    files = []
    total = 0

    # Remember where this came from so the owner can later "update" the

    # published copy to the latest version of the original project.
    source_project = ""
    source_folder = ""

    # Copy a 3D model from the user's own store by id.
    mid = str(data.get("model_id", "") or "").strip()
    if mid:
        store = _models_load(user)
        m = next((x for x in store.get("models", []) if x.get("id") == mid), None)
        if not m:
            return JSONResponse(status_code=404, content={"error": "Model not found"})
        rel = str(m.get("path", "") or "")
        base = _user_data_dir(_safe_username(user["username"]))
        full = os.path.join(base, rel)
        if not _is_within(base, full) or not os.path.isfile(full):
            return JSONResponse(status_code=404, content={"error": "Model file missing"})
        try:
            with open(full, "rb") as f:
                raw = f.read()
        except OSError as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        if len(raw) > PUBLIC_SUBMIT_MAX_BYTES:
            return JSONResponse(status_code=400, content={"error": "Model file too large"})
        files.append({"path": (m.get("name") or "model") + "." + (m.get("format", "glb") or "glb"),
                      "size": len(raw),
                      "content": "base64:" + base64.b64encode(raw).decode()})
        total = len(raw)
    else:
        proj_name = str(data.get("project_name", "") or "").strip().replace("\\", "/").strip("/")
        if proj_name and not data.get("files"):

            # Folder-based submit: read the listed paths server-side from a saved project.
            if ".." in proj_name.split("/") or proj_name.startswith("."):
                return JSONResponse(status_code=400, content={"error": "Bad project name"})
            pdir = os.path.join(_user_projects_dir(_safe_username(user["username"])), proj_name)
            if not os.path.isdir(pdir) or not _is_within(_user_projects_dir(_safe_username(user["username"])), pdir):
                return JSONResponse(status_code=404, content={"error": "Project not found"})
            source_project = proj_name
            for p in (data.get("paths") or [])[:200]:
                p = str(p or "")[:200].strip().lstrip("/").replace("\\", "/")
                if not p or ".." in p.split("/"):
                    continue
                full = os.path.join(pdir, p)
                if not _is_within(pdir, full) or not os.path.isfile(full):
                    continue
                try:
                    with open(full, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                if len(raw) > PUBLIC_FILE_MAX_BYTES:
                    return JSONResponse(status_code=400, content={"error": f"File too large: {p}"})
                total += len(raw)
                if total > PUBLIC_SUBMIT_MAX_BYTES:
                    return JSONResponse(status_code=400, content={"error": "Submission too large (2MB max)"})
                try:
                    c = raw.decode("utf-8")
                except Exception:
                    c = "base64:" + base64.b64encode(raw).decode()
                    files.append({"path": p, "size": len(raw), "content": c})
                    continue
                files.append({"path": p, "size": len(raw), "content": c})
        else:
            for f in (data.get("files") or [])[:200]:
                if not isinstance(f, dict):
                    continue
                p = str(f.get("path", ""))[:200].strip().lstrip("/").replace("\\", "/")
                if not p or ".." in p.split("/"):
                    continue
                c = f.get("content", "")
                if not isinstance(c, str):
                    c = ""
                if len(c.encode("utf-8", "ignore")) > PUBLIC_FILE_MAX_BYTES:
                    return JSONResponse(status_code=400, content={"error": f"File too large: {p}"})
                total += len(c.encode("utf-8", "ignore"))
                if total > PUBLIC_SUBMIT_MAX_BYTES:
                    return JSONResponse(status_code=400, content={"error": "Submission too large (2MB max)"})
                files.append({"path": p, "size": len(c.encode("utf-8", "ignore")), "content": c})
    if not files:
        return JSONResponse(status_code=400, content={"error": "Nothing to submit"})
    main_file = str(data.get("main_file", "") or "")[:200].strip().lstrip("/").replace("\\", "/")
    paths = {f["path"] for f in files}
    if main_file and main_file not in paths:
        main_file = ""

    # Link the source folder (publishing the currently-open folder) so an

    # "update" can re-read it later. Ignored when not accessible.
    if not source_project:
        raw_folder = str(data.get("folder", "") or "").strip()
        if raw_folder:
            try:
                cand = os.path.normpath(raw_folder)
                if not _folder_access_error(user, cand) and os.path.isdir(cand):
                    source_folder = cand
            except Exception:
                source_folder = ""
    source_paths = json.dumps([f["path"] for f in files])
    conn = get_db()
    try:
        slug = _public_slug(conn, user["id"], name)
        cur = conn.execute(
            "INSERT INTO public_projects (user_id, username, show_author, name, description, kind, allow_download, allow_edits, status, main_file, slug, source_project, source_folder, source_paths)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (user["id"], user.get("username", ""), show_author, name, desc, kind, allow_download,
             allow_edits, main_file, slug, source_project, source_folder, source_paths))
        pid = cur.lastrowid
        for f in files:
            conn.execute("INSERT INTO public_project_files (project_id, path, size, content) VALUES (?, ?, ?, ?)",
                         (pid, f["path"], f["size"], f["content"]))
        conn.commit()
    finally:
        conn.close()
    _audit("public.submit", user.get("username", ""), f"{pid} {name}")
    return {"status": "ok", "id": pid, "slug": slug}


@app.get("/api/public/projects")
async def public_list(request: Request, q: str = "", kind: str = "", sort: str = "new"):
    _user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        where = ["status = 'approved'"]
        params: list = []
        if kind in PUBLIC_KINDS:
            where.append("kind = ?")
            params.append(kind)
        if q:
            where.append("(name LIKE ? OR description LIKE ? OR username LIKE ?)")
            like = f"%{q.strip()[:60]}%"
            params += [like, like, like]
        order = "fav_count DESC, created_at DESC" if sort == "top" else "created_at DESC"
        rows = conn.execute(
            f"SELECT * FROM public_projects WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 200",
            params).fetchall()
        return {"projects": [_pub_row(r) for r in rows]}
    finally:
        conn.close()


def _public_payload(conn, r, user):
    """Detail payload shared by the id- and slug-addressed public endpoints."""
    files = conn.execute("SELECT path, size FROM public_project_files WHERE project_id = ? ORDER BY path",
                         (r["id"],)).fetchall()
    fav = conn.execute("SELECT 1 FROM project_favorites WHERE user_id = ? AND project_id = ?",
                       (user["id"], r["id"])).fetchone()
    out = _pub_row(r)
    out["files"] = [{"path": f["path"], "size": f["size"]} for f in files]
    out["favorited"] = bool(fav)
    out["mine"] = r["user_id"] == user["id"]
    return out


@app.get("/api/public/projects/{pid}")
async def public_detail(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["status"] != "approved" and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        return _public_payload(conn, r, user)
    finally:
        conn.close()


@app.get("/api/public/by-user/{username}/{slug}")
async def public_by_slug(request: Request, username: str, slug: str):
    """Resolve a shareable /public/<username>/<slug> link to a submission."""
    user, err = _require_auth(request)
    if err:
        return err
    username = (username or "").strip().lower()[:64]
    slug = (slug or "").strip().lower()[:80]
    if not username or not slug:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM public_projects WHERE username = ? AND slug = ? ORDER BY id DESC LIMIT 1",
            (username, slug)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["status"] != "approved" and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        return _public_payload(conn, r, user)
    finally:
        conn.close()


@app.get("/api/public/projects/{pid}/download")
async def public_download(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["status"] != "approved" and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if not r["allow_download"] and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "The creator disabled downloads"})
        files = conn.execute("SELECT path, content FROM public_project_files WHERE project_id = ? ORDER BY path",
                             (pid,)).fetchall()
    finally:
        conn.close()
    if not files:
        return JSONResponse(status_code=404, content={"error": "Empty project"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            c = f["content"] or ""
            if c.startswith("base64:"):
                try:
                    zf.writestr(f["path"], base64.b64decode(c[7:]))
                except Exception:
                    continue
            else:
                zf.writestr(f["path"], c)
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", r["name"] or "project")[:60] or "project"
    disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(fname + ".zip")

    # Return the bytes directly: FileResponse streams a path lazily, so any

    # temp file deleted here (in finally) would be gone before it is read,

    # giving a 500. Submissions are size-capped, so an in-memory response is

    # both simpler and race-free.
    from fastapi.responses import Response as _Response
    return _Response(content=buf.getvalue(), media_type="application/zip",
                     headers={"Content-Disposition": disp})


@app.get("/api/public/projects/{pid}/file")
async def public_file(request: Request, pid: int, path: str = ""):
    """Fetch one stored file's content (for the Run preview)."""
    user, err = _require_auth(request)
    if err:
        return err
    p = (path or "").strip().lstrip("/").replace("\\", "/")
    if not p or ".." in p.split("/"):
        return JSONResponse(status_code=400, content={"error": "Bad path"})
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["status"] != "approved" and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        f = conn.execute("SELECT path, size, content FROM public_project_files WHERE project_id = ? AND path = ?",
                         (pid, p)).fetchone()
        if not f:
            return JSONResponse(status_code=404, content={"error": "File not found"})
        c = f["content"] or ""
        if c.startswith("base64:"):
            return {"path": f["path"], "encoding": "base64", "content": c[7:]}
        return {"path": f["path"], "encoding": "text", "content": c}
    finally:
        conn.close()


@app.post("/api/public/projects/{pid}/favorite")
async def public_favorite(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT id FROM public_projects WHERE id = ? AND status = 'approved'", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        has = conn.execute("SELECT 1 FROM project_favorites WHERE user_id = ? AND project_id = ?",
                           (user["id"], pid)).fetchone()
        if has:
            conn.execute("DELETE FROM project_favorites WHERE user_id = ? AND project_id = ?", (user["id"], pid))
            conn.execute("UPDATE public_projects SET fav_count = MAX(0, fav_count - 1) WHERE id = ?", (pid,))
            conn.commit()
            return {"favorited": False}
        conn.execute("INSERT INTO project_favorites (user_id, project_id) VALUES (?, ?)", (user["id"], pid))
        conn.execute("UPDATE public_projects SET fav_count = fav_count + 1 WHERE id = ?", (pid,))
        conn.commit()
        return {"favorited": True}
    finally:
        conn.close()


@app.get("/api/public/mine")
async def public_mine(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        mine = conn.execute("SELECT * FROM public_projects WHERE user_id = ? ORDER BY created_at DESC",
                            (user["id"],)).fetchall()
        favs = conn.execute(
            "SELECT p.* FROM public_projects p JOIN project_favorites f ON f.project_id = p.id"
            " WHERE f.user_id = ? AND p.status = 'approved' ORDER BY f.created_at DESC",
            (user["id"],)).fetchall()
        pend = []
        if user.get("is_admin"):
            pend = conn.execute("SELECT * FROM public_projects WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        return {"mine": [_pub_row(r) for r in mine],
                "favorites": [_pub_row(r) for r in favs],
                "pending": [_pub_row(r) for r in pend],
                "is_admin": bool(user.get("is_admin"))}
    finally:
        conn.close()


@app.delete("/api/public/projects/{pid}")
async def public_delete(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT user_id FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        conn.execute("DELETE FROM public_projects WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()
    _audit("public.delete", user.get("username", ""), str(pid))
    return {"status": "ok"}


@app.post("/api/public/projects/{pid}/review")
async def public_review(request: Request, pid: int):
    _admin, err = _require_admin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    action = str((data or {}).get("action", "")).lower()
    if action not in ("approve", "reject"):
        return JSONResponse(status_code=400, content={"error": "action must be approve or reject"})
    conn = get_db()
    try:
        if action == "approve":
            conn.execute("UPDATE public_projects SET status = 'approved', approved_at = datetime('now') WHERE id = ?", (pid,))
        else:
            conn.execute("UPDATE public_projects SET status = 'rejected' WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()
    _audit("public.review", _admin.get("username", ""), f"{pid} {action}")
    return {"status": "ok"}


@app.post("/api/public/projects/{pid}/update")
async def public_update(request: Request, pid: int):
    """Re-sync a published project with the latest version of its source.

    Re-reads the originally-included file list from the saved project (or the
    linked folder) and replaces the stored snapshot. Owner/admin only; the
    review status is preserved so an approved project stays live.
    """
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Not yours"})

        def _col(name):
            try:
                return (r[name] or "") if name in r.keys() else ""
            except Exception:
                return ""

        src_proj = _col("source_project")
        src_folder = _col("source_folder")
        try:
            paths = json.loads(_col("source_paths") or "[]")
        except Exception:
            paths = []
        if not isinstance(paths, list):
            paths = []

        # Resolve the base folder to re-read from.
        base = ""
        if src_proj:
            pdir = _user_projects_dir(_safe_username(r["username"] or user["username"]))
            cand = os.path.join(pdir, src_proj)
            if os.path.isdir(cand) and _is_within(pdir, cand):
                base = cand
        if not base and src_folder:
            if not _folder_access_error(user, src_folder) and os.path.isdir(src_folder):
                base = src_folder
        if not base:
            return JSONResponse(status_code=400, content={
                "error": "This project has no linked source folder — re-publish it to enable updates."})
        if not paths:
            paths = [row["path"] for row in conn.execute(
                "SELECT path FROM public_project_files WHERE project_id = ?", (pid,)).fetchall()]
        new_files = []
        total = 0
        for p in paths[:200]:
            p = str(p or "")[:200].strip().lstrip("/").replace("\\", "/")
            if not p or ".." in p.split("/"):
                continue
            full = os.path.join(base, p)
            if not _is_within(base, full) or not os.path.isfile(full):
                continue
            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if len(raw) > PUBLIC_FILE_MAX_BYTES:
                return JSONResponse(status_code=400, content={"error": f"File too large: {p}"})
            total += len(raw)
            if total > PUBLIC_SUBMIT_MAX_BYTES:
                return JSONResponse(status_code=400, content={"error": "Project too large (2MB max)"})
            try:
                c = raw.decode("utf-8")
            except Exception:
                c = "base64:" + base64.b64encode(raw).decode()
            new_files.append({"path": p, "size": len(raw), "content": c})
        if not new_files:
            return JSONResponse(status_code=400, content={
                "error": "The source project has no matching files — nothing to update."})
        main_file = _col("main_file")
        if main_file and main_file not in {f["path"] for f in new_files}:
            main_file = ""
        conn.execute("DELETE FROM public_project_files WHERE project_id = ?", (pid,))
        for f in new_files:
            conn.execute("INSERT INTO public_project_files (project_id, path, size, content) VALUES (?, ?, ?, ?)",
                         (pid, f["path"], f["size"], f["content"]))

        # An update is a new revision: send it back for re-approval so it is

        # never published to the world without a fresh review.
        conn.execute("UPDATE public_projects SET main_file = ?, source_paths = ?,"
                     " status = 'pending', approved_at = NULL WHERE id = ?",
                     (main_file, json.dumps([f["path"] for f in new_files]), pid))
        conn.commit()
    finally:
        conn.close()
    _audit("public.update", user.get("username", ""), f"{pid}")
    return {"status": "ok", "files": len(new_files), "review": "pending"}


def _public_fork_dir(user_id: str) -> str:
    """Per-user folder holding this account's forks of published projects."""
    return _ensure_dir(os.path.join(_user_data_dir(user_id), "public_forks"))


def _public_fork_folder_name(pub_name: str) -> str:
    """A filesystem-safe, project-valid folder name derived from a publication."""
    base = re.sub(r'[\\/:*?"<>|]+', "-", (pub_name or "Untitled").strip())
    base = re.sub(r"\s+", " ", base).strip(" .") or "published-project"
    return base[:80]


def _public_fork_row_for(user_id: int, pid: int):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM public_forks WHERE project_id = ? AND user_id = ?", (pid, user_id)).fetchone()
    finally:
        conn.close()


def _public_fork_for_folder(folder: str):
    """Row (or None) when `folder` is a user's fork of a published project."""
    try:
        norm = os.path.abspath(folder or "")
        conn = get_db()
        try:
            r = conn.execute("SELECT * FROM public_forks WHERE folder = ?", (norm,)).fetchone()
        finally:
            conn.close()
        return r
    except Exception:
        return None


def _public_read_fork_files(folder: str):
    """Snapshot text files of a fork folder (same caps as a publish) for a
    submission. Returns (files, total); each file is {path, size, content}
    with binary files base64-encoded."""
    files = []
    total = 0
    tree = _build_file_tree(folder)
    walk = [tree]
    paths = []
    while walk:
        cur = walk.pop()
        for e in cur or []:
            if e.get("type") == "dir":
                walk.append(e.get("children") or [])
            else:
                paths.append(e.get("path") or e.get("name") or "")
    for p in sorted(paths)[:200]:
        full = os.path.join(folder, p)
        if not _is_within(folder, full) or not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if len(raw) > PUBLIC_FILE_MAX_BYTES:
            continue
        total += len(raw)
        if total > PUBLIC_SUBMIT_MAX_BYTES:
            break
        try:
            c = raw.decode("utf-8")
        except Exception:
            c = "base64:" + base64.b64encode(raw).decode()
        files.append({"path": p, "size": len(raw), "content": c})
    return files, total


@app.post("/api/public/projects/{pid}/fork")
async def public_fork(request: Request, pid: int):
    """Copy a published project (allow_edits) into the caller's own folder so
    they can work on it and submit the changes back. Idempotent per user."""
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["status"] != "approved" and r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] == user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "This is your own project"})
        if not r["allow_edits"] and r["user_id"] != user["id"]:
            return JSONResponse(status_code=403, content={"error": "This creator did not enable other-people edits"})
        existing = _public_fork_row_for(user["id"], pid)
        if existing:
            return {"status": "ok", "already": True, "name": existing["name"],
                    "folder": existing["folder"], "submitted": bool(existing["submitted"])}
        files = conn.execute("SELECT path, content FROM public_project_files WHERE project_id = ? ORDER BY path",
                             (pid,)).fetchall()
    finally:
        conn.close()
    if not files:
        return JSONResponse(status_code=400, content={"error": "Empty project — nothing to copy"})
    user_id = _safe_username(user["username"])
    base = _public_fork_dir(user_id)
    base_name = _public_fork_folder_name(r["name"])
    name, n = base_name, 2
    while os.path.isdir(os.path.join(base, name)):
        name = (base_name[:72].rstrip() + "-" + str(n))
        n += 1
    dest = os.path.join(base, name)
    try:
        os.makedirs(os.path.join(dest, ".omniilink"), exist_ok=True)
        meta = {"public_project_id": pid, "name": r["name"], "forked_at": time.time()}
        with open(os.path.join(dest, ".omniilink", "public-fork.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
    seen = set()
    for f in files:
        rel = f["path"] or ""
        if rel in seen or ".." in rel.split("/"):
            continue
        seen.add(rel)
        full = os.path.join(dest, rel)
        if not _is_within(dest, full):
            continue
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            c = f["content"] or ""
            data = base64.b64decode(c[7:]) if c.startswith("base64:") else c.encode("utf-8", "ignore")
            with open(full, "wb") as fh:
                fh.write(data)
        except Exception:
            continue
    conn = get_db()
    try:
        conn.execute("INSERT INTO public_forks (project_id, user_id, folder, name) VALUES (?, ?, ?, ?)",
                     (pid, user["id"], os.path.abspath(dest), name))
        conn.commit()
    finally:
        conn.close()
    _audit("public.fork", user.get("username", ""), f"{pid} {name}")
    return {"status": "ok", "already": False, "name": name, "folder": os.path.abspath(dest), "submitted": False}


@app.get("/api/public/forks")
async def public_my_forks(request: Request):
    """The published projects this user has forked (for the Public section)."""
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT f.*, p.name AS pub_name, p.username AS author, p.allow_edits "
            "FROM public_forks f LEFT JOIN public_projects p ON p.id = f.project_id"
            " WHERE f.user_id = ? ORDER BY f.created_at DESC", (user["id"],)).fetchall()
        out = []
        for f in rows:
            folder = f["folder"] or ""
            try:
                if not os.path.isdir(folder):
                    continue
                file_count, dir_size = _scan_project_dir(folder)
                out.append({"name": f["name"], "path": folder, "file_count": file_count,
                            "size_bytes": dir_size, "modified": os.path.getmtime(folder),
                            "public": True, "project_id": f["project_id"],
                            "pub_name": (f["pub_name"] or ""), "author": (f["author"] or ""),
                            "submitted": bool(f["submitted"]), "fork_id": f["id"]})
            except Exception:
                continue
        return {"forks": out}
    finally:
        conn.close()


@app.post("/api/public/projects/{pid}/contribute")
async def public_contribute(request: Request, pid: int):
    """Submit the current state of the caller's fork back to the author.

    A snapshot of the fork folder is stored server-side (the author applies
    it later); this never writes into the published project itself."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    desc = str((data or {}).get("description", ""))[:2000]
    conn = get_db()
    try:
        r = conn.execute("SELECT id, user_id FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] == user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "You are the author"})
        fork = conn.execute("SELECT * FROM public_forks WHERE project_id = ? AND user_id = ?",
                            (pid, user["id"])).fetchone()
    finally:
        conn.close()
    if not fork:
        return JSONResponse(status_code=400, content={"error": "Fork this project before submitting changes"})
    folder = fork["folder"] or ""
    base = _public_fork_dir(_safe_username(user["username"]))
    if not folder or not os.path.isdir(folder) or not _is_within(base, folder):
        return JSONResponse(status_code=400, content={"error": "Your fork folder is missing — fork the project again"})
    files, _total = _public_read_fork_files(folder)
    if not files:
        return JSONResponse(status_code=400, content={"error": "Your fork has no files — add something first"})
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO public_contributions (project_id, fork_id, user_id, username, description, status, files_json)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (pid, fork["id"], user["id"], user.get("username", ""), desc, json.dumps(files)))
        cid = cur.lastrowid
        conn.execute("UPDATE public_forks SET submitted = 1 WHERE id = ?", (fork["id"],))
        conn.commit()
    finally:
        conn.close()
    _audit("public.contribute", user.get("username", ""),
           f"{pid} -> contribution {cid} ({len(files)} files)")
    return {"status": "ok", "contribution_id": cid, "files": len(files)}


@app.post("/api/public/projects/{pid}/edit-request")
async def public_edit_request(request: Request, pid: int):
    """Send the author a message: bug report, feature request, etc."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    message = str((data or {}).get("message", "")).strip()[:2000]
    if not message:
        return JSONResponse(status_code=400, content={"error": "Message is required"})
    conn = get_db()
    try:
        r = conn.execute("SELECT user_id FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] == user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "This is your own project"})
        cur = conn.execute(
            "INSERT INTO public_edit_requests (project_id, user_id, username, message)"
            " VALUES (?, ?, ?, ?)", (pid, user["id"], user.get("username", ""), message))
        rid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _audit("public.edit_request", user.get("username", ""), f"{pid} -> {rid}")
    return {"status": "ok", "edit_request_id": rid}


def _public_owner_or_admin(conn, r, user):
    """Authorisation gate shared by the author-facing public endpoints."""
    if not r:
        return False
    return r["user_id"] == user["id"] or bool(user.get("is_admin"))


@app.get("/api/public/projects/{pid}/contributions")
async def public_list_contributions(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT id, user_id FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if not _public_owner_or_admin(conn, r, user):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        rows = conn.execute(
            "SELECT id, username, description, status, created_at,"
            " (SELECT COUNT(*) FROM json_each(public_contributions.files_json)) AS file_count"
            " FROM public_contributions WHERE project_id = ? ORDER BY created_at DESC", (pid,)).fetchall()
        return {"contributions": [{
            "id": x["id"], "username": x["username"] or "", "description": x["description"] or "",
            "status": x["status"] or "pending", "created_at": x["created_at"],
            "file_count": int(x["file_count"] or 0)} for x in rows]}
    finally:
        conn.close()


@app.get("/api/public/contributions/{cid}")
async def public_contribution_detail(request: Request, cid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM public_contributions WHERE id = ?", (cid,)).fetchone()
        if not c:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        proj = conn.execute("SELECT user_id FROM public_projects WHERE id = ?",
                            (c["project_id"],)).fetchone()
        if not _public_owner_or_admin(conn, proj, user):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        try:
            files = json.loads(c["files_json"] or "[]")
        except Exception:
            files = []
        return {"id": c["id"], "project_id": c["project_id"], "username": c["username"] or "",
                "description": c["description"] or "", "status": c["status"],
                "created_at": c["created_at"], "files": files}
    finally:
        conn.close()


@app.post("/api/public/contributions/{cid}/review")
async def public_contribution_review(request: Request, cid: int):
    """The author applies (or rejects) a submitted contribution.

    Approving replaces the published snapshot with the contributor's files and
    sends the project back to pending so an admin re-approves the revision."""
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    action = str((data or {}).get("action", "")).lower()
    if action not in ("approve", "reject"):
        return JSONResponse(status_code=400, content={"error": "action must be approve or reject"})
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM public_contributions WHERE id = ?", (cid,)).fetchone()
        if not c:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        proj = conn.execute("SELECT user_id FROM public_projects WHERE id = ?",
                            (c["project_id"],)).fetchone()
        if not _public_owner_or_admin(conn, proj, user):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        if action == "approve":
            try:
                files = json.loads(c["files_json"] or "[]")
            except Exception:
                files = []
            if not files:
                return JSONResponse(status_code=400, content={"error": "Contribution has no files"})
            allowed = {f["path"] for f in files}
            main_file = conn.execute("SELECT main_file FROM public_projects WHERE id = ?",
                                     (c["project_id"],)).fetchone()["main_file"] or ""
            if main_file and main_file not in allowed:
                main_file = ""
            conn.execute("DELETE FROM public_project_files WHERE project_id = ?", (c["project_id"],))
            for f in files:
                conn.execute("INSERT INTO public_project_files (project_id, path, size, content)"
                             " VALUES (?, ?, ?, ?)",
                             (c["project_id"], f["path"], int(f.get("size") or 0), f.get("content") or ""))
            conn.execute("UPDATE public_projects SET main_file = ?, source_paths = ?,"
                         " status = 'pending', approved_at = NULL WHERE id = ?",
                         (main_file, json.dumps([f["path"] for f in files]), c["project_id"]))
        conn.execute("UPDATE public_contributions SET status = ?, reviewed_at = datetime('now') WHERE id = ?",
                     (action, cid))
        conn.commit()
    finally:
        conn.close()
    _audit("public.contribution_review", user.get("username", ""), f"{cid} {action}")
    return {"status": "ok", "review": action}


@app.get("/api/public/projects/{pid}/edit-requests")
async def public_list_edit_requests(request: Request, pid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT id, user_id FROM public_projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if not _public_owner_or_admin(conn, r, user):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        rows = conn.execute(
            "SELECT * FROM public_edit_requests WHERE project_id = ? ORDER BY created_at DESC",
            (pid,)).fetchall()
        return {"edit_requests": [{
            "id": x["id"], "username": x["username"] or "", "message": x["message"] or "",
            "read": bool(x["read"]), "created_at": x["created_at"]} for x in rows]}
    finally:
        conn.close()


@app.post("/api/public/edit-requests/{rid}/read")
async def public_edit_request_read(request: Request, rid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        er = conn.execute("SELECT project_id FROM public_edit_requests WHERE id = ?", (rid,)).fetchone()
        if not er:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        proj = conn.execute("SELECT user_id FROM public_projects WHERE id = ?",
                            (er["project_id"],)).fetchone()
        if not _public_owner_or_admin(conn, proj, user):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        conn.execute("UPDATE public_edit_requests SET read = 1 WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/admin/public")
async def admin_public_list(request: Request, status: str = "pending"):
    """Admin review queue: every public submission with its real author.

    `_pub_row` hides the username when the creator opted out of showing it,
    but moderators must always see who submitted something, so the raw
    username/user_id are added back here. File count/size are included so the
    admin can judge the submission without opening it.
    """
    _admin, err = _require_admin(request)
    if err:
        return err
    status = (status or "").strip().lower()
    conn = get_db()
    try:
        if status in ("pending", "approved", "rejected"):
            rows = conn.execute(
                "SELECT * FROM public_projects WHERE status = ? ORDER BY created_at DESC LIMIT 500",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM public_projects ORDER BY created_at DESC LIMIT 500").fetchall()
        out = []
        for r in rows:
            fc = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS s "
                "FROM public_project_files WHERE project_id = ?", (r["id"],)).fetchone()
            row = _pub_row(r)
            row["username"] = r["username"] or ""
            row["user_id"] = r["user_id"]
            row["show_author"] = bool(r["show_author"])
            row["files"] = int(fc["c"] if fc else 0)
            row["bytes"] = int(fc["s"] if fc else 0)
            row["approved_at"] = r["approved_at"]
            out.append(row)
        counts = {}
        for st in ("pending", "approved", "rejected"):
            counts[st] = conn.execute(
                "SELECT COUNT(*) AS c FROM public_projects WHERE status = ?",
                (st,)).fetchone()["c"]
        return {"projects": out, "counts": counts}
    finally:
        conn.close()



# ==========================================

# Forums

# ==========================================
@app.get("/api/forums/sections")
async def forums_sections(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        secs = conn.execute("SELECT * FROM forum_sections ORDER BY sort, id").fetchall()
        out = []
        for s in secs:
            tc = conn.execute("SELECT COUNT(*) AS c FROM forum_threads WHERE section_id = ?", (s["id"],)).fetchone()["c"]
            out.append({"id": s["id"], "name": s["name"], "description": s["description"], "threads": tc})
        return {"sections": out}
    finally:
        conn.close()


@app.get("/api/forums/threads")
async def forums_threads(request: Request, section_id: int = 0):
    _user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        if section_id:
            rows = conn.execute("SELECT * FROM forum_threads WHERE section_id = ? ORDER BY last_post_at DESC LIMIT 100",
                                (section_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM forum_threads ORDER BY last_post_at DESC LIMIT 100").fetchall()
        out = []
        for r in rows:
            show = r["show_author"] if "show_author" in r.keys() else 1
            out.append({"id": r["id"], "section_id": r["section_id"], "title": r["title"],
                        "author": r["username"] if show else "", "posts": r["post_count"],
                        "created_at": r["created_at"], "last_post_at": r["last_post_at"],
                        "mine": r["user_id"] == _user["id"]})
        return {"threads": out}
    finally:
        conn.close()


@app.post("/api/forums/threads")
async def forums_thread_create(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    try:
        section_id = int(data.get("section_id", 0))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "section_id required"})
    title = str(data.get("title", ""))[:140].strip()
    content = str(data.get("content", ""))[:20000].strip()
    if not title or not content:
        return JSONResponse(status_code=400, content={"error": "title and content required"})
    show_author = 1 if data.get("show_author", True) else 0
    conn = get_db()
    try:
        sec = conn.execute("SELECT id FROM forum_sections WHERE id = ?", (section_id,)).fetchone()
        if not sec:
            return JSONResponse(status_code=404, content={"error": "Section not found"})
        cur = conn.execute(
            "INSERT INTO forum_threads (section_id, user_id, username, show_author, title, post_count) VALUES (?, ?, ?, ?, ?, 0)",
            (section_id, user["id"], user.get("username", ""), show_author, title))
        tid = cur.lastrowid
        conn.execute("INSERT INTO forum_posts (thread_id, user_id, username, show_author, content) VALUES (?, ?, ?, ?, ?)",
                     (tid, user["id"], user.get("username", ""), show_author, content))
        conn.execute("UPDATE forum_threads SET post_count = 1 WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "id": tid}


@app.get("/api/forums/threads/{tid}")
async def forums_thread_detail(request: Request, tid: int):
    _user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM forum_threads WHERE id = ?", (tid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        show = r["show_author"] if "show_author" in r.keys() else 1
        posts = conn.execute("SELECT * FROM forum_posts WHERE thread_id = ? ORDER BY id ASC LIMIT 500", (tid,)).fetchall()
        return {"id": r["id"], "section_id": r["section_id"], "title": r["title"],
                "author": r["username"] if show else "", "mine": r["user_id"] == _user["id"],
                "created_at": r["created_at"],
                "posts": [{"id": p["id"],
                           "author": p["username"] if (p["show_author"] if "show_author" in p.keys() else 1) else "",
                           "content": p["content"], "created_at": p["created_at"],
                           "mine": p["user_id"] == _user["id"]} for p in posts]}
    finally:
        conn.close()


@app.post("/api/forums/threads/{tid}/posts")
async def forums_post_create(request: Request, tid: int):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    content = str((data or {}).get("content", ""))[:20000].strip()
    if not content:
        return JSONResponse(status_code=400, content={"error": "content required"})
    show_author = 1 if (data or {}).get("show_author", True) else 0
    conn = get_db()
    try:
        r = conn.execute("SELECT id FROM forum_threads WHERE id = ?", (tid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        conn.execute("INSERT INTO forum_posts (thread_id, user_id, username, show_author, content) VALUES (?, ?, ?, ?, ?)",
                     (tid, user["id"], user.get("username", ""), show_author, content))
        conn.execute("UPDATE forum_threads SET post_count = post_count + 1, last_post_at = datetime('now') WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/forums/threads/{tid}")
async def forums_thread_delete(request: Request, tid: int):
    user, err = _require_auth(request)
    if err:
        return err
    conn = get_db()
    try:
        r = conn.execute("SELECT user_id FROM forum_threads WHERE id = ?", (tid,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if r["user_id"] != user["id"] and not user.get("is_admin"):
            return JSONResponse(status_code=403, content={"error": "Not yours"})
        conn.execute("DELETE FROM forum_threads WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


async def _drain_request_body(request: Request):
    """Read and discard the request body. Rejecting a request whose body was
    never read makes the server close the connection with unread data, which
    the client sees as a reset (WSAECONNABORTED) instead of the error."""
    try:
        async for _chunk in request.stream():
            pass
    except Exception:
        pass


def _extract_zip_safely(zip_path: str, dest: str, max_files: int = 20000,
                        max_total: int = 2 * 1024 * 1024 * 1024):
    """Extract a zip into dest, guarding against zip-slip, bombs and junk.

    Returns (file_count, total_bytes). Raises ValueError on unsafe entries,
    corrupt archives, or cap breaches."""
    import posixpath
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > max_files:
            raise ValueError(f"Too many files in zip ({len(members)})")
        total = sum(i.file_size for i in members)
        if total > max_total:
            raise ValueError("Uncompressed zip contents too large")
        os.makedirs(dest, exist_ok=True)
        count = 0
        for info in members:
            name = info.filename.replace("\\", "/").lstrip("/")
            if name.startswith("__MACOSX/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if (not parts or name.startswith("/")
                    or any(p == ".." for p in parts)
                    or any(re.match(r"^[A-Za-z]:$", p) for p in parts)):
                raise ValueError(f"Unsafe entry in zip: {info.filename!r}")
            target = os.path.join(dest, *parts)
            if os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(dest)) \
                    and not _is_within(dest, target):
                raise ValueError(f"Unsafe entry in zip: {info.filename!r}")
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            count += 1

    # posixpath import kept local to this helper's original scope note
    _ = posixpath  # silence linters about the local import
    return count, total


class _UploadRefused(Exception):
    def __init__(self, status, error):
        self.status = status
        self.error = error


def _upload_session_root(user_id: str) -> str:
    d = os.path.join(_user_data_dir(user_id), "coding_upload_parts")
    os.makedirs(d, exist_ok=True)
    return d


def _upload_session_dir(user_id: str, upload_id: str):
    """Session dir for a chunked upload, or None for a malformed id (never
    let a client-supplied id escape into a path)."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        return None
    return os.path.join(_upload_session_root(user_id), upload_id)


def _prune_upload_sessions(user_id: str):
    try:
        root = _upload_session_root(user_id)
        now = time.time()
        for name in os.listdir(root)[:500]:
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            try:
                if now - os.path.getmtime(d) > UPLOAD_PART_TTL_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def _load_upload_meta(user_id: str, upload_id: str):
    sdir = _upload_session_dir(user_id, upload_id)
    if not sdir or not os.path.isdir(sdir):
        return None, None
    try:
        with open(os.path.join(sdir, "meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            return None, None
        return sdir, meta
    except Exception:
        return None, None


def _save_upload_meta(sdir: str, meta: dict):
    with open(os.path.join(sdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _coding_after_store(user, user_id: str, folder: str, rel: str, size: int):
    """Shared tail for single-shot and chunked uploads: quota-check the file,
    auto-extract zips into a sibling folder named after the archive, log, and
    build the response dict. Raises _UploadRefused(status, error)."""
    qerr = _check_quota(user, user_id, size)
    if qerr is not None:
        try:
            os.remove(os.path.join(folder, rel))
        except OSError:
            pass
        try:
            msg = json.loads(qerr.body.decode("utf-8")).get("error", "Storage quota exceeded")
        except Exception:
            msg = "Storage quota exceeded"
        raise _UploadRefused(qerr.status_code, msg)
    resp = {"status": "ok", "path": rel, "size": size}
    if rel.lower().endswith(".zip"):
        base = os.path.basename(rel)
        stem = re.sub(r"[^A-Za-z0-9_.\- ]", "_", base[:-4].strip()).strip(" .")[:80] or "unzipped"
        parent = os.path.dirname(rel).replace("\\", "/")
        dest_rel = (parent + "/" + stem).lstrip("/") if parent else stem
        dest_full = os.path.join(folder, dest_rel)
        try:
            with zipfile.ZipFile(os.path.join(folder, rel)) as _zf:
                _uncompressed = sum(i.file_size for i in _zf.infolist() if not i.is_dir())
        except Exception:
            _uncompressed = 0
        if _uncompressed:
            _qerr = _check_quota(user, user_id, _uncompressed)
            if _qerr is not None:
                try:
                    os.remove(os.path.join(folder, rel))
                except OSError:
                    pass
                try:
                    _msg = json.loads(_qerr.body.decode("utf-8")).get("error", "Storage quota exceeded")
                except Exception:
                    _msg = "Storage quota exceeded"
                raise _UploadRefused(_qerr.status_code, _msg)
        try:
            count, _total = _extract_zip_safely(
                os.path.join(folder, rel), dest_full,
                max_files=ZIP_EXTRACT_MAX_FILES, max_total=ZIP_EXTRACT_MAX_BYTES)
        except ValueError as e:
            raise _UploadRefused(400, str(e))
        except zipfile.BadZipFile:
            raise _UploadRefused(400, "Not a valid zip file")
        except RuntimeError:
            raise _UploadRefused(400, "Encrypted zips are not supported")
        except Exception as e:
            raise _UploadRefused(500, f"Zip extraction failed: {e}")
        try:
            os.remove(os.path.join(folder, rel))
        except OSError:
            pass
        resp["extracted"] = {"folder": dest_rel, "files": count}
    logger.info(f"[CODING] Uploaded file: {rel} ({size} bytes) user={user.get('username','-')}")
    return resp


class CodingUploadInit(BaseModel):
    path: str
    size: int
    chunks: int


class CodingUploadComplete(BaseModel):
    upload_id: str


@app.post("/api/coding/file-upload")
async def coding_file_upload(request: Request, path: str = ""):
    """Upload a file into the open project (opencode-style drag-and-drop).
    The file travels as a raw octet-stream body and is streamed to disk with
    a hard 50 MB cap for regular users; admins are unlimited. Same
    containment rules as writes."""
    user, err = _require_auth(request)
    if err:
        await _drain_request_body(request)
        return err
    upload_limit = None if user.get("is_admin") else UPLOAD_LIMIT_BYTES
    user_id = _safe_username(user["username"])
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        await _drain_request_body(request)
        return access_err
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        cd = request.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cd)
        rel = (m.group(1) if m else "").strip().replace("\\", "/").lstrip("/")
    if _bad_path(rel) or not rel:
        await _drain_request_body(request)
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    ws_ck = _ws_enforce_lock(user, folder, rel)
    if ws_ck:
        await _drain_request_body(request)
        return ws_ck
    full = os.path.join(folder, rel)
    if not _is_within(folder, full):
        await _drain_request_body(request)
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    try:
        os.makedirs(os.path.dirname(full) or folder, exist_ok=True)
    except Exception:
        await _drain_request_body(request)
        return JSONResponse(status_code=500, content={"error": "Cannot create target folder"})
    tmp = full + ".upload-" + secrets.token_hex(4)
    size = 0
    try:
        with open(tmp, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if upload_limit is not None and size > upload_limit:
                    out.close()
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    await _drain_request_body(request)
                    return JSONResponse(status_code=413,
                                        content={"error": f"File too large (max {UPLOAD_LIMIT_MB:g}MB)"})
                out.write(chunk)
        os.replace(tmp, full)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return JSONResponse(status_code=500, content={"error": str(e)})
    try:
        _ws_log_change(folder, "upload", rel, user, user["id"], {"size": size})
        return _coding_after_store(user, user_id, folder, rel, size)
    except _UploadRefused as refused:
        return JSONResponse(status_code=refused.status, content={"error": refused.error})


@app.post("/api/coding/file-upload-init")
async def coding_file_upload_init(request: Request, req: CodingUploadInit):
    """Start a chunked upload session for a big file. Each chunk stays small
    (so proxies and timeouts never see a giant single body); the server
    reassembles on complete. Returns an upload_id for the chunk calls."""
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    folder = _get_coding_state(user_id).get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    rel = (req.path or "").strip().replace("\\", "/").lstrip("/")
    if _bad_path(rel) or not rel:
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    full = os.path.join(folder, rel)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    try:
        size = int(req.size)
        chunks = int(req.chunks)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid size/chunks"})
    if size <= 0 or chunks < 1 or chunks > UPLOAD_MAX_CHUNKS:
        return JSONResponse(status_code=400, content={"error": "Invalid size/chunks"})
    upload_limit = None if user.get("is_admin") else UPLOAD_LIMIT_BYTES
    if upload_limit is not None and size > upload_limit:
        return JSONResponse(status_code=413,
                            content={"error": f"File too large (max {UPLOAD_LIMIT_MB:g}MB)"})
    _prune_upload_sessions(user_id)
    upload_id = secrets.token_hex(16)
    sdir = os.path.join(_upload_session_root(user_id), upload_id)
    try:
        os.makedirs(sdir, exist_ok=False)
        _save_upload_meta(sdir, {"rel": rel, "size": size, "chunks": chunks,
                                 "received": 0, "received_bytes": 0,
                                 "created": time.time()})
    except Exception as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"upload_id": upload_id}


@app.post("/api/coding/file-upload-chunk")
async def coding_file_upload_chunk(request: Request, upload_id: str, index: int):
    """Accept one chunk (raw bytes) of a session started by file-upload-init.
    Chunks must arrive in order; out-of-order chunks get a 409 with the
    expected index so the client can resume cleanly."""
    user, err = _require_auth(request)
    if err:
        await _drain_request_body(request)
        return err
    user_id = _safe_username(user["username"])
    folder = _get_coding_state(user_id).get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        await _drain_request_body(request)
        return access_err
    sdir, meta = _load_upload_meta(user_id, upload_id or "")
    if meta is None:
        await _drain_request_body(request)
        return JSONResponse(status_code=404, content={"error": "Unknown or expired upload session"})
    full = os.path.join(folder, meta.get("rel", ""))
    if not _is_within(folder, full):
        await _drain_request_body(request)
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if index != meta.get("received", -1):
        await _drain_request_body(request)
        return JSONResponse(status_code=409, content={"error": "Out-of-order chunk",
                                                      "expected": meta.get("received", 0)})
    try:
        body = await request.body()
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Could not read chunk: {e}"})
    if len(body) > UPLOAD_CHUNK_MAX_BYTES:
        await _drain_request_body(request)
        return JSONResponse(status_code=413, content={"error": "Chunk too large"})
    if not body:
        return JSONResponse(status_code=400, content={"error": "Empty chunk"})
    upload_limit = None if user.get("is_admin") else UPLOAD_LIMIT_BYTES
    if upload_limit is not None and meta.get("received_bytes", 0) + len(body) > upload_limit:
        shutil.rmtree(sdir, ignore_errors=True)
        return JSONResponse(status_code=413,
                            content={"error": f"File too large (max {UPLOAD_LIMIT_MB:g}MB)"})
    try:
        with open(os.path.join(sdir, "data.part"), "ab") as out:
            out.write(body)
        meta["received"] = meta.get("received", 0) + 1
        meta["received_bytes"] = meta.get("received_bytes", 0) + len(body)
        _save_upload_meta(sdir, meta)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"received": meta["received"], "chunks": meta.get("chunks")}


@app.post("/api/coding/file-upload-complete")
async def coding_file_upload_complete(request: Request, req: CodingUploadComplete):
    """Finish a chunked upload: verify every chunk landed, move the assembled
    file into the project (with zip auto-extract), and clean up the session."""
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    folder = _get_coding_state(user_id).get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    sdir, meta = _load_upload_meta(user_id, req.upload_id or "")
    if meta is None:
        return JSONResponse(status_code=404, content={"error": "Unknown or expired upload session"})
    rel = meta.get("rel", "")
    ws_ck = _ws_enforce_lock(user, folder, rel)
    if ws_ck:
        shutil.rmtree(sdir, ignore_errors=True)
        return ws_ck
    full = os.path.join(folder, rel)
    if _bad_path(rel) or not rel or not _is_within(folder, full):
        shutil.rmtree(sdir, ignore_errors=True)
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    if meta.get("received", 0) != meta.get("chunks", -1):
        return JSONResponse(status_code=400, content={"error": "Upload incomplete",
                                                      "received": meta.get("received", 0),
                                                      "chunks": meta.get("chunks", 0)})
    part = os.path.join(sdir, "data.part")
    try:
        size = os.path.getsize(part)
    except OSError:
        shutil.rmtree(sdir, ignore_errors=True)
        return JSONResponse(status_code=400, content={"error": "Upload data missing"})
    try:
        os.makedirs(os.path.dirname(full) or folder, exist_ok=True)
        os.replace(part, full)
    except Exception as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
    shutil.rmtree(sdir, ignore_errors=True)
    _ws_log_change(folder, "upload", rel, user, user["id"], {"size": size})
    try:
        return _coding_after_store(user, user_id, folder, rel, size)
    except _UploadRefused as refused:
        return JSONResponse(status_code=refused.status, content={"error": refused.error})


@app.delete("/api/coding/file-upload")
async def coding_file_upload_abort(request: Request, upload_id: str):
    """Cancel a chunked upload: drop its buffered chunks."""
    user, err = _require_auth(request)
    if err:
        return err
    sdir = _upload_session_dir(_safe_username(user["username"]), upload_id or "")
    if sdir is None:
        return JSONResponse(status_code=400, content={"error": "Invalid upload id"})
    shutil.rmtree(sdir, ignore_errors=True)
    return {"status": "ok"}


@app.post("/api/coding/file-write")
async def coding_file_write(request: Request, req: CodingFileWrite):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    blk = _shared_write_block(user, folder)
    if blk:
        return blk
    if _bad_path(req.path) or len(req.content) > 5 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "Invalid path or content too large (max 5MB)"})
    ws_ck = _ws_enforce_lock(user, folder, req.path)
    if ws_ck:
        return ws_ck
    quota_err = _check_quota(user, _safe_username(user["username"]), len(req.content.encode("utf-8", "ignore")))
    if quota_err:
        return quota_err
    full = os.path.join(folder, req.path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        backup = None
        vts = None
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                backup = f.read()
            if backup == req.content:
                # Byte-identical rewrite: report it as a no-op instead of a
                # write, so the loop cannot mistake it for progress.
                return {"status": "ok", "path": req.path, "size": len(req.content),
                        "backed_up": True, "added": 0, "removed": 0, "noop": True}
            vts = _coding_save_version(_safe_username(user["username"]), req.path, backup, folder)
        with open(full, "w", encoding="utf-8") as f:
            f.write(req.content)

        # Diff stats for the activity feed (opencode-style +N -M)
        try:
            import difflib
            old_lines = backup.splitlines() if backup else []
            new_lines = req.content.splitlines()
            dl = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
            added = sum(1 for l in dl if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in dl if l.startswith("-") and not l.startswith("---"))
        except Exception:
            added, removed = len(req.content.splitlines()), 0
        _action_log(_safe_username(user["username"]), req.path,
                    "write" if vts is not None else "create", vts, added, removed)
        _ws_log_change(folder, "write" if vts is not None else "create",
                       req.path, user, user["id"], {"added": added, "removed": removed})
        logger.info(f"[CODING] Wrote file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(req.content),
                "backed_up": backup is not None, "added": added, "removed": removed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---- FILE_PATCH target location ----------------------------------------
# "Old text not found" is the most common coding failure, and the matcher used
# to exist in THREE copies with different forgiveness levels: this endpoint
# retried with trailing whitespace ignored, while the browser-local and
# paired-device paths in index.html did a bare indexOf and gave up. The same
# patch therefore applied on a server project but failed on a local one. The
# tiers below are duplicated verbatim in web/index.html (locatePatchTarget) so
# every backend behaves identically. Change one, change both.
#
# Tiers, strictest first; a tolerant tier must match EXACTLY ONCE:
#   1 exact   2 CRLF/LF   3 trailing spaces   4 indentation   5 blank lines
PATCH_KEY_EXACT = lambda l: l
PATCH_KEY_RTRIM = lambda l: l.rstrip()
PATCH_KEY_INDENT = lambda l: l.lstrip().rstrip()


def _patch_unique_match(cur_lines, want_keys, key_fn):
    """Index of the only line where the block matches, else None."""
    hits = []
    n = len(want_keys)
    if not n:
        return None
    for s in range(0, len(cur_lines) - n + 1):
        if all(key_fn(cur_lines[s + k]) == want_keys[k] for k in range(n)):
            hits.append(s)
            if len(hits) > 1:
                return None
    return hits[0] if len(hits) == 1 else None


def _patch_line_offsets(lines):
    off = [0]
    for ln in lines:
        off.append(off[-1] + len(ln) + 1)
    return off


def _patch_trim(text):
    return text.lstrip("\ufeff")


def _locate_patch_target(current, old_text):
    """Return (start, end, how) char offsets into current, or None."""
    cur = _patch_trim(current)
    old = _patch_trim(old_text)
    if not old:
        return None
    idx = cur.find(old)
    if idx != -1:
        return (idx, idx + len(old), "exact")
    cur_lines = cur.replace("\r\n", "\n").split("\n")
    old_lines = old.replace("\r\n", "\n").split("\n")
    off = _patch_line_offsets(cur_lines)
    for how, key_fn in (("line-endings", PATCH_KEY_EXACT),
                        ("trailing-spaces", PATCH_KEY_RTRIM),
                        ("indentation", PATCH_KEY_INDENT)):
        keys = [key_fn(l) for l in old_lines]
        s = _patch_unique_match(cur_lines, keys, key_fn)
        if s is not None:
            return (off[s], off[s + len(old_lines)], how)
    # The model wrapped the block in blank lines it invented.
    a, b = 0, len(old_lines)
    while a < b and old_lines[a].strip() == "":
        a += 1
    while b > a and old_lines[b - 1].strip() == "":
        b -= 1
    if a > 0 or b < len(old_lines):
        inner = old_lines[a:b]
        keys = [PATCH_KEY_INDENT(l) for l in inner]
        s = _patch_unique_match(cur_lines, keys, PATCH_KEY_INDENT) if keys else None
        if s is not None:
            return (off[s], off[s + len(inner)], "blank-lines")
    return None


def _patch_miss_reason(current, old_text):
    """Say WHY the OLD block missed, so the model does not resend it verbatim."""
    cur = _patch_trim(current).replace("\r\n", "\n")
    old = _patch_trim(old_text).replace("\r\n", "\n")
    first = ""
    for ln in old.split("\n"):
        if ln.strip():
            first = ln.strip()
            break
    if "\r" in old_text or "\r" in _patch_trim(current):
        lead = "its block mixes line endings"
    else:
        lead = "its block matches no run of lines in the file"
    if first and first not in cur:
        return lead + ", and its first line (%r) is not in the file at all" % first[:60]
    return lead + ", and its first line was found but the surrounding lines do not match verbatim"


@app.post("/api/coding/file-patch")
async def coding_file_patch(request: Request, req: CodingFilePatch):
    """Surgical search-replace edit: replaces the FIRST occurrence of
    old_text with new_text. Same auth/containment/quota/size rules as
    file-write, so fixes land without rewriting whole files."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    blk = _shared_write_block(user, folder)
    if blk:
        return blk
    if (_bad_path(req.path) or len(req.old_text) > 5 * 1024 * 1024
            or len(req.new_text) > 5 * 1024 * 1024):
        return JSONResponse(status_code=400, content={"error": "Invalid path or text too large (max 5MB)"})
    ws_ck = _ws_enforce_lock(user, folder, req.path)
    if ws_ck:
        return ws_ck
    full = os.path.join(folder, req.path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        if os.path.getsize(full) > 5 * 1024 * 1024:
            return JSONResponse(status_code=400, content={"error": "File too large (max 5MB)"})
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()
        old, new = req.old_text, req.new_text
        hit = _locate_patch_target(current, old)
        if hit is None:
            return JSONResponse(status_code=404, content={
                "error": "Old text not found — nothing was changed. "
                         + _patch_miss_reason(current, old),
                "hint": "Re-read the file first, then copy OLD verbatim from that "
                        "output (same indentation, same wording, no added blank lines). "
                        "Keep OLD to 3-8 consecutive lines that appear exactly once in "
                        "the file.\nCurrent file starts with:\n" + current[:200]})
        patched = current[:hit[0]] + new + current[hit[1]:]
        # An OLD/NEW pair that reproduces the current text is a no-op. Writing
        # it is harmless but reporting "ok" let the coding loop treat a stall as
        # progress, so say so explicitly and skip the write entirely.
        if patched == current:
            return {"status": "ok", "noop": True, "path": req.path,
                    "size": len(current), "added": 0, "removed": 0,
                    "how": hit[2]}
        quota_err = _check_quota(user, _safe_username(user["username"]), max(0, len(patched) - len(current)))
        if quota_err:
            return quota_err
        vts = _coding_save_version(_safe_username(user["username"]), req.path, current, folder)
        with open(full, "w", encoding="utf-8") as f:
            f.write(patched)
        try:
            import difflib
            dl = list(difflib.unified_diff(current.splitlines(), patched.splitlines(), lineterm=""))
            added = sum(1 for l in dl if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in dl if l.startswith("-") and not l.startswith("---"))
        except Exception:
            added, removed = 0, 0
        _action_log(_safe_username(user["username"]), req.path, "patch", vts, added, removed)
        _ws_log_change(folder, "patch", req.path, user, user["id"],
                       {"added": added, "removed": removed})
        logger.info(f"[CODING] Patched file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(patched),
                "added": added, "removed": removed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _coding_version_checks(user, folder, path, write=False):
    """Shared guard for the version endpoints: auth, open folder, path safety."""
    access_err = _folder_access_error(user, folder, write=write)
    if access_err:
        return access_err
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    full = os.path.join(folder, path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    return None


@app.get("/api/coding/file-versions")
async def coding_file_versions(request: Request, path: str):
    """List saved versions of a project file (newest first)."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    guard = _coding_version_checks(user, coding_state.get("folder"), path)
    if guard:
        return guard
    return {"path": path,
            "versions": _coding_list_versions(_safe_username(user["username"]), path,
                                              coding_state.get("folder"))}


@app.get("/api/coding/file-version-content")
async def coding_file_version_content(request: Request, path: str, ts: float):
    """Fetch the content of one saved version."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    guard = _coding_version_checks(user, coding_state.get("folder"), path)
    if guard:
        return guard
    content = _coding_get_version(_safe_username(user["username"]), path, ts,
                                 coding_state.get("folder"))
    if content is None:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    return {"path": path, "ts": ts, "content": content}


@app.post("/api/coding/file-restore")
async def coding_file_restore(request: Request, req: CodingFileRestore):
    """Restore a file to a saved version. The current content is snapshotted
    first, so restoring is itself undoable."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    guard = _coding_version_checks(user, folder, req.path, write=True)
    if guard:
        return guard
    full = os.path.join(folder, req.path)
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    content = _coding_get_version(_safe_username(user["username"]), req.path, req.ts, folder)
    if content is None:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()
        if current != content:
            _coding_save_version(_safe_username(user["username"]), req.path, current, folder)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[CODING] Restored file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(content)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _project_versions_folder(user, folder, write=False):
    """None when versioning is available; else a JSONResponse explaining why not."""
    if _folder_access_error(user, folder, write=write):
        return JSONResponse(status_code=400, content={
            "error": "Open a cloud project to use project versions."})
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No project is open."})
    return None


@app.get("/api/coding/project-versions")
async def coding_project_versions(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    if folder and os.path.isdir(folder) and not _folder_access_error(user, folder):
        return {"available": True, "keep": _project_versions_keep(uid),
                "max": _PROJECT_VERSION_MAX_KEEP,
                "versions": _project_version_list(uid, folder)}
    return {"available": False, "keep": _project_versions_keep(uid),
            "max": _PROJECT_VERSION_MAX_KEEP, "versions": [],
            "reason": "Versions are available for Omniilink cloud projects."}


@app.post("/api/coding/project-versions")
async def coding_project_version_create(request: Request, req: ProjectVersionCreate):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    bad = _project_versions_folder(user, folder, write=True)
    if bad:
        return bad
    try:
        rec, size = _project_version_save(uid, folder, req.short, req.paragraph, req.chat)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    if rec is None:
        latest = _project_version_list(uid, folder)
        return {"status": "ok", "skipped": True,
                "id": latest[0]["id"] if latest else None,
                "short": latest[0].get("short", "") if latest else ""}
    qerr = _check_quota(user, uid, size)
    if qerr:
        try:
            os.remove(os.path.join(_project_versions_dir(uid, folder), rec["id"] + ".json.gz"))
        except OSError:
            pass
        return JSONResponse(status_code=413, content={
            "error": "Not enough cloud storage for another version. Delete old versions or free space."})
    _project_version_prune(uid, folder, _project_versions_keep(uid))
    logger.info(f"[VERSIONS] Saved version {rec['id']} ({size} bytes) project={rec['project']}")
    return {"status": "ok", "id": rec["id"], "short": rec["short"],
            "files": len(rec["files"]), "added": rec["added"], "removed": rec["removed"]}


@app.get("/api/coding/project-versions/detail")
async def coding_project_version_detail(request: Request, id: str):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    bad = _project_versions_folder(user, folder)
    if bad:
        return bad
    rec = _project_version_load(uid, folder, id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    return {"id": rec.get("id"), "ts": rec.get("ts"), "short": rec.get("short"),
            "paragraph": rec.get("paragraph"), "project": rec.get("project"),
            "added": rec.get("added", 0), "removed": rec.get("removed", 0),
            "stats": rec.get("stats", []), "files": len(rec.get("files", {})),
            "chat_count": len(rec.get("chat", []) or [])}


@app.get("/api/coding/project-versions/diff")
async def coding_project_version_diff(request: Request, id: str, path: str = ""):
    """Unified diff between a saved version and the current project. With
    `path`, one file; without, every changed file (capped). This is the
    review half of 'review the AI's change before keeping it'."""
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    bad = _project_versions_folder(user, folder)
    if bad:
        return bad
    rec = _project_version_load(uid, folder, id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    ver_files = rec.get("files", {}) or {}
    cur = _project_snapshot(folder)
    if path:
        if path not in ver_files and path not in cur:
            return JSONResponse(status_code=404, content={"error": "File not in version or project"})
        return {"id": id, "path": path,
                "diff": _unified_diff(ver_files.get(path, ""), cur.get(path, ""), path)}
    out = []
    for p in sorted(set(ver_files) | set(cur)):
        a, b = ver_files.get(p, ""), cur.get(p, "")
        if a == b:
            continue
        out.append({"path": p, "diff": _unified_diff(a, b, p)})
        if len(out) >= 40:
            break
    return {"id": id, "files": out}


@app.delete("/api/coding/project-versions")
async def coding_project_version_delete(request: Request, id: str):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    bad = _project_versions_folder(user, folder, write=True)
    if bad:
        return bad
    p = os.path.join(_project_versions_dir(uid, folder),
                     os.path.basename(id or "") + ".json.gz")
    if not os.path.isfile(p):
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    try:
        os.remove(p)
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"status": "ok"}


@app.post("/api/coding/project-versions/restore")
async def coding_project_version_restore(request: Request, req: ProjectVersionId):
    """Revert the project files AND the coding chat to a saved version. The
    current state is snapshotted first, so restoring is itself undoable."""
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    bad = _project_versions_folder(user, folder, write=True)
    if bad:
        return bad
    rec = _project_version_load(uid, folder, req.id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    try:
        _project_version_save(uid, folder, "Before restore",
                              "Automatic snapshot taken just before restoring an earlier version.", [])
    except Exception:
        pass
    want = rec.get("files", {}) or {}
    now = _project_snapshot(folder)
    written = deleted = 0
    for p, content in want.items():
        if _bad_path(p):
            continue
        full = os.path.join(folder, p)
        if not _is_within(folder, full):
            continue
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            written += 1
        except Exception:
            pass
    for p in now:
        if p in want or _bad_path(p):
            continue
        full = os.path.join(folder, p)
        if not _is_within(folder, full):
            continue
        try:
            os.remove(full)
            deleted += 1
        except OSError:
            pass
    chat = rec.get("chat") or []
    if chat:
        try:
            cpath = os.path.join(folder, CODING_CHAT_DIR, CODING_CHAT_FILE)
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            tmp = cpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"updated": time.time(), "history": chat}, f)
            os.replace(tmp, cpath)
        except Exception:
            pass
    _project_version_prune(uid, folder, _project_versions_keep(uid))
    logger.info(f"[VERSIONS] Restored {req.id}: {written} written, {deleted} deleted")
    return {"status": "ok", "written": written, "deleted": deleted, "chat": chat}


@app.delete("/api/coding/file-delete")
async def coding_file_delete(request: Request, path: str):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    blk = _shared_write_block(user, folder)
    if blk:
        return blk
    if _bad_path(path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    ws_ck = _ws_enforce_lock(user, folder, path)
    if ws_ck:
        return ws_ck
    full = os.path.join(folder, path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        owner = _safe_username(user["username"])
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                vts = _coding_save_version(owner, path, f.read(), folder)
        except Exception:
            vts = None
        os.remove(full)
        _action_log(owner, path, "delete", vts)
        _ws_log_change(folder, "delete", path, user, user["id"])
        logger.info(f"[CODING] Deleted file: {path} user={user.get('username','-')}")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/coding/rename")
async def coding_rename(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    old_path = (body.get("old_path") or "").strip()
    new_path = (body.get("new_path") or "").strip()
    if not old_path or not new_path:
        return JSONResponse(status_code=400, content={"error": "old_path and new_path required"})
    if _bad_path(old_path) or _bad_path(new_path):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    ws_ck = _ws_enforce_lock(user, folder, old_path)
    if ws_ck:
        return ws_ck
    full_old = os.path.join(folder, old_path)
    full_new = os.path.join(folder, new_path)
    if not _is_within(folder, full_old) or not _is_within(folder, full_new):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    if not os.path.exists(full_old):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    if os.path.exists(full_new):
        return JSONResponse(status_code=409, content={"error": "Target already exists"})
    try:
        os.rename(full_old, full_new)
        _ws_log_change(folder, "rename", old_path, user, user["id"], {"to": new_path})
        logger.info(f"[CODING] Renamed: {old_path} -> {new_path} user={user.get('username','-')}")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})



# ==========================================

# More Tools: YouTube download + file conversion

# ==========================================

# Everything runs on this server (your own machine) — no third-party

# converter sites. Requires the yt-dlp + pypdfium2 + imageio-ffmpeg packages

# in requirements.txt (ffmpeg binary via imageio-ffmpeg when missing).
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be", "www.youtu.be",
            "youtube-nocookie.com", "www.youtube-nocookie.com"}
YT_MAX_DURATION = 3 * 3600          # refuse videos longer than 3h
YT_TIMEOUT_INFO = 60
YT_TIMEOUT_DOWNLOAD = 900
TOOLS_MAX_PAGES = 200               # PDF page cap for conversion


def _tools_ytdlp_base():
    """Argv prefix that invokes yt-dlp, or None when unavailable."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def _tools_ffmpeg_bin():
    """ffmpeg path: system binary first, else the bundled imageio-ffmpeg one."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def _tools_check_youtube_url(url: str):
    """Return (clean_url, None) or (None, error). Only YouTube hosts allowed."""
    u = (url or "").strip()
    if not u or len(u) > 2048:
        return None, "Paste a YouTube link"
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        host = urllib.parse.urlparse(u).hostname or ""
    except Exception:
        return None, "That link is not valid"
    if host.lower() not in YT_HOSTS:
        return None, "Only youtube.com / youtu.be links are supported"
    return u, None


def _tools_safe_filename(name: str, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 _.\-]+", "_", (name or "download").strip())[:80].strip(" .") or "download"
    return f"{base}.{ext}"


class YTInfoRequest(BaseModel):
    url: str


class YTDownloadRequest(BaseModel):
    url: str
    format: str = "mp4"       # legacy: mp3 (audio) | mp4 (video)
    kind: str = ""            # video | audio (new UI; wins over format)
    container: str = ""       # mp4 | mkv | webm
    codec: str = ""           # h264 | h265 | av1 | vp9
    compress: str = ""        # original | small | tiny
    fps: str = ""             # any | 30 | 60
    height: str = ""          # "" (best) or a detected height
    streams: str = ""         # combine | split
    subs: str = ""            # none | en
    audio_format: str = ""    # original | mp3 | m4a | opus | wav | flac | ogg
    audio_bitrate: str = ""   # 64 96 128 192 256 320
    cover: bool = True        # embed the thumbnail for audio


@app.get("/api/tools/converter/health")
async def tools_converter_health(request: Request):
    """Can THIS server reach the configured converter? Distinguishes a dead
    container/route (fix on the server) from a browser-side block (CORS,
    ad-blocker, VPN). Any HTTP reply counts as alive — even a 404."""
    user, err = _require_auth(request)
    if err:
        return err
    url = _converter_url(
        (os.environ.get("OMNIILINK_CONVERTER_API", "") or "").strip(),
        _converter_url_from_dotenv())
    if not url:
        return {"ok": False, "reason": "not-configured"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Omniilink/1.0"})
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        try:
            resp.read(1)
        except Exception:
            pass
        return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"ok": True, "status": e.code, "note": "route live"}
    except Exception as e:
        return {"ok": False, "reason": "unreachable", "detail": str(e)[:120]}


def _converter_verdict(url, host, dns_ok, cobalt, http_ok, http_status, internal):
    """Pure verdict picker for the diagnostics panel (unit-tested). cobalt is
    None (docker unavailable), or a dict with present/running/status.
    internal is None (not probed), True (cobalt answers over docker DNS) or
    False (container addressable but silent)."""
    if not url:
        return "not-configured: set OMNIILINK_CONVERTER_API (or rely on the built-in default) and restart."
    if cobalt is not None:
        if not cobalt.get("present"):
            return "cobalt-missing: no cobalt container exists — run update.bat (docker compose up -d) on the server."
        if not cobalt.get("running"):
            return ("cobalt-down: container exists but is not Up"
                    + (f" ({cobalt.get('status')})" if cobalt.get("status") else "")
                    + " — check docker logs cobalt on the server.")
        if internal is False:
            return ("cobalt-unhealthy: container is Up but does not answer on :9000 "
                    "— check docker logs cobalt on the server.")
    if not dns_ok:
        return (f"dns-fail: {host or 'the converter hostname'} does not resolve from this server "
                "— whitelist *.cfargotunnel.com in the network DNS filter, or try mobile data on the phone. "
                "The container itself may be fine.")
    if not http_ok:
        return "route-down: DNS works but nothing answers — the tunnel route is missing/stale (update.bat restarts it) or the container is unreachable."
    if http_status in (502, 503, 504):
        return "bad-gateway: the tunnel answers but the converter does not — container crashing or still starting (docker logs cobalt)."
    return "healthy: the server reaches the converter — a browser failure is client-side (CORS, ad-blocker, VPN, filtered DNS)."


@app.get("/api/tools/converter/diagnostics")
async def tools_converter_diagnostics(request: Request):
    """Admin-only ground truth about the converter chain: configured URL, DNS
    from this server, cobalt/tunnel container state via docker, plus a live
    HTTP probe. The page shows the verdict when a download fails."""
    user, err = _require_superadmin(request)
    if err:
        return err
    url = _converter_url(
        (os.environ.get("OMNIILINK_CONVERTER_API", "") or "").strip(),
        _converter_url_from_dotenv())
    host = ""
    try:
        host = (urllib.parse.urlparse(url).hostname or "") if url else ""
    except Exception:
        host = ""
    dns_ok, addrs = False, []
    if host:
        try:
            infos = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, 443), timeout=8)
            seen = []
            for fam, _typ, _proto, _canon, sockaddr in infos:
                ip = sockaddr[0]
                if ip not in seen:
                    seen.append(ip)
            addrs = seen[:6]
            dns_ok = bool(addrs)
        except Exception:
            dns_ok, addrs = False, []
    cobalt = None
    tunnel = None
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            proc = await asyncio.to_thread(
                subprocess.run, [docker_bin, "ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"],
                capture_output=True, text=True, timeout=12)
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    name, status, image = parts[0], parts[1], parts[2]
                    entry = {"present": True, "running": status.strip().lower().startswith("up"),
                             "status": status.strip()[:80]}
                    low = (name + " " + image).lower()
                    if "cobalt" in low and cobalt is None:
                        cobalt = entry
                    if ("cloudflared" in low or "tunnel" in name.lower()) and tunnel is None:
                        tunnel = entry
        except Exception:
            pass
    http_ok, http_status, http_detail = False, None, ""
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Omniilink/1.0"})
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
            try:
                resp.read(1)
            except Exception:
                pass
            http_ok, http_status = True, resp.status
        except urllib.error.HTTPError as e:
            http_ok, http_status, http_detail = True, e.code, "route live"
        except Exception as e:
            http_ok, http_detail = False, str(e)[:120]
    internal = None
    if cobalt is not None and cobalt.get("present") and cobalt.get("running"):

        # Same docker network, no public DNS involved: proves whether the

        # container itself is healthy regardless of tunnel/DNS state.
        try:
            req_in = urllib.request.Request("http://cobalt:9000/",
                                            headers={"User-Agent": "Omniilink/1.0"})
            resp_in = await asyncio.to_thread(urllib.request.urlopen, req_in, timeout=8)
            try:
                resp_in.read(1)
            except Exception:
                pass
            internal = True
        except Exception:
            internal = False
    verdict = _converter_verdict(url, host, dns_ok, cobalt, http_ok, http_status, internal)
    return {"url": url, "dns": {"ok": dns_ok, "addrs": addrs},
            "docker": {"available": docker_bin is not None,
                       "cobalt": cobalt, "tunnel": tunnel},
            "http": {"ok": http_ok, "status": http_status, "detail": http_detail},
            "internal": {"probed": internal is not None, "ok": internal is True},
            "verdict": verdict}


@app.post("/api/tools/youtube/info")
async def tools_youtube_info(request: Request, req: YTInfoRequest):
    """Video metadata without downloading: title, duration, thumbnail."""
    user, err = _require_auth(request)
    if err:
        return err
    url, uerr = _tools_check_youtube_url(req.url)
    if uerr:
        return JSONResponse(status_code=400, content={"error": uerr})
    ytdlp = _tools_ytdlp_base()
    if not ytdlp:
        return JSONResponse(status_code=503, content={
            "error": "YouTube support is not installed on this server (needs the yt-dlp package)"})
    try:
        proc = await asyncio.to_thread(
            subprocess.run, ytdlp + ["--dump-json", "--no-playlist",
                                     "--no-warnings", url],
            capture_output=True, text=True, timeout=YT_TIMEOUT_INFO)
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=504, content={"error": "YouTube took too long to respond"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip().splitlines()
        detail = msg[-1] if msg else "Could not read that video"
        if len(detail) > 200:
            detail = detail[:200]
        return JSONResponse(status_code=400, content={"error": detail})
    try:
        info = json.loads(proc.stdout)
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Could not parse video info"})
    try:
        duration = int(info.get("duration") or 0)
    except Exception:
        duration = 0
    quals = {}
    for f in info.get("formats") or []:
        try:
            h = int(f.get("height") or 0)
        except Exception:
            continue
        if h <= 0 or (f.get("vcodec") or "none") == "none":
            continue
        if (f.get("ext") or "") != "mp4":
            continue
        prog = (f.get("acodec") or "none") != "none"
        if h not in quals or (prog and not quals[h]):
            quals[h] = prog
    qualities = [{"height": h, "progressive": quals[h]}
                 for h in sorted(quals, reverse=True)[:8]]
    return {"title": info.get("title") or "YouTube video",
            "duration": duration,
            "thumbnail": info.get("thumbnail") or "",
            "uploader": info.get("uploader") or "",
            "ok_duration": duration <= YT_MAX_DURATION,
            "qualities": qualities}



# Default converter address: our own cobalt instance behind the nginx

# sidecar on the main hostname. Same-origin for the page (no CORS issues)

# and reachable over plain IPv4 (the tunnel's own *.cfargotunnel.com name

# has no IPv4 record, so it is unusable from phones and IPv4-only nets).
CONVERTER_DEFAULT_URL = "https://omniilink.dpdns.org/converter/"


def _converter_url_from_dotenv() -> str:
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(os.path.join(APP_DIR, ".env"))
        return ((vals or {}).get("OMNIILINK_CONVERTER_API", "") or "").strip()
    except Exception:
        return ""


def _converter_url(env_val: str, file_val: str) -> str:
    return (env_val or "").strip() or (file_val or "").strip() or CONVERTER_DEFAULT_URL


@app.get("/api/tools/converter")
async def tools_converter(request: Request):
    """Browser-side converter API base URL. Explicit env wins, then the
    mounted host .env, then the built-in default (our own instance on the
    committed tunnel route) — so the browser flow works with zero setup.
    Set OMNIILINK_CONVERTER_API to override."""
    user, err = _require_auth(request)
    if err:
        return err
    url = _converter_url(
        (os.environ.get("OMNIILINK_CONVERTER_API", "") or "").strip(),
        _converter_url_from_dotenv())
    return {"url": url}



# ---- YouTube download options (pure helpers, unit-tested) ----------------

# Source-codec prefixes YouTube reports, and the ffmpeg encoder used when we

# have to re-encode instead of copying the stream.
YT_VCODEC_SRC = {"h264": "avc1", "vp9": "vp09", "av1": "av01", "h265": "hev1"}
YT_VCODEC_ENC = {"h264": "libx264", "h265": "libx265",
                 "av1": "libaom-av1", "vp9": "libvpx-vp9"}
YT_CONTAINER_EXT = {"mp4": "mp4", "mkv": "mkv", "webm": "webm"}
YT_AUDIO_ENC = {"mp3": ("libmp3lame", "mp3"), "m4a": ("aac", "m4a"),
                "opus": ("libopus", "opus"), "ogg": ("libvorbis", "ogg"),
                "wav": ("pcm_s16le", "wav"), "flac": ("flac", "flac")}
YT_COMPRESS_CRF = {"small": "26", "tiny": "31"}
YT_AUDIO_BITRATES = ("64", "96", "128", "192", "256", "320")
YT_MIME = {"mp4": "video/mp4", "mkv": "video/x-matroska", "webm": "video/webm",
           "mp3": "audio/mpeg", "m4a": "audio/mp4", "opus": "audio/ogg",
           "ogg": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
           "zip": "application/zip"}

# Lossy formats where a bitrate actually applies (the rest are already fixed).
YT_LOSSLESS_AUDIO = ("original", "wav", "flac")


def _yt_compat(container: str, codec: str) -> tuple:
    """Coerce container + codec into a pair that actually muxes: WebM cannot
    hold H.264/H.265 (and its audio would be Opus), while VP9+Opus belongs in
    WebM rather than MP4. Keeps whatever the user picked when it is legal."""
    container = container if container in YT_CONTAINER_EXT else "mp4"
    codec = codec if codec in YT_VCODEC_SRC else "h264"
    if container == "webm" and codec not in ("vp9", "av1"):
        codec = "vp9"
    elif codec == "vp9" and container == "mp4":
        container = "webm"
    return container, codec


def _yt_format_sel(codec: str, height, fps, split: bool) -> str:
    """yt-dlp format selector: prefer the requested codec at or below the
    requested resolution/frame rate, then fall back to any codec so the
    download still succeeds."""
    h = f"[height<=?{int(height)}]" if height else ""
    f = f"[fps<=?{int(fps)}]" if str(fps) not in ("", "any", "None") else ""
    src = YT_VCODEC_SRC.get(codec)
    vcond = f"[vcodec^={src}]" if src else ""
    if split:
        return f"bv*{h}{f}{vcond}/bv*{h}{f}"
    return f"bv*{h}{f}{vcond}+ba/bv*{h}{f}+ba/b{h}{f}"


def _yt_plan(req: YTDownloadRequest) -> tuple:
    """Validate the requested options and turn them into an execution plan.
    Returns (plan, "") or (None, error message)."""
    kind = (req.kind or "").strip().lower()
    legacy = not kind
    if not kind:
        kind = "audio" if (req.format or "").strip().lower() == "mp3" else "video"
    if kind not in ("video", "audio"):
        return None, "Type must be video or audio"
    if kind == "audio":
        a_format = (req.audio_format or "").strip().lower()
        if not a_format:

            # Old clients only send format=mp3, so keep honouring it as mp3.
            a_format = "mp3" if legacy else "original"
        if a_format == "aac":
            a_format = "m4a"
        if a_format != "original" and a_format not in YT_AUDIO_ENC:
            return None, "Audio file type must be one of: original, " + ", ".join(YT_AUDIO_ENC)
        br = str(req.audio_bitrate or "128").strip()
        if br not in YT_AUDIO_BITRATES:
            br = "128"
        return {"kind": "audio", "a_format": a_format, "a_bitrate": br,
                "cover": bool(req.cover), "lossless": a_format in YT_LOSSLESS_AUDIO,
                "format_sel": "ba/best",
                "ext": "" if a_format == "original" else YT_AUDIO_ENC[a_format][1],
                "enc": "" if a_format == "original" else YT_AUDIO_ENC[a_format][0]}, ""
    raw_container = (req.container or "mp4").strip().lower()
    if raw_container not in YT_CONTAINER_EXT:
        return None, "File type must be mp4, mkv or webm"
    raw_codec = (req.codec or "h264").strip().lower()
    if raw_codec not in YT_VCODEC_SRC:
        return None, "Codec must be h264, h265, av1 or vp9"
    container, codec = _yt_compat(raw_container, raw_codec)
    compress = (req.compress or "original").strip().lower()
    if compress != "original" and compress not in YT_COMPRESS_CRF:
        return None, "Compression must be original, small or tiny"
    fps = str(req.fps or "any").strip().lower()
    if fps not in ("any", "30", "60"):
        return None, "Frame rate must be any, 30 or 60"
    try:
        height = int(req.height or 0)
    except (TypeError, ValueError):
        height = 0
    if height and not (144 <= height <= 4320):
        height = 0
    streams = (req.streams or "combine").strip().lower()
    if streams not in ("combine", "split"):
        return None, "Streams must be combine or split"
    subs = (req.subs or "none").strip().lower()
    if subs not in ("none", "en"):
        return None, "Subtitles must be none or en"
    return {"kind": "video", "container": container, "codec": codec,
            "compress": compress, "fps": fps, "height": height,
            "split": streams == "split", "subs": subs, "ext": container,
            "format_sel": _yt_format_sel(codec, height, fps, streams == "split")}, ""


_YT_ENCODERS_RAW = None


def _yt_have_encoder(name: str) -> bool:
    """True when this ffmpeg build can encode with `name` (cached)."""
    global _YT_ENCODERS_RAW
    if _YT_ENCODERS_RAW is None:
        ff = _tools_ffmpeg_bin()
        raw = ""
        if ff:
            try:
                raw = subprocess.run([ff, "-hide_banner", "-encoders"],
                                     capture_output=True, text=True, timeout=30).stdout or ""
            except Exception:
                raw = ""
        _YT_ENCODERS_RAW = raw
    return bool(re.search(r"\s" + re.escape(name) + r"\s", _YT_ENCODERS_RAW))


def _yt_video_ffmpeg(ff: str, plan: dict, src: str, dst: str) -> list:
    """ffmpeg argv that rewrites `src` into the requested container, applying
    the requested compression. Copy when possible; re-encode only when asked."""
    args = [ff, "-y", "-v", "error", "-i", src, "-map", "0"]
    if plan["compress"] == "original":
        args += ["-c", "copy"]
    else:
        enc = YT_VCODEC_ENC.get(plan["codec"], "libx264")
        if not _yt_have_encoder(enc):
            raise ValueError(f"this server's ffmpeg cannot encode {plan['codec'].upper()} — pick a different codec")
        args += ["-c:v", enc, "-crf", YT_COMPRESS_CRF[plan["compress"]]]
        if enc in ("libvpx-vp9", "libaom-av1"):
            args += ["-b:v", "0"]
        else:
            args += ["-preset", "medium"]
        if plan["container"] == "mp4":
            args += ["-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-c:a", "copy"]
    if plan["container"] == "mp4":
        args += ["-movflags", "+faststart"]
    return args + [dst]


def _yt_cover_map(cover: str) -> list:
    """Map + encoder for an embedded cover image. Tags only accept JPEG/PNG,
    so anything else (YouTube hands out .webp) gets re-encoded first."""
    if cover.lower().endswith((".jpg", ".jpeg", ".png")):
        return ["-map", "1:v", "-c:v", "copy"]
    return ["-map", "1:v", "-c:v", "mjpeg"]


def _yt_audio_ffmpeg(ff: str, plan: dict, src: str, dst: str, cover: str = "") -> list:
    """ffmpeg argv that encodes the first audio stream to the chosen format,
    optionally embedding the thumbnail as cover art."""
    enc, ext = YT_AUDIO_ENC[plan["a_format"]]
    if not _yt_have_encoder(enc):
        raise ValueError(f"this server's ffmpeg cannot write {plan['a_format'].upper()}")
    args = [ff, "-y", "-v", "error", "-i", src]

    # WAV cannot carry a picture, so do not even ask for one.
    use_cover = bool(cover) and plan["a_format"] != "wav" and os.path.isfile(cover)
    if use_cover:
        args += ["-i", cover]
    args += ["-map", "0:a:0"]
    if use_cover:
        args += _yt_cover_map(cover) + ["-disposition:v", "attached_pic"]
    args += ["-c:a", enc]
    if not plan["lossless"]:
        args += ["-b:a", plan["a_bitrate"] + "k"]
    if plan["a_format"] == "opus":
        args += ["-vbr", "off"]
    if plan["a_format"] == "m4a":
        args += ["-movflags", "+faststart"]
    return args + [dst]


def _yt_video_reencode(ff: str, plan: dict, src: str, dst: str, drop_subs: bool = False) -> list:
    """ffmpeg argv that re-encodes `src` into the requested container. Used
    when a plain stream copy cannot carry the source into that container
    (H.264+AAC into WebM, VP9/Opus into MP4, …)."""
    enc = YT_VCODEC_ENC.get(plan["codec"], "libx264")
    if not _yt_have_encoder(enc):
        raise ValueError(f"this server's ffmpeg cannot encode {plan['codec'].upper()} — pick a different codec")
    args = [ff, "-y", "-v", "error", "-i", src, "-map", "0:v", "-map", "0:a"]
    if drop_subs:
        args += ["-sn"]
    args += ["-c:v", enc, "-crf", YT_COMPRESS_CRF.get(plan["compress"], "26")]
    if enc in ("libvpx-vp9", "libaom-av1"):
        args += ["-b:v", "0"]
    else:
        args += ["-preset", "medium"]
    if plan["container"] == "mp4":
        args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    elif plan["container"] == "webm":
        args += ["-c:a", "libopus", "-b:a", "160k"]
    else:
        args += ["-c:a", "copy"]
    return args + [dst]


@app.post("/api/tools/youtube/download")
async def tools_youtube_download(request: Request, req: YTDownloadRequest):
    """Download a YouTube video or audio honouring the chosen file type, codec,
    compression, frame rate, stream layout (one merged file or separate video +
    audio) and subtitles. Runs yt-dlp plus ffmpeg on this server and streams the
    result back — the fallback path; the primary flow downloads straight to the
    browser via the configured converter API."""
    user, err = _require_auth(request)
    if err:
        return err
    url, uerr = _tools_check_youtube_url(req.url)
    if uerr:
        return JSONResponse(status_code=400, content={"error": uerr})
    fmt = (req.format or "mp4").strip().lower()
    if fmt not in ("mp3", "mp4"):
        return JSONResponse(status_code=400, content={"error": "Format must be mp3 or mp4"})
    plan, perr = _yt_plan(req)
    if perr:
        return JSONResponse(status_code=400, content={"error": perr})
    ytdlp = _tools_ytdlp_base()
    if not ytdlp:
        return JSONResponse(status_code=503, content={
            "error": "YouTube support is not installed on this server (needs the yt-dlp package)"})
    ffmpeg = _tools_ffmpeg_bin()
    needs_ff = plan["kind"] == "video" or (plan["kind"] == "audio" and plan["a_format"] != "original")
    if needs_ff and not ffmpeg:
        return JSONResponse(status_code=503, content={
            "error": "This selection needs ffmpeg on this server — install it or pick a simpler format"})
    tmpdir = tempfile.mkdtemp(prefix="yt_")

    def _cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    async def _run(args, timeout):
        return await asyncio.to_thread(subprocess.run, args, capture_output=True,
                                       text=True, timeout=timeout)

    try:

        # Metadata first: enforce the duration cap before any bytes move.
        proc = await _run(ytdlp + ["--dump-json", "--no-playlist",
                                   "--no-warnings", url], YT_TIMEOUT_INFO)
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip().splitlines()
            detail = msg[-1] if msg else "Could not read that video"
            return JSONResponse(status_code=400, content={"error": detail[:200]})
        try:
            info = json.loads(proc.stdout)
            duration = int(info.get("duration") or 0)
        except Exception:
            info, duration = {}, 0
        if duration > YT_MAX_DURATION:
            return JSONResponse(status_code=400, content={
                "error": f"Video is too long ({duration // 3600}h+ — 3h max)"})
        title = (info.get("title") if isinstance(info, dict) else "") or "youtube"
        base = ytdlp + ["--no-playlist", "--no-warnings",
                        "--concurrent-fragments", "16",
                        "--http-chunk-size", "10M",
                        "--buffer-size", "32K",
                        "--no-part"]
        if ffmpeg:
            base += ["--ffmpeg-location", ffmpeg]
        out_tpl = os.path.join(tmpdir, "media.%(ext)s")
        split = plan["split"] if plan["kind"] == "video" else False


        # ---- download ----
        if split:

            # Two independent pulls: video-only and audio-only, kept as-is.
            for tag, sel in (("video", plan["format_sel"]),
                             ("audio", "ba/best")):
                tpl = os.path.join(tmpdir, tag + ".%(ext)s")
                p = await _run(base + ["-f", sel, "-o", tpl, url], YT_TIMEOUT_DOWNLOAD)
                if p.returncode != 0:
                    msg = (p.stderr or "").strip().splitlines()
                    detail = msg[-1] if msg else f"Could not download the {tag} stream"
                    return JSONResponse(status_code=400, content={"error": detail[:200]})
        else:
            dl = base + ["-f", plan["format_sel"], "-o", out_tpl]
            if plan["kind"] == "video":
                dl += ["--merge-output-format", plan["container"]]
                if plan["subs"] != "none":
                    dl += ["--write-subs", "--write-auto-subs",
                           "--sub-langs", plan["subs"], "--embed-subs"]
            elif plan["cover"]:
                dl += ["--write-thumbnail"]
            try:
                proc = await _run(dl + [url], YT_TIMEOUT_DOWNLOAD)
            except subprocess.TimeoutExpired:
                return JSONResponse(status_code=504, content={"error": "Download timed out"})
            if proc.returncode != 0:
                msg = (proc.stderr or "").strip().splitlines()
                detail = msg[-1] if msg else "Download failed"
                return JSONResponse(status_code=400, content={"error": detail[:200]})

        produced = [f for f in os.listdir(tmpdir)
                    if os.path.isfile(os.path.join(tmpdir, f))
                    and not f.endswith((".part", ".ytdl", ".temp", ".tmp"))]
        if not produced:
            return JSONResponse(status_code=500, content={"error": "Nothing was downloaded"})


        # ---- package ----
        if split:
            vids = [f for f in produced if f.startswith("video.")]
            auds = [f for f in produced if f.startswith("audio.")]
            if not vids or not auds:
                return JSONResponse(status_code=400, content={
                    "error": "Could not get both streams — try Combine into one file"})
            vids.sort(key=lambda f: os.path.getsize(os.path.join(tmpdir, f)), reverse=True)
            auds.sort(key=lambda f: os.path.getsize(os.path.join(tmpdir, f)), reverse=True)
            zpath = os.path.join(tmpdir, "split.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                zf.write(os.path.join(tmpdir, vids[0]),
                         _tools_safe_filename(title + " video", vids[0].split(".")[-1]))
                zf.write(os.path.join(tmpdir, auds[0]),
                         _tools_safe_filename(title + " audio", auds[0].split(".")[-1]))
            fname = _tools_safe_filename(title, "zip")
            disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(fname)
            return FileResponse(zpath, media_type=YT_MIME["zip"],
                                headers={"Content-Disposition": disp},
                                background=BackgroundTask(_cleanup))

        produced.sort(key=lambda f: os.path.getsize(os.path.join(tmpdir, f)), reverse=True)
        media = [f for f in produced if not f.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif", ".vtt", ".srt"))] or produced
        src = os.path.join(tmpdir, media[0])
        ext = media[0].rsplit(".", 1)[-1].lower()

        if plan["kind"] == "video":
            dst = os.path.join(tmpdir, "out." + plan["container"])

            # Ladder: plain copy/re-encode first, then re-encode into the

            # target container, then the same without subtitle tracks a

            # container refuses to carry.
            attempts, build_err = [], ""
            for builder in (lambda: _yt_video_ffmpeg(ffmpeg, plan, src, dst),
                            lambda: _yt_video_reencode(ffmpeg, plan, src, dst),
                            lambda: _yt_video_reencode(ffmpeg, plan, src, dst, True)):
                try:
                    attempts.append(builder())
                except ValueError as ve:
                    build_err = str(ve)
            if not attempts:
                return JSONResponse(status_code=400, content={"error": build_err or "Cannot encode that combination"})
            fproc = None
            try:
                for argv in attempts:
                    fproc = await _run(argv, 3600)
                    if fproc.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                        break
            except subprocess.TimeoutExpired:
                return JSONResponse(status_code=504, content={"error": "Re-encoding timed out"})
            if fproc is None or fproc.returncode != 0 or not os.path.isfile(dst):
                lines = ((fproc.stderr if fproc else "") or "").strip().splitlines()
                return JSONResponse(status_code=500, content={
                    "error": (lines[-1] if lines else build_err or "Remux failed")[:200]})
            src, ext = dst, plan["container"]
        else:
            if plan["a_format"] != "original":
                dst = os.path.join(tmpdir, "out." + plan["ext"])
                thumbs = [f for f in produced if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                cover = os.path.join(tmpdir, thumbs[0]) if (plan["cover"] and thumbs) else ""
                try:
                    fproc = await _run(_yt_audio_ffmpeg(ffmpeg, plan, src, dst, cover), 1800)
                except ValueError as ve:
                    return JSONResponse(status_code=400, content={"error": str(ve)})
                except subprocess.TimeoutExpired:
                    return JSONResponse(status_code=504, content={"error": "Audio conversion timed out"})
                if (fproc.returncode != 0 or not os.path.isfile(dst)) and cover:
                    fproc = await _run(_yt_audio_ffmpeg(ffmpeg, plan, src, dst, ""), 1800)
                if fproc.returncode != 0 or not os.path.isfile(dst):
                    err_line = ((fproc.stderr or "").strip().splitlines() or ["Conversion failed"])[-1]
                    return JSONResponse(status_code=500, content={"error": err_line[:200]})
                src, ext = dst, plan["ext"]

        fname = _tools_safe_filename(title, ext)
        disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(fname)
        return FileResponse(src, media_type=YT_MIME.get(ext, "application/octet-stream"),
                            headers={"Content-Disposition": disp},
                            background=BackgroundTask(_cleanup))
    except subprocess.TimeoutExpired:
        _cleanup()
        return JSONResponse(status_code=504, content={"error": "Download timed out"})
    except Exception as e:
        _cleanup()
        return JSONResponse(status_code=500, content={"error": str(e)})


CONVERT_TARGETS = {
    "png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP",
    "gif": "GIF", "bmp": "BMP", "tiff": "TIFF", "ico": "ICO",
}
CONVERT_FROM_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
                     ".tiff", ".tif", ".ico", ".pdf"}
CONVERT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
                "tiff": "image/tiff", "ico": "image/x-icon", "zip": "application/zip"}


@app.post("/api/tools/convert")
async def tools_convert(request: Request, target: str = "", quality: int = 85,
                        max_width: int = 0, filename: str = ""):
    """Convert an uploaded file: images between formats (PNG/JPG/WebP/GIF/BMP/
    TIFF/ICO) or PDF pages to images. Raw octet-stream body like file-upload;
    target/quality/max_width are query params. Multi-page PDFs come back as
    a zip of page images."""
    user, err = _require_auth(request)
    if err:
        await _drain_request_body(request)
        return err
    target = (target or "").strip().lower().lstrip(".")
    if target not in CONVERT_TARGETS:
        await _drain_request_body(request)
        return JSONResponse(status_code=400, content={
            "error": "Target must be one of: " + ", ".join(sorted(CONVERT_TARGETS))})
    try:
        quality = max(1, min(100, int(quality)))
    except Exception:
        quality = 85
    try:
        max_width = max(0, int(max_width))
    except Exception:
        max_width = 0
    fname = (filename or "").strip().replace("\\", "/").split("/")[-1][:120]
    ext = os.path.splitext(fname)[1].lower() if fname else ""
    if ext not in CONVERT_FROM_EXTS:
        await _drain_request_body(request)
        return JSONResponse(status_code=400, content={
            "error": "Upload a PNG, JPG, WebP, GIF, BMP, TIFF, ICO or PDF file"})
    upload_limit = None if user.get("is_admin") else UPLOAD_LIMIT_BYTES
    tmpdir = tempfile.mkdtemp(prefix="conv_")

    def _cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        src = os.path.join(tmpdir, "input" + ext)
        size = 0
        with open(src, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if upload_limit is not None and size > upload_limit:
                    out.close()
                    await _drain_request_body(request)
                    return JSONResponse(status_code=413, content={
                        "error": f"File too large (max {UPLOAD_LIMIT_MB:g}MB)"})
                out.write(chunk)
        if size == 0:
            return JSONResponse(status_code=400, content={"error": "Empty file"})

        def _prep(img):
            if max_width and img.width > max_width:
                h = round(img.height * max_width / img.width)
                img = img.resize((max_width, h))
            return img

        try:
            from PIL import Image as PIL_Image
        except Exception:
            return JSONResponse(status_code=503, content={
                "error": "Image support is not installed on this server"})

        def _save(img, path, fmt):
            kw = {}
            if fmt in ("JPEG", "WEBP"):
                kw["quality"] = quality
            if fmt == "JPEG" and img.mode in ("RGBA", "LA", "PA"):
                bg = PIL_Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif fmt in ("JPEG", "BMP") and img.mode not in ("RGB",):
                img = img.convert("RGB")
            img.save(path, fmt, **kw)

        pages = []
        if ext == ".pdf":
            try:
                import pypdfium2 as pdfium
            except Exception:
                return JSONResponse(status_code=503, content={
                    "error": "PDF support is not installed on this server"})
            try:
                pdf = pdfium.PdfDocument(src)
            except Exception:
                return JSONResponse(status_code=400, content={"error": "Not a valid PDF file"})
            try:
                if len(pdf) > TOOLS_MAX_PAGES:
                    return JSONResponse(status_code=400, content={
                        "error": f"PDF has too many pages (max {TOOLS_MAX_PAGES})"})
                for i, page in enumerate(pdf):
                    bitmap = page.render(scale=2)
                    pages.append(bitmap.to_pil())
            finally:
                try:
                    pdf.close()
                except Exception:
                    pass
            if not pages:
                return JSONResponse(status_code=400, content={"error": "PDF has no pages"})
        else:
            try:
                with PIL_Image.open(src) as im:
                    im.load()
                    pages = [im.convert("RGBA") if im.mode == "P" else im.copy()]
            except Exception:
                return JSONResponse(status_code=400, content={"error": "Not a valid image file"})
        fmt = CONVERT_TARGETS[target]
        stem = (os.path.splitext(fname)[0] or "converted")[:60] or "converted"
        stem = re.sub(r"[^A-Za-z0-9 _.\-]+", "_", stem).strip(" .") or "converted"
        if len(pages) == 1:
            out_path = os.path.join(tmpdir, f"{stem}.{target}")
            _save(_prep(pages[0]), out_path, fmt)
            disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(f"{stem}.{target}")
            return FileResponse(out_path, media_type=CONVERT_MIME[target],
                                headers={"Content-Disposition": disp},
                                background=BackgroundTask(_cleanup))
        zip_path = os.path.join(tmpdir, f"{stem}-{target}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, pg in enumerate(pages, 1):
                tmp_img = os.path.join(tmpdir, f"page-{i:03d}.{target}")
                _save(_prep(pg), tmp_img, fmt)
                zf.write(tmp_img, f"page-{i:03d}.{target}")
                try:
                    os.remove(tmp_img)
                except OSError:
                    pass
        disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(f"{stem}-{target}.zip")
        return FileResponse(zip_path, media_type=CONVERT_MIME["zip"],
                            headers={"Content-Disposition": disp},
                            background=BackgroundTask(_cleanup))
    except Exception as e:
        _cleanup()
        return JSONResponse(status_code=500, content={"error": str(e)})


class ScanRequest(BaseModel):
    path: Optional[str] = ""
    content: Optional[str] = ""
    language: Optional[str] = ""



# Heuristic danger rules: (language, level, regex, message).

# This is a tripwire, not a sandbox: obvious malicious patterns are

# blocked/flagged, but determined obfuscation can evade static checks.
_SCAN_RULES = [
    ("py", "danger", r"os\.system\s*\(", "os.system() shell execution"),
    ("py", "danger", r"os\.popen\s*\(", "os.popen() shell execution"),
    ("py", "danger", r"pty\.spawn\s*\(", "pty.spawn() interactive shell"),
    ("py", "danger", r"ctypes\s*\.\s*(CDLL|windll|cdll|oledll)", "ctypes native-code loading"),
    ("py", "danger", r"__import__\s*\(\s*['\"]os['\"]\s*\)", "obfuscated os import"),
    ("py", "danger", r"subprocess\.\w+\s*\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    ("py", "danger", r"(eval|exec)\s*\(\s*.*(urlopen|requests\.get|urllib\.request|urlretrieve)", "executes remotely-fetched code"),
    ("py", "danger", r"socket\.(socket|create_connection)\s*\(", "raw socket networking"),
    ("py", "warn", r"(?<!\w)(eval|exec)\s*\(", "dynamic code execution"),
    ("py", "warn", r"pickle\.loads?\s*\(", "unsafe pickle deserialization"),
    ("py", "warn", r"marshal\.loads?\s*\(", "unsafe marshal deserialization"),
    ("py", "warn", r"(os\.remove|os\.rmdir|os\.removedirs|shutil\.rmtree)\s*\(", "destructive filesystem call"),
    ("py", "warn", r"base64\.b64decode\s*\(", "obfuscated payload (base64)"),
    ("js", "danger", r"require\s*\(\s*['\"]child_process['\"]\s*\)", "child_process (shell access)"),
    ("js", "danger", r"from\s+['\"]child_process['\"]", "child_process import (shell access)"),
    ("js", "danger", r"process\.env\b", "reads server environment (may hold API keys)"),
    ("js", "danger", r"fs\.rm\s*\(.*recursive\s*:\s*true", "recursive filesystem delete"),
    ("js", "danger", r"(eval|exec)\s*\(\s*.*(fetch\s*\(|XMLHttpRequest|https?://)", "executes remotely-fetched code"),
    ("js", "warn", r"(?<!\w)(eval|exec)\s*\(", "dynamic code execution"),
    ("js", "warn", r"new\s+Function\s*\(", "dynamic code construction"),
    ("js", "warn", r"fetch\s*\(\s*['\"]https?://", "outbound network fetch"),
    ("html", "danger", r"(?<!\w)eval\s*\(", "inline eval()"),
    ("html", "danger", r"new\s+Function\s*\(", "dynamic code construction"),
    ("html", "danger", r"document\.cookie", "cookie access (combine with fetch = exfiltration)"),
    ("html", "warn", r"<form[^>]*action\s*=\s*['\"]https?://", "form posts to external URL"),
    ("html", "warn", r"href\s*=\s*['\"]javascript:", "javascript: URL"),
    ("html", "warn", r"<iframe[^>]*srcdoc", "iframe with inline srcdoc content"),
]
_SCAN_RULES_COMPILED = [(lang, lvl, re.compile(rx, re.IGNORECASE), msg)
                        for lang, lvl, rx, msg in _SCAN_RULES]
_SCAN_EXTS = {
    ".py": "py", ".pyw": "py",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".html": "html", ".htm": "html",
    ".php": "js", ".rb": "js", ".sh": "js", ".ps1": "js", ".bat": "js",
}
_SCAN_MAX_BYTES = 1024 * 1024


def _scan_text(content: str, language: str):
    findings = []
    for lang, level, rx, msg in _SCAN_RULES_COMPILED:
        if lang != language:
            continue
        try:
            for m in rx.finditer(content):
                line = content.count("\n", 0, m.start()) + 1
                findings.append({"level": level, "rule": msg, "line": line,
                                 "detail": m.group(0)[:80]})
                if len(findings) >= 50:
                    break
        except Exception:
            continue
        if len(findings) >= 50:
            break
    verdict = "clean"
    if any(f["level"] == "danger" for f in findings):
        verdict = "danger"
    elif findings:
        verdict = "warn"
    return verdict, findings


@app.post("/api/coding/scan")
async def coding_scan(request: Request, req: ScanRequest):
    user, err = _require_auth(request)
    if err:
        return err
    content = req.content or ""
    language = (req.language or "").lower()
    if req.path:
        coding_state = _get_coding_state(_safe_username(user["username"]))
        folder = coding_state.get("folder")
        access_err = _folder_access_error(user, folder)
        if access_err:
            return access_err
        if _bad_path(req.path):
            return JSONResponse(status_code=400, content={"error": "Invalid path"})
        full = os.path.join(folder, req.path)
        if not _is_within(folder, full):
            return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
        if not os.path.isfile(full):
            return JSONResponse(status_code=404, content={"error": "File not found"})
        try:
            if os.path.getsize(full) > _SCAN_MAX_BYTES:
                return JSONResponse(status_code=400, content={"error": "File too large to scan"})
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})


        if not language:
            language = _SCAN_EXTS.get(os.path.splitext(req.path)[1].lower(), "")
    if not content:
        return JSONResponse(status_code=400, content={"error": "Nothing to scan"})
    if len(content.encode("utf-8", "ignore")) > _SCAN_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "Content too large to scan"})
    if not language:
        language = "js"
    verdict, findings = _scan_text(content, language)
    logger.info(f"[SCAN] user={user.get('username','-')} verdict={verdict} findings={len(findings)}")
    note = "" if language in ("py", "js", "html") else "no rules for this language; only generic patterns checked"
    return {"verdict": verdict, "findings": findings,
            "scanned_bytes": len(content.encode("utf-8", "ignore")), "note": note}


def _sanitize_shell_command(command: str) -> str:
    """Clean an AI- or user-supplied shell command.

    Models often emit stray wrapping quotes/backticks (e.g. `cmd /c dir"`
    or `"dir"`), which cmd.exe then treats as part of the program name.
    Strip markdown fences, surrounding quotes/backticks, and any single
    unbalanced trailing quote so the intent executes.
    """
    c = (command or "").strip()

    # Strip markdown code fences if the whole command is fenced
    if c.startswith("```"):
        c = re.sub(r"^```\w*\n?", "", c)
        c = re.sub(r"\n?```$", "", c).strip()

    # Strip one layer of surrounding matching quotes/backticks
    if len(c) >= 2 and c[0] == c[-1] and c[0] in "\"'`":
        c = c[1:-1].strip()

    # Drop a lone unbalanced trailing quote: the classic `dir"` bug
    for q in ("\"", "'"):
        if c.endswith(q) and c.count(q) % 2 == 1:
            c = c[:-1].rstrip()

    # Drop a lone unbalanced leading quote
    for q in ("\"", "'"):
        if c.startswith(q) and c.count(q) % 2 == 1:
            c = c[1:].lstrip()

    # subprocess runs with shell=True, i.e. already inside cmd.exe. A

    # redundant `cmd /c` prefix gets double-wrapped (cmd /d /s /c "cmd /c X")

    # and cmd.exe's /s quote handling mangles it (observed: program `dir"`).

    # Strip it so bare commands execute.
    c = re.sub(r"^(cmd(\.exe)?\s+/[ck]\s+)", "", c, flags=re.IGNORECASE).strip()
    return c.strip()


def _api_in_container() -> bool:
    """True when THIS api process is itself running inside a container."""
    if os.environ.get("OMNIILINK_IN_CONTAINER", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        return os.path.exists("/.dockerenv")
    except Exception:
        return False


def _sandbox_host_path(path: str) -> str:
    """Map a path the API process sees to the path the Docker DAEMON sees.

    Bind sources are resolved on the daemon's filesystem, not the caller's.
    Native runs: OMNIILINK_SANDBOX_HOST_ROOT is unset -> identity (unchanged
    behavior). Containerized API (compose mounts ./user_data at /app/...):
    set OMNIILINK_SANDBOX_HOST_ROOT to the host folder backing APP_DIR, so
    /app/user_data/x becomes <root>/user_data/x on the host. Without it the
    daemon would mount an empty, freshly-created directory instead of the
    real project (commands appear to work, writes vanish)."""
    root = (os.environ.get("OMNIILINK_SANDBOX_HOST_ROOT", "") or "").strip()
    if not root:
        return path
    try:
        if not _is_within(APP_DIR, path):
            return path
        rel = os.path.relpath(path, APP_DIR)
        if rel in (os.curdir, "."):
            return root
        return os.path.normpath(os.path.join(root, rel))
    except Exception:
        return path


_host_root_warned = False


def _warn_missing_host_root():
    """Loud one-time warning when containerized without a host-root mapping."""
    global _host_root_warned
    if _host_root_warned:
        return
    if _api_in_container() and not os.environ.get("OMNIILINK_SANDBOX_HOST_ROOT", "").strip():
        _host_root_warned = True
        logger.warning(
            "[SANDBOX] OMNIILINK_SANDBOX_HOST_ROOT is not set while the API runs "
            "in a container: sandbox bind mounts resolve against the Docker "
            "daemon's root, not /app. Set it to the host folder that backs /app "
            "(compose default: C:/Modellink).")


def _sandbox_config() -> Optional[dict]:
    """Docker execution sandbox config, or None when disabled.

    OMNIILINK_SANDBOX=""      -> host execution (default, old behavior)
    OMNIILINK_SANDBOX=docker  -> every command in a FRESH ephemeral container
    OMNIILINK_SANDBOX=project -> one PERSISTENT container per project, so
                                 installs (pip/npm/pyenv/...) survive between
                                 commands. Only that project folder is ever
                                 mounted. Read per-request so toggles need no
                                 restart.
    """
    mode = os.environ.get("OMNIILINK_SANDBOX", "").strip().lower()
    if mode not in ("docker", "project"):
        return None

    # Default 5: project containers stop after 5 idle minutes (their installed

    # layers survive and they revive on next use), so a forgotten preview never

    # keeps a container running forever. Set 0 to keep them running.
    try:
        idle_minutes = int(os.environ.get("OMNIILINK_PROJ_IDLE_MIN", "5") or 0)
    except ValueError:
        idle_minutes = 5
    return {
        "mode": mode,
        "image": os.environ.get("OMNIILINK_SANDBOX_IMAGE", "omniilink-sandbox:latest"),
        "network": os.environ.get("OMNIILINK_SANDBOX_NET", "bridge"),
        "memory": os.environ.get("OMNIILINK_SANDBOX_MEM", "512m"),
        "cpus": os.environ.get("OMNIILINK_SANDBOX_CPUS", "1.0"),
        "timeout": 60,
        "idle_minutes": max(0, idle_minutes),
    }


def _project_container_name(user_id: str, folder: str) -> str:
    """Deterministic container name for a user's project folder.

    Includes a hash of the FULL folder path so two different folders with
    the same base name (possible for admins) never share a container.
    """
    import hashlib as _hl
    proj = os.path.basename(os.path.normpath(folder or ""))
    safe = re.sub(r"[^a-z0-9_-]", "-", (proj or "proj").lower())[:32].strip("-") or "proj"
    digest = _hl.sha1(os.path.normcase(os.path.abspath(folder or "")).encode()).hexdigest()[:8]
    return f"omniilink-p-{user_id[:20]}-{safe}-{digest}"[:63]



# name -> last-used epoch seconds (drives the idle reaper)
_proj_containers: dict = {}
_proj_lock = threading.Lock()
_reaper_started = False

# Mirrored to disk so idle bookkeeping survives an API restart: a container

# that was busy seconds before a restart is not mistaken for a stale one.
_PROJ_ACTIVITY_FILE = os.path.join(APP_DIR, "user_data", ".proj_activity.json")


def _proj_activity_load() -> dict:
    try:
        with open(_PROJ_ACTIVITY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {str(k): float(v) for k, v in raw.items() if v}
    except Exception:
        return {}


def _proj_activity_save() -> None:
    try:
        os.makedirs(os.path.dirname(_PROJ_ACTIVITY_FILE), exist_ok=True)
        with _proj_lock:
            snap = dict(_proj_containers)
        tmp = _PROJ_ACTIVITY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, _PROJ_ACTIVITY_FILE)
    except Exception:
        pass


def _proj_touch(name: str) -> None:
    """Record that a project container was just used (drives idle reaping)."""
    with _proj_lock:
        _proj_containers[name] = time.time()
    _proj_activity_save()


def _docker_available() -> bool:
    import shutil as _shutil
    return bool(_shutil.which("docker"))


def _ensure_project_container(name: str, folder: str, cfg: dict) -> tuple:
    """Make sure the persistent project container exists and runs.

    Revives stopped containers (installed layers survive), recreates only
    when missing. Returns (True, "") or (False, error_tail).
    """
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip() == "true":
            _proj_touch(name)
            return True, ""
        if r.returncode == 0:

            # Exists but stopped: revive (keeps installed packages)
            r2 = subprocess.run(["docker", "start", name],
                                capture_output=True, text=True, timeout=60)
            if r2.returncode == 0:
                _proj_touch(name)
                return True, ""
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, timeout=30)
        argv = ["docker", "run", "-d", "--name", name,
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                f"--memory={cfg['memory']}", f"--cpus={cfg['cpus']}",
                "--pids-limit=128",
                "--network", cfg["network"],
                "--restart", "unless-stopped",
                "-v", f"{_sandbox_host_path(folder)}:/work", "-w", "/work",
                cfg["image"], "sleep", "infinity"]
        r3 = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        if r3.returncode != 0:
            return False, ((r3.stderr or r3.stdout) or "docker run failed")[-500:]
        _proj_touch(name)
        _maybe_start_reaper(cfg.get("idle_minutes", 0))
        return True, ""
    except Exception as e:
        return False, str(e)[-500:]


def _exec_project_container(name: str, command: str, timeout: int):
    _proj_touch(name)
    return subprocess.run(["docker", "exec", "-w", "/work", name,
                           "sh", "-c", command],
                          capture_output=True, text=True, timeout=timeout)


def _maybe_warm_project(user, folder) -> None:
    """Bring a project's sandbox container back up when its preview is opened.

    Stopped containers keep their installed layers, so reviving here makes a
    preview link work transparently after an idle shutdown. Best-effort: any
    failure is ignored so previewing still works when Docker is unavailable.
    Never removes anything — the project's files live on the bind mount, not
    in the container.
    """
    try:
        sb = _sandbox_config()
        if not sb or sb.get("mode") != "project" or not folder:
            return
        if not _docker_available():
            return
        if _api_in_container():
            _warn_missing_host_root()
        user_id = _safe_username(user.get("username", ""))
        name = _project_container_name(user_id, folder)
        _ensure_project_container(name, folder, sb)
    except Exception:
        pass


def _maybe_start_reaper(idle_minutes: int):
    """Idle reaper: STOP (not remove, so installed layers survive) project
    containers unused for idle_minutes. 0 disables. Started once.

    `docker stop` keeps the container and its installed layers; the next
    command or preview revives it with `docker start`. The project's files
    live on the bind mount, so a stopped/removed container never deletes a
    project. Activity is mirrored to disk so this survives API restarts.
    """
    global _reaper_started
    if not idle_minutes or _reaper_started:
        return
    _reaper_started = True

    def _loop():

        # Seed from disk first so containers used shortly before a restart are

        # not treated as idle the moment the reaper comes back.
        for n, t in _proj_activity_load().items():
            with _proj_lock:
                _proj_containers.setdefault(n, t)
        while True:
            try:
                time.sleep(60)
                cutoff = time.time() - idle_minutes * 60
                with _proj_lock:
                    stale = [n for n, t in _proj_containers.items() if t < cutoff]
                for n in stale:
                    try:
                        subprocess.run(["docker", "stop", "-t", "10", n],
                                       capture_output=True, timeout=30)
                    except Exception:
                        pass
                    with _proj_lock:
                        _proj_containers.pop(n, None)
                if stale:
                    _proj_activity_save()
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()


def _drop_project_container(user_id: str, folder: str):
    """Best-effort removal of a project's container (project deleted)."""
    try:
        if not _docker_available():
            return
        name = _project_container_name(user_id, folder or "")
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, timeout=30)
        with _proj_lock:
            _proj_containers.pop(name, None)
    except Exception:
        pass


def _build_docker_cmd(command: str, workdir_host: str, cfg: dict) -> list:
    """Build the `docker run` argv for one sandboxed command (no shell)."""
    return [
        "docker", "run", "--rm",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--memory={cfg['memory']}",
        f"--cpus={cfg['cpus']}",
        "--pids-limit=128",
        "--network", cfg["network"],
        "-v", f"{_sandbox_host_path(workdir_host)}:/work",
        "-w", "/work",
        cfg["image"],
        "sh", "-c", command,
    ]



# Heuristic escape patterns for the restricted (non-admin) shell.

# This is defense-in-depth, not a sandbox boundary: the real containment is

# the forced cwd inside the user's own data dir plus authentication.

# - drive-letter absolute paths (C:\, D:/) - lookbehind avoids matching http://

# - UNC paths (\\server\share)

# - parent (..) segments

# - %VAR% expansion (leaks environment variables / escapes the folder)

# - script hosts and LOLBins that can reach outside the sandbox
_RESTRICTED_CMD_RE = re.compile(
    r"(?<![a-zA-Z/])[a-zA-Z]:[\\/]|\\\\\S|\.\."
    r"|%[^%]"
    r"|(?<![a-z0-9_])(powershell|pwsh|wscript|cscript|mshta|rundll32|bitsadmin|certutil)(?![a-z0-9_])"
    r"|(?<![a-z0-9_])reg\s+(?:add|delete|query|import|export|copy)",
    re.IGNORECASE)


def _rewrite_inline_python(command: str, cwd: str):
    """cmd.exe (and quoted sh one-liners) cannot take a multi-line `python -c`
    script — the shell mangles it and python dies with 'Argument expected for
    the -c option'. Rewrite `python -c <multiline script>` into a temp file
    next to the project and run that instead. Returns (command, tmp_abspath
    or None). Single-line -c calls pass through untouched."""
    m = re.match(r"(?i)^\s*(python3?|py)(?:\.exe)?\s+-c\s+(.*)$", command or "", re.DOTALL)
    if not m or "\n" not in m.group(2):
        return command, None
    prog, script = m.group(1), m.group(2).strip()
    if len(script) >= 2 and script[0] == script[-1] and script[0] in "\"'":
        script = script[1:-1]
    try:
        fd, tmp = tempfile.mkstemp(prefix="omni_inline_", suffix=".py", dir=cwd)
    except Exception:
        return command, None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return command, None
    return prog + ' "' + os.path.basename(tmp) + '"', tmp


@app.post("/api/coding/run-command")
async def coding_run_command(request: Request, req: CodingRunCommand):
    user, err = _require_auth(request)
    if err:
        return err
    command = _sanitize_shell_command(req.command)
    if not command:
        return JSONResponse(status_code=400, content={"error": "Empty command"})
    uname = user.get("username", "-")
    user_id = _safe_username(uname)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    if user.get("is_admin"):
        cwd = req.cwd or folder or os.getcwd()
        if not os.path.isdir(cwd):
            cwd = os.getcwd()
    else:

        # Restricted shell: the command always runs inside the user's own

        # data dir (or, for workspace members, inside the workspace folder).

        # Client-supplied cwd is validated, never trusted, and

        # absolute/parent paths in the command are rejected.
        base = folder if _workspace_row(folder) else _user_data_dir(user_id)
        if req.cwd:
            cand = req.cwd if os.path.isabs(req.cwd) else os.path.join(folder or base, req.cwd)
            cand = os.path.normpath(cand)
            if not _is_within(base, cand) or not os.path.isdir(cand):
                return JSONResponse(status_code=403, content={"error": "Commands are limited to your own project folder"})
            cwd = cand
        elif folder and _is_within(base, folder) and os.path.isdir(folder):
            cwd = folder
        else:
            return JSONResponse(status_code=400, content={"error": "Open one of your projects first — commands run inside it"})
        if _RESTRICTED_CMD_RE.search(command):
            return JSONResponse(status_code=403, content={"error": "Absolute paths, .., %VAR% expansion and script hosts (powershell, wscript, mshta, ...) are not allowed — use paths inside your project folder"})
    command, inline_tmp = _rewrite_inline_python(command, cwd)
    try:
        sb = _sandbox_config()
        backend = "host"
        if sb is None:
            if _api_in_container():

                # Never fall back to running inside the API container: it

                # holds the docker socket, the database and every user's

                # files - that is exactly what must stay unreachable.
                return JSONResponse(status_code=503, content={"error": "Execution sandbox is disabled inside the API container. Set OMNIILINK_SANDBOX=project (the docker-compose default)."})
        else:
            _warn_missing_host_root()

            # A sandbox only ever mounts paths inside the app tree, so no

            # command (admin or not) can bind-mount the rest of the host.
            if not _is_within(APP_DIR, cwd):
                cwd = folder if (folder and _is_within(APP_DIR, folder)) else _user_data_dir(user_id)
        if sb is not None and sb["mode"] == "project":

            # Persistent per-project container: installs survive between

            # commands. Fail LOUDLY if docker is missing.
            import shutil as _shutil
            if not _shutil.which("docker"):
                return JSONResponse(status_code=503, content={"error": "Execution sandbox unavailable (docker not found). Set OMNIILINK_SANDBOX='' to use host execution."})
            cname = _project_container_name(user_id, cwd)
            ok, err = _ensure_project_container(cname, cwd, sb)
            if not ok:
                return JSONResponse(status_code=503, content={"error": f"Project container unavailable: {err}"})
            backend = f"proj:{cname}"
            proc = _exec_project_container(cname, command, sb["timeout"])
        elif sb is not None:

            # Ephemeral per-command container. Fail LOUDLY if missing.
            import shutil as _shutil
            if not _shutil.which("docker"):
                return JSONResponse(status_code=503, content={"error": "Execution sandbox unavailable (docker not found). Set OMNIILINK_SANDBOX='' to use host execution."})
            backend = "docker"
            proc = subprocess.run(
                _build_docker_cmd(command, cwd, sb),
                capture_output=True, text=True, timeout=sb["timeout"]
            )
        else:
            proc = subprocess.run(
                command, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=60
            )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
        logger.info(f"[CMD] user={uname} [{backend}] {command} (exit {exit_code})")
        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=408, content={"error": "Command timed out (60s)"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if inline_tmp:
            try:
                os.remove(inline_tmp)
            except OSError:
                pass

@app.get("/api/coding/settings")
async def coding_get_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    settings = dict(coding_state["settings"])
    env = _coding_exec_env()

    # Server-owned: the stored per-user value is ignored and OVERWRITTEN, so a
    # prompt saved on another machine (or before a Windows->Docker switch)
    # cannot pin the wrong OS into the model's instructions.
    settings["system_prompt"] = _coding_system_prompt(_safe_username(user["username"]))
    settings["prompt_locked"] = True
    settings["hosting_mode"] = env["hosting"]
    settings["sandbox"] = env["sandbox"]

    settings["sandbox_mode"] = env["sandbox_mode"]
    settings["exec_os"] = env["exec_os"]
    settings["exec_shell"] = env["exec_shell"]
    settings["container_workdir"] = env["container_workdir"]
    settings["host_os"] = sys.platform
    settings["upload_limit_mb"] = UPLOAD_LIMIT_MB
    settings["upload_chunk_mb"] = UPLOAD_CHUNK_BYTES // (1024 * 1024)
    return settings

@app.post("/api/coding/settings")
async def coding_update_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    for k, v in body.items():
        if k not in coding_state["settings"]:
            continue
        if k == "system_prompt":

            # Server-owned. Accepting it would let a stale prompt (saved on a
            # Windows box, replayed on a Docker host) reintroduce the very
            # Windows/Docker command mix this prompt was split to prevent.
            continue
        if k == "versions_to_keep":
            try:
                v = max(1, min(_PROJECT_VERSION_MAX_KEEP, int(v)))
            except (TypeError, ValueError):
                continue
        coding_state["settings"][k] = v
    _save_coding_settings(_safe_username(user["username"]), coding_state["settings"])
    return {"status": "ok", "settings": coding_state["settings"]}

@app.get("/api/coding/project-context")
async def coding_project_context(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    if not folder or not os.path.isdir(folder):
        return {"context": ""}
    if not user.get("is_admin") and not _is_within(_user_data_dir(_safe_username(user["username"])), folder):
        return {"context": ""}
    tree = _build_file_tree(folder)
    context_lines = [f"Project: {os.path.basename(folder)}", f"Path: {folder}", "", "File tree:"]
    def _render_tree(entries, indent=0):
        for e in entries:
            prefix = "  " * indent
            if e["type"] == "dir":
                context_lines.append(f"{prefix}{e['name']}/")
                _render_tree(e.get("children", []), indent + 1)
            else:
                size = e.get("size", 0)
                context_lines.append(f"{prefix}{e['name']} ({size} bytes)")
    _render_tree(tree)
    return {"context": "\n".join(context_lines), "folder": folder}


@app.get("/api/bandwidth")
async def user_bandwidth(request: Request):
    """Bandwidth used inside the rolling window + current transfer speed.
    Admins are unlimited (cap: null) but still measured."""
    user, err = _require_auth(request)
    if err:
        return err
    is_admin = bool(user.get("is_admin"))
    used = 0
    speed = 0.0
    if BW_ENABLED:
        st = _bandwidth_state(user["id"])
        with _bw_lock:
            now = time.monotonic()
            used = _bandwidth_window_used(st, now)
            speed = _bandwidth_speed(st, now)
    return {
        "used": used,
        "cap": None if is_admin else BW_WINDOW_BYTES,
        "window_hours": BW_WINDOW_HOURS,
        "rate_mbps": BW_RATE_MBPS,
        "percent": round(100.0 * used / BW_WINDOW_BYTES, 1)
        if (not is_admin and BW_WINDOW_BYTES) else 0,
        "limited": bool(BW_ENABLED and not is_admin),
        "speed": speed,
    }


# ==========================================================================
# RAG over the open project — local, dependency-free lexical retrieval (BM25).
# Index the folder once; query returns the most relevant chunks with file:line
# so coding chat (or you) can pull in just the relevant code, not the whole tree.
# ==========================================================================
_RAG_MAX_CHUNK_LINES = 60
_RAG_CHUNK_OVERLAP = 10
_RAG_MAX_FILE_BYTES = 400 * 1024
_RAG_MAX_CHUNKS = 5000
_RAG_STOP = set(
    "the a an and or of to in is it for on with as at by be this that from are was were "
    "will can not you your we our they them he she his her its if then else return def "
    "class import from self none true false".split())


def _rag_tokenize(text: str) -> list:
    toks = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    return [t for t in toks if len(t) >= 2 and t not in _RAG_STOP]


def _rag_chunks(text: str) -> list:
    lines = (text or "").splitlines()
    step = max(1, _RAG_MAX_CHUNK_LINES - _RAG_CHUNK_OVERLAP)
    out, i = [], 0
    while i < len(lines):
        block = lines[i:i + _RAG_MAX_CHUNK_LINES]
        if block:
            out.append((i + 1, i + len(block), "\n".join(block)))
        if i + _RAG_MAX_CHUNK_LINES >= len(lines):
            break
        i += step
    return out


def _rag_index(user_id: str, folder: str) -> int:
    """(Re)build the chunk index for a project folder. Returns chunk count."""
    conn = get_db()
    count = 0
    try:
        conn.execute("DELETE FROM project_chunks WHERE user_id = ?", (user_id,))
        stop = False
        for dirpath, dirnames, filenames in os.walk(folder):
            if stop:
                break
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            for name in filenames:
                if count >= _RAG_MAX_CHUNKS:
                    stop = True
                    break
                if name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > _RAG_MAX_FILE_BYTES:
                        continue
                    with open(full, "rb") as f:
                        if b"\x00" in f.read(8192):
                            continue
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except Exception:
                    continue
                rel = os.path.relpath(full, folder).replace("\\", "/")
                for (s, e, block) in _rag_chunks(text):
                    toks = _rag_tokenize(block)
                    if not toks:
                        continue
                    conn.execute(
                        "INSERT INTO project_chunks (user_id, path, start_line, end_line,"
                        " text, tokens, tlen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (user_id, rel, s, e, block[:8000], " ".join(toks), len(toks)))
                    count += 1
                    if count >= _RAG_MAX_CHUNKS:
                        stop = True
                        break
        conn.commit()
    finally:
        conn.close()
    return count


def _rag_query(user_id: str, q: str, k: int = 8) -> list:
    """BM25 over the user's indexed chunks. Returns top-k with file:line."""
    qtoks = _rag_tokenize(q)
    if not qtoks:
        return []
    qset = set(qtoks)
    conn = get_db()
    try:
        rows = conn.execute("SELECT path, start_line, end_line, text, tokens, tlen"
                            " FROM project_chunks WHERE user_id = ?", (user_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    n = len(rows)
    avgdl = (sum((r["tlen"] or 0) for r in rows) / n) or 1.0
    docs = [r["tokens"].split() for r in rows]
    df = {}
    for toks in docs:
        for t in set(toks):
            if t in qset:
                df[t] = df.get(t, 0) + 1
    k1, b = 1.5, 0.75
    scored = []
    for r, toks in zip(rows, docs):
        tf = {}
        for t in toks:
            if t in qset:
                tf[t] = tf.get(t, 0) + 1
        if not tf:
            continue
        tlen = r["tlen"] or 1
        score = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * tlen / avgdl))
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [{"path": r["path"], "start_line": r["start_line"], "end_line": r["end_line"],
             "score": round(score, 3), "text": r["text"]} for score, r in scored[:k]]


def _rag_context(user_id: str, q: str, k: int = 6, budget: int = 6000) -> str:
    """Compact context block for prompt injection (file:line + code)."""
    hits = _rag_query(user_id, q, k)
    if not hits:
        return ""
    parts, used = [], 0
    for h in hits:
        snippet = h["text"]
        if used + len(snippet) > budget:
            snippet = snippet[:max(0, budget - used)]
        if not snippet:
            break
        parts.append(f"### {h['path']}:{h['start_line']}-{h['end_line']}\n{snippet}")
        used += len(snippet)
    return "RELEVANT PROJECT CODE (retrieved by relevance):\n" + "\n\n".join(parts)


@app.post("/api/coding/rag/index")
async def coding_rag_index(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    try:
        n = await asyncio.to_thread(_rag_index, uid, folder)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"status": "ok", "chunks": n, "folder": folder}


@app.post("/api/coding/rag/query")
async def coding_rag_query(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    q = str(data.get("q") or "").strip()
    if not q or len(q) > 500:
        return JSONResponse(status_code=400, content={"error": "Query must be 1-500 characters"})
    try:
        k = max(1, min(30, int(data.get("k") or 8)))
    except (TypeError, ValueError):
        k = 8
    uid = _safe_username(user["username"])
    hits = await asyncio.to_thread(_rag_query, uid, q, k)
    return {"query": q, "count": len(hits), "results": hits}


@app.get("/api/coding/rag/status")
async def coding_rag_status(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    conn = get_db()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM project_chunks WHERE user_id = ?",
                         (uid,)).fetchone()["n"]
        files = conn.execute("SELECT COUNT(DISTINCT path) AS n FROM project_chunks"
                             " WHERE user_id = ?", (uid,)).fetchone()["n"]
    finally:
        conn.close()
    return {"chunks": n, "files": files}


_CODE_EXTS = (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java",
              ".cs", ".php", ".rs", ".c", ".cpp", ".h", ".hpp", ".swift", ".kt")


def _suggest_test_path(src: str) -> str:
    d, name = os.path.split(src or "")
    base, ext = os.path.splitext(name)
    prefix = (d + "/") if d else ""
    if ext == ".py":
        return prefix + "tests/test_" + base + ".py"
    if ext in (".js", ".mjs", ".ts", ".tsx", ".jsx"):
        return prefix + base + ".test" + ext
    return prefix + base + "_test" + ext


class CodingGenerateTests(BaseModel):
    path: str = ""
    model: str = ""


@app.post("/api/coding/generate-tests")
async def coding_generate_tests(request: Request, req: CodingGenerateTests):
    """Ask a model to write a test file for a source file (or the largest
    source file when none is given). Returns the text — it is NOT written to
    disk; you review and save it. Local-first (omniilink routes to your models)."""
    user, err = _require_auth(request)
    if err:
        return err
    uid = _safe_username(user["username"])
    folder = _get_coding_state(uid).get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    snap = await asyncio.to_thread(_project_snapshot, folder)
    src = (req.path or "").strip().replace("\\", "/")
    if not src:
        cands = [p for p in snap
                 if p.lower().endswith(_CODE_EXTS) and "test" not in p.lower()
                 and "spec" not in p.lower() and "node_modules" not in p]
        cands.sort(key=lambda p: -len(snap.get(p, "")))
        src = cands[0] if cands else ""
    if not src or src not in snap:
        return JSONResponse(status_code=400, content={"error": "No source file to test"})
    content = snap[src][:20000]
    try:
        ctx = _rag_context(uid, f"tests for {src}", k=3)
    except Exception:
        ctx = ""
    model = (req.model or "omniilink").strip() or "omniilink"
    messages = [
        {"role": "system", "content": "You write focused, runnable automated tests. "
         "Match the project's existing test framework and style. Output ONLY the "
         "test file contents — no prose, no markdown fences."},
        {"role": "user", "content": f"Write a complete test file for this source file "
         f"({src}). Cover the main behaviours and edge cases.\n\nSOURCE:\n{content}"
         + (f"\n\n{ctx}" if ctx else "")},
    ]
    try:
        resp, used = await asyncio.to_thread(
            generate, messages, model, 0.3, 4000, False, None,
            {"temperature": 0.3, "max_tokens": 4000, "health_scope": "coding"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    text = ((resp or {}).get("choices") or [{}])[0].get("message", {}).get("content") or ""
    # strip a wrapping code fence if the model added one anyway
    m = re.search(r"```[a-zA-Z0-9]*\n([\s\S]*?)```", text)
    if m:
        text = m.group(1)
    if not text.strip():
        return JSONResponse(status_code=500, content={"error": "Model returned no tests"})
    return {"source_path": src, "test_path": _suggest_test_path(src),
            "content": text.strip(), "used": used}


@app.get("/api/coding/search")
async def coding_search(request: Request, q: str, ext: str = "", max: int = 100):
    """Project-wide text search over the open folder (grep). Skips ignored
    dirs, dotfiles, binaries and files over 1MB; caps results and per-file hits."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    query = (q or "").strip()
    if not query or len(query) > 200:
        return JSONResponse(status_code=400, content={"error": "Query must be 1-200 characters"})
    try:
        limit = max(1, min(int(max), 200))
    except Exception:
        limit = 100
    exts = {e.strip().lower().lstrip(".") for e in ext.split(",") if e.strip()} if ext else set()
    needle = query.lower()
    results = []
    per_file_cap = 20
    try:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            for name in filenames:
                if len(results) >= limit:
                    break
                if name.startswith("."):
                    continue
                file_ext = os.path.splitext(name)[1].lower().lstrip(".")
                if exts and file_ext not in exts:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > 1024 * 1024:
                        continue
                    with open(full, "rb") as f:
                        if b"\x00" in f.read(8192):
                            continue
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read(200 * 1024)
                except Exception:
                    continue
                rel = os.path.relpath(full, folder).replace("\\", "/")
                hits = 0
                for i, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        results.append({"path": rel, "line": i, "text": line.strip()[:160]})
                        hits += 1
                        if hits >= per_file_cap or len(results) >= limit:
                            break
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"query": q, "count": len(results), "results": results}


def _coding_git(cwd: str, args: list, timeout: int = 30) -> tuple:
    """Run git inside the project folder. Returns (returncode, output)."""
    try:
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    except FileNotFoundError:
        return 127, "git is not installed on this server"
    except Exception as e:
        return 1, str(e)


@app.get("/api/coding/git/status")
async def coding_git_status(request: Request):
    """Branch, ahead/behind and per-file change lists for the open folder."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if not folder or not os.path.isdir(os.path.join(folder, ".git")):
        return {"git": False, "folder": folder}
    code, out = _coding_git(folder, ["status", "--porcelain", "--branch"])
    if code != 0:
        return {"git": False, "folder": folder, "error": out[:300]}
    branch, ahead, behind = "", 0, 0
    staged, modified, untracked, conflicted = [], [], [], []
    for line in out.splitlines():
        if line.startswith("##"):
            parts = line[2:].strip()
            branch = parts.split("...")[0].strip()
            m = re.search(r"ahead (\d+)", parts)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", parts)
            if m:
                behind = int(m.group(1))
            continue
        if len(line) < 3:
            continue
        x, y, path = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked.append(path)
        elif x == "U" or y == "U" or (x, y) in (("A", "A"), ("D", "D")):
            conflicted.append(path)
        elif x != " ":
            staged.append(path)
        elif y != " ":
            modified.append(path)
    return {"git": True, "branch": branch, "ahead": ahead, "behind": behind,
            "clean": not (staged or modified or untracked or conflicted),
            "staged": staged, "modified": modified, "untracked": untracked,
            "conflicted": conflicted}


@app.get("/api/coding/git/diff")
async def coding_git_diff(request: Request, path: str = ""):
    """Unified diff of the whole tree (staged + unstaged) or of one file."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if not folder or not os.path.isdir(os.path.join(folder, ".git")):
        return JSONResponse(status_code=404, content={"error": "Not a git repository"})
    args = ["diff", "--no-color", "HEAD"]
    if path:
        if _bad_path(path):
            return JSONResponse(status_code=400, content={"error": "Invalid path"})
        full = os.path.join(folder, path)
        if not _is_within(folder, full):
            return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
        args += ["--", path]
    code, out = _coding_git(folder, args, timeout=60)
    if code != 0:
        return JSONResponse(status_code=500, content={"error": out[:500]})
    return {"diff": out[:200000]}


@app.post("/api/coding/git/commit")
async def coding_git_commit(request: Request, req: CodingGitCommit):
    """Commit staged changes. `add: all` stages everything first; `add: none`
    commits only what is already staged."""
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    folder = coding_state.get("folder")
    access_err = _folder_access_error(user, folder, write=True)
    if access_err:
        return access_err
    message = (req.message or "").strip()
    if not message or len(message) > 500:
        return JSONResponse(status_code=400, content={"error": "Commit message required (max 500 chars)"})
    if req.add not in ("all", "none"):
        return JSONResponse(status_code=400, content={"error": "add must be 'all' or 'none'"})
    if not folder or not os.path.isdir(os.path.join(folder, ".git")):
        return JSONResponse(status_code=404, content={"error": "Not a git repository"})
    if req.add == "all":
        code, out = _coding_git(folder, ["add", "-A"])
        if code != 0:
            return JSONResponse(status_code=500, content={"error": out[:500]})
    code, out = _coding_git(folder, ["commit", "-m", message])
    if code != 0:
        return JSONResponse(status_code=409, content={"error": out[:500]})
    logger.info(f"[CODING] git commit user={user.get('username','-')} msg={message[:60]!r}")
    return {"status": "ok", "output": out[:2000]}


def _coding_validate_fetch_url(url: str):
    """SSRF guard for the web-fetch tool: http/https only, no credentials in the
    URL, standard ports only, and nothing that resolves to a private, loopback,
    link-local or reserved address. Returns (parsed_url, error)."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return None, "Invalid URL"
    if u.scheme not in ("http", "https"):
        return None, "Only http/https URLs are allowed"
    if not u.hostname:
        return None, "URL has no host"
    if u.username or u.password:
        return None, "Credentials in the URL are not allowed"
    try:
        port = u.port
    except ValueError:
        return None, "Invalid port"
    if port and port not in (80, 443, 8080):
        return None, "Only ports 80, 443 and 8080 are allowed"
    try:
        infos = socket.getaddrinfo(u.hostname, port or 443)
    except Exception:
        return None, "Host does not resolve"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return None, "Unparseable address"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None, "Private/internal addresses are not allowed"
    return u, None


class _CodingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only through URLs that pass the same SSRF guard."""
    def __init__(self, final_url):
        super().__init__()
        self.final_url = final_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u, verr = _coding_validate_fetch_url(newurl)
        if verr:
            return None
        self.final_url[0] = u.geturl()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@app.get("/api/coding/web-fetch")
async def coding_web_fetch(request: Request, url: str):
    """Fetch a URL for the model (opencode-style web tool). SSRF-guarded,
    redirect-checked, size-capped; HTML is flattened to readable text."""
    user, err = _require_auth(request)
    if err:
        return err
    u, verr = _coding_validate_fetch_url(url)
    if verr:
        return JSONResponse(status_code=400, content={"error": verr})
    final_url = [u.geturl()]
    opener = urllib.request.build_opener(_CodingRedirectHandler(final_url))
    req = urllib.request.Request(
        u.geturl(),
        headers={"User-Agent": "Mozilla/5.0 (compatible; ModellinkCoding/1.0)"})
    try:
        with opener.open(req, timeout=15) as resp:
            ctype = resp.headers.get("Content-Type", "text/plain")
            raw = resp.read(1024 * 1024)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Fetch failed: {e}"})
    text = raw.decode("utf-8", errors="replace")
    if "html" in ctype.lower():
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return {"url": final_url[0], "content_type": ctype, "size": len(raw),
            "text": text[:100000]}


# ==========================================

# API Key Management

# ==========================================
PROVIDER_INFO = {
    "gemini": {"name": "Google Gemini", "color": "#4285F4", "env": "GEMINI_API_KEY"},
    "openai": {"name": "OpenAI", "color": "#10A37F", "env": "OPENAI_API_KEY"},
    "anthropic": {"name": "Anthropic", "color": "#D97706", "env": "ANTHROPIC_API_KEY"},
    "openrouter": {"name": "OpenRouter", "color": "#6366F1", "env": "OPENROUTER_API_KEY"},
    "groq": {"name": "Groq", "color": "#F55036", "env": "GROQ_API_KEY"},
    "mistral": {"name": "Mistral", "color": "#FF7000", "env": "MISTRAL_API_KEY"},
    "together": {"name": "Together AI", "color": "#6366F1", "env": "TOGETHER_API_KEY"},
    "deepseek": {"name": "DeepSeek", "color": "#4F46E5", "env": "DEEPSEEK_API_KEY"},
    "cohere": {"name": "Cohere", "color": "#39D98A", "env": "COHERE_API_KEY"},
    "opencode": {"name": "OpenCode Zen", "color": "#8B5CF6", "env": "OPENCODE_API_KEY"},
    "opencode-go": {"name": "OpenCode Go", "color": "#8B5CF6", "env": "OPENCODE_GO_API_KEY"},
}

@app.get("/api/keys")
async def list_api_keys(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    data = get_user_api_keys(user["id"])
    db_provider_keys = get_user_provider_keys(user["id"])
    providers_with_keys = {}
    for prov_id, prov_info in PROVIDER_INFO.items():
        has_key = prov_id in db_provider_keys and len(db_provider_keys[prov_id]) > 10
        if not has_key and user.get("is_admin"):

            # Server .env keys only ever reveal themselves to an admin: a

            # plain account must not be able to probe which keys the host has.
            env_prefix = prov_info["env"].replace("_API_KEY", "")
            for env_key, env_val in os.environ.items():
                if env_key.startswith(env_prefix) and "_API_KEY" in env_key and env_val and len(env_val) > 10 and not env_val.startswith("your_"):
                    has_key = True
                    break
        providers_with_keys[prov_id] = {
            **prov_info,
            "id": prov_id,
            "configured": has_key,
        }
    return {"keys": data, "providers": providers_with_keys}

@app.post("/api/keys/generate")
async def gen_api_key(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    if len(get_user_api_keys(user["id"])) >= 50:
        return JSONResponse(status_code=400, content={"error": "Key limit reached (50). Revoke unused keys first."})
    entry = generate_api_key(user["id"])
    logger.info(f"[APIKEY] Generated: {entry['key'][:12]}...")
    return entry

@app.post("/api/keys/revoke")
async def rev_api_key(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    body, jerr = await _safe_json(request)
    if jerr:
        return jerr
    key = body.get("key", "")
    if not isinstance(key, str) or not key.startswith("ml-"):
        return JSONResponse(status_code=400, content={"error": "Invalid key"})
    revoke_api_key(user["id"], key)
    logger.info(f"[APIKEY] Revoked: {key[:12]}...")
    return {"status": "ok"}

@app.get("/api/keys/info")
async def api_keys_info(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    base_url = "https://omniilink.dpdns.org"
    try:
        from modellink.splitter import PROVIDERS, MODEL_TIERS
        all_models = []
        for prov_id, prov_cfg in PROVIDERS.items():
            for m in prov_cfg.get("models", []):
                all_models.append({"id": f"{prov_id}/{m}", "provider": prov_id, "name": m})
        return {
            "base_url": base_url,
            "openai_base_url": f"{base_url}/v1",
            "models": all_models,
            "tiers": MODEL_TIERS,
            "providers": {k: {"name": v.get("name", k), "models": v.get("models", [])} for k, v in PROVIDERS.items()},
        }
    except Exception as e:
        return {"base_url": base_url, "openai_base_url": f"{base_url}/v1", "models": [], "tiers": {}, "providers": {}}


# ==========================================

# Project Management

# ==========================================
def _valid_project_name(name: str) -> bool:
    if not name or len(name) > 100:
        return False
    if any(c in name for c in '/\\:*?"<>|'):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    if name in ('.', '..') or '..' in name:
        return False
    if name.startswith('.') or name.startswith('_del_'):
        return False
    return True

def _scan_project_dir(path: str, max_files: int = 50000, max_dirs: int = 20000) -> tuple:
    """(file_count, total_bytes) for a project folder — never raises.

    Uses an explicit stack with a visited-realpath set so Windows directory
    junctions / symlink cycles (common after importing a folder) cannot make
    the walk loop forever or bubble an OSError that would 500 /api/projects
    (the frontend shows that as "Server offline")."""
    file_count = 0
    dir_size = 0
    seen = set()
    stack = [path]
    while stack and file_count < max_files and len(seen) < max_dirs:
        d = stack.pop()
        try:
            key = os.path.normcase(os.path.realpath(d))
            if key in seen:
                continue
            seen.add(key)
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if not any(entry.name.endswith(e) for e in IGNORE_EXTS):
                                file_count += 1
                            dir_size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return file_count, dir_size


def _shared_project_entries(user, projects_dir=""):
    """Projects other accounts shared with this user.

    Returns list entries shaped like /api/projects items plus shared/role/
    owner fields, so every project picker can render and open them directly.
    Folders that vanished (unshared/deleted) are skipped instead of surfacing
    dead cards.
    """
    out = []
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT s.id AS share_id, s.folder, s.role, u.username AS owner"
                " FROM project_shares s LEFT JOIN users u ON u.id = s.owner_id"
                " WHERE s.shared_with_id = ? ORDER BY s.id DESC", (user["id"],)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[PROJECTS] cannot list shares for {user.get('username', '-')}: {e}")
        return out
    for r in rows:
        folder = r["folder"] or ""
        try:
            if not folder or not os.path.isdir(folder):
                continue
            if projects_dir and _is_within(projects_dir, folder):
                continue
            if _folder_shared_role(folder, user["id"]) is None:
                continue
            file_count, dir_size = _scan_project_dir(folder)
            out.append({"name": os.path.basename(os.path.normpath(folder)),
                        "path": folder, "file_count": file_count,
                        "modified": os.path.getmtime(folder),
                        "size_bytes": dir_size, "shared": True,
                        "role": (r["role"] or "edit"),
                        "owner": r["owner"] or "", "share_id": r["share_id"]})
        except Exception as e:
            logger.warning(f"[PROJECTS] skipped shared {folder}: {e}")
            continue
    return out


@app.get("/api/projects")
async def list_projects(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    try:
        projects_dir = _user_projects_dir(user_id)
        names = sorted(os.listdir(projects_dir))
    except Exception as e:
        logger.warning(f"[PROJECTS] cannot list projects for {user_id}: {e}")
        return {"projects": [], "dir": "", "storage_used_bytes": 0,
                "storage_limit_bytes": _user_quota_bytes()}
    projects = []
    total_size = 0
    for name in names:
        if name.startswith("_del_"):
            continue
        path = os.path.join(projects_dir, name)
        try:
            if not os.path.isdir(path):
                continue
            file_count, dir_size = _scan_project_dir(path)
            mtime = os.path.getmtime(path)
            total_size += dir_size
            projects.append({"name": name, "path": path, "file_count": file_count,
                             "modified": mtime, "size_bytes": dir_size})
        except Exception as e:
            logger.warning(f"[PROJECTS] skipped {path}: {e}")
            continue
    return {"projects": projects, "shared_projects": _shared_project_entries(user, projects_dir),
            "public_edits": _public_fork_entries(user),
            "workspaces": _workspace_entries(user),
            "dir": projects_dir, "storage_used_bytes": total_size, "storage_limit_bytes": _user_quota_bytes()}


def _workspace_entries(user):
    """Workspaces the user owns or belongs to (for the project pickers)."""
    out = []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT w.*, u.username AS owner_name FROM workspaces w"
            " LEFT JOIN users u ON u.id = w.owner_id"
            " WHERE w.owner_id = ? OR EXISTS (SELECT 1 FROM workspace_members m"
            "       WHERE m.workspace_id = w.id AND m.user_id = ?)"
            " ORDER BY w.created_at DESC", (user["id"], user["id"])).fetchall()
    finally:
        conn.close()
    for w in rows:
        folder = w["folder"] or ""
        try:
            if not os.path.isdir(folder):
                continue
            fc, ds = _scan_project_dir(folder)
            out.append({"name": w["name"] or os.path.basename(folder.rstrip("\\/")),
                        "path": folder, "workspace": True, "workspace_id": w["id"],
                        "owner": (w["owner_name"] or ""),
                        "role": ("owner" if w["owner_id"] == user["id"]
                                 else (_workspace_role(user, folder) or "view")),
                        "file_count": fc, "size_bytes": ds,
                        "modified": os.path.getmtime(folder),
                        "member_count": len(_ws_members(w["id"]))})
        except Exception:
            continue
    return out


def _public_fork_entries(user):
    """The published-project forks this user owns (Public section entries)."""
    out = []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT f.id AS fork_id, f.name, f.folder, f.submitted, f.created_at,"
            " p.id AS project_id, p.name AS pub_name, p.username AS author, p.allow_edits"
            " FROM public_forks f LEFT JOIN public_projects p ON p.id = f.project_id"
            " WHERE f.user_id = ? ORDER BY f.created_at DESC", (user["id"],)).fetchall()
    finally:
        conn.close()
    for r in rows:
        folder = r["folder"] or ""
        try:
            if not os.path.isdir(folder):
                continue
            file_count, dir_size = _scan_project_dir(folder)
            out.append({"name": r["name"], "path": folder, "file_count": file_count,
                        "size_bytes": dir_size, "modified": os.path.getmtime(folder),
                        "public": True, "project_id": r["project_id"],
                        "pub_name": (r["pub_name"] or ""), "author": (r["author"] or ""),
                        "submitted": bool(r["submitted"]), "fork_id": r["fork_id"],
                        "can_submit": bool(r["allow_edits"])})
        except Exception:
            continue
    return out


@app.get("/api/projects/tree")
async def project_tree(request: Request, name: str = ""):
    """File tree for one saved project (for the publish picker)."""
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    safe = (name or "").strip().replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/") or safe.startswith("."):
        return JSONResponse(status_code=400, content={"error": "Bad project name"})
    path = os.path.join(projects_dir, safe)
    if not os.path.isdir(path) or not _is_within(projects_dir, path):
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    tree = _build_file_tree(path)
    return {"name": safe, "tree": tree}

class ProjectCreate(BaseModel):
    name: str

class ProjectImport(BaseModel):
    name: str
    source_path: str

@app.post("/api/projects/create")
async def create_project(request: Request, req: ProjectCreate):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    name = req.name.strip()
    if not _valid_project_name(name):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    path = os.path.join(projects_dir, name)
    if not _is_within(projects_dir, path) or os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(projects_dir)):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    if os.path.exists(path):
        return JSONResponse(status_code=409, content={"error": "Project already exists"})
    quota_err = _check_quota(user, user_id, 4096)
    if quota_err:
        return quota_err
    os.makedirs(path, exist_ok=True)
    logger.info(f"[PROJECT] Created: {name} user={user.get('username','-')}")
    return {"status": "ok", "name": name, "path": path}

@app.post("/api/projects/import")
async def import_project(request: Request, req: ProjectImport):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    name = req.name.strip()
    source = req.source_path.strip()
    if not _valid_project_name(name):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    if not user.get("is_admin") and not _is_within(_user_data_dir(user_id), source):

# Containment before existence: no oracle for probing other paths
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only import folders from inside your own data directory"})
    if not os.path.isdir(source):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {source}"})
    if not user.get("is_admin") and not _is_within(_user_data_dir(user_id), source):
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only import folders from inside your own data directory"})
    dest = os.path.join(projects_dir, name)
    if not _is_within(projects_dir, dest):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    if os.path.exists(dest):
        return JSONResponse(status_code=409, content={"error": "Project already exists"})
    quota_err = _check_quota(user, user_id, _dir_size(source))
    if quota_err:
        return quota_err
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*IGNORE_DIRS))
    logger.info(f"[PROJECT] Imported: {name} from {source} user={user.get('username','-')}")
    return {"status": "ok", "name": name, "path": dest}

@app.post("/api/projects/open")
async def open_project(request: Request, req: ProjectCreate):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    coding_state = _get_coding_state(user_id)
    name = req.name.strip()
    if not _valid_project_name(name):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    path = os.path.join(projects_dir, name)
    if not _is_within(projects_dir, path) or not os.path.isdir(path):
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    coding_state["folder"] = path
    _save_coding_folder(user_id, path)
    tree = _build_file_tree(path)
    file_count = _count_tree_files(tree)
    logger.info(f"[PROJECT] Opened: {name} ({file_count} files) user={user.get('username','-')}")
    return {"status": "ok", "name": name, "folder": path, "tree": tree, "file_count": file_count}

@app.delete("/api/projects/{name}")
async def delete_project(request: Request, name: str):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    if not _valid_project_name(name):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    path = os.path.join(projects_dir, name)
    return await _do_delete(request, path, name)

class ProjectDelete(BaseModel):
    path: str

@app.post("/api/projects/delete")
async def delete_project_by_path(request: Request, req: ProjectDelete):
    user, err = _require_auth(request)
    if err:
        return err
    path = req.path
    name = os.path.basename(path)
    return await _do_delete(request, path, name)

async def _do_delete(request: Request, path, name):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)

    # Hard containment: only ever delete inside this user's own projects directory
    if not _is_within(projects_dir, path):
        return JSONResponse(status_code=403, content={"error": "Access denied: path is outside your projects directory"})
    if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(projects_dir)):
        return JSONResponse(status_code=403, content={"error": "Cannot delete the projects root"})
    if not _valid_project_name(name):
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    coding_state = _get_coding_state(user_id)
    if not os.path.isdir(path):
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    if coding_state.get("folder") == path:
        coding_state["folder"] = None
        _save_coding_folder(user_id, None)
    import stat, time, subprocess
    def _mark_writable(d):
        for root, dirs, files in os.walk(d):
            for f in files:
                fp = os.path.join(root, f)
                try: os.chmod(fp, stat.S_IWRITE)
                except: pass
            for dd in dirs:
                dp = os.path.join(root, dd)
                try: os.chmod(dp, stat.S_IWRITE)
                except: pass

    # Try 1: direct rmtree with writable fix
    try:
        _mark_writable(path)
        shutil.rmtree(path)
        if not os.path.isdir(path):
            logger.info(f"[PROJECT] Deleted: {name} user={user.get('username','-')}")
            _drop_project_container(user_id, path)
            return {"status": "ok"}
    except: pass

    # Try 2: rename then delete
    import tempfile
    tmp = os.path.join(os.path.dirname(path), f"_del_{name}_{int(time.time())}")
    try:
        os.rename(path, tmp)
        _mark_writable(tmp)
        shutil.rmtree(tmp)
        if not os.path.isdir(tmp) and not os.path.isdir(path):
            logger.info(f"[PROJECT] Deleted (renamed): {name} user={user.get('username','-')}")
            _drop_project_container(user_id, path)
            return {"status": "ok"}
    except: pass

    # Try 3: cmd rmdir (synchronous)
    try:
        result = subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", path],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        if not os.path.isdir(path):
            logger.info(f"[PROJECT] Deleted (cmd): {name} user={user.get('username','-')}")
            _drop_project_container(user_id, path)
            return {"status": "ok"}
    except: pass

    # Folder is locked by a system process. Write a marker file in the

    # projects dir, then hide the folder so it doesn't show in the list.
    marker = os.path.join(os.path.dirname(path), f".pending_delete_{name}")
    try:
        with open(marker, "w") as f:
            f.write(str(time.time()))
    except: pass
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_HIDDEN)
        logger.info(f"[PROJECT] Locked by system, hidden: {name} user={user.get('username','-')}")
    except: pass
    return {"status": "ok", "hidden": True}


# ==========================================

# Admin panel — allowlisted super-admins only

# ==========================================

# Gating: is_admin AND username in OMNIILINK_ADMIN_USERS (default "isaac tribbe,admin").

# Every mutating action is rate limited and appended to user_data/audit.log.

_ADMIN_STARTED_AT = time.time()

# Lives under user_data (a bind-mounted directory): it survives container

# rebuilds without needing a new compose mount, and it is never served —

# static files come from web/ only, and file APIs are confined to each

# account's own subfolder.
_AUDIT_FILE = os.path.join(APP_DIR, "user_data", "audit.log")
_AUDIT_MAX_BYTES = 2 * 1024 * 1024
_ADMIN_JOBS: dict = {}
_ADMIN_JOBS_LOCK = threading.Lock()
_ADMIN_TERMINAL: dict = {}          # username -> current shell cwd
_ADMIN_BUCKETS: dict = {}
_ADMIN_BUCKET_LOCK = threading.Lock()
_ADMIN_CAPS = {"ts": 0.0, "data": None}
_ADMIN_CAPS_LOCK = threading.Lock()
_ADMIN_SHELL_SEM = asyncio.Semaphore(2)


def _admin_allowlist() -> set:
    raw = os.environ.get("OMNIILINK_ADMIN_USERS", "isaac tribbe,admin")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _allowlisted(username: str) -> bool:
    """True if the account matches an allowlist entry, exactly or after
    filesystem-safe normalisation — so an entry of "isaac tribbe" also
    covers a "isaac_tribbe" account (and vice versa)."""
    u = (username or "").strip().lower()
    allow = _admin_allowlist()
    if u in allow:
        return True
    su = _safe_username(u)
    return any(_safe_username(a) == su for a in allow)


def _require_superadmin(request: Request):
    """Return (user, None) or (None, 401/403 JSONResponse).

    Admin AND allowlisted: a second admin account can never open the panel.
    """
    user, err = _require_auth(request)
    if err:
        return None, err
    if not user.get("is_admin"):
        return None, JSONResponse(status_code=403, content={"error": "Admin access required"})
    if not _allowlisted(user.get("username")):
        return None, JSONResponse(status_code=403, content={"error": "Not authorised for the admin panel"})
    return user, None


def _audit(action: str, username, detail: str = "") -> None:
    """Append-only audit trail. Never raises, never blocks a request."""
    try:
        os.makedirs(os.path.dirname(_AUDIT_FILE), exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} user={username or '-'} action={action}"
        if detail:
            line += " " + re.sub(r"[\r\n\t]+", " ", str(detail))[:2000]
        with open(_AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            if os.path.getsize(_AUDIT_FILE) > _AUDIT_MAX_BYTES:
                with open(_AUDIT_FILE, "r", encoding="utf-8", errors="replace") as fh:
                    kept = fh.readlines()[-2000:]
                with open(_AUDIT_FILE, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
        except Exception:
            pass
        log_broadcaster.emit(f"{time.strftime('%H:%M:%S')} [AUDIT] user={username or '-'} {action} {detail}"[:400])
    except Exception:
        pass


def _tail_file(path: str, lines: int = 300) -> str:
    try:
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-max(1, min(int(lines), 2000)):])
    except Exception as exc:
        return f"(could not read {path}: {exc})"


def _clip(text: str, limit: int = 64 * 1024) -> str:
    if not text or len(text) <= limit:
        return text or ""
    half = limit // 2
    return text[:half] + f"\n… [{len(text) - limit} bytes trimmed] …\n" + text[-half:]


def _admin_rate_ok(username: str, limit: int = 60, window: int = 60, scope: str = "") -> bool:

    # Buckets are keyed by (user, scope, limit, window): endpoints that share

    # only limit+window would otherwise eat each other's budget.
    key = f"{username}:{scope}:{limit}:{window}"
    now = time.time()
    with _ADMIN_BUCKET_LOCK:
        hits = [t for t in _ADMIN_BUCKETS.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _ADMIN_BUCKETS[key] = hits
            return False
        hits.append(now)
        _ADMIN_BUCKETS[key] = hits
        return True


def _tool_version(binary: str, args: list) -> Optional[str]:
    if not shutil.which(binary):
        return None
    try:
        proc = subprocess.run([binary] + args, capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or proc.stderr or "").strip().splitlines()
    return first[0][:160] if first else None


def _docker_daemon_up() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def _docker_host_root() -> Optional[str]:
    """Drive/root to bind into the throwaway shell container."""
    root = (os.environ.get("OMNIILINK_SANDBOX_HOST_ROOT", "") or "").strip().replace("\\", "/")
    if not root:
        return None
    m = re.match(r"^([a-zA-Z]:/)", root)
    if m:
        return m.group(1)
    return root.rstrip("/") + "/"


def _updater_image_name() -> str:
    return ((os.environ.get("OMNIILINK_UPDATER_IMAGE", "") or "").strip()
            or "omniilink-updater:latest")


def _updater_image_present() -> bool:
    """The throwaway image the panel update runs in (built by compose)."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", _updater_image_name()],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _docker_socket_present() -> bool:
    try:
        return os.path.exists("/var/run/docker.sock")
    except Exception:
        return False


def _host_checkout() -> Optional[str]:
    """Host-side path of the checkout (C:/Modellink on prod)."""
    for key in ("OMNIILINK_HOST_CHECKOUT", "OMNIILINK_SANDBOX_HOST_ROOT"):
        v = (os.environ.get(key, "") or "").strip().replace("\\", "/").rstrip("/")
        if v:
            return v
    return None


def _git_token() -> str:
    """GitHub token for pulling a private repo. Explicit env wins;
    otherwise the mounted host .env is re-read, so adding the token to
    .env takes effect without restarting the container."""
    tok = (os.environ.get("OMNIILINK_GIT_TOKEN", "") or "").strip()
    if tok:
        return tok
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(os.path.join(APP_DIR, ".env"))
        return ((vals or {}).get("OMNIILINK_GIT_TOKEN", "") or "").strip()
    except Exception:
        return ""


def _write_updater_env_file(token: str):
    """Single-use env file carrying the git token (0600). Returns
    (cli_path, helper_path, name), or (None, None, None) when unusable."""
    if not token or "\n" in token or "\r" in token:
        return None, None, None
    name = ".update-env-" + secrets.token_hex(5)
    cli_path = os.path.join(APP_DIR, "user_data", name)
    try:
        fd = os.open(cli_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"GIT_TOKEN={token}\nGIT_TOKEN_FILE=/work/user_data/{name}\n")
    except Exception:
        return None, None, None
    return cli_path, f"/work/user_data/{name}", name


def _expected_repo() -> str:
    """Comma-separated allowlist of repositories this server may pull from."""
    return ((os.environ.get("OMNIILINK_GIT_REPO", "") or "").strip()
            or ("https://github.com/Omniilink/ModelLink.git,"
                "https://github.com/Tribbninja/ModelLink.git"))


def _build_updater_command(checkout: str, img: str, check: bool = False):
    """docker run string for the throwaway updater. The token (if any)
    travels in a single-use env file, never in this string — it lands in
    job records, the UI and the audit log. Returns
    (command, cli_cleanup_path_or_None).

    check=True runs `omni-update --check` (no docker socket needed): it only
    compares HEAD with origin and reports, it never pulls or recreates."""
    token = _git_token()
    cli_env, _helper_env, _name = _write_updater_env_file(token) if token else (None, None, None)
    parts = ["docker", "run", "--rm", "-i"]
    if not check:
        parts += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    parts += ["-v", f"{checkout}:/work"]
    if cli_env:
        parts += ["--env-file", cli_env]
    parts += ["-e", f"HOST_CHECKOUT={checkout}",
              "-e", f"OMNIILINK_GIT_REPO={_expected_repo()}",
              img, "python3", "/usr/local/bin/omni-update"]
    if check:
        parts.append("--check")
    command = " ".join(shlex.quote(p) for p in parts)
    if cli_env:

        # Best-effort cleanup if the api survives the run; the updater

        # itself deletes the file at startup, so a mid-update recreate

        # cannot leak it either. `$?` is preserved so the job/check exit

        # code still reflects the updater, not the `rm`.
        command += f"; rc=$?; rm -f {shlex.quote(cli_env)}; exit $rc"
    return command, cli_env


def _probe_capabilities() -> dict:
    """Cached environment probe (git / docker / compose / shell mode)."""
    now = time.time()
    with _ADMIN_CAPS_LOCK:
        if _ADMIN_CAPS["data"] and now - _ADMIN_CAPS["ts"] < 300:
            return dict(_ADMIN_CAPS["data"])
    in_container = _api_in_container()
    docker_cli = bool(shutil.which("docker"))
    daemon = _docker_daemon_up() if docker_cli else False
    git_ver = _tool_version("git", ["--version"])
    compose_ver = _tool_version("docker", ["compose", "version"]) or _tool_version("docker-compose", ["version"])
    if not in_container:
        shell_mode, shell_reason = "native", "Commands run directly on this machine."
    elif docker_cli and daemon and _docker_host_root():
        shell_mode = "docker"
        shell_reason = "Commands run in a throwaway container with the host volume mounted at /host."
    elif docker_cli and daemon:
        shell_mode, shell_reason = "none", "OMNIILINK_SANDBOX_HOST_ROOT is not set, so no host volume can be mounted."
    elif docker_cli:
        shell_mode, shell_reason = "none", "The docker CLI is present but the daemon is unreachable."
    else:
        shell_mode, shell_reason = "none", "No shell backend: neither native execution nor docker is available here."
    caps = {
        "in_container": in_container,
        "app_dir": APP_DIR,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "started_at": _ADMIN_STARTED_AT,
        "uptime_seconds": int(now - _ADMIN_STARTED_AT),
        "docker_cli": docker_cli,
        "docker_daemon": daemon,
        "docker_version": _tool_version("docker", ["--version"]),
        "docker_compose": compose_ver,
        "git": bool(git_ver),
        "git_version": git_ver,
        "updater_image": _updater_image_present() if docker_cli else False,
        "updater_image_name": _updater_image_name(),
        "docker_socket": _docker_socket_present(),
        "host_checkout": _host_checkout(),
        "git_token": bool(_git_token()),
        "shell_mode": shell_mode,
        "shell_reason": shell_reason,
        "host_root": _docker_host_root(),
        "sandbox_image": os.environ.get("OMNIILINK_SANDBOX_IMAGE", "omniilink-sandbox:latest"),
        "admin_users": sorted(_admin_allowlist()),
    }
    with _ADMIN_CAPS_LOCK:
        _ADMIN_CAPS["ts"] = time.time()
        _ADMIN_CAPS["data"] = caps
    return dict(caps)




def _docker_shell_exec(command: str, cwd: Optional[str], timeout: int = 30):
    """Full shell over the host volume via a throwaway container."""
    root = _docker_host_root()
    if not root:
        return 127, "", "OMNIILINK_SANDBOX_HOST_ROOT is not configured."
    workdir = "/host"
    if cwd:
        c = str(cwd).replace("\\", "/")
        if c.lower().startswith(root.lower()):
            rel = c[len(root):].strip("/")
            rel = "/".join(p for p in rel.split("/") if p and p != "..")
            workdir = "/host/" + rel if rel else "/host"
    argv = ["docker", "run", "--rm", "-i",
            "-v", f"{root}:/host", "-w", workdir,
            os.environ.get("OMNIILINK_SANDBOX_IMAGE", "omniilink-sandbox:latest"),
            "sh", "-lc", command]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out ({timeout}s)"
    except Exception as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _native_shell_exec(command: str, cwd: Optional[str], timeout: int = 30):
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out ({timeout}s)"
    except Exception as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


_URL_CRED_RE = re.compile(r"(://)[^/@]+@")


def _redact_url(text: str) -> str:
    """Never let credentials embedded in a remote URL (https://<token>@host)
    reach a status field, the panel or the audit log - git echoes the full
    URL in its own error messages."""
    return _URL_CRED_RE.sub(r"\1***@", text or "")


def _start_admin_job(kind: str, command: str, cwd: str, env: Optional[dict] = None) -> str:
    job_id = secrets.token_hex(5)
    job = {"id": job_id, "kind": kind, "command": command, "status": "running",
           "lines": [], "exit_code": None, "started": time.time(), "finished": None, "_proc": None}
    with _ADMIN_JOBS_LOCK:
        for jid in list(_ADMIN_JOBS):
            if len(_ADMIN_JOBS) > 20 and _ADMIN_JOBS[jid].get("status") != "running":
                _ADMIN_JOBS.pop(jid, None)
        _ADMIN_JOBS[job_id] = job

    def _runner():
        try:
            proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace",
                                    bufsize=1, env=env)
            job["_proc"] = proc
            for line in proc.stdout:
                if len(job["lines"]) >= 2000:
                    job["lines"].append("… output truncated (2000 lines) …")
                    break
                job["lines"].append(_redact_url(line.rstrip("\n")))
            proc.wait(timeout=900)
            job["exit_code"] = proc.returncode
            job["status"] = "done" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            try:
                job["_proc"].kill()
            except Exception:
                pass
            job["status"] = "timeout"
            job["lines"].append("Job exceeded the 15 minute limit and was killed.")
        except Exception as exc:
            job["status"] = "failed"
            job["lines"].append(_redact_url(f"error: {exc}"))
        finally:
            job["finished"] = time.time()
            job.pop("_proc", None)

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


def _public_job(job: dict) -> dict:
    out = {k: job.get(k) for k in ("id", "kind", "command", "status", "lines",
                                   "exit_code", "started", "finished")}
    out["command"] = _redact_url(out.get("command") or "")
    return out


def _safe_job(job_id: str) -> Optional[dict]:
    with _ADMIN_JOBS_LOCK:
        return _ADMIN_JOBS.get(job_id)


@app.get("/api/admin/error-reports")
async def admin_error_reports(request: Request, status: str = "open"):
    user, err = _require_superadmin(request)
    if err:
        return err
    status = (status or "open").strip().lower()
    conn = get_db()
    try:
        if status in ("open", "resolved"):
            rows = conn.execute(
                "SELECT id, user_id, username, context, provider, model, error, note, status, created_at"
                " FROM error_reports WHERE status = ? ORDER BY id DESC LIMIT 200",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, username, context, provider, model, error, note, status, created_at"
                " FROM error_reports ORDER BY id DESC LIMIT 200").fetchall()
        open_count = conn.execute(
            "SELECT COUNT(*) AS c FROM error_reports WHERE status = 'open'").fetchone()["c"]
        return {"reports": [dict(r) for r in rows], "open_count": open_count}
    finally:
        conn.close()


@app.post("/api/admin/error-reports/{report_id}/resolve")
async def admin_error_report_resolve(request: Request, report_id: int):
    user, err = _require_superadmin(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    want = "resolved" if bool((data or {}).get("resolved", True)) else "open"
    conn = get_db()
    try:
        cur = conn.execute("UPDATE error_reports SET status = ? WHERE id = ?", (want, report_id))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "Report not found"})
        return {"status": "ok", "id": report_id, "new_status": want}
    finally:
        conn.close()



# ==========================================

# Remote pairing agents: run commands on a client computer

# ==========================================
_AGENTS_PATH = os.path.join(APP_DIR, "user_data", "agents.json")
_AGENT_LOCK = threading.Lock()
_AGENT_CMD_TIMEOUT = 300
_AGENT_OUT_MAX = 65536


def _agents_blank():
    return {"agents": {}, "pairing": {}, "commands": {}, "next_agent": 1, "next_command": 1}


def _agents_load():
    try:
        with open(_AGENTS_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            return _agents_blank()
        for k in ("agents", "pairing", "commands"):
            if not isinstance(st.get(k), dict):
                st[k] = {}
        st.setdefault("next_agent", 1)
        st.setdefault("next_command", 1)
        return st
    except (OSError, ValueError):
        return _agents_blank()


def _agents_save(st):
    try:
        os.makedirs(os.path.dirname(_AGENTS_PATH), exist_ok=True)
        tmp = _AGENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, _AGENTS_PATH)
    except OSError:
        pass


def _agent_online(a):
    """True when the companion polled within the last 75 seconds."""
    try:
        seen = float(a.get("last_seen") or 0)
    except (TypeError, ValueError):
        seen = 0
    return bool(seen and time.time() - seen < 75)


def _agent_public(a, viewer=None):
    owner_name = a.get("owner_name", "")
    if not owner_name:
        owner_name = _agent_owner_username(_agent_owner_id(a))
    out = {"id": a.get("id", ""), "name": a.get("name", ""), "hostname": a.get("hostname", ""),
            "platform": a.get("platform", ""), "created_at": a.get("created_at", ""),
            "last_seen": a.get("last_seen", 0), "online": _agent_online(a),
            "owner": owner_name, "owner_id": _agent_owner_id(a),
            "is_owner": _agent_is_owner(viewer, a), "caps": a.get("caps") or []}
    # Only the owner needs to manage (or even see) the explicit share list.
    if _agent_is_owner(viewer, a):
        out["access"] = "owner"
        out["shared_with"] = [_agent_owner_username(uid) or f"user {uid}"
                              for uid in _agent_shared_ids(a)]
    else:
        try:
            out["access"] = ("shared" if int((viewer or {}).get("id") or 0)
                             in _agent_shared_ids(a) else "admin")
        except (TypeError, ValueError):
            out["access"] = "admin"
    return out


def _agent_from_token(request):
    auth = request.headers.get("authorization", "") or ""
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    h = hashlib.sha256(parts[1].encode()).hexdigest()
    with _AGENT_LOCK:
        st = _agents_load()
        for a in st["agents"].values():
            if a.get("token_hash") == h:
                return dict(a)
    return None


def _agent_owner_id(agent) -> int:
    try:
        return int((agent or {}).get("owner_id") or 0)
    except (TypeError, ValueError):
        return 0


def _agent_shared_ids(agent) -> list:
    """User ids explicitly granted access to someone else's computer."""
    try:
        shared = (agent or {}).get("shared_with") or []
        if isinstance(shared, dict):
            shared = list(shared.keys())
        return sorted({int(x) for x in shared})
    except (TypeError, ValueError):
        return []


def _agent_is_owner(user, agent) -> bool:
    if not user or not isinstance(agent, dict):
        return False
    try:
        return _agent_owner_id(agent) == int(user.get("id") or 0)
    except (TypeError, ValueError):
        return False


def _agent_visible(user, agent):
    """Computers a caller may list and use.

    Paired computers are strictly private: the owner plus users the owner
    explicitly shares with. Server admins do NOT inherit access; the opt-in
    OMNIILINK_ADMIN_DEVICE_ACCESS=1 escape hatch is the only exception, for
    account recovery/support, and it must be deliberately enabled. Unknown
    agents are invisible (404, never 403) so ids cannot be probed.
    """
    if not user or not isinstance(agent, dict):
        return False
    if _agent_is_owner(user, agent):
        return True
    try:
        if int(user.get("id") or 0) in _agent_shared_ids(agent):
            return True
    except (TypeError, ValueError):
        return False
    if user.get("is_admin"):
        return os.environ.get("OMNIILINK_ADMIN_DEVICE_ACCESS") == "1"
    return False


def _agent_owner_username(user_id: int) -> str:
    try:
        row = get_user_by_id(int(user_id))
    except Exception:
        return ""
    return str((row or {}).get("username") or "")


_AGENT_POLL_SECONDS = 25
_AGENT_WAITERS = {}
_CMD_WAITERS = {}


def _agent_take_queued(agent_id):
    """Oldest queued command for the agent, marked running.
    Returns (command_dict_or_None, agent_known_bool). Ephemeral relay
    commands (device Ollama) come first — they never touch agents.json."""
    with _AGENT_LOCK:
        st = _agents_load()
        cur = st["agents"].get(str(agent_id))
        if not cur:
            return None, False
        cur["last_seen"] = time.time()
        relay_cmd = relay.take_pending(agent_id)
        if relay_cmd is not None:
            _agents_save(st)
            return relay_cmd, True
        nxt = None
        for c in st["commands"].values():
            if str(c.get("agent_id")) == str(agent_id) and c.get("status") == "queued":
                if nxt is None or int(c.get("id", 0)) < int(nxt.get("id", 0)):
                    nxt = c
        if nxt is not None:
            nxt["status"] = "running"
        _agents_save(st)
        if nxt is None:
            return None, True
        out = {"id": nxt["id"], "kind": nxt.get("kind") or "shell",
               "command": nxt.get("command") or "", "timeout": _AGENT_CMD_TIMEOUT}
        if isinstance(nxt.get("payload"), dict):
            out["payload"] = nxt["payload"]
        return out, True


def _agent_notify(agent_id):
    """Wake long-polling agents the moment a command is queued (single
    uvicorn worker, so in-process events are enough)."""
    for ev in list(_AGENT_WAITERS.get(str(agent_id), ())):
        try:
            ev.set()
        except Exception:
            pass


def _relay_agent_info(agent_id):
    """relay.py's view of an agent: online flag, display name, capabilities."""
    with _AGENT_LOCK:
        st = _agents_load()
        a = st["agents"].get(str(agent_id))
        if not a:
            return None
        return {"online": _agent_online(a),
                "name": a.get("name") or a.get("hostname") or str(agent_id),
                "caps": a.get("caps") or []}


relay.set_notifier(_agent_notify)
relay.set_agent_info(_relay_agent_info)


@app.post("/api/agents/pairing")
async def agents_pairing(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _optional_json_body(request)
    if bad:
        return bad
    data = data or {}
    owner_name = str(data.get("owner_username") or user.get("username") or "").strip().lower()
    target = _share_target_user(owner_name) if owner_name else None
    if target is None:
        return JSONResponse(status_code=404, content={"error": "Device-owner account not found"})
    if not user.get("is_admin") and int(target["id"]) != int(user.get("id") or 0):
        return JSONResponse(status_code=403, content={
            "error": "Pairing codes can only create computers owned by your own account"})
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(10))
    now = time.time()
    with _AGENT_LOCK:
        st = _agents_load()
        # Expired invitations are single-use secrets: prune them so a leaked
        # code cannot linger indefinitely alongside active invitations.
        st["pairing"] = {k: p for k, p in st["pairing"].items()
                         if float((p or {}).get("expires", 0)) >= now}
        st["pairing"][code] = {"code": code, "created": now, "expires": now + 600,
                               "claimed": False,
                               "created_by": user["id"],
                               "created_by_name": user.get("username", ""),
                               "owner_id": int(target["id"]),
                               "owner_name": target["username"]}
        _agents_save(st)
    base = str(request.base_url or "").strip().rstrip("/")
    return {"code": code, "expires_in": 600, "server_hint": base,
            "owner": target["username"],
            "warning": ("This code gives '%s' full control over the computer that claims it. "
                        "Only share it with the owner of that computer.") % target["username"]}


@app.get("/api/agents")
async def agents_list(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    with _AGENT_LOCK:
        st = _agents_load()
        agents = [_agent_public(a, user) for a in st["agents"].values()
                  if _agent_visible(user, a)]
    agents.sort(key=lambda a: str(a["id"]))
    return {"agents": agents}


def _queue_agent_command(user, agent_id, command, kind, payload):
    """Queue a shell/file command. Returns (command_id, None) or
    (None, error JSONResponse)."""
    with _AGENT_LOCK:
        st = _agents_load()
        a = st["agents"].get(str(agent_id))
        if a is None or not _agent_visible(user, a):
            return None, JSONResponse(status_code=404, content={"error": "Agent not found"})
        cid = str(st.get("next_command", 1))
        st["next_command"] = int(cid) + 1
        st["commands"][cid] = {"id": cid, "agent_id": str(agent_id), "command": command,
                               "kind": kind, "payload": payload,
                               "status": "queued", "exit_code": None, "output": "",
                               "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": ""}
        _agents_save(st)
    return cid, None


@app.post("/api/agents/{agent_id}/exec")
async def agents_exec(request: Request, agent_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 60, 60, "agents"):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    command = str((data or {}).get("command", "") or "").strip()
    if not command or len(command) > 8000:
        return JSONResponse(status_code=400, content={"error": "Empty or oversized command"})
    cwd = str((data or {}).get("cwd", "") or "")
    if len(cwd) > 1024:
        return JSONResponse(status_code=400, content={"error": "Path too long"})
    cid, err2 = _queue_agent_command(user, agent_id, command, "shell",
                                     {"cwd": cwd} if cwd else None)
    if err2:
        return err2
    _agent_notify(agent_id)
    _audit("agent.exec", user["username"], f"agent={agent_id} cmd={command[:120]}")
    return {"status": "ok", "command_id": cid}


@app.post("/api/agents/{agent_id}/file")
async def agents_file(request: Request, agent_id: str):
    """Queue a file operation for a paired computer (the companion app
    executes it jailed inside its projects folder). Ops: list, tree, read,
    write, mkdir, delete. The result arrives as JSON through the commands feed."""
    user, err = _require_auth(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 120, 60, "agents"):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    op = str(data.get("op", "") or "").strip().lower()
    if op not in ("list", "tree", "read", "write", "mkdir", "delete"):
        return JSONResponse(status_code=400, content={"error": "Unknown file op"})
    rel = str(data.get("path", "") or "")
    if len(rel) > 1024:
        return JSONResponse(status_code=400, content={"error": "Path too long"})
    payload = {"op": op, "path": rel}
    if op == "write":
        content = data.get("content", "")
        if not isinstance(content, str) or len(content) > 1024 * 1024:
            return JSONResponse(status_code=400, content={"error": "Content missing or over 1MB"})
        payload["content"] = content
    cid, err2 = _queue_agent_command(user, agent_id, "", "file", payload)
    if err2:
        return err2
    _agent_notify(agent_id)
    _audit("agent.file", user["username"], f"agent={agent_id} op={op} path={rel[:160]}")
    return {"status": "ok", "command_id": cid}


@app.get("/api/agents/{agent_id}/commands")
async def agents_commands(request: Request, agent_id: str, limit: int = 20):
    user, err = _require_auth(request)
    if err:
        return err
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    with _AGENT_LOCK:
        st = _agents_load()
        a = st["agents"].get(str(agent_id))
        if a is None or not _agent_visible(user, a):
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        rows = [c for c in st["commands"].values() if str(c.get("agent_id")) == str(agent_id)]
    rows.sort(key=lambda c: int(c.get("id", 0)), reverse=True)
    return {"commands": rows[:limit]}


@app.get("/api/agents/{agent_id}/commands/{command_id}/wait")
async def agents_command_wait(request: Request, agent_id: str, command_id: str,
                              timeout: int = 25):
    """Hold until one queued command finishes (or the timeout), so browsers
    make one long request per operation instead of polling the feed several
    times a second."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        timeout = max(1, min(int(timeout or 25), 60))
    except (TypeError, ValueError):
        timeout = 25
    cid = str(command_id)

    def _done():
        with _AGENT_LOCK:
            st = _agents_load()
            a = st["agents"].get(str(agent_id))
            if a is None or not _agent_visible(user, a):
                return "gone", None
            c = st["commands"].get(cid)
            if c is None or str(c.get("agent_id")) != str(agent_id):
                return "gone", None
            if c.get("status") == "done":
                return "done", {"status": "done",
                                "exit_code": c.get("exit_code"),
                                "output": c.get("output") or ""}
        return "pending", None

    state, payload = _done()
    if state == "gone":
        return JSONResponse(status_code=404, content={"error": "Command not found"})
    if state == "done":
        return payload
    ev = asyncio.Event()
    _CMD_WAITERS.setdefault(cid, set()).add(ev)
    try:
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        left = _CMD_WAITERS.get(cid)
        if left is not None:
            left.discard(ev)
            if not left:
                _CMD_WAITERS.pop(cid, None)
    state, payload = _done()
    if state == "gone":
        return JSONResponse(status_code=404, content={"error": "Command not found"})
    if state == "done":
        return payload
    return {"status": "pending"}


@app.delete("/api/agents/{agent_id}")
async def agents_delete(request: Request, agent_id: str):
    user, err = _require_auth(request)
    if err:
        return err
    with _AGENT_LOCK:
        st = _agents_load()
        a = st["agents"].get(str(agent_id))
        if a is None or not _agent_visible(user, a):
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        del st["agents"][str(agent_id)]
        st["commands"] = {k: c for k, c in st["commands"].items() if str(c.get("agent_id")) != str(agent_id)}
        _agents_save(st)
    try:
        relay.drop_agent(agent_id)
    except Exception:
        pass
    _audit("agent.remove", user["username"], f"agent={agent_id}")
    return {"status": "ok"}


def _agent_owner_record(user, agent_id):
    """Load the agent store and return (store, agent) only for its owner."""
    st = _agents_load()
    a = st["agents"].get(str(agent_id))
    if a is None or not _agent_is_owner(user, a):
        return None, None
    return st, a


def _agent_share_names(agent) -> list:
    return [_agent_owner_username(uid) or f"user {uid}"
            for uid in _agent_shared_ids(agent)]


@app.post("/api/agents/{agent_id}/share")
async def agents_share(request: Request, agent_id: str):
    """Explicitly grant another account access to one paired computer.

    Sharing is deliberately account-to-account and computer-specific: it does
    not expose any other device, project, chat, or setting belonging to either
    user. Only the computer's owner can grant access.
    """
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    username = str((data or {}).get("username") or "").strip().lower()
    if not username:
        return JSONResponse(status_code=400, content={"error": "Username is required"})
    target = _share_target_user(username)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    with _AGENT_LOCK:
        st, a = _agent_owner_record(user, agent_id)
        if a is None:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        if int(target["id"]) == _agent_owner_id(a):
            return JSONResponse(status_code=400, content={"error": "That user already owns this computer"})
        shared = set(_agent_shared_ids(a))
        shared.add(int(target["id"]))
        a["shared_with"] = sorted(shared)
        _agents_save(st)
        names = _agent_share_names(a)
    _audit("agent.share", user["username"], f"agent={agent_id} shared_with={target['username']}")
    return {"status": "ok", "shared_with": names}


@app.delete("/api/agents/{agent_id}/share/{username}")
async def agents_unshare(request: Request, agent_id: str, username: str):
    user, err = _require_auth(request)
    if err:
        return err
    target_name = str(username or "").strip().lower()
    if not target_name:
        return JSONResponse(status_code=400, content={"error": "Username is required"})
    with _AGENT_LOCK:
        st, a = _agent_owner_record(user, agent_id)
        if a is None:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        target = _share_target_user(target_name)
        if target is None:
            return JSONResponse(status_code=404, content={"error": "User not found"})
        shared = set(_agent_shared_ids(a))
        shared.discard(int(target["id"]))
        a["shared_with"] = sorted(shared)
        _agents_save(st)
        names = _agent_share_names(a)
    _audit("agent.unshare", user["username"], f"agent={agent_id} unshared={target['username']}")
    return {"status": "ok", "shared_with": names}


@app.post("/api/agents/{agent_id}/transfer")
async def agents_transfer(request: Request, agent_id: str):
    """Move computer ownership to another account and remove prior shares.

    This is the recovery path when a computer was claimed under the wrong
    invitation: the current owner can hand it to the person who physically
    controls it, after which the previous owner immediately loses access.
    """
    user, err = _require_auth(request)
    if err:
        return err
    data, bad = await _safe_json(request)
    if bad:
        return bad
    username = str((data or {}).get("username") or "").strip().lower()
    if not username:
        return JSONResponse(status_code=400, content={"error": "Username is required"})
    target = _share_target_user(username)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    with _AGENT_LOCK:
        st, a = _agent_owner_record(user, agent_id)
        if a is None:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        if int(target["id"]) == _agent_owner_id(a):
            return JSONResponse(status_code=400, content={"error": "That user already owns this computer"})
        old_owner = a.get("owner_name", "")
        a["owner_id"] = int(target["id"])
        a["owner_name"] = target["username"]
        a["shared_with"] = []
        a["transferred_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        a["transferred_by"] = user.get("username", "")
        _agents_save(st)
    _audit("agent.transfer", user["username"],
           f"agent={agent_id} from={old_owner} to={target['username']}")
    return {"status": "ok", "owner": target["username"]}


@app.get("/api/agents/client")
async def agents_client(request: Request):
    """Headless agent script, server URL baked in. Any signed-in user."""
    user, err = _require_auth(request)
    if err:
        return err
    base = str(request.base_url or "").strip().rstrip("/")
    script = AGENT_CLIENT_PY.replace("__SERVER__", base or "https://YOUR-SERVER")
    return PlainTextResponse(script, headers={"Content-Disposition": "attachment; filename=omniilink-agent.py"})


@app.get("/api/agents/companion")
async def agents_companion(request: Request):
    """Windows companion installer (MSI with tray EXE). Any signed-in user,
    or a paired agent bearer token (self-update)."""
    user, err = _require_auth(request)
    if err:
        agent = _agent_from_token(request)
        if agent is None:
            return err
    p = os.path.join(APP_DIR, "companion", "OmniilinkServerSetup.msi")
    if not os.path.isfile(p):
        return JSONResponse(status_code=404, content={"error": "Companion installer not built yet"})
    return FileResponse(p, media_type="application/x-msi",
                        filename="OmniilinkServerSetup.msi",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                 "Pragma": "no-cache"})


@app.get("/api/agents/companion-version")
async def agents_companion_version(request: Request):
    """Published companion version. Public: the tray polls it to offer
    in-place updates without a reinstall."""
    ver = ""
    try:
        with open(os.path.join(APP_DIR, "companion", "VERSION"), "r", encoding="utf-8") as f:
            ver = (f.read() or "").strip()
    except OSError:
        pass
    return {"version": ver or "0.0.0"}
@app.post("/api/agent/claim")
async def agent_claim(request: Request):
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    code = str(data.get("code", "") or "").strip().upper()
    name = str(data.get("name", "") or "").strip()[:64] or "client"
    hostname = str(data.get("hostname", "") or "").strip()[:128]
    platform_name = str(data.get("platform", "") or "").strip()[:128]
    if not code:
        return JSONResponse(status_code=400, content={"error": "Missing pairing code"})
    ip = _request_ip(request)
    if not _admin_rate_ok(ip or "unknown-claim-ip", 20, 300, "agent_claim"):
        return JSONResponse(status_code=429, content={"error": "Too many pairing attempts. Try again later."})
    now = time.time()
    with _AGENT_LOCK:
        st = _agents_load()
        p = st["pairing"].get(code)
        if not p or p.get("claimed") or float(p.get("expires", 0)) < now:
            return JSONResponse(status_code=404, content={"error": "Invalid or expired pairing code"})
        p["claimed"] = True
        token = secrets.token_hex(32)
        aid = str(st.get("next_agent", 1))
        st["next_agent"] = int(aid) + 1
        owner_id = p.get("owner_id", p.get("created_by"))
        owner_name = p.get("owner_name", p.get("created_by_name", ""))
        st["agents"][aid] = {"id": aid, "name": name, "hostname": hostname, "platform": platform_name,
                             "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                             "owner_id": owner_id,
                             "owner_name": owner_name,
                             "created_by": p.get("created_by"),
                             "created_by_name": p.get("created_by_name", ""),
                             "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "claimed_ip": ip,
                             "shared_with": [],
                             "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "last_seen": 0}
        # Claiming consumes the invitation and prunes any other expired codes.
        st["pairing"].pop(code, None)
        st["pairing"] = {k: q for k, q in st["pairing"].items()
                         if float((q or {}).get("expires", 0)) >= now}
        _agents_save(st)
    _audit("agent.pair", str(owner_name or "-"), f"agent={aid} host={hostname} owner={owner_name}")
    return {"status": "ok", "agent_id": aid, "token": token, "owner": owner_name}


@app.get("/api/agent/pending")
async def agent_pending(request: Request, caps: str = ""):
    a = _agent_from_token(request)
    if not a:
        return JSONResponse(status_code=401, content={"error": "Bad agent token"})
    if caps:

        # Capability handshake: new companions announce e.g. ?caps=ollama on

        # every poll; old ones never send it and stay un-relay-capable.
        with _AGENT_LOCK:
            st = _agents_load()
            cur = st["agents"].get(str(a["id"]))
            if cur:
                cur["caps"] = sorted({c for c in str(caps).split(",") if c})
                cur["last_seen"] = time.time()
                _agents_save(st)
    nxt, known = _agent_take_queued(a["id"])
    if not known:
        return JSONResponse(status_code=401, content={"error": "Unknown agent"})
    if nxt is not None:
        return {"command": nxt}

    # Long-poll: hold the request until a command lands (or timeout) so

    # pickup is instant instead of one poll interval late.
    ev = asyncio.Event()
    key = str(a["id"])
    _AGENT_WAITERS.setdefault(key, set()).add(ev)
    try:
        nxt, known = _agent_take_queued(a["id"])
        if nxt is None and known:
            try:
                await asyncio.wait_for(ev.wait(), timeout=_AGENT_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            nxt, known = _agent_take_queued(a["id"])
    finally:
        waiters = _AGENT_WAITERS.get(key)
        if waiters is not None:
            waiters.discard(ev)
            if not waiters:
                _AGENT_WAITERS.pop(key, None)
    if not known:
        return JSONResponse(status_code=401, content={"error": "Unknown agent"})
    if nxt is None:
        return {"command": None}
    return {"command": nxt}


@app.post("/api/agent/result")
async def agent_result(request: Request):
    a = _agent_from_token(request)
    if not a:
        return JSONResponse(status_code=401, content={"error": "Bad agent token"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    data = data or {}
    cid = str(data.get("command_id", "") or "")
    with _AGENT_LOCK:
        st = _agents_load()
        c = st["commands"].get(cid)
        if not c or str(c.get("agent_id")) != str(a["id"]):
            return JSONResponse(status_code=404, content={"error": "Unknown command"})
        try:
            c["exit_code"] = int(data.get("exit_code", 0))
        except (TypeError, ValueError):
            c["exit_code"] = 0
        c["output"] = str(data.get("output", "") or "")[:_AGENT_OUT_MAX]
        c["status"] = "done"
        c["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if a["id"] in st["agents"]:
            st["agents"][a["id"]]["last_seen"] = time.time()
        _agents_save(st)
    for ev in list(_CMD_WAITERS.pop(cid, ())):
        try:
            ev.set()
        except Exception:
            pass
    return {"status": "ok"}


@app.post("/api/agent/relay")
async def agent_relay(request: Request):
    """Companion -> host: one seq-numbered batch of an Ollama relay stream.
    HTTP 410 means the stream is gone (finished, dropped, or cancelled):
    abort the local Ollama read immediately."""
    a = _agent_from_token(request)
    if not a:
        return JSONResponse(status_code=401, content={"error": "Bad agent token"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    if not relay.attach(a["id"], data or {}):
        return JSONResponse(status_code=410, content={"error": "stream gone"})
    return {"status": "ok"}


AGENT_CLIENT_PY = '''#!/usr/bin/env python3
"""Omniilink pairing agent: runs shell commands from your Omniilink server.
Usage: python omniilink-agent.py --server https://YOUR-SERVER --code ABC123 [--name my-pc]
Needs only the Python standard library. Polls every few seconds; press Ctrl+C to stop.
Also relays a local Ollama (http://127.0.0.1:11434, override with --ollama-url
or OMNIILINK_OLLAMA_URL) so its models can be used through the server.
"""
import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

SERVER = "__SERVER__"
OLLAMA_URL = (os.environ.get("OMNIILINK_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
RELAY_PATHS = ("/api/tags", "/api/show", "/api/chat", "/v1/chat/completions", "/api/version", "/api/pull")


# Socket deadlines. The server holds GET /api/agent/pending open for up to

# ~25s waiting for a command, so every deadline here must clear that with

# headroom: a shorter one reports a perfectly healthy idle poll as

# "The read operation timed out".
HTTP_TIMEOUT = 75
LONG_TIMEOUT = 120


def api(method, path, token=None, payload=None, timeout=None):
    req = urllib.request.Request(SERVER + path, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OmniilinkAgent/1.0"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    try:

        # The server long-polls /api/agent/pending for ~25s, so the default

        # deadline must clear that with headroom or a healthy idle poll

        # surfaces as a socket read timeout.
        with urllib.request.urlopen(req, data=body,
                                    timeout=timeout or HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise Exception("HTTP %s: %s" % (e.code, detail.strip() or e.reason))
    except (socket.timeout, TimeoutError):
        raise TimeoutError("socket read timed out")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise TimeoutError("socket read timed out")
        raise


_relay_conn = None
_relay_conn_path = "/api/agent/relay"
_relay_conn_lock = threading.Lock()


def _relay_status(token, payload):
    """POST one relay batch; returns the HTTP status (410 = stream gone).

    Reuses one keep-alive connection: a streamed reply is dozens of small
    batches, and a fresh TCP+TLS handshake per batch dominates the round
    trip. A server-side close just triggers one transparent reconnect.
    """
    global _relay_conn, _relay_conn_path
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OmniilinkAgent/1.0",
               "Content-Length": str(len(body)), "Connection": "keep-alive",
               "Authorization": "Bearer " + token}
    with _relay_conn_lock:
        for attempt in (0, 1):
            if _relay_conn is None:
                import http.client
                from urllib.parse import urlparse
                u = urlparse(SERVER.rstrip("/") + "/api/agent/relay")
                port = u.port or (443 if u.scheme == "https" else 80)
                cls = (http.client.HTTPSConnection if u.scheme == "https"
                       else http.client.HTTPConnection)
                _relay_conn = cls(u.hostname or "", port, timeout=HTTP_TIMEOUT)
                _relay_conn_path = u.path or "/api/agent/relay"
            try:
                _relay_conn.request("POST", _relay_conn_path, body=body, headers=headers)
                r = _relay_conn.getresponse()
                r.read()
                return r.status
            except Exception:
                try:
                    _relay_conn.close()
                except Exception:
                    pass
                _relay_conn = None
                if attempt == 0:
                    continue
                return 0


def run_relay(token, cmd, ollama_url):
    """Service one kind=ollama command against the local Ollama, streaming
    seq-numbered batches back to the host."""
    cid = cmd.get("id")
    payload = cmd.get("payload") or {}
    path = str(payload.get("path") or "")
    body = payload.get("body")
    state = {"seq": 0}

    def send(**fields):
        fields["command_id"] = cid
        fields["seq"] = state["seq"]
        state["seq"] += 1
        st = _relay_status(token, fields)
        if st in (404, 410):
            raise RuntimeError("gone")
        if st and st != 200:
            raise RuntimeError("relay rejected (HTTP %s)" % st)
        if not st:
            raise RuntimeError("host unreachable")

    if path not in RELAY_PATHS:
        send(error="relay path not allowed: " + path)
        return
    url = (ollama_url or "http://127.0.0.1:11434").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except Exception as e:
        send(error="Ollama unreachable on this computer: " + str(e)[:300])
        return
    try:
        status = resp.getcode() or 200
        headers = {"content-type": resp.headers.get("Content-Type") or "application/json"}
        stream_intent = isinstance(body, dict) and body.get("stream") is True
        if status != 200 or not stream_intent:
            raw = resp.read(8 * 1024 * 1024)
            send(status=status, headers=headers, body=raw.decode("utf-8", "replace"),
                 done=True)
            return
        send(status=status, headers=headers)
        buf, size = [], 0
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                continue
            buf.append(text)
            size += len(text)
            if size >= 32768:
                send(lines=buf)
                buf, size = [], 0
        if buf:
            send(lines=buf)
        send(done=True)
    except RuntimeError:
        raise
    except Exception as e:
        try:
            send(error="relay failed: " + str(e)[:300])
        except Exception:
            pass
    finally:
        try:
            resp.close()
        except Exception:
            pass


def main():
    global SERVER
    ap = argparse.ArgumentParser(description="Omniilink pairing agent")
    ap.add_argument("--server", default=SERVER, help="Omniilink server URL")
    ap.add_argument("--code", required=True, help="10-character pairing code from Admin > Agents")
    ap.add_argument("--name", default=socket.gethostname(), help="Display name for this computer")
    ap.add_argument("--ollama-url", default=OLLAMA_URL, help="Local Ollama URL for model relay")
    args = ap.parse_args()
    SERVER = args.server.rstrip("/")
    ollama_url = (args.ollama_url or "").rstrip("/")
    hostname = socket.gethostname()
    plat = platform.system() + " " + platform.release()
    print("pairing with " + SERVER + " ...")
    try:
        d = api("POST", "/api/agent/claim", None,
                {"code": args.code, "name": args.name, "hostname": hostname, "platform": plat})
    except Exception as e:
        print("pairing failed: " + str(e))
        return 1
    token = d["token"]
    print("paired as agent " + str(d["agent_id"]) + " - polling for commands (Ctrl+C to stop)")
    while True:
        try:
            p = api("GET", "/api/agent/pending?caps=ollama", token,
                    timeout=LONG_TIMEOUT)
            cmd = p.get("command")
            if cmd and cmd.get("kind") == "ollama":
                threading.Thread(target=run_relay, args=(token, cmd, ollama_url),
                                 daemon=True).start()
            elif cmd:
                print("$ " + cmd["command"])
                try:
                    proc = subprocess.run(cmd["command"], shell=True, capture_output=True,
                                          text=True, timeout=int(cmd.get("timeout") or 300))
                    out = (proc.stdout or "") + (proc.stderr or "")
                    api("POST", "/api/agent/result", token,
                        {"command_id": cmd["id"], "exit_code": proc.returncode,
                         "output": out[-65536:]})
                    print("exit " + str(proc.returncode))
                except subprocess.TimeoutExpired:
                    api("POST", "/api/agent/result", token,
                        {"command_id": cmd["id"], "exit_code": 124, "output": "[timed out]"})
                except Exception as e:
                    api("POST", "/api/agent/result", token,
                        {"command_id": cmd["id"], "exit_code": 1, "output": "agent error: " + str(e)})
            else:
                time.sleep(5)
        except KeyboardInterrupt:
            print("stopped")
            return 0
        except TimeoutError:

            # A stalled read is a network hiccup, not a broken pairing: back

            # off briefly and keep polling rather than printing an alarming

            # read-timeout error on every cycle.
            time.sleep(3)
        except Exception as e:
            print("poll error: " + str(e))
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
'''


@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    caps = await asyncio.to_thread(_probe_capabilities)
    conn = get_db()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        admins = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
        sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions WHERE expires_at > datetime('now')").fetchone()["c"]
        reqs_24h = conn.execute("SELECT COUNT(*) AS c FROM usage_events WHERE ts >= datetime('now','-24 hours')").fetchone()["c"]
        tokens_24h = conn.execute("SELECT COALESCE(SUM(prompt_tokens + completion_tokens),0) AS c "
                                  "FROM usage_events WHERE ts >= datetime('now','-24 hours')").fetchone()["c"]
        errors_24h = conn.execute("SELECT COUNT(*) AS c FROM usage_events "
                                  "WHERE ts >= datetime('now','-24 hours') AND status >= 400").fetchone()["c"]
    finally:
        conn.close()
    running = [j for j in list(_ADMIN_JOBS.values()) if j.get("status") == "running"]
    return {
        "counts": {"users": users, "admins": admins, "sessions": sessions,
                   "requests_24h": reqs_24h, "tokens_24h": tokens_24h, "errors_24h": errors_24h},
        "capabilities": caps,
        "jobs": [_public_job(j) for j in running],
        "model": MODEL_NAME,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/admin/users")
async def admin_users(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 60, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT u.id, u.username, u.name, u.is_admin, u.created_at,"
            " (SELECT COUNT(*) FROM api_keys k WHERE k.user_id = u.id) AS api_keys,"
            " (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.expires_at > datetime('now')) AS sessions,"
            " (SELECT COUNT(*) FROM usage_events e WHERE e.user_id = u.id) AS requests,"
            " (SELECT COALESCE(SUM(e.prompt_tokens + e.completion_tokens),0) FROM usage_events e WHERE e.user_id = u.id) AS tokens"
            " FROM users u ORDER BY u.id"
        ).fetchall()
        return {"users": [dict(r) for r in rows], "allowlist": sorted(_admin_allowlist())}
    finally:
        conn.close()


@app.post("/api/admin/users/{user_id}/admin")
async def admin_set_admin(request: Request, user_id: int):
    actor, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(actor["username"], 20, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    want = 1 if bool((data or {}).get("is_admin")) else 0
    conn = get_db()
    try:
        target = conn.execute("SELECT id, username, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return JSONResponse(status_code=404, content={"error": "User not found"})
        if not want and target["is_admin"]:
            admins = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
            if admins <= 1:
                return JSONResponse(status_code=400, content={"error": "Cannot remove the last admin"})
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (want, user_id))
        conn.commit()
    finally:
        conn.close()
    _audit("user.admin_set", actor["username"], f"target={user_id} is_admin={want}")
    return {"ok": True, "is_admin": bool(want)}


@app.post("/api/admin/users/{user_id}/revoke-sessions")
async def admin_revoke_sessions(request: Request, user_id: int):
    actor, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(actor["username"], 20, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    revoke_user_sessions(user_id, None)
    _audit("user.revoke_sessions", actor["username"], f"target={user_id}")
    return {"ok": True}


@app.get("/api/admin/usage")
async def admin_usage(request: Request, range: str = "24h"):
    user, err = _require_superadmin(request)
    if err:
        return err
    hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(range, 24)
    data = get_usage(user_id=None, range_hours=hours)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT u.username, u.is_admin, COUNT(e.id) AS requests,"
            " COALESCE(SUM(e.prompt_tokens),0) AS prompt_tokens,"
            " COALESCE(SUM(e.completion_tokens),0) AS completion_tokens,"
            " COALESCE(SUM(e.duration_ms),0) AS duration_ms"
            " FROM users u LEFT JOIN usage_events e ON e.user_id = u.id AND e.ts >= datetime('now', ?)"
            " GROUP BY u.id ORDER BY requests DESC LIMIT 100",
            (f"-{hours} hours",)
        ).fetchall()
        data["by_user"] = [dict(r) for r in rows]
    finally:
        conn.close()
    data["range"] = range
    return data


@app.get("/api/admin/logs")
async def admin_logs(request: Request, lines: int = 300):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 120, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    return {"path": "modellink/server.log", "lines": _tail_file(os.path.join(APP_DIR, "modellink", "server.log"), lines)}


@app.get("/api/admin/audit")
async def admin_audit(request: Request, lines: int = 300):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 120, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    return {"path": "user_data/audit.log", "lines": _tail_file(_AUDIT_FILE, lines)}


@app.get("/api/admin/terminal/cwd")
async def admin_terminal_cwd(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    caps = await asyncio.to_thread(_probe_capabilities)
    return {"cwd": _ADMIN_TERMINAL.get(user["username"]) or os.getcwd(),
            "mode": caps["shell_mode"]}


@app.get("/api/admin/terminal/history")
async def admin_terminal_history(request: Request, lines: int = 100):
    user, err = _require_superadmin(request)
    if err:
        return err
    tail = _tail_file(_AUDIT_FILE, max(1, min(int(lines), 500)))
    cmds = [ln for ln in tail.splitlines() if "action=terminal" in ln]
    return {"history": cmds[-100:]}


@app.post("/api/admin/terminal/execute")
async def admin_terminal_execute(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 60, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit: 60 commands per minute"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    command = str((data or {}).get("command") or "").strip()
    if not command:
        return JSONResponse(status_code=400, content={"error": "Empty command"})
    if len(command) > 8000 or "\x00" in command:
        return JSONResponse(status_code=400, content={"error": "Command too long or contains a null byte"})
    caps = await asyncio.to_thread(_probe_capabilities)
    mode = caps.get("shell_mode")
    if mode == "none":
        return JSONResponse(status_code=503, content={"error": "No shell backend: " + caps.get("shell_reason", "")})
    uname = user["username"]
    requested = (data or {}).get("cwd")
    cwd = str(requested) if requested else _ADMIN_TERMINAL.get(uname)
    if cwd and not os.path.isdir(cwd):
        cwd = None


    # Persistent `cd` for the native backend (each command is its own shell).
    cd_match = re.fullmatch(r"\s*cd\s*(.*?)\s*", command) if mode == "native" else None
    if cd_match and ";" not in command and "&&" not in command:
        target = cd_match.group(1).strip().strip('"').strip("'")
        base = cwd or os.getcwd()
        cand = os.path.realpath(target if (target and os.path.isabs(target)) else os.path.join(base, target or "."))
        if os.path.isdir(cand):
            _ADMIN_TERMINAL[uname] = cand
            _audit("terminal", uname, command)
            return {"stdout": "", "stderr": "", "exit_code": 0, "cwd": cand, "mode": mode}
        _audit("terminal", uname, command + " -> failed (no such directory)")
        return {"stdout": "", "stderr": f"cd: no such directory: {target}", "exit_code": 1,
                "cwd": base, "mode": mode}

    _audit("terminal", uname, command)
    timeout = 30 if mode == "native" else 45
    async with _ADMIN_SHELL_SEM:
        if mode == "native":
            code, out, errout = await asyncio.to_thread(_native_shell_exec, command, cwd, timeout)
            if cwd:
                _ADMIN_TERMINAL[uname] = cwd
        else:
            code, out, errout = await asyncio.to_thread(_docker_shell_exec, command, cwd, timeout)
    return {"stdout": _clip(out), "stderr": _clip(errout), "exit_code": code,
            "cwd": _ADMIN_TERMINAL.get(uname) or cwd or os.getcwd(), "mode": mode,
            "timeout": code == 124}


@app.get("/api/admin/docker/status")
async def admin_docker_status(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 30, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    caps = await asyncio.to_thread(_probe_capabilities)
    if not caps.get("docker_cli"):
        return {"available": False, "reason": "The docker CLI is not installed in this environment.",
                "containers": [], "capabilities": caps}
    if not caps.get("docker_daemon"):
        return {"available": False, "reason": "The docker daemon is not reachable from this environment.",
                "containers": [], "capabilities": caps}
    try:
        proc = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}"],
                              capture_output=True, text=True, timeout=15, errors="replace")
        containers = []
        for ln in (proc.stdout or "").splitlines():
            parts = ln.split("\t")
            if parts and parts[0]:
                containers.append({"name": parts[0], "image": parts[1] if len(parts) > 1 else "",
                                   "status": parts[2] if len(parts) > 2 else "",
                                   "state": parts[3] if len(parts) > 3 else ""})
        return {"available": True, "containers": containers, "capabilities": caps}
    except Exception as exc:
        return {"available": False, "reason": str(exc), "containers": [], "capabilities": caps}


@app.post("/api/admin/docker/restart")
async def admin_docker_restart(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 10, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    name = str((data or {}).get("name") or "").strip()
    if not name or len(name) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", name):
        return JSONResponse(status_code=400, content={"error": "Invalid container name"})
    caps = await asyncio.to_thread(_probe_capabilities)
    if not caps.get("docker_cli") or not caps.get("docker_daemon"):
        return JSONResponse(status_code=503, content={"error": "Docker is unavailable here."})
    listing = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=15, errors="replace")
    known = {ln.strip() for ln in (listing.stdout or "").splitlines() if ln.strip()}
    if name not in known:
        return JSONResponse(status_code=404, content={"error": "No such container"})
    _audit("docker.restart", user["username"], name)
    proc = subprocess.run(["docker", "restart", "-t", "15", name],
                          capture_output=True, text=True, timeout=60, errors="replace")
    return {"ok": proc.returncode == 0, "name": name,
            "output": _clip((proc.stdout or "") + (proc.stderr or ""), 4096)}


@app.post("/api/admin/server/restart")
async def admin_server_restart(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(user["username"], 5, 300):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    caps = await asyncio.to_thread(_probe_capabilities)
    if not (caps.get("in_container") and caps.get("docker_cli") and caps.get("docker_daemon")):
        return JSONResponse(status_code=409, content={
            "error": "Restart is only available when running in Docker. Locally, restart run-server.py manually."})
    try:
        with open("/etc/hostname", "r") as fh:
            cid = fh.read().strip()[:12]
    except Exception:
        cid = ""
    if not re.fullmatch(r"[0-9a-f]{6,12}", cid):
        return JSONResponse(status_code=409, content={"error": "Could not determine this container's id."})
    _audit("server.restart", user["username"], cid)
    proc = subprocess.run(["docker", "restart", "-t", "15", cid],
                          capture_output=True, text=True, timeout=60, errors="replace")
    return {"ok": proc.returncode == 0, "container": cid,
            "output": _clip((proc.stdout or "") + (proc.stderr or ""), 4096)}


@app.get("/api/admin/jobs/{job_id}")
async def admin_job_status(request: Request, job_id: str):
    user, err = _require_superadmin(request)
    if err:
        return err
    job = _safe_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Unknown job"})
    return _public_job(job)


@app.post("/api/admin/jobs/{job_id}/cancel")
async def admin_job_cancel(request: Request, job_id: str):
    user, err = _require_superadmin(request)
    if err:
        return err
    job = _safe_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Unknown job"})
    proc = job.get("_proc")
    if proc and job.get("status") == "running":
        try:
            proc.kill()
            job["status"] = "cancelled"
            job["lines"].append("Cancelled by administrator.")
        except Exception as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
    _audit("job.cancel", user["username"], job_id)
    return _public_job(job)


def _admin_page(request: Request):
    user, err = _require_superadmin(request)
    if err:
        if getattr(err, "status_code", None) == 401:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse("<!doctype html><meta charset='utf-8'><title>403</title>"
                            "<body style='font-family:system-ui;background:#0b0d10;color:#e6e9ef;padding:40px'>"
                            "<h1>403 Forbidden</h1><p>You are not authorised to open the admin panel.</p>"
                            "<p><a href='/' style='color:#7aa2f7'>Back to Omniilink</a></p>", status_code=403)
    return FileResponse(os.path.join(APP_DIR, "web", "admin.html"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})



# Registered BEFORE the /static mount below, so /static/admin.html can never

# be served by the public static handler.
@app.get("/static/admin.html")
async def serve_admin_static(request: Request):
    return _admin_page(request)


@app.get("/static/components/ModelPreviewGallery.js")
async def serve_model_gallery():
    """The Models gallery component lives at the repo root (components/), so
    it is not inside web/ and the /static mount cannot reach it. Serve it
    explicitly. Registered before the mount below."""
    p = os.path.join(APP_DIR, "components", "ModelPreviewGallery.js")
    if not os.path.isfile(p):
        return JSONResponse(status_code=404, content={"error": "Component not installed"})
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/admin")
async def serve_admin(request: Request):
    return _admin_page(request)


@app.get("/")
async def serve_index():
    landing_path = os.path.join(APP_DIR, "web", "landing.html")
    return FileResponse(landing_path, headers={"Cache-Control": "public, max-age=600"})

@app.get("/signin")
async def serve_signin():
    p = os.path.join(APP_DIR, "web", "signin.html")
    return FileResponse(p, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

@app.get("/signup")
async def serve_signup():
    p = os.path.join(APP_DIR, "web", "signup.html")
    return FileResponse(p, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

@app.get("/menu")
async def serve_menu():
    p = os.path.join(APP_DIR, "web", "menu.html")
    return FileResponse(p, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

@app.get("/favicon.svg")
async def serve_favicon():
    favicon_path = os.path.join(APP_DIR, "web", "favicon.svg")
    return FileResponse(favicon_path, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/favicon.ico")
async def serve_favicon_ico():
    favicon_path = os.path.join(APP_DIR, "web", "favicon.ico")
    return FileResponse(favicon_path, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})

web_dir = os.path.join(APP_DIR, "web")
if os.path.isdir(web_dir):
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

@app.get("/{path:path}")
async def serve_spa(path: str):

    # Unknown API paths must be a 404, not the HTML shell - a client would

    # otherwise treat index.html as a successful JSON answer.
    if path == "api" or path.startswith("api/"):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    index_path = os.path.join(APP_DIR, "web", "index.html")
    return FileResponse(index_path, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="127.0.0.1", port=port, server_header=False, date_header=False)
