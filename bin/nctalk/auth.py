"""Talk Login Flow v2 and private account lifecycle.

``run(subcommand, payload)`` always returns a JSON-ready dict, including errors
(``ok:false,error,supported``); no dispatcher exception handling is required.
The optional keyword ``runner`` injects subprocess.run for unit tests. Passwords
are only passed to TalkClient and the locked 0600 registry, never returned.
A successful credential save remains successful if systemd cannot start: the
response then has sync_started:false (retrying login would be misleading).
Existing write guards survive reauthentication. Cross-origin login/poll endpoints are
conservatively refused.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit

from . import api, prefs


def _string(payload: dict[str, Any], key: str, maximum: int = 4096) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError("invalid login field")
    return value


def _same_server(server: str, value: str, *, endpoint: bool = False) -> str:
    value = api._server(value)
    a, b = urlsplit(server), urlsplit(value)
    if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port):
        raise ValueError("login origin mismatch")
    if endpoint:
        if not b.path.startswith(a.path.rstrip("/") + "/"):
            raise ValueError("login endpoint outside server")
    elif value != server:
        raise ValueError("login server mismatch")
    return value


def _unit(identity: str, operation: str, runner: Callable[..., Any] | None) -> bool:
    try:
        result = (runner or subprocess.run)(
            ["systemctl", "--user", operation, "--now", prefs.unit(identity)],
            capture_output=True, timeout=10, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _safe_state() -> None:
    root = prefs.state_dir()
    if any(path.is_symlink() for path in (root, *root.parents, prefs.accounts_file())):
        raise ValueError("unsafe account state path")


def _save(server: str, user: str, password: str, guard: str | None,
          runner: Callable[..., Any] | None) -> dict[str, Any]:
    _safe_state()
    identity = prefs.account_id(server, user)
    # Authentication is a read-only probe; invalid credentials never enter state.
    api.TalkClient(server, user, password, guard).capabilities()
    def update(raw: dict[str, Any]) -> None:
        rows = prefs.load_accounts()
        for item in rows:
            item["default"] = item.get("default", False)
        record = {"account": identity, "id": identity, "server": server, "user": user,
                  "app_password": password, "write_guard_prefix": guard}
        existing = next((item for item in rows if item["account"] == identity), None)
        if existing is not None:
            if (existing.get("server"), existing.get("user")) != (server, user):
                raise ValueError("account identity collision")
            previous_guard = existing.get("write_guard_prefix")
            if previous_guard is not None:
                if guard is not None and guard != previous_guard:
                    raise ValueError("existing account guard cannot be changed")
                record["write_guard_prefix"] = previous_guard
            existing.update(record)
        else:
            rows.append(record)
        default = raw.get("default", "") or rows[0]["account"]
        for item in rows:
            item["default"] = item["account"] == default
        raw.clear()
        raw.update(version=1, default=default, accounts=rows)
    prefs.locked_update("accounts.json", {}, update)
    started = _unit(identity, "enable", runner)
    return {"ok": True, "account": identity, "user": user, "sync_started": started}


def run(subcommand: str, payload: dict[str, Any], *,
        runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    try:
        if subcommand == "login-start":
            server = api._server(_string(payload, "server"))
            flow = api.login_flow_start(server)
            login = _same_server(server, flow["login"], endpoint=True)
            endpoint = _same_server(server, flow["poll"]["endpoint"], endpoint=True)
            token = _string(flow["poll"], "token")
            return {"ok": True, "login_url": login, "poll_token": token,
                    "poll_endpoint": endpoint, "server": server}
        if subcommand == "login-finish":
            server = api._server(_string(payload, "server"))
            endpoint = _same_server(server, _string(payload, "poll_endpoint"), endpoint=True)
            try:
                result = api.login_flow_poll(endpoint, _string(payload, "poll_token"))
            except api.ApiError as exc:
                if exc.status == 404:
                    return {"ok": False, "pending": True}
                raise
            _same_server(server, _string(result, "server"))
            return _save(server, _string(result, "loginName"), _string(result, "appPassword"),
                         None, runner)
        if subcommand == "account-add":
            server = api._server(_string(payload, "server"))
            user, password = _string(payload, "user", 255), _string(payload, "app_password")
            guard = payload.get("write_guard_prefix")
            if guard is not None and (not isinstance(guard, str) or not guard or len(guard) > 255):
                raise ValueError("invalid write guard")
            return _save(server, user, password, guard, runner)
        if subcommand == "account-remove":
            _safe_state()
            identity = _string(payload, "account", 512)
            prefs.store_dir(identity)
            if not any(a["account"] == identity for a in prefs.load_accounts()):
                return {"ok": True, "account": identity, "removed": False, "archive_kept": True}
            if not _unit(identity, "disable", runner):
                raise ValueError("sync unit could not be stopped")
            def remove(raw: dict[str, Any]) -> None:
                rows = [a for a in prefs.load_accounts() if a["account"] != identity]
                default = raw.get("default", "")
                if default == identity or default not in [a["account"] for a in rows]:
                    default = rows[0]["account"] if rows else ""
                for item in rows:
                    item["default"] = item["account"] == default
                raw.clear()
                raw.update(version=1, default=default, accounts=rows)
            prefs.locked_update("accounts.json", {}, remove)
            return {"ok": True, "account": identity, "removed": True, "archive_kept": True}
        return {"ok": False, "supported": False, "error": "not supported"}
    except api.ApiError as exc:
        return {"ok": False, "error": exc.ocs_message}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {"ok": False, "error": "invalid login request or private account state"}
