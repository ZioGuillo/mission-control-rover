import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings

DB_PATH = settings.data_dir / "robocontrol.db"

_ADMIN_DEFAULT_PASSWORD = "admin"

_VALID_ROLES = {"pending", "approved", "revoked", "admin"}
_VALID_ICONS = {"astronaut", "alien", "ufo", "dog"}


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"{salt.hex()}:{key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return secrets.compare_digest(expected, bytes.fromhex(key_hex))
    except Exception:
        return False


@contextmanager
def get_conn():
    path = DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                github_id INTEGER UNIQUE,
                github_login TEXT,
                github_avatar TEXT,
                role TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
        """)
        for col, definition in [
            ("user_icon", "TEXT DEFAULT 'astronaut'"),
            ("must_change_password", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO users (username, password_hash, role, must_change_password, created_at) "
                "VALUES (?, ?, 'admin', 1, ?)",
                ("admin", hash_password(_ADMIN_DEFAULT_PASSWORD), now),
            )
        for key, default in [
            ("github_oauth_enabled", "false"),
            ("github_client_id", ""),
            ("github_client_secret", ""),
            ("ml_detection_enabled", "false"),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, default),
            )


# ── User helpers ─────────────────────────────────────────────────────────────

def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_github_id(github_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()


def upsert_github_user(github_id: int, login: str, avatar: str) -> sqlite3.Row:
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE github_id = ?", (github_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET github_login = ?, github_avatar = ? WHERE github_id = ?",
                (login, avatar, github_id),
            )
        else:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO users (github_id, github_login, github_avatar, role, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (github_id, login, avatar, now),
            )
        return conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()


def set_user_role(user_id: int, role: str) -> None:
    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role: {role!r}")
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if role in ("pending", "revoked"):
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def get_all_github_users() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE github_id IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()


def set_user_icon(user_id: int, icon: str) -> None:
    if icon not in _VALID_ICONS:
        raise ValueError(f"Invalid icon: {icon!r}")
    with get_conn() as conn:
        conn.execute("UPDATE users SET user_icon = ? WHERE id = ?", (icon, user_id))


def update_admin_password(new_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (new_hash,))


def update_user_password(user_id: int, new_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))


def clear_must_change_password(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (user_id,))


# ── Session helpers ───────────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
    return token


def get_session_user(token: str) -> sqlite3.Row | None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, now),
        ).fetchone()


def delete_session(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ── App settings helpers ──────────────────────────────────────────────────────

def get_setting(key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
