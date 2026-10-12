"""Offline, synthetic write-path integration tests against FakeTalkServer."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import actions, api, prefs, store
from fake_talk import FakeTalkServer


class ActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = FakeTalkServer()
        self.addCleanup(self.server.close)
        room = self.server.add_room("room1")
        self.server.add_room("room2", name="omanctalk-test-target")
        self.server.add_room("blocked", name="Synthetic protected room")
        msg = self.server.add_message("room1")
        self.mid = str(msg["id"])
        self.identity = prefs.account_id(self.server.url, self.server.user)
        self.account = {"account": self.identity, "server": self.server.url, "user": self.server.user,
                        "app_password": self.server.password, "write_guard_prefix": "omanctalk-test-"}
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [self.account]})
        self.db = prefs.store_dir(self.identity) / "talk.db"
        with closing(store.connect(self.db)) as conn:
            store.upsert_room(conn, room)
            store.upsert_room(conn, self.server.rooms["blocked"])
            store.upsert_message(conn, "room1", msg)
            store.upsert_member(conn, "room1", {"actorType": "users", "actorId": "bob", "displayName": "Synthetic Bob"})
            store.upsert_member(conn, "room1", {"actorType": "users", "actorId": "bobby", "displayName": "Synthetic Bob Long"})
            store.upsert_member(conn, "room1", {"actorType": "users", "actorId": "odd user", "displayName": "Odd Person"})

    def call(self, command: str, **payload: object) -> dict:
        return actions.run(command, [], {"account": self.identity, "jid": "room1", **payload})

    def file(self, name: str = "synthetic.txt", data: bytes = b"Synthetic fixture") -> Path:
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        return path

    def outbox(self) -> list:
        with closing(store.connect_ro(self.db)) as conn:
            return conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def test_send_reply_reference_and_mirror_unchanged(self) -> None:
        result = self.call("send", text="Synthetic reply", reply_id=self.mid, local_id="local-1")
        self.assertTrue(result["ok"])
        msg = self.server.message("room1", int(result["message_id"]))
        self.assertEqual(msg["parent"]["id"], int(self.mid))
        self.assertEqual(msg["referenceId"], "local-1")
        self.assertEqual(len(actions.rows(self.db, "SELECT id FROM messages")), 1)

    def test_online_send_repetition_returns_stored_result(self) -> None:
        first = self.call("send", text="Synthetic once", local_id="synthetic-once")
        before = len(self.server.requests)
        second = self.call("send", text="Synthetic once", local_id="synthetic-once")
        self.assertEqual(second, first)
        self.assertTrue(first["message_id"])
        self.assertEqual(len(self.server.requests), before)
        self.assertEqual(self.outbox()[0]["state"], "done")
        self.assertEqual(json.loads(self.outbox()[0]["payload"])["_result"]["message_id"], first["message_id"])
        changed = self.call("send", text="Synthetic changed", local_id="synthetic-once")
        self.assertFalse(changed["ok"])
        self.assertIn("another action", changed["error"])
        self.assertEqual(len(self.server.requests), before)

    def test_concurrent_helpers_reserve_before_network(self) -> None:
        started, release = threading.Event(), threading.Event()
        original = actions.ShareClient.send_message
        results = []
        def send(client, *args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("synthetic timeout")
            return original(client, *args, **kwargs)
        with patch.object(actions.ShareClient, "send_message", send):
            worker = threading.Thread(target=lambda: results.append(
                self.call("send", text="Synthetic concurrent", local_id="synthetic-race")))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.outbox()[0]["state"], "sending")
                from nctalk.sync import SyncDaemon
                client = api.TalkClient(self.server.url, self.server.user, self.server.password)
                with SyncDaemon(self.identity, client, self.db.parent):
                    self.assertEqual(self.outbox()[0]["state"], "sending")
                repeated = self.call("send", text="Synthetic concurrent", local_id="synthetic-race")
                self.assertFalse(repeated["ok"])
                self.assertIn("in progress", repeated["error"])
                self.assertEqual(self.server.requests, [])
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["ok"])
        self.assertEqual(sum(r["method"] == "POST" for r in self.server.requests), 1)
        self.assertEqual(len(self.outbox()), 1)

    def test_ambiguous_send_is_failed_and_never_automatically_replayed(self) -> None:
        self.server.fail_next(503, api.CHAT)
        first = self.call("send", text="Synthetic ambiguous", local_id="synthetic-ambiguous")
        self.assertFalse(first["ok"])
        self.assertIn("verify", first["error"])
        self.assertEqual(self.outbox()[0]["state"], "failed")
        before = len(self.server.requests)
        second = self.call("send", text="Synthetic ambiguous", local_id="synthetic-ambiguous")
        self.assertFalse(second["ok"])
        self.assertEqual(len(self.server.requests), before)
        self.assertEqual(json.loads(self.outbox()[0]["payload"])["reference_id"], "synthetic-ambiguous")

    def test_post_connection_loss_is_not_queued(self) -> None:
        with patch.object(actions.ShareClient, "send_message", side_effect=api.ApiError(
                0, "network request failed", request_sent=True)):
            self.assertFalse(self.call("send", text="Synthetic", local_id="synthetic-lost")["ok"])
        self.assertEqual(self.outbox()[0]["state"], "failed")

    def test_queued_send_result_persisted_by_daemon(self) -> None:
        from nctalk.sync import SyncDaemon
        self.server.fail_next(503, api.ROOM)
        request = {"text": "Synthetic queued result", "local_id": "synthetic-queued-result"}
        self.assertTrue(self.call("send", **request)["queued"])
        client = api.TalkClient(self.server.url, self.server.user, self.server.password, "omanctalk-test-")
        with SyncDaemon(self.identity, client, self.db.parent) as daemon:
            daemon.process_outbox()
        before = len(self.server.requests)
        result = self.call("send", **request)
        self.assertTrue(result["ok"])
        self.assertFalse(result["queued"])
        self.assertTrue(result["message_id"])
        self.assertEqual(len(self.server.requests), before)

    def test_files_and_send_new_are_idempotent(self) -> None:
        path = self.file()
        request = {"paths": [str(path)], "local_id": "synthetic-file-once"}
        first = self.call("files", **request)
        self.assertTrue(first["ok"])
        before = len(self.server.requests)
        self.assertEqual(self.call("files", **request), first)
        self.assertEqual(len(self.server.requests), before)
        self.account["write_guard_prefix"] = None
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [self.account]})
        request = {"target": {"user": "bob"}, "text": "Synthetic new once", "local_id": "synthetic-new-once"}
        first = self.call("send-new", **request)
        self.assertTrue(first["ok"])
        before = len(self.server.requests)
        self.assertEqual(self.call("send-new", **request), first)
        self.assertEqual(len(self.server.requests), before)

    def test_mentions_longest_only_requested_and_quoted_identity(self) -> None:
        result = self.call("send", text="@Synthetic Bob Long, @Synthetic Bob and @Odd Person; @Other.",
                           mentions=["users/bob", "users/bobby", "users/odd user"])
        self.assertTrue(result["ok"])
        msg = self.server.message("room1", int(result["message_id"]))
        self.assertEqual(msg["message"], '@bobby, @bob and @"odd user"; @Other.')

    def test_unknown_mention_is_rejected(self) -> None:
        self.assertFalse(self.call("send", text="Synthetic", mentions=["users/missing"])["ok"])
        self.assertEqual(len(self.server.messages["room1"]), 1)

    def test_mention_does_not_match_word_prefix(self) -> None:
        result = self.call("send", text="@Synthetic Bobson", mentions=["users/bob"])
        self.assertEqual(self.server.message("room1", int(result["message_id"]))["message"], "@Synthetic Bobson")

    def test_send_new_room_and_existing_token(self) -> None:
        self.account["write_guard_prefix"] = None
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [self.account]})
        result = self.call("send-new", target={"user": "bob"}, text="Synthetic first message")
        self.assertTrue(result["ok"])
        self.assertEqual(result["jid"], result["chat_jid"])
        existing = self.call("send-new", target={"jid": "room2"}, text="Synthetic existing target")
        self.assertEqual(existing["chat_jid"], "room2")

    def test_guard_blocks_send_new_and_mutations(self) -> None:
        for command, payload in [("send", {"text": "Synthetic"}), ("edit", {"id": self.mid, "text": "Synthetic"}),
                                 ("delete", {"id": self.mid}), ("react", {"id": self.mid, "emoji": ""})]:
            with self.subTest(command=command):
                result = self.call(command, jid="blocked", **payload)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "write blocked by dev guard")
        self.assertFalse(self.call("send-new", target={"user": "bob"}, text="Synthetic")["ok"])
        self.assertTrue(all(r["state"] == "failed" for r in self.outbox()))

    def test_offline_send_queues_normalized_once_and_replays(self) -> None:
        self.server.fail_next(503, api.ROOM)
        for _ in range(2):
            result = self.call("send", text="@Synthetic Bob", mentions=["users/bob"], local_id="offline-1")
            self.assertTrue(result["queued"])
        entries = self.outbox()
        self.assertEqual(len(entries), 1)
        payload = json.loads(entries[0]["payload"])
        self.assertEqual(payload["text"], "@bob")
        self.assertEqual(payload["reference_id"], "offline-1")
        client = api.TalkClient(self.server.url, self.server.user, self.server.password, "omanctalk-test-")
        result = actions.replay(client, entries[0]["kind"], payload)
        self.assertEqual(result["referenceId"], "offline-1")

    def test_offline_edit_delete_react(self) -> None:
        for command, path, extra in [("edit", api.CHAT, {"text": "Synthetic edit"}),
                                      ("delete", api.CHAT, {}), ("react", api.REACTION, {"emoji": "👍"})]:
            self.server.fail_next(503, api.ROOM)
            self.assertTrue(self.call(command, id=self.mid, **extra)["queued"])
        self.assertEqual([r["kind"] for r in self.outbox()], ["edit", "delete", "react"])

    def test_offline_guard_needs_cached_safe_room(self) -> None:
        for jid in ("blocked", "missing"):
            self.server.fail_next(503, api.ROOM)
            result = self.call("send", jid=jid, text="Synthetic", local_id="guard-" + jid)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "write blocked by dev guard")
        self.assertTrue(all(r["state"] == "failed" for r in self.outbox()))

    def test_offline_guard_does_not_allow_renamed_room(self) -> None:
        self.server.rooms["room1"]["name"] = "Synthetic no longer test"
        result = self.call("send", text="Synthetic")
        self.assertFalse(result["ok"])
        self.assertNotIn("queued", result)

    def test_queued_retry_online_never_bypasses_daemon_and_done_is_idempotent(self) -> None:
        self.server.fail_next(503, api.ROOM)
        self.assertTrue(self.call("send", text="Synthetic queued", local_id="queued-id")["queued"])
        before = len(self.server.requests)
        self.assertTrue(self.call("send", text="Synthetic queued", local_id="queued-id")["queued"])
        self.assertEqual(len(self.server.requests), before)
        with closing(actions.rw(self.db)) as conn, conn:
            conn.execute("UPDATE outbox SET state='done' WHERE local_id='queued-id'")
        self.assertFalse(self.call("send", text="Synthetic queued", local_id="queued-id")["queued"])
        self.assertEqual(len(self.server.requests), before)

    def test_local_id_conflict_refused(self) -> None:
        for text in ("Synthetic first", "Synthetic changed"):
            self.server.fail_next(503, api.CHAT)
            result = self.call("send", text=text, local_id="same-id")
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.outbox()), 1)

    def test_auth_and_rate_limit_do_not_queue(self) -> None:
        for code in (401, 403, 429, 400):
            self.server.fail_next(code, api.CHAT)
            self.assertFalse(self.call("send", text="Synthetic")["ok"])
        self.assertTrue(all(r["state"] == "failed" for r in self.outbox()))

    def test_network_failure_queues(self) -> None:
        with patch.object(actions.ShareClient, "_open", side_effect=api.ApiError(0, "network request failed")):
            self.assertTrue(self.call("send", text="Synthetic")["queued"])

    def test_edit_delete_and_reactions(self) -> None:
        self.assertTrue(self.call("edit", id=self.mid, text="Synthetic changed")["ok"])
        self.assertTrue(self.call("react", id=self.mid, emoji="👍")["ok"])
        self.assertTrue(self.call("react", id=self.mid, emoji="❤")["ok"])
        self.assertTrue(self.call("react", id=self.mid, emoji="")["ok"])
        self.assertEqual(self.server.reactions[("room1", int(self.mid))], {})
        self.assertTrue(self.call("delete", id=self.mid, for_me=True)["ok"])
        self.assertTrue(self.server.message("room1", int(self.mid))["deleted"])

    def test_offline_remove_freezes_own_reactions(self) -> None:
        self.server.react("room1", int(self.mid), "👍")
        msg = self.server.message("room1", int(self.mid))
        with closing(store.connect(self.db)) as conn:
            store.upsert_message(conn, "room1", msg)
        self.server.fail_next(503, api.REACTION)
        result = self.call("react", id=self.mid, emoji="", local_id="remove-1")
        self.assertTrue(result["queued"])
        payload = json.loads(self.outbox()[0]["payload"])
        self.assertEqual(payload["remove_emojis"], ["👍"])
        client = api.TalkClient(self.server.url, self.server.user, self.server.password, "omanctalk-test-")
        actions.replay(client, "react", payload)
        self.assertEqual(self.server.reactions[("room1", int(self.mid))], {})

    def test_star_idempotent_and_local_only(self) -> None:
        before = len(self.server.requests)
        for _ in range(2):
            self.assertTrue(self.call("star", id=self.mid, starred=True)["starred"])
        self.assertEqual(len(actions.rows(self.db, "SELECT * FROM stars")), 1)
        self.assertEqual(len(self.server.requests), before)
        self.assertTrue(self.call("star", id=self.mid, starred=False)["ok"])
        self.assertEqual(actions.rows(self.db, "SELECT * FROM stars"), [])

    def test_files_album_caption_reply_and_multiple_uploads(self) -> None:
        paths = [self.file("synthetic-1.png"), self.file("synthetic-2.png")]
        result = self.call("files", paths=[str(p) for p in paths], caption="Synthetic caption", reply_id=self.mid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["album_id"])
        self.assertEqual([i["album_index"] for i in result["items"]], [0, 1])
        shares = [r for r in self.server.requests if r["path"] == actions.SHARES]
        first = json.loads(shares[0]["data"]["talkMetaData"])
        second = json.loads(shares[1]["data"]["talkMetaData"])
        self.assertEqual(first["caption"], "Synthetic caption")
        self.assertEqual(first["replyTo"], int(self.mid))
        self.assertEqual(second["caption"], "")
        self.assertNotIn("replyTo", second)

    def test_share_without_chat_id_resolves_file_source_once(self) -> None:
        path = self.file()
        original = actions.ShareClient.upload
        def upload(client, jid, *args, **kwargs):
            response = original(client, jid, *args, **kwargs)
            msg = self.server.message(jid, int(response["messageId"]))
            return {"file_source": msg["messageParameters"]["file"]["id"]}
        with patch.object(actions.ShareClient, "upload", upload):
            result = self.call("files", paths=[str(path)])
        self.assertTrue(result["ok"])
        self.assertTrue(result["items"][0]["id"])
        self.assertEqual(sum(r["method"] == "GET" and r["path"].startswith(api.CHAT) for r in self.server.requests), 1)

    def test_share_read_failure_does_not_make_confirmed_delivery_retryable(self) -> None:
        path = self.file()
        original = actions.ShareClient.upload
        def upload(client, jid, *args, **kwargs):
            original(client, jid, *args, **kwargs)
            self.server.fail_next(503, api.CHAT)
            return {"file_source": 1}
        with patch.object(actions.ShareClient, "upload", upload):
            result = self.call("files", paths=[str(path)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["id"], "")
        self.assertNotIn("partial", result)

    def test_files_partial_preserves_exact_ui_paths(self) -> None:
        paths = [self.file("partial-1.txt").as_uri(), str(self.file("partial-2.txt"))]
        original = actions.ShareClient.upload
        count = 0
        def upload(client, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise api.ApiError(503, "server unavailable")
            return original(client, *args, **kwargs)
        with patch.object(actions.ShareClient, "upload", upload):
            result = self.call("files", paths=paths)
        self.assertFalse(result["ok"])
        self.assertEqual(result["partial"]["sent_count"], 1)
        self.assertEqual(result["partial"]["sent_paths"], paths[:1])
        self.assertEqual(result["partial"]["remaining_paths"], paths[1:])
        self.assertEqual([r["state"] for r in self.outbox()], ["failed"])

    def test_partial_guard_error_is_not_hidden_as_queued(self) -> None:
        paths = [str(self.file("guard-1.txt")), str(self.file("guard-2.txt"))]
        original = actions.ShareClient.upload
        count = 0
        def upload(client, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise api.GuardError()
            return original(client, *args, **kwargs)
        with patch.object(actions.ShareClient, "upload", upload):
            result = self.call("files", paths=paths)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "write blocked by dev guard")
        self.assertEqual(result["partial"]["sent_count"], 1)
        self.assertNotIn("queued", result)

    def test_snapshot_rejects_linked_parent_even_after_validation(self) -> None:
        path = self.file()
        link = path.parent / "synthetic-linked-parent"
        link.symlink_to(path.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            with actions.snapshot(link / path.name):
                pass

    def test_files_prevalidate_all_and_guard_upload(self) -> None:
        good = self.file()
        self.assertFalse(self.call("files", paths=[str(good), str(good.parent / "missing")])["ok"])
        self.assertEqual(self.server.files, {})
        result = self.call("files", jid="blocked", paths=[str(good)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "write blocked by dev guard")
        self.assertEqual(self.server.files, {})

    def test_file_symlinks_directories_relative_remote_urls_and_size(self) -> None:
        path = self.file()
        link = path.with_name("link")
        link.symlink_to(path)
        directory_link = path.with_name("dir-link")
        directory_link.symlink_to(path.parent, target_is_directory=True)
        for value in (str(link), str(directory_link / path.name), str(path.parent), "relative.txt", "file://remote/tmp/file"):
            with self.subTest(value=value):
                self.assertFalse(self.call("files", paths=[value])["ok"])
        with patch.object(actions, "MAX_FILE", 2):
            self.assertFalse(self.call("files", paths=[str(path)])["ok"])

    def draft(self, content: bytes = b"OggS synthetic OpusHead audio") -> Path:
        root = prefs.state_dir() / "voice-drafts"
        root.mkdir(parents=True)
        path = root / "omanctalk-synthetic.ogg"
        path.write_bytes(content)
        return path

    def test_voice_upload_metadata_and_cleanup(self) -> None:
        path = self.draft()
        result = self.call("voice", path=str(path), reply_id=self.mid)
        self.assertTrue(result["ok"])
        self.assertTrue(result["draft_cleaned"])
        self.assertFalse(path.exists())
        share = next(r for r in self.server.requests if r["path"] == actions.SHARES)
        self.assertEqual(json.loads(share["data"]["talkMetaData"])["messageType"], "voice-message")

    def test_voice_failed_send_keeps_draft(self) -> None:
        path = self.draft()
        self.server.fail_next(503, actions.SHARES)
        self.assertFalse(self.call("voice", path=str(path))["ok"])
        self.assertTrue(path.exists())

    def test_voice_checks_private_empty_link_and_format(self) -> None:
        external = self.file("external.ogg", b"OggS OpusHead")
        self.assertFalse(self.call("voice", path=str(external))["ok"])
        path = self.draft(b"")
        self.assertFalse(self.call("voice", path=str(path))["ok"])
        path.write_bytes(b"invalid synthetic audio")
        with patch.object(actions.shutil, "which", return_value=None):
            self.assertFalse(self.call("voice", path=str(path))["ok"])
        hardlink = path.with_name("omanctalk-other.ogg")
        os.link(path, hardlink)
        self.assertFalse(self.call("voice", path=str(path))["ok"])

    def test_voice_optional_conversion(self) -> None:
        path = self.draft(b"RIFF0000WAVE synthetic audio")
        def convert(args, **kwargs):
            Path(args[-1]).write_bytes(b"OggS synthetic converted OpusHead")
            self.assertIsInstance(args, list)
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(actions.shutil, "which", return_value="/synthetic/ffmpeg"), patch.object(actions.subprocess, "run", side_effect=convert):
            self.assertTrue(self.call("voice", path=str(path))["ok"])

    def add_media(self, name: str = "synthetic.txt") -> int:
        fid = self.server.add_file("Talk/synthetic.txt", b"Synthetic download bytes")
        msg = self.server.add_message("room1", "{file}", messageParameters={"file": {
            "type": "file", "id": fid, "name": name, "path": "Talk/synthetic.txt", "size": 24, "mimetype": "text/plain"}})
        with closing(store.connect(self.db)) as conn:
            store.upsert_message(conn, "room1", msg)
        return msg["id"]

    def test_media_download_cache_and_path_update(self) -> None:
        mid = self.add_media("../../unsafe/synthetic.txt")
        result = self.call("media", id=str(mid))
        self.assertTrue(result["ok"])
        path = Path(result["local_path"])
        self.assertTrue(path.is_relative_to(self.db.parent / "media/room1"))
        self.assertEqual(path.read_bytes(), b"Synthetic download bytes")
        before = len(self.server.requests)
        self.assertEqual(self.call("media", id=str(mid)), result)
        self.assertEqual(len(self.server.requests), before)
        self.assertEqual(actions.cached_message(self.db, "room1", mid)["local_path"], str(path))

    def test_media_rejects_symlink_target_and_oversize(self) -> None:
        mid = self.add_media()
        media_root = self.db.parent / "media"
        media_root.symlink_to(Path(self.temp.name), target_is_directory=True)
        self.assertFalse(self.call("media", id=str(mid))["ok"])
        media_root.unlink()
        with patch.object(actions, "MAX_FILE", 2):
            self.assertFalse(self.call("media", id=str(mid))["ok"])
        self.assertFalse(self.call("media", id=self.mid)["ok"])

    def test_download_pending_bounded(self) -> None:
        self.add_media()
        result = self.call("download-pending", limit=1)
        self.assertEqual(result["downloaded"], 1)
        self.assertFalse(self.call("download-pending", limit=1000)["ok"])

    def test_forward_text_and_file_reference(self) -> None:
        result = self.call("forward", id=self.mid, to_jid="room2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["target_jid"], "room2")
        mid = self.add_media()
        result = self.call("forward", id=str(mid), to_jid="room2")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.server.files), 1)
        self.assertFalse(self.call("forward", id=self.mid, to_jid="blocked")["ok"])

    def test_groups_join_info_and_supported_actions(self) -> None:
        result = self.call("create-group", name="omanctalk-test-created", participants=["users/bob"])
        self.assertTrue(result["ok"])
        jid = result["jid"]
        self.assertTrue(self.call("join-group", invite=self.server.url + "/call/" + jid)["ok"])
        for action, value in [("description", "Synthetic description"), ("rename", "omanctalk-test-renamed"),
                              ("add", ["users/carol"]), ("promote", "users/bob"), ("demote", "users/bob"), ("remove", "users/bob")]:
            self.assertTrue(self.call("group-action", jid=jid, action=action, value=value)["ok"])
        info = self.call("group-info", jid=jid)
        self.assertEqual(info["name"], "omanctalk-test-renamed")
        self.assertEqual(info["description"], "Synthetic description")
        self.assertEqual(info["my_role"], "owner")
        self.assertFalse(self.call("group-action", jid=jid, action="announce", value=True)["supported"])
        self.assertTrue(self.call("group-action", jid=jid, action="leave")["ok"])

    def test_invalid_invite_group_and_account_scope(self) -> None:
        self.assertFalse(self.call("create-group", name="not-test", participants=[])["ok"])
        self.assertFalse(self.call("join-group", invite="https://other.invalid/call/room1")["ok"])
        self.assertFalse(self.call("join-group", invite="../room1")["ok"])
        self.assertFalse(actions.run("send", ["--account", "another"], {"account": self.identity, "jid": "room1", "text": "Synthetic"})["ok"])
        self.assertFalse(self.call("send", text="")["ok"])
        self.assertFalse(self.call("edit", id="../2", text="Synthetic")["ok"])

    def test_contacts_online_offline_and_profiles(self) -> None:
        online = self.call("contacts-search", query="Synthetic")
        self.assertEqual(online["people"][0]["jid"], "users/bob")
        self.server.fail_next(503, "/ocs/v2.php/core/autocomplete")
        offline = self.call("contacts-search", query="Synthetic Bob")
        self.assertEqual(len(offline["people"]), 2)
        self.assertTrue(all(p["phone"] == "" for p in offline["people"]))
        self.assertEqual(self.call("contact-profile", person="users/bob")["about"], "")

    def test_unsupported_commands_and_check_number(self) -> None:
        for command in actions.UNSUPPORTED:
            result = self.call(command)
            self.assertFalse(result["ok"])
            self.assertFalse(result["supported"])
        result = self.call("check-number", phone="synthetic-number")
        self.assertFalse(result["registered"])
        self.assertFalse(result["supported"])

    def test_errors_never_echo_content_or_password(self) -> None:
        self.server.fail_next(403, api.CHAT)
        result = self.call("send", text="Synthetic confidential fixture")
        serialized = json.dumps(result)
        self.assertNotIn("Synthetic confidential", serialized)
        self.assertNotIn(self.server.password, serialized)


if __name__ == "__main__":
    unittest.main()
