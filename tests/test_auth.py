"""Synthetic Login Flow v2 tests; systemctl is always injected."""
from __future__ import annotations

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
from nctalk import api, auth, prefs
from fake_talk import FakeTalkServer


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = FakeTalkServer()
        self.addCleanup(self.server.close)
        self.calls = []
        self.runner_patch = patch.object(auth.subprocess, "run", side_effect=self.runner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)

    def runner(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    def add(self, **extra: object) -> dict:
        payload = {"server": self.server.url, "user": self.server.user,
                   "app_password": self.server.password, "write_guard_prefix": "omanctalk-test-", **extra}
        return auth.run("account-add", payload, runner=self.runner)

    def start(self) -> dict:
        return auth.run("login-start", {"server": self.server.url}, runner=self.runner)

    def finish(self, start: dict) -> dict:
        return auth.run("login-finish", {k: start[k] for k in ("server", "poll_token", "poll_endpoint")}, runner=self.runner)

    def test_login_flow_and_permissions(self) -> None:
        start = self.start()
        self.assertTrue(start["ok"])
        self.assertEqual(start["login_url"], self.server.url + "/login/synthetic")
        result = self.finish(start)
        self.assertTrue(result["ok"])
        self.assertTrue(result["sync_started"])
        self.assertEqual(result["account"], prefs.account_id(self.server.url, self.server.user))
        registry = prefs.load_accounts()
        self.assertEqual(registry[0]["app_password"], self.server.password)
        self.assertEqual(prefs.accounts_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual(prefs.state_dir().stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.calls[0], ["systemctl", "--user", "enable", "--now", prefs.unit(result["account"])])
        self.assertNotIn(self.server.password, json.dumps(result))
        login_requests = [r for r in self.server.requests if r["path"].startswith(("/index.php/login", "/login/"))]
        self.assertTrue(all(not r["has_auth"] for r in login_requests))

    def test_pending_poll_does_not_write_registry(self) -> None:
        start = self.start()
        self.server.fail_next(404, "/login/v2/poll")
        result = self.finish(start)
        self.assertEqual(result, {"ok": False, "pending": True})
        self.assertFalse(prefs.accounts_file().exists())
        self.assertEqual(self.calls, [])

    def test_repeated_account_add_is_idempotent(self) -> None:
        for _ in range(2):
            result = self.add()
            self.assertTrue(result["ok"])
        registry = prefs.load_accounts()
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry[0]["write_guard_prefix"], "omanctalk-test-")
        self.assertTrue(registry[0]["default"])

    def test_login_does_not_remove_existing_guard(self) -> None:
        self.assertTrue(self.add()["ok"])
        self.assertTrue(self.finish(self.start())["ok"])
        self.assertEqual(prefs.load_accounts()[0]["write_guard_prefix"], "omanctalk-test-")
        self.assertFalse(self.add(write_guard_prefix="broader-")["ok"])
        self.assertEqual(prefs.load_accounts()[0]["write_guard_prefix"], "omanctalk-test-")

    def test_account_registry_symlink_rejected(self) -> None:
        prefs.state_dir().mkdir(parents=True)
        target = Path(self.temp.name) / "synthetic-registry"
        target.write_text('{}', encoding="utf-8")
        prefs.accounts_file().symlink_to(target)
        self.assertFalse(self.add()["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), '{}')

    def test_bad_credentials_never_saved_or_returned(self) -> None:
        result = self.add(app_password="synthetic-wrong-password")
        self.assertFalse(result["ok"])
        self.assertNotIn("synthetic-wrong-password", json.dumps(result))
        self.assertFalse(prefs.accounts_file().exists())
        self.assertEqual(self.calls, [])

    def test_failed_unit_start_keeps_successful_login(self) -> None:
        def failed(args, **kwargs):
            return subprocess.CompletedProcess(args, 1)
        result = auth.run("account-add", {"server": self.server.url, "user": self.server.user,
                                          "app_password": self.server.password}, runner=failed)
        self.assertTrue(result["ok"])
        self.assertFalse(result["sync_started"])
        self.assertEqual(len(prefs.load_accounts()), 1)

    def test_unit_timeout_is_bounded_and_no_password_output(self) -> None:
        def timeout(args, **kwargs):
            self.assertEqual(kwargs["timeout"], 10)
            raise subprocess.TimeoutExpired(args, 10)
        result = auth.run("account-add", {"server": self.server.url, "user": self.server.user,
                                          "app_password": self.server.password}, runner=timeout)
        self.assertTrue(result["ok"])
        self.assertFalse(result["sync_started"])

    def test_remove_preserves_archive_and_is_idempotent(self) -> None:
        identity = self.add()["account"]
        archive = prefs.store_dir(identity)
        archive.mkdir(parents=True)
        (archive / "synthetic-marker").write_text("synthetic", encoding="utf-8")
        result = auth.run("account-remove", {"account": identity}, runner=self.runner)
        self.assertTrue(result["removed"])
        self.assertTrue(result["archive_kept"])
        self.assertTrue((archive / "synthetic-marker").exists())
        self.assertEqual(prefs.load_accounts(), [])
        self.assertEqual(self.calls[-1], ["systemctl", "--user", "disable", "--now", prefs.unit(identity)])
        self.assertFalse(auth.run("account-remove", {"account": identity}, runner=self.runner)["removed"])

    def test_remove_failure_keeps_registry(self) -> None:
        identity = self.add()["account"]
        def failed(args, **kwargs):
            return subprocess.CompletedProcess(args, 1)
        self.assertFalse(auth.run("account-remove", {"account": identity}, runner=failed)["ok"])
        self.assertEqual(len(prefs.load_accounts()), 1)

    def test_untrusted_poll_origin_rejected_before_http(self) -> None:
        start = self.start()
        count = len(self.server.requests)
        start["poll_endpoint"] = "https://foreign.invalid/poll"
        self.assertFalse(self.finish(start)["ok"])
        self.assertEqual(len(self.server.requests), count)

    def test_untrusted_login_response_and_server_mismatch(self) -> None:
        with patch.object(api, "login_flow_start", return_value={"login": "https://foreign.invalid/login", "poll": {"token": "synthetic", "endpoint": self.server.url + "/poll"}}):
            self.assertFalse(self.start()["ok"])
        start = self.start()
        with patch.object(api, "login_flow_poll", return_value={"server": "https://foreign.invalid", "loginName": "synthetic", "appPassword": "synthetic"}):
            self.assertFalse(self.finish(start)["ok"])
        self.assertFalse(prefs.accounts_file().exists())

    def test_invalid_urls_and_request_fields(self) -> None:
        for url in ("http://external.invalid", "https://user:pass@example.invalid", "https://example.invalid/?secret=x", "not-a-url"):
            self.assertFalse(auth.run("login-start", {"server": url}, runner=self.runner)["ok"])
        self.assertFalse(self.add(write_guard_prefix="")["ok"])
        self.assertFalse(auth.run("account-remove", {"account": "../../unsafe"}, runner=self.runner)["ok"])
        self.assertFalse(auth.run("missing", {}, runner=self.runner)["supported"])
        self.assertEqual(self.calls, [])

    def test_concurrent_registry_updates_are_locked(self) -> None:
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.add())) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(len(prefs.load_accounts()), 1)
        self.assertEqual(prefs.accounts_file().stat().st_mode & 0o777, 0o600)

    def test_account_normalization_collision_refused(self) -> None:
        self.assertTrue(self.add()["ok"])
        with patch.object(api.TalkClient, "capabilities", return_value={}):
            result = self.add(user=self.server.user.upper())
        self.assertFalse(result["ok"])
        self.assertEqual(prefs.load_accounts()[0]["user"], self.server.user)

    def test_lock_symlink_rejected_without_overwriting(self) -> None:
        prefs.state_dir().mkdir(parents=True)
        target = Path(self.temp.name) / "synthetic-target"
        target.write_text("synthetic untouched", encoding="utf-8")
        (prefs.state_dir() / "accounts.json.lock").symlink_to(target)
        self.assertFalse(self.add()["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "synthetic untouched")


if __name__ == "__main__":
    unittest.main()
