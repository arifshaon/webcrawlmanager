"""What a capture browser records, against a real Chrome and a local site.

A crawl must archive what it fetched: the browser's cache must neither hand
over resources nobody requested nor turn repeats into empty 304 answers; and
a page that arrives but never falls quiet must still count as visited.
"""
from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from webarc.config import BehaviorConfig, BrowserConfig

POLL = "<script>(function p(){fetch('/poll?'+Math.random()).then(()=>setTimeout(p,100))})()</script>"


class _Site(SimpleHTTPRequestHandler):
    served: list = []

    def do_GET(self):
        if self.path.startswith("/poll"):
            time.sleep(0.2)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError):
                pass                      # the browser closed while polling
            return
        super().do_GET()

    def log_request(self, code="-", size="-"):
        if not self.path.startswith("/poll"):
            _Site.served.append((self.path, int(code) if str(code).isdigit() else code))


class CaptureBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:                                # pragma: no cover
            raise unittest.SkipTest("playwright is not installed")
        from tests.chrome_for_tests import find_chrome
        cls.chrome = find_chrome()
        if not cls.chrome:                                 # pragma: no cover
            raise unittest.SkipTest("no Chrome to drive")
        cls._dir = tempfile.TemporaryDirectory()
        site = Path(cls._dir.name)
        for name in ("a", "b", "c"):
            (site / f"{name}.html").write_text(
                f"<link rel=stylesheet href=/style.css><script src=/app.js></script>"
                f"<img src=/logo.png><h1>{name}</h1>")
        (site / "busy.html").write_text(f"<h1>busy</h1>{POLL}")
        (site / "style.css").write_text("body{}" * 50)
        (site / "app.js").write_text("1;" * 50)
        (site / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        cls.port = probe.getsockname()[1]
        probe.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), partial(_Site, directory=str(site)))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._dir.cleanup()

    def driver(self, **behaviour):
        from webarc.browser import BrowserDriver
        options = dict(obey_robots=False, delay_range=(0, 0), page_timeout=20, wait_until="load",
                       scroll=False, mouse_jitter=False, dismiss_consent=False, challenge_grace=0)
        options.update(behaviour)
        return BrowserDriver(BrowserConfig(mode="headless", chrome_path=self.chrome),
                             BehaviorConfig(**options))

    def url(self, path):
        return f"http://127.0.0.1:{self.port}/{path}"

    def test_every_page_fetches_what_it_shows_from_the_network(self):
        """No 304s, and nothing handed over by the memory cache: each page's
        stylesheet, script and image are requested from the site itself."""
        seen = []
        _Site.served.clear()
        with self.driver() as driver:
            page = driver.new_page(lambda r: seen.append((r.url.rsplit("/", 1)[-1], r.status)))
            for name in ("a", "b", "c"):
                driver.visit(page, self.url(f"{name}.html"))

        for resource in ("/style.css", "/app.js", "/logo.png"):
            with self.subTest(resource):
                self.assertEqual([code for path, code in _Site.served if path == resource], [200, 200, 200])
        self.assertNotIn(304, [status for _, status in seen])
        self.assertEqual(len([r for r in seen if r[0] == "style.css"]), 3)

    def test_a_page_that_never_falls_quiet_still_counts_as_visited(self):
        with self.driver(wait_until="networkidle", page_timeout=3) as driver:
            page = driver.new_page(lambda r: None)
            started = time.monotonic()
            resp = driver.visit(page, self.url("busy.html"))

            self.assertIsNotNone(resp)
            self.assertEqual(resp.status, 200)
            self.assertTrue(driver.last_unsettled)
            self.assertLess(time.monotonic() - started, 15)
            self.assertIn("busy", page.content())

            quiet = driver.visit(page, self.url("a.html"))
            self.assertEqual(quiet.status, 200)
            self.assertFalse(driver.last_unsettled)

    def test_a_page_that_never_arrives_is_still_a_failure(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        with self.driver(page_timeout=5) as driver:
            page = driver.new_page(lambda r: None)

            self.assertIsNone(driver.visit(page, f"http://127.0.0.1:{closed}/"))


if __name__ == "__main__":
    unittest.main()
