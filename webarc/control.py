"""Controller: the seam between the pure crawl loop and the outside world.

The crawl loop calls into a Controller to (a) report progress and (b) check
whether it should pause or stop. This keeps crawler.py free of any knowledge
about databases or servers.

- NullController: used by the plain CLI. Never pauses/stops; logs only.
- StoreController: used by worker subprocesses. Backs onto the SQLite store,
  polling the control column between pages and blocking while paused.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from .store import (CTRL_PAUSE, CTRL_RESUME, CTRL_STOP, PAUSED, RUNNING,
                    Store)

log = logging.getLogger(__name__)


class Controller(Protocol):
    def should_stop(self) -> bool: ...
    def wait_if_paused(self) -> None: ...
    def report(self, seed_idx: int, **fields) -> None: ...
    def seed_status(self, seed_idx: int, status: str) -> None: ...


class NullController:
    """No-op controller for CLI runs."""

    def should_stop(self) -> bool:
        return False

    def wait_if_paused(self) -> None:
        return

    def report(self, seed_idx: int, **fields) -> None:
        return

    def seed_status(self, seed_idx: int, status: str) -> None:
        return


class StoreController:
    """Controller backed by the SQLite store, for worker subprocesses."""

    def __init__(self, store: Store, crawl_id: int, poll_interval: float = 0.5):
        self.store = store
        self.crawl_id = crawl_id
        self.poll = poll_interval
        self._stopping = False

    def should_stop(self) -> bool:
        if self._stopping:
            return True
        if self.store.get_control(self.crawl_id) == CTRL_STOP:
            self._stopping = True
        return self._stopping

    def wait_if_paused(self) -> None:
        cmd = self.store.get_control(self.crawl_id)
        if cmd != CTRL_PAUSE:
            return
        # enter paused state and block until resume/stop
        self.store.set_status(self.crawl_id, PAUSED)
        log.info("Crawl %d paused", self.crawl_id)
        while True:
            time.sleep(self.poll)
            cmd = self.store.get_control(self.crawl_id)
            if cmd == CTRL_STOP:
                self._stopping = True
                return
            if cmd == CTRL_RESUME:
                self.store.clear_control(self.crawl_id)
                self.store.set_status(self.crawl_id, RUNNING)
                log.info("Crawl %d resumed", self.crawl_id)
                return

    def report(self, seed_idx: int, **fields) -> None:
        self.store.update_progress(self.crawl_id, seed_idx, **fields)

    def seed_status(self, seed_idx: int, status: str) -> None:
        self.store.update_progress(self.crawl_id, seed_idx, status=status)
