import sys
import os
import re
import logging
import asyncio
import queue
import subprocess
import shutil

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("Omniilink")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import time
import json

from modellink.splitter import (
    generate, PROVIDERS, MODEL_NAME, lb,
    load_all_keys, MODEL_RATE_LIMITS,
    scan_ollama_models, get_backup_model, OLLAMA_BASE_URL,
    get_language, set_language
)
from modellink.auth import (
    init_db, get_current_user, require_auth, create_user, authenticate_user,
    create_session, destroy_session, set_session_cookie, get_user_by_id,
    update_user_name, update_user_password, generate_api_key, get_user_api_keys,
    revoke_api_key, get_user_provider_keys, save_user_provider_key,
    delete_user_provider_key, get_db, get_user_from_session,
    revoke_user_sessions, record_usage, get_usage, get_api_key_id_by_key
)

app = FastAPI(title="Omniilink API", version="3.0.0")

MAX_BODY_BYTES = 15 * 1024 * 1024  # 15 MB request body cap

class BodyLimitMiddleware:
    """Reject request bodies over MAX_BODY_BYTES (works for Content-Length and chunked)."""
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
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
            return {"type": "http.disconnect"}

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

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

# Access log: broadcast API activity to the terminal (skip static/health noise)
_SKIP_LOG_PATHS = {"/api/health", "/api/logs", "/favicon.svg", "/favicon.ico"}

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
    """Reject null bytes and absurd lengths in filesystem paths."""
    if not path or len(path) > 1024:
        return True
    if "\x00" in path:
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
        return int(os.environ.get("OMNIILINK_USER_QUOTA", str(5 * 1024 * 1024 * 1024)))
    except ValueError:
        return 5 * 1024 * 1024 * 1024

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

def _get_coding_state(user_id: str) -> dict:
    if user_id not in _coding_states:
        _coding_states[user_id] = {
            "folder": None,
            "settings": {
                "auto_edit": False,
                "auto_command": False,
                "max_file_size": 100000,
                "system_prompt": DEFAULT_CODING_PROMPT
            }
        }
    return _coding_states[user_id]

# ==========================================
# Provider Key Resolution
# ==========================================
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
class LogBroadcaster:
    def __init__(self):
        self._queues: List[queue.Queue] = []
        self._lock = asyncio.Lock()
        self._history: List[str] = []
        self._history_max = 500

    def subscribe(self) -> queue.Queue:
        if len(self._queues) >= 64:
            raise RuntimeError("Too many log subscriptions")
        q = queue.Queue(maxsize=200)
        # Replay recent history so the terminal isn't empty on connect
        for msg in self._history:
            try:
                q.put_nowait(msg)
            except queue.Full:
                break
        self._queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, message: str):
        self._history.append(message)
        if len(self._history) > self._history_max:
            del self._history[: len(self._history) - self._history_max]
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(message)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

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

class CodingRunCommand(BaseModel):
    command: str
    cwd: Optional[str] = None

# ==========================================
# API Endpoints
# ==========================================
@app.get("/api/logs")
async def stream_logs(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    try:
        q = log_broadcaster.subscribe()
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

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "3.0.0", "model": MODEL_NAME}

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
    return {"models": all_models, "by_provider": discovered, "backup_model": get_backup_model()}

@app.get("/api/ollama/models")
async def ollama_models(request: Request):
    _user, err = _require_auth(request)
    if err:
        return err
    models = scan_ollama_models()
    return {"models": models, "available": len(models) > 0, "base_url": OLLAMA_BASE_URL}

