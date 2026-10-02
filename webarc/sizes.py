"""How much a job's or collection's folder holds, without making the
dashboard wait for it.

The dashboard refreshes every two seconds and shows a size for every job and
every collection. Walking a large folder on each refresh made every refresh
as slow as the largest folder. A small folder (up to ``INLINE_ENTRIES``
files and folders) is cheap, and is measured on the spot every time, so its
size is always current. A larger one is measured on a background thread and
the result kept for ``max_age`` seconds: until the first measurement is done
``measuring`` is set and no size is known, so the page can say
"Measuring…" and fill the figure in when it arrives; after that, a stale
size is shown while it is measured again.

Deleting a job or collection still measures exactly, in the request: that
figure is part of what the curator confirms.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

INLINE_ENTRIES = 2000          # a folder this small is measured in the request
DEFAULT_MAX_AGE = 30.0         # seconds a measured size is reported as current


class _TooLarge(Exception):
    pass


def folder_bytes(path: Path, limit: Optional[int] = None) -> int:
    """The bytes of every file under ``path``; raises _TooLarge after
    ``limit`` entries when one is given. Links are not followed."""
    total = 0
    seen = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if limit is not None and seen > limit:
                        raise _TooLarge()
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class FolderSizes:
    """Sizes of folders, kept for ``max_age`` seconds and measured again in
    the background. ``get`` never waits for a large folder."""

    def __init__(self, max_age: float = DEFAULT_MAX_AGE, inline_entries: int = INLINE_ENTRIES):
        self.max_age = max_age
        self.inline_entries = inline_entries
        self._known: dict[str, tuple[int, float]] = {}      # large folder -> (bytes, measured at)
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    def get(self, path: Path | str) -> dict:
        """``{"bytes", "measuring", "measured_at"}``: bytes is the last size
        measured (0 for a folder that is not there, None while a large
        folder is measured for the first time)."""
        key = str(path)
        now = time.time()
        with self._lock:
            known = self._known.get(key)
            pending = key in self._pending
        if known and now - known[1] < self.max_age:
            return {"bytes": known[0], "measuring": pending, "measured_at": known[1]}
        if not os.path.isdir(key):
            self.forget(key)
            return {"bytes": 0, "measuring": False, "measured_at": now}
        if not pending:
            try:
                size = folder_bytes(Path(key), self.inline_entries)
            except _TooLarge:
                self._enqueue(key)
                pending = True
            else:
                self.forget(key)                  # small (again): measured afresh each time
                return {"bytes": size, "measuring": False, "measured_at": time.time()}
        return {"bytes": known[0] if known else None, "measuring": True,
                "measured_at": known[1] if known else None}

    def forget(self, path: Path | str) -> None:
        """Measure this folder afresh next time (it was changed or removed)."""
        with self._lock:
            self._known.pop(str(path), None)

    def _enqueue(self, key: str) -> None:
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="swm-folder-sizes", daemon=True)
                self._worker.start()
        self._queue.put(key)

    def _run(self) -> None:
        while True:
            key = self._queue.get()
            try:
                size = folder_bytes(Path(key))
                with self._lock:
                    self._known[key] = (size, time.time())
            except Exception as exc:                     # pragma: no cover
                log.debug("Could not measure %s: %s", key, exc)
            finally:
                with self._lock:
                    self._pending.discard(key)
