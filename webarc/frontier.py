"""Crawl frontier: BFS queue with visited-set dedup, depth tracking, robots.txt."""

from __future__ import annotations

import logging
import urllib.robotparser
from collections import deque
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class RobotsCache:
    def __init__(self, user_agent: str = "webarc"):
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{origin}/robots.txt")
            try:
                rp.read()
                self._cache[origin] = rp
            except Exception as exc:  # unreachable robots.txt => allow
                log.debug("robots.txt fetch failed for %s: %s", origin, exc)
                self._cache[origin] = None
        rp = self._cache[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)


class Frontier:
    def __init__(self, max_depth: int, max_pages: int):
        self.max_depth = max_depth
        self.max_pages = max_pages
        self._queue: deque[tuple[str, int]] = deque()
        self._seen: set[str] = set()
        self.pages_done = 0

    def add(self, url: str, depth: int) -> bool:
        if depth > self.max_depth or url in self._seen:
            return False
        self._seen.add(url)
        self._queue.append((url, depth))
        return True

    def next(self) -> tuple[str, int] | None:
        if self.pages_done >= self.max_pages or not self._queue:
            return None
        return self._queue.popleft()

    def mark_done(self) -> None:
        self.pages_done += 1

    def __len__(self) -> int:
        return len(self._queue)
