"""Stage-B mutations; only local helper tables and confirmed polls are written.

``run(subcommand, argv, payload)`` returns JSON-ready success/error dicts. Internal
validation raises ActionError(message, supported=True); run sanitizes unexpected
local failures and never includes remote bodies, credentials or message text.
GuardError is always an error, never an offline success.

Outbox contract: kind=send|react|edit|delete, payload is the validated UI object
(jid,text/reply_id/id/emoji/local_id); send.text is already mention-normalized.
``replay(client, kind, payload)`` executes it without re-queueing. referenceId is
local_id. An empty reaction is frozen as ``remove_emojis`` when queued. Entries
with the same local_id must have identical kind/token/payload or are refused.
Only network failures before a write request queue, not ambiguous delivery,
auth/guard/protocol errors. Successful online results are persisted too.
Offline dev-guard authorization additionally needs a matching mirrored room.

Attachments use immutable, private snapshots. A small TalkClient extension adds
caption/replyTo/referenceId to the guarded share request, since the base API's
upload convenience method doesn't expose those fields. No uploads are queued.
Albums are returned to the composer; durable album storage awaits local_media.
"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterator
from contextlib import contextmanager, closing
from urllib.parse import unquote, urlsplit
import uuid

from . import api, polls, prefs, store
from .paths import check_file

MAX_FILE = 100 * 1024 * 1024
MAX_ATTACHMENTS = 10
SHARES = "/ocs/v2.php/apps/files_sharing/api/v1/shares"
UNSUPPORTED = set("select sticker fetch-stickers stickers contact-alias contact-tag".split())


class ActionError(Exception):
    def __init__(self, message: str, supported: bool = True) -> None:
        self.supported = supported
        super().__init__(message)


class ShareClient(api.TalkClient):
    def _write(self, token: str, path: str, method: str = "POST", data: Any = None) -> Any:
        if path == SHARES and isinstance(data, dict):
            data = dict(data, **getattr(self, "_share_options", {}))
        return super()._write(token, path, method, data)

    def upload(self, token: str, path: Path, caption: str, reply: int | None,
               reference: str, voice: bool = False) -> Any:
        self._share_options = {"caption": caption, "replyTo": reply, "referenceId": reference}
        try:
            return self.upload_and_share(token, path, caption=caption, voice=voice)
        finally:
            self._share_options = {}


def string(payload: dict[str, Any], key: str, default: str = "", maximum: int = 4096) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ActionError("invalid request field")
    return value


def token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ActionError("invalid room token")
    return value


def message_id(value: Any, *, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,19}", str(value)):
        raise ActionError("invalid message id")
    number = int(value)
    if not 0 < number < 2 ** 63:
        raise ActionError("invalid message id")
    return number


def local_id(payload: dict[str, Any]) -> str:
    value = string(payload, "local_id", maximum=256) or uuid.uuid4().hex
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,256}", value):
        raise ActionError("invalid local id")
    return value


def offline(exc: api.ApiError) -> bool:
    return not isinstance(exc, api.GuardError) and (exc.status in (502, 503, 504) or
           (exc.status == 0 and exc.ocs_message == "network request failed"))


def rw(db: Path) -> sqlite3.Connection:
    # Never create/migrate a daemon mirror from the helper.
    for suffix in ("", "-wal", "-shm"):
        check_file(str(db) + suffix)
    conn = sqlite3.connect(db.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.75)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=750")
    return conn


NOTE_TO_SELF_ROOM_TYPE = 6  # Talk room type of the personal notes conversation


def rows(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if not db.exists():
        return []
    with closing(store.connect_ro(db)) as conn:
        return conn.execute(sql, params).fetchall()


def cached_message(db: Path, jid: str, mid: int) -> sqlite3.Row:
    result = rows(db, "SELECT * FROM messages WHERE token=? AND id=?", (jid, mid))
    if not result:
        raise ActionError("message not available in local mirror")
    return result[0]


def mention_text(db: Path, jid: str, text: str, mentions: Any) -> str:
    if not isinstance(mentions, list) or len(mentions) > 100 or any(not isinstance(m, str) for m in mentions):
        raise ActionError("invalid mentions")
    names: dict[str, str] = {}
    for mention in dict.fromkeys(mentions):
        if not mention.startswith("users/") or not mention[6:] or len(mention) > 512:
            raise ActionError("invalid mention identity")
        uid = mention[6:]
        found = rows(db, "SELECT display_name FROM members WHERE token=? AND actor_type='users' "
                     "AND actor_id=? UNION ALL SELECT display_name FROM users WHERE actor_type='users' AND actor_id=?",
                     (jid, uid, uid))
        if not found or not found[0][0]:
            raise ActionError("mention not available in local directory")
        name = str(found[0][0])
        syntax = "@" + uid if re.fullmatch(r"[A-Za-z0-9_.-]+", uid) else '@"' + uid.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if name in names and names[name] != syntax:
            raise ActionError("ambiguous mention display name")
        names[name] = syntax
    if not names:
        return text
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n)))
    return re.sub(r"(?<![\w@])@(" + alternatives + r")(?=$|[^\w])",
                  lambda m: names[m.group(1)], text)


def replay(client: api.TalkClient, kind: str, payload: dict[str, Any]) -> Any:
    """Sync entry point. Payload must be the validated, persisted outbox object."""
    jid = token(payload.get("jid"))
    if kind == "send":
        return client.send_message(jid, string(payload, "text", maximum=32000),
                                   message_id(payload.get("reply_id"), optional=True),
                                   string(payload, "local_id"))
    mid = message_id(payload.get("id"))
    if kind == "edit":
        return client.edit_message(jid, mid, string(payload, "text", maximum=32000))
    if kind == "delete":
        return client.delete_message(jid, mid)
    if kind == "react":
        emoji = string(payload, "emoji", maximum=64)
        if emoji:
            return client.add_reaction(jid, mid, emoji)
        client._guard(jid)  # Even an empty/no-op removal must honor the dev guard.
        result: Any = {}
        for previous in payload.get("remove_emojis", []):
            result = client.remove_reaction(jid, mid, previous)
        return result
    raise ActionError("not supported", supported=False)


def _encoded(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in ("_result", "_owner_pid") and
            not (k == "remove_emojis" and payload.get("emoji") == "")}


def _reserve(db: Path, kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    now = int(time.time())
    payload["_owner_pid"] = os.getpid()
    with closing(rw(db)) as conn, conn:
        # The UNIQUE constraint, not a separate SELECT, arbitrates helper races.
        inserted = conn.execute("INSERT OR IGNORE INTO outbox(local_id,token,kind,payload,state,created_at,updated_at) "
                                "VALUES (?,?,?,?,'sending',?,?)",
                                (payload["local_id"], payload["jid"], kind, _encoded(payload), now, now)).rowcount
        if inserted:
            return None
        existing = conn.execute("SELECT * FROM outbox WHERE local_id=?", (payload["local_id"],)).fetchone()
        saved = json.loads(existing["payload"])
        if (existing["kind"], existing["token"], _identity(saved)) != (kind, payload["jid"], _identity(payload)):
            raise ActionError("local id already used for another action")
        if existing["state"] == "sending":
            raise ActionError("action already in progress; do not resend")
        if existing["state"] == "failed":
            raise ActionError("action failed; verify delivery before manual retry")
        if existing["state"] == "done":
            return saved.get("_result", {"ok": True, "kind": kind, "queued": False, "local_id": payload["local_id"]})
        return {"ok": True, "kind": kind, "queued": True, "local_id": payload["local_id"]}


def _finish(db: Path, payload: dict[str, Any], state: str, error: str = "") -> None:
    payload.pop("_owner_pid", None)
    with closing(rw(db)) as conn, conn:
        conn.execute("UPDATE outbox SET state=?,payload=?,error=?,updated_at=? WHERE local_id=? AND state='sending'",
                     (state, _encoded(payload), error, int(time.time()), payload["local_id"]))


def _queue(db: Path, client: api.TalkClient, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if client.write_guard_prefix is not None:
        found = rows(db, "SELECT name FROM rooms WHERE token=? AND removed=0", (payload["jid"],))
        if not found or not found[0][0].startswith(client.write_guard_prefix):
            raise api.GuardError()
    _finish(db, payload, "pending")
    return {"ok": True, "kind": kind, "queued": True, "local_id": payload["local_id"]}


def mutate(db: Path, client: api.TalkClient, kind: str, request: dict[str, Any]) -> dict[str, Any]:
    jid = token(request.get("jid"))
    payload: dict[str, Any] = {"jid": jid, "local_id": local_id(request)}
    if kind == "send":
        text = string(request, "text", maximum=32000)
        if not text.strip():
            raise ActionError("empty message")
        payload.update(text=mention_text(db, jid, text, request.get("mentions", [])),
                       reply_id=str(message_id(request.get("reply_id"), optional=True) or ""),
                       reference_id=payload["local_id"])
        if len(payload["text"]) > 32000:
            raise ActionError("message too long")
    else:
        payload["id"] = str(message_id(request.get("id")))
        if kind == "edit":
            payload["text"] = string(request, "text", maximum=32000)
            if not payload["text"].strip():
                raise ActionError("empty message")
        elif kind == "react":
            payload["emoji"] = string(request, "emoji", maximum=64)
            if not payload["emoji"]:
                # Determine our previous reactions before attempting the write.
                previous = rows(db, "SELECT reactions_self FROM messages WHERE token=? AND id=?", (jid, int(payload["id"])))
                payload["remove_emojis"] = json.loads(previous[0][0]) if previous else []
    existing = _reserve(db, kind, payload)
    if existing is not None:
        return existing
    try:
        if kind == "react" and not payload["emoji"]:
            try:
                details = client.reaction_details(jid, payload["id"])
                payload["remove_emojis"] = [e for e, actors in details.items() if any(
                    a.get("actorType") == "users" and a.get("actorId") == client.user for a in actors)]
            except api.ApiError as exc:
                if not offline(exc):
                    raise
                if not previous:
                    raise ActionError("own reactions unavailable offline")
                return _queue(db, client, kind, payload)
        response = replay(client, kind, payload)
    except api.ApiError as exc:
        if offline(exc) and not exc.request_sent:
            try:
                return _queue(db, client, kind, payload)
            except api.GuardError:
                _finish(db, payload, "failed", "guard")
                raise
        ambiguous = exc.request_sent and (not exc.status or exc.status >= 500)
        _finish(db, payload, "failed", "verify delivery before retry" if ambiguous else exc.ocs_message)
        if ambiguous:
            raise ActionError("delivery uncertain; verify before manual retry") from None
        raise
    except (ActionError, OSError, ValueError, TypeError, KeyError):
        _finish(db, payload, "failed", "local operation failed; verify before retry")
        raise
    result = {"ok": True, "kind": kind, "local_id": payload["local_id"], "queued": False}
    if kind == "send":
        result["message_id"] = str(response.get("id", "")) if isinstance(response, dict) else ""
    payload["_result"] = result
    _finish(db, payload, "done")
    return result


def _no_links(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ActionError("unsafe linked path")


def local_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ActionError("invalid local path")
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ActionError("nonlocal file URL")
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ActionError("absolute safe path required")
    _no_links(path)
    return path


def validate_file(value: Any, *, voice: bool = False) -> Path:
    path = local_path(value)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE:
        raise ActionError("unsafe or oversized file")
    if voice:
        root = prefs.state_dir() / "voice-drafts"
        if path.parent != root or not re.fullmatch(r"(?:omanctalk-[A-Za-z0-9_-]+|voice-[0-9a-f]{32})\.ogg", path.name):
            raise ActionError("not a private voice draft")
        if root.stat().st_uid != os.getuid() or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_size == 0:
            raise ActionError("unsafe or empty voice draft")
    return path


def _open_regular(path: Path) -> int:
    """Traverse every directory without following links, including during races."""
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                       dir_fd=directory)
    finally:
        os.close(directory)


@contextmanager
def snapshot(path: Path, *, voice: bool = False) -> Iterator[Path]:
    # Keep a bounded immutable source while urllib retries a PUT.
    with tempfile.TemporaryDirectory(prefix="omanctalk-upload-") as directory:
        target = Path(directory) / path.name
        fd = _open_regular(path)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE:
                raise ActionError("unsafe or oversized file")
            if voice:
                if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_size == 0:
                    raise ActionError("unsafe or empty voice draft")
                os.fchmod(source.fileno(), 0o600)
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                count = 0
                while chunk := source.read(65536):
                    count += len(chunk)
                    if count > MAX_FILE:
                        raise ActionError("file too large")
                    output.write(chunk)
        yield target


def _error(exc: Exception) -> str:
    if isinstance(exc, api.ApiError):
        return exc.ocs_message
    if isinstance(exc, ActionError):
        return str(exc)
    return "local operation failed"


def _shared_message_id(client: api.TalkClient, jid: str, response: Any, reference: str) -> str:
    if not isinstance(response, dict):
        return ""
    if response.get("messageId"):
        return str(response["messageId"])
    # Share responses can contain only the share/file ID, never a chat ID.
    # One bounded read can resolve referenceId; a read failure must not turn a
    # confirmed delivery into a retryable send failure.
    try:
        history, _, _ = client.chat_history(jid, limit=50)
        fid = response.get("file_source", response.get("fileSource"))
        for msg in history:
            if msg.get("referenceId") == reference:
                return str(msg["id"])
            params = msg.get("messageParameters")
            file = params.get("file") if isinstance(params, dict) else None
            if fid and isinstance(file, dict) and str(file.get("id")) == str(fid) and msg.get("actorId") == client.user:
                return str(msg["id"])
    except (api.ApiError, ValueError, KeyError, TypeError):
        pass
    return ""


def files(client: ShareClient, payload: dict[str, Any]) -> dict[str, Any]:
    jid = token(payload.get("jid"))
    values = payload.get("paths")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ATTACHMENTS:
        raise ActionError("choose between one and ten files")
    paths = [validate_file(value) for value in values]
    caption = string(payload, "caption", maximum=32000)
    reply = message_id(payload.get("reply_id"), optional=True)
    reference = local_id(payload)
    mimes = [mimetypes.guess_type(p.name)[0] or "application/octet-stream" for p in paths]
    album = uuid.uuid4().hex if len(paths) > 1 and all(m.startswith(("image/", "video/")) for m in mimes) else ""
    items: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        try:
            with snapshot(path) as source:
                response = client.upload(jid, source, caption if index == 0 else "", reply if index == 0 else None,
                                         reference + ":" + str(index))
            mid = _shared_message_id(client, jid, response, reference + ":" + str(index))
            items.append({"ok": True, "kind": "file", "id": mid, "local_path": str(path),
                          "mime_type": mimes[index], "media_type": store.media_type_for(mimes[index], path.name),
                          "filename": path.name, "album_id": album, "album_index": index, "album_count": len(paths)})
        except (api.ApiError, ActionError, OSError, ValueError) as exc:
            result: dict[str, Any] = {"ok": False, "kind": "files", "error": _error(exc)}
            if items:
                result["partial"] = {"kind": "files", "sent_count": len(items), "sent_paths": values[:index],
                                     "remaining_paths": values[index:], "items": items, "album_id": album}
            return result
    return {"ok": True, "kind": "files", "count": len(items), "album_id": album, "items": items}


def voice(client: ShareClient, payload: dict[str, Any]) -> dict[str, Any]:
    jid = token(payload.get("jid"))
    path = validate_file(payload.get("path"), voice=True)
    metadata = path.stat()
    reply = message_id(payload.get("reply_id"), optional=True)
    with snapshot(path, voice=True) as source:
        with source.open("rb") as stream:
            header = stream.read(65536)
        if not header.startswith(b"OggS") or b"OpusHead" not in header:
            audio_container = (header.startswith(b"OggS") or header.startswith(b"\x1a\x45\xdf\xa3") or
                               (header.startswith(b"RIFF") and header[8:12] == b"WAVE") or header[4:8] == b"ftyp")
            if not audio_container:
                raise ActionError("unrecognized voice recording format")
            executable = shutil.which("ffmpeg")
            if not executable:
                raise ActionError("recording must be OGG/Opus (ffmpeg unavailable)")
            converted = source.with_name("converted.ogg")
            result = subprocess.run([executable, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                                     "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libopus", "-fs", str(MAX_FILE + 1),
                                     "-f", "ogg", str(converted)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
            if result.returncode or not converted.exists() or converted.stat().st_size > MAX_FILE:
                raise ActionError("voice conversion failed")
            with converted.open("rb") as stream:
                header = stream.read(65536)
            if not header.startswith(b"OggS") or b"OpusHead" not in header:
                raise ActionError("recording must be OGG/Opus")
            source = converted
        size = source.stat().st_size
        client.upload(jid, source, "", reply, local_id(payload), voice=True)
    cleaned = False
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) == (
                metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size):
            path.unlink()
            cleaned = True
    except OSError:
        pass
    return {"ok": True, "kind": "voice", "size": size, "draft_cleaned": cleaned}


def media(db: Path, client: api.TalkClient, payload: dict[str, Any]) -> dict[str, Any]:
    jid = token(payload.get("jid"))
    mid = message_id(payload.get("id"))
    msg = cached_message(db, jid, mid)
    if not msg["file_id"] and not msg["file_path"]:
        raise ActionError("message has no downloadable file")
    if msg["file_size"] > MAX_FILE:
        raise ActionError("file too large")
    root = db.parent
    directory = root / "media" / jid
    _no_links(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = re.sub(r"[^\w. -]", "_", msg["file_name"] or "file")[:180].strip(". ") or "file"
    destination = directory / (str(mid) + "_" + name)
    _no_links(destination)
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ActionError("unsafe download destination")
    if not destination.exists():
        client.max_file_bytes = MAX_FILE
        client.download_file(msg["file_id"] or msg["file_path"], destination)
    if not destination.is_file() or destination.stat().st_size > MAX_FILE:
        raise ActionError("unsafe or oversized cached file")
    with closing(rw(db)) as conn, conn:
        conn.execute("UPDATE messages SET local_path=? WHERE token=? AND id=?", (str(destination), jid, mid))
    return {"ok": True, "kind": "media", "local_path": str(destination)}


def user_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("users/") or not value[6:] or len(value) > 512 or "\x00" in value:
        raise ActionError("Talk user reference required")
    return value[6:]


def group_info(client: api.TalkClient, jid: str) -> dict[str, Any]:
    room = client.room(jid)
    members = client.participants(jid)
    role = next((m.get("participantType") for m in members if m.get("actorType") == "users" and m.get("actorId") == client.user), 3)
    return {"ok": True, "kind": "group-info", "jid": jid, "name": room.get("displayName", room.get("name", "")),
            "description": room.get("description", ""), "announce_only": bool(room.get("readOnly")),
            "locked": False, "approval_required": False, "participant_count": len(members),
            "my_role": {1: "owner", 2: "moderator", 4: "guest", 6: "moderator"}.get(role, "user")}


def contacts(db: Path, client: api.TalkClient, payload: dict[str, Any]) -> dict[str, Any]:
    query = string(payload, "query", maximum=256)
    try:
        people = [(str(p["id"]), str(p.get("label", p["id"]))) for p in client.autocomplete_users(query)
                  if isinstance(p, dict) and p.get("source", "users") == "users" and p.get("id")]
    except api.ApiError as exc:
        if not offline(exc):
            raise
        people = [(r[0], r[1]) for r in rows(db, "SELECT actor_id,display_name FROM users WHERE actor_type='users' "
                  "UNION SELECT actor_id,display_name FROM members WHERE actor_type='users'") if query.casefold() in r[1].casefold() or query.casefold() in r[0].casefold()]
    existing = {r[0] for r in rows(db, "SELECT object_id FROM rooms WHERE type=1 AND removed=0 UNION SELECT m.actor_id FROM members m "
                "JOIN rooms r ON r.token=m.token WHERE r.type=1 AND r.removed=0 AND m.actor_type='users'")}
    unique = dict(people)
    result = [{"jid": "users/" + uid, "name": name, "phone": "", "has_chat": uid in existing, "alias": "", "tags": []}
              for uid, name in sorted(unique.items(), key=lambda p: (p[1].casefold(), p[0]))][:50]
    return {"ok": True, "kind": "contacts-search", "query": query, "people": result}


def _once(command: str, db: Path, client: ShareClient, payload: dict[str, Any]) -> dict[str, Any]:
    reference = local_id(payload)
    payload = dict(payload, local_id=reference)
    # Room creation is reserved separately from the send, which can be queued.
    fields = {"send-new": ("target", "text", "reply_id", "mentions"),
              "files": ("paths", "caption", "reply_id"), "voice": ("path", "reply_id"),
              "forward": ("id", "to_jid")}[command]
    reservation = {k: payload[k] for k in fields if k in payload}
    reservation.update(jid=str(payload.get("jid", "")), local_id=reference)
    if command == "send-new":
        reservation["local_id"] = reference + ":room"
    existing = _reserve(db, command, reservation)
    if existing is not None:
        return existing
    try:
        result = _run(command, db, client, payload, reserved=True)
    except (api.ApiError, ActionError, OSError, ValueError, TypeError, KeyError):
        _finish(db, reservation, "failed", "verify before manual retry")
        raise
    reservation["_result"] = result
    _finish(db, reservation, "done" if result.get("ok") else "failed", "" if result.get("ok") else "verify before manual retry")
    return result


def poll_action(command: str, db: Path, client: api.TalkClient, payload: dict[str, Any]) -> dict[str, Any]:
    jid = token(payload.get("jid"))
    if "talk-polls" not in client.features:
        raise ActionError("polls not supported", supported=False)
    if not rows(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='polls'"):
        raise ActionError("poll mirror unavailable; synchronize first")
    completed = False
    try:
        if command == "poll":
            question = string(payload, "question", maximum=1000)
            options = payload.get("options")
            if not question.strip() or not isinstance(options, list) or not 2 <= len(options) <= 20:
                raise ActionError("invalid poll question or options")
            if any(not isinstance(o, str) or not o.strip() or len(o) > 1000 or "\x00" in o for o in options):
                raise ActionError("invalid poll option")
            if len(set(options)) != len(options):
                raise ActionError("ambiguous poll option texts")
            multi, hidden = payload.get("multi", 1), payload.get("hidden", False)
            if type(multi) is not int or not 1 <= multi <= len(options) or type(hidden) is not bool:
                raise ActionError("invalid poll selection limit")
            if any(r["type"] == NOTE_TO_SELF_ROOM_TYPE for r in rows(db, "SELECT type FROM rooms WHERE token=?", (jid,))):
                # The server refuses polls here ({"error":"room"}); say so instead of "OCS request failed".
                raise ActionError("Polls cannot be created in this conversation.")
            response = client.poll_create(jid, question, options, 0 if multi == len(options) else multi, int(hidden))
            completed = True
            pid = polls.integer(response.get("id")) if isinstance(response, dict) else 0
            if not 0 < pid < 2 ** 63:
                raise ActionError("poll created; invalid response; do not retry")
            mid = str(response.get("messageId") or "") if isinstance(response, dict) else ""
            if not mid:
                history, _, _ = client.chat_history(jid, limit=200)
                match = next((m for m in history if polls.poll_id(m.get("messageParameters")) == pid), None)
                mid = str(match["id"]) if match else ""
            refreshed = client.poll_get(jid, pid)
            with closing(rw(db)) as conn, conn:
                polls.upsert_poll(conn, jid, refreshed)
            return {"ok": True, "kind": command, "message_id": mid}
        msg = cached_message(db, jid, message_id(payload.get("id")))
        pid = polls.poll_id(polls.decode(msg["params"], {}))
        if not pid or msg["is_deleted"]:
            raise ActionError("message has no poll")
        current = client.poll_get(jid, pid)
        if command == "poll-vote":
            chosen = payload.get("options")
            if not isinstance(chosen, list) or any(not isinstance(o, str) for o in chosen):
                raise ActionError("invalid poll options")
            options = current.get("options")
            if not isinstance(options, list) or any(not isinstance(o, str) for o in options):
                raise ActionError("poll options unavailable")
            if len(set(options)) != len(options):
                raise ActionError("ambiguous poll option texts")
            if len(set(chosen)) != len(chosen) or any(o not in options for o in chosen):
                raise ActionError("unknown or duplicate poll option")
            maximum = polls.integer(current.get("maxVotes"))
            if maximum and len(chosen) > maximum:
                raise ActionError("poll selection limit exceeded")
            if polls.integer(current.get("status")):
                raise ActionError("poll closed")
            client.poll_vote(jid, pid, [options.index(o) for o in chosen])
            result = {"ok": True, "kind": command, "options": chosen}
        else:
            if polls.integer(current.get("status")):
                client._guard(jid)  # Closing twice is a guarded no-op.
            else:
                client.poll_close(jid, pid)
            result = {"ok": True, "kind": command}
        completed = True
        refreshed = client.poll_get(jid, pid)
        with closing(rw(db)) as conn, conn:
            polls.upsert_poll(conn, jid, refreshed)
        return result
    except api.ApiError as exc:
        if completed:
            raise ActionError("poll action succeeded; refresh failed; do not retry") from None
        if exc.request_sent and (not exc.status or exc.status >= 500):
            prefix = "offline; " if offline(exc) else ""
            raise ActionError(prefix + "delivery uncertain; verify before retry") from None
        if offline(exc):
            raise ActionError("offline") from None
        raise
    except (OSError, sqlite3.Error):
        if completed:
            raise ActionError("poll action succeeded; mirror refresh failed; do not retry") from None
        raise


def _run(command: str, db: Path, client: ShareClient, payload: dict[str, Any], *, reserved: bool = False) -> dict[str, Any]:
    if command in {"poll", "poll-vote", "poll-close"}:
        return poll_action(command, db, client, payload)
    if not reserved and command in {"send-new", "files", "voice"}:
        return _once(command, db, client, payload)
    if command in {"send", "react", "edit", "delete"}:
        return mutate(db, client, command, payload)
    if command == "send-new":
        target = payload.get("target")
        if not isinstance(target, dict):
            raise ActionError("Talk target required")
        if target.get("jid"):
            jid = token(target["jid"])
        else:
            uid = string(target, "user", maximum=512)
            if not uid:
                raise ActionError("Talk user required")
            # Validate text before creating a conversation.
            if not string(payload, "text", maximum=32000).strip():
                raise ActionError("empty message")
            jid = token(client.create_one_to_one(uid).get("token"))
        result = mutate(db, client, "send", dict(payload, jid=jid))
        return dict(result, kind=command, jid=jid, chat_jid=jid)
    if command == "files":
        return files(client, payload)
    if command == "voice":
        return voice(client, payload)
    if command == "media":
        return media(db, client, payload)
    if command == "download-pending":
        limit = payload.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ActionError("invalid download limit")
        pending = rows(db, "SELECT token,id,is_deleted FROM messages WHERE (file_id IS NOT NULL OR file_path!='') "
                       "AND local_path='' ORDER BY timestamp DESC LIMIT ?", (limit,))
        downloaded = failed = expired = 0
        for msg in pending:
            if msg["is_deleted"]:
                expired += 1
                continue
            try:
                media(db, client, {"jid": msg["token"], "id": str(msg["id"])})
                downloaded += 1
            except (api.ApiError, ActionError, OSError, sqlite3.Error) as exc:
                failed += 1
                if isinstance(exc, api.ApiError) and (offline(exc) or exc.status in (401, 429)):
                    break
        return {"ok": True, "kind": command, "downloaded": downloaded, "expired": expired, "failed": failed}
    if command == "star":
        jid, mid = token(payload.get("jid")), message_id(payload.get("id"))
        starred = payload.get("starred")
        if type(starred) is not bool:
            raise ActionError("invalid star state")
        cached_message(db, jid, mid)
        with closing(rw(db)) as conn, conn:
            if starred:
                conn.execute("INSERT OR IGNORE INTO stars(token,message_id,ts) VALUES (?,?,?)", (jid, mid, int(time.time())))
            else:
                conn.execute("DELETE FROM stars WHERE token=? AND message_id=?", (jid, mid))
        return {"ok": True, "kind": command, "starred": starred}
    if command == "forward":
        jid, dest = token(payload.get("jid")), token(payload.get("to_jid"))
        msg = cached_message(db, jid, message_id(payload.get("id")))
        if msg["is_deleted"]:
            raise ActionError("cannot forward deleted message")
        if msg["file_path"] or msg["file_id"]:
            if not reserved:
                return _once(command, db, client, payload)
            remote = msg["file_path"]
            if not remote:
                resolved = client._resolve_file_id(msg["file_id"])
                base = client._dav_path("")
                if not resolved.startswith(base):
                    raise ActionError("unsafe file reference")
                remote = unquote(resolved[len(base):])
            client._dav_path(remote)  # Validate reference before the guarded share.
            reference = local_id(payload)
            response = client._write(dest, SHARES, data={"shareType": 10, "shareWith": dest, "path": "/" + remote.lstrip("/"), "referenceId": reference})
            result = {"ok": True, "message_id": _shared_message_id(client, dest, response, reference)}
        else:
            text = store.render_text(msg["message"], json.loads(msg["params"]))
            result = mutate(db, client, "send", {"jid": dest, "text": text, "local_id": local_id(payload)})
        return dict(result, kind=command, target=dest, target_jid=dest)
    if command == "create-group":
        name = string(payload, "name", maximum=255).strip()
        if not name:
            raise ActionError("room name required")
        participants = payload.get("participants", [])
        if not isinstance(participants, list) or len(participants) > 100:
            raise ActionError("invalid participants")
        users = list(dict.fromkeys(user_id(p) for p in participants))
        jid = token(client.create_room(2, name).get("token"))
        failed = []
        for index, uid in enumerate(users):
            try:
                client.add_participant(jid, uid)
            except api.GuardError:
                raise
            except api.ApiError:
                failed.extend("users/" + u for u in users[index:])
                break
        return {"ok": True, "kind": command, "jid": jid, "name": name, "failed": failed}
    if command == "join-group":
        invite = string(payload, "invite")
        if "://" in invite:
            parsed, server = urlsplit(invite), urlsplit(client.server)
            if (parsed.scheme, parsed.hostname, parsed.port) != (server.scheme, server.hostname, server.port) or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ActionError("invite belongs to another server")
            prefix = server.path.rstrip("/") + "/call/"
            if not parsed.path.startswith(prefix):
                raise ActionError("invalid Talk invite")
            invite = parsed.path[len(prefix):]
        jid = token(invite)
        client.join_room_by_token(jid)
        return {"ok": True, "kind": command, "jid": jid}
    if command == "group-info":
        return group_info(client, token(payload.get("jid")))
    if command == "group-action":
        jid = token(payload.get("jid"))
        action, value = string(payload, "action"), payload.get("value")
        if action in {"rename", "description"}:
            text = string({"value": value}, "value", maximum=2000)
            if action == "rename":
                if not text.strip() or len(text) > 255:
                    raise ActionError("invalid room name")
                client.set_room_name(jid, text)
            else:
                client.set_room_description(jid, text)
        elif action == "add":
            users = value if isinstance(value, list) else [value]
            if not 1 <= len(users) <= 100:
                raise ActionError("invalid participants")
            for uid in list(dict.fromkeys(user_id(v) for v in users)):
                client.add_participant(jid, uid)
        elif action in {"remove", "promote", "demote"}:
            user_id(value)
            {"remove": client.remove_participant, "promote": client.promote_moderator,
             "demote": client.demote_moderator}[action](jid, value)
        elif action == "leave":
            client.leave_room(jid)
        else:
            raise ActionError("not supported", supported=False)
        return {"ok": True, "kind": command, "action": action, "jid": jid}
    if command == "contacts-search":
        return contacts(db, client, payload)
    if command == "contact-profile":
        user_id(payload.get("person"))
        return {"ok": True, "kind": command, "about": "", "business": {}}
    raise ActionError("not supported", supported=False)


def run(subcommand: str, argv: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        if subcommand in UNSUPPORTED:
            raise ActionError("not supported", supported=False)
        if subcommand == "check-number":
            return {"ok": False, "kind": subcommand, "supported": False, "registered": False,
                    "jid": "", "phone": string(payload, "phone"), "has_chat": False, "name": "", "error": "not supported"}
        identity = string(payload, "account", maximum=512)
        for index in range(0, len(argv), 2):
            if index + 1 >= len(argv) or argv[index] not in {"--account", "--limit"}:
                raise ActionError("invalid command arguments")
            if argv[index] == "--account":
                if identity and identity != argv[index + 1]:
                    raise ActionError("account scope mismatch")
                identity = argv[index + 1]
        accounts = prefs.load_accounts()
        if not identity:
            identity = next((a["account"] for a in accounts if a.get("default")), accounts[0]["account"] if accounts else "")
        account = next((a for a in accounts if a["account"] == identity), None)
        if account is None:
            raise ActionError("unknown account")
        db = prefs.store_dir(identity) / "talk.db"
        _no_links(db)
        client = ShareClient(account["server"], account["user"], account["app_password"], account.get("write_guard_prefix"))
        client.max_file_bytes = MAX_FILE
        # Helpers must be bounded; the daemon, not the UI, handles long backoff.
        client.max_retries = 0
        client.timeout = 15
        if subcommand in {"poll", "poll-vote", "poll-close"}:
            if not prefs.account_state(prefs.load_preferences(), identity)["online"]:
                raise ActionError("offline")
            caps = rows(db, "SELECT value FROM meta WHERE key='capabilities_json'")
            if not caps:
                raise ActionError("poll capabilities unavailable; synchronize first")
            client._capabilities = polls.decode(caps[0][0], {})
        return _run(subcommand, db, client, payload)
    except ActionError as exc:
        return {"ok": False, "supported": exc.supported, "error": str(exc)}
    except api.ApiError as exc:
        return {"ok": False, "error": exc.ocs_message}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error, subprocess.TimeoutExpired):
        return {"ok": False, "error": "local operation failed or invalid request"}
