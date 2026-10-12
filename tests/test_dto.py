"""Synthetic offline mirrors, DTO schemas and an opt-in startup benchmark."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import dto, notify, prefs, store
import omanctalk_core as core

ACCOUNT = {"account": "alice@example.test", "server": "https://example.test", "user": "alice", "app_password": "synthetic-password"}
CHAT_SCHEMA = {**dict.fromkeys("jid account account_label name kind last_message_id preview last_media_type last_sender last_status avatar_path".split(), str),
               **dict.fromkeys("timestamp unread notification_unread room_type".split(), int),
               **dict.fromkeys("last_from_me archived pinned muted mentioned".split(), bool)}
MESSAGE_SCHEMA = {**dict.fromkeys("id text sender sender_jid media_type mime_type local_path filename quoted_id quoted_sender quoted_text quoted_media_type album_id location_name location_address".split(), str),
                  **dict.fromkeys("timestamp file_size album_index album_count".split(), int),
                  **dict.fromkeys("from_me media_unavailable edited forwarded starred revoked system voice location_live".split(), bool),
                  "buttons": list, "reactions": list, "latitude": (float, int, type(None)), "longitude": (float, int, type(None))}
MEMBER_SCHEMA = dict.fromkeys("jid name phone role".split(), str)


def validate(test: unittest.TestCase, value: dict, schema: dict) -> None:
    for key, expected in schema.items():
        test.assertIn(key, value)
        if isinstance(expected, tuple):
            test.assertIn(type(value[key]), expected, key)
        else:
            test.assertIs(type(value[key]), expected, key)


def fixture(conn: sqlite3.Connection, rooms: int = 1, messages: int = 8) -> None:
    conn.execute("BEGIN")
    for index in range(rooms):
        token = "room" + str(index)
        store.upsert_room(conn, {"token": token, "type": 2, "displayName": "Synthetic room " + str(index),
                                "lastActivity": 1000 + messages // 2, "lastMessage": {"id": messages},
                                "unreadMessages": 3, "unreadMention": True})
        for mid in range(1, messages + 1):
            store.upsert_message(conn, token, {"id": mid, "actorType": "users", "actorId": "bob",
                                               "actorDisplayName": "Synthetic Bob", "timestamp": 1000 + mid // 2,
                                               "message": "Synthetische Grüße Äpfel " + str(mid)})
    store.meta_set(conn, "last_sync_ok_ts", int(time.time()))
    conn.execute("COMMIT")


class MirrorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "XDG_CONFIG_HOME": self.temp.name + "/config"})
        self.env.start()
        prefs.atomic_json(prefs.accounts_file(), {"version": 1, "accounts": [ACCOUNT]})
        self.conn = store.connect(prefs.store_dir(ACCOUNT["account"]) / "talk.db")
        fixture(self.conn)
        self.reader = dto.Reader(ACCOUNT)

    def tearDown(self) -> None:
        self.reader.close()
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()


class DTOTests(MirrorCase):
    def test_empty_mirror_database_ready_but_not_rail_ready(self) -> None:
        self.conn.execute("DELETE FROM rooms")
        with patch("subprocess.run", return_value=type("Result", (), {"returncode": 1})()):
            result = dto.status()
        self.assertTrue(result["database_ready"])
        self.assertTrue(result["any_database_ready"])
        self.assertTrue(result["accounts"][0]["database_ready"])
        self.assertFalse(result["rail_ready"])
        self.assertFalse(result["accounts"][0]["rail_ready"])
        self.assertEqual(self.reader.chats(), [])

    def test_message_reference_id_string(self) -> None:
        self.conn.execute("UPDATE messages SET reference_id='synthetic-reference' WHERE id=8")
        messages = self.reader.messages("room0")["messages"]
        self.assertEqual(messages[0]["reference_id"], "synthetic-reference")
        self.assertTrue(all(isinstance(m["reference_id"], str) for m in messages))
        self.assertEqual(messages[-1]["reference_id"], "")

    def test_dm_peer_from_object_members_or_name(self) -> None:
        self.assertEqual(self.reader.chats()[0]["peer_jid"], "")
        self.conn.execute("UPDATE rooms SET type=1,object_type='user',object_id='bob' WHERE token='room0'")
        self.assertEqual(self.reader.chats()[0]["peer_jid"], "users/bob")
        self.conn.execute("UPDATE rooms SET object_type='',object_id='',name='' WHERE token='room0'")
        self.assertEqual(self.reader.chats()[0]["peer_jid"], "")
        store.upsert_member(self.conn, "room0", {"actorType": "users", "actorId": "alice", "displayName": "Synthetic Alice"})
        store.upsert_member(self.conn, "room0", {"actorType": "users", "actorId": "bob", "displayName": "Synthetic Bob"})
        self.assertEqual(self.reader.chats()[0]["peer_jid"], "users/bob")
        self.conn.execute("DELETE FROM members")
        self.conn.execute("UPDATE rooms SET name='synthetic-peer' WHERE token='room0'")
        self.assertEqual(self.reader.chats()[0]["peer_jid"], "users/synthetic-peer")

    def test_avatars_reports_unsupported_refresh(self) -> None:
        result = core.dispatch("avatars", [], {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["supported"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["refreshed"], 0)

    def test_chat_schema_and_search(self) -> None:
        rows = self.reader.chats()
        self.assertEqual(len(rows), 1)
        validate(self, rows[0], CHAT_SCHEMA)
        self.assertEqual(rows[0]["unread"], 3)
        self.assertTrue(rows[0]["mentioned"])
        self.assertEqual(len(self.reader.chats("Synthetic")), 1)
        self.assertEqual(self.reader.chats("%"), [])
        self.assertEqual(self.reader.chats("missing"), [])

    def test_message_schema_and_newest_first(self) -> None:
        result = self.reader.messages("room0")
        self.assertFalse(result["has_more"])
        self.assertEqual([m["id"] for m in result["messages"]], list(map(str, range(8, 0, -1))))
        for message in result["messages"]:
            validate(self, message, MESSAGE_SCHEMA)

    def test_pagination_equal_second(self) -> None:
        ids, cursor = [], None
        for _ in range(5):
            result = self.reader.messages("room0", limit=3, before=cursor)
            ids.extend(m["id"] for m in result["messages"])
            if not result["has_more"]:
                break
            last = result["messages"][-1]
            cursor = {"ts": last["timestamp"], "id": last["id"]}
        self.assertEqual(ids, list(map(str, range(8, 0, -1))))
        self.assertEqual(len(set(ids)), 8)
        with self.assertRaises(ValueError):
            self.reader.messages("room0", before={"ts": "oops", "id": "2"})

    def test_fts_prefix_umlauts_and_tokens(self) -> None:
        for query in ("Synthet", "grü", "GRÜßE", "Äpf", "Apf", "synthet äpf", '"Äpf"'):
            self.assertEqual(len(self.reader.messages("room0", query=query)["messages"]), 8, query)
        self.assertEqual(self.reader.messages("room0", query="synthet missing")["messages"], [])

    def test_mentions_reply_edits_reactions_and_stars(self) -> None:
        store.upsert_message(self.conn, "room0", {"id": 9, "actorType": "users", "actorId": "alice",
            "timestamp": 1010, "actorDisplayName": "Synthetic Alice", "message": "Hello {mention-user1} {file}",
            "messageParameters": {"mention-user1": {"type": "user", "name": "Bob"},
                                  "file": {"type": "file", "id": 5, "name": "synthetic.png", "mimetype": "image/png", "size": 12}},
            "parent": {"id": 8}, "lastEditTimestamp": 1011, "reactions": {"👍": 2}})
        self.conn.execute("INSERT INTO stars VALUES ('room0',9,1012)")
        row = self.reader.messages("room0")["messages"][0]
        self.assertEqual(row["text"], "Hello @Bob synthetic.png")
        self.assertEqual(row["quoted_id"], "8")
        self.assertIn("Grüße", row["quoted_text"])
        self.assertTrue(row["edited"] and row["starred"] and row["from_me"])
        self.assertEqual(len(row["reactions"]), 2)
        self.assertEqual(row["reactions"][0]["sender_jid"], "")
        store.replace_reactions(self.conn, "room0", 9, {"👍": [{"actorType": "users", "actorId": "alice", "actorDisplayName": "Alice"}]})
        items = self.reader.messages("room0")["messages"][0]["reactions"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["from_me"])
        self.assertEqual(items[0]["sender_jid"], "users/alice")

    def test_system_deleted_voice_and_filters(self) -> None:
        for mid, system in ((9, "message_edited"), (10, "conversation_created")):
            store.upsert_message(self.conn, "room0", {"id": mid, "timestamp": 1010 + mid, "message": "Synthetic system", "systemMessage": system})
        store.upsert_message(self.conn, "room0", {"id": 11, "timestamp": 1022, "message": "Deleted synthetic", "deleted": True})
        store.upsert_message(self.conn, "room0", {"id": 12, "timestamp": 1023, "messageType": "voice-message", "message": ""})
        messages = self.reader.messages("room0")["messages"]
        self.assertNotIn("9", [m["id"] for m in messages])
        self.assertTrue(next(m for m in messages if m["id"] == "10")["system"])
        revoked = next(m for m in messages if m["id"] == "11")
        self.assertTrue(revoked["revoked"])
        self.assertEqual(revoked["text"], "")
        self.assertEqual(messages[0]["media_type"], "audio")
        store.upsert_message(self.conn, "room0", {"id": 13, "timestamp": 1024, "message": "https://example.test/synthetic"})
        self.assertEqual([m["id"] for m in self.reader.messages("room0", kind="links")["messages"]], ["13"])
        self.assertEqual(self.reader.messages("room0", kind="docs")["messages"], [])

    def test_media_documents_and_local_paths(self) -> None:
        for mid, mime, name in ((9, "image/png", "synthetic.png"), (10, "application/pdf", "synthetic.pdf")):
            store.upsert_message(self.conn, "room0", {"id": mid, "timestamp": 1020 + mid,
                "message": "{file}", "messageParameters": {"file": {"type": "file", "id": mid,
                    "name": name, "mimetype": mime, "size": 16}}})
        self.conn.execute("UPDATE messages SET local_path='/synthetic/file.png' WHERE token='room0' AND id=9")
        media = self.reader.messages("room0", kind="media")["messages"]
        docs = self.reader.messages("room0", kind="docs")["messages"]
        self.assertEqual([m["id"] for m in media], ["9"])
        self.assertEqual([m["id"] for m in docs], ["10"])
        self.assertEqual(media[0]["local_path"], "/synthetic/file.png")
        self.assertEqual(docs[0]["media_type"], "document")
        self.assertEqual(docs[0]["filename"], "synthetic.pdf")
        self.assertEqual(docs[0]["file_size"], 16)
        counts = self.reader.details("room0")["counts"]
        self.assertEqual((counts["media"], counts["documents"], counts["missing"]), (1, 1, 1))

    def test_members_and_details(self) -> None:
        store.upsert_member(self.conn, "room0", {"actorType": "users", "actorId": "alice", "displayName": "Alice", "participantType": 1})
        store.upsert_member(self.conn, "room0", {"actorType": "guests", "actorId": "g", "displayName": "Guest", "participantType": 4})
        result = self.reader.members("room0")
        for member in result["members"]:
            validate(self, member, MEMBER_SCHEMA)
        self.assertEqual(result["members"][0]["role"], "owner")
        detail = self.reader.details("room0")
        validate(self, detail, {"chat": dict, "account": str, "archived": bool, "pinned": bool, "muted": bool, "since": int, "counts": dict, "avatar_path": str, "group": dict, "participants": list})
        validate(self, detail["counts"], dict.fromkeys("total media documents links starred missing".split(), int))
        self.assertEqual(detail["counts"]["total"], 8)
        self.assertEqual(detail["group"]["my_role"], "owner")
        self.conn.execute("UPDATE rooms SET type=1 WHERE token='room0'")
        dm = self.reader.details("room0")
        validate(self, dm["person"], {**dict.fromkeys("phone full_name push_name business_name alias".split(), str), "tags": list})
        self.assertEqual(dm["groups_in_common"], [])

    def test_acknowledge_cycle(self) -> None:
        self.assertEqual(core.acknowledge([ACCOUNT], "room0")["count"], 1)
        reader = dto.Reader(ACCOUNT)
        self.assertEqual(reader.chats()[0]["notification_unread"], 0)
        reader.close()
        self.conn.execute("UPDATE rooms SET unread_messages=4,last_message_id=9,last_activity=1010")
        reader = dto.Reader(ACCOUNT)
        self.assertEqual(reader.chats()[0]["notification_unread"], 1)
        self.assertEqual(reader.chats()[0]["unread"], 4)
        reader.close()
        core.acknowledge([ACCOUNT])
        reader = dto.Reader(ACCOUNT)
        self.assertEqual(reader.chats()[0]["notification_unread"], 0)
        reader.close()

    def test_offline_status_unit_probe_and_avatar(self) -> None:
        store.meta_set(self.conn, "last_sync_ok_ts", 1)
        with patch("subprocess.run", return_value=type("Result", (), {"returncode": 1})()) as runner:
            result = dto.status()
        runner.assert_called_once()
        self.assertTrue(result["rail_ready"])
        self.assertFalse(result["online"] or result["sync_active"])
        avatar = Path(self.temp.name) / "synthetic.png"
        avatar.write_bytes(b"synthetic-avatar")
        self.conn.execute("INSERT INTO avatars(token,path) VALUES ('room0',?)", (str(avatar),))
        self.assertEqual(self.reader.chats()[0]["avatar_path"], str(avatar))
        avatar.unlink()
        self.assertEqual(self.reader.chats()[0]["avatar_path"], "")
        validate(self, result["accounts"][0], {
            **dict.fromkeys("account label store unit connection_state error".split(), str),
            **dict.fromkeys("default main active authenticated sync_active needs_relink online offline_mode send_read_receipts notifications_muted fts_enabled database_ready".split(), bool),
            "signature": dict})
        validate(self, result["setup"], {
            **dict.fromkeys("consented agents previous_install complete legacy_copies zenity managed".split(), bool),
            "units": str, "links": dict, "conflicts": list, "wacli": dict, "original_plugin": dict, "plugin_root": str})

    def test_corrupt_mirror_status_is_honest(self) -> None:
        self.conn.execute("DROP TABLE rooms")
        with patch("subprocess.run", return_value=type("Result", (), {"returncode": 1})()):
            result = dto.status()
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["database_ready"])
        self.assertEqual(result["error"], "local mirror unavailable")
        chats = core.all_chats([ACCOUNT])
        self.assertEqual(chats["chats"], [])
        self.assertFalse(chats["accounts"][0]["ready"])

    def test_read_only_and_no_outbox_bubbles(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            self.reader.conn.execute("UPDATE rooms SET name='no'")
        self.conn.execute("INSERT INTO outbox(local_id,token,kind,payload,created_at,updated_at) VALUES ('local1','room0','text','{}',1000,1000)")
        self.assertEqual(len(self.reader.messages("room0")["messages"]), 8)

    def test_chat_sort_hide_and_muted_badges(self) -> None:
        store.upsert_room(self.conn, {"token": "older", "type": 5, "displayName": "Older", "lastActivity": 1, "isFavorite": True})
        self.assertEqual(self.reader.chats(limit=1)[0]["jid"], "older")
        core.chat_action(ACCOUNT, {"jid": "older", "action": "remove-local"})
        reader = dto.Reader(ACCOUNT)
        self.assertEqual(len(reader.chats()), 1)
        reader.close()
        self.conn.execute("UPDATE rooms SET notification_level=3 WHERE token='room0'")
        self.assertEqual(self.reader.chats()[1]["notification_unread"], 0)

    def test_status_schema_and_auth_error(self) -> None:
        with patch("nctalk.notify.available", return_value=False):
            status = dto.status()
        validate(self, status, {**dict.fromkeys("installed authenticated database_ready any_authenticated any_database_ready rail_ready sync_active needs_relink online offline_mode notify_available closed fts_enabled".split(), bool),
                               **dict.fromkeys("helper_version wacli_version account connection_state error".split(), str),
                               "accounts": list, "setup": dict, "notifications": dict})
        self.assertTrue(status["authenticated"] and status["setup"]["complete"])
        self.assertEqual(status["accounts"][0]["store"], str(prefs.store_dir(ACCOUNT["account"])))
        self.assertNotIn("app_password", json.dumps(status))
        store.meta_set(self.conn, "last_error", "auth")
        self.assertTrue(dto.status()["needs_relink"])
        self.assertEqual(dto.status()["connection_state"], "relink-required")
        store.meta_set(self.conn, "last_error", "network")
        result = dto.status()
        self.assertEqual(result["connection_state"], "connecting")
        self.assertFalse(result["online"])


class NotifyTests(MirrorCase):
    def enable(self) -> None:
        prefs.update_preferences(lambda value: value["notifications"].update(enabled=True, sound=False))

    def run_notify(self, sender=lambda *args: True, quiet=False, skip="") -> dict:
        return notify.run(ACCOUNT["account"], skip, sender=sender, sound_player=lambda: False, is_dnd=lambda: quiet)

    def advance(self, token="room0", mid=9, timestamp=1010) -> None:
        self.conn.execute("UPDATE rooms SET last_message_id=?,last_activity=?,unread_messages=4 WHERE token=?", (mid, timestamp, token))

    def test_seed_delivery_no_replay_or_edit(self) -> None:
        self.enable()
        self.assertTrue(self.run_notify()["seeded"])
        self.advance()
        self.assertEqual(self.run_notify()["sent"], 1)
        self.assertEqual(self.run_notify()["sent"], 0)
        self.advance(timestamp=1020)
        self.assertEqual(self.run_notify()["sent"], 0)

    def test_ack_is_independent(self) -> None:
        self.enable()
        self.run_notify()
        self.advance()
        core.acknowledge([ACCOUNT])
        self.assertEqual(self.run_notify()["sent"], 1)

    def test_failed_delivery_retried(self) -> None:
        self.enable()
        self.run_notify()
        self.advance()
        result = self.run_notify(sender=lambda *args: False)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.run_notify()["sent"], 1)

    def test_dnd_visible_and_muted_adopt_watermark(self) -> None:
        self.enable()
        self.run_notify()
        self.advance()
        self.assertEqual(self.run_notify(quiet=True)["sent"], 0)
        self.assertEqual(self.run_notify()["sent"], 0)
        self.advance(mid=10, timestamp=1020)
        self.assertEqual(self.run_notify(skip="room0")["sent"], 0)
        self.assertEqual(self.run_notify()["sent"], 0)

    def test_burst_cap_and_summary(self) -> None:
        fixture(self.conn, rooms=8)
        self.enable()
        self.run_notify()
        for index in range(8):
            self.advance("room" + str(index))
        self.assertEqual(self.run_notify()["sent"], 6)
        self.assertEqual(self.run_notify()["sent"], 0)

    def test_sound_once_for_burst_and_account_mute(self) -> None:
        self.enable()
        self.run_notify()
        prefs.update_preferences(lambda value: value["notifications"].update(sound=True))
        self.advance()
        sounds = []
        result = notify.run(sender=lambda *args: True, sound_player=lambda: sounds.append(True) or True, is_dnd=lambda: False)
        self.assertTrue(result["sound"])
        self.assertEqual(sounds, [True])
        prefs.update_account(ACCOUNT["account"], lambda state: state.update(notifications_muted=True))
        self.advance(mid=10, timestamp=1020)
        self.assertEqual(self.run_notify()["sent"], 0)
        prefs.update_account(ACCOUNT["account"], lambda state: state.update(notifications_muted=False))
        self.assertEqual(self.run_notify()["sent"], 0)

    def test_mode_reseeds_and_validates(self) -> None:
        with patch("nctalk.notify.available", return_value=True):
            notify.mode({"enabled": True, "sound": False, "preview": False})
        self.assertEqual(self.run_notify()["sent"], 0)
        with self.assertRaises(ValueError):
            notify.mode({"sound": 1})


def benchmark() -> None:
    with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_STATE_HOME": root, "XDG_CONFIG_HOME": root + "/config", "XDG_CACHE_HOME": root + "/cache"}):
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [ACCOUNT]})
        conn = store.connect(prefs.store_dir(ACCOUNT["account"]) / "talk.db")
        conn.execute("BEGIN")
        for index in range(200):
            token = "room" + str(index)
            store.upsert_room(conn, {"token": token, "displayName": "Synthetic " + str(index), "type": 2,
                                    "lastActivity": 1500, "lastMessage": {"id": 1500}})
            conn.executemany("INSERT INTO messages(token,id,actor_type,actor_id,actor_name,timestamp,message,search_text) VALUES (?,?,?,?,?,?,?,?)",
                             ((token, mid, "users", "bob", "Synthetic Bob", mid, "Synthetic message", "Synthetic message") for mid in range(1, 1501)))
        conn.execute("COMMIT")
        conn.close()
        helper = str(Path(__file__).resolve().parents[1] / "bin/omanctalk")
        for command, limit, payload in (("chats", "500", {}), ("messages", "240", {"jid": "room0"})):
            samples = []
            for _ in range(6):
                start = time.perf_counter()
                result = subprocess.run([helper, command, "--limit", limit], input=json.dumps(payload), capture_output=True, text=True, check=True)
                samples.append((time.perf_counter() - start) * 1000)
                json.loads(result.stdout)
            print(f"{command}: cold={samples[0]:.1f}ms warm_min={min(samples[1:]):.1f}ms warm_max={max(samples[1:]):.1f}ms (200 rooms/300000 messages)")


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        benchmark()
    else:
        unittest.main()
