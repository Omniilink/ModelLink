"""
Omniilink Auth Module — SQLite-based user authentication.
Replaces Cloudflare Workers (signupomniilink + useomniilink).
"""
import os
import sys
import sqlite3
import hashlib
import secrets
import time
import json
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse


def _base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DB_PATH = os.path.join(_base_path(), "omnilink.db")

# ==========================================
# Database Setup
# ==========================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT,
            request_count INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, provider)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key);
        CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
        CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id);
    """)
    # Migration: is_admin column (first user becomes admin)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
    env_admin = os.environ.get("OMNIILINK_ADMIN", "").strip().lower()
    if env_admin:
        for u in [x.strip() for x in env_admin.split(",") if x.strip()]:
            conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (u,))
    conn.commit()
    conn.close()


# ==========================================
# Password Hashing (PBKDF2-HMAC-SHA256)
# ==========================================
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected_hex = stored_hash.split('$', 1)
        h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return h.hex() == expected_hex
    except Exception:
        return False


# ==========================================
# Session Management
# ==========================================
def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (token, user_id, expires))
        conn.commit()
    finally:
        conn.close()
    return token


def get_user_from_session(token: str) -> Optional[dict]:
    if not token:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT u.id, u.username, u.name, u.created_at, u.is_admin FROM sessions s "
            "JOIN users u ON s.user_id = u.id "
            "WHERE s.token = ? AND s.expires_at > datetime('now')",
            (token,)
        ).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def destroy_session(token: str):
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


# ==========================================
# User Management
# ==========================================
def create_user(username: str, password: str, name: str = "") -> dict:
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?",
                                (username,)).fetchone()
        if existing:
            return {"error": "Username already taken"}
        password_hash = hash_password(password)
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        is_admin = 1 if count == 0 else 0
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
            (username, password_hash, name or username, is_admin)
        )
        conn.commit()
        user_id = cur.lastrowid
        return {"id": user_id, "username": username, "name": name or username,
                "is_admin": is_admin}
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, name, password_hash, created_at, is_admin FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        if not row:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return {"id": row["id"], "username": row["username"], "name": row["name"],
                "created_at": row["created_at"], "is_admin": bool(row["is_admin"])}
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, name, created_at, is_admin FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if row:
            d = dict(row)
            d["is_admin"] = bool(d.get("is_admin"))
            return d
        return None
    finally:
        conn.close()


def update_user_name(user_id: int, name: str):
    conn = get_db()
    try:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        conn.commit()
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str):
    conn = get_db()
    try:
        pw_hash = hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
        conn.commit()
    finally:
        conn.close()


# ==========================================
# Permanent API Keys (for programmatic access)
# ==========================================
def generate_api_key(user_id: int, name: str = "") -> dict:
    key = f"ml-{secrets.token_hex(24)}"
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO api_keys (user_id, key, name) VALUES (?, ?, ?)",
            (user_id, key, name or f"Key")
        )
        conn.commit()
        return {"id": cur.lastrowid, "key": key, "name": name or "Key"}
    finally:
        conn.close()


def get_user_api_keys(user_id: int) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, key, name, created_at, last_used_at, request_count FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_api_key(user_id: int, key: str):
    conn = get_db()
    try:
        conn.execute("DELETE FROM api_keys WHERE user_id = ? AND key = ?", (user_id, key))
        conn.commit()
    finally:
        conn.close()


def validate_api_key(key: str) -> Optional[int]:
    """Validate a permanent API key and return the user_id."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT user_id FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE api_keys SET last_used_at = datetime('now'), request_count = request_count + 1 WHERE key = ?",
                (key,)
            )
            conn.commit()
            return row["user_id"]
        return None
    finally:
        conn.close()


# ==========================================
# Provider API Keys (per-user, stored in DB)
# ==========================================
def get_user_provider_keys(user_id: int) -> dict:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT provider, api_key FROM user_api_keys WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        result = {}
        for r in rows:
            result[r["provider"]] = r["api_key"]
        return result
    finally:
        conn.close()


def save_user_provider_key(user_id: int, provider: str, api_key: str):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_api_keys (user_id, provider, api_key) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, provider) DO UPDATE SET api_key = ?, updated_at = datetime('now')",
            (user_id, provider, api_key, api_key)
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_provider_key(user_id: int, key_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_api_keys WHERE user_id = ? AND id = ?",
                     (user_id, key_id))
        conn.commit()
    finally:
        conn.close()


# ==========================================
# Auth Middleware for FastAPI
# ==========================================
def _extract_token(request: Request) -> Optional[str]:
    """Extract session token from cookie or Authorization header (never from query strings)."""
    token = request.cookies.get("omnilink_token")
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        candidate = auth_header[7:]
        if candidate.startswith("ml-"):
            return None
        return candidate
    return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get the authenticated user from the request. Returns None if not authenticated."""
    token = _extract_token(request)
    if token:
        return get_user_from_session(token)
    api_key = request.headers.get("Authorization", "")
    if api_key.startswith("Bearer ml-"):
        key = api_key[7:]
        user_id = validate_api_key(key)
        if user_id:
            return get_user_by_id(user_id)
    return None


def require_auth(request: Request) -> Optional[JSONResponse]:
    """Returns None if authenticated, or a 401 JSONResponse if not."""
    user = get_current_user(request)
    if user:
        return None
    return JSONResponse(status_code=401, content={"error": "Authentication required"})


def set_session_cookie(response, token: str, secure: bool = False):
    """Set the session cookie on a response."""
    response.set_cookie(
        key="omnilink_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=30 * 24 * 3600,
        path="/",
    )


# Initialize DB on import
init_db()
