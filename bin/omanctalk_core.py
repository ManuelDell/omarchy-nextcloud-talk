"""Small JSON dispatcher for the local OmaNCTalk bridge."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from typing import Any
from nctalk import dto, prefs

ACTIONS = set("send send-new files voice react edit delete star forward media download-pending create-group join-group group-info group-action contacts-search contact-profile check-number poll poll-vote poll-close select sticker fetch-stickers stickers contact-alias contact-tag".split())
AUTH = {"login-start", "login-finish", "account-add", "account-remove"}
READS = {"messages", "members", "chat-details"}
LOCAL = {"export-chat", "save-media", "open-media", "paste", "discard-stage", "voice-draft"}
COMMANDS = ACTIONS | AUTH | READS | LOCAL | set("status chats acknowledge notify notify-mode notify-open settings media-mode sync-mode quit launch about update-check setup teardown chat-action avatars presence chat-presence".split())
UNIMPLEMENTED = {"ok": False, "supported": False, "error": "not implemented"}


def delegate(module: str, command: str, argv: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        worker = importlib.import_module("nctalk." + module)
    except ModuleNotFoundError as exc:
        if exc.name == "nctalk." + module:
            return dict(UNIMPLEMENTED)
        raise
    try:
        result = worker.run(command, payload) if module == "auth" else worker.run(command, argv, payload)
    except (NotImplementedError, KeyError):
        return dict(UNIMPLEMENTED)
    return result if isinstance(result, dict) else dict(UNIMPLEMENTED)


def arguments(argv: list[str]) -> tuple[str, int]:
    account, limit = "", 240
    index = 0
    while index < len(argv):
        option = argv[index]
        if option not in {"--account", "--limit"} or index + 1 >= len(argv):
            raise ValueError("invalid command arguments")
        val = argv[index + 1]
        if option == "--account":
            account = val
        else:
            try:
                limit = max(1, min(10000, int(val)))
            except ValueError:
                raise ValueError("invalid limit") from None
        index += 2
    return account, limit


def selected(accounts: list[dict[str, Any]], identity: str) -> dict[str, Any]:
    if not identity and accounts:
        identity = next((a["account"] for a in accounts if a.get("default")), accounts[0]["account"])
    for account in accounts:
        if account["account"] == identity:
            return account
    raise ValueError("unknown account")


def all_chats(accounts: list[dict[str, Any]], query: str = "", limit: int = 500) -> dict[str, Any]:
    value = prefs.load_preferences()
    rows, readiness = [], []
    for account in accounts:
        if not (prefs.store_dir(account["account"]) / "talk.db").exists():
            readiness.append({"account": account["account"], "ready": False, "error": "local mirror unavailable"})
            continue
        reader = None
        try:
            reader = dto.Reader(account, value)
            rows.extend(reader.chats(query, limit))
            readiness.append({"account": account["account"], "ready": True, "error": ""})
        except sqlite3.Error:
            readiness.append({"account": account["account"], "ready": False, "error": "local mirror unavailable"})
        finally:
            if reader is not None:
                reader.close()
    rows.sort(key=lambda c: (not c["pinned"], -c["timestamp"], c["name"].casefold(), c["account"], c["jid"]))
    return {"ok": True, "query": query, "chats": rows[:limit], "accounts": readiness}


def acknowledge(accounts: list[dict[str, Any]], token: str = "") -> dict[str, Any]:
    value = prefs.load_preferences()
    snapshots = {}
    count = 0
    for account in accounts:
        if not (prefs.store_dir(account["account"]) / "talk.db").exists():
            if token:
                raise ValueError("local mirror unavailable")
            continue
        reader = dto.Reader(account, value)
        try:
            if token:
                reader.chat(token)
            rows = [c for c in reader.chats(limit=10000) if not token or c["jid"] == token]
            snapshots[account["account"]] = {c["jid"]: dto.snapshot(c) for c in rows}
            count += len(rows)
        finally:
            reader.close()
    def apply(current: dict[str, Any]) -> None:
        for identity, items in snapshots.items():
            state = prefs.account_state(current, identity)
            state["acknowledged_unread"].update(items)
            current["stores"][identity] = state
    prefs.update_preferences(apply)
    return {"ok": True, "kind": "acknowledge", "count": count}


def chat_action(account: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    token = string(payload, "jid")
    action = string(payload, "action")
    if action not in {"read", "unread", "pin", "unpin", "archive", "unarchive", "mute", "unmute", "remove-local"}:
        raise ValueError("unsupported chat action")
    reader = dto.Reader(account)
    try:
        reader.chat(token)
        last = reader.conn.execute("SELECT last_message_id FROM rooms WHERE token=?", (token,)).fetchone()[0]
    finally:
        reader.close()
    if action == "remove-local":
        def hide(state: dict[str, Any]) -> None:
            state["hidden_rooms"] = sorted(set(state["hidden_rooms"]) | {token})
        prefs.update_account(account["account"], hide)
    else:
        try:
            api = importlib.import_module("nctalk.api")
        except ModuleNotFoundError as exc:
            if exc.name != "nctalk.api":
                raise
            if action != "read":
                return dict(UNIMPLEMENTED)
        else:
            client = api.TalkClient(account["server"], account["user"], account["app_password"],
                                    write_guard_prefix=account.get("write_guard_prefix"))
            client.max_retries = 0
            client.timeout = 15
            if action == "read":
                client.mark_read(token, last)
            elif action == "unread":
                client.mark_unread(token)
            elif action in {"pin", "unpin"}:
                client.set_favorite(token, action == "pin")
            elif action in {"archive", "unarchive"}:
                client.set_archived(token, action == "archive")
            else:
                client.set_notification_level(token, 3 if action == "mute" else 1)
        if action == "read":
            acknowledge([account], token)
        elif action == "unread":
            prefs.update_account(account["account"], lambda state: state["acknowledged_unread"].pop(token, None))
    return {"ok": True, "kind": "chat-action", "action": action}


def string(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid request field")
    return value


def dispatch(command: str, argv: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    if command in ACTIONS | AUTH:
        return delegate("auth" if command in AUTH else "actions", command, argv, payload)
    identity, limit = arguments(argv)
    scope = string(payload, "account")
    if identity and scope and identity != scope:
        raise ValueError("account scope mismatch")
    identity = identity or scope
    if command == "status":
        return dto.status(identity)
    accounts = prefs.load_accounts()
    if command == "chats":
        return all_chats([selected(accounts, identity)] if identity else accounts,
                         string(payload, "query")[:256], limit if "--limit" in argv else 500)
    if command in READS:
        account = selected(accounts, identity)
        token = string(payload, "jid")
        reader = dto.Reader(account)
        try:
            if command == "messages":
                return reader.messages(token, string(payload, "query")[:256], limit, payload.get("before"), string(payload, "kind"))
            if command == "members":
                return reader.members(token, string(payload, "query")[:256])
            return reader.details(token)
        finally:
            reader.close()
    if command == "acknowledge":
        token = string(payload, "jid")
        return acknowledge([selected(accounts, identity)] if token else accounts, token)
    if command in {"notify", "notify-mode", "notify-open"}:
        from nctalk import notify
        if command == "notify-mode":
            return notify.mode(payload)
        if command == "notify-open":
            return notify.open_notification(payload)
        active = selected(accounts, identity)["account"] if accounts else ""
        return notify.run(active, string(payload, "skip_jid"))
    if command in {"settings", "media-mode"}:
        account = selected(accounts, identity)["account"] if accounts or identity else ""
        if command == "media-mode":
            result = prefs.settings(account, {"auto_download_media": payload.get("auto_download_media")})
            return {"ok": True, "kind": command, "auto_download_media": result["auto_download_media"], "restarted": 0}
        return prefs.settings(account, payload.get("settings"))
    if command == "chat-action":
        return chat_action(selected(accounts, identity), payload)
    if command == "presence":
        return {"ok": False, "supported": False, "error": "not supported"}
    if command == "chat-presence":
        state = string(payload, "state")
        if state not in {"typing", "recording", "paused"}:
            raise ValueError("invalid presence state")
        return {"ok": True, "kind": command, "state": state, "sent": False}
    if command == "sync-mode":
        account = selected(accounts, identity)
        online = payload.get("online")
        if type(online) is not bool:
            raise ValueError("online must be on or off")
        enable = online and prefs.load_preferences()["start_at_login"]
        prefs.systemctl("enable" if enable else "disable", [account])
        prefs.systemctl("start" if online else "stop", [account])
        prefs.update_account(account["account"], lambda state: state.update(online=online))
        return {"ok": True, "kind": command, "account": account["account"], "online": online}
    if command in {"quit", "launch"}:
        eligible = accounts if command == "quit" else [a for a in accounts if prefs.account_state(prefs.load_preferences(), a["account"])["online"]]
        count = prefs.systemctl("stop" if command == "quit" else "start", eligible)
        prefs.update_preferences(lambda value: value.update(closed=command == "quit"))
        return {"ok": True, "kind": command, "closed": command == "quit", "started": count if command == "launch" else 0}
    if command == "update-check":
        return {"ok": True, "kind": command, "managed": False, "current": "0.1.0", "available": False}
    if command == "setup":
        return {"ok": True, "kind": command, "changed": False, "setup": dto.setup(bool(accounts)), "complete": True}
    if command == "teardown":
        if payload.get("confirm") != "remove":
            raise ValueError("confirm removal with remove")
        prefs.systemctl("stop", accounts)
        prefs.apply_start_at_login(False, accounts)
        return {"ok": True, "kind": command, "removed": [], "kept": [str(prefs.state_dir())], "remove_command": ""}
    if command == "avatars":
        cached = 0
        for account in accounts:
            if not (prefs.store_dir(account["account"]) / "talk.db").exists():
                continue
            reader = dto.Reader(account)
            try:
                cached += reader.conn.execute("SELECT COUNT(*) FROM avatars WHERE path!=''").fetchone()[0]
            finally:
                reader.close()
        return {"ok": True, "kind": command, "checked": 0, "pending": 0, "refreshed": 0,
                "unchanged": 0, "missing": 0, "failed": 0, "cached": cached, "supported": False}
    if command in LOCAL or command == "about":
        from nctalk import desktop
        return desktop.run(command, payload, selected(accounts, identity) if command in {"save-media", "export-chat"} else None)
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "unknown subcommand"}))
        return 2
    command = argv.pop(0)
    try:
        payload: Any = {}
        if command not in {"status", "update-check"} and not sys.stdin.isatty():
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("request too large")
            payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("request must be an object")
        result = dispatch(command, argv, payload)
        code = 0 if result.get("ok") else 1
    except (ValueError, TypeError, KeyError):
        # Only our static validation messages are safe; external exception text
        # can contain credentials, paths or server-supplied message contents.
        result, code = {"ok": False, "error": "invalid request or operation failed"}, 1
    except Exception:
        result, code = {"ok": False, "error": "operation failed"}, 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return code
