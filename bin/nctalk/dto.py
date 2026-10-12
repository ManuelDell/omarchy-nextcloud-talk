"""Read-only Talk mirror projections. No network or write connections."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any
from . import polls, prefs, store
from .avatars import avatar_fields

SYSTEMS = ("conversation_created", "room_created", "user_added", "guest_added", "user_removed", "user_joined", "user_left")
VISIBLE = "(m.message_type!='system' OR m.system_message IN (" + ",".join("'" + s + "'" for s in SYSTEMS) + "))"


def decode(value: str, default: Any) -> Any:
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


def text(row: Any) -> str:
    return "" if row["is_deleted"] else store.render_text(row["message"], decode(row["params"], {}))


def snapshot(chat: dict[str, Any]) -> dict[str, Any]:
    return {"unread": chat["unread"], "timestamp": chat["timestamp"], "message_id": chat["last_message_id"]}


def badge(unread: int, timestamp: int, mid: str, ack: dict[str, Any]) -> int:
    if not ack:
        return unread
    if timestamp <= int(ack.get("timestamp", 0)) and mid == str(ack.get("message_id", mid)):
        return max(0, unread - int(ack.get("unread", 0)))
    return min(unread, max(1, unread - int(ack.get("unread", 0)))) if unread else 0


class Reader:
    def __init__(self, account: dict[str, Any], preferences: dict[str, Any] | None = None) -> None:
        self.account = account
        self.identity = account["account"]
        self.preferences = preferences if preferences is not None else prefs.load_preferences()
        self.state = prefs.account_state(self.preferences, self.identity)
        self.conn = store.connect_ro(prefs.store_dir(self.identity) / "talk.db")
        self.has_polls = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='polls'").fetchone() is not None

    def close(self) -> None:
        self.conn.close()

    def chat(self, token: str) -> dict[str, str]:
        row = self.conn.execute("SELECT token,display_name,type FROM rooms WHERE token=?", (token,)).fetchone()
        if row is None:
            raise ValueError("chat unavailable in local mirror")
        return {"jid": row["token"], "name": row["display_name"], "kind": "dm" if row["type"] in (1, 5) else "group"}

    def peer_jid(self, room: sqlite3.Row) -> str:
        if room["type"] != 1:
            return ""
        mine = self.account.get("user", "")
        if room["object_type"] in ("user", "users") and room["object_id"] and room["object_id"] != mine:
            return "users/" + room["object_id"]
        peers = self.conn.execute("SELECT actor_id FROM members WHERE token=? AND actor_type='users' "
                                  "AND actor_id!=? ORDER BY actor_id", (room["token"], mine)).fetchall()
        if len(peers) == 1:
            return "users/" + peers[0][0]
        # Talk one-to-one `name` is the peer's user ID, not displayName.
        if not peers and room["name"] and room["name"] != mine:
            return "users/" + room["name"]
        return ""

    def chats(self, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
        args: list[Any] = []
        where = "r.removed=0"
        if query:
            where += " AND r.display_name LIKE ? ESCAPE '\\'"
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        hidden = self.state["hidden_rooms"]
        if hidden:
            where += " AND r.token NOT IN (" + ",".join("?" for _ in hidden) + ")"
            args.extend(hidden)
        rows = self.conn.execute(
            "SELECT r.*,a.path AS avatar_path,a.emoji AS avatar_emoji,a.color AS avatar_color,m.message,m.params,m.is_deleted,m.actor_type AS sender_type,"
            "m.actor_id AS sender_id,m.actor_name,m.media_type FROM rooms r "
            "LEFT JOIN avatars a ON a.token=r.token LEFT JOIN messages m "
            "ON m.token=r.token AND m.id=r.last_message_id WHERE " + where +
            " ORDER BY r.is_favorite DESC,r.last_activity DESC,r.display_name COLLATE NOCASE,r.token LIMIT ?", (*args, limit)).fetchall()
        return [{"jid": r["token"], "account": self.identity,
                 "account_label": str(self.account.get("label") or self.identity),
                 "name": r["display_name"], "kind": "dm" if r["type"] in (1, 5) else "group", "room_type": r["type"],
                 "peer_jid": self.peer_jid(r),
                 "timestamp": r["last_activity"], "last_message_id": str(r["last_message_id"]),
                 "preview": " ".join(text(r).split())[:512] if r["message"] is not None else "",
                 "last_media_type": r["media_type"] or "", "last_sender": r["actor_name"] or "",
                 "last_from_me": r["sender_type"] == "users" and r["sender_id"] == self.account.get("user"),
                 "last_status": "", "unread": r["unread_messages"],
                 "notification_unread": 0 if (r["is_archived"] or r["notification_level"] == 3 or (
                     r["sender_type"] == "users" and r["sender_id"] == self.account.get("user")
                     and r["token"] in self.state["acknowledged_unread"])) else badge(
                     r["unread_messages"], r["last_activity"], str(r["last_message_id"]),
                     self.state["acknowledged_unread"].get(r["token"], {})),
                 "mentioned": bool(r["unread_mention"] and r["unread_messages"]), "pinned": bool(r["is_favorite"]),
                 "archived": bool(r["is_archived"]), "muted": r["notification_level"] == 3,
                 **avatar_fields(r["avatar_path"], r["avatar_emoji"], r["avatar_color"])} for r in rows]

    def messages(self, token: str, query: str = "", limit: int = 240,
                 before: Any = None, kind: str = "") -> dict[str, Any]:
        chat = self.chat(token)
        where = ["m.token=?", VISIBLE]
        args: list[Any] = [token]
        join = ""
        if query:
            match = store.fts_query(query)
            if match:
                join = " JOIN messages_fts ON messages_fts.rowid=m.rowid"
                where.append("messages_fts MATCH ?")
                args.append(match)
        if before is not None:
            if not isinstance(before, dict) or type(before.get("ts")) is not int:
                raise ValueError("invalid pagination cursor")
            try:
                mid = int(before["id"])
            except (KeyError, ValueError, TypeError):
                raise ValueError("invalid pagination cursor") from None
            where.append("(m.timestamp < ? OR (m.timestamp=? AND m.id < ?))")
            args.extend((before["ts"], before["ts"], mid))
        if kind == "media":
            where.append("(m.media_type IN ('image','video','gif','audio') OR m.message_type='voice-message') AND m.is_deleted=0")
        elif kind == "docs":
            where.append("m.media_type='document' AND m.is_deleted=0")
        elif kind == "links":
            where.append("(m.message LIKE '%https://%' OR m.message LIKE '%http://%') AND m.is_deleted=0")
        elif kind:
            raise ValueError("unsupported message filter")
        rows = self.conn.execute(
            "SELECT m.*,p.actor_name AS q_sender,p.message AS q_message,p.params AS q_params,"
            "p.media_type AS q_media,p.is_deleted AS q_deleted,s.message_id AS star "
            "FROM messages m" + join + " LEFT JOIN messages p ON p.token=m.token AND p.id=m.parent_id "
            "LEFT JOIN stars s ON s.token=m.token AND s.message_id=m.id WHERE " + " AND ".join(where) +
            " ORDER BY m.timestamp DESC,m.id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        reactions: dict[int, list[dict[str, Any]]] = {}
        if rows:
            ids = [r["id"] for r in rows]
            details = self.conn.execute("SELECT * FROM reactions WHERE token=? AND message_id IN (" +
                                        ",".join("?" for _ in ids) + ") ORDER BY message_id,emoji,actor_type,actor_id", (token, *ids))
            for item in details:
                reactions.setdefault(item["message_id"], []).append({
                    "emoji": item["emoji"], "from_me": item["actor_type"] == "users" and item["actor_id"] == self.account.get("user"),
                    "sender": item["actor_name"], "sender_jid": item["actor_type"] + "/" + item["actor_id"]})
        result = []
        for r in rows:
            items = reactions.get(r["id"], [])
            if not items:
                mine = decode(r["reactions_self"], [])
                for emoji, count in sorted(decode(r["reactions_json"], {}).items()):
                    try:
                        count = min(1000, max(0, int(count)))
                    except (TypeError, ValueError):
                        continue
                    items.extend({"emoji": emoji, "from_me": i == 0 and emoji in mine,
                                  "sender": "", "sender_jid": ""} for i in range(count))
            pid = polls.poll_id(decode(r["params"], {})) if not r["is_deleted"] else 0
            poll = self.conn.execute("SELECT * FROM polls WHERE token=? AND poll_id=?", (token, pid)).fetchone() if pid and self.has_polls else None
            result.append({"id": str(r["id"]), "reference_id": str(r["reference_id"] or ""),
                           "text": "" if pid else text(r), "sender": r["actor_name"],
                           "sender_jid": r["actor_type"] + "/" + r["actor_id"], "timestamp": r["timestamp"],
                           "from_me": r["actor_type"] == "users" and r["actor_id"] == self.account.get("user"),
                           "media_type": "audio" if r["message_type"] == "voice-message" else r["media_type"],
                           "mime_type": r["file_mime"], "local_path": r["local_path"],
                           "filename": r["file_name"], "file_size": r["file_size"], "media_unavailable": bool(r["is_deleted"]),
                           "edited": bool(r["edited_ts"]), "forwarded": False, "starred": r["star"] is not None,
                           "revoked": bool(r["is_deleted"]), "system": r["message_type"] == "system",
                           "voice": r["message_type"] == "voice-message", "quoted_id": str(r["parent_id"] or ""),
                           "quoted_sender": r["q_sender"] or "",
                           "quoted_text": "" if r["q_deleted"] else store.render_text(r["q_message"] or "", decode(r["q_params"], {})),
                           "quoted_media_type": r["q_media"] or "", "reactions": items, "buttons": [],
                           "album_id": "", "album_index": 0, "album_count": 0,
                           "latitude": None, "longitude": None, "location_name": "", "location_address": "", "location_live": False,
                           **({"poll": polls.projection(poll)} if poll is not None else {})})
        # Pending bubbles are owned by Service.qml; the original never reads an outbox.
        return {"ok": True, "chat": chat, "query": query, "messages": result, "has_more": more}

    def members(self, token: str, query: str = "") -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM members WHERE token=? ORDER BY display_name,actor_type,actor_id", (token,))
        members = [{"jid": r["actor_type"] + "/" + r["actor_id"], "name": r["display_name"], "phone": "",
                    "role": {1: "owner", 2: "moderator", 4: "guest", 5: "guest", 6: "moderator"}.get(r["participant_type"], "user")}
                   for r in rows if not query or query.casefold() in r["display_name"].casefold()]
        return {"ok": True, "chat": self.chat(token), "query": query, "members": members}

    def details(self, token: str) -> dict[str, Any]:
        chat = self.chat(token)
        room = self.conn.execute("SELECT r.*,a.path AS avatar_path,a.emoji AS avatar_emoji,a.color AS avatar_color FROM rooms r LEFT JOIN avatars a ON a.token=r.token WHERE r.token=?", (token,)).fetchone()
        counts = self.conn.execute("SELECT COUNT(*) AS total,COALESCE(MIN(m.timestamp),0) AS since,"
                                  "COALESCE(SUM(m.media_type IN ('image','video','gif','audio') OR m.message_type='voice-message'),0) AS media,"
                                  "COALESCE(SUM(m.media_type='document'),0) AS documents,"
                                  "COALESCE(SUM(m.message LIKE '%http://%' OR m.message LIKE '%https://%'),0) AS links,"
                                  "COALESCE(SUM(s.message_id IS NOT NULL),0) AS starred,"
                                  "COALESCE(SUM(m.media_type!='' AND m.local_path='' AND m.is_deleted=0),0) AS missing "
                                  "FROM messages m LEFT JOIN stars s ON s.token=m.token AND s.message_id=m.id "
                                  "WHERE m.token=? AND " + VISIBLE, (token,)).fetchone()
        members = self.members(token)["members"]
        result = {"ok": True, "kind": "chat-details", "chat": chat, "account": self.identity,
                  "archived": bool(room["is_archived"]), "pinned": bool(room["is_favorite"]),
                  "muted": room["notification_level"] == 3, "since": counts["since"],
                  "counts": {key: counts[key] for key in ("total", "media", "documents", "links", "starred", "missing")},
                  **avatar_fields(room["avatar_path"], room["avatar_emoji"], room["avatar_color"])}
        if chat["kind"] == "group":
            mine = "users/" + self.account.get("user", "")
            result["participants"] = [dict(m, me=m["jid"] == mine) for m in members]
            result["group"] = {"created_ts": counts["since"], "left": bool(room["removed"]),
                               "owner_name": next((m["name"] for m in members if m["role"] == "owner"), ""),
                               "participant_count": len(members), "my_role": next((m["role"] for m in members if m["jid"] == mine), "user")}
        else:
            result["person"] = {"phone": "", "full_name": chat["name"], "push_name": "", "business_name": "", "alias": "", "tags": []}
            result["groups_in_common"] = []
        return result


def account_status(account: dict[str, Any], value: dict[str, Any], active: str, default: str) -> dict[str, Any]:
    identity = account["account"]
    state = prefs.account_state(value, identity)
    ready, rail, sync, fresh, relink = False, False, False, False, False
    error = ""
    path = prefs.store_dir(identity)
    if (path / "talk.db").is_file():
        conn = None
        try:
            conn = store.connect_ro(path / "talk.db")
            rail = conn.execute("SELECT 1 FROM rooms WHERE removed=0 LIMIT 1").fetchone() is not None
            for table in ("messages", "messages_fts", "members", "users", "avatars", "reactions", "sync_state", "outbox", "stars"):
                conn.execute("SELECT * FROM " + table + " LIMIT 0")
            if store.schema_version(conn) < store.SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported mirror schema")
            meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('last_sync_ok_ts','last_error')"))
            ready = True
            try:
                age = time.time() - float(meta.get("last_sync_ok_ts", 0))
                sync = fresh = 0 <= age < 30
            except ValueError:
                pass
            relink = meta.get("last_error") == "auth"
            if meta.get("last_error"):
                error = "authentication required" if relink else "sync connection failed"
        except (sqlite3.Error, ValueError, OSError):
            ready = rail = False
            error = "local mirror unavailable"
        finally:
            if conn is not None:
                conn.close()
    # Bounded unit probe, never on the hot chats/messages paths.
    if not sync:
        import subprocess
        try:
            sync = subprocess.run(["systemctl", "--user", "is-active", "--quiet", prefs.unit(identity)],
                                  timeout=0.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    online = state["online"] and fresh and not error and not value["closed"]
    connection = ("relink-required" if relink else "closed" if value["closed"] else
                  "offline" if not state["online"] else "connected" if online else
                  "connecting" if sync else "stopped")
    return {"account": identity, "label": str(account.get("label") or identity), "store": str(path),
            "unit": prefs.unit(identity), "default": identity == default, "main": identity == default,
            "active": identity == active, "authenticated": True, "sync_active": sync,
            "needs_relink": relink, "connection_state": connection,
            "online": online, "offline_mode": not state["online"], "send_read_receipts": state["send_read_receipts"],
            "signature": state["signature"], "notifications_muted": state["notifications_muted"],
            "fts_enabled": True, "database_ready": ready, "rail_ready": rail, "error": error}


def setup(complete: bool) -> dict[str, Any]:
    return {"consented": complete, "agents": False, "previous_install": False, "complete": complete,
            "units": "omanctalk-sync@.service", "links": {}, "conflicts": [], "legacy_copies": False,
            "wacli": {"path": "", "found": False}, "zenity": False,
            "original_plugin": {"installed": False, "enabled": False}, "managed": False,
            "plugin_root": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}


def status(active: str = "") -> dict[str, Any]:
    accounts = prefs.load_accounts()
    default = next((a["account"] for a in accounts if a.get("default")), accounts[0]["account"] if accounts else "")
    active = active or default
    if active and active not in [a["account"] for a in accounts]:
        raise ValueError("unknown account")
    value = prefs.load_preferences()
    rows = [account_status(a, value, active, default) for a in accounts]
    selected = next((r for r in rows if r["account"] == active), {})
    from .notify import available
    return {**prefs.settings(active), "kind": "status", "installed": True, "helper_version": "0.1.0",
            "wacli_version": "", "setup": setup(bool(accounts)), "authenticated": bool(selected),
            "database_ready": selected.get("database_ready", False), "any_authenticated": bool(rows),
            "any_database_ready": any(r["database_ready"] for r in rows), "rail_ready": any(r["rail_ready"] for r in rows),
            "sync_active": selected.get("sync_active", False), "needs_relink": selected.get("needs_relink", False),
            "connection_state": selected.get("connection_state", "unlinked"), "online": selected.get("online", False),
            "offline_mode": selected.get("offline_mode", False), "accounts": rows,
            "notifications": value["notifications"], "notify_available": available(), "closed": bool(value["closed"]),
            "fts_enabled": True, "error": selected.get("error", "")}
