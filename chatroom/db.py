# Copyright 2026 Warren Schultz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SQLite storage layer for chatroom.

Design notes:
  * One SQLite file, WAL mode. A fresh connection per operation, which is
    cheap under WAL and sidesteps sqlite3 thread-affinity entirely.
  * `events` + `cursors` is what makes "what changed since I last looked"
    cheap. A tasks table alone cannot answer that without a full re-read,
    which burns agent context on every turn.
  * `tasks.version` gives optimistic concurrency so two agents updating the
    same task cannot silently clobber each other.
  * Rooms are tenancy. Every row carries a room, and the room an agent may
    touch is derived from its token, never from a tool argument.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any
from collections.abc import Sequence

STATUSES: tuple[str, ...] = ("pending", "in_progress", "blocked", "done", "cancelled")
OPEN_STATUSES: tuple[str, ...] = ("pending", "in_progress", "blocked")
CLAIMABLE_STATUSES: tuple[str, ...] = ("pending", "blocked")
TOKEN_PREFIX = "cr_"

DEFAULT_DB = "/var/lib/chatroom/chatroom.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    name        TEXT PRIMARY KEY,
    created_ts  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash    TEXT PRIMARY KEY,
    agent_name    TEXT NOT NULL,
    default_room  TEXT NOT NULL,
    allowed_rooms TEXT NOT NULL DEFAULT '[]',
    created_ts    TEXT NOT NULL,
    last_seen     TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_agent ON tokens(agent_name);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    assignee    TEXT,
    depends_on  TEXT NOT NULL DEFAULT '[]',
    version     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_ts  TEXT NOT NULL,
    updated_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_room_status ON tasks(room, status);
CREATE INDEX IF NOT EXISTS idx_tasks_room_assignee ON tasks(room, assignee);

CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL,
    room     TEXT NOT NULL,
    author   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    body     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_task ON notes(task_id);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    room     TEXT NOT NULL,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    task_id  INTEGER,
    actor    TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_room_id ON events(room, id);

CREATE TABLE IF NOT EXISTS cursors (
    agent_name    TEXT NOT NULL,
    room          TEXT NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_name, room)
);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    room    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    author  TEXT NOT NULL,
    body    TEXT NOT NULL,
    reply_to INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room, id);

CREATE TABLE IF NOT EXISTS files (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    room    TEXT NOT NULL,
    name    TEXT NOT NULL,
    sha256  TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mime    TEXT NOT NULL DEFAULT 'application/octet-stream',
    author  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    content BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_room_id ON files(room, id);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.environ.get("CHATROOM_DB", DEFAULT_DB))


class StorageError(Exception):
    """The database file cannot be opened, with a hint about how to fix it."""


