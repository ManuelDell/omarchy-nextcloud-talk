"""Opt-in live smoke test; exactly one owned room, never enumerate other rooms.

Run with OMANCTALK_LIVE=1 OMANCTALK_LIVE_WRITE=1. Credentials stay in memory;
assertion failures report only step/status, never response content or tokens.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from nctalk import actions, api


@unittest.skipUnless(os.environ.get("OMANCTALK_LIVE") == "1" and
                     os.environ.get("OMANCTALK_LIVE_WRITE") == "1", "live writes disabled")
class LiveWriteTests(unittest.TestCase):
    def test_owned_room_write_lifecycle(self) -> None:
        values = {}
        for line in (Path.home() / ".config/omanctalk/credentials").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"NCTALK_URL", "NCTALK_USER", "NCTALK_APP_PASSWORD"}:
                values[key.strip()] = value.strip().strip("\"'")
        if not all(values.get(k) for k in ("NCTALK_URL", "NCTALK_USER", "NCTALK_APP_PASSWORD")):
            self.fail("live credentials incomplete")
        client = actions.ShareClient(values["NCTALK_URL"], values["NCTALK_USER"],
                                     values["NCTALK_APP_PASSWORD"], "omanctalk-test-")
        values.clear()
        client.max_retries = 0
        client.timeout = 30
        jid = None
        step = "create"
        counts = {"rooms": 0, "sent": 0, "edited": 0, "reacted": 0, "deleted": 0, "uploaded": 0, "downloaded": 0}
        try:
            with tempfile.TemporaryDirectory(prefix="omanctalk-live-") as directory:
                root = Path(directory)
                db = root / "unused.db"
                created = actions._run("create-group", db, client, {
                    "name": "omanctalk-test-" + uuid.uuid4().hex, "participants": []})
                jid = actions.token(created.get("jid"))
                counts["rooms"] += 1
                def run(command: str, **payload: object) -> dict:
                    result = actions._run(command, db, client, {"jid": jid, **payload})
                    if not result.get("ok"):
                        raise RuntimeError("live operation failed")
                    return result
                step = "send"
                sent = run("send", text="Synthetic OmaNCTalk smoke fixture", local_id=uuid.uuid4().hex)
                mid = sent.get("message_id")
                if not mid:
                    raise RuntimeError("missing live message identifier")
                counts["sent"] += 1
                step = "read-send"
                history, _, _ = client.chat_history(jid, limit=50)
                if not any(str(m.get("id")) == mid for m in history):
                    raise RuntimeError("live message not found")
                step = "edit"
                run("edit", id=mid, text="Synthetic OmaNCTalk edited fixture")
                counts["edited"] += 1
                step = "read-edit"
                history, _, _ = client.chat_history(jid, limit=50)
                if not any(str(m.get("id")) == mid and m.get("message") == "Synthetic OmaNCTalk edited fixture" for m in history):
                    raise RuntimeError("live edit not found")
                step = "react"
                run("react", id=mid, emoji="👍")
                counts["reacted"] += 1
                step = "read-react"
                if "👍" not in client.reaction_details(jid, mid):
                    raise RuntimeError("live reaction not found")
                run("react", id=mid, emoji="")
                if "👍" in client.reaction_details(jid, mid):
                    raise RuntimeError("live reaction removal failed")
                step = "upload"
                source = root / "synthetic.txt"
                source.write_bytes(b"Synthetic OmaNCTalk upload fixture\n")
                result = run("files", paths=[str(source)], caption="Synthetic upload caption")
                if result.get("count") != 1:
                    raise RuntimeError("live upload count mismatch")
                counts["uploaded"] += 1
                step = "read-upload"
                history, _, _ = client.chat_history(jid, limit=50)
                file_params = []
                for msg in history:
                    params = msg.get("messageParameters")
                    if isinstance(params, dict) and isinstance(params.get("file"), dict):
                        file_params.append(params["file"])
                if len(file_params) != 1:
                    raise RuntimeError("live file count mismatch")
                step = "download"
                fid = int(file_params[0]["id"])
                target = client.download_file(fid, root / "downloaded.txt")
                if target.read_bytes() != source.read_bytes():
                    raise RuntimeError("live file byte count mismatch")
                counts["downloaded"] += 1
                step = "delete"
                run("delete", id=mid)
                counts["deleted"] += 1
                step = "read-delete"
                history, _, _ = client.chat_history(jid, limit=50)
                if not any((str(m.get("id")) == mid and (m.get("deleted") or m.get("systemMessage") == "message_deleted")) or
                           (m.get("systemMessage", "").startswith("message_deleted") and isinstance(m.get("parent"), dict) and str(m["parent"].get("id")) == mid)
                           for m in history):
                    raise RuntimeError("live deletion not found")
        except api.ApiError as exc:
            self.fail("live step=" + step + " http=" + str(exc.status))
        except Exception:
            self.fail("live step=" + step + " failed")
        finally:
            if jid is not None:
                try:
                    client.delete_room(jid)
                except Exception:
                    self.fail("live cleanup failed")
        print("live counters: " + " ".join(k + "=" + str(v) for k, v in counts.items()))


if __name__ == "__main__":
    unittest.main()
