"""Private state, registry and preference validation; never calls systemd."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import dto, prefs


class PrefsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "XDG_CONFIG_HOME": self.temp.name + "/config"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_identity_and_paths(self) -> None:
        self.assertEqual(prefs.account_id("https://EXAMPLE.test:8443/nextcloud", "Alice"), "alice@example.test_8443")
        self.assertEqual(prefs.account_id("example.test", "Alice"), "alice@example.test")
        self.assertEqual(prefs.store_dir("alice@example.test"), Path(self.temp.name) / "omanctalk/stores/alice@example.test")
        for identity in ("../other", "/tmp", "..", "", "A@B"):
            with self.assertRaises(ValueError):
                prefs.store_dir(identity)

    def test_atomic_modes_and_retained_scopes(self) -> None:
        prefs.settings("alice@example.test", {"enter_sends": False, "send_read_receipts": True})
        prefs.settings("bob@example.test", {"send_read_receipts": False})
        path = prefs.state_dir() / "preferences.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(prefs.settings("alice@example.test")["send_read_receipts"])
        self.assertFalse(prefs.settings("bob@example.test")["enter_sends"])
        self.assertFalse(prefs.settings("bob@example.test")["send_read_receipts"])
        self.assertFalse(any(p.name.startswith(".preferences") for p in path.parent.iterdir()))

    def test_all_settings_schema(self) -> None:
        result = prefs.settings("")
        self.assertEqual(set(prefs.DEFAULTS) - set(result), set())
        for key, default in prefs.DEFAULTS.items():
            self.assertIs(type(result[key]), type(default), key)
        self.assertIs(type(result["send_read_receipts"]), bool)
        self.assertIs(type(result["account_notifications"]), bool)
        self.assertEqual(result["signature"], {"enabled": False, "name": "", "position": "top"})

    def test_valid_settings(self) -> None:
        values = {"dropdown_rows": 9, "composer_max_lines": 10, "rail_width": 640,
                  "rail_density": "compact", "time_format": "24h", "account_notifications": False,
                  "signature": {"enabled": True, "name": "Synthetic Alice", "position": "bottom"}}
        result = prefs.settings("alice@example.test", values)
        for key, value in values.items():
            self.assertEqual(result[key], value)

    def test_validation_rejects_types_and_unknown(self) -> None:
        invalid = [{"unknown": True}, {"show_avatars": 1}, {"rail_width": True}, {"rail_width": 641},
                   {"dropdown_rows": 6}, {"composer_max_lines": "6"}, {"time_format": "25h"},
                   {"signature": {"name": "bad\nname"}}, {"signature": {"name": "*bad"}},
                   {"signature": {"enabled": True, "name": ""}}, {"signature": {"other": 1}},
                   {"rail_density": []}, {"signature": {"position": []}}, {"account_notifications": "yes"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prefs.settings("", value)
        self.assertFalse((prefs.state_dir() / "preferences.json").exists())

    def test_autostart_injected_runner(self) -> None:
        account = {"account": "alice@example.test", "server": "https://example.test", "user": "alice", "app_password": "synthetic"}
        prefs.atomic_json(prefs.accounts_file(), {"accounts": [account]})
        runner = Mock(return_value=Mock(returncode=0))
        self.assertEqual(prefs.apply_start_at_login(True, [account], runner), 1)
        self.assertEqual(runner.call_args.args[0], ["systemctl", "--user", "enable", "omanctalk-sync@alice@example.test.service"])
        prefs.settings(account["account"], {"start_at_login": False}, runner)
        self.assertEqual(runner.call_args.args[0][2], "disable")
        runner.return_value.returncode = 1
        with self.assertRaises(ValueError):
            prefs.settings(account["account"], {"start_at_login": True}, runner)
        self.assertFalse(prefs.settings(account["account"])["start_at_login"])

    def test_a_credentials_file_never_creates_an_account(self) -> None:
        # Accounts come from the login flow (or account-add), never from files lying around.
        root = Path(os.environ["XDG_CONFIG_HOME"]) / "omanctalk"
        root.mkdir(parents=True)
        (root / "credentials").write_text("NCTALK_URL=https://example.test\nNCTALK_USER=Alice\nNCTALK_APP_PASSWORD=synthetic-password\n", encoding="utf-8")
        result = dto.status()
        self.assertFalse(result["authenticated"])
        self.assertEqual(prefs.load_accounts(), [])
        self.assertFalse(prefs.accounts_file().exists())

    def test_empty_onboarding(self) -> None:
        result = dto.status()
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["setup"]["complete"])
        self.assertFalse(result["rail_ready"])

    def test_corrupt_state_not_overwritten(self) -> None:
        root = prefs.state_dir()
        root.mkdir()
        path = root / "preferences.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            prefs.settings("", {"enter_sends": False})
        self.assertEqual(path.read_text(), "{broken")

    def test_session_closed_and_offline_autostart(self) -> None:
        with patch("nctalk.prefs.session_key", return_value="boot:session1"):
            prefs.update_preferences(lambda value: value.update(closed=True))
            self.assertTrue(prefs.load_preferences()["closed"])
        with patch("nctalk.prefs.session_key", return_value="boot:session2"):
            self.assertFalse(prefs.load_preferences()["closed"])
        account = {"account": "alice@example.test"}
        prefs.update_account(account["account"], lambda state: state.update(online=False))
        runner = Mock(return_value=Mock(returncode=0))
        self.assertEqual(prefs.apply_start_at_login(True, [account], runner), 0)
        runner.assert_not_called()
        self.assertEqual(prefs.apply_start_at_login(False, [account], runner), 1)

    def test_snapshot_validation(self) -> None:
        value = {"stores": {"alice@example.test": {"signature": None, "hidden_rooms": None,
                 "acknowledged_unread": {"room0": {"unread": "bad"}, "room1": {"unread": 2, "timestamp": 5}}}}}
        state = prefs.account_state(value, "alice@example.test")
        self.assertEqual(state["hidden_rooms"], [])
        self.assertNotIn("room0", state["acknowledged_unread"])
        self.assertEqual(state["acknowledged_unread"]["room1"]["unread"], 2)
        self.assertFalse(state["signature"]["enabled"])

    def test_registry_accepts_actions_variants(self) -> None:
        row = {"id": "alice@example.test", "server": "https://example.test", "user": "alice", "app_password": "synthetic"}
        for value in ([row], {"accounts": [row]}, {"accounts": {"alice@example.test": row}}):
            prefs.atomic_json(prefs.accounts_file(), value)
            self.assertEqual(prefs.load_accounts()[0]["account"], "alice@example.test")
