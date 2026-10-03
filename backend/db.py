"""Persistent records for DocChat: uploaded files and chat history.

Qdrant holds the vectors, but it is not a good system of record for "which
files exist, who owns them, and what was discussed". That lives in SQLite so
nothing depends on re-scanning a vector store to answer it.
"""

import json
import os
import secrets
import sqlite3
import time
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
FILES_DIR = os.path.join(DATA_DIR, "files")

ROLE_ADMIN = "admin"


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id           TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                name         TEXT    NOT NULL,
                kind         TEXT    NOT NULL,
                media_kind   TEXT    NOT NULL DEFAULT 'text',
                filename     TEXT    NOT NULL,
                stored_path  TEXT,
                mime         TEXT,
                size_bytes   INTEGER NOT NULL DEFAULT 0,
                chunks       INTEGER NOT NULL DEFAULT 0,
                images       INTEGER NOT NULL DEFAULT 0,
                origin       TEXT    NOT NULL DEFAULT 'upload',
                created_at   REAL    NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_user ON sources(user_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                title      TEXT    NOT NULL DEFAULT 'New chat',
                created_at REAL    NOT NULL,
                updated_at REAL    NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT    NOT NULL,
                user_id         INTEGER NOT NULL,
                role            TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                sources         TEXT    NOT NULL DEFAULT '[]',
                created_at      REAL    NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id)")


# ------------------------------------------------------------------ file storage


def store_file(source_id: str, data: bytes, extension: str) -> str:
    """Write the original upload to permanent storage and return its path.

    Extensionless or unknown extensions still get a name, so nothing is ever
    written to a temp directory that could be reaped by the OS.
    """
    os.makedirs(FILES_DIR, exist_ok=True)
    ext = (extension or "").lower()
    if not ext.startswith("."):
        ext = "." + ext if ext else ".bin"
    path = os.path.join(FILES_DIR, source_id + ext)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def delete_file(stored_path: Optional[str]) -> None:
    if stored_path and os.path.isfile(stored_path):
        try:
            os.remove(stored_path)
        except OSError:
            pass


# ------------------------------------------------------------------ sources


def create_source(
    user_id: int,
    name: str,
    kind: str,
    filename: str,
    data: Optional[bytes],
    mime: Optional[str] = None,
    origin: str = "upload",
    source_id: Optional[str] = None,
) -> dict:
    """Register an ingested document, persisting the original bytes to disk.

    source_id must be supplied when the caller has already chosen one (URL
    ingest does, so the Qdrant payloads and this row agree). Left unset it is
    minted here, which is fine for byte uploads because they have not yet been
    indexed.
    """
    init_db()
    if not source_id:
        source_id = secrets.token_hex(16)
    stored_path = None
    size = 0
    if data is not None:
        stored_path = store_file(source_id, data, os.path.splitext(filename)[1])
        size = len(data)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sources (id,user_id,name,kind,media_kind,filename,stored_path,mime,"
            "size_bytes,chunks,images,origin,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?)",
            (source_id, user_id, name, kind, "text", filename, stored_path, mime, size, origin, now),
        )
    return get_source(source_id)


def finalize_source(source_id: str, chunks: int, images: int, media_kind: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET chunks=?, images=?, media_kind=? WHERE id=?",
            (chunks, images, media_kind, source_id),
        )


