import sys
import os
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
    delete_user_provider_key, get_db, get_user_from_session
)

app = FastAPI(title="Omniilink API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# Per-User Data Isolation
# ==========================================
def _get_user_id(request: Request) -> str:
    """Extract user_id from authenticated session. 'default' for local/unauthenticated."""
    user = get_current_user(request)
    if user:
        return str(user["id"])
    return "default"

def get_user_id(request: Request) -> Optional[int]:
    """Get integer user_id or None."""
    user = get_current_user(request)
    return user["id"] if user else None

def _user_data_dir(user_id: str) -> str:
    """Get or create per-user data directory."""
    d = os.path.join(APP_DIR, "user_data", user_id)
    os.makedirs(d, exist_ok=True)
    return d

def _user_projects_dir(user_id: str) -> str:
    """Get per-user projects directory."""
    d = os.path.join(_user_data_dir(user_id), "projects")
    os.makedirs(d, exist_ok=True)
    return d

def _user_api_keys_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "api_keys.json")

def _user_settings_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "settings.json")

def _user_coding_settings_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "coding_settings.json")

# Per-user coding states: {user_id: {"folder": ..., "settings": {...}}}
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
    """Get provider keys: prefer DB-stored keys (authenticated), fall back to env vars."""
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
    return load_all_keys()

# ==========================================
# Log Broadcasting via SSE
# ==========================================
class LogBroadcaster:
    def __init__(self):
        self._queues: List[queue.Queue] = []
        self._lock = asyncio.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=200)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, message: str):
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
async def stream_logs():
    q = log_broadcaster.subscribe()
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

@app.post("/api/auth/signup")
async def auth_signup(request: Request, req: SignupRequest):
    username = req.username.strip().lower()
    if len(username) < 3:
        return JSONResponse(status_code=400, content={"error": "Username must be at least 3 characters"})
    if len(req.password) < 6:
        return JSONResponse(status_code=400, content={"error": "Password must be at least 6 characters"})
    result = create_user(username, req.password, req.name)
    if "error" in result:
        return JSONResponse(status_code=409, content={"error": result["error"]})
    token = create_session(result["id"])
    user = get_user_by_id(result["id"])
    resp = JSONResponse(content={"status": "ok", "user": user})
    set_session_cookie(resp, token)
    return resp

@app.post("/api/auth/signin")
async def auth_signin(request: Request, req: SigninRequest):
    user = authenticate_user(req.username.strip().lower(), req.password)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Invalid username or password"})
    token = create_session(user["id"])
    resp = JSONResponse(content={"status": "ok", "user": user})
    set_session_cookie(resp, token)
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
        update_user_name(user["id"], req.name.strip())
    if req.new_password:
        if len(req.new_password) < 6:
            return JSONResponse(status_code=400, content={"error": "Password must be at least 6 characters"})
        update_user_password(user["id"], req.new_password)
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
    save_user_provider_key(user["id"], req.provider.strip(), req.api_key.strip())
    return {"status": "ok"}

