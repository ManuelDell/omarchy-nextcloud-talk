from __future__ import annotations

import sys
import os
import io
import http.client
import time
import threading
from unittest.mock import patch
from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk.api import ApiError, CHAT, GuardError, ROOM, TalkClient, login_flow_poll, login_flow_start
from fake_talk import FakeTalkServer


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeTalkServer()
        self.addCleanup(self.server.close)
        self.server.add_room("room1")
        self.client = TalkClient(self.server.url, self.server.user, self.server.password)
        self.delays: list[float] = []
        self.client.sleep = self.delays.append

    def test_capabilities_and_headers(self) -> None:
        self.client.capabilities()
        self.assertIn("chat-v2", self.client.features)
        req = self.server.requests[-1]
        self.assertTrue(req["authenticated"])
        self.assertEqual(req["ocs"], "true")
        self.assertEqual(req["accept"], "application/json")

    def test_rooms_modified_since(self) -> None:
        rooms, before = self.client.rooms()
        self.assertEqual(len(rooms), 1)
        self.assertEqual(self.client.rooms(before)[0], [])
        self.server.add_message("room1", "Synthetic update")
        self.assertEqual(len(self.client.rooms(before)[0]), 1)
        self.assertEqual(self.client.room("room1")["token"], "room1")

    def test_history_directions_and_304_never_read(self) -> None:
        for i in range(5):
            self.server.add_message("room1", f"Synthetic {i}")
        msgs, given, _ = self.client.chat_history("room1", limit=2)
        self.assertEqual([m["id"] for m in msgs], [5, 4])
        self.assertEqual(given, 4)
        msgs, given, _ = self.client.chat_history("room1", 3, look_into_future=True)
        self.assertEqual([m["id"] for m in msgs], [4, 5])
        self.assertEqual(given, 5)
        self.assertEqual(self.client.chat_history("room1", 5, look_into_future=True), ([], 0, 0))
        for req in self.server.requests:
            self.assertEqual(req["query"]["setReadMarker"], "0")
            self.assertEqual(req["query"]["markNotificationsAsRead"], "0")

    def test_unicode_send_reply_edit_delete(self) -> None:
        msg = self.client.send_message("room1", "Synthetisch ä 🍀", reference_id="synthetic-ref")
        self.assertEqual(msg["message"], "Synthetisch ä 🍀")
        reply = self.client.send_message("room1", "Synthetic reply", reply_to=msg["id"], silent=True)
        self.assertEqual(reply["parent"]["id"], msg["id"])
        self.assertEqual(self.client.edit_message("room1", msg["id"], "Synthetic edit")["systemMessage"], "message_edited")
        self.assertEqual(self.client.delete_message("room1", msg["id"])["systemMessage"], "message_deleted")

    def test_reactions(self) -> None:
        msg = self.server.add_message("room1")
        self.client.add_reaction("room1", msg["id"], "🍀")
        self.assertEqual(self.client.reaction_details("room1", msg["id"])["🍀"][0]["actorId"], "alice")
        self.client.remove_reaction("room1", msg["id"], "🍀")
        self.assertEqual(self.client.reaction_details("room1", msg["id"]), {})

    def test_room_mutations_and_participants(self) -> None:
        self.client.set_favorite("room1", True)
        self.client.set_archived("room1", True)
        self.client.set_notification_level("room1", 3)
        self.client.set_room_name("room1", "Synthetic rename")
        self.client.set_room_description("room1", "Synthetic description")
        room = self.client.room("room1")
        self.assertTrue(room["isFavorite"])
        self.assertTrue(room["isArchived"])
        self.assertEqual(room["notificationLevel"], 3)
        self.assertEqual(room["description"], "Synthetic description")
        self.client.add_participant("room1", "bob")
        self.client.promote_moderator("room1", "users/bob")
        self.assertEqual(self.client.participants("room1")[-1]["participantType"], 2)
        self.client.demote_moderator("room1", 2)
        self.client.remove_participant("room1", "bob")
        self.assertEqual(len(self.client.participants("room1")), 1)
        self.client.mark_read("room1", 1)
        self.client.mark_unread("room1")
        self.assertEqual(self.client.room("room1")["unreadMessages"], 1)

    def test_create_join_leave_delete(self) -> None:
        room = self.client.create_room(2, "Synthetic group", "bob")
        self.client.join_room_by_token(room["token"])
        self.client.leave_room(room["token"])
        self.assertNotIn(room["token"], self.server.rooms)
        room = self.client.create_one_to_one("bob")
        self.assertEqual(room["type"], 1)
        self.client.delete_room(room["token"])
        self.assertNotIn(room["token"], self.server.rooms)

    def test_guard_every_mutation(self) -> None:
        self.server.add_room("unsafe", name="Existing synthetic room")
        c = TalkClient(self.server.url, self.server.user, self.server.password, "omanctalk-test-")
        mutations = [lambda: c.send_message("unsafe", "Synthetic"),
            lambda: c.edit_message("unsafe", 1, "Synthetic"), lambda: c.delete_message("unsafe", 1),
            lambda: c.add_reaction("unsafe", 1, "🍀"), lambda: c.remove_reaction("unsafe", 1, "🍀"),
            lambda: c.mark_read("unsafe"), lambda: c.mark_unread("unsafe"),
            lambda: c.set_favorite("unsafe", True), lambda: c.set_archived("unsafe", True),
            lambda: c.set_notification_level("unsafe", 3), lambda: c.create_room(2, "Unsafe"),
            lambda: c.create_one_to_one("bob"), lambda: c.join_room_by_token("unsafe"),
            lambda: c.leave_room("unsafe"), lambda: c.delete_room("unsafe"),
            lambda: c.add_participant("unsafe", "bob"), lambda: c.remove_participant("unsafe", 1),
            lambda: c.promote_moderator("unsafe", 1), lambda: c.demote_moderator("unsafe", 1),
            lambda: c.set_room_name("unsafe", "omanctalk-test-new"),
            lambda: c.set_room_description("unsafe", "Synthetic"),
            lambda: c.upload_and_share("unsafe", "/nonexistent")]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(GuardError):
                mutate()
        self.assertTrue(all(r["method"] == "GET" for r in self.server.requests))
        c.send_message("room1", "Synthetic allowed")
        with self.assertRaises(GuardError):
            c.set_room_name("room1", "unsafe")
        self.server.rooms["room1"]["name"] = "Unsafe renamed by other client"
        with self.assertRaises(GuardError):
            c.mark_read("room1")

    def test_retries_429_and_500(self) -> None:
        self.server.fail_next(429, ROOM, count=2, retry_after="2")
        self.client.rooms()
        self.assertEqual(self.delays, [2.0, 2.0])
        self.server.fail_next(500, ROOM)
        self.client.rooms()
        self.assertEqual(self.delays[-1], 0.5)

    def test_retry_exhaustion_and_auth(self) -> None:
        self.server.fail_next(503, ROOM, count=4)
        with self.assertRaises(ApiError) as result:
            self.client.rooms()
        self.assertEqual(result.exception.status, 503)
        self.assertEqual(len(self.delays), 3)
        self.server.fail_next(401, ROOM)
        with self.assertRaises(ApiError) as result:
            self.client.rooms()
        self.assertEqual(result.exception.ocs_status, 401)
        self.assertNotIn(self.server.password, str(result.exception))
        self.assertEqual(len(self.delays), 3)

    def test_post_500_not_replayed(self) -> None:
        self.server.fail_next(500, CHAT)
        with self.assertRaises(ApiError):
            self.client.send_message("room1", "Synthetic")
        self.assertEqual(self.delays, [])

    def test_avatar_autocomplete(self) -> None:
        data, version = self.client.avatar("room1", dark=True)
        self.assertEqual(data, b"synthetic-avatar")
        self.assertEqual(version, "v1")
        self.assertEqual(self.client.autocomplete_users("Synthetic")[0]["id"], "bob")

    def test_download_path_id_size_and_atomicity(self) -> None:
        self.server.add_file("Talk/synthetic ä.txt", b"synthetic bytes", file_id=42)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "download"
            self.client.download_file("Talk/synthetic ä.txt", dest)
            self.assertEqual(dest.read_bytes(), b"synthetic bytes")
            self.client.download_file(42, dest)
            self.assertEqual(dest.read_bytes(), b"synthetic bytes")
            self.client.max_file_bytes = 3
            with self.assertRaises(ApiError):
                self.client.download_file(42, dest)
            self.assertEqual(dest.read_bytes(), b"synthetic bytes")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
        with self.assertRaises(ValueError):
            self.client.download_file("../synthetic", "/unused")

    def test_upload_share_voice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.ogg"
            path.write_bytes(b"synthetic audio")
            result = self.client.upload_and_share("room1", path, caption="Synthetic caption", voice=True)
        self.assertIn("messageId", result)
        self.assertEqual(self.server.messages["room1"][-1]["messageType"], "voice-message")
        req = self.server.requests[-1]
        self.assertEqual(req["data"]["shareType"], "10")
        self.assertIn("Synthetic caption", req["data"]["talkMetaData"])

    def test_share_metadata_extension(self) -> None:
        self.server.add_file("Talk/synthetic.bin", b"synthetic bytes")
        self.client._write("room1", "/ocs/v2.php/apps/files_sharing/api/v1/shares", data={
            "shareWith": "room1", "shareType": 10, "path": "Talk/synthetic.bin",
            "talkMetaData": '{"messageType":"comment"}', "replyTo": 1, "caption": "Synthetic caption"})
        req = self.server.requests[-1]
        self.assertNotIn("replyTo", req["data"])
        self.assertIn('"replyTo": 1', req["data"]["talkMetaData"])
        self.assertIn('"caption": "Synthetic caption"', req["data"]["talkMetaData"])

    def test_unknown_capability_fields(self) -> None:
        self.server.capabilities = {"capabilities": {"spreed": {"features": ["chat-v2", {}, None], "future": True}}}
        self.client.capabilities()
        self.assertEqual(self.client.features, {"chat-v2"})

    def test_login_no_auth(self) -> None:
        flow = login_flow_start(self.server.url)
        result = login_flow_poll(flow["poll"]["endpoint"], flow["poll"]["token"])
        self.assertEqual(result["loginName"], "alice")
        self.assertTrue(all(not r["has_auth"] for r in self.server.requests))

    def test_protocol_rejects_nonlists_and_invalid_entries(self) -> None:
        for data in ({}, None, "wrong", [None], [{}]):
            with self.subTest(data_type=type(data).__name__):
                self.server.respond_next(ROOM, data)
                with self.assertRaises(ApiError):
                    self.client.rooms()
                self.server.respond_next(CHAT + "/room1", data)
                with self.assertRaises(ApiError):
                    self.client.chat_history("room1")
        for mid in (0, True, 1.5, "+1", 2 ** 63):
            self.server.respond_next(CHAT + "/room1", [{"id": mid}])
            with self.assertRaises(ApiError):
                self.client.chat_history("room1")

    def test_protocol_requires_ocs_envelope_and_success_metadata(self) -> None:
        import json
        class Response(io.BytesIO):
            status = 200
            headers = {}
        for obj in ([], {"data": []}, {"ocs": {"data": []}},
                    {"ocs": {"meta": {"status": "ok", "statuscode": 200}}}):
            with patch.object(self.client, "_open", return_value=Response(json.dumps(obj).encode())):
                with self.assertRaises(ApiError):
                    self.client.rooms()

    def test_participants_rejects_unusable_actor_fields(self) -> None:
        self.server.respond_next(ROOM + "/room1/participants", [{"actorType": [], "actorId": "synthetic"}])
        with self.assertRaises(ApiError):
            self.client.participants("room1")

    def test_loopback_http_requires_explicit_environment(self) -> None:
        for enabled in ("", "0", "true"):
            with patch.dict(os.environ, {"OMANCTALK_ALLOW_INSECURE_LOCALHOST": enabled}):
                for server in (self.server.url, "http://localhost", "http://[::1]"):
                    with self.assertRaises(ValueError):
                        TalkClient(server, "synthetic", "synthetic")
                TalkClient("https://example.test", "synthetic", "synthetic")
        with patch.dict(os.environ, {"OMANCTALK_ALLOW_INSECURE_LOCALHOST": "1"}):
            TalkClient(self.server.url, "synthetic", "synthetic")
            with self.assertRaises(ValueError):
                TalkClient("http://example.test", "synthetic", "synthetic")

    def test_long_retry_after_and_http_date(self) -> None:
        from nctalk.api import _delay
        self.server.fail_next(429, ROOM, retry_after="300")
        self.client.rooms()
        self.assertEqual(self.delays, [300.0])
        import email.utils
        date = email.utils.formatdate(time.time() + 310, usegmt=True)
        self.assertGreater(_delay({"Retry-After": date}, 0), 300)
        self.assertEqual(_delay({"Retry-After": "999999"}, 0), 900)

    def test_mutation_connection_failure_is_not_sent(self) -> None:
        with patch.object(http.client.HTTPConnection, "connect", side_effect=OSError("synthetic")):
            with self.assertRaises(ApiError) as result:
                self.client.send_message("room1", "Synthetic")
        self.assertFalse(result.exception.request_sent)
        self.server.fail_next(500, CHAT)
        with self.assertRaises(ApiError) as result:
            self.client.send_message("room1", "Synthetic")
        self.assertTrue(result.exception.request_sent)

    def test_stop_after_connect_before_mutation_is_not_sent(self) -> None:
        event = threading.Event()
        self.client.cancelled = event.is_set
        original = http.client.HTTPConnection.connect
        def connect(connection):
            original(connection)
            event.set()
        with patch.object(http.client.HTTPConnection, "connect", connect):
            with self.assertRaises(ApiError) as result:
                self.client.send_message("room1", "Synthetic")
        self.assertFalse(result.exception.request_sent)
        self.assertEqual(self.server.requests, [])

    def test_download_cancellation_and_total_deadline_are_atomic(self) -> None:
        for limit in (False, True):
            clock = [0.0]
            stopped = [False]
            class Response(io.BytesIO):
                headers = {}
                def read1(self, size):
                    clock[0] += 1
                    stopped[0] = True
                    return super().read(size)
            self.client.cancelled = (lambda: False) if limit else lambda: stopped[0]
            self.client.transfer_timeout = 0.5
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "file"
                target.write_bytes(b"synthetic original")
                with patch.object(self.client, "_open", return_value=Response(b"synthetic replacement")), \
                     patch("nctalk.api.time.monotonic", side_effect=lambda: clock[0]):
                    with self.assertRaises(ApiError):
                        self.client.download_file("Talk/synthetic", target)
                self.assertEqual(target.read_bytes(), b"synthetic original")
                self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_upload_checks_cancellation_and_deadline_between_chunks(self) -> None:
        for limit in (False, True):
            stopped = [False]
            clock = [0.0]
            self.client.cancelled = (lambda: False) if limit else lambda: stopped[0]
            self.client.transfer_timeout = 0.5
            @contextmanager
            def opened(path, method="GET", data=None, **kwargs):
                if method == "PUT":
                    self.assertTrue(data.read(1024))
                    clock[0] += 1
                    stopped[0] = True
                    data.read(1024)
                yield io.BytesIO()
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "synthetic.bin"
                source.write_bytes(b"x" * 4096)
                with patch.object(self.client, "_open", side_effect=opened), \
                     patch("nctalk.api.time.monotonic", side_effect=lambda: clock[0]):
                    with self.assertRaises(ApiError):
                        self.client.upload_and_share("room1", source)
        self.assertFalse(any(r["method"] == "POST" for r in self.server.requests))

    def test_transfer_deadline_includes_retry_wait(self) -> None:
        clock = [0.0]
        self.client.transfer_timeout = 5
        self.client.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        remote = "/remote.php/dav/files/alice/Talk/synthetic"
        self.server.fail_next(429, remote, retry_after="300")
        with tempfile.TemporaryDirectory() as tmp, \
             patch("nctalk.api.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ApiError):
                self.client.download_file("Talk/synthetic", Path(tmp) / "file")
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(clock[0], 5)
        self.assertEqual(sum(r["path"] == remote for r in self.server.requests), 1)
        self.assertIsNone(self.client._transfer_deadline)

    def test_download_rejects_linked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside").mkdir()
            (root / "linked").symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(ApiError):
                self.client.download_file("Talk/synthetic", root / "linked/file")
            self.assertEqual(list((root / "outside").iterdir()), [])

    def test_server_validation(self) -> None:
        for url in ("http://example.com", "https://user:password@example.com", "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                TalkClient(url, "synthetic", "synthetic")


if __name__ == "__main__":
    unittest.main()
