"""Read-only once-sync; temporary mirror is removed, output is counters only.

No credentials registry is written: credentials remain in memory. Run only
with OMANCTALK_LIVE=1. All mutation methods are additionally blocked here.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from nctalk.api import GuardError, TalkClient
from nctalk.sync import SyncDaemon


class ReadOnlyClient(TalkClient):
    def _open(self, path: str, method: str = "GET", data: Any = None, **kwargs: Any) -> Any:
        if method not in ("GET", "REPORT", "PROPFIND", "SEARCH"):
            raise GuardError()
        return super()._open(path, method, data, **kwargs)


@unittest.skipUnless(os.environ.get("OMANCTALK_LIVE") == "1", "live access disabled")
class LiveReadOnlyTests(unittest.TestCase):
    def test_once_sync(self) -> None:
        path = Path.home() / ".config/omanctalk/credentials"
        if not path.is_file():
            self.skipTest("credentials unavailable")
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"NCTALK_URL", "NCTALK_USER", "NCTALK_APP_PASSWORD"}:
                values[key.strip()] = value.strip().strip("\"'")
        if not all(values.get(key) for key in ("NCTALK_URL", "NCTALK_USER", "NCTALK_APP_PASSWORD")):
            self.skipTest("credentials incomplete")
        client = ReadOnlyClient(values["NCTALK_URL"], values["NCTALK_USER"],
                                values["NCTALK_APP_PASSWORD"], "omanctalk-test-")
        with tempfile.TemporaryDirectory(prefix="omanctalk-live-") as tmp:
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                with SyncDaemon("live-readonly", client, Path(tmp) / "mirror") as daemon:
                    counts = daemon.run_once(backfill_budget=5)
                    self.assertTrue(all(type(value) is int for value in counts.values()))
                    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
