"""The replay server must serve from a port it really owns."""
from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from webarc.replay import ReplayServer, build_replay_site


class ReplayPortTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (Path(self._tmp.name) / "hello.txt").write_text("served", encoding="utf-8")

    def test_it_steps_past_a_port_another_program_holds(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        server = ReplayServer(self._tmp.name, port=port)
        self.addCleanup(server.stop)

        server.start_background()

        self.assertNotEqual(server.port, port)
        self.assertIn(f":{server.port}/", server.replay_url("coll"))
        with urlopen(f"http://127.0.0.1:{server.port}/hello.txt", timeout=5) as answer:
            self.assertEqual(answer.read(), b"served")

    def test_a_failed_start_leaves_nothing_running(self):
        from unittest import mock
        server = ReplayServer(self._tmp.name, port=8091)
        server.PORT_TRIES = 2

        with mock.patch("webarc.replay._ReplayTCPServer",
                        side_effect=OSError("cannot bind")), \
                self.assertRaises(OSError):
            server.start_background()

        self.assertFalse(server.is_running())

    def test_it_stops_at_once_while_a_browser_holds_a_connection_open(self):
        """ReplayWeb.page keeps connections open for as long as its tab
        lives; stopping must not wait for it, or Ctrl+C hangs."""
        import threading
        server = ReplayServer(self._tmp.name, port=0)
        server.start_background()
        held = socket.create_connection(("127.0.0.1", server.port))
        self.addCleanup(held.close)
        held.sendall(b"GET /hello.txt HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
        held.recv(4096)                              # served; connection stays open
        self.assertTrue(server._httpd.daemon_threads)

        stopper = threading.Thread(target=server.stop)
        stopper.start()
        stopper.join(timeout=5)

        self.assertFalse(stopper.is_alive(), "stop() waited for the open connection")
        self.assertFalse(server.is_running())

    def test_the_dashboard_stops_its_services_on_shutdown(self):
        from fastapi.testclient import TestClient

        from webarc import server as srv
        app = srv.create_app(str(Path(self._tmp.name) / "swm.db"), str(Path(self._tmp.name) / "warcs"),
                             simulate=True, replay_root=str(Path(self._tmp.name) / "replay"),
                             monitor_resources=False)
        srv._PYWB = ReplayServer(str(Path(self._tmp.name) / "replay"), port=0)
        srv._PYWB.start_background()

        with TestClient(app):
            pass                                     # enters and leaves the lifespan

        self.assertIsNone(srv._PYWB)


if __name__ == "__main__":
    unittest.main()


class XReplayTests(unittest.TestCase):
    """An archive of X made signed in replays only for a client that thinks
    it is signed in; the replay page gives it placeholder session cookies."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.warc = self.tmp / "one.warc.gz"
        from webarc.capture import WarcSession
        from webarc.config import WarcConfig
        session = WarcSession(self.tmp, "one", "https://x.com/qnl", 1, "webarc", WarcConfig())
        session.write_exchange(url="https://x.com/qnl", method="GET", req_headers={},
                               post_data=None, status=200, status_text="OK",
                               resp_headers={"content-type": "text/html"}, body=b"<p>hi</p>")
        session.close()
        self.warc = next(self.tmp.glob("*.warc.gz"))

    def index_for(self, seed):
        from unittest import mock
        site = self.tmp / f"site-{abs(hash(seed))}"
        with mock.patch("webarc.replay._ensure_vendor_assets", return_value=True):
            (site / "vendor").mkdir(parents=True, exist_ok=True)
            build_replay_site([self.warc], site, seed_url=seed)
        return (site / "index.html").read_text(encoding="utf-8")

    def test_an_x_archive_gets_placeholder_session_cookies(self):
        index = self.index_for("https://x.com/qnl")
        self.assertIn('document.cookie = "twid=u%3D1', index)
        self.assertIn('document.cookie = "ct0=swm-replay', index)
        self.assertLess(index.index("twid="), index.index("<replay-web-page"))

    def test_other_archives_are_left_alone(self):
        for seed in ("https://www.instagram.com/qnl/", "https://example.org/", None):
            with self.subTest(seed=seed):
                self.assertNotIn("twid=", self.index_for(seed))

    def test_a_youtube_archive_plays_the_downloaded_file_in_the_watch_page(self):
        """The WARC of a YouTube capture holds the watch page, never the
        streams; the replay page swaps the archived player for the file."""
        from unittest import mock
        site = self.tmp / "site-yt"
        media = {"wGA27zJEnaU": {"url": "http://127.0.0.1:8006/captures/9/media/videos/wGA27zJEnaU/wGA27zJEnaU.mp4",
                                 "file": "media/videos/wGA27zJEnaU/wGA27zJEnaU.mp4",
                                 "resolution": "1920x1080"}}
        with mock.patch("webarc.replay._ensure_vendor_assets", return_value=True):
            (site / "vendor").mkdir(parents=True, exist_ok=True)
            build_replay_site([self.warc], site, seed_url="https://www.youtube.com/watch?v=wGA27zJEnaU",
                              youtube_media=media)
        index = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("/captures/9/media/videos/wGA27zJEnaU/wGA27zJEnaU.mp4", index)
        self.assertIn('"resolution": "1920x1080"', index)
        self.assertIn("#movie_player", index)
        self.assertNotIn("__SWM_YOUTUBE_MEDIA__", index)
        self.assertNotIn("swm-youtube-playback", self.index_for("https://www.youtube.com/@qnl"))
