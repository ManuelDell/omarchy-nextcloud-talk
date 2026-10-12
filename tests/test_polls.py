"""Synthetic offline poll integration, including Talk's system-cursor quirk."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import actions, api, dto, polls, prefs, store
from nctalk.sync import SyncDaemon
from fake_talk import FakeTalkServer, POLL


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name, "XDG_CONFIG_HOME": tmp.name + "/config",
                                      "XDG_CACHE_HOME": tmp.name + "/cache"})
        env.start()
        self.addCleanup(env.stop)
        self.server = FakeTalkServer()
        self.addCleanup(self.server.close)
        self.server.add_room("room1")
        self.server.add_room("blocked", name="Synthetic protected room")
        self.identity = prefs.account_id(self.server.url, self.server.user)
        self.account = {"account": self.identity, "server": self.server.url, "user": self.server.user,
                        "app_password": self.server.password, "write_guard_prefix": "omanctalk-test-"}
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [self.account]})
        self.client = api.TalkClient(self.server.url, self.server.user, self.server.password, "omanctalk-test-")
        self.client.max_retries = 0
        self.daemon = SyncDaemon(self.identity, self.client, prefs.store_dir(self.identity))
        self.addCleanup(self.daemon.close)
        self.db = prefs.store_dir(self.identity) / "talk.db"

    def add_poll(self, **kwargs: object) -> tuple[int, str]:
        poll = self.server.add_poll("room1", **kwargs)
        return poll["id"], str(self.server.rooms["room1"]["lastMessage"]["id"])

    def sync(self) -> None:
        self.daemon.run_once(backfill_budget=0)

    def force_events(self) -> None:
        self.daemon._next_poll.clear()
        self.sync()

    def call(self, command: str, **kwargs: object) -> dict:
        return actions.run(command, [], {"account": self.identity, "jid": "room1", **kwargs})

    def messages(self, query: str = "") -> list[dict]:
        reader = dto.Reader(self.account)
        try:
            return reader.messages("room1", query=query)["messages"]
        finally:
            reader.close()

    def mirrored(self, pid: int) -> dict:
        return dict(self.daemon.conn.execute("SELECT * FROM polls WHERE token='room1' AND poll_id=?", (pid,)).fetchone())

    def requests(self, method: str = "GET") -> list[dict]:
        return [r for r in self.server.requests if r["path"].startswith(POLL) and r["method"] == method]

    def test_initial_public_projection_and_mine(self) -> None:
        pid, mid = self.add_poll(options=["Synthetic Amber", "Synthetic Blue", "Synthetic Cyan"])
        self.server.vote_poll("room1", pid, [0, 2])
        self.server.vote_poll("room1", pid, [0], actor_id="bob")
        self.sync()
        message = self.messages()[0]
        self.assertEqual(message["id"], mid)
        self.assertEqual(message["text"], "")
        self.assertEqual(message["poll"], {"question": "Synthetic question", "selectable": 3,
            "options": [{"text": "Synthetic Amber", "votes": 2, "voters": ["Synthetic alice", "Synthetic bob"], "mine": True},
                        {"text": "Synthetic Blue", "votes": 0, "voters": [], "mine": False},
                        {"text": "Synthetic Cyan", "votes": 1, "voters": ["Synthetic alice"], "mine": True}],
            "voters": 2, "id": str(pid), "closed": False, "hidden_results": False})
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertTrue(all(r["method"] == "GET" for r in self.server.requests))

    def test_hidden_then_closed_results(self) -> None:
        pid, _ = self.add_poll(max_votes=2, result_mode=1)
        self.server.vote_poll("room1", pid, [0])
        self.server.vote_poll("room1", pid, [1], actor_id="bob")
        self.sync()
        poll = self.messages()[0]["poll"]
        self.assertEqual(poll["selectable"], 2)
        self.assertTrue(poll["hidden_results"])
        self.assertEqual(poll["voters"], 2)
        self.assertEqual([o["votes"] for o in poll["options"]], [0, 0])
        self.assertEqual([o["voters"] for o in poll["options"]], [[], []])
        self.assertEqual([o["mine"] for o in poll["options"]], [True, False])
        self.server.close_poll("room1", pid)
        self.force_events()
        poll = self.messages()[0]["poll"]
        self.assertTrue(poll["closed"])
        self.assertFalse(poll["hidden_results"])
        self.assertEqual([o["votes"] for o in poll["options"]], [1, 1])

    def test_other_voter_and_close_events_use_comment_cursor(self) -> None:
        pid, mid = self.add_poll()
        self.sync()
        self.server.vote_poll("room1", pid, [1], actor_id="bob")
        self.force_events()
        self.assertEqual(self.messages()[0]["poll"]["options"][1]["votes"], 1)
        self.server.close_poll("room1", pid)
        self.force_events()
        self.assertTrue(self.messages()[0]["poll"]["closed"])
        future = [r for r in self.server.requests if r["query"].get("lookIntoFuture") == "1"]
        self.assertTrue(future)
        self.assertTrue(all(r["query"]["lastKnownMessageId"] == mid for r in future))
        self.assertEqual(len(self.messages()), 1)

    def test_periodic_refresh_without_events(self) -> None:
        pid, _ = self.add_poll()
        self.sync()
        self.server.poll_votes[("room1", pid)]["bob"] = [1]
        self.daemon.conn.execute("UPDATE polls SET updated_at=?", (store.now() - 61,))
        self.sync()
        self.assertEqual(self.messages()[0]["poll"]["options"][1]["votes"], 1)
        self.assertEqual(len(self.requests()), 2)
        self.sync()
        self.assertEqual(len(self.requests()), 2)

    def test_backfill_and_search_replay(self) -> None:
        pid, mid = self.add_poll(question="Synthetic Astronomy", options=["Synthetic Nebula", "Synthetic Comet"])
        self.server.add_message("room1", "Synthetic newer")
        self.server.page_size = 1
        self.sync()
        self.assertFalse(self.requests())
        self.assertTrue(self.daemon.backfill_step())
        self.assertEqual(self.mirrored(pid)["question"], "Synthetic Astronomy")
        self.assertEqual(self.messages("Nebula")[0]["id"], mid)
        self.assertEqual(self.messages("Astronomy")[0]["id"], mid)
        msg = self.server.message("room1", int(mid))
        self.daemon._apply_page("room1", [msg], int(mid), 0, history=True)
        self.assertEqual(len(self.messages("Comet")), 1)
        self.assertEqual(len(self.requests()), 1)

    def test_missing_polls_resume_with_cycle_budget(self) -> None:
        for i in range(11):
            self.add_poll(question=f"Synthetic Poll {i}")
        self.sync()
        self.assertEqual(len(self.requests()), 4)
        self.sync()
        self.assertEqual(len(self.requests()), 8)
        self.sync()
        self.assertEqual(len(self.requests()), 11)
        self.assertEqual(self.daemon.conn.execute("SELECT count(*) FROM polls").fetchone()[0], 11)
        self.sync()
        self.assertEqual(len(self.requests()), 11)

    def test_missing_poll_recovered_after_restart(self) -> None:
        for _ in range(6):
            self.add_poll()
        self.sync()
        with SyncDaemon(self.identity, self.client, prefs.store_dir(self.identity)) as restarted:
            restarted.run_once(backfill_budget=0)
        self.assertEqual(self.daemon.conn.execute("SELECT count(*) FROM polls").fetchone()[0], 6)

    def test_poll_404_403_nonfatal_and_no_hammering(self) -> None:
        for status in (404, 403):
            with self.subTest(status=status):
                pid, _ = self.add_poll()
                self.server.fail_next(status, POLL + "/room1/" + str(pid))
                self.sync()
                self.assertFalse(self.daemon.conn.execute("SELECT 1 FROM polls WHERE poll_id=?", (pid,)).fetchone())
                before = len(self.requests())
                self.sync()
                self.assertEqual(len(self.requests()), before)
                self.assertEqual(self.daemon.conn.execute("SELECT removed FROM rooms WHERE token='room1'").fetchone()[0], 0)
                self.daemon.conn.execute("UPDATE meta SET value='0' WHERE key LIKE 'poll_retry:%'")
                self.sync()
                self.assertEqual(self.mirrored(pid)["status"], 0)

    def test_poll_401_stops_and_429_preserves_backoff(self) -> None:
        pid, _ = self.add_poll()
        self.server.fail_next(429, POLL, retry_after="12")
        with self.assertRaises(api.ApiError) as caught:
            self.sync()
        self.assertGreaterEqual(caught.exception.retry_after, 12)
        self.daemon.conn.execute("UPDATE meta SET value='0' WHERE key LIKE 'poll_retry:%'")
        self.server.fail_next(401, POLL)
        with self.assertRaises(api.ApiError):
            self.sync()
        self.assertTrue(self.daemon.auth_failed)
        self.assertEqual(store.meta_get(self.daemon.conn, "last_error"), "auth")

    def test_old_mirror_and_malformed_params(self) -> None:
        _, _ = self.add_poll()
        self.sync()
        self.daemon.conn.execute("DROP TABLE polls")
        self.assertNotIn("poll", self.messages()[0])
        for value in (None, [], {"object": None}, {"object": {"type": "talk-poll", "id": "bad"}}):
            self.assertEqual(polls.poll_id(value), 0)
        # An older mirror gets the table from the regular migration, and migrating twice is harmless.
        self.daemon.conn.execute("DELETE FROM schema_migrations WHERE version=4")
        store.migrate(self.daemon.conn)
        store.migrate(self.daemon.conn)
        self.assertEqual(store.schema_version(self.daemon.conn), 4)
        self.assertTrue(self.daemon.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='polls'").fetchone())

    def test_polls_are_refused_in_the_personal_notes_room_without_asking_the_server(self) -> None:
        self.sync()  # the room must be in the mirror before it can be turned into a notes room
        self.daemon.conn.execute("UPDATE rooms SET type=6 WHERE token='room1'")
        before = len(self.server.requests)
        result = self.call("poll", question="Synthetic q", options=["Synthetic A", "Synthetic B"], multi=1)
        self.assertFalse(result["ok"])
        self.assertIn("cannot be created in this conversation", result["error"])
        self.assertEqual(len(self.server.requests), before, "the server is not asked")

    def test_tallies_use_talks_option_keys_and_plain_indexes(self) -> None:
        # The server answers {"option-0": 1, "option-2": 1}; plain "0"/"2" must keep working.
        row = {"question": "q", "options_json": '["A","B","C"]', "max_votes": 0, "result_mode": 0,
               "status": 0, "num_voters": 1, "voted_self_json": "[]", "details_json": "[]", "poll_id": 1}
        for votes in ('{"option-0": 1, "option-2": 3}', '{"0": 1, "2": 3}'):
            card = polls.projection({**row, "votes_json": votes})
            self.assertEqual([o["votes"] for o in card["options"]], [1, 0, 3], votes)
            self.assertEqual(card["selectable"], 3)

    def test_upsert_partial_missing_fields_and_idempotent_fts(self) -> None:
        pid, mid = self.add_poll(options=["Synthetic Quartz", "Synthetic Ruby"])
        self.sync()
        polls.upsert_poll(self.daemon.conn, "room1", {"id": pid, "numVoters": None})
        polls.upsert_poll(self.daemon.conn, "room1", {"id": pid})
        self.assertEqual(json.loads(self.mirrored(pid)["options_json"]), ["Synthetic Quartz", "Synthetic Ruby"])
        self.assertEqual(self.messages("Quartz")[0]["id"], mid)
        before = self.daemon.conn.execute("SELECT search_text FROM messages WHERE id=?", (int(mid),)).fetchone()[0]
        polls.upsert_poll(self.daemon.conn, "room1", {"id": pid})
        self.assertEqual(self.daemon.conn.execute("SELECT search_text FROM messages WHERE id=?", (int(mid),)).fetchone()[0], before)
        polls.upsert_poll(self.daemon.conn, "room1", {"id": 900})
        self.assertEqual(self.mirrored(900)["question"], "")
        with self.assertRaises(ValueError):
            polls.upsert_poll(self.daemon.conn, "room1", {})

    def test_vote_withdraw_immediate_mirror_no_outbox(self) -> None:
        pid, mid = self.add_poll(max_votes=2)
        self.sync()
        result = self.call("poll-vote", id=mid, options=["Synthetic A", "Synthetic B"])
        self.assertEqual(result, {"ok": True, "kind": "poll-vote", "options": ["Synthetic A", "Synthetic B"]})
        self.assertEqual(json.loads(self.mirrored(pid)["voted_self_json"]), [0, 1])
        self.assertTrue(all(o["mine"] for o in self.messages()[0]["poll"]["options"]))
        result = self.call("poll-vote", id=mid, options=[])
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(self.mirrored(pid)["voted_self_json"]), [])
        self.assertEqual(self.messages()[0]["poll"]["voters"], 0)
        self.assertEqual(self.daemon.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.requests("POST")[-1]["data"]["optionIds"], [])

    def test_vote_limit_text_and_duplicates(self) -> None:
        _, mid = self.add_poll(options=["Synthetic A", "Synthetic B", "Synthetic C"], max_votes=2)
        self.sync()
        for options in (["Synthetic A", "Synthetic B", "Synthetic C"], ["Synthetic wrong"], ["Synthetic A", "Synthetic A"], "bad", [None]):
            with self.subTest(options=options):
                self.assertFalse(self.call("poll-vote", id=mid, options=options)["ok"])
        self.assertFalse(self.requests("POST"))
        self.assertTrue(self.call("poll-vote", id=mid, options=["Synthetic A", "Synthetic C"])["ok"])

    def test_ambiguous_remote_options(self) -> None:
        _, mid = self.add_poll(options=["Synthetic same", "Synthetic same"])
        self.sync()
        self.assertIn("ambiguous", self.call("poll-vote", id=mid, options=["Synthetic same"])["error"])
        self.assertFalse(self.requests("POST"))

    def test_create_unlimited_and_hidden(self) -> None:
        self.sync()
        result = self.call("poll", question="Synthetic New", options=["Synthetic A", "Synthetic B"], multi=2, hidden=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "poll")
        self.assertTrue(result["message_id"])
        created = next(iter(self.server.polls.values()))
        self.assertEqual((created["maxVotes"], created["resultMode"]), (0, 1))
        self.assertEqual(self.mirrored(created["id"])["max_votes"], 0)
        self.sync()
        self.assertTrue(self.messages()[0]["poll"]["hidden_results"])
        mid = result["message_id"]
        self.assertTrue(self.call("poll-vote", id=mid, options=["Synthetic A", "Synthetic B"])["ok"])

    def test_create_limited_and_validation(self) -> None:
        self.sync()
        self.assertTrue(self.call("poll", question="Synthetic New", options=["A", "B", "C"], multi=2)["ok"])
        self.assertEqual(next(iter(self.server.polls.values()))["maxVotes"], 2)
        count = len(self.requests("POST"))
        base = {"question": "Synthetic", "options": ["A", "B"], "multi": 1}
        for overrides in ({"question": ""}, {"options": ["A"]}, {"options": ["A", "A"]}, {"multi": 0},
                          {"multi": True}, {"multi": 3}, {"hidden": 1}, {"options": ["A", None]}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.call("poll", **dict(base, **overrides))["ok"])
        self.assertEqual(len(self.requests("POST")), count)

    def test_close_immediate_mirror_and_closed_vote(self) -> None:
        pid, mid = self.add_poll()
        self.sync()
        self.assertEqual(self.call("poll-close", id=mid), {"ok": True, "kind": "poll-close"})
        self.assertEqual(self.mirrored(pid)["status"], 1)
        self.assertIn("closed", self.call("poll-vote", id=mid, options=[])["error"])
        self.assertTrue(self.call("poll-close", id=mid)["ok"])
        self.assertEqual(len(self.requests("DELETE")), 1)

    def test_guard_all_poll_mutations_and_feature_check(self) -> None:
        self.sync()
        with self.assertRaises(api.GuardError):
            self.client.poll_create("blocked", "Synthetic", ["A", "B"])
        poll = self.server.add_poll("blocked")
        for operation in (lambda: self.client.poll_vote("blocked", poll["id"], []),
                          lambda: self.client.poll_close("blocked", poll["id"])):
            with self.assertRaises(api.GuardError):
                operation()
        self.assertFalse(self.requests("POST"))
        self.assertFalse(self.requests("DELETE"))
        self.client._capabilities = {}
        with self.assertRaises(api.ApiError):
            self.client.poll_get("room1", 1)
        store.meta_set(self.daemon.conn, "capabilities_json", "{}")
        self.assertFalse(self.call("poll", question="Synthetic", options=["A", "B"], multi=1)["supported"])

    def test_offline_not_queued(self) -> None:
        pid, mid = self.add_poll()
        self.sync()
        for command in ("poll", "poll-vote", "poll-close"):
            self.server.fail_next(503, POLL)
            result = self.call(command, id=mid, options=["Synthetic A", "Synthetic B"], question="Synthetic", multi=1)
            self.assertFalse(result["ok"])
            self.assertIn("offline", result["error"])
        prefs.update_account(self.identity, lambda state: state.update(online=False))
        before = len(self.server.requests)
        self.assertEqual(self.call("poll-vote", id=mid, options=[])["error"], "offline")
        self.assertEqual(len(self.server.requests), before)
        self.assertEqual(self.daemon.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(json.loads(self.mirrored(pid)["voted_self_json"]), [])

    def test_refresh_failure_never_invites_resending(self) -> None:
        pid, mid = self.add_poll()
        self.sync()
        original = actions.ShareClient.poll_vote
        def vote(client: api.TalkClient, *args: object) -> object:
            result = original(client, *args)
            self.server.fail_next(404, POLL)
            return result
        with patch.object(actions.ShareClient, "poll_vote", vote):
            result = self.call("poll-vote", id=mid, options=["Synthetic A"])
        self.assertIn("do not retry", result["error"])
        self.assertEqual(self.server.poll_votes[("room1", pid)][self.server.user], [0])
        self.assertEqual(len(self.requests("POST")), 1)

    def test_close_requires_creator_or_moderator(self) -> None:
        pid, mid = self.add_poll()
        self.server.polls[("room1", pid)]["actorId"] = "bob"
        self.server.members["room1"][0]["participantType"] = 3
        self.sync()
        self.assertEqual(self.call("poll-close", id=mid)["error"], "forbidden")
        self.assertEqual(self.mirrored(pid)["status"], 0)
        self.server.members["room1"][0]["participantType"] = 2
        self.assertTrue(self.call("poll-close", id=mid)["ok"])

    def test_actions_guard_refreshes_room_name(self) -> None:
        pid, mid = self.add_poll()
        self.sync()
        self.server.rooms["room1"]["name"] = "Synthetic protected"
        before = len(self.requests("POST")) + len(self.requests("DELETE"))
        for command in ("poll-vote", "poll-close", "poll"):
            result = self.call(command, id=mid, options=["Synthetic A", "Synthetic B"], question="Synthetic", multi=1)
            self.assertIn("guard", result["error"])
        self.assertEqual(len(self.requests("POST")) + len(self.requests("DELETE")), before)
        self.assertEqual(self.mirrored(pid)["status"], 0)

    def test_actions_require_capabilities_and_poll_schema_before_write(self) -> None:
        self.sync()
        self.daemon.conn.execute("DELETE FROM meta WHERE key='capabilities_json'")
        result = self.call("poll", question="Synthetic", options=["A", "B"], multi=1)
        self.assertIn("synchronize", result["error"])
        self.assertTrue(result["supported"])
        store.meta_set(self.daemon.conn, "capabilities_json", json.dumps(self.server.capabilities))
        self.daemon.conn.execute("DROP TABLE polls")
        result = self.call("poll", question="Synthetic", options=["A", "B"], multi=1)
        self.assertIn("synchronize", result["error"])
        self.assertFalse(self.requests("POST"))

    def test_no_poll_feature_sync_and_actions(self) -> None:
        self.server.features.remove("talk-polls")
        self.add_poll()
        self.sync()
        self.assertFalse(self.requests())
        self.assertNotIn("poll", self.messages()[0])
        for command in ("poll", "poll-vote", "poll-close"):
            result = self.call(command)
            self.assertFalse(result["supported"])

    def test_deleted_poll_not_searchable_or_votable(self) -> None:
        _, mid = self.add_poll(options=["Synthetic Sapphire", "Synthetic Zircon"])
        self.sync()
        self.server.delete_message("room1", int(mid))
        self.sync()
        self.assertFalse(self.messages("Sapphire"))
        self.assertNotIn("poll", self.messages()[0])
        self.assertFalse(self.call("poll-vote", id=mid, options=[])["ok"])

    def test_create_ambiguous_failure_is_not_queued_or_replayed(self) -> None:
        self.sync()
        self.server.fail_next(503, POLL)
        result = self.call("poll", question="Synthetic", options=["A", "B"], multi=1)
        self.assertFalse(result["ok"])
        self.assertIn("verify before retry", result["error"])
        self.assertEqual(len(self.requests("POST")), 1)
        self.assertEqual(self.daemon.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_cli_poll_close_dispatch(self) -> None:
        _, mid = self.add_poll()
        self.sync()
        helper = Path(__file__).resolve().parents[1] / "bin" / "omanctalk"
        result = subprocess.run([str(helper), "poll-close"], input=json.dumps({"account": self.identity, "jid": "room1", "id": mid}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "kind": "poll-close"})


if __name__ == "__main__":
    unittest.main()
