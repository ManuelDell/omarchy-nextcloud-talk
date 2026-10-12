from __future__ import annotations

import json
from contextlib import closing
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import store
from nctalk.api import ApiError, CHAT, ROOM, TalkClient
from nctalk.sync import SyncDaemon, accounts_file, load_account, store_dir
from fake_talk import FakeTalkServer

ROOT = Path(__file__).resolve().parents[1]


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeTalkServer()
        self.addCleanup(self.server.close)
        self.server.add_room("room1")
        self.client = TalkClient(self.server.url, self.server.user, self.server.password)
        self.delays: list[float] = []
        self.client.sleep = self.delays.append
        self.directory = Path(self.tmp.name) / "store"
        self.daemon = SyncDaemon("alice@test", self.client, self.directory)
        self.addCleanup(lambda: self.daemon.close())

    def row(self, mid: int) -> dict:
        return dict(self.daemon.conn.execute("SELECT * FROM messages WHERE token='room1' AND id=?", (mid,)).fetchone())

    def state(self) -> dict:
        return dict(self.daemon.conn.execute("SELECT * FROM sync_state WHERE token='room1'").fetchone())

    def sync(self, budget: float = 0) -> dict[str, int]:
        return self.daemon.run_once(backfill_budget=budget)

    def test_first_sync_metadata_members_avatars_permissions(self) -> None:
        msg = self.server.add_message("room1", "Synthetic initial")
        counts = self.sync(1)
        self.assertEqual(counts["messages"], 1)
        self.assertEqual(counts["members"], 1)
        self.assertEqual(self.row(msg["id"])["message"], "Synthetic initial")
        self.assertEqual(self.state()["history_complete"], 1)
        self.assertTrue(store.meta_get(self.daemon.conn, "capabilities_json"))
        self.assertTrue(store.meta_get(self.daemon.conn, "last_sync_ok_ts"))
        avatar = self.daemon.conn.execute("SELECT * FROM avatars").fetchone()
        self.assertTrue(Path(avatar["path"]).is_file())
        self.assertEqual((self.directory / "talk.db").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(r["method"] == "GET" for r in self.server.requests))

    def test_incremental_and_idempotent(self) -> None:
        self.server.add_message("room1", "Synthetic first")
        self.sync(1)
        before = store.meta_get(self.daemon.conn, "rooms_modified_before")
        msg = self.server.add_message("room1", "Synthetic second")
        self.sync()
        self.sync()
        self.assertEqual(self.daemon.counts()["messages"], 2)
        self.assertEqual(self.row(msg["id"])["message"], "Synthetic second")
        poll = [r for r in self.server.requests if r["path"] == ROOM][1]
        self.assertEqual(poll["query"]["modifiedSince"], before)
        future = [r for r in self.server.requests if r["path"].startswith(CHAT) and r["query"].get("lookIntoFuture") == "1"]
        self.assertEqual(len(future), 2)  # data page then 304
        self.assertEqual(self.state()["newest_id"], msg["id"])

    def test_edit_delete_reaction_and_expiration(self) -> None:
        a = self.server.add_message("room1", "Synthetic old")
        b = self.server.add_message("room1", "Synthetic expiry")
        self.sync(1)
        self.server.edit_message("room1", a["id"], "Synthetic edited")
        self.server.react("room1", a["id"], "🍀")
        self.sync()
        self.assertEqual(self.row(a["id"])["message"], "Synthetic edited")
        self.assertGreater(self.row(a["id"])["edited_ts"], 0)
        self.assertEqual(json.loads(self.row(a["id"])["reactions_json"]), {"🍀": 1})
        self.assertEqual(self.daemon.counts()["reactions"], 1)
        self.server.react("room1", a["id"], "🍀", remove=True)
        self.sync()
        self.assertEqual(self.daemon.counts()["reactions"], 0)
        self.server.delete_message("room1", a["id"])
        self.server.delete_message("room1", b["id"], expired=True)
        self.sync()
        self.assertEqual(self.row(a["id"])["is_deleted"], 1)
        self.assertEqual(self.row(b["id"])["is_deleted"], 1)
        self.assertEqual(self.row(a["id"])["search_text"], "")
        systems = self.daemon.conn.execute("SELECT count(*) FROM messages WHERE message_type='system'").fetchone()[0]
        self.assertEqual(systems, 5)

    def test_changes_to_last_message_are_fetched_without_room_activity(self) -> None:
        # Real Talk keeps lastMessage on the edited/deleted parent and does NOT raise
        # lastActivity for edits, reactions or deletions: only scheduled polling sees them.
        import copy
        last = self.server.add_message("room1", "Synthetic last")
        self.sync(1)
        activity = self.server.rooms["room1"]["lastActivity"]

        def like_real_talk() -> None:
            self.server.rooms["room1"]["lastMessage"] = copy.deepcopy(self.server.message("room1", last["id"]))
            self.server.rooms["room1"]["lastActivity"] = activity
            self.daemon._next_poll.clear()  # the poll interval has elapsed

        self.server.edit_message("room1", last["id"], "Synthetic edited")
        like_real_talk()
        self.sync()
        self.assertEqual(self.row(last["id"])["message"], "Synthetic edited")
        self.server.react("room1", last["id"], "🍀")
        like_real_talk()
        self.sync()
        self.assertEqual(json.loads(self.row(last["id"])["reactions_json"]), {"🍀": 1})
        self.server.delete_message("room1", last["id"])
        like_real_talk()
        self.sync()
        self.assertEqual(self.row(last["id"])["is_deleted"], 1)

    def test_scheduled_poll_fires_even_when_cycles_are_shorter_than_the_interval(self) -> None:
        import copy
        last = self.server.add_message("room1", "Synthetic last")
        self.sync(1)
        activity = self.server.rooms["room1"]["lastActivity"]
        clock = [1000.0]
        self.daemon._next_poll.clear()  # schedule entries from the first sync use the real clock
        # The fake server's lastActivity is a tiny counter, i.e. "ancient": pin the intervals.
        with patch("nctalk.sync.time.monotonic", side_effect=lambda: clock[0]), \
                patch.multiple("nctalk.sync", POLL_WARM_SECONDS=3, POLL_COLD_SECONDS=3):
            clock[0] += 1.0
            self.sync()  # quiet cycle: polls once, so the next poll is due 3 s later
            self.server.edit_message("room1", last["id"], "Synthetic edited")
            self.server.rooms["room1"]["lastMessage"] = copy.deepcopy(self.server.message("room1", last["id"]))
            self.server.rooms["room1"]["lastActivity"] = activity
            seen_after = None
            for cycle in range(1, 9):
                clock[0] += 1.0  # cycles are 1 s, the hot interval is 3 s
                self.sync()
                if self.row(last["id"])["message"] == "Synthetic edited":
                    seen_after = cycle
                    break
        self.assertIsNotNone(seen_after)
        self.assertLessEqual(seen_after, 4)

    def test_poll_schedule_follows_room_activity(self) -> None:
        now = store.now()
        for age, expected in ((60, 3), (2 * 86400, 15), (30 * 86400, 120)):
            room = {"last_activity": now - age}
            self.assertEqual(self.daemon._poll_interval(room), expected)  # type: ignore[arg-type]

    def test_deletion_suppresses_older_reaction_details(self) -> None:
        msg = self.server.add_message("room1", "Synthetic removed")
        self.server.react("room1", msg["id"], "🍀")
        self.server.delete_message("room1", msg["id"])
        self.sync()
        self.assertEqual(self.row(msg["id"])["is_deleted"], 1)
        self.assertEqual(self.daemon.counts()["reactions"], 0)

    def test_backfill_resume_restart(self) -> None:
        self.server.page_size = 2
        for i in range(9):
            self.server.add_message("room1", f"Synthetic history {i}")
        self.sync()
        self.assertEqual(self.state()["oldest_id"], 8)
        self.assertEqual(self.state()["history_complete"], 0)
        self.daemon.backfill_step()
        self.assertEqual(self.state()["oldest_id"], 6)
        self.daemon.close()
        self.daemon = SyncDaemon("alice@test", self.client, self.directory)
        self.sync(3)
        self.assertEqual(self.daemon.counts()["messages"], 9)
        self.assertEqual(self.state()["history_complete"], 1)
        polls = [r for r in self.server.requests if r["path"] == ROOM]
        self.assertNotIn("modifiedSince", polls[-1]["query"])

    def test_backfill_cannot_undo_newer_edit(self) -> None:
        msg = self.server.add_message("room1", "Synthetic original")
        self.server.edit_message("room1", msg["id"], "Synthetic earlier edit")
        self.server.edit_message("room1", msg["id"], "Synthetic final edit")
        self.server.page_size = 1
        self.sync()
        self.assertEqual(self.row(msg["id"])["message"], "Synthetic final edit")
        self.sync(2)
        self.assertEqual(self.row(msg["id"])["message"], "Synthetic final edit")
        self.assertEqual(self.state()["history_complete"], 1)

    def test_304_and_empty_room(self) -> None:
        self.sync()
        self.assertEqual(self.state()["history_complete"], 1)
        self.assertEqual(self.daemon.counts()["messages"], 0)
        self.server.add_message("room1", "Synthetic after empty")
        self.sync()
        self.assertEqual(self.daemon.counts()["messages"], 1)
        chats = [r for r in self.server.requests if r["path"].startswith(CHAT)]
        self.assertTrue(all(r["query"]["setReadMarker"] == "0" for r in chats))

    def test_empty_then_filled_room_reopens_backfill(self) -> None:
        self.sync()
        self.assertEqual(self.state()["history_complete"], 1)
        for _ in range(450):
            self.server.add_message("room1", "Synthetic history")
        self.sync()
        self.assertEqual(self.state()["history_complete"], 0)
        self.assertEqual(self.daemon.counts()["messages"], 200)
        self.sync(2)
        self.assertEqual(self.daemon.counts()["messages"], 450)
        self.assertEqual(self.state()["oldest_id"], 1)
        self.assertEqual(self.state()["history_complete"], 1)

    def test_invalid_room_response_never_removes_local_rooms(self) -> None:
        self.sync()
        for data in ({}, None, [{}], [None]):
            self.daemon._last_full = 0
            self.server.respond_next(ROOM, data)
            with self.assertRaises(ApiError):
                self.sync()
            self.assertEqual(self.daemon.conn.execute("SELECT removed FROM rooms").fetchone()[0], 0)

    def test_invalid_history_never_completes_or_advances(self) -> None:
        self.server.add_message("room1", "Synthetic")
        self.sync()
        before = self.state()
        for data in ({}, None, [{"id": 0}], [None]):
            self.server.respond_next(CHAT + "/room1", data)
            with self.assertRaises(ApiError):
                self.daemon.backfill_step()
            self.assertEqual(self.state(), before)
        # Invalid first pages must not set completion either.
        self.daemon.conn.execute("DELETE FROM sync_state")
        self.server.respond_next(CHAT + "/room1", None)
        with self.assertRaises(ApiError):
            self.sync()
        self.assertEqual(self.daemon._state("room1")["history_complete"], 0)

    def test_backfill_backoff_independent_of_successful_polls(self) -> None:
        clock = [0.0]
        polls, history = [], []
        def poll(**kwargs):
            polls.append(clock[0])
        def backfill():
            history.append(clock[0])
            raise ApiError(503, "server unavailable")
        def wait(delay):
            clock[0] += delay
            if clock[0] >= 32:
                self.daemon.stop_event.set()
            return self.daemon.stop_event.is_set()
        with patch("nctalk.sync.time.monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.daemon, "run_once", side_effect=poll), \
             patch.object(self.daemon, "backfill_step", side_effect=backfill), \
             patch.object(self.daemon.stop_event, "wait", side_effect=wait):
            self.assertEqual(self.daemon.run(), 0)
        self.assertEqual(history, [0, 3, 9, 21])
        self.assertEqual(polls[:5], [0, 3, 6, 9, 12])

    def test_daemon_respects_long_retry_after_interruptibly(self) -> None:
        self.client.max_retries = 0
        self.server.fail_next(429, ROOM, retry_after="300")
        waits = []
        def wait(delay):
            waits.append(delay)
            self.daemon.stop_event.set()
            return True
        with patch.object(self.daemon.stop_event, "wait", side_effect=wait):
            self.daemon.run()
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 300, places=2)

    def test_history_retry_after_also_pauses_foreground(self) -> None:
        clock = [0.0]
        polls, history = [], []
        def poll(**kwargs):
            polls.append(clock[0])
        def backfill():
            history.append(clock[0])
            raise ApiError(429, "rate limited", retry_after=300)
        def wait(delay):
            clock[0] += delay
            if clock[0] >= 301:
                self.daemon.stop_event.set()
            return self.daemon.stop_event.is_set()
        with patch("nctalk.sync.time.monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.daemon, "run_once", side_effect=poll), \
             patch.object(self.daemon, "backfill_step", side_effect=backfill), \
             patch.object(self.daemon.stop_event, "wait", side_effect=wait):
            self.daemon.run()
        self.assertEqual(polls, [0, 300])
        self.assertEqual(history, [0, 300])

    def test_transport_retry_wait_interruptible_for_embedded_daemon(self) -> None:
        client = TalkClient(self.server.url, self.server.user, self.server.password)
        with SyncDaemon("alice@embedded", client, Path(self.tmp.name) / "embedded") as daemon:
            self.server.fail_next(429, ROOM, retry_after="300")
            def wait(delay):
                self.assertEqual(delay, 300)
                daemon.stop_event.set()
                return True
            with patch.object(daemon.stop_event, "wait", side_effect=wait):
                with self.assertRaises(ApiError):
                    daemon.run_once()
        self.assertEqual(sum(r["path"] == ROOM for r in self.server.requests), 1)

    def test_members_written_in_bounded_transactions(self) -> None:
        self.sync()
        self.server.members["room1"] = [{"actorType": "users", "actorId": "synthetic-" + str(i),
                                         "displayName": "Synthetic member"} for i in range(1201)]
        sizes, writes = [], [0]
        original = store.upsert_member
        def member(*args):
            writes[0] += 1
            original(*args)
        def trace(sql):
            if sql == "COMMIT":
                sizes.append(writes[0])
                writes[0] = 0
        self.daemon.conn.set_trace_callback(trace)
        try:
            with patch.object(store, "upsert_member", side_effect=member):
                self.daemon._members(self.daemon.conn.execute("SELECT * FROM rooms").fetchone(), active=True)
        finally:
            self.daemon.conn.set_trace_callback(None)
        self.assertEqual(sizes[:3], [500, 500, 201])
        self.assertEqual(self.daemon.counts()["members"], 1201)
        self.assertGreater(self.state()["members_synced_at"], 0)

    def test_outbox_guard_transport_failure_stays_pending(self) -> None:
        self.client.max_retries = 0
        self.client.write_guard_prefix = "omanctalk-test-"
        self.queue("send", {"text": "Synthetic"})
        self.server.fail_next(503, ROOM)
        with self.assertRaises(ApiError):
            self.daemon.process_outbox()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "pending")
        self.assertFalse(any(r["method"] == "POST" for r in self.server.requests))
        self.daemon.process_outbox()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "done")

    def test_outbox_stop_before_write_stays_pending(self) -> None:
        self.client.write_guard_prefix = "omanctalk-test-"
        self.queue("send", {"text": "Synthetic"})
        original = self.client._guard
        def guard(token):
            original(token)
            self.daemon.stop_event.set()
        with patch.object(self.client, "_guard", side_effect=guard):
            with self.assertRaises(ApiError):
                self.daemon.process_outbox()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "pending")
        self.assertFalse(any(r["method"] == "POST" for r in self.server.requests))

    def test_auto_media_rejects_symlinked_token_directory(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.directory / "media").mkdir()
        (self.directory / "media/room1").symlink_to(outside, target_is_directory=True)
        self.server.add_message("room1", "{file}", messageParameters={"file": {
            "type": "file", "id": 1, "name": "synthetic.png", "mimetype": "image/png", "size": 1}})
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}):
            root = Path(self.tmp.name) / "omanctalk"
            root.mkdir()
            (root / "preferences.json").write_text(json.dumps({"auto_download_media": True}))
            self.sync()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.row(1)["local_path"], "")

    def test_429_retry_backoff(self) -> None:
        self.server.fail_next(429, ROOM, retry_after="3")
        self.sync()
        self.assertEqual(self.delays, [3.0])
        self.assertEqual(store.meta_get(self.daemon.conn, "last_error"), "")

    def test_401_stops_without_retry(self) -> None:
        self.server.fail_next(401, ROOM)
        with self.assertRaises(ApiError):
            self.sync()
        self.assertTrue(self.daemon.auth_failed)
        self.assertTrue(self.daemon.stop_event.is_set())
        self.assertEqual(store.meta_get(self.daemon.conn, "last_error"), "auth")
        count = len(self.server.requests)
        with self.assertRaises(ApiError):
            self.sync()
        self.assertEqual(len(self.server.requests), count)
        self.assertEqual(self.daemon.run(), 3)
        self.assertEqual(len(self.server.requests), count)
        self.assertEqual(self.delays, [])

    def test_daemon_error_backoff_without_hammering(self) -> None:
        self.client.max_retries = 0
        self.server.fail_next(429, ROOM, retry_after="4")
        waits: list[float] = []
        def stop_after_wait(seconds: float) -> bool:
            waits.append(seconds)
            self.daemon.stop_event.set()
            return True
        with patch.object(self.daemon.stop_event, "wait", side_effect=stop_after_wait):
            self.assertEqual(self.daemon.run(), 0)
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 4.0, places=2)
        self.assertEqual(sum(r["path"] == ROOM for r in self.server.requests), 1)

    def test_transaction_rollback_preserves_cursor(self) -> None:
        self.server.add_message("room1", "Synthetic first")
        self.sync()
        self.server.add_message("room1", "Synthetic rollback")
        with patch.object(store, "upsert_message", side_effect=RuntimeError("synthetic abort")):
            with self.assertRaises(RuntimeError):
                self.sync()
        self.assertEqual(self.state()["newest_id"], 1)
        self.assertEqual(self.daemon.counts()["messages"], 1)
        self.sync()
        self.assertEqual(self.state()["newest_id"], 2)

    def test_http_failure_cursor_unchanged(self) -> None:
        self.server.add_message("room1", "Synthetic retained")
        self.sync()
        self.server.add_message("room1", "Synthetic later")
        self.server.fail_next(500, CHAT, count=4)
        with self.assertRaises(ApiError):
            self.sync()
        self.assertEqual(self.state()["newest_id"], 1)
        self.assertEqual(store.meta_get(self.daemon.conn, "last_error"), "http-500")
        self.sync()
        self.assertEqual(self.state()["newest_id"], 2)

    def test_missing_room_periodic_full_refresh(self) -> None:
        self.server.add_message("room1", "Synthetic retained offline")
        self.sync(1)
        self.server.remove_room("room1")
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT removed FROM rooms").fetchone()[0], 0)
        self.daemon._last_full -= 301
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT removed FROM rooms").fetchone()[0], 1)
        self.assertEqual(self.daemon.counts()["messages"], 1)

    def test_fts_prefix_sender_placeholder_and_file(self) -> None:
        self.server.add_message("room1", "Synthetic orchard")
        self.server.add_message("room1", "{mention-user1} shares {file}", messageParameters={
            "mention-user1": {"type": "user", "name": "SyntheticBob"},
            "file": {"type": "file", "name": "SyntheticDocument.pdf", "id": 42,
                     "mimetype": "application/pdf", "size": 19, "path": "Talk/synthetic.pdf"}})
        self.sync(1)
        for query in ("orch", "SyntheticBob", "SyntheticDoc"):
            count = self.daemon.conn.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?", (store.fts_query(query),)).fetchone()[0]
            self.assertGreater(count, 0)
        self.assertEqual(self.row(2)["media_type"], "document")

    def test_unknown_optional_fields(self) -> None:
        self.server.rooms["room1"].update(type=99, mystery={"future": True})
        self.server.add_message("room1", {"id": 1, "message": "Synthetic future", "messageParameters": [],
                                        "mystery": True, "actorType": "federated_users"})
        self.sync()
        self.assertEqual(self.row(1)["actor_type"], "federated_users")

    def test_no_progress_is_bounded(self) -> None:
        msg = self.server.add_message("room1", "Synthetic duplicate")
        self.sync()
        self.daemon.conn.execute("UPDATE rooms SET last_message_id=99")
        # A server that keeps repeating an already mirrored record must neither loop forever
        # nor duplicate it: records at or below the cursor are dropped, the poll ends.
        with patch.object(self.client, "chat_history", return_value=([msg], 1, 0)) as history:
            self.daemon._new_messages(self.daemon.conn.execute("SELECT * FROM rooms").fetchone())
        self.assertEqual(history.call_count, 1)
        self.assertEqual(self.daemon.counts()["messages"], 1)

    def queue(self, kind: str, payload: dict, token: str = "room1") -> None:
        self.daemon.conn.execute("INSERT INTO outbox(local_id,token,kind,payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("synthetic-local-" + str(time.monotonic_ns()), token, kind, json.dumps(payload), store.now(), store.now()))

    def test_outbox_send_edit_react_and_no_read(self) -> None:
        self.queue("send", {"text": "Synthetic queued"})
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "done")
        self.sync()
        self.assertEqual(self.daemon.counts()["messages"], 1)
        self.queue("edit", {"id": 1, "text": "Synthetic queued edit"})
        self.queue("react", {"id": 1, "emoji": "🍀"})
        self.queue("read", {})
        self.sync()
        states = [r[0] for r in self.daemon.conn.execute("SELECT state FROM outbox ORDER BY id")]
        self.assertEqual(states, ["done", "done", "done", "failed"])
        self.assertFalse(any(r["path"].endswith("/read") for r in self.server.requests))

    def test_outbox_observed_reference_not_resent(self) -> None:
        self.server.add_message("room1", "Synthetic already sent", referenceId="synthetic-observed")
        self.queue("send", {"text": "Synthetic already sent", "reference_id": "synthetic-observed"})
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "done")
        self.assertFalse(any(r["method"] == "POST" for r in self.server.requests))

    def test_outbox_frozen_reaction_removal(self) -> None:
        msg = self.server.add_message("room1", "Synthetic reaction target")
        self.server.react("room1", msg["id"], "🍀")
        self.sync()
        self.queue("react", {"id": msg["id"], "emoji": "", "remove_emojis": ["🍀"]})
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "done")
        self.sync()
        self.assertEqual(self.daemon.counts()["reactions"], 0)

    def test_outbox_guard(self) -> None:
        self.client.write_guard_prefix = "omanctalk-test-"
        self.server.add_room("unsafe", name="Synthetic existing")
        self.queue("send", {"text": "Synthetic blocked"}, "unsafe")
        self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT state,error FROM outbox").fetchone()[:], ("failed", "guard"))
        self.assertFalse(any(r["method"] != "GET" for r in self.server.requests))

    def test_outbox_ambiguous_post_not_retried(self) -> None:
        self.queue("send", {"text": "Synthetic uncertain"})
        self.sync()
        self.queue("send", {"text": "Synthetic uncertain second"})
        self.server.fail_next(500, CHAT)
        # Avoid the incremental read using the same endpoint for the injected failure.
        self.daemon.conn.execute("UPDATE sync_state SET newest_id=1, activity_seen=2000000000")
        with self.assertRaises(ApiError):
            self.sync()
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox ORDER BY id DESC").fetchone()[0], "failed")
        self.sync()
        self.assertEqual(len(self.server.messages["room1"]), 1)

    def test_interrupted_outbox_not_replayed(self) -> None:
        self.queue("send", {"text": "Synthetic ambiguous"})
        self.daemon.conn.execute("UPDATE outbox SET state='sending'")
        self.daemon.close()
        self.daemon = SyncDaemon("alice@test", self.client, self.directory)
        self.assertEqual(self.daemon.conn.execute("SELECT state FROM outbox").fetchone()[0], "failed")

    def test_clean_stop_interrupts_backfill_wait(self) -> None:
        self.server.add_message("room1", "Synthetic")
        self.sync()
        self.daemon._last_backfill = time.monotonic()
        self.daemon.stop_event.set()
        start = time.monotonic()
        self.assertFalse(self.daemon.backfill_step())
        self.assertLess(time.monotonic() - start, 0.1)

    def test_auto_media_opt_in_and_limit(self) -> None:
        self.server.add_file("Talk/synthetic.png", b"synthetic image")
        self.server.add_message("room1", "{file}", messageParameters={"file": {
            "type": "file", "id": 1, "name": "synthetic.png", "mimetype": "image/png",
            "path": "Talk/synthetic.png", "size": 15}})
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}):
            root = Path(self.tmp.name) / "omanctalk"
            root.mkdir()
            (root / "preferences.json").write_text(json.dumps({"auto_download_media": True}))
            self.sync()
        self.assertTrue(Path(self.row(1)["local_path"]).is_file())
        self.assertEqual(Path(self.row(1)["local_path"]).parent.name, "room1")


