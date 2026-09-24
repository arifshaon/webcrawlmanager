"""The control server's start and end.

What the dashboard starts -- the replay server, the resource monitor -- is
stopped when the server ends, so Ctrl+C ends the process. That used to hang
off FastAPI's on_event hook, which newer FastAPI versions warn about on
every app creation; it is now the app's lifespan, which the test client
drives when used as a context manager.
"""

from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from webarc import server as srv


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_app(self):
        return srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                              simulate=True, replay_root=str(self.tmp / "replay"),
                              monitor_resources=False)

    def test_creating_the_app_raises_no_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.make_app()

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)
                        and "on_event" in str(w.message)]
        self.assertEqual(deprecations, [])

    def test_shutdown_stops_what_the_dashboard_started(self):
        app = self.make_app()
        replay = mock.Mock()
        with mock.patch.object(srv, "_PYWB", replay), \
                mock.patch.object(srv._MONITOR, "stop") as monitor_stop:
            with TestClient(app):
                pass                       # start-up, then shutdown

            replay.stop.assert_called_once()
            monitor_stop.assert_called_once()
            self.assertIsNone(srv._PYWB)


if __name__ == "__main__":
    unittest.main()
