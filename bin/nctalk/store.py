"""SQLite mirror for OmaNCTalk: schema, migrations and idempotent upserts.

The sync daemon is the only network writer. Readers use ``connect_ro``.
Everything here is stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .paths import check_file, ensure_directory

SCHEMA_VERSION = 4

_DDL_V1 = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE rooms(
  token TEXT PRIMARY KEY, room_id INTEGER, type INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  object_type TEXT NOT NULL DEFAULT '', object_id TEXT NOT NULL DEFAULT '',
  last_activity INTEGER NOT NULL DEFAULT 0, last_message_id INTEGER NOT NULL DEFAULT 0,
  last_read_message INTEGER NOT NULL DEFAULT 0, last_common_read INTEGER NOT NULL DEFAULT 0,
  unread_messages INTEGER NOT NULL DEFAULT 0, unread_mention INTEGER NOT NULL DEFAULT 0,
  unread_mention_direct INTEGER NOT NULL DEFAULT 0,
  is_favorite INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0,
  notification_level INTEGER NOT NULL DEFAULT 1,
  read_only INTEGER NOT NULL DEFAULT 0, participant_type INTEGER NOT NULL DEFAULT 0,
  can_leave INTEGER NOT NULL DEFAULT 1, can_delete INTEGER NOT NULL DEFAULT 0,
  has_call INTEGER NOT NULL DEFAULT 0, avatar_version TEXT NOT NULL DEFAULT '',
  is_custom_avatar INTEGER NOT NULL DEFAULT 0, message_expiration INTEGER NOT NULL DEFAULT 0,
  actor_type TEXT NOT NULL DEFAULT '', actor_id TEXT NOT NULL DEFAULT '',
  removed INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL DEFAULT 0);
CREATE INDEX rooms_activity ON rooms(removed, last_activity DESC);

CREATE TABLE messages(
  rowid INTEGER PRIMARY KEY,
  token TEXT NOT NULL, id INTEGER NOT NULL,
  actor_type TEXT NOT NULL DEFAULT '', actor_id TEXT NOT NULL DEFAULT '', actor_name TEXT NOT NULL DEFAULT '',
  timestamp INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
  message_type TEXT NOT NULL DEFAULT 'comment', system_message TEXT NOT NULL DEFAULT '',
  parent_id INTEGER, reference_id TEXT NOT NULL DEFAULT '', thread_id INTEGER,
  is_deleted INTEGER NOT NULL DEFAULT 0, edited_ts INTEGER NOT NULL DEFAULT 0,
  expiration_ts INTEGER NOT NULL DEFAULT 0, markdown INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}',
  reactions_json TEXT NOT NULL DEFAULT '{}', reactions_self TEXT NOT NULL DEFAULT '[]',
  file_id INTEGER, file_name TEXT NOT NULL DEFAULT '', file_mime TEXT NOT NULL DEFAULT '',
  file_size INTEGER NOT NULL DEFAULT 0, file_path TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '{}',
  UNIQUE(token, id));
CREATE INDEX messages_room ON messages(token, id DESC);
CREATE INDEX messages_room_ts ON messages(token, timestamp DESC);
CREATE INDEX messages_media ON messages(token, media_type, id DESC) WHERE media_type != '';
CREATE VIRTUAL TABLE messages_fts USING fts5(
  search_text, content='messages', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
  INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;

CREATE TABLE reactions(
  token TEXT NOT NULL, message_id INTEGER NOT NULL, emoji TEXT NOT NULL,
  actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_name TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(token, message_id, emoji, actor_type, actor_id));
CREATE TABLE members(
  token TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '', participant_type INTEGER NOT NULL DEFAULT 0,
  last_seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(token, actor_type, actor_id));
CREATE TABLE sync_state(
  token TEXT PRIMARY KEY, newest_id INTEGER NOT NULL DEFAULT 0, oldest_id INTEGER NOT NULL DEFAULT 0,
  history_complete INTEGER NOT NULL DEFAULT 0, members_synced_at INTEGER NOT NULL DEFAULT 0,
  last_sync_ts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, local_id TEXT NOT NULL UNIQUE, token TEXT NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE stars(token TEXT NOT NULL, message_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY(token, message_id));
CREATE TABLE avatars(token TEXT PRIMARY KEY, version TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '', checked_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE users(actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(actor_type, actor_id));
"""

