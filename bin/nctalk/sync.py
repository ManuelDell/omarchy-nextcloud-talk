"""Single-account mirror daemon. Reads never advance server read markers.

Small transactions commit messages and pagination cursors together. Backfill
is resumable and globally limited to one request per 0.3 seconds. Remote error
bodies and message/account content are never logged.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import uuid
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import avatars, polls, store
from .api import ApiError, GuardError, TalkClient
from .paths import check_file, ensure_directory, directory_fd

POLL_SECONDS = 3.0
BACKFILL_SECONDS = 0.3
FULL_REFRESH_SECONDS = 300
MEMBERS_SECONDS = 6 * 3600
# Talk raises a room's lastActivity only for new comments, never for edits, reactions or
# deletions (they arrive as system messages in the chat stream). So rooms are re-polled on
# a schedule that follows how recently they were active, within a per-cycle request budget.
POLL_HOT_SECONDS = 3          # activity in the last 24 h
POLL_WARM_SECONDS = 15        # activity in the last 7 days
POLL_COLD_SECONDS = 120       # older
POLLS_PER_CYCLE = 6


def retry_delay(exc: ApiError, failures: int) -> float:
    return max(min(900.0, exc.retry_after), min(60.0, 3.0 * 2 ** min(failures - 1, 5)))


def state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME", "")
    return (Path(root) if os.path.isabs(root) else Path.home() / ".local/state") / "omanctalk"


def accounts_file() -> Path:
    return state_dir() / "accounts.json"


def store_dir(account_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9._@-]+", account_id) or account_id in {".", ".."}:
        raise ValueError("invalid account identity")
    return state_dir() / "stores" / account_id


def load_account(account_id: str) -> dict[str, Any]:
    store_dir(account_id)
    try:
        with accounts_file().open(encoding="utf-8") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError
        rows = json.loads(raw).get("accounts", [])
        for row in rows:
            if isinstance(row, dict) and (row.get("id") or row.get("account")) == account_id:
                if not all(isinstance(row.get(k), str) and row[k] for k in ("server", "user", "app_password")):
                    raise ValueError
                guard = row.get("write_guard_prefix")
                if guard is not None and (not isinstance(guard, str) or not guard):
                    raise ValueError
                return row
    except (OSError, ValueError, AttributeError, TypeError):
        raise ValueError("invalid account configuration") from None
    raise ValueError("account not configured")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _atomic_bytes(path: Path, data: bytes) -> None:
    with directory_fd(path.parent, create=True) as directory:
        check_file(path)
        name = ".omanctalk-" + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


class SyncDaemon:
    def __init__(self, account_id: str, client: TalkClient,
                 directory: str | os.PathLike[str] | None = None, *,
                 stop_event: threading.Event | None = None) -> None:
        self.account_id, self.client = account_id, client
        self.directory = Path(directory) if directory is not None else store_dir(account_id)
        self.conn = store.connect(self.directory / "talk.db")
        self.stop_event = stop_event or threading.Event()
        self.client.cancelled = self.stop_event.is_set
        if self.client.sleep is time.sleep or isinstance(getattr(self.client.sleep, "__self__", None), SyncDaemon):
            self.client.sleep = self._retry_wait
        self.initialized = False
        self.auth_failed = False
        self._last_full = 0.0
        self._last_backfill = 0.0
        self._next_poll: dict[str, float] = {}
        self._backfill_rotation = 0
        self._media_cursor = 0
        self._poll_budget = 4
        self._pending_polls: dict[tuple[str, int], bool] = {}
        self._poll_scan_cursor = 0
        for key, value in (("account_id", account_id), ("user", client.user), ("server", client.server)):
            store.meta_set(self.conn, key, value)
        self._recover_outbox()

    def _retry_wait(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise ApiError(0, "request interrupted")

    def _recover_outbox(self) -> None:
        # A restarting daemon must not invalidate a still-running helper's claim.
        for row in self.conn.execute("SELECT id,payload FROM outbox WHERE state='sending'").fetchall():
            try:
                payload = json.loads(row["payload"])
                pid = payload.get("_owner_pid", 0) if isinstance(payload, dict) else 0
                if isinstance(pid, int) and pid > 0:
                    os.kill(pid, 0)
                    continue
            except (OSError, ValueError, TypeError):
                pass
            self.conn.execute("UPDATE outbox SET state='failed',error='interrupted; verify before retry' "
                              "WHERE id=? AND state='sending'", (row["id"],))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SyncDaemon:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _state(self, token: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sync_state WHERE token=?", (token,)).fetchone()
        return dict(row) if row else {"newest_id": 0, "oldest_id": 0, "history_complete": 0,
                                     "members_synced_at": 0, "last_sync_ts": 0}

    def initialize(self) -> None:
        if self.initialized:
            return
        caps = self.client.capabilities()
        store.meta_set(self.conn, "capabilities_json", json.dumps(caps, ensure_ascii=False))
        self.initialized = True

    def _event_key(self, token: str, mid: int) -> str:
        return "message_event:" + token + ":" + str(mid)

    def _upsert_normal(self, token: str, msg: dict[str, Any]) -> None:
        mid = _int(msg.get("id"))
        if not mid:
            return
        if _int(store.meta_get(self.conn, self._event_key(token, mid))) > mid:
            # Older history/reply snapshots cannot undo a later system event.
            return
        store.upsert_message(self.conn, token, msg)
        pid = polls.poll_id(msg.get("messageParameters"))
        if pid:
            poll = self.conn.execute("SELECT * FROM polls WHERE token=? AND poll_id=?", (token, pid)).fetchone()
            if poll is not None:
                row = self.conn.execute("SELECT * FROM messages WHERE token=? AND id=?", (token, mid)).fetchone()
                polls.index_message(self.conn, row, poll)
        if msg.get("deleted"):
            self.conn.execute("UPDATE messages SET message='', search_text='' WHERE token=? AND id=?", (token, mid))

    def _apply_page(self, token: str, messages: list[dict[str, Any]], last_given: int,
                    common_read: int, *, history: bool) -> None:
        records = [m for m in messages if isinstance(m, dict) and _int(m.get("id")) > 0]
        if messages and not records:
            raise ApiError(0, "invalid message page")
        reaction_ids: set[int] = set()
        for msg in records:
            parent = msg.get("parent")
            if str(msg.get("systemMessage", "")).startswith("reaction") and isinstance(parent, dict):
                reaction_ids.add(_int(parent.get("id")))
            if isinstance(msg.get("reactions"), dict) and msg["reactions"]:
                reaction_ids.add(_int(msg.get("id")))
        details: dict[int, dict[str, Any]] = {}
        if "reactions" in self.client.features:
            for mid in reaction_ids - {0}:
                try:
                    details[mid] = self.client.reaction_details(token, mid)
                except ApiError as exc:
                    if exc.status != 404:
                        raise
        state = self._state(token)
        with transaction(self.conn):
            for msg in sorted(records, key=lambda m: _int(m["id"])):
                event = str(msg.get("systemMessage") or "")
                parent = msg.get("parent")
                actionable = (event == "message_edited" or event.startswith("message_deleted")
                              or event == "message_expired" or event.startswith("reaction"))
                if isinstance(parent, dict) and _int(parent.get("id")):
                    mid = _int(parent["id"])
                    previous = self.conn.execute("SELECT raw FROM messages WHERE token=? AND id=?", (token, mid)).fetchone()
                    if actionable:
                        key = self._event_key(token, mid)
                        if _int(msg["id"]) > _int(store.meta_get(self.conn, key)):
                            merged = json.loads(previous["raw"]) if previous else {}
                            merged.update(parent)
                            merged["messageType"] = merged.get("messageType") or "comment"
                            merged["systemMessage"] = ""
                            if merged["messageType"] == "system":
                                merged["messageType"] = "comment"
                            deleted = event.startswith("message_deleted") or event == "message_expired"
                            if deleted:
                                merged.update(deleted=True, message="", messageParameters={}, reactions={}, reactionsSelf=[])
                            store.upsert_message(self.conn, token, merged)
                            if deleted:
                                self.conn.execute("UPDATE messages SET is_deleted=1, search_text='' WHERE token=? AND id=?", (token, mid))
                                store.replace_reactions(self.conn, token, mid, {})
                            store.meta_set(self.conn, key, _int(msg["id"]))
                    elif not previous:
                        self._upsert_normal(token, parent)
                if event:
                    store.upsert_message(self.conn, token, msg)
                else:
                    self._upsert_normal(token, msg)
            for mid, actors in details.items():
                deleted = self.conn.execute("SELECT is_deleted FROM messages WHERE token=? AND id=?", (token, mid)).fetchone()
                if deleted and deleted[0]:
                    store.replace_reactions(self.conn, token, mid, {})
                    continue
                # Unknown actor fields are ignored by store.py.
                cleaned = {emoji: [a for a in rows if isinstance(a, dict)] for emoji, rows in actors.items()
                           if isinstance(rows, list)}
                store.replace_reactions(self.conn, token, mid, cleaned)
                counts = {e: len(a) for e, a in cleaned.items() if a}
                mine = [e for e, a in cleaned.items() if any(x.get("actorType") == "users" and
                        x.get("actorId") == self.client.user for x in a)]
                self.conn.execute("UPDATE messages SET reactions_json=?, reactions_self=? WHERE token=? AND id=?",
                                  (json.dumps(counts, ensure_ascii=False), json.dumps(mine, ensure_ascii=False), token, mid))
            if records:
                ids = [_int(m["id"]) for m in records]
                oldest = min(ids)
                if history and last_given > 0:
                    oldest = min(oldest, last_given)
                newest = max(ids)
                if not history:
                    newest = max(newest, last_given)
                store.set_sync_state(self.conn, token,
                    newest_id=max(state["newest_id"], newest),
                    oldest_id=min(state["oldest_id"] or oldest, oldest), last_sync_ts=store.now(),
                    **({"history_complete": 0} if state["newest_id"] == 0 else {}))
            else:
                store.set_sync_state(self.conn, token, last_sync_ts=store.now())
            if common_read:
                self.conn.execute("UPDATE rooms SET last_common_read=? WHERE token=?", (common_read, token))

        if "talk-polls" in self.client.features:
            for msg in records:
                event = str(msg.get("systemMessage") or "")
                pid = polls.poll_id(msg.get("messageParameters"), event)
                if pid:
                    key = (token, pid)
                    self._pending_polls[key] = self._pending_polls.get(key, False) or bool(event)
            self._load_pending_polls()

    def _load_pending_polls(self) -> None:
        if "talk-polls" not in self.client.features:
            return
        for (token, pid), event in sorted(self._pending_polls.items(), key=lambda item: not item[1]):
            if self.stop_event.is_set() or self._poll_budget <= 0:
                break
            retry_key = "poll_retry:" + token + ":" + str(pid)
            if _int(store.meta_get(self.conn, retry_key)) > store.now():
                continue
            cached = self.conn.execute("SELECT updated_at FROM polls WHERE token=? AND poll_id=?", (token, pid)).fetchone()
            if cached and not event:
                self._pending_polls.pop((token, pid), None)
                continue
            self._poll_budget -= 1
            try:
                poll = self.client.poll_get(token, pid)
            except ApiError as exc:
                store.meta_set(self.conn, retry_key, store.now() + (300 if exc.status in (403, 404) else 60))
                if exc.status in (401, 429) or exc.status >= 500:
                    raise
                continue
            with transaction(self.conn):
                polls.upsert_poll(self.conn, token, poll)
                store.meta_set(self.conn, retry_key, 0)
            self._pending_polls.pop((token, pid), None)

    def _refresh_polls(self) -> None:
        if "talk-polls" not in self.client.features:
            return
        # Bounded recovery scan also finds polls left behind by crashes/restarts.
        rows = self.conn.execute("SELECT m.rowid,m.token,m.params FROM messages m JOIN rooms r ON r.token=m.token "
                                 "WHERE r.removed=0 AND m.is_deleted=0 AND m.message_type!='system' "
                                 "AND m.rowid>? AND m.params LIKE '%talk-poll%' ORDER BY m.rowid LIMIT 500",
                                 (self._poll_scan_cursor,)).fetchall()
        for row in rows:
            pid = polls.poll_id(polls.decode(row["params"], {}))
            if pid:
                self._pending_polls.setdefault((row["token"], pid), False)
        self._poll_scan_cursor = rows[-1]["rowid"] if len(rows) == 500 else 0
        # Non-removed rooms are the active set; oldest refresh first, bounded globally.
        for row in self.conn.execute("SELECT p.token,p.poll_id FROM polls p JOIN rooms r ON r.token=p.token "
                                     "WHERE r.removed=0 AND p.status=0 AND p.updated_at<=? ORDER BY p.updated_at,p.token,p.poll_id",
                                     (store.now() - 60,)):
            self._pending_polls[(row["token"], row["poll_id"])] = True
        self._load_pending_polls()

    def _poll_cursor(self, token: str, newest: int) -> int:
        row = self.conn.execute(
            "SELECT MAX(id) FROM messages WHERE token=? AND id<=? AND message_type!='system'",
            (token, newest)).fetchone()
        return int(row[0]) if row and row[0] else newest

    def _poll_interval(self, room: sqlite3.Row) -> float:
        age = store.now() - int(room["last_activity"] or 0)
        if age < 86400:
            return POLL_HOT_SECONDS
        return POLL_WARM_SECONDS if age < 7 * 86400 else POLL_COLD_SECONDS

    def _poll_due(self, room: sqlite3.Row) -> bool:
        return time.monotonic() >= self._next_poll.get(room["token"], 0.0)

    def _new_messages(self, room: sqlite3.Row, force: bool = False) -> None:
        token = room["token"]
        state = self._state(token)
        if state["newest_id"] == 0:
            self._next_poll[token] = time.monotonic() + self._poll_interval(room)
            records, given, common = self.client.chat_history(token)
            self._apply_page(token, records, given, common, history=True)
            if not records:
                store.set_sync_state(self.conn, token, history_complete=1)
            store.set_sync_state(self.conn, token, activity_seen=room["last_activity"])
            return
        # lastMessage keeps pointing at an edited/deleted/reacted message, so a changed
        # last_activity is the signal that system messages may be waiting.
        if (not force and room["last_message_id"] <= state["newest_id"]
                and room["last_activity"] <= state.get("activity_seen", 0)):
            return  # not polled now: leave the schedule alone, or short cycles would starve it
        self._next_poll[token] = time.monotonic() + self._poll_interval(room)
        for _ in range(100):  # bounded fairness; remaining pages continue next poll
            if self.stop_event.is_set():
                return
            cursor = self._state(token)["newest_id"]
            # Talk returns nothing newer than a *system* message (edit/reaction/delete events),
            # so poll from the last real comment and drop what is already mirrored.
            records, given, common = self.client.chat_history(
                token, self._poll_cursor(token, cursor), look_into_future=True)
            records = [m for m in records if _int(m.get("id")) > cursor]
            if not records:
                break
            self._apply_page(token, records, given, common, history=False)
            if self._state(token)["newest_id"] <= cursor:
                raise ApiError(0, "message cursor did not advance")
        else:
            return  # page budget exhausted: keep activity_seen so the next poll continues
        store.set_sync_state(self.conn, token, activity_seen=room["last_activity"])

    def _members(self, room: sqlite3.Row, *, active: bool = False) -> None:
        if self.stop_event.is_set():
            return
        token = room["token"]
        state = self._state(token)
        if not active and state["members_synced_at"] and store.now() - state["members_synced_at"] < MEMBERS_SECONDS:
            return
        try:
            members = self.client.participants(token)
        except ApiError as exc:
            if exc.status in (403, 412):
                store.set_sync_state(self.conn, token, members_synced_at=store.now())
                return
            raise
        # Readers may see a partial refresh; the timestamp advances only at completion.
        old = self.conn.execute("SELECT actor_type,actor_id FROM members WHERE token=?", (token,)).fetchall()
        present = {(m.get("actorType"), m.get("actorId")) for m in members}
        gone = [(token, r["actor_type"], r["actor_id"]) for r in old
                if (r["actor_type"], r["actor_id"]) not in present]
        for offset in range(0, len(members), 500):
            if self.stop_event.is_set():
                return
            with transaction(self.conn):
                for member in members[offset:offset + 500]:
                    store.upsert_member(self.conn, token, member)
        for offset in range(0, len(gone), 500):
            if self.stop_event.is_set():
                return
            with transaction(self.conn):
                self.conn.executemany("DELETE FROM members WHERE token=? AND actor_type=? AND actor_id=?",
                                      gone[offset:offset + 500])
        store.set_sync_state(self.conn, token, members_synced_at=store.now())

    def _avatar(self, room: sqlite3.Row) -> None:
        if self.stop_event.is_set():
            return
        token = room["token"]
        old = self.conn.execute("SELECT * FROM avatars WHERE token=?", (token,)).fetchone()
        if old and old["version"] == room["avatar_version"]:
            if old["path"] and not old["scanned"]:  # cached before emoji avatars were recognised
                try:
                    emoji, color = avatars.emoji_avatar(Path(old["path"]).read_bytes()[:4096])
                except OSError:
                    emoji, color = "", ""
                self.conn.execute("UPDATE avatars SET emoji=?,color=?,scanned=1 WHERE token=?", (emoji, color, token))
            return
        if old and not old["path"] and store.now() - old["checked_at"] < 3600:
            return
        try:
            data, _ = self.client.avatar(token)
        except ApiError as exc:
            if exc.status not in (403, 404):
                raise
            data = b""
        path = self.directory / "avatars" / (hashlib.sha256(token.encode()).hexdigest() + ".img")
        if data:
            _atomic_bytes(path, data)
        emoji, color = avatars.emoji_avatar(data) if data else ("", "")
        self.conn.execute("INSERT INTO avatars(token,version,path,checked_at,emoji,color,scanned) VALUES (?,?,?,?,?,?,1) "
                          "ON CONFLICT(token) DO UPDATE SET version=excluded.version,path=excluded.path,"
                          "checked_at=excluded.checked_at,emoji=excluded.emoji,color=excluded.color,scanned=1",
                          (token, room["avatar_version"], str(path) if data else "", store.now(), emoji, color))

    def _room_gone(self, token: str) -> None:
        self.conn.execute("UPDATE rooms SET removed=1 WHERE token=?", (token,))

    def _error(self, exc: ApiError) -> None:
        category = "auth" if exc.status == 401 else (f"http-{exc.status}" if exc.status else "network")
        store.meta_set(self.conn, "last_error", category)
        if exc.status == 401:
            self.auth_failed = True
            self.stop_event.set()

    def run_once(self, *, backfill_budget: float = 5.0) -> dict[str, int]:
        if self.auth_failed:
            raise ApiError(401, "authentication failed")
        if self.stop_event.is_set():
            return self.counts()
        self._poll_budget = 4
        try:
            self.initialize()
            if self.stop_event.is_set():
                return self.counts()
            full = not self._last_full or time.monotonic() - self._last_full >= FULL_REFRESH_SECONDS
            since = store.meta_get(self.conn, "rooms_modified_before")
            rooms, modified = self.client.rooms(None if full or not since else since)
            with transaction(self.conn):
                for room in rooms:
                    if isinstance(room, dict) and room.get("token"):
                        store.upsert_room(self.conn, room)
                if full:
                    store.mark_missing_rooms_removed(self.conn, [r["token"] for r in rooms if isinstance(r, dict) and r.get("token")])
                if modified:
                    store.meta_set(self.conn, "rooms_modified_before", modified)
                store.meta_set(self.conn, "last_rooms_poll", store.now())
            if full:
                self._last_full = time.monotonic()
            active = self.conn.execute("SELECT * FROM rooms WHERE removed=0 ORDER BY last_activity DESC, token").fetchall()
            budget = POLLS_PER_CYCLE
            for room in active:
                if self.stop_event.is_set():
                    break
                try:
                    active_room = room["last_message_id"] > self._state(room["token"])["newest_id"]
                    due = budget > 0 and self._poll_due(room)
                    if due:
                        budget -= 1
                    self._new_messages(room, force=due)
                    self._members(room, active=active_room)
                    self._avatar(room)
                except ApiError as exc:
                    if exc.status == 404:
                        self._room_gone(room["token"])
                        continue
                    raise
            if not self.stop_event.is_set():
                self._refresh_polls()
                self.process_outbox()
                self._auto_media()
                end = time.monotonic() + min(5.0, max(0.0, backfill_budget))
                while not self.stop_event.is_set() and time.monotonic() < end:
                    if max(time.monotonic(), self._last_backfill + BACKFILL_SECONDS) >= end:
                        break
                    if not self.backfill_step():
                        break
            if not self.auth_failed:
                store.meta_set(self.conn, "last_sync_ok_ts", store.now())
                store.meta_set(self.conn, "last_error", "")
            return self.counts()
        except ApiError as exc:
            self._error(exc)
            raise

    def backfill_step(self) -> bool:
        rooms = self.conn.execute("SELECT r.token FROM rooms r JOIN sync_state s ON s.token=r.token "
            "WHERE r.removed=0 AND s.history_complete=0 ORDER BY r.last_activity DESC, r.token").fetchall()
        if not rooms or self.stop_event.is_set():
            return False
        # Round robin keeps large recent rooms from starving older rooms.
        token = rooms[self._backfill_rotation % len(rooms)]["token"]
        self._backfill_rotation += 1
        delay = max(0.0, self._last_backfill + BACKFILL_SECONDS - time.monotonic())
        if self.stop_event.wait(delay):
            return False
        state = self._state(token)
        self._last_backfill = time.monotonic()
        try:
            records, given, common = self.client.chat_history(token, state["oldest_id"] or None)
            if not records:
                store.set_sync_state(self.conn, token, history_complete=1, last_sync_ts=store.now())
                return True
            self._apply_page(token, records, given, common, history=True)
            if state["oldest_id"] and self._state(token)["oldest_id"] >= state["oldest_id"]:
                raise ApiError(0, "history cursor did not advance")
            return True
        except ApiError as exc:
            if exc.status == 404:
                self._room_gone(token)
                return True
            self._error(exc)
            raise

    def _auto_media(self) -> None:
        try:
            raw = (state_dir() / "preferences.json").read_text(encoding="utf-8")
            prefs = json.loads(raw) if len(raw) <= 4 * 1024 * 1024 else {}
        except (OSError, ValueError):
            return
        if not isinstance(prefs, dict) or prefs.get("auto_download_media") is not True:
            return
        rows = self.conn.execute("SELECT m.* FROM messages m JOIN rooms r ON r.token=m.token "
            "WHERE r.removed=0 AND m.rowid>? AND m.media_type IN ('image','gif') AND m.local_path='' "
            "AND m.is_deleted=0 AND m.file_size<=? ORDER BY m.rowid LIMIT 4",
            (self._media_cursor, 20 * 1024 * 1024)).fetchall()
        for row in rows:
            if self.stop_event.is_set():
                break
            self._media_cursor = row["rowid"]
            checked_key = "media_checked:" + str(row["rowid"])
            if store.now() - _int(store.meta_get(self.conn, checked_key)) < 3600:
                continue
            store.meta_set(self.conn, checked_key, store.now())
            remote = row["file_path"] or row["file_id"]
            if not remote:
                continue
            token_dir = row["token"] if re.fullmatch(r"[a-zA-Z0-9_-]+", row["token"]) else hashlib.sha256(row["token"].encode()).hexdigest()
            target = self.directory / "media" / token_dir / str(row["id"])
            try:
                ensure_directory(target.parent)
                check_file(target)
                if not target.resolve().is_relative_to(self.directory.resolve()):
                    continue
            except (OSError, ValueError):
                continue
            previous = self.client.max_file_bytes
            self.client.max_file_bytes = min(previous, 20 * 1024 * 1024)
            try:
                self.client.download_file(remote, target)
            except ApiError as exc:
                if exc.status == 401 or exc.status == 429 or exc.status >= 500:
                    raise
                continue
            finally:
                self.client.max_file_bytes = previous
            self.conn.execute("UPDATE messages SET local_path=? WHERE rowid=?", (str(target), row["rowid"]))
        if not rows:
            self._media_cursor = 0

    def _outbox_action(self, row: sqlite3.Row, payload: dict[str, Any]) -> Any:
        token, kind = row["token"], row["kind"]
        mid = payload.get("id", payload.get("message_id"))
        if kind in ("send", "send_message"):
            reference = payload.get("reference_id") or payload.get("referenceId") or payload.get("local_id") or hashlib.sha256(row["local_id"].encode()).hexdigest()
            existing = self.conn.execute("SELECT id FROM messages WHERE token=? AND reference_id=?", (token, reference)).fetchone()
            if existing:
                return {"id": existing[0]}
            return self.client.send_message(token, str(payload.get("text", "")),
                reply_to=payload.get("reply_to", payload.get("reply_id")) or None, reference_id=reference,
                silent=bool(payload.get("silent", False)))
        if kind == "edit":
            return self.client.edit_message(token, mid, str(payload.get("text", "")))
        if kind == "delete":
            return self.client.delete_message(token, mid)
        if kind in ("react", "reaction"):
            emoji = str(payload.get("emoji", ""))
            if payload.get("remove"):
                return self.client.remove_reaction(token, mid, emoji)
            if not emoji:
                self.client._guard(token)
                removed = payload.get("remove_emojis", [])
                if not isinstance(removed, list) or any(not isinstance(e, str) for e in removed):
                    raise ValueError("invalid reaction action")
                result: Any = {}
                for previous in removed:
                    result = self.client.remove_reaction(token, mid, previous)
                return result
            return self.client.add_reaction(token, mid, emoji)
        if kind in ("file", "files", "voice", "upload"):
            return self.client.upload_and_share(token, payload["path"], caption=payload.get("caption"),
                                                voice=kind == "voice" or bool(payload.get("voice")))
        # The daemon deliberately does NOT support queued read-marker operations.
        raise ValueError("unsupported outbox action")

    def process_outbox(self) -> None:
        self._recover_outbox()
        rows = self.conn.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY id LIMIT 50").fetchall()
        blocked: set[str] = set()
        for row in rows:
            if self.stop_event.is_set():
                break
            if row["token"] in blocked:
                continue
            claimed = self.conn.execute("UPDATE outbox SET state='sending',attempts=attempts+1,updated_at=? "
                                        "WHERE id=? AND state='pending'", (store.now(), row["id"]))
            if not claimed.rowcount:
                continue
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    raise ValueError
                response = self._outbox_action(row, payload)
            except GuardError:
                self.conn.execute("UPDATE outbox SET state='failed',error='guard',updated_at=? WHERE id=?", (store.now(), row["id"]))
            except ApiError as exc:
                # Network/5xx can be ambiguous after POST: fail rather than duplicate.
                retry = exc.status in (401, 429) or (not exc.request_sent and
                         (not exc.status or exc.status >= 500))
                ambiguous = exc.request_sent and (not exc.status or exc.status >= 500)
                error = ("auth" if exc.status == 401 else "delivery uncertain; verify before retry" if ambiguous
                         else f"http-{exc.status}")
                self.conn.execute("UPDATE outbox SET state=?,error=?,updated_at=? WHERE id=?",
                    ("pending" if retry else "failed", error, store.now(), row["id"]))
                blocked.add(row["token"])
                if retry or exc.status >= 500 or not exc.status:
                    raise
            except (ValueError, KeyError, TypeError, OSError):
                self.conn.execute("UPDATE outbox SET state='failed',error='invalid action',updated_at=? WHERE id=?", (store.now(), row["id"]))
            else:
                result = {"ok": True, "kind": row["kind"], "local_id": row["local_id"], "queued": False}
                if row["kind"] in ("send", "send_message"):
                    result["message_id"] = str(response.get("id", "")) if isinstance(response, dict) else ""
                payload["_result"] = result
                self.conn.execute("UPDATE outbox SET state='done',payload=?,error='',updated_at=? WHERE id=?",
                                  (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                                   store.now(), row["id"]))

    def counts(self) -> dict[str, int]:
        return {table: self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("rooms", "messages", "members", "reactions")}

    def run(self, *, verbose: bool = False) -> int:
        poll_failures = backfill_failures = 0
        next_poll = next_backfill = 0.0
        while not self.stop_event.is_set():
            if time.monotonic() >= next_poll:
                try:
                    self.run_once(backfill_budget=0)
                except ApiError as exc:
                    self._error(exc)
                    if self.auth_failed:
                        return 3
                    poll_failures += 1
                    next_poll = time.monotonic() + retry_delay(exc, poll_failures)
                else:
                    poll_failures = 0
                    next_poll = time.monotonic() + POLL_SECONDS
                    if verbose:
                        print(json.dumps(self.counts()), flush=True)
            # Foreground failures suspend all networking; history failures do not
            # reset the foreground schedule or their own exponential backoff.
            if not poll_failures and not self.stop_event.is_set() and time.monotonic() >= next_backfill:
                try:
                    progressed = self.backfill_step()
                except ApiError as exc:
                    self._error(exc)
                    if self.auth_failed:
                        return 3
                    backfill_failures += 1
                    next_backfill = time.monotonic() + retry_delay(exc, backfill_failures)
                    # Retry-After can be server-wide, unlike our own history backoff.
                    if exc.retry_after:
                        next_poll = max(next_poll, time.monotonic() + min(900.0, exc.retry_after))
                else:
                    backfill_failures = 0
                    next_backfill = time.monotonic() + BACKFILL_SECONDS if progressed else next_poll
            wake = next_poll if poll_failures else min(next_poll, next_backfill)
            self.stop_event.wait(max(0.0, wake - time.monotonic()))
        return 3 if self.auth_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize one Talk account into its offline mirror")
    parser.add_argument("account_id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    lock_fd: int | None = None
    try:
        config = load_account(args.account_id)
        directory = store_dir(args.account_id)
        ensure_directory(directory)
        lock_fd = os.open(directory / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("sync already running", file=sys.stderr)
            return 0
        stop = threading.Event()
        def terminate(signum: int, frame: Any) -> None:
            stop.set()
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        client = TalkClient(config["server"], config["user"], config["app_password"], config.get("write_guard_prefix"))
        with SyncDaemon(args.account_id, client, directory, stop_event=stop) as daemon:
            if args.once:
                counts = daemon.run_once()
                if args.verbose:
                    print(json.dumps(counts), flush=True)
                return 0
            return daemon.run(verbose=args.verbose)
    except ApiError as exc:
        print("sync: authentication failed" if exc.status == 401 else "sync: network/API failure", file=sys.stderr)
        return 3 if exc.status == 401 else 1
    except (ValueError, OSError, sqlite3.Error):
        print("sync: configuration/storage failure", file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
