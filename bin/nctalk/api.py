"""Bounded stdlib Talk transport (room v4, chat/reaction/avatar v1).

Endpoints follow nextcloud-talk.readthedocs.io: conversation, participant,
chat, reaction and avatar. Mutations always pass the optional dev guard.
No remote error bodies/URLs are exposed: they can contain credentials/content.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import email.utils
import http.client
import json
import os
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import error, parse, request

from .paths import absolute, check_file, directory_fd

ROOM = "/ocs/v2.php/apps/spreed/api/v4/room"
CHAT = "/ocs/v2.php/apps/spreed/api/v1/chat"
REACTION = "/ocs/v2.php/apps/spreed/api/v1/reaction"
POLL = "/ocs/v2.php/apps/spreed/api/v1/poll"
MAX_BYTES = 100 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int = 0, ocs_message: str = "request failed", *,
                 ocs_status: int | None = None, retry_after: float = 0,
                 request_sent: bool = False) -> None:
        self.status = status
        self.request_sent = request_sent
        self.ocs_status = ocs_status
        self.ocs_message = ocs_message
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {ocs_message}")


class GuardError(ApiError):
    def __init__(self) -> None:
        super().__init__(0, "write blocked by dev guard")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # urllib otherwise forwards Basic credentials to redirected hosts.
        return None


def _server(value: str) -> str:
    p = parse.urlsplit(value)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError("invalid server URL")
    if p.query or p.fragment or (p.scheme == "http" and (
            p.hostname not in ("localhost", "127.0.0.1", "::1") or
            os.environ.get("OMANCTALK_ALLOW_INSECURE_LOCALHOST") != "1")):
        raise ValueError("HTTPS required")
    return value.rstrip("/")


def _q(value: Any) -> str:
    return parse.quote(str(value), safe="")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_message(status: int) -> str:
    return {401: "authentication failed", 403: "forbidden", 404: "not found",
            429: "rate limited", 500: "server error", 502: "server error",
            503: "server unavailable", 504: "server timeout"}.get(status, "OCS request failed")


def _delay(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After", "")
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            delay = 0
    return min(900.0, max(0.5 * 2 ** attempt, delay))


class TalkClient:
    def __init__(self, server: str, user: str, app_password: str,
                 write_guard_prefix: str | None = None) -> None:
        self.server = _server(server)
        self.user = user
        if write_guard_prefix is not None and (not isinstance(write_guard_prefix, str) or not write_guard_prefix):
            raise ValueError("invalid write guard")
        self.write_guard_prefix = write_guard_prefix
        self.timeout = 30.0
        self.max_retries = 3
        self.max_file_bytes = MAX_BYTES
        self.transfer_timeout = 900.0
        self._transfer_deadline: float | None = None
        self.cancelled: Callable[[], bool] = lambda: False
        self.sleep = time.sleep
        self._auth = "Basic " + base64.b64encode(
            f"{user}:{app_password}".encode("utf-8")).decode("ascii")
        client = self
        class TrackedConnection:
            def send(self, data: Any) -> None:
                try:
                    client._check_stop()
                    if self.sock is None:
                        self._establishing = True
                        try:
                            self.connect()
                        finally:
                            self._establishing = False
                    client._check_stop()
                    if not getattr(self, "_establishing", False):
                        client._request_sent = True
                    super().send(data)
                except BaseException:
                    self.close()
                    raise
        class HTTPConnection(TrackedConnection, http.client.HTTPConnection):
            pass
        class HTTPSConnection(TrackedConnection, http.client.HTTPSConnection):
            pass
        class HTTPHandler(request.HTTPHandler):
            def http_open(self, req: Any) -> Any:
                return self.do_open(HTTPConnection, req)
        class HTTPSHandler(request.HTTPSHandler):
            def https_open(self, req: Any) -> Any:
                return self.do_open(HTTPSConnection, req, context=self._context)
        self._opener = request.build_opener(_NoRedirect(), HTTPHandler(), HTTPSHandler())
        self._request_sent = False
        self._rooms: dict[str, dict[str, Any]] = {}
        self._capabilities: dict[str, Any] = {}

    @contextmanager
    def _transfer(self) -> Iterator[None]:
        previous = self._transfer_deadline
        deadline = time.monotonic() + self.transfer_timeout
        self._transfer_deadline = min(previous, deadline) if previous is not None else deadline
        try:
            self._check_stop()
            yield
        finally:
            self._transfer_deadline = previous

    def _check_stop(self, deadline: float | None = None) -> None:
        deadline = deadline if deadline is not None else self._transfer_deadline
        if self.cancelled():
            raise ApiError(0, "request interrupted")
        if deadline is not None and time.monotonic() >= deadline:
            raise ApiError(0, "transfer time limit exceeded")

    def _open(self, path: str, method: str = "GET", data: Any = None,
              *, headers: dict[str, str] | None = None, auth: bool = True,
              timeout: float | None = None) -> Any:
        url = self.server + path
        h = {"Accept": "application/json", "OCS-APIRequest": "true"}
        if auth:
            h["Authorization"] = self._auth
        if isinstance(data, dict):
            data = parse.urlencode({k: int(v) if isinstance(v, bool) else v
                                    for k, v in data.items() if v is not None},
                                   doseq=True).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        h.update(headers or {})
        mutation = method not in ("GET", "PROPFIND", "REPORT", "SEARCH")
        for attempt in range(self.max_retries + 1):
            self._request_sent = False
            self._check_stop()
            if hasattr(data, "seek"):
                data.seek(0)
            req = request.Request(url, data=data, headers=h, method=method)
            try:
                remaining = self._transfer_deadline - time.monotonic() if self._transfer_deadline is not None else None
                socket_timeout = timeout or self.timeout
                if remaining is not None:
                    self._check_stop()
                    socket_timeout = min(socket_timeout, max(0.001, remaining))
                return self._opener.open(req, timeout=socket_timeout)
            except error.HTTPError as exc:
                status, eh = exc.code, exc.headers
                try:
                    body = exc.read(MAX_JSON + 1)
                except (OSError, http.client.HTTPException):
                    raise ApiError(0, "network request failed",
                                   request_sent=mutation and self._request_sent) from None
                finally:
                    exc.close()
                if status == 304:
                    return _EmptyResponse(eh)
                delay = _delay(eh, attempt)
                # A 5xx POST may already have committed: never replay it.
                retry = status == 429 or (status >= 500 and method in
                                         ("GET", "PROPFIND", "REPORT", "SEARCH", "PUT", "DELETE"))
                if retry and attempt < self.max_retries:
                    if self._transfer_deadline is not None:
                        self._check_stop()
                        delay = min(delay, max(0.0, self._transfer_deadline - time.monotonic()))
                    self.sleep(delay)
                    self._check_stop()
                    continue
                ocs_status = None
                try:
                    ocs_status = _int(json.loads(body).get("ocs", {}).get("meta", {}).get("statuscode"))
                except (ValueError, AttributeError, TypeError):
                    pass
                raise ApiError(status, _safe_message(status), ocs_status=ocs_status,
                               retry_after=delay if retry else 0,
                               request_sent=mutation and self._request_sent) from None
            except ApiError as exc:
                exc.request_sent = exc.request_sent or (mutation and self._request_sent)
                raise
            except (error.URLError, OSError, socket.timeout, ValueError, http.client.HTTPException):
                raise ApiError(0, "network request failed",
                               request_sent=mutation and self._request_sent) from None

    def _json(self, path: str, method: str = "GET", data: Any = None,
              **kwargs: Any) -> tuple[Any, Any]:
        try:
            with self._open(path, method, data, **kwargs) as response:
                headers = response.headers
                if response.status == 304:
                    return [], headers
                body = response.read(MAX_JSON + 1)
        except (OSError, http.client.HTTPException):
            raise ApiError(0, "network request failed", request_sent=method != "GET") from None
        if len(body) > MAX_JSON:
            raise ApiError(0, "response too large", request_sent=method != "GET")
        try:
            obj = json.loads(body.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError
            if "ocs" not in obj:
                if path.startswith("/ocs/"):
                    raise ValueError
                return obj, headers
            ocs = obj["ocs"]
            meta = ocs.get("meta", {})
            code = _int(meta.get("statuscode"))
            if meta.get("status") == "failure" or code >= 400:
                raise ApiError(code, _safe_message(code), ocs_status=code, request_sent=method != "GET")
            if meta.get("status") != "ok" or code < 100 or "data" not in ocs:
                raise ValueError
            return ocs["data"], headers
        except (ValueError, UnicodeError, TypeError, AttributeError):
            raise ApiError(0, "invalid JSON response", request_sent=method != "GET") from None

    def capabilities(self) -> dict[str, Any]:
        data, _ = self._json("/ocs/v2.php/cloud/capabilities")
        self._capabilities = data if isinstance(data, dict) else {}
        return self._capabilities

    @property
    def features(self) -> set[str]:
        caps = self._capabilities.get("capabilities", self._capabilities)
        if not isinstance(caps, dict):
            return set()
        spreed = caps.get("spreed", {})
        values = spreed.get("features", []) if isinstance(spreed, dict) else []
        return {v for v in values if isinstance(v, str)} if isinstance(values, list) else set()

    def rooms(self, modified_since: Any = None) -> tuple[list[dict[str, Any]], str]:
        params: dict[str, Any] = {"noStatusUpdate": 1, "includeStatus": 0}
        if modified_since is not None:
            params["modifiedSince"] = modified_since
        data, h = self._json(ROOM + "?" + parse.urlencode(params))
        if not isinstance(data, list) or any(not isinstance(r, dict) or
                not isinstance(r.get("token"), str) or not r["token"] for r in data):
            raise ApiError(0, "invalid room list")
        rooms = data
        for room in rooms:
            self._rooms[room["token"]] = room
        return rooms, h.get("X-Nextcloud-Talk-Modified-Before", "")

    def room(self, token: str) -> dict[str, Any]:
        data, _ = self._json(ROOM + "/" + _q(token))
        room = data if isinstance(data, dict) else {}
        self._rooms[token] = room
        return room

    def participants(self, token: str) -> list[dict[str, Any]]:
        data, _ = self._json(ROOM + "/" + _q(token) + "/participants")
        if not isinstance(data, list) or any(not isinstance(m, dict) or
                any(not isinstance(m.get(k, ""), str) for k in ("actorType", "actorId")) for m in data):
            raise ApiError(0, "invalid participants list")
        return data

    def chat_history(self, token: str, last_known_id: Any = None, limit: int = 200,
                     look_into_future: bool = False, timeout: int = 0
                     ) -> tuple[list[dict[str, Any]], int, int]:
        params = {"lookIntoFuture": int(look_into_future), "limit": max(1, min(200, limit)),
                  "timeout": max(0, min(30, timeout)), "setReadMarker": 0,
                  "includeLastKnown": 0, "markNotificationsAsRead": 0}
        if last_known_id is not None:
            params["lastKnownMessageId"] = _int(last_known_id)
        data, h = self._json(CHAT + "/" + _q(token) + "?" + parse.urlencode(params),
                             timeout=max(self.timeout, params["timeout"] + 10))
        if not isinstance(data, list) or any(not isinstance(m, dict) or
                not isinstance(m.get("id"), (str, int)) or isinstance(m.get("id"), bool) or
                not str(m["id"]).isascii() or not str(m["id"]).isdigit() or
                not 0 < _int(m["id"]) < 2 ** 63 for m in data):
            raise ApiError(0, "invalid message page")
        return (data, _int(h.get("X-Chat-Last-Given")),
                _int(h.get("X-Chat-Last-Common-Read")))

    def reaction_details(self, token: str, message_id: Any) -> dict[str, Any]:
        data, _ = self._json(REACTION + "/" + _q(token) + "/" + _q(message_id))
        return data if isinstance(data, dict) else {}

    def avatar(self, token: str, dark: bool = False) -> tuple[bytes, str]:
        path = "/ocs/v2.php/apps/spreed/api/v1/room/" + _q(token) + "/avatar"
        try:
            with self._open(path + ("/dark" if dark else "")) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                version = response.headers.get("ETag", "")
        except (OSError, http.client.HTTPException):
            raise ApiError(0, "network request failed") from None
        if len(data) > 2 * 1024 * 1024:
            raise ApiError(0, "avatar too large")
        return data, version or str(self._rooms.get(token, {}).get("avatarVersion", ""))

    def _dav_path(self, remote: str) -> str:
        remote = remote.lstrip("/")
        if any(p in (".", "..") for p in remote.split("/")) or "\x00" in remote:
            raise ValueError("invalid remote path")
        return "/remote.php/dav/files/" + _q(self.user) + "/" + parse.quote(remote, safe="/")

    def _resolve_file_id(self, file_id: Any) -> str:
        # Nextcloud filter-files REPORT does not support file IDs; DAV SEARCH does.
        root = ET.Element("{DAV:}searchrequest")
        search = ET.SubElement(root, "{DAV:}basicsearch")
        prop = ET.SubElement(ET.SubElement(search, "{DAV:}select"), "{DAV:}prop")
        ET.SubElement(prop, "{http://owncloud.org/ns}fileid")
        scope = ET.SubElement(ET.SubElement(search, "{DAV:}from"), "{DAV:}scope")
        ET.SubElement(scope, "{DAV:}href").text = "/files/" + _q(self.user) + "/"
        ET.SubElement(scope, "{DAV:}depth").text = "infinity"
        eq = ET.SubElement(ET.SubElement(search, "{DAV:}where"), "{DAV:}eq")
        ET.SubElement(ET.SubElement(eq, "{DAV:}prop"), "{http://owncloud.org/ns}fileid")
        ET.SubElement(eq, "{DAV:}literal").text = str(int(file_id))
        ET.SubElement(ET.SubElement(search, "{DAV:}limit"), "{DAV:}nresults").text = "1"
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            with self._open("/remote.php/dav", "SEARCH", body,
                            headers={"Content-Type": "application/xml"}) as r:
                raw = r.read(MAX_JSON + 1)
        except (OSError, http.client.HTTPException):
            raise ApiError(0, "network request failed") from None
        try:
            root = ET.fromstring(raw)
            for response in root.findall("{DAV:}response"):
                href = response.findtext("{DAV:}href", "")
                fid = response.findtext(".//{http://owncloud.org/ns}fileid", "")
                if fid == str(file_id):
                    path = parse.urlsplit(href).path
                    # Never fetch a DAV result outside this account's namespace.
                    base = parse.urlsplit(self.server).path + self._dav_path("")
                    if path.startswith(base):
                        return self._dav_path(parse.unquote(path[len(base):]))
        except (ET.ParseError, ValueError):
            pass
        raise ApiError(404, "file not found")

    def download_file(self, remote_path_or_file_id: str | int,
                      dest: str | os.PathLike[str]) -> Path:
        with self._transfer():
            return self._download_file(remote_path_or_file_id, dest)

    def _download_file(self, remote_path_or_file_id: str | int,
                       dest: str | os.PathLike[str]) -> Path:
        deadline = self._transfer_deadline
        self._check_stop(deadline)
        remote = remote_path_or_file_id
        path = self._resolve_file_id(remote) if isinstance(remote, int) or str(remote).isdigit() else self._dav_path(str(remote))
        target = absolute(dest)
        try:
            with directory_fd(target.parent, create=True) as directory:
                check_file(target)
                name = ".omanctalk-" + uuid.uuid4().hex
                try:
                    with self._open(path) as response:
                        if _int(response.headers.get("Content-Length")) > self.max_file_bytes:
                            raise ApiError(0, "file too large")
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                        with os.fdopen(fd, "wb") as out:
                            count = 0
                            while True:
                                self._check_stop(deadline)
                                chunk = getattr(response, "read1", response.read)(64 * 1024)
                                self._check_stop(deadline)
                                if not chunk:
                                    break
                                count += len(chunk)
                                if count > self.max_file_bytes:
                                    raise ApiError(0, "file too large")
                                out.write(chunk)
                        os.replace(name, target.name, src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    try:
                        os.unlink(name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            return target
        except (OSError, error.URLError, http.client.HTTPException):
            raise ApiError(0, "file download failed") from None

    def autocomplete_users(self, search: str) -> list[dict[str, Any]]:
        data, _ = self._json("/ocs/v2.php/core/autocomplete/get?" + parse.urlencode(
            {"search": search, "shareTypes[]": 0, "limit": 50}))
        return data if isinstance(data, list) else []

    def _guard(self, token: str | None = None, name: str | None = None) -> None:
        if self.write_guard_prefix is None:
            return
        if token is not None:
            # Refresh at mutation time: a stale cached name must not bypass the guard.
            name = str(self.room(token).get("name", ""))
        if name is None or not name.startswith(self.write_guard_prefix):
            raise GuardError()

    def _write(self, token: str, path: str, method: str = "POST", data: Any = None) -> Any:
        self._guard(token)
        if path == "/ocs/v2.php/apps/files_sharing/api/v1/shares" and isinstance(data, dict):
            data = dict(data)
            meta = json.loads(data.get("talkMetaData", "{}"))
            for key in ("caption", "replyTo", "threadId", "threadTitle", "silent"):
                value = data.pop(key, None)
                if value is not None:
                    meta[key] = value
            data["talkMetaData"] = json.dumps(meta, ensure_ascii=False)
        return self._json(path, method, data)[0]

    def send_message(self, token: str, text: str, reply_to: Any = None,
                     reference_id: str | None = None, silent: bool = False) -> Any:
        return self._write(token, CHAT + "/" + _q(token), data={"message": text,
            "replyTo": reply_to or None, "referenceId": reference_id, "silent": silent})

    def edit_message(self, token: str, message_id: Any, text: str) -> Any:
        return self._write(token, CHAT + "/" + _q(token) + "/" + _q(message_id),
                           "PUT", {"message": text})

    def delete_message(self, token: str, message_id: Any) -> Any:
        return self._write(token, CHAT + "/" + _q(token) + "/" + _q(message_id), "DELETE")

    def add_reaction(self, token: str, message_id: Any, emoji: str) -> Any:
        return self._write(token, REACTION + "/" + _q(token) + "/" + _q(message_id),
                           data={"reaction": emoji})

    def remove_reaction(self, token: str, message_id: Any, emoji: str) -> Any:
        return self._write(token, REACTION + "/" + _q(token) + "/" + _q(message_id),
                           "DELETE", {"reaction": emoji})

    def _poll_feature(self) -> None:
        if "talk-polls" not in self.features:
            raise ApiError(0, "polls not supported")

    def poll_get(self, token: str, poll_id: Any) -> dict[str, Any]:
        self._poll_feature()
        data, _ = self._json(POLL + "/" + _q(token) + "/" + _q(poll_id))
        if not isinstance(data, dict) or _int(data.get("id")) != _int(poll_id):
            raise ApiError(0, "invalid poll response")
        return data

    def poll_create(self, token: str, question: str, options: list[str],
                    max_votes: int = 0, result_mode: int = 0) -> Any:
        self._poll_feature()
        return self._write(token, POLL + "/" + _q(token), data={
            "question": question, "options[]": options, "maxVotes": max_votes,
            "resultMode": result_mode})

    def poll_vote(self, token: str, poll_id: Any, option_ids: list[int]) -> Any:
        self._poll_feature()
        # JSON preserves [] exactly; an empty form field could be coerced to option 0.
        self._guard(token)
        return self._json(POLL + "/" + _q(token) + "/" + _q(poll_id), "POST",
                          json.dumps({"optionIds": option_ids}).encode("utf-8"),
                          headers={"Content-Type": "application/json"})[0]

    def poll_close(self, token: str, poll_id: Any) -> Any:
        self._poll_feature()
        return self._write(token, POLL + "/" + _q(token) + "/" + _q(poll_id), "DELETE")

    def mark_read(self, token: str, last_read_message: Any = None) -> Any:
        return self._write(token, CHAT + "/" + _q(token) + "/read",
                           data={"lastReadMessage": last_read_message})

    def mark_unread(self, token: str) -> Any:
        return self._write(token, CHAT + "/" + _q(token) + "/read", "DELETE")

    def _room_write(self, token: str, suffix: str, method: str = "POST", data: Any = None) -> Any:
        return self._write(token, ROOM + "/" + _q(token) + suffix, method, data)

    def set_favorite(self, token: str, favorite: bool) -> Any:
        return self._room_write(token, "/favorite", "POST" if favorite else "DELETE")

    def set_archived(self, token: str, archived: bool) -> Any:
        return self._room_write(token, "/archive", "POST" if archived else "DELETE")

    def set_notification_level(self, token: str, level: int) -> Any:
        return self._room_write(token, "/notify", data={"level": level})

    def create_room(self, type: int, name: str = "", invite: str = "") -> dict[str, Any]:
        # 1:1 room names cannot be supplied; guard cannot safely allow their creation.
        self._guard(name=None if type == 1 else name)
        data, _ = self._json(ROOM, "POST", {"roomType": type, "roomName": name, "invite": invite})
        if isinstance(data, dict) and data.get("token"):
            self._rooms[str(data["token"])] = data
        return data

    def create_one_to_one(self, user_id: str) -> dict[str, Any]:
        return self.create_room(1, invite=user_id)

    def join_room_by_token(self, token: str) -> Any:
        return self._room_write(token, "/participants/active")

    def leave_room(self, token: str) -> Any:
        return self._room_write(token, "/participants/self", "DELETE")

    def delete_room(self, token: str) -> Any:
        result = self._room_write(token, "", "DELETE")
        self._rooms.pop(token, None)
        return result

    def add_participant(self, token: str, user_id: str, source: str = "users") -> Any:
        return self._room_write(token, "/participants", data={"newParticipant": user_id, "source": source})

    def _attendee(self, token: str, participant: str | int) -> int:
        if isinstance(participant, int) or str(participant).isdigit():
            return int(participant)
        actor_type, sep, actor_id = str(participant).partition("/")
        if not sep:
            actor_type, actor_id = "users", str(participant)
        for member in self.participants(token):
            if member.get("actorType") == actor_type and member.get("actorId") == actor_id:
                return int(member["attendeeId"])
        raise ApiError(404, "participant not found")

    def remove_participant(self, token: str, participant: str | int) -> Any:
        return self._room_write(token, "/attendees", "DELETE", {"attendeeId": self._attendee(token, participant)})

    def promote_moderator(self, token: str, participant: str | int) -> Any:
        return self._room_write(token, "/moderators", data={"attendeeId": self._attendee(token, participant)})

    def demote_moderator(self, token: str, participant: str | int) -> Any:
        return self._room_write(token, "/moderators", "DELETE", {"attendeeId": self._attendee(token, participant)})

    def set_room_name(self, token: str, name: str) -> Any:
        self._guard(name=name)
        result = self._room_write(token, "", "PUT", {"roomName": name})
        self._rooms.pop(token, None)
        return result

    def set_room_description(self, token: str, description: str) -> Any:
        return self._room_write(token, "/description", "PUT", {"description": description})

    def upload_and_share(self, token: str, local_path: str | os.PathLike[str],
                         caption: str | None = None, voice: bool = False) -> Any:
        with self._transfer():
            return self._upload_and_share(token, local_path, caption, voice)

    def _upload_and_share(self, token: str, local_path: str | os.PathLike[str],
                          caption: str | None, voice: bool) -> Any:
        deadline = self._transfer_deadline
        self._check_stop(deadline)
        self._guard(token)
        local = Path(local_path)
        if not local.is_file() or local.stat().st_size > self.max_file_bytes:
            raise ValueError("invalid upload file")
        # Unique names avoid overwriting user files. Talk may not exist on fresh accounts.
        try:
            with self._open(self._dav_path("Talk"), "MKCOL"):
                pass
        except ApiError as exc:
            if exc.status != 405:
                raise
        remote = "Talk/" + uuid.uuid4().hex + "-" + local.name
        client = self
        class BoundedUpload:
            def __init__(self, source: Any) -> None:
                self.source = source
            def read(self, size: int = -1) -> bytes:
                client._check_stop(deadline)
                data = self.source.read(min(size, 65536) if size >= 0 else 65536)
                client._check_stop(deadline)
                return data
            def seek(self, offset: int) -> int:
                client._check_stop(deadline)
                return self.source.seek(offset)
        with local.open("rb") as source:
            with self._open(self._dav_path(remote), "PUT", BoundedUpload(source), headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(local.stat().st_size)}):
                pass
        self._check_stop(deadline)
        meta = {"messageType": "voice-message" if voice else "comment"}
        if caption is not None:
            meta["caption"] = caption
        return self._write(token, "/ocs/v2.php/apps/files_sharing/api/v1/shares", data={
            "shareType": 10, "shareWith": token, "path": "/" + remote,
            "talkMetaData": json.dumps(meta, ensure_ascii=False)})

    @staticmethod
    def login_flow_start(server: str) -> dict[str, Any]:
        return login_flow_start(server)

    @staticmethod
    def login_flow_poll(endpoint: str, token: str) -> dict[str, Any]:
        return login_flow_poll(endpoint, token)


class _EmptyResponse:
    status = 304

    def __init__(self, headers: Any) -> None:
        self.headers = headers

    def __enter__(self) -> _EmptyResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        return b""


def login_flow_start(server: str) -> dict[str, Any]:
    client = TalkClient(server, "", "")
    return client._json("/index.php/login/v2", "POST", {}, auth=False)[0]


def login_flow_poll(endpoint: str, token: str) -> dict[str, Any]:
    p = parse.urlsplit(_server(endpoint))
    origin = parse.urlunsplit((p.scheme, p.netloc, "", "", ""))
    client = TalkClient(origin, "", "")
    return client._json(p.path, "POST", {"token": token}, auth=False)[0]
