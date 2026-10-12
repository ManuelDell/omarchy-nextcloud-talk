"""Poll mirror helpers: poll ids, FTS indexing, upsert and the DTO projection (schema: store v4)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from . import store

def integer(value: Any) -> int:
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def decode(value: Any, default: Any) -> Any:
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


def poll_id(params: Any, event: str = "") -> int:
    if not isinstance(params, Mapping):
        return 0
    obj = params.get("poll" if event in ("poll_voted", "poll_closed") else "object")
    if not isinstance(obj, Mapping) or (not event and obj.get("type") != "talk-poll"):
        return 0
    if event and event not in ("poll_voted", "poll_closed"):
        return 0
    pid = integer(obj.get("id"))
    return pid if pid < 2 ** 63 else 0


def index_message(conn: sqlite3.Connection, row: sqlite3.Row, poll: Mapping[str, Any]) -> None:
    if row["is_deleted"] or row["message_type"] == "system":
        return
    base = store.build_search_text(row["message"], decode(row["params"], {}), row["actor_name"], row["file_name"])
    options = decode(poll["options_json"], [])
    search = " ".join([base, poll["question"], *[o for o in options if isinstance(o, str)]])
    if row["search_text"] != search:
        conn.execute("UPDATE messages SET search_text=? WHERE rowid=?", (search, row["rowid"]))


def index_poll(conn: sqlite3.Connection, token: str, pid: int) -> None:
    poll = conn.execute("SELECT * FROM polls WHERE token=? AND poll_id=?", (token, pid)).fetchone()
    if poll is None:
        return
    for row in conn.execute("SELECT * FROM messages WHERE token=? AND params LIKE '%talk-poll%'", (token,)).fetchall():
        if poll_id(decode(row["params"], {})) == pid:
            index_message(conn, row, poll)


def upsert_poll(conn: sqlite3.Connection, token: str, poll: Mapping[str, Any]) -> None:
    pid = integer(poll.get("id"))
    if not token or not 0 < pid < 2 ** 63:
        raise ValueError("poll without valid id")
    values: dict[str, Any] = {"token": token, "poll_id": pid}
    # Missing fields in partial responses preserve known data, while new polls use defaults.
    fields = {"question": ("question", str), "options": ("options_json", list),
              "maxVotes": ("max_votes", int), "resultMode": ("result_mode", int),
              "status": ("status", int), "numVoters": ("num_voters", int),
              "votedSelf": ("voted_self_json", list), "votes": ("votes_json", dict),
              "details": ("details_json", list), "actorId": ("actor_id", str)}
    for key, (column, kind) in fields.items():
        if key not in poll:
            continue
        value = poll[key]
        if kind is int:
            value = integer(value)
        elif not isinstance(value, kind):
            value = kind()
        if kind in (dict, list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        values[column] = value
    values["updated_at"] = store.now()
    conn.execute("INSERT INTO polls(" + ",".join(values) + ") VALUES (" +
                 ",".join("?" for _ in values) + ") ON CONFLICT(token,poll_id) DO UPDATE SET " +
                 ",".join(c + "=excluded." + c for c in values if c not in ("token", "poll_id")), tuple(values.values()))
    index_poll(conn, token, pid)


def projection(row: sqlite3.Row) -> dict[str, Any]:
    # Talk keys the tallies "option-<index>" (older notes in the docs show plain indexes).
    options = decode(row["options_json"], [])
    votes = decode(row["votes_json"], {})
    mine = {str(i) for i in decode(row["voted_self_json"], [])}
    hidden = row["result_mode"] == 1 and row["status"] == 0
    names: dict[str, list[str]] = {}
    if not hidden:
        for detail in decode(row["details_json"], []):
            if not isinstance(detail, dict):
                continue
            name = detail.get("actorDisplayName") or detail.get("actorId") or ""
            ids = detail.get("optionIds", [detail.get("optionId")])
            if not isinstance(ids, list) or not isinstance(name, str):
                continue
            for option in ids:
                bucket = names.setdefault(str(option), [])
                if name and name not in bucket:
                    bucket.append(name)
    return {"question": row["question"], "selectable": row["max_votes"] or len(options),
            "options": [{"text": o if isinstance(o, str) else "", "votes": 0 if hidden else integer(votes.get("option-%d" % i, votes.get(str(i)))),
                         "voters": [] if hidden else names.get(str(i), []), "mine": str(i) in mine}
                        for i, o in enumerate(options)],
            "voters": row["num_voters"], "id": str(row["poll_id"]),
            "closed": bool(row["status"]), "hidden_results": hidden}
