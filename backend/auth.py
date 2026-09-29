"""User accounts and session tokens for DocChat.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-user random salt,
never in plaintext. Session tokens are HMAC-signed values carried in an
HttpOnly cookie, so no server-side session table is needed.
"""

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Shared with db.py: one SQLite file holds users, sources and messages.
DB_PATH = os.path.join(DATA_DIR, "app.db")
SECRET_PATH = os.path.join(DATA_DIR, ".session_secret")

PBKDF2_ROUNDS = 240_000
TOKEN_TTL = 60 * 60 * 24 * 14
COOKIE_NAME = "docchat_session"

ROLE_ADMIN = "admin"
ROLE_USER = "user"


class AuthError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                salt          TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'user',
                is_active     INTEGER NOT NULL DEFAULT 1,
                epoch         INTEGER NOT NULL DEFAULT 1,
                created_at    REAL    NOT NULL,
                updated_at    REAL    NOT NULL
            )
            """
        )
        # Additive migrations for databases created by an earlier build.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "epoch" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN epoch INTEGER NOT NULL DEFAULT 1")


def _secret() -> bytes:
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "rb") as fh:
            data = fh.read().strip()
            if data:
                return data
    value = secrets.token_bytes(48)
    fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(value)
    return value


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return base64.b64encode(digest).decode(), base64.b64encode(salt).decode()


def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    salt = base64.b64decode(stored_salt)
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


# --------------------------------------------------------------------------- users


def create_user(username: str, password: str, role: str = ROLE_USER) -> dict:
    username = (username or "").strip()
    if len(username) < 3:
        raise AuthError("Username must be at least 3 characters")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters")
    if role not in (ROLE_ADMIN, ROLE_USER):
        raise AuthError("Unknown role")

    pwd, salt = hash_password(password)
    now = time.time()
    with _connect() as conn:
        _init_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, is_active, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)",
                (username, pwd, salt, role, now, now),
            )
        except sqlite3.IntegrityError:
            raise AuthError(f"User '{username}' already exists")
    # Read back after commit: inside the with-block a second connection
    # cannot see the uncommitted INSERT.
    return get_user_by_name(username)


def get_user(user_id: int) -> dict:
    with _connect() as conn:
        _init_db()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise AuthError("User not found")
    return _public(row)


def get_user_by_name(username: str) -> Optional[dict]:
    with _connect() as conn:
        _init_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
    return _public(row) if row else None


def list_users() -> list:
    with _connect() as conn:
        _init_db()
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [_public(r) for r in rows]


def _public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "epoch": int(row["epoch"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def session_epoch(user_id: int) -> int:
    with _connect() as conn:
        _init_db()
        row = conn.execute("SELECT epoch FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["epoch"]) if row else 0


def bump_session_epoch(user_id: int) -> None:
    """Force every existing cookie for this account to stop working."""
    with _connect() as conn:
        _init_db()
        conn.execute("UPDATE users SET epoch = epoch + 1 WHERE id=?", (user_id,))


def set_password(user_id: int, new_password: str) -> None:
    if len(new_password) < 8:
        raise AuthError("Password must be at least 8 characters")
    pwd, salt = hash_password(new_password)
    with _connect() as conn:
        _init_db()
        cur = conn.execute(
            "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE id=?",
            (pwd, salt, time.time(), user_id),
        )
        if not cur.rowcount:
            raise AuthError("User not found")


def set_active(user_id: int, is_active: bool) -> dict:
    with _connect() as conn:
        _init_db()
        cur = conn.execute(
            "UPDATE users SET is_active=?, updated_at=? WHERE id=?", (1 if is_active else 0, time.time(), user_id)
        )
        if not cur.rowcount:
            raise AuthError("User not found")
    return get_user(user_id)  # after commit


def delete_user(user_id: int, protect: Optional[int] = None) -> None:
    with _connect() as conn:
        _init_db()
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise AuthError("User not found")
        if row["role"] == ROLE_ADMIN and protect is not None:
            admins = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1"
            ).fetchone()["n"]
            if admins <= 1:
                raise AuthError("Cannot remove the last active admin")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def check_password(username: str, password: str) -> bool:
    """Constant-time credential check that does not leak account existence."""
    with _connect() as conn:
        _init_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", ((username or "").strip(),)
        ).fetchone()
    if not row:
        verify_password(password, "x", "x")
        return False
    return verify_password(password, row["password_hash"], row["salt"])


def login(username: str, password: str) -> dict:
    """Verify credentials and return a signed session token."""
    with _connect() as conn:
        _init_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", ((username or "").strip(),)
        ).fetchone()
    if not row or not verify_password(password, row["password_hash"], row["salt"]):
        raise AuthError("Invalid username or password")
    if not row["is_active"]:
        raise AuthError("This account has been disabled")
    payload = f"{row['id']}:{row['epoch']}:{int(time.time()) + TOKEN_TTL}"
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:64]
    return {
        "token": f"{body}.{sig}",
        "expires_at": int(time.time()) + TOKEN_TTL,
        "user": _public(row),
    }


def user_from_token(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:64]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        user_id, epoch, expiry = (
            base64.urlsafe_b64decode(padded.encode()).decode().split(":")
        )
        if time.time() > int(expiry):
            return None
        user = get_user(int(user_id))
        # An epoch mismatch means the account was disabled, deleted, or had its
        # password reset after this cookie was issued.
        if int(epoch) != int(user.get("epoch", 0)):
            return None
        # A disabled account must not keep working through an old cookie.
        if not user.get("is_active", False):
            return None
        return user
    except Exception:
        return None


def count_admins() -> int:
    with _connect() as conn:
        _init_db()
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1"
        ).fetchone()["n"]


def ensure_admin(username: str, password: str) -> None:
    _init_db()
    if count_admins() == 0:
        create_user(username, password, ROLE_ADMIN)
