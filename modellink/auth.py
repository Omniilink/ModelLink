"""
Omniilink Auth Module — SQLite-based user authentication.
Replaces Cloudflare Workers (signupomniilink + useomniilink).
"""
import os
import re
import sys
import sqlite3
import hashlib
import hmac
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
def _auth_safe_username(username: str) -> str:
    """Filesystem-safe folder form ([a-z0-9_-], 1-32 chars), mirroring
    api_server._safe_username without importing it (would be circular)."""
    name = re.sub(r"[^a-z0-9_-]", "_", (username or "").lower())[:32].strip("_-")
    return name or "user"


def _slugify(text: str) -> str:
    """URL slug for shareable /public/<user>/<slug> links (a-z0-9 and -)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].strip("-")


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

        CREATE TABLE IF NOT EXISTS custom_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            model TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            source TEXT DEFAULT 'chat',
            status INTEGER DEFAULT 200,
            api_key_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key);
        CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
        CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id);
        CREATE INDEX IF NOT EXISTS idx_custom_providers_user ON custom_providers(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_events(user_id, ts);
        CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);

        CREATE TABLE IF NOT EXISTS error_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            context TEXT DEFAULT 'chat',
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            error TEXT DEFAULT '',
            note TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_error_reports_status ON error_reports(status, created_at);

        CREATE TABLE IF NOT EXISTS shared_previews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            folder TEXT DEFAULT '',
            path TEXT DEFAULT '',
            content TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            first_opened_at REAL,
            open_count INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_shared_previews_token ON shared_previews(token);

        CREATE TABLE IF NOT EXISTS public_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            show_author INTEGER DEFAULT 1,
            name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            kind TEXT DEFAULT 'project',
            allow_download INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            fav_count INTEGER DEFAULT 0,
            main_file TEXT DEFAULT '',
            slug TEXT DEFAULT '',
            source_project TEXT DEFAULT '',
            source_folder TEXT DEFAULT '',
            source_paths TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            approved_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS public_project_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            path TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            content TEXT,
            FOREIGN KEY (project_id) REFERENCES public_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_favorites (
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, project_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES public_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS forum_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            sort INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS forum_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            show_author INTEGER DEFAULT 1,
            title TEXT DEFAULT '',
            post_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_post_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (section_id) REFERENCES forum_sections(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS forum_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            show_author INTEGER DEFAULT 1,
            content TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (thread_id) REFERENCES forum_threads(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_public_projects_status ON public_projects(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_public_project_files_pid ON public_project_files(project_id);
        CREATE INDEX IF NOT EXISTS idx_forum_threads_section ON forum_threads(section_id, last_post_at);
        CREATE INDEX IF NOT EXISTS idx_forum_posts_thread ON forum_posts(thread_id, id);
    """)
    # Migration: is_admin column (first user becomes admin)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
    if "google_sub" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    try:
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(public_projects)").fetchall()]
    except Exception:
        pcols = []
    if pcols and "main_file" not in pcols:
        conn.execute("ALTER TABLE public_projects ADD COLUMN main_file TEXT DEFAULT ''")
    if pcols and "source_project" not in pcols:
        # Where a published project came from, so the owner can "update" it to
        # the latest version of the original folder. source_project = saved
        # project folder name; source_folder = absolute folder used when
        # publishing the currently-open folder; source_paths = JSON list of the
        # included relative paths.
        conn.execute("ALTER TABLE public_projects ADD COLUMN source_project TEXT DEFAULT ''")
        conn.execute("ALTER TABLE public_projects ADD COLUMN source_folder TEXT DEFAULT ''")
        conn.execute("ALTER TABLE public_projects ADD COLUMN source_paths TEXT DEFAULT ''")
    if pcols and "slug" not in pcols:
        # Shareable links use /public/<username>/<slug>. Backfill existing
        # rows with a per-user-unique slug derived from the project name.
        conn.execute("ALTER TABLE public_projects ADD COLUMN slug TEXT DEFAULT ''")
        used = {}
        for r in conn.execute("SELECT id, user_id, name FROM public_projects").fetchall():
            base = _slugify(r["name"]) or "project"
            slug, n = base, 2
            seen = used.setdefault(r["user_id"], set())
            while slug in seen:
                slug = (base[:55].rstrip("-") + "-" + str(n))
                n += 1
            seen.add(slug)
            conn.execute("UPDATE public_projects SET slug = ? WHERE id = ?", (slug, r["id"]))
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_public_projects_slug ON public_projects(user_id, slug)")
    except Exception:
        pass
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub)")
    env_admin = os.environ.get("OMNIILINK_ADMIN", "").strip().lower()
    # Admin panel allowlist (default: "isaac tribbe", matching api_server):
    # promoted on every boot so an existing account can always reach /admin
    # even if is_admin was ever flipped off in the database. Each entry also
    # matches its filesystem-safe form, so "isaac tribbe" promotes a
    # "isaac_tribbe" account (signup forbids spaces, so that is the real row).
    env_admin_list = os.environ.get("OMNIILINK_ADMIN_USERS", "isaac tribbe").strip().lower()
    admin_names = {x.strip() for x in (env_admin + "," + env_admin_list).split(",") if x.strip()}
    for u in admin_names:
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (u,))
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?",
                     (_auth_safe_username(u),))
    # Seed default forum sections once.
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM forum_sections").fetchone()["c"]
    except Exception:
        n = 1
    if not n:
        for i, (nm, ds) in enumerate([
            ("General", "Anything about Omniilink"),
            ("Showcase", "Share what you built"),
            ("Help", "Ask questions, get unstuck"),
            ("Ideas", "Feature requests and feedback"),
        ]):
            conn.execute("INSERT INTO forum_sections (name, description, sort) VALUES (?, ?, ?)",
                         (nm, ds, i))
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
        return hmac.compare_digest(h.hex().encode(), expected_hex.encode())
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