class WrapperTests(unittest.TestCase):
    def test_paths_registry_wrapper_once(self) -> None:
        with FakeTalkServer() as server, tempfile.TemporaryDirectory() as tmp:
            server.add_room("room1")
            server.add_message("room1", "Synthetic wrapper")
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                accounts_file().parent.mkdir()
                accounts_file().write_text(json.dumps({"accounts": [{"id": "alice@test", "server": server.url,
                    "user": server.user, "app_password": server.password, "write_guard_prefix": "omanctalk-test-"}]}))
                self.assertEqual(load_account("alice@test")["user"], "alice")
                self.assertEqual(store_dir("alice@test"), Path(tmp) / "omanctalk/stores/alice@test")
                result = subprocess.run([str(ROOT / "bin/omanctalk-sync"), "alice@test", "--once", "--verbose"],
                    env=dict(os.environ, XDG_CACHE_HOME=tmp), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["messages"], 1)
                with closing(store.connect_ro(store_dir("alice@test") / "talk.db")) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertTrue(all(r["method"] == "GET" for r in server.requests))

    def test_wrapper_auth_stop(self) -> None:
        with FakeTalkServer() as server, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "omanctalk"
            root.mkdir()
            (root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "alice@test", "server": server.url,
                "user": server.user, "app_password": "synthetic-wrong"}]}))
            result = subprocess.run([str(ROOT / "bin/omanctalk-sync"), "alice@test"],
                env=dict(os.environ, XDG_STATE_HOME=tmp, XDG_CACHE_HOME=tmp), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn("synthetic-wrong", result.stderr)

    def test_wrapper_sigterm(self) -> None:
        with FakeTalkServer() as server, tempfile.TemporaryDirectory() as tmp:
            server.add_room("room1")
            root = Path(tmp) / "omanctalk"
            root.mkdir()
            (root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "alice@test", "server": server.url,
                "user": server.user, "app_password": server.password}]}))
            process = subprocess.Popen([str(ROOT / "bin/omanctalk-sync"), "alice@test"],
                env=dict(os.environ, XDG_STATE_HOME=tmp, XDG_CACHE_HOME=tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while len(server.requests) < 3 and time.monotonic() < deadline:
                    time.sleep(0.02)
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_invalid_identity(self) -> None:
        for identity in ("../escape", "/absolute", "..", ""):
            with self.assertRaises(ValueError):
                store_dir(identity)


if __name__ == "__main__":
    unittest.main()
