"""The replay server must serve from a port it really owns."""
from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from webarc.replay import ReplayServer


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

        with mock.patch("webarc.replay.socketserver.ThreadingTCPServer",
                        side_effect=OSError("cannot bind")), \
                self.assertRaises(OSError):
            server.start_background()

        self.assertFalse(server.is_running())


if __name__ == "__main__":
    unittest.main()
