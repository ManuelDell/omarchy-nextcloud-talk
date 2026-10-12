"""Private account registry and atomic, locked UI preferences."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

DEFAULTS: dict[str, Any] = {
    "show_unread_count": True, "check_updates_on_launch": False,
    "start_at_login": True, "dropdown_rows": 7, "composer_max_lines": 6,
    "time_format": "auto", "auto_download_media": False,
    "read_on_reply": False, "show_online": False, "notify_reply": True,
    "enter_sends": True, "show_avatars": True, "auto_refresh_avatars": False,
    "rail_density": "comfortable", "rail_width": 0,
}


def state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME", "")
    return (Path(root) if os.path.isabs(root) else Path.home() / ".local/state") / "omanctalk"


def accounts_file() -> Path:
    return state_dir() / "accounts.json"


def account_id(server: str, user: str) -> str:
    from urllib.parse import urlsplit
    import re
    parsed = urlsplit(server if "://" in server else "https://" + server)
    host = (parsed.hostname or "").lower()
    if parsed.port:
        host += "_" + str(parsed.port)
    value = (user + "@" + host).lower()
    value = re.sub(r"[^a-z0-9._@-]", "_", value)
    if not user or not host or value in {".", ".."}:
        raise ValueError("invalid account identity")
    return value


def store_dir(account: str) -> Path:
    import re
    if not re.fullmatch(r"[a-z0-9._@-]+", account) or account in {".", ".."}:
        raise ValueError("invalid account identity")
    return state_dir() / "stores" / account


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("state too large")
        return json.loads(data)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError):
        raise ValueError("invalid private state") from None


def atomic_json(path: Path, value: Any) -> None:
    import tempfile
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def locked_update(name: str, default: Any, update: Callable[[Any], None]) -> Any:
    import fcntl
    root = state_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    fd = os.open(root / (name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = read_json(root / name, default)
        update(value)
        atomic_json(root / name, value)
        return value
    finally:
        os.close(fd)


def load_accounts() -> list[dict[str, Any]]:
    value = read_json(accounts_file(), {})
    rows = value.get("accounts", []) if isinstance(value, dict) else value
    if isinstance(rows, dict):
        rows = [dict(v, account=k) for k, v in rows.items() if isinstance(v, dict)]
    if not isinstance(rows, list):
        raise ValueError("invalid account registry")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid account registry")
        item = dict(row)
        identity = item.get("account") or item.get("id") or account_id(item.get("server", ""), item.get("user", ""))
        store_dir(identity)
        item["account"] = identity
        if isinstance(value, dict) and value.get("default"):
            item["default"] = identity == value["default"]
        result.append(item)
    return result


def snapshots(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in list(value.items())[:10000]:
        if not isinstance(key, str) or not isinstance(item, dict):
            continue
        try:
            result[key[:4096]] = {"unread": max(0, min(10**9, int(item.get("unread", 0)))),
                                  "timestamp": max(0, min(10**10, int(item.get("timestamp", 0)))),
                                  "message_id": str(item.get("message_id", ""))[:256]}
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def account_state(value: dict[str, Any], account: str) -> dict[str, Any]:
    raw = value.get("stores", {}).get(account, {})
    if not isinstance(raw, dict):
        raw = {}
    signature = raw.get("signature", {})
    if not isinstance(signature, dict):
        signature = {}
    name = " ".join("".join(c for c in str(signature.get("name", "")) if c.isprintable() and c != "*").split())[:40]
    hidden = raw.get("hidden_rooms", [])
    return {"online": raw.get("online") is not False,
            "send_read_receipts": raw.get("send_read_receipts") is True,
            "signature": {"enabled": signature.get("enabled") is True and bool(name), "name": name,
                          "position": "bottom" if signature.get("position") == "bottom" else "top"},
            "notifications_muted": raw.get("notifications_muted") is True,
            "acknowledged_unread": snapshots(raw.get("acknowledged_unread")),
            "notified": snapshots(raw.get("notified")),
            "notify_seeded": raw.get("notify_seeded", bool(raw.get("notified"))) is True,
            "hidden_rooms": [t for t in hidden[:10000] if isinstance(t, str) and len(t) <= 4096] if isinstance(hidden, list) else []}


def session_key() -> str:
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        boot = ""
    return boot + ":" + os.environ.get("XDG_SESSION_ID", "")


def load_preferences() -> dict[str, Any]:
    raw = read_json(state_dir() / "preferences.json", {})
    if not isinstance(raw, dict):
        raise ValueError("invalid preferences")
    value = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw:
            try:
                validate_settings({key: raw[key]})
            except ValueError:
                continue
            value[key] = raw[key]
    stores = raw.get("stores", {})
    notifications = raw.get("notifications", {})
    if not isinstance(stores, dict) or not isinstance(notifications, dict):
        raise ValueError("invalid preferences")
    value.update(version=1, stores=stores,
                 closed=raw.get("closed") is True and raw.get("closed_session") == session_key())
    value["notifications"] = {k: notifications.get(k, d) is True
                              for k, d in (("enabled", False), ("preview", True), ("sound", True))}
    return value


def update_preferences(update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    def apply(raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            raise ValueError("invalid preferences")
        value = load_preferences()
        update(value)
        value["closed_session"] = session_key() if value["closed"] else ""
        raw.clear()
        raw.update(value)
    return locked_update("preferences.json", {}, apply)


def update_account(account: str, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    def apply(value: dict[str, Any]) -> None:
        state = account_state(value, account)
        update(state)
        value["stores"][account] = state
    return update_preferences(apply)


def validate_settings(update: Any) -> None:
    if not isinstance(update, dict):
        raise ValueError("settings must be an object")
    for key, val in update.items():
        if key in {"send_read_receipts", "account_notifications"} or (key in DEFAULTS and type(DEFAULTS[key]) is bool):
            valid = type(val) is bool
        elif key == "dropdown_rows":
            valid = type(val) is int and val in (5, 7, 9)
        elif key == "composer_max_lines":
            valid = type(val) is int and val in (4, 6, 8, 10)
        elif key == "rail_width":
            valid = type(val) is int and 0 <= val <= 640
        elif key == "time_format":
            valid = type(val) is str and val in ("auto", "12h", "24h")
        elif key == "rail_density":
            valid = type(val) is str and val in ("comfortable", "compact")
        elif key == "signature":
            valid = isinstance(val, dict) and not (set(val) - {"enabled", "name", "position"})
            if valid:
                name = val.get("name", "")
                valid = (type(val.get("enabled", False)) is bool and isinstance(name, str)
                         and len(name) <= 40 and all(c.isprintable() and c != "*" for c in name)
                         and val.get("position", "top") in ("top", "bottom")
                         and (not val.get("enabled") or bool(name.strip())))
        else:
            valid = False
        if not valid:
            raise ValueError("unsupported setting value")


def settings(account: str, update: Any = None, runner: Any = None) -> dict[str, Any]:
    if update is not None:
        validate_settings(update)
        if "start_at_login" in update:
            apply_start_at_login(update["start_at_login"], load_accounts(), runner)
        def apply(value: dict[str, Any]) -> None:
            state = account_state(value, account)
            for key, val in update.items():
                if key == "account_notifications":
                    state["notifications_muted"] = not val
                elif key == "signature":
                    state[key] = dict(state[key], **val)
                    state[key]["name"] = " ".join(state[key]["name"].split())
                    state[key]["enabled"] = bool(state[key]["enabled"] and state[key]["name"])
                elif key == "send_read_receipts":
                    state[key] = val
                else:
                    value[key] = val
            value["stores"][account] = state
        value = update_preferences(apply)
    else:
        value = load_preferences()
    state = account_state(value, account)
    return {"ok": True, "kind": "settings", "account": account,
            **{key: value[key] for key in DEFAULTS},
            "send_read_receipts": state["send_read_receipts"], "signature": state["signature"],
            "account_notifications": not state["notifications_muted"]}


def unit(account: str) -> str:
    store_dir(account)
    return "omanctalk-sync@" + account + ".service"


def systemctl(operation: str, accounts: list[dict[str, Any]], runner: Any = None) -> int:
    if not accounts:
        return 0
    import subprocess
    if runner is None:
        runner = subprocess.run
    try:
        result = runner(["systemctl", "--user", operation, *[unit(a["account"]) for a in accounts]],
                        timeout=5, capture_output=True)
        if result.returncode:
            raise ValueError("sync unit operation failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("sync unit operation failed") from exc
    return len(accounts)


def apply_start_at_login(enabled: bool, accounts: list[dict[str, Any]], runner: Any = None) -> int:
    if enabled:
        value = load_preferences()
        accounts = [a for a in accounts if account_state(value, a["account"])["online"]]
    return systemctl("enable" if enabled else "disable", accounts, runner)