@app.get("/api/settings")
async def get_settings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    db_provider_keys = get_user_provider_keys(user["id"])
    settings = {
        "backup_model": get_backup_model(),
        "ollama_url": OLLAMA_BASE_URL,
        "language": get_language(),
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

@app.post("/api/settings")
async def update_settings(request: Request, req: SettingsRequest):
    _user, err = _require_admin(request)
    if err:
        return err
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    target = f"{request.key_name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(target):
            lines[i] = f"{request.key_name}={request.key_value}\n"
            found = True
            break
    if not found:
        lines.append(f"\n{request.key_name}={request.key_value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ[request.key_name] = request.key_value
    logger.info(f"Settings updated: {request.key_name}")
    return {"status": "ok", "message": f"Updated {request.key_name}"}

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
    env_path = os.path.join(APP_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    target = f"{request.key_name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(target):
            lines[i] = f"{request.key_name}=\n"
            break
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ.pop(request.key_name, None)
    logger.info(f"Key cleared: {request.key_name}")
    return {"status": "ok", "message": f"Cleared {request.key_name}"}

@app.get("/api/language")
async def get_language_setting(http_request: Request):
    _user, err = _require_auth(http_request)
    if err:
        return err
    return {"language": get_language()}

class LanguageRequest(BaseModel):
    language: str

@app.post("/api/language")
async def set_language_setting(http_request: Request, request: LanguageRequest):
    _user, err = _require_auth(http_request)
    if err:
        return err
    set_language(request.language)
    settings_path = os.path.join(APP_DIR, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as f:
                settings = json.load(f)
        except: pass
    settings["language"] = request.language
    with open(settings_path, "w") as f:
        json.dump(settings, f)
    os.environ["LANGUAGE"] = request.language
    logger.info(f"Language set to: {request.language}")
    return {"status": "ok", "language": request.language}

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
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(range, 24)
    try:
        data = get_usage(user_id=user["id"], range_hours=hours)
        data["rate_limits"] = lb.rate_limiter.get_status()
        data["model_name"] = MODEL_NAME
        return data
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

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

    # Key policy: admin uses the server's .env keys; everyone else must use their own keys.
    user_keys = None
    if not user.get("is_admin"):
        db_keys = get_user_provider_keys(user["id"])
        if not db_keys:
            return JSONResponse(status_code=403, content={"error": "No API keys configured. Add your own provider keys in Settings."})
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
        f"est_tokens~{est_prompt_tokens} temp={temp} max_tokens={max_tokens}"
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

    if is_stream:
        stream, used_model = await asyncio.to_thread(
            generate,
            messages=messages,
            model=req_model,
            temperature=temp,
            max_tokens=max_tokens,
            stream=True,
            user_keys=user_keys,
        )

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
            response, used_model = await asyncio.to_thread(
                generate,
                messages=messages,
                model=req_model,
                temperature=temp,
                max_tokens=max_tokens,
                stream=False,
                user_keys=user_keys,
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
            ext = os.path.splitext(name)[1].lower()
            if ext not in IGNORE_EXTS and not name.startswith("."):
                files.append(name)
    for d in dirs:
        sub_path = os.path.join(root_path, d)
        children = _build_file_tree(sub_path, prefix + d + "/")
        if children:
            entries.append({"name": d, "type": "dir", "children": children})
    for f in files:
        size = os.path.getsize(os.path.join(root_path, f))
        entries.append({"name": f, "type": "file", "size": size})
    return entries

DEFAULT_CODING_PROMPT = """You are a coding assistant. You can create, edit, delete files and run commands.

IMPORTANT: When you need to create, edit, delete a file, or run a command, you MUST use these exact text blocks in your response. The system will detect these blocks and perform the actions.

To CREATE a new file, write EXACTLY this format:
FILE_CREATE:filename.py
your complete file content here
END_FILE_CREATE

To EDIT an existing file, write EXACTLY this format:
FILE_EDIT:filename.py
your complete file content here
END_FILE_EDIT

To DELETE a file, write EXACTLY this format:
FILE_DELETE:filename.py
END_FILE_DELETE

To RUN a command, write EXACTLY this format:
RUN_COMMAND:python filename.py
END_RUN_COMMAND

You can use multiple blocks in one response. Always explain what you do.

Example response when creating a file:
I will create a new Python file for you.

FILE_CREATE:hello.py
print("Hello World")
END_FILE_CREATE

The file has been created. You can run it with:
RUN_COMMAND:python hello.py
END_RUN_COMMAND

Rules:
- For FILE_EDIT, include the COMPLETE file content, not just changes.
- For FILE_CREATE, the full file content goes between the markers.
- Always put the file path right after the colon, no spaces before the path.
- Each block must start on its own line.
- You are an expert in Python, JavaScript, TypeScript, HTML, CSS, Java, C, C++, Go, Rust, Ruby, PHP, and all other programming languages.
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
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {folder}"})
    if not user.get("is_admin") and not _is_within(_user_data_dir(user_id), folder):
        return JSONResponse(status_code=403, content={"error": "Access denied: you can only open folders inside your own data directory"})
    coding_state["folder"] = folder
    tree = _build_file_tree(folder)
    file_count = sum(1 for _ in os.walk(folder) for f in _[2] if not any(f.endswith(e) for e in IGNORE_EXTS))
    logger.info(f"[CODING] Opened folder: {folder} ({file_count} files)")
    return {"status": "ok", "folder": folder, "tree": tree, "file_count": file_count}

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
        with open(full, "w", encoding="utf-8") as f:
            f.write(req.content)
        logger.info(f"[CODING] Wrote file: {req.path}")
        return {"status": "ok", "path": req.path, "size": len(req.content), "backed_up": backup is not None}
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
        logger.info(f"[CODING] Deleted file: {path}")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/coding/run-command")
async def coding_run_command(request: Request, req: CodingRunCommand):
    _user, err = _require_admin(request)
    if err:
        return err
    user_id = _safe_username(_user["username"])
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    cwd = req.cwd or folder or os.getcwd()
    if not os.path.isdir(cwd):
        cwd = os.getcwd()
    try:
        proc = subprocess.run(
            req.command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=60
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
        logger.info(f"[CMD] {req.command} (exit {exit_code})")
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
    return coding_state["settings"]

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
    "xai": {"name": "xAI", "color": "#1DA1F2", "env": "XAI_API_KEY"},
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
        if not has_key:
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
    return {"projects": projects, "dir": projects_dir, "storage_used_bytes": total_size, "storage_limit_bytes": 5 * 1024 * 1024 * 1024}

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
    logger.info(f"[PROJECT] Created: {name}")
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
    logger.info(f"[PROJECT] Imported: {name} from {source}")
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
    tree = _build_file_tree(path)
    file_count = sum(1 for _, _, files in os.walk(path) for f in files if not any(f.endswith(e) for e in IGNORE_EXTS))
    logger.info(f"[PROJECT] Opened: {name} ({file_count} files)")
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
            logger.info(f"[PROJECT] Deleted: {name}")
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
            logger.info(f"[PROJECT] Deleted (renamed): {name}")
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
            logger.info(f"[PROJECT] Deleted (cmd): {name}")
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
        logger.info(f"[PROJECT] Locked by system, hidden: {name}")
    except: pass
    return {"status": "ok", "hidden": True}

@app.get("/")
async def serve_index():
    index_path = os.path.join(APP_DIR, "web", "index.html")
    return FileResponse(index_path, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

@app.get("/favicon.svg")
async def serve_favicon():
    favicon_path = os.path.join(APP_DIR, "web", "favicon.svg")
    return FileResponse(favicon_path, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/favicon.ico")
async def serve_favicon_ico():
    favicon_path = os.path.join(APP_DIR, "web", "favicon.svg")
    return FileResponse(favicon_path, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

web_dir = os.path.join(APP_DIR, "web")
if os.path.isdir(web_dir):
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

@app.get("/{path:path}")
async def serve_spa(path: str):
    index_path = os.path.join(APP_DIR, "web", "index.html")
    return FileResponse(index_path, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="127.0.0.1", port=port, server_header=False, date_header=False)
