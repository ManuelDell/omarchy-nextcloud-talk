"""Validate daemon-published local avatar paths without downloading."""
from __future__ import annotations

import html
import os
import re
import stat


def cached_path(value: str | None) -> str:
    if not value or not os.path.isabs(value):
        return ""
    try:
        metadata = os.lstat(value)
    except OSError:
        return ""
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or not 0 < metadata.st_size <= 1024 * 1024):
        return ""
    return value


_RECT_FILL = re.compile(r"<rect\b[^>]*\bfill=\"(#[0-9a-fA-F]{6})\"", re.I)
_TEXT = re.compile(r"<text\b[^>]*>([^<]{1,32})</text>", re.I)


def emoji_avatar(data: bytes) -> tuple[str, str]:
    """Return ``(emoji, "#rrggbb")`` for Talk's generated emoji avatars, else ``("", "")``.

    The server renders them as an SVG with a ``<text>`` node; Qt's SVG renderer does not
    centre that text, so the UI draws the emoji itself from these two values.
    """
    head = data[:4096]
    if b"<svg" not in head or b"<text" not in head:
        return "", ""
    try:
        svg = head.decode("utf-8")
    except UnicodeDecodeError:
        return "", ""
    text = _TEXT.search(svg)
    fill = _RECT_FILL.search(svg)
    if not text or not fill:
        return "", ""
    emoji = html.unescape(text.group(1)).strip()
    if not emoji or len(emoji) > 16 or any(c.isalnum() and c.isascii() for c in emoji):
        return "", ""
    return emoji, fill.group(1).lower()


def avatar_fields(path: str | None, emoji: str | None, color: str | None) -> dict[str, str]:
    """DTO fields for a room avatar: an emoji avatar is drawn by the UI, so it has no image path."""
    if emoji and color:
        return {"avatar_path": "", "avatar_emoji": emoji, "avatar_color": color}
    return {"avatar_path": cached_path(path), "avatar_emoji": "", "avatar_color": ""}
