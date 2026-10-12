"""Small no-symlink filesystem primitives for private files and exports."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator


def absolute(path: str | os.PathLike[str]) -> Path:
    value = Path(os.path.abspath(path))
    if ".." in Path(path).parts:
        raise ValueError("unsafe path")
    return value


@contextmanager
def directory_fd(path: str | os.PathLike[str], *, create: bool = False) -> Iterator[int]:
    value = absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in value.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def ensure_directory(path: str | os.PathLike[str]) -> None:
    with directory_fd(path, create=True) as fd:
        os.fchmod(fd, 0o700)


def check_file(path: str | os.PathLike[str]) -> None:
    value = absolute(path)
    with directory_fd(value.parent) as fd:
        try:
            mode = os.stat(value.name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            raise ValueError("unsafe file path")


def open_regular(path: str | os.PathLike[str]) -> int:
    value = absolute(path)
    with directory_fd(value.parent) as fd:
        return os.open(value.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
