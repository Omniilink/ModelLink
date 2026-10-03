import sys
import os
import re
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
    Prefers the git sha; the Docker image has no git binary, so fall back
    to a hash of this file's contents (changes with every backend edit)."""
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
        with open(__file__, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
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
import json
import socket
import ipaddress
import urllib.request
import urllib.parse

from modellink.splitter import (
    generate, PROVIDERS, MODEL_NAME, lb,
    load_all_keys, MODEL_RATE_LIMITS,
    scan_ollama_models, get_backup_model, OLLAMA_BASE_URL,
    get_language, set_language, normalize_level, EFFORT_LEVELS,
    provider_health
)
from modellink.auth import (
    init_db, get_current_user, require_auth, create_user, authenticate_user,
    create_session, destroy_session, set_session_cookie, get_user_by_id,
    update_user_name, update_user_password, generate_api_key, get_user_api_keys,
    revoke_api_key, get_user_provider_keys, save_user_provider_key,
    delete_user_provider_key, get_custom_providers, get_custom_provider,
    save_custom_provider, delete_custom_provider, get_user_by_google_sub,
    username_taken, set_user_google_sub, get_db, get_user_from_session,
    revoke_user_sessions, record_usage, get_usage, get_api_key_id_by_key,
    all_usernames, validate_api_key
)

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
BW_RATE_MBPS = float(os.environ.get("OMNIILINK_BW_RATE_MBPS", "10") or 10)
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
        transfer = path.startswith(BW_TRANSFER_PREFIXES)
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
    connect = "connect-src 'self' cloudflareinsights.com" + (f" {extra}" if extra else "")
    return ("default-src 'self'; script-src 'self' 'unsafe-inline' static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: *.ytimg.com; "
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
        is_preview = scope.get("path", "") == "/api/coding/preview"
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

def _user_data_dir(user_id: str) -> str:
    """Get or create per-user data directory (user_id must already be sanitized)."""
    safe = _safe_username(user_id)
    d = os.path.join(APP_DIR, "user_data", safe)
    os.makedirs(d, exist_ok=True)
    return d

def _user_projects_dir(user_id: str) -> str:
    """Get per-user projects directory."""
    d = os.path.join(_user_data_dir(user_id), "projects")
    os.makedirs(d, exist_ok=True)
    return d

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


def _coding_version_dir(user_id: str, path: str) -> str:
    """One directory per tracked file. The key is a hash of the project-relative
    path so separators and odd characters can never escape the user dir."""
    import hashlib
    key = hashlib.sha1((path or "").encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(_coding_versions_dir(user_id), key)


def _coding_list_versions(user_id: str, path: str) -> list:
    out = []
    try:
        d = _coding_version_dir(user_id, path)
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


def _coding_get_version(user_id: str, path: str, ts: float) -> Optional[str]:
    try:
        prefix = f"{float(ts):.6f}_"
        for name in os.listdir(_coding_version_dir(user_id, path)):
            if not name.startswith(prefix):
                continue
            full = os.path.join(_coding_version_dir(user_id, path), name)
            with open(full, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if isinstance(rec, dict) and "content" in rec:
                return rec["content"]
    except Exception:
        pass
    return None


def _coding_prune_versions(user_id: str, path: str) -> None:
    """Keep the newest N per file, then the newest overall under the size cap."""
    d = _coding_version_dir(user_id, path)
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


def _coding_save_version(user_id: str, path: str, content: str) -> None:
    """Snapshot content BEFORE an edit. No-ops are skipped; oldest versions are
    pruned once the per-file or total cap is hit."""
    if not content:
        return
    try:
        d = _coding_version_dir(user_id, path)
        os.makedirs(d, exist_ok=True)
        ts = time.time()
        rec = {"ts": ts, "size": len(content.encode("utf-8", "ignore")), "content": content}
        with open(os.path.join(d, f"{ts:.6f}_{secrets.token_hex(4)}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f)
        _coding_prune_versions(user_id, path)
    except Exception:
        pass

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

# The language value is interpolated into the system prompt, so it is
# validated against a strict alphabet instead of being trusted.
_LANGUAGE_RE = re.compile(r"[A-Za-z]{3,20}")

def _user_language(user_id: str) -> str:
    lang = _load_user_prefs(user_id).get("language")
    if isinstance(lang, str) and _LANGUAGE_RE.fullmatch(lang.strip()):
        return lang.strip()
    return get_language()

def _get_coding_state(user_id: str) -> dict:
    if user_id not in _coding_states:
        settings = _load_coding_settings(user_id, {
            "auto_create": False,
            "auto_edit": False,
            "auto_command": False,
            "plan_mode": False,
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

def _google_redirect_uri() -> str:
    return os.environ.get("GOOGLE_REDIRECT_URI", "https://omniilink.dpdns.org/api/auth/google/callback")

@app.get("/api/auth/oauth-status")
async def oauth_status():
    return {"google": _google_oauth_configured()}

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
        for m in ollama_models:
            if not any(x["id"] == m for x in all_models):
                all_models.append({"id": m, "provider": "ollama", "source": "local"})
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

@app.get("/api/ollama/models")
async def ollama_models(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    models = scan_ollama_models()
    return {"models": models, "available": len(models) > 0, "base_url": OLLAMA_BASE_URL}


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

@app.get("/api/settings")
async def get_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    db_provider_keys = get_user_provider_keys(user["id"])
    settings = {
        "backup_model": get_backup_model(),
        "ollama_url": OLLAMA_BASE_URL,
        "language": _user_language(_safe_username(user["username"])),
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

def _split_used_model(used_model: str):
    """'groq/qwen3.6-27b' -> ('groq', 'qwen3.6-27b')."""
    if used_model and "/" in used_model:
        provider, _, model = used_model.partition("/")
        return provider or "unknown", model or used_model
    return "unknown", used_model or "unknown"


@app.get("/api/usage")
async def api_usage(request: Request, range: str = "24h"):
    user, err = _require_auth(request)
    if err:
        return err
    hours = {"1h": 1, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(range, 24)
    try:
        data = get_usage(user_id=user["id"], range_hours=hours)
        data["rate_limits"] = lb.rate_limiter.get_status()
        data["model_name"] = MODEL_NAME
        return data
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

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
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") \
                or not isinstance(m.get("content"), str) or len(m["content"]) > 500000:
            return JSONResponse(status_code=400, content={"error": "Invalid message entry"})

    temp = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 1000)
    is_stream = body.get("stream", False)
    req_model = body.get("model", MODEL_NAME)
    if not isinstance(req_model, str) or not re.fullmatch(r"[a-zA-Z0-9._/-]{1,80}", req_model):
        return JSONResponse(status_code=400, content={"error": "Invalid model name"})
    try:
        temp = max(0.0, min(2.0, float(temp)))
        max_tokens = max(1, min(32000, int(max_tokens)))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "Invalid temperature or max_tokens"})
    is_stream = bool(is_stream)

    # Optional generation controls from the UI menus: invalid values are
    # coerced to "auto" (provider default) instead of rejecting the request.
    effort = normalize_level(body.get("effort"), "auto")
    thinking = normalize_level(body.get("thinking"), "auto")
    req_opts = {"temperature": temp, "max_tokens": max_tokens, "effort": effort, "thinking": thinking,
                "language": _user_language(_safe_username(user["username"])),
                "health_scope": health_scope(request)}

    # Key policy: admin uses the server's .env keys; everyone else must use their own keys.
    user_keys = None
    if not user.get("is_admin"):
        db_keys = get_user_provider_keys(user["id"])
        db_customs = get_custom_providers(user["id"])
        if not db_keys and not db_customs:
            return JSONResponse(status_code=403, content={"error": "No API keys configured. Add your providers under Providers."})
        user_keys = db_keys

    auth_header = request.headers.get("Authorization", "")
    api_key_id = None
    if auth_header.startswith("Bearer ml-"):
        api_key_id = get_api_key_id_by_key(auth_header[7:])

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
        provider, model_name = _split_used_model(used_model)
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
            _finish_usage("unknown/unknown", completion_tokens=0, status=500)
            async def error_event_gen():
                chat_id = f"chatcmpl-{int(time.time())}"
                created_time = int(time.time())
                error_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created_time, "model": req_model,
                    "choices": [{"index": 0, "delta": {"content": f"Error: {str(e)}"}, "finish_reason": "stop"}]
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
            chat_id = f"chatcmpl-{int(time.time())}"
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
                    for chunk in stream:
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
            _finish_usage("unknown/unknown", status=500)
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

def _build_file_tree(root_path, prefix=""):
    entries = []
    try:
        items = sorted(os.listdir(root_path))
    except PermissionError:
        return entries
    dirs = []
    files = []
    for name in items:
        full = os.path.join(root_path, name)
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
        children = _build_file_tree(sub_path, prefix + d + "/")
        if children:
            entries.append({"name": d, "type": "dir", "path": prefix + d, "children": children})
    for f in files:
        size = os.path.getsize(os.path.join(root_path, f))
        entries.append({"name": f, "type": "file", "path": prefix + f, "size": size})
    return entries


def _count_tree_files(entries):
    """Count file entries in a _build_file_tree result (matches the tree)."""
    n = 0
    for e in entries or []:
        if e.get("type") == "dir":
            n += _count_tree_files(e.get("children") or [])
        else:
            n += 1
    return n

DEFAULT_CODING_PROMPT = """You are an expert coding assistant inside the Omniilink Coding Mode. You can create, edit, delete files and run commands. The user sees your explanations; a parser executes your blocks.

ENVIRONMENT (memorize this):
- The server runs on WINDOWS. Commands execute in cmd.exe, already a shell.
- Working directory is the open project folder. All paths are relative to it.
- NEVER prefix commands with `cmd /c` — it is already implied and breaks parsing.
- NEVER wrap a command in quotes. Write it bare on one single line.
- One command per RUN_COMMAND block. Chain with `&&` if needed.
- Forward slashes work in paths (e.g. `type src/app.js`).

COMMAND REFERENCE (use exactly these forms):
- List files: dir /b
- List with sizes: dir
- Show a file: type path\\to\\file
- Make a folder: mkdir foldername
- Delete a file: del filename
- Run Python: python filename.py  (check first: python --version)
- Run JavaScript: node filename.js  (check first: node --version)
- Packages: pip install name  /  npm install
- Git: git status / git add -A / git commit -m "msg"
- Fetch a web page: WEB_FETCH:https://example.com (below)

BLOCK SYNTAX (exact — the parser is strict):
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
4. After creating code, run it to verify (python/node) and report the output.
5. Deliverables (code files) MUST go in FILE_CREATE/FILE_EDIT blocks — never
bare fenced code alone, or the system cannot save them.
6. NEVER describe a task as done/finished/complete while any [ ] item
remains. The system independently verifies that every file you name
actually exists AND is non-empty — false completion claims are rejected
and sent back.
7. Keep chat text SHORT (2-4 sentences). NEVER paste file contents into
   chat text — code lives in FILE_CREATE/FILE_EDIT blocks only.

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

# Per-user coding states handled by _get_coding_state()

def _folder_access_error(user, folder):
    """Return a JSONResponse error if the user may not access folder, else None."""
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    if not user.get("is_admin"):
        if not _is_within(_user_data_dir(_safe_username(user["username"])), folder):
            return JSONResponse(status_code=403, content={"error": "Access denied: folder is outside your data directory"})
    return None

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
    if not user.get("is_admin") and not _is_within(_user_data_dir(user_id), folder):
        # Containment before existence: no oracle for probing other paths
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only open folders inside your own data directory"})
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {folder}"})
    if not user.get("is_admin") and not _is_within(_user_data_dir(user_id), folder):
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only open folders inside your own data directory"})
    coding_state["folder"] = folder
    _save_coding_folder(user_id, folder)
    tree = _build_file_tree(folder)
    file_count = _count_tree_files(tree)
    logger.info(f"[CODING] Opened folder: {folder} ({file_count} files) user={user.get('username','-')}")
    return {"status": "ok", "folder": folder, "tree": tree, "file_count": file_count}

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
    tree = _build_file_tree(folder)
    return {"folder": folder, "tree": tree}

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
        content = _rewrite_preview_assets(content, path, user["id"])
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


def _rewrite_preview_assets(content: bytes, page_path: str, user_id=None) -> bytes:
    """Rewrite relative asset URLs in a previewed HTML page to absolute
    preview-asset URLs (with a signed token), so linked CSS/JS/images actually
    load inside the opaque-origin iframe. Covers quoted, unquoted and srcset
    attributes plus inline <style> url()/@import. External, data:, fragment
    and javascript: URLs are left untouched."""
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
        norm = posixpath.normpath(posixpath.join("/" + page_dir, ref)).lstrip("/")
        if not norm or norm == ".":
            return None
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
                + _rewrite_css_urls(m.group(2), page_dir, user_id)
                + m.group(3))

    text = _QUOTED_ASSET_RE.sub(repl_quoted, text)
    text = _UNQUOTED_ASSET_RE.sub(repl_unquoted, text)
    text = _SRCSET_RE.sub(repl_srcset, text)
    text = _STYLE_BLOCK_RE.sub(repl_style, text)
    return text.encode("utf-8")


def _rewrite_css_urls(css_text: str, css_dir: str, user_id=None) -> str:
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
        norm = posixpath.normpath(posixpath.join("/" + css_dir, ref)).lstrip("/")
        if not norm or norm == ".":
            return None
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
    access_err = _folder_access_error(user, folder)
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
    access_err = _folder_access_error(user, folder)
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
    access_err = _folder_access_error(user, folder)
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
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    sdir, meta = _load_upload_meta(user_id, req.upload_id or "")
    if meta is None:
        return JSONResponse(status_code=404, content={"error": "Unknown or expired upload session"})
    rel = meta.get("rel", "")
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
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if _bad_path(req.path) or len(req.content) > 5 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "Invalid path or content too large (max 5MB)"})
    quota_err = _check_quota(user, _safe_username(user["username"]), len(req.content.encode("utf-8", "ignore")))
    if quota_err:
        return quota_err
    full = os.path.join(folder, req.path)
    if not _is_within(folder, full):
        return JSONResponse(status_code=403, content={"error": "Path outside project folder"})
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        backup = None
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                backup = f.read()
            _coding_save_version(_safe_username(user["username"]), req.path, backup)
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
        logger.info(f"[CODING] Wrote file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(req.content),
                "backed_up": backup is not None, "added": added, "removed": removed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

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
    access_err = _folder_access_error(user, folder)
    if access_err:
        return access_err
    if (_bad_path(req.path) or len(req.old_text) > 5 * 1024 * 1024
            or len(req.new_text) > 5 * 1024 * 1024):
        return JSONResponse(status_code=400, content={"error": "Invalid path or text too large (max 5MB)"})
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
        idx = current.find(old)
        if idx == -1:
            # Forgiving retry: ignore trailing-whitespace differences
            old_stripped = "\n".join(line.rstrip() for line in old.split("\n"))
            cur_lines = current.split("\n")
            found_at = -1
            want = old_stripped.split("\n")
            for s in range(len(cur_lines) - len(want) + 1):
                if all(cur_lines[s + k].rstrip() == want[k] for k in range(len(want))):
                    found_at = s
                    break
            if found_at == -1:
                return JSONResponse(status_code=404, content={"error": "Old text not found in file — it may have changed; re-read it first"})
            end_line = found_at + len(want)
            patched = "\n".join(cur_lines[:found_at] + [new] + cur_lines[end_line:])
        else:
            patched = current[:idx] + new + current[idx + len(old):]
        quota_err = _check_quota(user, _safe_username(user["username"]), max(0, len(patched) - len(current)))
        if quota_err:
            return quota_err
        _coding_save_version(_safe_username(user["username"]), req.path, current)
        with open(full, "w", encoding="utf-8") as f:
            f.write(patched)
        try:
            import difflib
            dl = list(difflib.unified_diff(current.splitlines(), patched.splitlines(), lineterm=""))
            added = sum(1 for l in dl if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in dl if l.startswith("-") and not l.startswith("---"))
        except Exception:
            added, removed = 0, 0
        logger.info(f"[CODING] Patched file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(patched),
                "added": added, "removed": removed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _coding_version_checks(user, folder, path):
    """Shared guard for the version endpoints: auth, open folder, path safety."""
    access_err = _folder_access_error(user, folder)
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
            "versions": _coding_list_versions(_safe_username(user["username"]), path)}


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
    content = _coding_get_version(_safe_username(user["username"]), path, ts)
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
    guard = _coding_version_checks(user, folder, req.path)
    if guard:
        return guard
    full = os.path.join(folder, req.path)
    if not os.path.isfile(full):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    content = _coding_get_version(_safe_username(user["username"]), req.path, req.ts)
    if content is None:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()
        if current != content:
            _coding_save_version(_safe_username(user["username"]), req.path, current)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[CODING] Restored file: {req.path} user={user.get('username','-')}")
        return {"status": "ok", "path": req.path, "size": len(content)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/coding/file-delete")
async def coding_file_delete(request: Request, path: str):
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
    try:
        os.remove(full)
        logger.info(f"[CODING] Deleted file: {path} user={user.get('username','-')}")
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
        base = ytdlp + ["--no-playlist", "--no-warnings"]
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
    try:
        idle_minutes = int(os.environ.get("OMNIILINK_PROJ_IDLE_MIN", "0") or 0)
    except ValueError:
        idle_minutes = 0
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
            with _proj_lock:
                _proj_containers[name] = time.time()
            return True, ""
        if r.returncode == 0:
            # Exists but stopped: revive (keeps installed packages)
            r2 = subprocess.run(["docker", "start", name],
                                capture_output=True, text=True, timeout=60)
            if r2.returncode == 0:
                with _proj_lock:
                    _proj_containers[name] = time.time()
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
        with _proj_lock:
            _proj_containers[name] = time.time()
        _maybe_start_reaper(cfg.get("idle_minutes", 0))
        return True, ""
    except Exception as e:
        return False, str(e)[-500:]


def _exec_project_container(name: str, command: str, timeout: int):
    with _proj_lock:
        _proj_containers[name] = time.time()
    return subprocess.run(["docker", "exec", "-w", "/work", name,
                           "sh", "-c", command],
                          capture_output=True, text=True, timeout=timeout)


def _maybe_start_reaper(idle_minutes: int):
    """Idle reaper: STOP (not remove, so installed layers survive) project
    containers unused for idle_minutes. 0 disables. Started once."""
    global _reaper_started
    if not idle_minutes or _reaper_started:
        return
    _reaper_started = True

    def _loop():
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
    if user.get("is_admin"):
        cwd = req.cwd or folder or os.getcwd()
        if not os.path.isdir(cwd):
            cwd = os.getcwd()
    else:
        # Restricted shell: the command always runs inside the user's own
        # data dir. Client-supplied cwd is validated, never trusted, and
        # absolute/parent paths in the command are rejected.
        base = _user_data_dir(user_id)
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

@app.get("/api/coding/settings")
async def coding_get_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    coding_state = _get_coding_state(_safe_username(user["username"]))
    settings = dict(coding_state["settings"])
    settings["sandbox"] = _sandbox_config() is not None
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
        if k in coding_state["settings"]:
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
    access_err = _folder_access_error(user, folder)
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
    "opencode": {"name": "OpenCode", "color": "#8B5CF6", "env": "OPENCODE_API_KEY"},
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

@app.get("/api/projects")
async def list_projects(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    user_id = _safe_username(user["username"])
    projects_dir = _user_projects_dir(user_id)
    projects = []
    total_size = 0
    for name in sorted(os.listdir(projects_dir)):
        if name.startswith("_del_"): continue
        path = os.path.join(projects_dir, name)
        if not os.path.isdir(path): continue
        if os.path.isdir(path):
            file_count = 0
            dir_size = 0
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    if not any(f.endswith(e) for e in IGNORE_EXTS):
                        file_count += 1
                    try:
                        dir_size += os.path.getsize(fp)
                    except: pass
            mtime = os.path.getmtime(path)
            total_size += dir_size
            projects.append({"name": name, "path": path, "file_count": file_count, "modified": mtime, "size_bytes": dir_size})
    return {"projects": projects, "dir": projects_dir, "storage_used_bytes": total_size, "storage_limit_bytes": _user_quota_bytes()}

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
# Gating: is_admin AND username in OMNIILINK_ADMIN_USERS (default "isaac tribbe").
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
    raw = os.environ.get("OMNIILINK_ADMIN_USERS", "isaac tribbe")
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


def _agent_public(a):
    now = time.time()
    try:
        seen = float(a.get("last_seen") or 0)
    except (TypeError, ValueError):
        seen = 0
    return {"id": a.get("id", ""), "name": a.get("name", ""), "hostname": a.get("hostname", ""),
            "platform": a.get("platform", ""), "created_at": a.get("created_at", ""),
            "last_seen": a.get("last_seen", 0), "online": bool(seen and now - seen < 75)}


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


@app.post("/api/admin/agents/pairing")
async def admin_agent_pairing(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
    now = time.time()
    with _AGENT_LOCK:
        st = _agents_load()
        st["pairing"][code] = {"code": code, "created": now, "expires": now + 600, "claimed": False}
        _agents_save(st)
    base = str(request.base_url or "").strip().rstrip("/")
    return {"code": code, "expires_in": 600, "server_hint": base}


@app.get("/api/admin/agents")
async def admin_agents(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    with _AGENT_LOCK:
        st = _agents_load()
        agents = [_agent_public(a) for a in st["agents"].values()]
    agents.sort(key=lambda a: str(a["id"]))
    return {"agents": agents}


@app.post("/api/admin/agents/{agent_id}/exec")
async def admin_agent_exec(request: Request, agent_id: str):
    actor, err = _require_superadmin(request)
    if err:
        return err
    if not _admin_rate_ok(actor["username"], 60, 60):
        return JSONResponse(status_code=429, content={"error": "Rate limit"})
    data, bad = await _safe_json(request)
    if bad:
        return bad
    command = str((data or {}).get("command", "") or "").strip()
    if not command or len(command) > 8000:
        return JSONResponse(status_code=400, content={"error": "Empty or oversized command"})
    with _AGENT_LOCK:
        st = _agents_load()
        if agent_id not in st["agents"]:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        cid = str(st.get("next_command", 1))
        st["next_command"] = int(cid) + 1
        st["commands"][cid] = {"id": cid, "agent_id": agent_id, "command": command,
                               "status": "queued", "exit_code": None, "output": "",
                               "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": ""}
        _agents_save(st)
    _audit("agent.exec", actor["username"], f"agent={agent_id} cmd={command[:120]}")
    return {"status": "ok", "command_id": cid}


@app.get("/api/admin/agents/{agent_id}/commands")
async def admin_agent_commands(request: Request, agent_id: str, limit: int = 20):
    user, err = _require_superadmin(request)
    if err:
        return err
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    with _AGENT_LOCK:
        st = _agents_load()
        rows = [c for c in st["commands"].values() if str(c.get("agent_id")) == str(agent_id)]
    rows.sort(key=lambda c: int(c.get("id", 0)), reverse=True)
    return {"commands": rows[:limit]}


@app.delete("/api/admin/agents/{agent_id}")
async def admin_agent_delete(request: Request, agent_id: str):
    actor, err = _require_superadmin(request)
    if err:
        return err
    with _AGENT_LOCK:
        st = _agents_load()
        if agent_id not in st["agents"]:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})
        del st["agents"][agent_id]
        st["commands"] = {k: c for k, c in st["commands"].items() if str(c.get("agent_id")) != str(agent_id)}
        _agents_save(st)
    _audit("agent.remove", actor["username"], f"agent={agent_id}")
    return {"status": "ok"}


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
        st["agents"][aid] = {"id": aid, "name": name, "hostname": hostname, "platform": platform_name,
                             "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                             "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "last_seen": 0}
        _agents_save(st)
    _audit("agent.pair", "-", f"agent={aid} host={hostname}")
    return {"status": "ok", "agent_id": aid, "token": token}


@app.get("/api/agent/pending")
async def agent_pending(request: Request):
    a = _agent_from_token(request)
    if not a:
        return JSONResponse(status_code=401, content={"error": "Bad agent token"})
    now = time.time()
    with _AGENT_LOCK:
        st = _agents_load()
        cur = st["agents"].get(a["id"])
        if not cur:
            return JSONResponse(status_code=401, content={"error": "Unknown agent"})
        cur["last_seen"] = now
        nxt = None
        for c in st["commands"].values():
            if str(c.get("agent_id")) == str(a["id"]) and c.get("status") == "queued":
                if nxt is None or int(c.get("id", 0)) < int(nxt.get("id", 0)):
                    nxt = c
        if nxt is not None:
            nxt["status"] = "running"
        _agents_save(st)
    if nxt is None:
        return {"command": None}
    return {"command": {"id": nxt["id"], "command": nxt["command"], "timeout": _AGENT_CMD_TIMEOUT}}


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
    return {"status": "ok"}


AGENT_CLIENT_PY = '''#!/usr/bin/env python3
"""Omniilink pairing agent: runs shell commands from your Omniilink server.
Usage: python omniilink-agent.py --server https://YOUR-SERVER --code ABC123 [--name my-pc]
Needs only the Python standard library. Polls every few seconds; press Ctrl+C to stop.
"""
import argparse
import json
import platform
import socket
import subprocess
import sys
import time
import urllib.request

SERVER = "__SERVER__"


def api(method, path, token=None, payload=None):
    req = urllib.request.Request(SERVER + path, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=body, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    ap = argparse.ArgumentParser(description="Omniilink pairing agent")
    ap.add_argument("--server", default=SERVER, help="Omniilink server URL")
    ap.add_argument("--code", required=True, help="6-character pairing code from Admin > Agents")
    ap.add_argument("--name", default=socket.gethostname(), help="Display name for this computer")
    args = ap.parse_args()
    global SERVER
    SERVER = args.server.rstrip("/")
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
            p = api("GET", "/api/agent/pending", token)
            cmd = p.get("command")
            if cmd:
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
        except Exception as e:
            print("poll error: " + str(e))
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
'''


@app.get("/api/admin/agent-client")
async def admin_agent_client(request: Request):
    user, err = _require_superadmin(request)
    if err:
        return err
    base = str(request.base_url or "").strip().rstrip("/")
    script = AGENT_CLIENT_PY.replace("__SERVER__", base or "https://YOUR-SERVER")
    return PlainTextResponse(script, headers={"Content-Disposition": "attachment; filename=omniilink-agent.py"})


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
