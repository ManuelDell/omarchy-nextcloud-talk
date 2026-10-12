"""Synthetic filesystem regression tests; no private desktop state is read."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from nctalk import desktop, store
from nctalk.paths import open_regular


class PathTests(unittest.TestCase):
    def test_store_rejects_database_wal_shm_and_parent_links(self) -> None:
        for component in ("talk.db", "talk.db-wal", "talk.db-shm", "parent"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "file"
                sentinel.write_bytes(b"synthetic sentinel")
                directory = root / "store"
                if component == "parent":
                    directory.symlink_to(outside, target_is_directory=True)
                else:
                    directory.mkdir()
                    (directory / component).symlink_to(sentinel)
                with self.assertRaises((OSError, ValueError)):
                    store.connect(directory / "talk.db")
                self.assertEqual(sentinel.read_bytes(), b"synthetic sentinel")
                self.assertFalse((outside / "talk.db").exists())

    def test_read_only_rejects_linked_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with closing(store.connect(root / "real.db")):
                pass
            (root / "linked.db").symlink_to(root / "real.db")
            with self.assertRaises(ValueError):
                store.connect_ro(root / "linked.db")

    def test_publish_rechecks_parents_after_destination_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "destination"
            outside = root / "outside"
            directory.mkdir()
            outside.mkdir()
            path, replace = desktop.destination({"destination": str(directory / "export")})
            directory.rename(root / "moved")
            directory.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                desktop.publish(path, replace, lambda stream: stream.write(b"synthetic export"))
            self.assertEqual(list(outside.iterdir()), [])

    def test_publish_pins_directory_during_write_and_cleans_temp(self) -> None:
        for replace in (False, True):
            with self.subTest(replace=replace), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                directory, outside = root / "destination", root / "outside"
                directory.mkdir()
                outside.mkdir()
                def write(stream):
                    directory.rename(root / "moved")
                    directory.symlink_to(outside, target_is_directory=True)
                    stream.write(b"synthetic export")
                desktop.publish(directory / "export", replace, write)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual((root / "moved/export").read_bytes(), b"synthetic export")
                self.assertEqual(len(list((root / "moved").iterdir())), 1)

    def test_source_open_rejects_linked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside").mkdir()
            (root / "outside/file").write_bytes(b"synthetic source")
            (root / "linked").symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(OSError):
                open_regular(root / "linked/file")
            fd = open_regular(root / "outside/file")
            try:
                self.assertEqual(os.read(fd, 100), b"synthetic source")
            finally:
                os.close(fd)