def revoke_user_sessions(user_id: int, keep_token: Optional[str] = None):
    """Revoke all sessions for a user (optionally keeping the current one)."""
    conn = get_db()
    try:
        if keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?",
                         (user_id, keep_token))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
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
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
                (username, password_hash, name or username, is_admin)
            )
        except sqlite3.IntegrityError:
            return {"error": "Username already taken"}
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


def get_user_by_google_sub(sub: str):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, name, created_at, is_admin FROM users WHERE google_sub = ?",
            (sub,)
        ).fetchone()
        if row:
            d = dict(row)
            d["is_admin"] = bool(d.get("is_admin"))
            return d
        return None
    finally:
        conn.close()


def username_taken(username: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM users WHERE username = ?",
                           (username,)).fetchone()
        return row is not None
    finally:
        conn.close()


def set_user_google_sub(user_id: int, sub: str):
    conn = get_db()
    try:
        conn.execute("UPDATE users SET google_sub = ? WHERE id = ?",
                     (sub, user_id))
        conn.commit()
    finally:
        conn.close()


def update_user_name(user_id: int, name: str):
    conn = get_db()
    try:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        conn.commit()
    finally:
        conn.close()


def all_usernames() -> list:
    """Every registered username (used to block folder-name collisions)."""
    conn = get_db()
    try:
        return [r["username"] for r in conn.execute("SELECT username FROM users").fetchall()]
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


def _rewrite_paths_in(root: str, old: str, new: str):
    """Replace a stale absolute dir prefix inside a user's small state files.

    Renaming user_data/<old> -> user_data/<new> moves the folder but leaves
    any stored absolute path (e.g. the open coding folder) pointing at the old
    location. Rewrite it in plain and JSON-escaped form.
    """
    variants = [(old, new)]
    try:
        variants.append((json.dumps(old)[1:-1], json.dumps(new)[1:-1]))
    except Exception:
        pass
    for name in ("coding_folder.txt", "coding_settings.json", "prefs.json"):
        p = os.path.join(root, name)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
                continue
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read()
            orig = txt
            for a, b in variants:
                if a and a in txt:
                    txt = txt.replace(a, b)
            if txt != orig:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(txt)
        except Exception:
            pass