def _storage_hint(p: Path, exc: Exception) -> StorageError:
    """Turn 'unable to open database file' into something actionable.

    Almost always this is the containerised case: ./data did not exist, so Docker
    created the bind-mount source as root, and the container's non-root user cannot
    write there. The raw sqlite3 message names neither the directory nor the uid, which
    makes a first-run failure needlessly hard to place.
    """
    uid = getattr(os, "getuid", lambda: "?")()
    gid = getattr(os, "getgid", lambda: "?")()
    return StorageError(
        f"cannot open the chatroom database at {p}: {exc}\n"
        f"  The directory {p.parent} must be writable by uid {uid}:{gid} (this process).\n"
        f"  In Docker this usually means ./data is owned by root. Fix on the host with:\n"
        f"    sudo chown -R $(id -u):$(id -g) ./data\n"
        f"  and set CHATROOM_UID / CHATROOM_GID in .env to your `id -u` / `id -g`."
    )


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _storage_hint(p, exc) from exc
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(p), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Switching to WAL is the first *write*. An unwritable file opens fine and
        # fails here instead, so the guard has to cover the pragmas, not just connect().
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.close()
        raise _storage_hint(p, exc) from exc
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations. Safe to run on every connect."""
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
    if "readonly" not in tcols:
        conn.execute("ALTER TABLE tokens ADD COLUMN readonly INTEGER NOT NULL DEFAULT 0")
    if "admin" not in tcols:
        conn.execute("ALTER TABLE tokens ADD COLUMN admin INTEGER NOT NULL DEFAULT 0")
    if "all_rooms" not in tcols:
        conn.execute("ALTER TABLE tokens ADD COLUMN all_rooms INTEGER NOT NULL DEFAULT 0")
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    if "expires_at" not in fcols:
        # Per-file lifetime. Room retention already sweeps files by age, but that is one
        # policy for the whole room; a scratch artefact often wants to die in an hour
        # while the room keeps months of chat. NULL means "keep until room retention".
        conn.execute("ALTER TABLE files ADD COLUMN expires_at TEXT")
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "message_id" not in ecols:
        # Events and messages have separate AUTOINCREMENT sequences, so "msg 19" read off
        # the event log meant event 19, not message 19 — a citation that silently pointed
        # at the wrong thing. Recording which message an event describes makes a reference
        # resolvable. NULL for rows written before this column existed.
        conn.execute("ALTER TABLE events ADD COLUMN message_id INTEGER")
    if "revoked_ts" not in tcols:
        # When the token was revoked, so "purge revocations older than N days" can mean
        # what it says. Rows revoked before this column existed keep NULL; readers fall
        # back to created_ts for those rather than treating them as age zero.
        conn.execute("ALTER TABLE tokens ADD COLUMN revoked_ts TEXT")
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(rooms)")}
    for col, decl in (
        ("description", "TEXT"),
        ("repo_url", "TEXT"),
        ("onboarding_notes", "TEXT"),
        ("retention_days", "INTEGER"),
    ):
        if col not in rcols:
            conn.execute(f"ALTER TABLE rooms ADD COLUMN {col} {decl}")


_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Run init_db once per process instead of once per request.

    init_db is idempotent, but it is ~20 statements (the CREATE script plus the
    PRAGMA table_info migration probes). The request path used to pay that on
    every call *before* checking the credential, which let an unauthenticated
    caller drive real work per request. Doing it once leaves auth as a single
    indexed SELECT. The server calls this; `admin init` still calls init_db
    directly, which is what actually creates the file.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        init_db(conn)
        _SCHEMA_READY = True


# ---------------------------------------------------------------- tokens

def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def revoke_agent(conn: sqlite3.Connection, agent: str) -> int:
    """Revoke every live token for an agent. Returns how many were revoked.

    Shared by the CLI and the admin API so both stamp revoked_ts identically; a
    revocation recorded by one and not the other would quietly break age-based purging.
    """
    cur = conn.execute(
        "UPDATE tokens SET revoked=1, revoked_ts=? WHERE agent_name=? AND revoked=0",
        (now(), agent),
    )
    return cur.rowcount or 0


def purge_revoked_tokens(conn: sqlite3.Connection, older_than_days: int | None = None) -> int:
    """Delete revoked token rows. Returns how many were removed.

    `revoked=1` is in the WHERE clause unconditionally — a live credential must never be
    removable by this path, whatever the age filter says. Age is measured from revoked_ts,
    falling back to created_ts for rows revoked before that column existed.
    """
    if older_than_days and int(older_than_days) > 0:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=int(older_than_days))).isoformat(timespec="seconds")
        cur = conn.execute(
            "DELETE FROM tokens WHERE revoked=1 AND COALESCE(revoked_ts, created_ts) < ?",
            (cutoff,),
        )
    else:
        cur = conn.execute("DELETE FROM tokens WHERE revoked=1")
    return cur.rowcount or 0


class AuthError(Exception):
    """Bad or missing credential."""


class RoomError(Exception):
    """Caller asked for a room its token does not grant."""


class Identity:
    """Who the caller is and which rooms it may touch. Derived from the token."""

    __slots__ = ("agent", "default_room", "allowed_rooms", "readonly", "admin", "all_rooms")

    def __init__(self, agent: str, default_room: str, allowed_rooms: Sequence[str],
                 readonly: bool = False, admin: bool = False, all_rooms: bool = False):
        self.agent = agent
        self.default_room = default_room
        self.allowed_rooms = sorted({default_room, *allowed_rooms})
        self.readonly = bool(readonly)
        self.admin = bool(admin)
        self.all_rooms = bool(all_rooms)

    def room(self, requested: str | None) -> str:
        if requested is None or requested == "":
            return self.default_room
        if self.all_rooms or requested in self.allowed_rooms:
            return requested
        raise RoomError(
            f"token for agent {self.agent!r} does not grant room {requested!r}; "
            f"granted: {', '.join(self.allowed_rooms)}"
        )

    def visible_rooms(self, conn: sqlite3.Connection) -> list[str]:
        """Rooms this identity may see: all of them for an all_rooms token, else its grants."""
        if self.all_rooms:
            return list_rooms(conn)
        return self.allowed_rooms

    def __repr__(self) -> str:  # pragma: no cover
        flags = "".join(f", {f}" for f in ("readonly", "admin", "all_rooms") if getattr(self, f))
        return f"Identity(agent={self.agent!r}, rooms={self.allowed_rooms}{flags})"


def resolve_token(conn: sqlite3.Connection, token: str | None) -> Identity:
    """Map a bearer token to an Identity, touching last_seen."""
    if not token:
        raise AuthError("missing bearer token")
    th = hash_token(token)
    row = conn.execute(
        "SELECT agent_name, default_room, allowed_rooms, revoked, readonly, admin, all_rooms "
        "FROM tokens WHERE token_hash=?",
        (th,),
    ).fetchone()
    if row is None:
        raise AuthError("unknown token")
    if row["revoked"]:
        raise AuthError("token revoked")
    conn.execute("UPDATE tokens SET last_seen=? WHERE token_hash=?", (now(), th))
    try:
        allowed = json.loads(row["allowed_rooms"]) or []
    except (TypeError, ValueError):
        allowed = []
    return Identity(row["agent_name"], row["default_room"], allowed,
                    bool(row["readonly"]), bool(row["admin"]), bool(row["all_rooms"]))


# ---------------------------------------------------------------- rooms

def ensure_room(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO rooms(name, created_ts) VALUES (?,?)", (name, now()))


def list_rooms(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM rooms ORDER BY name")]


# ---------------------------------------------------------------- events

# Optional sink for events (the server registers an MQTT publisher here). Kept in the
# storage layer as a plain callback so db.py stays dependency-free; failures never
# propagate into the request path.
_EVENT_HOOK = None


def set_event_hook(fn) -> None:
    """Register a callable invoked with each event dict after it is logged."""
    global _EVENT_HOOK
    _EVENT_HOOK = fn


def log_event(
    conn: sqlite3.Connection,
    room: str,
    kind: str,
    actor: str,
    task_id: int | None = None,
    detail: str = "",
    message_id: int | None = None,
) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO events(room, ts, kind, task_id, actor, detail, message_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (room, ts, kind, task_id, actor, detail, message_id),
    )
    eid = int(cur.lastrowid or 0)
    if _EVENT_HOOK is not None:
        try:
            _EVENT_HOOK({"id": eid, "room": room, "ts": ts, "kind": kind, "actor": actor,
                         "task_id": task_id, "message_id": message_id, "detail": detail})
        except Exception:
            pass
    return eid


def event_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """One event as returned to agents. message_id/task_id are the stable citation."""
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "ts": row["ts"],
        "kind": row["kind"],
        "actor": row["actor"],
        "task_id": row["task_id"],
        "message_id": row["message_id"] if "message_id" in keys else None,
        "detail": row["detail"],
    }


def read_events(
    conn: sqlite3.Connection,
    room: str,
    since_id: int = 0,
    limit: int = 50,
    kind: str | None = None,
    task_id: int | None = None,
) -> list[sqlite3.Row]:
    """Event history, oldest first. Never touches a cursor — this is a read, not a catch-up."""
    sql = "SELECT * FROM events WHERE room=? AND id>?"
    args: list[Any] = [room, int(since_id)]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if task_id is not None:
        sql += " AND task_id=?"
        args.append(int(task_id))
    sql += " ORDER BY id LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    return conn.execute(sql, args).fetchall()


def max_event_id(conn: sqlite3.Connection, room: str) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM events WHERE room=?", (room,)).fetchone()
    return int(row["m"] or 0)


def get_cursor(conn: sqlite3.Connection, agent: str, room: str) -> int:
    row = conn.execute(
        "SELECT last_event_id FROM cursors WHERE agent_name=? AND room=?", (agent, room)
    ).fetchone()
    return int(row["last_event_id"]) if row else 0


def set_cursor(conn: sqlite3.Connection, agent: str, room: str, event_id: int) -> None:
    conn.execute(
        "INSERT INTO cursors(agent_name, room, last_event_id) VALUES (?,?,?) "
        "ON CONFLICT(agent_name, room) DO UPDATE SET "
        "last_event_id=MAX(last_event_id, excluded.last_event_id)",
        (agent, room, event_id),
    )


# ---------------------------------------------------------------- tasks

def _loads_ids(raw: Any) -> list[int]:
    try:
        val = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [int(x) for x in val]
    except (TypeError, ValueError):
        return []


def task_to_dict(conn: sqlite3.Connection, row: sqlite3.Row, with_notes: bool = False) -> dict[str, Any]:
    deps = _loads_ids(row["depends_on"])
    blocked_by: list[int] = []
    if deps:
        qs = ",".join("?" * len(deps))
        open_rows = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({qs}) AND room=? AND status != 'done'",
            (*deps, row["room"]),
        ).fetchall()
        blocked_by = [int(r["id"]) for r in open_rows]

    out: dict[str, Any] = {
        "id": int(row["id"]),
        "room": row["room"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "assignee": row["assignee"],
        "depends_on": deps,
        "blocked_by_unfinished": blocked_by,
        "version": int(row["version"]),
        "created_by": row["created_by"],
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }
    if with_notes:
        notes = conn.execute(
            "SELECT author, ts, body FROM notes WHERE task_id=? ORDER BY id", (row["id"],)
        ).fetchall()
        out["notes"] = [{"author": n["author"], "ts": n["ts"], "body": n["body"]} for n in notes]
    return out


def fetch_task(conn: sqlite3.Connection, task_id: int, room: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()


# ---------------------------------------------------------------- chat
# Room chat sits alongside the task board: durable, ordered, room-scoped
# messages for announcements and discussion that are not themselves work items.
# Every post also writes an event, so whats_new() (and the hook) surface chat the
# same way they surface board activity - an agent notices a peer's message
# without having to poll a separate channel.

def post_message(
    conn: sqlite3.Connection,
    room: str,
    author: str,
    body: str,
    reply_to: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages(room, ts, author, body, reply_to) VALUES (?,?,?,?,?)",
        (room, now(), author, body, reply_to),
    )
    return int(cur.lastrowid or 0)


def read_messages(
    conn: sqlite3.Connection,
    room: str,
    since_id: int = 0,
    limit: int = 50,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE room=? AND id>? ORDER BY id LIMIT ?",
        (room, since_id, max(1, min(int(limit), 200))),
    ).fetchall()


def max_message_id(conn: sqlite3.Connection, room: str) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM messages WHERE room=?", (room,)).fetchone()
    return int(row["m"] or 0)


def message_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ts": row["ts"],
        "author": row["author"],
        "body": row["body"],
        "reply_to": int(row["reply_to"]) if row["reply_to"] is not None else None,
    }


# ---------------------------------------------------------------- files
# Small file transfer: content lives as a BLOB in SQLite (cap enforced by the server).
# For sharing source/config between agents, not media.

def put_file(conn: sqlite3.Connection, room: str, name: str, content: bytes,
             mime: str, author: str, expires_at: str | None = None) -> tuple[int, str, int]:
    sha = hashlib.sha256(content).hexdigest()
    cur = conn.execute(
        "INSERT INTO files(room, name, sha256, size, mime, author, ts, content, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (room, name, sha, len(content), mime, author, now(),
         sqlite3.Binary(content), expires_at),
    )
    return int(cur.lastrowid or 0), sha, len(content)


def expiry_from_hours(hours: float | None) -> str | None:
    """Absolute UTC expiry from a relative lifetime. None/0 means it never expires."""
    if not hours or float(hours) <= 0:
        return None
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(hours=float(hours))).isoformat(timespec="seconds")


#: Files past expires_at are filtered from reads immediately, not just when the
#: background sweep next runs — an expired file should never be servable, and the
#: sweep interval (default an hour) is far too coarse to rely on for that.
_UNEXPIRED = "(expires_at IS NULL OR expires_at > ?)"


def delete_file(conn: sqlite3.Connection, file_id: int, room: str) -> sqlite3.Row | None:
    """Delete one file, returning its metadata row if it existed. Room-scoped."""
    row = conn.execute("SELECT id, room, name, sha256, size, mime, author, ts, expires_at "
                       "FROM files WHERE id=? AND room=?", (file_id, room)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM files WHERE id=? AND room=?", (file_id, room))
    return row


def purge_expired_files(conn: sqlite3.Connection) -> int:
    """Remove files past their own expires_at, across every room."""
    cur = conn.execute("DELETE FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?",
                       (now(),))
    return cur.rowcount or 0


def get_file(conn: sqlite3.Connection, file_id: int, room: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM files WHERE id=? AND room=? AND {_UNEXPIRED}",
        (file_id, room, now()),
    ).fetchone()


def list_files(conn: sqlite3.Connection, room: str, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT id, room, name, sha256, size, mime, author, ts, expires_at FROM files "
        f"WHERE room=? AND {_UNEXPIRED} ORDER BY id DESC LIMIT ?",
        (room, now(), max(1, min(int(limit), 500))),
    ).fetchall()


def file_meta_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": int(row["id"]), "name": row["name"], "sha256": row["sha256"],
        "size": int(row["size"]), "mime": row["mime"], "author": row["author"],
        "ts": row["ts"],
        "expires_at": row["expires_at"] if "expires_at" in keys else None,
    }


# ---------------------------------------------------------------- room info & retention

def get_room(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rooms WHERE name=?", (name,)).fetchone()


def room_info_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "name": row["name"],
        "created_ts": row["created_ts"],
        "description": row["description"],
        "repo_url": row["repo_url"],
        "onboarding_notes": row["onboarding_notes"],
        "retention_days": row["retention_days"],
    }


def set_room_info(conn: sqlite3.Connection, name: str, description: str | None = None,
                  repo_url: str | None = None, onboarding_notes: str | None = None) -> None:
    ensure_room(conn, name)
    sets: list[str] = []
    args: list[Any] = []
    for col, val in (("description", description), ("repo_url", repo_url),
                     ("onboarding_notes", onboarding_notes)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if sets:
        args.append(name)
        conn.execute(f"UPDATE rooms SET {', '.join(sets)} WHERE name=?", args)


def set_retention(conn: sqlite3.Connection, name: str, days: int | None) -> None:
    ensure_room(conn, name)
    conn.execute("UPDATE rooms SET retention_days=? WHERE name=?",
                 (int(days) if days else None, name))


def list_rooms_full(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in conn.execute("SELECT * FROM rooms ORDER BY name"):
        d = room_info_dict(r) or {}
        nm = r["name"]
        d["open_tasks"] = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE room=? AND status IN "
            "('pending','in_progress','blocked')", (nm,)).fetchone()["c"]
        d["messages"] = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE room=?", (nm,)).fetchone()["c"]
        d["files"] = conn.execute(
            "SELECT COUNT(*) c FROM files WHERE room=?", (nm,)).fetchone()["c"]
        out.append(d)
    return out


def delete_room(conn: sqlite3.Connection, name: str) -> dict[str, int]:
    """Cascade-remove a room's data. Tokens are left as-is (a room can be recreated)."""
    counts: dict[str, int] = {}
    for tbl in ("tasks", "notes", "events", "cursors", "messages", "files"):
        cur = conn.execute(f"DELETE FROM {tbl} WHERE room=?", (name,))
        counts[tbl] = cur.rowcount or 0
    conn.execute("DELETE FROM rooms WHERE name=?", (name,))
    return counts


def prune_room(conn: sqlite3.Connection, room: str, retention_days: int | None) -> int:
    """Delete events/messages/files older than retention_days. Tasks are never pruned."""
    if not retention_days or int(retention_days) <= 0:
        return 0
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=int(retention_days))).isoformat(timespec="seconds")
    n = 0
    for tbl in ("events", "messages", "files"):
        cur = conn.execute(f"DELETE FROM {tbl} WHERE room=? AND ts < ?", (room, cutoff))
        n += cur.rowcount or 0
    return n


def prune_all(conn: sqlite3.Connection) -> int:
    total = purge_expired_files(conn)
    for r in conn.execute(
        "SELECT name, retention_days FROM rooms WHERE retention_days IS NOT NULL AND retention_days > 0"
    ).fetchall():
        total += prune_room(conn, r["name"], r["retention_days"])
    return total
