"""Independent popup watermarks; no message text enters process arguments."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable
from . import prefs

BURST = 5


def available() -> bool:
    import shutil
    return shutil.which("notify-send") is not None


def dnd() -> bool:
    path = prefs.state_dir().parent / "omarchy/notifications.json"
    try:
        if path.stat().st_size > 65536:
            return False
        return prefs.read_json(path, {}).get("dnd") is True
    except (OSError, ValueError, AttributeError):
        return False


def deliver(target: dict[str, Any] | None, preview: bool = False) -> bool:
    import subprocess
    if not available():
        return False
    helper = os.path.join(os.path.dirname(os.path.dirname(__file__)), "omanctalk")
    try:
        child = subprocess.Popen([helper, "notify-open"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True, start_new_session=True)
        assert child.stdin is not None
        # Preview policy matches the hardened original: public args are redacted.
        child.stdin.write(json.dumps({"target": target or {}, "preview": preview}).encode())
        child.stdin.close()
        return True
    except (OSError, AssertionError):
        return False


def play_sound() -> bool:
    import subprocess
    path = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"
    if dnd() or not os.path.isfile(path) or not os.access("/usr/bin/pw-play", os.X_OK):
        return False
    try:
        subprocess.Popen(["/usr/bin/pw-play", path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def collect(accounts: list[dict[str, Any]], value: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    from .dto import Reader
    result = {}
    for account in accounts:
        if not (prefs.store_dir(account["account"]) / "talk.db").exists():
            continue
        reader = Reader(account, value)
        try:
            result[account["account"]] = reader.chats(limit=10000)
        finally:
            reader.close()
    return result


def mode(payload: dict[str, Any]) -> dict[str, Any]:
    from .dto import snapshot
    update = {k: payload[k] for k in ("enabled", "preview", "sound") if k in payload}
    if not update or any(type(v) is not bool for v in update.values()):
        raise ValueError("notification settings must be on or off")
    if update.get("enabled") and not available():
        raise ValueError("desktop notifications require notify-send")
    chats = collect(prefs.load_accounts(), prefs.load_preferences()) if update.get("enabled") else {}
    def apply(value: dict[str, Any]) -> None:
        value["notifications"].update(update)
        for account, rows in chats.items():
            state = prefs.account_state(value, account)
            state["notified"] = {c["jid"]: snapshot(c) for c in rows}
            state["notify_seeded"] = True
            value["stores"][account] = state
    value = prefs.update_preferences(apply)
    return {"ok": True, "kind": "notify-mode", "notifications": value["notifications"]}


def run(active: str = "", skip_jid: str = "", sender: Callable[..., bool] | None = None,
        sound_player: Callable[[], bool] | None = None, is_dnd: Callable[[], bool] | None = None) -> dict[str, Any]:
    import fcntl
    root = prefs.state_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / "notify.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _run(active, skip_jid, sender or deliver, sound_player or play_sound, is_dnd or dnd)
    finally:
        os.close(fd)


def _run(active: str, skip_jid: str, sender: Callable[..., bool], sound_player: Callable[[], bool],
         is_dnd: Callable[[], bool]) -> dict[str, Any]:
    from .dto import snapshot
    value = prefs.load_preferences()
    settings = value["notifications"]
    result: dict[str, Any] = {"ok": True, "kind": "notify", "enabled": settings["enabled"], "sent": 0}
    if not settings["enabled"]:
        return result
    if sender is deliver and not available():
        return dict(result, available=False)
    chats = collect(prefs.load_accounts(), value)
    watermarks, pending, seeded = {}, [], []
    quiet = is_dnd() or value["closed"]
    for account, rows in chats.items():
        state = prefs.account_state(value, account)
        seen = state["notified"]
        watermark = {c["jid"]: snapshot(c) for c in rows}
        watermarks[account] = watermark
        if not state.get("notify_seeded", bool(seen)):
            seeded.append(account)
            continue
        if state["notifications_muted"] or quiet:
            continue
        for chat in rows:
            previous = seen.get(chat["jid"], {})
            if previous.get("message_id") == chat["last_message_id"]:
                continue
            if chat["timestamp"] < previous.get("timestamp", 0):
                continue
            if chat["timestamp"] == previous.get("timestamp", 0) and chat["unread"] <= previous.get("unread", 0) and not previous.get("message_id"):
                continue
            if chat["last_from_me"] or chat["muted"] or chat["archived"] or (account == active and chat["jid"] == skip_jid):
                continue
            pending.append((account, chat, previous))
    pending.sort(key=lambda item: (-item[1]["timestamp"], item[0], item[1]["jid"]))
    failed = 0
    for account, chat, previous in pending[:BURST]:
        if sender({"account": account, "jid": chat["jid"]}, settings["preview"]):
            result["sent"] += 1
        else:
            failed += 1
            watermarks[account][chat["jid"]] = previous
    if len(pending) > BURST:
        if sender(None, False):
            result["sent"] += 1
        else:
            failed += len(pending) - BURST
            for account, chat, previous in pending[BURST:]:
                watermarks[account][chat["jid"]] = previous
    def apply(current: dict[str, Any]) -> None:
        for account, watermark in watermarks.items():
            state = prefs.account_state(current, account)
            state.update(notified=watermark, notify_seeded=True)
            current["stores"][account] = state
    prefs.update_preferences(apply)
    result.update(failed=failed, pending=len(pending), sound=bool(result["sent"] and settings["sound"] and not quiet and sound_player()))
    if seeded:
        result.update(seeded=True, seeded_accounts=seeded)
    return result


def open_notification(payload: dict[str, Any]) -> dict[str, Any]:
    import shutil
    import subprocess
    import selectors
    target = payload.get("target", {})
    if not isinstance(target, dict):
        raise ValueError("invalid notification target")
    executable = shutil.which("notify-send")
    result = {"ok": True, "kind": "notify-open", "opened": False}
    if not executable:
        return result
    key = str(target.get("account", "")) + "\n" + str(target.get("jid", ""))
    ids_path = prefs.state_dir() / "notify-ids.json"
    ids = prefs.read_json(ids_path, {})
    command = [executable, "--app-name=OmaNCTalk", "--urgency=normal", "--category=im.received", "--expire-time=10000"]
    if target.get("jid"):
        command += ["--print-id", "--action=default=Open chat"]
        previous = ids.get(key, {})
        if time.time() - previous.get("at", 0) < 86400 and previous.get("id"):
            command.append("--replace-id=" + str(previous["id"]))
    command += ["--", "OmaNCTalk", "New messages. Open OmaNCTalk to read them."]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return result
    data = b""
    recorded = False
    deadline = time.monotonic() + 70
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while len(data) <= 4096:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    break
                part = os.read(process.stdout.fileno(), 1024)
                if not part:
                    break
                data += part
                if not recorded and b"\n" in data:
                    recorded = True
                    first = data.split(b"\n", 1)[0].strip()
                    if first.isdigit() and 0 < int(first) < 2**31:
                        def remember(value: dict[str, Any]) -> None:
                            value[key] = {"id": int(first), "at": int(time.time())}
                            for old in sorted(value, key=lambda k: value[k].get("at", 0))[:-2048]:
                                value.pop(old, None)
                        prefs.locked_update("notify-ids.json", {}, remember)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
    if b"default" not in data.split() or not target.get("jid"):
        return result
    shell = shutil.which("omarchy-shell")
    if shell:
        surface = "openDropdown" if prefs.load_preferences()["notify_reply"] else "openApp"
        response = subprocess.run([shell, "io.github.manueldell.omanctalk", surface, json.dumps({
            "account": str(target.get("account", "")), "jid": str(target["jid"])})],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result["opened"] = response.returncode == 0
    return result