def change_username(user_id: int, new_username: str) -> dict:
    """Rename a user, keeping their Google link, password, and all data.

    Renames user_data/<old> to user_data/<new> (projects, models, settings,
    version history), updates denormalized username columns, and rewrites
    stored absolute paths that pointed inside the old data dir. Sessions are
    keyed by user id, so the caller stays signed in. Returns {"ok": True,
    "username": new} or {"error": ...}.
    """
    new = (new_username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", new):
        return {"error": "Username must be 3-32 chars: letters, numbers, - and _ only"}
    conn = get_db()
    try:
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"error": "User not found"}
        old = row["username"]
        if new == old:
            return {"ok": True, "username": new}
        if conn.execute("SELECT id FROM users WHERE username = ? AND id != ?",
                        (new, user_id)).fetchone():
            return {"error": "Username already taken"}
        new_safe = _auth_safe_username(new)
        for r in conn.execute("SELECT username FROM users WHERE id != ?", (user_id,)).fetchall():
            if _auth_safe_username(r["username"]) == new_safe:
                return {"error": "Username already taken"}
        try:
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (new, user_id))
        except sqlite3.IntegrityError:
            return {"error": "Username already taken"}
        # Keep denormalized copies in step so history/forums/public show the
        # new name (they are keyed by user id, so nothing is lost).
        for table in ("error_reports", "shared_previews", "public_projects",
                      "forum_threads", "forum_posts"):
            try:
                conn.execute(f"UPDATE {table} SET username = ? WHERE user_id = ?", (new, user_id))
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    # Rename the data folder AFTER the DB commit. A failure here is reported
    # but never loses data: the old folder is still on disk.
    try:
        base = os.path.join(_base_path(), "user_data")
        old_dir = os.path.join(base, _auth_safe_username(old))
        new_dir = os.path.join(base, new_safe)
        if os.path.isdir(old_dir) and os.path.normcase(old_dir) != os.path.normcase(new_dir):
            if os.path.exists(new_dir):
                return {"ok": True, "username": new,
                        "warning": "renamed, but a data folder with the new name already existed"}
            os.rename(old_dir, new_dir)
            _rewrite_paths_in(new_dir, old_dir, new_dir)
    except Exception as e:
        return {"ok": True, "username": new, "warning": f"data folder could not be moved ({e})"}
    return {"ok": True, "username": new}


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
    if not key or not (key.startswith("ml-") and len(key) == 51
                       and all(c in "0123456789abcdef" for c in key[3:])):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT key, user_id FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        if row and hmac.compare_digest(row["key"].encode(), key.encode()):
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


def save_user_provider_key(user_id: int, provider: str, api_key: str) -> Optional[int]:
    """Upsert a provider key; returns the row id (or None on failure)."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_api_keys (user_id, provider, api_key) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, provider) DO UPDATE SET api_key = ?, updated_at = datetime('now')",
            (user_id, provider, api_key, api_key)
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM user_api_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider)
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
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
# Custom OpenAI-compatible providers (per-user)
# ==========================================
def get_custom_providers(user_id: int) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, base_url, api_key, model, created_at, updated_at "
            "FROM custom_providers WHERE user_id = ? ORDER BY name",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_custom_provider(user_id: int, name: str):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, base_url, api_key, model, created_at, updated_at "
            "FROM custom_providers WHERE user_id = ? AND name = ?",
            (user_id, name)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_custom_provider(user_id: int, name: str, base_url: str, api_key: str, model: str) -> Optional[int]:
    """Upsert a custom provider by (user, name); returns the row id (or None on failure)."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO custom_providers (user_id, name, base_url, api_key, model) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET base_url = ?, api_key = ?, model = ?, updated_at = datetime('now')",
            (user_id, name, base_url, api_key, model, base_url, api_key, model)
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM custom_providers WHERE user_id = ? AND name = ?",
            (user_id, name)
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def delete_custom_provider(user_id: int, row_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM custom_providers WHERE user_id = ? AND id = ?",
                     (user_id, row_id))
        conn.commit()
    finally:
        conn.close()


