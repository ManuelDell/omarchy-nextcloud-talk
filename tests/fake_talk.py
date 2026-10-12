"""Synthetic, stateful Talk HTTP fixture, shared by all test modules.

Usage::
    with FakeTalkServer() as server:
        room = server.add_room('room1', name='omanctalk-test-demo')
        msg = server.add_message('room1', 'Synthetic text', actor_id='alice')
        client = TalkClient(server.url, server.user, server.password)

Aliases: ``server.server``/``base_url`` are the URL, ``app_password`` is the
synthetic password. ``add_room``/``add_message`` accept keyword Talk fields or
raw dictionaries; return the mutable fixture dictionaries. ``edit_message``,
``delete_message``, ``react`` emit system events with parent snapshots.
``remove_room`` removes a fixture; ``add_file(path, bytes, file_id=...)`` adds
DAV data. ``fail_next(status, path=None, count=1, retry_after='0')`` queues HTTP
failures. ``requests`` records method/path/query/data/authenticated (never the
Authorization header). ``page_size`` can reduce chat pages for backfill tests.
Polls: add_poll(token, question, options, max_votes=0, result_mode=0) posts
an object/talk-poll comment; vote_poll/close_poll emit cursor-sensitive system
events. Options are zero-based indices; repeated form arrays remain lists.
All state and requests are synthetic; HTTP logging is disabled.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

# Explicit opt-in for this synthetic loopback fixture (also inherited by CLI tests).
os.environ["OMANCTALK_ALLOW_INSECURE_LOCALHOST"] = "1"

ROOM = "/ocs/v2.php/apps/spreed/api/v4/room"
CHAT = "/ocs/v2.php/apps/spreed/api/v1/chat"
REACTION = "/ocs/v2.php/apps/spreed/api/v1/reaction"
POLL = "/ocs/v2.php/apps/spreed/api/v1/poll"


class FakeTalkServer:
    quirk_system_cursor = True  # emulate Talk: lookIntoFuture from a system-message id returns nothing

    def __init__(self, user: str = "alice", password: str = "synthetic-password") -> None:
        self.user, self.password = user, password
        self.app_password = password
        self.rooms: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.members: dict[str, list[dict[str, Any]]] = {}
        self.reactions: dict[tuple[str, int], dict[str, list[dict[str, Any]]]] = {}
        self.files: dict[str, tuple[int, bytes]] = {}
        self.polls: dict[tuple[str, int], dict[str, Any]] = {}
        self.poll_votes: dict[tuple[str, int], dict[str, list[int]]] = {}
        self.requests: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.responses: list[tuple[str, Any]] = []
        self.page_size = 200
        self.clock = 1700000000
        self.next_id = 1
        self.features = ["chat-v2", "reactions", "edit-messages", "delete-messages",
                         "threads", "federation-v1", "archived-conversations-v2",
                         "scheduled-messages", "chat-read-marker", "talk-polls"]
        self.capabilities = {"capabilities": {"spreed": {"features": self.features,
            "config": {"chat": {"max-length": 32000}}}}}
        self.lock = threading.RLock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.handle_request()

            do_POST = do_PUT = do_DELETE = do_MKCOL = do_REPORT = do_SEARCH = do_GET

            def send(self, status: int, data: Any = None, headers: dict[str, str] | None = None,
                     raw: bytes | None = None) -> None:
                payload = raw if raw is not None else json.dumps({"ocs": {
                    "meta": {"status": "ok" if status < 400 else "failure",
                             "statuscode": status, "message": "synthetic response"},
                    "data": data if data is not None else {}}}, ensure_ascii=False).encode()
                if status == 304:
                    payload = b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def handle_request(self) -> None:
                with fixture.lock:
                    p = urlsplit(self.path)
                    path = unquote(p.path)
                    query = {k: v[-1] for k, v in parse_qs(p.query).items()}
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    data: Any = {k: v if k.endswith("[]") else v[-1] for k, v in
                                 parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
                    if self.headers.get("Content-Type", "").startswith("application/json"):
                        data = json.loads(raw)
                    expected = "Basic " + base64.b64encode(f"{fixture.user}:{fixture.password}".encode()).decode()
                    auth = self.headers.get("Authorization") == expected
                    fixture.requests.append({"method": self.command, "path": path,
                        "query": query, "data": data, "authenticated": auth,
                        "ocs": self.headers.get("OCS-APIRequest"),
                        "accept": self.headers.get("Accept"),
                        "has_auth": "Authorization" in self.headers})
                    for i, failure in enumerate(fixture.errors):
                        if failure["path"] is None or path.startswith(failure["path"]):
                            fixture.errors.pop(i)
                            self.send(failure["status"], headers={"Retry-After": failure["retry_after"]})
                            return
                    if path == "/index.php/login/v2":
                        payload = json.dumps({"login": fixture.url + "/login/synthetic",
                            "poll": {"endpoint": fixture.url + "/login/v2/poll", "token": "synthetic-poll"}}).encode()
                        self.send(200, raw=payload)
                        return
                    if path == "/login/v2/poll":
                        payload = json.dumps({"server": fixture.url, "loginName": fixture.user,
                                              "appPassword": fixture.password}).encode()
                        self.send(200, raw=payload)
                        return
                    if not auth:
                        self.send(401)
                        return
                    for i, (response_path, response_data) in enumerate(fixture.responses):
                        if path == response_path:
                            fixture.responses.pop(i)
                            self.send(200, raw=json.dumps({"ocs": {"meta": {"status": "ok", "statuscode": 200},
                                                                  "data": response_data}}).encode())
                            return
                    if path == "/ocs/v2.php/cloud/capabilities":
                        self.send(200, fixture.capabilities)
                        return
                    if path == "/ocs/v2.php/core/autocomplete/get":
                        self.send(200, [{"id": "bob", "label": "Synthetic Bob", "source": "users"}])
                        return
                    if path == "/remote.php/dav" and self.command == "SEARCH":
                        requested = ET.fromstring(raw).findtext(".//{DAV:}literal", "")
                        root = ET.Element("{DAV:}multistatus")
                        for rp, (fid, _) in fixture.files.items():
                            if str(fid) != requested:
                                continue
                            response = ET.SubElement(root, "{DAV:}response")
                            ET.SubElement(response, "{DAV:}href").text = "/remote.php/dav/files/" + fixture.user + "/" + rp
                            prop = ET.SubElement(ET.SubElement(response, "{DAV:}propstat"), "{DAV:}prop")
                            ET.SubElement(prop, "{http://owncloud.org/ns}fileid").text = str(fid)
                        self.send(207, raw=ET.tostring(root))
                        return
                    if path.startswith("/remote.php/dav/files/" + fixture.user + "/"):
                        remote = path.split("/files/" + fixture.user + "/", 1)[1]
                        if self.command == "MKCOL":
                            self.send(201)
                        elif self.command == "PUT":
                            fixture.add_file(remote, raw)
                            self.send(201)
                        elif self.command == "REPORT":
                            root = ET.Element("{DAV:}multistatus")
                            for rp, (fid, _) in fixture.files.items():
                                response = ET.SubElement(root, "{DAV:}response")
                                ET.SubElement(response, "{DAV:}href").text = "/remote.php/dav/files/" + fixture.user + "/" + rp
                                prop = ET.SubElement(ET.SubElement(response, "{DAV:}propstat"), "{DAV:}prop")
                                ET.SubElement(prop, "{http://owncloud.org/ns}fileid").text = str(fid)
                            self.send(207, raw=ET.tostring(root))
                        elif remote in fixture.files:
                            self.send(200, raw=fixture.files[remote][1])
                        else:
                            self.send(404)
                        return
                    if path == "/ocs/v2.php/apps/files_sharing/api/v1/shares":
                        token, rp = data["shareWith"], data["path"].lstrip("/")
                        if token not in fixture.rooms or rp not in fixture.files:
                            self.send(404)
                            return
                        fid, content = fixture.files[rp]
                        meta = json.loads(data.get("talkMetaData", "{}"))
                        msg = fixture.add_message(token, "{file}", messageType=meta.get("messageType", "comment"),
                            messageParameters={"file": {"type": "file", "id": fid, "name": rp.split("/")[-1],
                                "path": rp, "mimetype": "application/octet-stream", "size": len(content)}})
                        self.send(200, {"id": str(fid), "messageId": msg["id"]})
                        return
                    if path == ROOM:
                        if self.command == "POST":
                            token = "room" + str(len(fixture.rooms) + 1)
                            room = fixture.add_room(token, name=data.get("roomName", ""), type=int(data["roomType"]))
                            self.send(201, room)
                        else:
                            since = int(query.get("modifiedSince", "0"))
                            rooms = [r for r in fixture.rooms.values() if r["lastActivity"] > since]
                            stamp = fixture.clock
                            fixture.clock += 1
                            self.send(200, rooms, {"X-Nextcloud-Talk-Modified-Before": str(stamp)})
                        return
                    prefix = next((x for x in (ROOM, CHAT, REACTION, POLL,
                        "/ocs/v2.php/apps/spreed/api/v1/room") if path.startswith(x + "/")), None)
                    if not prefix:
                        self.send(404)
                        return
                    parts = path[len(prefix) + 1:].split("/")
                    token = parts[0]
                    if token not in fixture.rooms:
                        self.send(404)
                        return
                    room = fixture.rooms[token]
                    suffix = "/".join(parts[1:])
                    if prefix.endswith("/api/v1/room") and suffix.startswith("avatar"):
                        self.send(200, raw=b"synthetic-avatar", headers={"ETag": room.get("avatarVersion", "v1")})
                    elif prefix == ROOM:
                        if not suffix:
                            if self.command == "DELETE":
                                fixture.remove_room(token)
                            elif self.command == "PUT":
                                room["name"] = room["displayName"] = data["roomName"]
                                fixture.touch(token)
                            self.send(200, room)
                        elif suffix == "participants" and self.command == "GET":
                            self.send(200, fixture.members[token])
                        elif suffix == "participants" and self.command == "POST":
                            fixture.members[token].append({"actorType": data.get("source", "users"),
                                "actorId": data["newParticipant"], "displayName": "Synthetic User",
                                "attendeeId": len(fixture.members[token]) + 1, "participantType": 3})
                            self.send(200)
                        elif suffix == "participants/self":
                            fixture.remove_room(token)
                            self.send(200)
                        elif suffix == "attendees":
                            fixture.members[token] = [m for m in fixture.members[token]
                                if m["attendeeId"] != int(data["attendeeId"])]
                            self.send(200)
                        elif suffix == "moderators":
                            for m in fixture.members[token]:
                                if m["attendeeId"] == int(data["attendeeId"]):
                                    m["participantType"] = 2 if self.command == "POST" else 3
                            self.send(200)
                        elif suffix in ("favorite", "archive", "notify", "description", "participants/active"):
                            key = {"favorite": "isFavorite", "archive": "isArchived", "notify": "notificationLevel",
                                   "description": "description"}.get(suffix)
                            if key:
                                room[key] = data.get("description", "") if suffix == "description" else (
                                    int(data["level"]) if suffix == "notify" else self.command == "POST")
                            fixture.touch(token)
                            self.send(200, room)
                        else:
                            self.send(404)
                    elif prefix == CHAT:
                        if suffix == "read":
                            room["lastReadMessage"] = int(data.get("lastReadMessage", room.get("lastMessage", {}).get("id", 0)))
                            room["unreadMessages"] = 1 if self.command == "DELETE" else 0
                            self.send(200)
                        elif suffix:
                            mid = int(suffix)
                            msg = (fixture.edit_message(token, mid, data["message"]) if self.command == "PUT"
                                   else fixture.delete_message(token, mid))
                            self.send(200, msg)
                        elif self.command == "POST":
                            extra: dict[str, Any] = {"referenceId": data.get("referenceId", "")}
                            if data.get("replyTo"):
                                extra["parent"] = copy.deepcopy(fixture.message(token, int(data["replyTo"])))
                            self.send(201, fixture.add_message(token, data["message"], **extra))
                        else:
                            future = query.get("lookIntoFuture") == "1"
                            offset = int(query.get("lastKnownMessageId", "0"))
                            selected = [m for m in fixture.messages[token] if (m["id"] > offset if future
                                else not offset or m["id"] < offset)]
                            if future and fixture.quirk_system_cursor and any(
                                    m["id"] == offset and m.get("messageType") == "system"
                                    for m in fixture.messages[token]):
                                selected = []  # real Talk: nothing newer than a *system* message
                            selected.sort(key=lambda m: m["id"], reverse=not future)
                            selected = selected[:min(fixture.page_size, int(query.get("limit", "200")))]
                            if selected:
                                self.send(200, selected, {"X-Chat-Last-Given": str(selected[-1]["id"]),
                                    "X-Chat-Last-Common-Read": str(room.get("lastCommonReadMessage", 0))})
                            else:
                                self.send(304)
                    elif prefix == POLL:
                        if not suffix and self.command == "POST":
                            poll = fixture.add_poll(token, data.get("question", ""), data.get("options[]", []),
                                                    int(data.get("maxVotes", "0")), int(data.get("resultMode", "0")))
                            self.send(201, fixture.poll_snapshot(token, poll["id"]))
                            return
                        if not suffix.isdigit() or (token, int(suffix)) not in fixture.polls:
                            self.send(404)
                            return
                        pid = int(suffix)
                        poll = fixture.polls[(token, pid)]
                        if self.command == "POST":
                            try:
                                options = [int(i) for i in data.get("optionIds", data.get("optionIds[]", []))]
                                if poll["status"] or any(i < 0 or i >= len(poll["options"]) for i in options) or (
                                        poll["maxVotes"] and len(options) > poll["maxVotes"]):
                                    raise ValueError
                            except ValueError:
                                self.send(400)
                                return
                            fixture.vote_poll(token, pid, options)
                        elif self.command == "DELETE":
                            moderator = any(m["actorId"] == fixture.user and m.get("participantType") in (1, 2, 6)
                                            for m in fixture.members[token])
                            if poll["actorId"] != fixture.user and not moderator:
                                self.send(403)
                                return
                            fixture.close_poll(token, pid)
                        self.send(200, fixture.poll_snapshot(token, pid))
                    elif prefix == REACTION:
                        mid = int(suffix)
                        if self.command != "GET":
                            fixture.react(token, mid, data["reaction"], remove=self.command == "DELETE")
                        self.send(200, fixture.reactions.get((token, mid), {}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = self.server = self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def __enter__(self) -> FakeTalkServer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def touch(self, token: str) -> None:
        self.clock += 1
        self.rooms[token]["lastActivity"] = self.clock

    def add_room(self, token: str | dict[str, Any] = "room1", name: str = "omanctalk-test-demo",
                 **fields: Any) -> dict[str, Any]:
        with self.lock:
            if isinstance(token, dict):
                fields = {**token, **fields}
                token = str(fields.pop("token"))
            room = {"token": token, "id": len(self.rooms) + 1, "type": 2, "name": name,
                    "displayName": name, "lastActivity": self.clock + 1, "lastMessage": {},
                    "unreadMessages": 0, "avatarVersion": "v1", **fields}
            self.clock += 1
            self.rooms[token] = room
            self.messages.setdefault(token, [])
            self.members[token] = [{"actorType": "users", "actorId": self.user,
                "displayName": "Synthetic Alice", "attendeeId": 1, "participantType": 1}]
            return room

    def remove_room(self, token: str) -> None:
        with self.lock:
            self.rooms.pop(token, None)

    def add_message(self, token: str, text: str | dict[str, Any] = "Synthetic text",
                    actor_id: str = "alice", **fields: Any) -> dict[str, Any]:
        with self.lock:
            if isinstance(text, dict):
                fields = {**text, **fields}
                text = str(fields.pop("message", ""))
            msg = {"id": self.next_id, "token": token, "actorType": "users", "actorId": actor_id,
                   "actorDisplayName": "Synthetic " + actor_id, "timestamp": self.clock,
                   "message": text, "messageType": "comment", "messageParameters": {},
                   "reactions": {}, "reactionsSelf": [], **fields}
            msg["id"] = int(msg["id"])
            self.next_id = max(self.next_id, msg["id"] + 1)
            self.messages[token].append(msg)
            self.touch(token)
            self.rooms[token]["lastMessage"] = copy.deepcopy(msg)
            self.rooms[token]["unreadMessages"] += 1
            return msg

    def message(self, token: str, message_id: int) -> dict[str, Any]:
        return next(m for m in self.messages[token] if m["id"] == message_id)

    def edit_message(self, token: str, message_id: int, text: str) -> dict[str, Any]:
        with self.lock:
            parent = self.message(token, message_id)
            parent.update(message=text, lastEditTimestamp=self.clock + 1)
            return self.add_message(token, "Synthetic edit event", messageType="system",
                                    systemMessage="message_edited", parent=copy.deepcopy(parent))

    def delete_message(self, token: str, message_id: int, expired: bool = False) -> dict[str, Any]:
        with self.lock:
            parent = self.message(token, message_id)
            parent.update(message="", deleted=True)
            return self.add_message(token, "Synthetic deletion event", messageType="system",
                systemMessage="message_expired" if expired else "message_deleted", parent=copy.deepcopy(parent))

    def react(self, token: str, message_id: int, emoji: str, actor_id: str = "alice",
              remove: bool = False) -> dict[str, Any]:
        with self.lock:
            details = self.reactions.setdefault((token, message_id), {})
            actors = [a for a in details.get(emoji, []) if a["actorId"] != actor_id]
            if not remove:
                actors.append({"actorType": "users", "actorId": actor_id,
                               "actorDisplayName": "Synthetic " + actor_id, "timestamp": self.clock})
            if actors:
                details[emoji] = actors
            else:
                details.pop(emoji, None)
            parent = self.message(token, message_id)
            parent["reactions"] = {e: len(a) for e, a in details.items()}
            parent["reactionsSelf"] = [e for e, a in details.items() if any(x["actorId"] == self.user for x in a)]
            return self.add_message(token, "Synthetic reaction event", messageType="system",
                systemMessage="reaction_deleted" if remove else "reaction", parent=copy.deepcopy(parent))

    def add_poll(self, token: str, question: str = "Synthetic question", options: list[str] | None = None,
                 max_votes: int = 0, result_mode: int = 0) -> dict[str, Any]:
        with self.lock:
            pid = len(self.polls) + 1
            poll = {"id": pid, "question": question, "options": options or ["Synthetic A", "Synthetic B"],
                    "maxVotes": max_votes, "resultMode": result_mode, "status": 0,
                    "actorType": "users", "actorId": self.user, "actorDisplayName": "Synthetic Alice"}
            self.polls[(token, pid)] = poll
            self.poll_votes[(token, pid)] = {}
            self.add_message(token, "{object}", messageParameters={"object": {
                "type": "talk-poll", "id": pid, "name": question}})
            return poll

    def poll_snapshot(self, token: str, pid: int) -> dict[str, Any]:
        with self.lock:
            poll = copy.deepcopy(self.polls[(token, pid)])
            actors = self.poll_votes[(token, pid)]
            # Real Talk names the tallies "option-<index>" and only lists options that got votes.
            poll.update(votes={f"option-{i}": n for i in range(len(poll["options"]))
                               if (n := sum(i in choices for choices in actors.values()))},
                        votedSelf=actors.get(self.user, []), numVoters=sum(bool(v) for v in actors.values()),
                        details=[{"optionId": i, "actorType": "users", "actorId": actor,
                                  "actorDisplayName": "Synthetic " + actor}
                                 for actor, choices in actors.items() for i in choices])
            return poll

    def _poll_event(self, token: str, pid: int, event: str) -> None:
        room = self.rooms[token]
        last, activity = copy.deepcopy(room["lastMessage"]), room["lastActivity"]
        self.add_message(token, "Synthetic poll event", messageType="system", systemMessage=event,
                         messageParameters={"poll": {"type": "talk-poll", "id": pid}})
        room.update(lastMessage=last, lastActivity=activity)

    def vote_poll(self, token: str, pid: int, options: list[int], actor_id: str | None = None) -> None:
        with self.lock:
            actor = actor_id or self.user
            if options:
                self.poll_votes[(token, pid)][actor] = list(dict.fromkeys(options))
            else:
                self.poll_votes[(token, pid)].pop(actor, None)
            self._poll_event(token, pid, "poll_voted")

    def close_poll(self, token: str, pid: int) -> None:
        with self.lock:
            self.polls[(token, pid)]["status"] = 1
            self._poll_event(token, pid, "poll_closed")

    def add_file(self, path: str, content: bytes, file_id: int | None = None) -> int:
        with self.lock:
            fid = file_id or len(self.files) + 1
            self.files[path.lstrip("/")] = (fid, content)
            return fid

    def respond_next(self, path: str, data: Any) -> None:
        with self.lock:
            self.responses.append((path, data))

    def fail_next(self, status: int, path: str | None = None, count: int = 1,
                  retry_after: str = "0") -> None:
        with self.lock:
            self.errors.extend({"status": status, "path": path, "retry_after": retry_after}
                               for _ in range(count))
