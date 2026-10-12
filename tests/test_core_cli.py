"""Offline stdin/stdout CLI roundtrips and lazy worker dispatch."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_dto import ACCOUNT, MirrorCase
import omanctalk_core as core
from nctalk import desktop, dto, prefs

HELPER = str(Path(__file__).resolve().parents[1] / "bin/omanctalk")


class CoreCLITests(MirrorCase):
    def call(self, command: str, payload=None, args=()) -> tuple[int, dict]:
        response = subprocess.run([HELPER, command, *args], input=json.dumps(payload or {}),
                                  text=True, capture_output=True, timeout=5)
        self.assertEqual(response.stderr, "")
        return response.returncode, json.loads(response.stdout)

    def test_cli_read_roundtrips(self) -> None:
        for command in ("chats", "messages", "members", "chat-details", "settings", "acknowledge"):
            code, result = self.call(command, {"jid": "room0", "account": ACCOUNT["account"]})
            self.assertEqual(code, 0, command)
            self.assertTrue(result["ok"])
        code, result = self.call("messages", {"jid": "room0"}, ("--limit", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(len(result["messages"]), 2)
        self.assertTrue(result["has_more"])

    def test_cli_status_about_setup(self) -> None:
        for command in ("status", "about", "setup", "update-check", "avatars"):
            code, result = self.call(command)
            self.assertEqual(code, 0, command)
            self.assertTrue(result["ok"])
            self.assertNotIn("synthetic-password", json.dumps(result))
        _, result = self.call("update-check")
        self.assertFalse(result["managed"] or result["available"])

    def test_cli_errors_and_unknown(self) -> None:
        code, result = self.call("bad-command")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        for command, payload in (("messages", {"jid": "missing"}), ("settings", {"settings": {"enter_sends": 1}}),
                                  ("presence", {}), ("teardown", {"confirm": "no"})):
            code, result = self.call(command, payload)
            self.assertEqual(code, 1)
            self.assertIsInstance(result["error"], str)
        response = subprocess.run([HELPER, "settings"], input="[1]", text=True, capture_output=True)
        self.assertEqual(response.returncode, 1)
        self.assertFalse(json.loads(response.stdout)["ok"])

    def test_lazy_actions_and_auth_signatures(self) -> None:
        worker = types.SimpleNamespace(run=Mock(return_value={"ok": True}))
        with patch("omanctalk_core.importlib.import_module", return_value=worker):
            self.assertTrue(core.dispatch("send", ["--limit", "5"], {"jid": "room0"})["ok"])
            worker.run.assert_called_once_with("send", ["--limit", "5"], {"jid": "room0"})
            worker.run.reset_mock()
            core.dispatch("login-start", [], {"server": "https://example.test"})
            worker.run.assert_called_once_with("login-start", {"server": "https://example.test"})
        with patch("omanctalk_core.importlib.import_module", side_effect=ModuleNotFoundError(name="nctalk.actions")):
            self.assertEqual(core.dispatch("send", [], {}), core.UNIMPLEMENTED)
        worker.run.side_effect = NotImplementedError
        with patch("omanctalk_core.importlib.import_module", return_value=worker):
            self.assertEqual(core.dispatch("send", [], {}), core.UNIMPLEMENTED)

    def test_chat_actions_injected_api_and_read_ack(self) -> None:
        client = Mock()
        api = types.SimpleNamespace(TalkClient=Mock(return_value=client))
        with patch("omanctalk_core.importlib.import_module", return_value=api):
            for action in ("read", "unread", "pin", "unpin", "archive", "unarchive", "mute", "unmute"):
                result = core.chat_action(ACCOUNT, {"jid": "room0", "action": action})
                self.assertTrue(result["ok"])
            client.mark_read.assert_called_once_with("room0", 8)
            client.mark_unread.assert_called_once_with("room0")
            self.assertEqual(client.set_favorite.call_count, 2)
            self.assertEqual(client.set_notification_level.call_args.args, ("room0", 1))
        with patch("omanctalk_core.importlib.import_module", side_effect=ModuleNotFoundError(name="nctalk.api")):
            self.assertTrue(core.chat_action(ACCOUNT, {"jid": "room0", "action": "read"})["ok"])
            self.assertEqual(core.chat_action(ACCOUNT, {"jid": "room0", "action": "pin"}), core.UNIMPLEMENTED)

    def test_lifecycle_mocked(self) -> None:
        with patch("nctalk.prefs.systemctl", return_value=1) as runner:
            self.assertTrue(core.dispatch("quit", [], {})["closed"])
            self.assertFalse(core.dispatch("launch", [], {})["closed"])
            self.assertFalse(core.dispatch("sync-mode", [], {"online": False})["online"])
            result = core.dispatch("teardown", [], {"confirm": "remove"})
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(runner.call_count, 5)
        self.assertTrue(core.dispatch("chat-presence", [], {"state": "typing"})["ok"])

    def test_voice_draft_lifecycle_and_security(self) -> None:
        code, result = self.call("voice-draft", {"action": "create"})
        self.assertEqual(code, 0)
        path = Path(result["path"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_bytes(b"OggSsynthetic-recording")
        code, finalized = self.call("voice-draft", {"action": "finalize", "path": str(path)})
        self.assertEqual(code, 0)
        self.assertEqual(finalized["size"], path.stat().st_size)
        self.assertEqual(self.call("voice-draft", {"action": "discard", "path": str(path)})[0], 0)
        self.assertFalse(path.exists())
        code, _ = self.call("voice-draft", {"action": "discard", "path": "/etc/passwd"})
        self.assertEqual(code, 1)

    def test_export_save_and_no_overwrite(self) -> None:
        destination = Path(self.temp.name) / "synthetic-export.txt"
        code, result = self.call("export-chat", {"jid": "room0", "destination": str(destination)})
        self.assertEqual(code, 0)
        self.assertEqual(result["messages"], 8)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.call("export-chat", {"jid": "room0", "destination": str(destination)})[0], 1)
        source = Path(self.temp.name) / "synthetic-media.txt"
        source.write_bytes(b"synthetic media")
        self.conn.execute("UPDATE messages SET local_path=? WHERE token='room0' AND id=8", (str(source),))
        saved = Path(self.temp.name) / "saved.txt"
        code, result = self.call("save-media", {"jid": "room0", "id": "8", "destination": str(saved)})
        self.assertEqual(code, 0)
        self.assertEqual(saved.read_bytes(), b"synthetic media")
        link = Path(self.temp.name) / "link"
        link.symlink_to(source)
        self.assertEqual(self.call("save-media", {"jid": "room0", "id": "8", "destination": str(link), "replace": True})[0], 1)

    def test_clipboard_and_opener_are_mocked(self) -> None:
        path = Path(self.temp.name) / "synthetic-file.txt"
        path.write_text("Synthetic", encoding="utf-8")
        def runner(command, **kwargs):
            kwargs["stdout"].write(b"text/plain\n" if "--list-types" in command else b"Synthetic clipboard")
            return types.SimpleNamespace(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/wl-paste"), patch("subprocess.run", side_effect=runner):
            self.assertEqual(desktop.clipboard(), {"ok": True, "kind": "text", "text": "Synthetic clipboard"})
        with patch("shutil.which", return_value="/usr/bin/xdg-open"), patch("subprocess.Popen") as opener:
            self.assertTrue(desktop.run("open-media", {"path": str(path)})["ok"])
            opener.assert_called_once()
        draft = desktop.allocate("staging", ".png")
        self.assertTrue(desktop.run("discard-stage", {"path": draft.as_uri()})["discarded"])
        self.assertFalse(desktop.run("discard-stage", {"path": draft.as_uri()})["discarded"])