# ==========================================
# Usage Events
# ==========================================
def record_usage(user_id: int, model: str = "", provider: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 duration_ms: int = 0, source: str = "chat",
                 status: int = 200, api_key_id: Optional[int] = None):
    """Insert a usage event. Best-effort: never raises."""
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO usage_events "
                "(user_id, model, provider, prompt_tokens, completion_tokens, "
                " duration_ms, source, status, api_key_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, (model or "")[:80], (provider or "")[:40],
                 max(0, int(prompt_tokens)), max(0, int(completion_tokens)),
                 max(0, int(duration_ms)), (source or "chat")[:20],
                 int(status), api_key_id)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_api_key_id_by_key(key: str) -> Optional[int]:
    if not key or not key.startswith("ml-"):
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM api_keys WHERE key = ?", (key,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def get_usage(user_id: Optional[int] = None, range_hours: int = 24) -> dict:
    """Aggregate usage events for a user (or all users if user_id is None)."""
    conn = get_db()
    try:
        now = time.time()
        # SQLite datetime('now') is UTC; our local server may not be UTC —
        # filter with a computed ISO cutoff in UTC to match DEFAULT timestamps.
        cutoff_utc = datetime.utcfromtimestamp(now - range_hours * 3600).strftime(
            "%Y-%m-%d %H:%M:%S")

        where = "WHERE ts >= ?"
        params: list = [cutoff_utc]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)

        summary = conn.execute(
            f"SELECT COUNT(*) AS requests, "
            f"COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            f"COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
            f"COALESCE(AVG(duration_ms),0) AS avg_duration, "
            f"COALESCE(MAX(duration_ms),0) AS max_duration "
            f"FROM usage_events {where}",
            params,
        ).fetchone()

        # Bucket size: 1min for 1h range, 30min for 24h, 12h for 7d, 1d for 30d
        if range_hours <= 1:
            bucket_expr = "strftime('%Y-%m-%d %H:%M', ts)"
            bucket_fmt = "%Y-%m-%d %H:%M"
        elif range_hours <= 24:
            bucket_expr = (
                "strftime('%Y-%m-%d %H:', ts) || "
                "printf('%02d', CAST(CAST(strftime('%M', ts) AS INTEGER) / 30 AS INTEGER) * 30)"
            )
            bucket_fmt = "%Y-%m-%d %H:%M"
        elif range_hours <= 24 * 7:
            bucket_expr = (
                "strftime('%Y-%m-%d ', ts) || "
                "CASE WHEN CAST(strftime('%H', ts) AS INTEGER) < 12 THEN '00' ELSE '12' END"
            )
            bucket_fmt = "%Y-%m-%d %H"
        else:
            # 30d and beyond: daily buckets
            bucket_expr = "strftime('%Y-%m-%d', ts)"
            bucket_fmt = "%Y-%m-%d"

        timeseries = [dict(r) for r in conn.execute(
            f"SELECT {bucket_expr} AS bucket, COUNT(*) AS requests, "
            f"COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            f"COALESCE(SUM(completion_tokens),0) AS completion_tokens "
            f"FROM usage_events {where} GROUP BY bucket ORDER BY bucket",
            params,
        ).fetchall()]

        by_model = [dict(r) for r in conn.execute(
            f"SELECT model, COUNT(*) AS requests, "
            f"COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens, "
            f"COALESCE(AVG(duration_ms),0) AS avg_duration "
            f"FROM usage_events {where} GROUP BY model ORDER BY requests DESC LIMIT 20",
            params,
        ).fetchall()]

        by_provider = [dict(r) for r in conn.execute(
            f"SELECT provider, COUNT(*) AS requests, "
            f"COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens "
            f"FROM usage_events {where} GROUP BY provider ORDER BY requests DESC",
            params,
        ).fetchall()]

        key_params: list = [cutoff_utc]
        key_where = "WHERE e.ts >= ?"
        if user_id is not None:
            key_where += " AND k.user_id = ?"
            key_params.append(user_id)
        by_key = [dict(r) for r in conn.execute(
            f"SELECT k.id, k.name, k.request_count, k.last_used_at, "
            f"COUNT(e.id) AS requests_in_range, "
            f"COALESCE(SUM(e.prompt_tokens+e.completion_tokens),0) AS tokens "
            f"FROM api_keys k LEFT JOIN usage_events e ON e.api_key_id = k.id "
            f"{key_where} GROUP BY k.id ORDER BY requests_in_range DESC, k.request_count DESC LIMIT 20",
            key_params,
        ).fetchall()]

        recent = [dict(r) for r in conn.execute(
            f"SELECT ts, model, provider, prompt_tokens, completion_tokens, "
            f"duration_ms, source, status "
            f"FROM usage_events {where} ORDER BY id DESC LIMIT 50",
            params,
        ).fetchall()]

        # All-time totals (unfiltered by range)
        at_where = ""
        at_params: list = []
        if user_id is not None:
            at_where = "WHERE user_id = ?"
            at_params.append(user_id)
        all_time = conn.execute(
            f"SELECT COUNT(*) AS requests, "
            f"COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens "
            f"FROM usage_events {at_where}",
            at_params,
        ).fetchone()

        # Active days
        days = conn.execute(
            f"SELECT COUNT(DISTINCT date(ts)) AS days FROM usage_events {where}",
            params,
        ).fetchone()

        return {
            "range_hours": range_hours,
            "bucket_fmt": bucket_fmt,
            "summary": {
                "requests": summary["requests"] or 0,
                "prompt_tokens": summary["prompt_tokens"] or 0,
                "completion_tokens": summary["completion_tokens"] or 0,
                "total_tokens": (summary["prompt_tokens"] or 0) + (summary["completion_tokens"] or 0),
                "avg_duration_ms": round(summary["avg_duration"] or 0, 1),
                "max_duration_ms": summary["max_duration"] or 0,
                "active_days": days["days"] or 0,
            },
            "all_time": {
                "requests": all_time["requests"] or 0,
                "tokens": all_time["tokens"] or 0,
            },
            "timeseries": timeseries,
            "by_model": by_model,
            "by_provider": by_provider,
            "by_key": by_key,
            "recent": recent,
        }
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
