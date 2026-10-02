"""Folder sizes that never hold up a dashboard refresh."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from webarc.sizes import FolderSizes, folder_bytes


def fill(folder: Path, files: int, size: int = 100) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (folder / f"f{i}.bin").write_bytes(b"x" * size)


def settled(sizes: FolderSizes, path: Path, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = sizes.get(path)
        if not found["measuring"]:
            return found
        time.sleep(0.02)
    raise AssertionError("never finished measuring")


class FolderSizeTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_a_small_folder_is_exact_at_once(self):
        fill(self.root / "small" / "jobs" / "1", 3)
        found = FolderSizes().get(self.root / "small")
        self.assertEqual((found["bytes"], found["measuring"]), (300, False))

    def test_a_large_folder_is_measured_in_the_background(self):
        fill(self.root / "large", 30)
        sizes = FolderSizes(inline_entries=10)
        first = sizes.get(self.root / "large")
        self.assertEqual((first["bytes"], first["measuring"]), (None, True))   # not waited for
        self.assertEqual(settled(sizes, self.root / "large")["bytes"], 3000)

    def test_a_stale_size_is_shown_while_it_is_measured_again(self):
        fill(self.root / "large", 30)
        sizes = FolderSizes(max_age=0.05, inline_entries=10)
        sizes.get(self.root / "large")
        settled(sizes, self.root / "large")
        fill(self.root / "large" / "more", 10)
        time.sleep(0.1)
        again = sizes.get(self.root / "large")
        self.assertEqual((again["bytes"], again["measuring"]), (3000, True))  # the old figure meanwhile
        time.sleep(0.2)
        sizes.max_age = 60
        self.assertEqual(settled(sizes, self.root / "large")["bytes"], 4000)

    def test_a_small_folder_is_always_current(self):
        sizes = FolderSizes()
        self.assertEqual(sizes.get(self.root / "job")["bytes"], 0)            # not there yet
        fill(self.root / "job", 2)
        self.assertEqual(sizes.get(self.root / "job")["bytes"], 200)          # no waiting for max_age
        fill(self.root / "job" / "more", 1)
        self.assertEqual(sizes.get(self.root / "job")["bytes"], 300)

    def test_a_large_folder_forgotten_is_measured_afresh(self):
        fill(self.root / "large", 30)
        sizes = FolderSizes(inline_entries=10)
        sizes.get(self.root / "large")
        settled(sizes, self.root / "large")
        sizes.forget(self.root / "large")
        self.assertEqual(sizes.get(self.root / "large")["bytes"], None)       # measured again from scratch

    def test_links_are_not_followed(self):
        fill(self.root / "real", 2)
        (self.root / "job").mkdir()
        (self.root / "job" / "link").symlink_to(self.root / "real", target_is_directory=True)
        self.assertEqual(folder_bytes(self.root / "job"), 0)


if __name__ == "__main__":
    unittest.main()