@app.delete("/api/provider-keys/{key_id}")
async def delete_provider_key(request: Request, key_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    delete_user_provider_key(user["id"], key_id)
    return {"status": "ok"}

@app.get("/api/status")
async def get_status(request: Request):
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
async def ollama_models():
    models = scan_ollama_models()
    return {"models": models, "available": len(models) > 0, "base_url": OLLAMA_BASE_URL}

@app.get("/api/settings")
async def get_settings(request: Request):
    user = get_current_user(request)
    db_provider_keys = get_user_provider_keys(user["id"]) if user else {}
    settings = {
        "backup_model": get_backup_model(),
        "ollama_url": OLLAMA_BASE_URL,
        "language": get_language(),
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
        else:
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
async def update_settings(request: SettingsRequest):
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
    body = await request.json()
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
async def clear_key(request: ClearKeyRequest):
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
async def get_language_setting():
    return {"language": get_language()}

class LanguageRequest(BaseModel):
    language: str

@app.post("/api/language")
async def set_language_setting(request: LanguageRequest):
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

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "No messages provided"})

    temp = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 1000)
    is_stream = body.get("stream", False)
    req_model = body.get("model", MODEL_NAME)

    # Read per-user API keys from DB
    user_keys = None
    user = get_current_user(request)
    if user:
        db_keys = get_user_provider_keys(user["id"])
        if db_keys:
            user_keys = db_keys

    if is_stream:
        stream, used_model = generate(
            messages=messages,
            model=req_model,
            temperature=temp,
            max_tokens=max_tokens,
            stream=True,
            user_keys=user_keys
        )

        async def event_generator():
            chat_id = f"chatcmpl-{int(time.time())}"
            created_time = int(time.time())
            try:
                if isinstance(stream, dict):
                    text_chunk = stream.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if text_chunk:
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

            finish_chunk = {
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created_time, "model": used_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(finish_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        response, used_model = generate(
            messages=messages,
            model=req_model,
            temperature=temp,
            max_tokens=max_tokens,
            stream=False,
            user_keys=user_keys
        )
        return JSONResponse(content=response)

@app.get("/v1/models")
async def list_models(request: Request):
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

@app.post("/api/coding/open-folder")
async def coding_open_folder(request: Request, req: CodingOpenFolder):
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = req.path.strip()
    if not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {folder}"})
    coding_state["folder"] = folder
    tree = _build_file_tree(folder)
    file_count = sum(1 for _ in os.walk(folder) for f in _[2] if not any(f.endswith(e) for e in IGNORE_EXTS))
    logger.info(f"[CODING] Opened folder: {folder} ({file_count} files)")
    return {"status": "ok", "folder": folder, "tree": tree, "file_count": file_count}

@app.get("/api/coding/files")
async def coding_list_files(request: Request):
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    if not folder or not os.path.isdir(folder):
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    tree = _build_file_tree(folder)
    return {"folder": folder, "tree": tree}

@app.get("/api/coding/file-content")
async def coding_file_content(request: Request, path: str):
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    full = os.path.join(folder, path)
    real = os.path.realpath(full)
    if not real.startswith(os.path.realpath(folder)):
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
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    full = os.path.join(folder, req.path)
    real = os.path.realpath(full)
    if not real.startswith(os.path.realpath(folder)):
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
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    if not folder:
        return JSONResponse(status_code=400, content={"error": "No folder open"})
    full = os.path.join(folder, path)
    real = os.path.realpath(full)
    if not real.startswith(os.path.realpath(folder)):
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
    user_id = _get_user_id(request)
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
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    return coding_state["settings"]

@app.post("/api/coding/settings")
async def coding_update_settings(request: Request):
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    body = await request.json()
    for k, v in body.items():
        if k in coding_state["settings"]:
            coding_state["settings"][k] = v
    return {"status": "ok", "settings": coding_state["settings"]}

@app.get("/api/coding/project-context")
async def coding_project_context(request: Request):
    user_id = _get_user_id(request)
    coding_state = _get_coding_state(user_id)
    folder = coding_state.get("folder")
    if not folder or not os.path.isdir(folder):
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
    entry = generate_api_key(user["id"])
    logger.info(f"[APIKEY] Generated: {entry['key'][:12]}...")
    return entry

@app.post("/api/keys/revoke")
async def rev_api_key(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    body = await request.json()
    key = body.get("key", "")
    revoke_api_key(user["id"], key)
    logger.info(f"[APIKEY] Revoked: {key[:12]}...")
    return {"status": "ok"}

@app.get("/api/keys/info")
async def api_keys_info():
    base_url = "http://127.0.0.1:8080"
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
PROJECTS_DIR = os.path.join(APP_DIR, "projects")

def _ensure_projects_dir():
    os.makedirs(PROJECTS_DIR, exist_ok=True)

@app.get("/api/projects")
async def list_projects(request: Request):
    user_id = _get_user_id(request)
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
    user_id = _get_user_id(request)
    projects_dir = _user_projects_dir(user_id)
    name = req.name.strip()
    if not name or '/' in name or '\\' in name:
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    path = os.path.join(projects_dir, name)
    if os.path.exists(path):
        return JSONResponse(status_code=409, content={"error": "Project already exists"})
    os.makedirs(path, exist_ok=True)
    logger.info(f"[PROJECT] Created: {name}")
    return {"status": "ok", "name": name, "path": path}

@app.post("/api/projects/import")
async def import_project(request: Request, req: ProjectImport):
    user_id = _get_user_id(request)
    projects_dir = _user_projects_dir(user_id)
    name = req.name.strip()
    source = req.source_path.strip()
    if not name or '/' in name or '\\' in name:
        return JSONResponse(status_code=400, content={"error": "Invalid project name"})
    if not os.path.isdir(source):
        return JSONResponse(status_code=400, content={"error": f"Not a valid directory: {source}"})
    dest = os.path.join(projects_dir, name)
    if os.path.exists(dest):
        return JSONResponse(status_code=409, content={"error": "Project already exists"})
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*IGNORE_DIRS))
    logger.info(f"[PROJECT] Imported: {name} from {source}")
    return {"status": "ok", "name": name, "path": dest}

@app.post("/api/projects/open")
async def open_project(request: Request, req: ProjectCreate):
    user_id = _get_user_id(request)
    projects_dir = _user_projects_dir(user_id)
    coding_state = _get_coding_state(user_id)
    name = req.name.strip()
    path = os.path.join(projects_dir, name)
    if not os.path.isdir(path):
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    coding_state["folder"] = path
    tree = _build_file_tree(path)
    file_count = sum(1 for _, _, files in os.walk(path) for f in files if not any(f.endswith(e) for e in IGNORE_EXTS))
    logger.info(f"[PROJECT] Opened: {name} ({file_count} files)")
    return {"status": "ok", "name": name, "folder": path, "tree": tree, "file_count": file_count}

@app.delete("/api/projects/{name}")
async def delete_project(request: Request, name: str):
    user_id = _get_user_id(request)
    projects_dir = _user_projects_dir(user_id)
    path = os.path.join(projects_dir, name)
    return await _do_delete(request, path, name)

class ProjectDelete(BaseModel):
    path: str

@app.post("/api/projects/delete")
async def delete_project_by_path(request: Request, req: ProjectDelete):
    user_id = _get_user_id(request)
    path = req.path
    name = os.path.basename(path)
    return await _do_delete(request, path, name)

async def _do_delete(request: Request, path, name):
    user_id = _get_user_id(request)
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
    uvicorn.run(app, host="0.0.0.0", port=port)