def get_source(source_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not row:
        raise KeyError(source_id)
    return _src(row)


def list_sources(user_id: int, is_admin: bool = False, owner_id: Optional[int] = None) -> list:
    """List the caller's documents. Admins see every user's files."""
    init_db()
    with _connect() as conn:
        if owner_id is not None:
            rows = conn.execute(
                "SELECT * FROM sources WHERE user_id=? ORDER BY created_at DESC", (owner_id,)
            ).fetchall()
        elif is_admin:
            rows = conn.execute("SELECT * FROM sources ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
    out = [_src(r) for r in rows]
    if is_admin or owner_id is not None:
        with _connect() as conn:
            names = {
                r["id"]: r["username"]
                for r in conn.execute("SELECT id, username FROM users").fetchall()
            }
        for s in out:
            s["owner"] = names.get(s["user_id"], "unknown")
    return out


def delete_source_row(source_id: str) -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise KeyError(source_id)
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    delete_file(row["stored_path"])
    return _src(row)


# ------------------------------------------------------------------ conversations


def ensure_conversation(user_id: int, conversation_id: Optional[str] = None) -> dict:
    """Return the requested conversation if the user owns it, else their latest.

    A caller may ask for a stable id that does not exist yet (e.g. the
    Telegram bot's "telegram" conversation). In that case create it with the
    requested id instead of silently reusing the latest conversation, which
    would mix web and bot turns together.
    """
    init_db()
    now = time.time()
    with _connect() as conn:
        if conversation_id:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
            ).fetchone()
            if row:
                return _conv(row)
            conn.execute(
                "INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (conversation_id, user_id, "Telegram", now, now),
            )
            created = conversation_id
        else:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if row:
                return _conv(row)
            cid = secrets.token_hex(12)
            conn.execute(
                "INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (cid, user_id, "New chat", now, now),
            )
            created = cid
    return get_conversation(created)  # after commit


def get_conversation(conversation_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if not row:
        raise KeyError(conversation_id)
    return _conv(row)


def list_conversations(user_id: int) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
    return [_conv(r) for r in rows]


def new_conversation(user_id: int) -> dict:
    init_db()
    now = time.time()
    cid = secrets.token_hex(12)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (cid, user_id, "New chat", now, now),
        )
    return get_conversation(cid)


def delete_conversation(conversation_id: str, user_id: int) -> None:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
        )
        if not cur.rowcount:
            raise KeyError(conversation_id)


# ------------------------------------------------------------------ messages


def add_message(
    conversation_id: str, user_id: int, role: str, content: str, sources=None
) -> dict:
    init_db()
    now = time.time()
    payload = json.dumps(sources or [])
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id,user_id,role,content,sources,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (conversation_id, user_id, role, content, payload, now),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        if role == "user":
            conn.execute(
                "UPDATE conversations SET title=? WHERE id=? AND title='New chat'",
                (content.strip().split("\n")[0][:60] or "New chat", conversation_id),
            )
        mid = cur.lastrowid
    return get_message(mid)


def get_message(message_id: int) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        raise KeyError(message_id)
    return _msg(row)


def list_messages(conversation_id: str, user_id: int) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND user_id=? ORDER BY id ASC",
            (conversation_id, user_id),
        ).fetchall()
    return [_msg(r) for r in rows]


def _msg(row: sqlite3.Row) -> dict:
    try:
        sources = json.loads(row["sources"])
    except Exception:
        sources = []
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "user_id": row["user_id"],
        "role": row["role"],
        "content": row["content"],
        "sources": sources,
        "created_at": row["created_at"],
    }


def _conv(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _src(row: sqlite3.Row) -> dict:
    return {
        "source_id": row["id"],
        "user_id": row["user_id"],
        "source_name": row["name"],
        "kind": row["kind"],
        "media_kind": row["media_kind"],
        "filename": row["filename"],
        "stored_path": row["stored_path"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "chunks": row["chunks"],
        "images": row["images"],
        "origin": row["origin"],
        "created_at": row["created_at"],
    }


def usage_stats(user_id: int, is_admin: bool) -> dict:
    init_db()
    with _connect() as conn:
        src = (
            "SELECT COUNT(*) c, COALESCE(SUM(size_bytes),0) b FROM sources"
            if is_admin
            else "SELECT COUNT(*) c, COALESCE(SUM(size_bytes),0) b FROM sources WHERE user_id=?"
        )
        row = conn.execute(src, () if is_admin else (user_id,)).fetchone()
        msg = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE user_id=?", (user_id,)
        ).fetchone()
    return {
        "sources": row["c"],
        "bytes": row["b"],
        "messages": msg["c"],
    }