# Ordered migrations: (version, script). Append new entries; never edit old ones.
# v2: remember the room activity already checked, so edits/reactions/deletions of the
# last message (which leave the room's lastMessage id unchanged) are still fetched.
_DDL_V2 = "ALTER TABLE sync_state ADD COLUMN activity_seen INTEGER NOT NULL DEFAULT 0;"

# v3: generated emoji avatars are drawn by the UI (emoji + colour), not rendered from SVG.
_DDL_V3 = """
ALTER TABLE avatars ADD COLUMN emoji TEXT NOT NULL DEFAULT '';
ALTER TABLE avatars ADD COLUMN color TEXT NOT NULL DEFAULT '';
ALTER TABLE avatars ADD COLUMN scanned INTEGER NOT NULL DEFAULT 0;
"""

# v4: Talk polls (question, options, tallies and own votes), loaded by the daemon.
_DDL_V4 = """
CREATE TABLE polls(
  token TEXT NOT NULL, poll_id INTEGER NOT NULL, question TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]', max_votes INTEGER NOT NULL DEFAULT 0,
  result_mode INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 0,
  num_voters INTEGER NOT NULL DEFAULT 0, voted_self_json TEXT NOT NULL DEFAULT '[]',
  votes_json TEXT NOT NULL DEFAULT '{}', details_json TEXT NOT NULL DEFAULT '[]',
  actor_id TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(token, poll_id));
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _DDL_V1), (2, _DDL_V2), (3, _DDL_V3), (4, _DDL_V4)]


def now() -> int:
    return int(time.time())


# --- connections ---------------------------------------------------------

def _prepare_dir(path: Path) -> None:
    ensure_directory(path)


def connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Read-write connection for the daemon (creates and migrates)."""
    p = Path(db_path)
    _prepare_dir(p.parent)
    for suffix in ("", "-wal", "-shm"):
        check_file(str(p) + suffix)
    conn = sqlite3.connect(str(p), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(str(p) + suffix, 0o600, follow_symlinks=False)
        except OSError:
            pass
    return conn


def connect_ro(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Read-only connection for the UI helper (never creates the file)."""
    for suffix in ("", "-wal", "-shm"):
        check_file(str(db_path) + suffix)
    # Path.as_uri() would import urllib.request (~40 ms) on every helper call.
    from urllib.parse import quote
    uri = "file:" + quote(str(Path(db_path).absolute()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
    current = schema_version(conn)
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_script(script):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                         (version, now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _split_script(script: str) -> Iterable[str]:
    """Split a DDL script on top-level semicolons (triggers contain inner ones)."""
    buf: list[str] = []
    in_trigger = False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                in_trigger = False
                yield "\n".join(buf).rstrip(";")
                buf = []
        elif stripped.endswith(";"):
            yield "\n".join(buf).rstrip(";")
            buf = []
    if buf:
        yield "\n".join(buf)


# --- meta ----------------------------------------------------------------

def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None or row[0] is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# --- normalisation helpers ----------------------------------------------

def _b(value: Any) -> int:
    return 1 if value else 0


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def media_type_for(mime: str, name: str = "", message_type: str = "") -> str:
    """Map a mime type to the UI media type vocabulary."""
    mime = (mime or "").lower()
    if message_type == "voice-message":
        return "audio"
    if mime == "image/gif":
        return "gif"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime or name:
        return "document"
    return ""


def render_text(message: str, params: Mapping[str, Any] | None) -> str:
    """Replace Talk ``{placeholder}`` tokens by readable text."""
    text = message or ""
    for key, value in (params or {}).items():
        if not isinstance(value, Mapping):
            continue
        kind = _s(value.get("type"))
        name = _s(value.get("name"))
        if kind in ("user", "guest", "federated_user", "user-group", "call"):
            replacement = "@" + name if key.startswith("mention") else name
        elif kind == "file":
            replacement = name
        else:
            replacement = name
        text = text.replace("{" + key + "}", replacement)
    return text


def build_search_text(message: str, params: Mapping[str, Any] | None, actor_name: str,
                      file_name: str) -> str:
    parts = [render_text(message, params), file_name, actor_name]
    return " ".join(p for p in parts if p).strip()


# --- upserts ---------------------------------------------------------------

def upsert_room(conn: sqlite3.Connection, room: Mapping[str, Any]) -> None:
    """Upsert a room from a Talk ``/room`` entry (api v4)."""
    token = _s(room.get("token"))
    if not token:
        raise ValueError("room without token")
    last = room.get("lastMessage") if isinstance(room.get("lastMessage"), Mapping) else {}
    last_id = _i(last.get("id")) if last else 0
    values = {
        "token": token,
        "room_id": _i(room.get("id")),
        "type": _i(room.get("type"), 2),
        "name": _s(room.get("name")),
        "display_name": _s(room.get("displayName")) or _s(room.get("name")),
        "description": _s(room.get("description")),
        "object_type": _s(room.get("objectType")),
        "object_id": _s(room.get("objectId")),
        "last_activity": _i(room.get("lastActivity")),
        "last_message_id": last_id,
        "last_read_message": _i(room.get("lastReadMessage")),
        "last_common_read": _i(room.get("lastCommonReadMessage")),
        "unread_messages": _i(room.get("unreadMessages")),
        "unread_mention": _b(room.get("unreadMention")),
        "unread_mention_direct": _b(room.get("unreadMentionDirect")),
        "is_favorite": _b(room.get("isFavorite")),
        "is_archived": _b(room.get("isArchived")),
        "notification_level": _i(room.get("notificationLevel"), 1),
        "read_only": _b(room.get("readOnly")),
        "participant_type": _i(room.get("participantType")),
        "can_leave": _b(room.get("canLeaveConversation", True)),
        "can_delete": _b(room.get("canDeleteConversation")),
        "has_call": _b(room.get("hasCall")),
        "avatar_version": _s(room.get("avatarVersion")),
        "is_custom_avatar": _b(room.get("isCustomAvatar")),
        "message_expiration": _i(room.get("messageExpiration")),
        "actor_type": _s(room.get("actorType")),
        "actor_id": _s(room.get("actorId")),
        "removed": 0,
        "raw": json.dumps(room, separators=(",", ":"), ensure_ascii=False),
        "updated_at": now(),
    }
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != "token")
    conn.execute(
        f"INSERT INTO rooms({cols}) VALUES ({marks}) ON CONFLICT(token) DO UPDATE SET {updates}",
        tuple(values.values()))


def upsert_message(conn: sqlite3.Connection, token: str, msg: Mapping[str, Any],
                   *, keep_local_path: bool = True) -> None:
    """Upsert one chat message dict as returned by the Talk chat API."""
    mid = _i(msg.get("id"))
    if not mid:
        raise ValueError("message without id")
    params = msg.get("messageParameters")
    if not isinstance(params, Mapping):  # Talk returns [] for "none"
        params = {}
    file_param = params.get("file") if isinstance(params.get("file"), Mapping) else {}
    message_type = _s(msg.get("messageType")) or "comment"
    if _s(msg.get("systemMessage")):
        message_type = "system"
    file_name = _s(file_param.get("name"))
    file_mime = _s(file_param.get("mimetype"))
    parent = msg.get("parent") if isinstance(msg.get("parent"), Mapping) else None
    reactions = msg.get("reactions") if isinstance(msg.get("reactions"), Mapping) else {}
    reactions_self = msg.get("reactionsSelf") if isinstance(msg.get("reactionsSelf"), list) else []
    actor_name = _s(msg.get("actorDisplayName"))
    text = _s(msg.get("message"))
    deleted = 1 if _s(msg.get("systemMessage")) in ("message_deleted",) or msg.get("deleted") else 0
    values = {
        "token": token,
        "id": mid,
        "actor_type": _s(msg.get("actorType")),
        "actor_id": _s(msg.get("actorId")),
        "actor_name": actor_name,
        "timestamp": _i(msg.get("timestamp")),
        "message": text,
        "message_type": message_type,
        "system_message": _s(msg.get("systemMessage")),
        "parent_id": _i(parent.get("id")) if parent else None,
        "reference_id": _s(msg.get("referenceId")),
        "thread_id": _i(msg.get("threadId")) or None,
        "is_deleted": deleted,
        "edited_ts": _i(msg.get("lastEditTimestamp")),
        "expiration_ts": _i(msg.get("expirationTimestamp")),
        "markdown": _b(msg.get("markdown")),
        "params": json.dumps(params, separators=(",", ":"), ensure_ascii=False),
        "reactions_json": json.dumps(reactions, separators=(",", ":"), ensure_ascii=False),
        "reactions_self": json.dumps(reactions_self, ensure_ascii=False),
        "file_id": _i(file_param.get("id")) or None,
        "file_name": file_name,
        "file_mime": file_mime,
        "file_size": _i(file_param.get("size")),
        "file_path": _s(file_param.get("path")),
        "media_type": media_type_for(file_mime, file_name, _s(msg.get("messageType")))
        if file_param else "",
        "search_text": ""
        if message_type == "system"
        else build_search_text(text, params, actor_name, file_name),
        "raw": json.dumps(msg, separators=(",", ":"), ensure_ascii=False),
    }
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    skip = {"token", "id"} | ({"local_path"} if keep_local_path else set())
    updates = ", ".join(f"{c}=excluded.{c}" for c in values if c not in skip)
    conn.execute(
        f"INSERT INTO messages({cols}) VALUES ({marks}) "
        f"ON CONFLICT(token, id) DO UPDATE SET {updates}",
        tuple(values.values()))


def replace_reactions(conn: sqlite3.Connection, token: str, message_id: int,
                      details: Mapping[str, Iterable[Mapping[str, Any]]]) -> None:
    """Replace the per-actor reaction list of one message (``{emoji: [actor,...]}``)."""
    conn.execute("DELETE FROM reactions WHERE token=? AND message_id=?", (token, message_id))
    for emoji, actors in details.items():
        for actor in actors:
            conn.execute(
                "INSERT OR REPLACE INTO reactions(token, message_id, emoji, actor_type, actor_id, "
                "actor_name, ts) VALUES (?,?,?,?,?,?,?)",
                (token, message_id, emoji, _s(actor.get("actorType")), _s(actor.get("actorId")),
                 _s(actor.get("actorDisplayName")), _i(actor.get("timestamp"))))


def upsert_member(conn: sqlite3.Connection, token: str, member: Mapping[str, Any]) -> None:
    actor_type = _s(member.get("actorType"))
    actor_id = _s(member.get("actorId"))
    if not actor_type or not actor_id:
        return
    name = _s(member.get("displayName"))
    conn.execute(
        "INSERT INTO members(token, actor_type, actor_id, display_name, participant_type, last_seen) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(token, actor_type, actor_id) DO UPDATE SET "
        "display_name=excluded.display_name, participant_type=excluded.participant_type, "
        "last_seen=excluded.last_seen",
        (token, actor_type, actor_id, name, _i(member.get("participantType")),
         _i(member.get("lastPing"))))
    if name:
        conn.execute(
            "INSERT INTO users(actor_type, actor_id, display_name, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(actor_type, actor_id) DO UPDATE SET display_name=excluded.display_name, "
            "updated_at=excluded.updated_at", (actor_type, actor_id, name, now()))


def mark_missing_rooms_removed(conn: sqlite3.Connection, present_tokens: Iterable[str]) -> int:
    """Flag rooms absent from a *full* server listing as removed (data stays searchable)."""
    present = set(present_tokens)
    rows = conn.execute("SELECT token FROM rooms WHERE removed=0").fetchall()
    gone = [r[0] for r in rows if r[0] not in present]
    for token in gone:
        conn.execute("UPDATE rooms SET removed=1 WHERE token=?", (token,))
    return len(gone)


def set_sync_state(conn: sqlite3.Connection, token: str, **fields: Any) -> None:
    allowed = {"newest_id", "oldest_id", "history_complete", "members_synced_at", "last_sync_ts",
               "activity_seen"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown sync_state fields: {sorted(unknown)}")
    conn.execute("INSERT OR IGNORE INTO sync_state(token) VALUES (?)", (token,))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE sync_state SET {sets} WHERE token=?", (*fields.values(), token))


# --- search ----------------------------------------------------------------

def fts_query(text: str) -> str:
    """Build a safe FTS5 query: every token quoted, prefix-matched, AND-ed."""
    tokens = [t for t in text.replace('"', " ").split() if t]
    return " AND ".join('"' + t + '"*' for t in tokens[:12])
