from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk.avatars import avatar_fields, emoji_avatar

# Shape of the SVG that Nextcloud generates for an emoji room avatar (synthetic emoji/colour).
EMOJI_SVG = ('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<svg width="512" height="512" '
             'viewBox="0 0 500 500" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" '
             'fill="#0082C9"></rect><text x="50%" y="330" style="font-size:240px;text-anchor:middle;">'
             '\U0001F4DD</text></svg>').encode()
LOGO_SVG = b'<svg viewBox="0 0 32 32"><rect width="32" height="32" fill="#0082c9"/><path d="m1 1" fill="#fff"/></svg>'


class EmojiAvatarTests(unittest.TestCase):
    def test_generated_emoji_avatar_is_recognised(self) -> None:
        self.assertEqual(emoji_avatar(EMOJI_SVG), ("\U0001F4DD", "#0082c9"))

    def test_entity_encoded_emoji(self) -> None:
        svg = EMOJI_SVG.replace("\U0001F4DD".encode(), b"&#128221;")
        self.assertEqual(emoji_avatar(svg), ("\U0001F4DD", "#0082c9"))

    def test_other_images_are_not_emoji_avatars(self) -> None:
        for data in (LOGO_SVG, b"\x89PNG\r\n\x1a\n" + b"\0" * 64, b"\xff\xd8\xff\xe0JFIF", b"", b"<svg><text>abc</text></svg>"):
            self.assertEqual(emoji_avatar(data), ("", ""), data[:12])

    def test_text_that_is_not_an_emoji_is_rejected(self) -> None:
        svg = EMOJI_SVG.replace("\U0001F4DD".encode(), b"Hello")
        self.assertEqual(emoji_avatar(svg), ("", ""))

    def test_dto_fields_drop_the_image_path_for_emoji_avatars(self) -> None:
        self.assertEqual(avatar_fields("/x/a.img", "\U0001F4DD", "#0082c9"),
                         {"avatar_path": "", "avatar_emoji": "\U0001F4DD", "avatar_color": "#0082c9"})
        plain = avatar_fields("relative/path", "", "")
        self.assertEqual((plain["avatar_emoji"], plain["avatar_color"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
