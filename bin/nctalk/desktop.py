"""Lazy local desktop helpers: bounded files, clipboard and private drafts."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from typing import Any
from . import dto, prefs
from .paths import directory_fd, open_regular

MAX_FILE = 1024 * 1024 * 1024


def local_path(value: Any) -> Path:
    from urllib.parse import unquote, urlsplit
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid local path")
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("nonlocal file URL")
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute safe local path required")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("symlink file path is not supported")
    return path


def inspect(path: Path, maximum: int = MAX_FILE) -> int:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise ValueError("unsafe or oversized file")
    return metadata.st_size


def private_path(kind: str, value: Any) -> Path:
    path = local_path(value)
    root = prefs.state_dir() / kind
    if path.parent != root or not path.name.startswith("omanctalk-"):
        raise ValueError("file is not a private draft")
    inspect(path, 128 * 1024 * 1024)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("file is not owned exclusively by this user")
    return path


def allocate(kind: str, suffix: str) -> Path:
    import tempfile
    root = prefs.state_dir() / kind
    local_path(str(root))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    fd, value = tempfile.mkstemp(prefix="omanctalk-", suffix=suffix, dir=root)
    os.fchmod(fd, 0o600)
    os.close(fd)
    return Path(value)


def destination(payload: dict[str, Any]) -> tuple[Path, bool]:
    path = local_path(payload.get("destination"))
    if path.exists() and not payload.get("replace") is True:
        raise ValueError("destination exists")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("unsafe destination")
    return path, payload.get("replace") is True


def publish(path: Path, replace: bool, write: Any) -> None:
    import uuid
    with directory_fd(path.parent) as directory:
        name = ".omanctalk-" + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(name, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


def clipboard() -> dict[str, Any]:
    import shutil
    executable = shutil.which("wl-paste")
    if not executable:
        raise ValueError("clipboard tool unavailable")
    # Spooled output avoids unbounded communicate() allocations.
    def read_clip(args: list[str], maximum: int) -> bytes:
        import tempfile
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([executable, *args], stdout=output, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode or output.tell() > maximum:
                raise ValueError("clipboard unavailable or oversized")
            output.seek(0)
            return output.read(maximum + 1)
    types = read_clip(["--list-types"], 16384).decode("utf-8").splitlines()
    if "text/uri-list" in types:
        paths = [v.strip() for v in read_clip(["--no-newline", "--type", "text/uri-list"], 65536).decode().splitlines()
                 if v.strip() and not v.startswith("#")]
        if len(paths) != 1:
            raise ValueError("paste one local file at a time")
        source = local_path(paths[0])
        inspect(source)
        return {"ok": True, "kind": "file", "path": source.as_uri(), "name": source.name}
    if "image/png" in types:
        data = read_clip(["--no-newline", "--type", "image/png"], 20 * 1024 * 1024)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid clipboard image")
        path = allocate("staging", ".png")
        path.write_bytes(data)
        return {"ok": True, "kind": "image", "path": path.as_uri(), "name": path.name}
    text = read_clip(["--no-newline", "--type", "text"], 1024 * 1024).decode("utf-8")
    return {"ok": True, "kind": "text", "text": text}


def usage(root: Path) -> tuple[int, int, bool]:
    size, count = 0, 0
    if not root.exists():
        return size, count, False
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                size += path.stat().st_size
            except OSError:
                continue
            count += 1
            if count >= 10000:
                return size, count, True
    return size, count, False


def run(command: str, payload: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
    if command == "voice-draft":
        action = payload.get("action")
        result: dict[str, Any] = {"ok": True, "kind": command, "action": action}
        if action == "create":
            result["path"] = str(allocate("voice-drafts", ".ogg"))
        elif action in {"finalize", "discard"}:
            try:
                path = private_path("voice-drafts", payload.get("path"))
            except FileNotFoundError:
                if action != "discard":
                    raise
                return result
            if action == "discard":
                path.unlink()
            else:
                size = inspect(path, 128 * 1024 * 1024)
                if size == 0:
                    raise ValueError("empty recording")
                with path.open("rb") as stream:
                    if stream.read(4) != b"OggS":
                        raise ValueError("invalid recording")
                result.update(path=str(path), size=size)
        else:
            raise ValueError("invalid draft action")
        return result
    if command == "discard-stage":
        try:
            path = private_path("staging", payload.get("path"))
        except (FileNotFoundError, ValueError):
            return {"ok": True, "kind": command, "discarded": False}
        path.unlink()
        return {"ok": True, "kind": command, "discarded": True}
    if command == "paste":
        return clipboard()
    if command == "open-media":
        import shutil
        path = local_path(payload.get("path"))
        inspect(path)
        opener = shutil.which("xdg-open")
        if not opener:
            raise ValueError("file opener unavailable")
        subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "kind": command, "opener": "xdg-open"}
    if command == "about":
        stores = []
        for item in prefs.load_accounts():
            root = prefs.store_dir(item["account"])
            db = root / "talk.db"
            size, count, truncated = usage(root / "media")
            stores.append({"account": item["account"], "label": str(item.get("label") or item["account"]),
                           "database_bytes": db.stat().st_size if db.exists() else 0,
                           "media_bytes": size, "media_files": count, "media_truncated": truncated})
        size, count, _ = usage(prefs.state_dir() / "avatars")
        return {"ok": True, "kind": command, "app_version": "0.1.0", "install_mode": "local",
                "wacli_version": "", "wacli_minimum_version": "", "wacli_tested_version": "",
                "stores": stores, "avatar_cache_bytes": size, "avatar_cache_files": count}
    if account is None:
        raise ValueError("account required")
    reader = dto.Reader(account)
    try:
        token = payload.get("jid", "")
        reader.chat(token)
        path, replace = destination(payload)
        if command == "save-media":
            row = reader.conn.execute("SELECT local_path,is_deleted FROM messages WHERE token=? AND id=?", (token, payload.get("id"))).fetchone()
            if not row or row["is_deleted"] or not row["local_path"]:
                raise ValueError("media not downloaded")
            source = local_path(row["local_path"])
            expected = inspect(source)
            def copy(stream: Any) -> None:
                fd = open_regular(source)
                with os.fdopen(fd, "rb") as incoming:
                    metadata = os.fstat(incoming.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected:
                        raise ValueError("media changed before copy")
                    remaining = expected
                    while remaining:
                        part = incoming.read(min(remaining, 65536))
                        if not part:
                            raise ValueError("media changed during copy")
                        stream.write(part)
                        remaining -= len(part)
                    if incoming.read(1):
                        raise ValueError("media changed during copy")
            publish(path, replace, copy)
            return {"ok": True, "kind": command, "path": str(path)}
        count = 0
        def export(stream: Any) -> None:
            nonlocal count
            from datetime import datetime, timezone
            stream.write(b"OmaNCTalk chat export\n")
            for row in reader.conn.execute("SELECT m.* FROM messages m WHERE token=? AND " + dto.VISIBLE + " ORDER BY timestamp,id", (token,)):
                when = datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat()
                line = f"[{when}] {row['actor_name']}: {dto.text(row)}\n"
                stream.write(line.encode("utf-8"))
                count += 1
        publish(path, replace, export)
        return {"ok": True, "kind": command, "path": str(path), "messages": count}
    finally:
        reader.close()
