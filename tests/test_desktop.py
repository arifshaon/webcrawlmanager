"""Showing a job's or collection's folder in the machine's file manager."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webarc import desktop


class AvailabilityTests(unittest.TestCase):
    def test_windows_and_macos_always_have_a_file_manager(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform):
                self.assertTrue(desktop.availability(platform, which=lambda _: None)["available"])

    def test_linux_needs_xdg_open_and_a_desktop(self):
        missing = desktop.availability("linux", which=lambda _: None, has_desktop=lambda: True)
        headless = desktop.availability("linux", which=lambda _: "/usr/bin/xdg-open", has_desktop=lambda: False)
        ready = desktop.availability("linux", which=lambda _: "/usr/bin/xdg-open", has_desktop=lambda: True)

        self.assertFalse(missing["available"])
        self.assertIn("xdg-open", missing["reason"])
        self.assertFalse(headless["available"])
        self.assertIn("desktop", headless["reason"])
        self.assertEqual(ready, {"available": True, "reason": None})


class OpenFolderTests(unittest.TestCase):
    def test_each_system_opens_its_own_file_manager(self):
        folder = Path("/data/swm/collections/qnl")
        self.assertIsNone(desktop.command_for(folder, "win32"))            # Explorer, via os.startfile
        self.assertEqual(desktop.command_for(folder, "darwin"), ["open", str(folder)])
        self.assertEqual(desktop.command_for(folder, "linux"), ["xdg-open", str(folder)])

    def test_the_file_manager_is_started_apart_from_the_server(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(desktop.subprocess, "Popen") as popen:
            desktop.open_folder(Path(d), platform="linux")

        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["xdg-open", d])
        self.assertTrue(kwargs["start_new_session"])

    def test_windows_hands_the_folder_to_explorer(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(desktop.os, "startfile", create=True) as startfile:
            desktop.open_folder(Path(d), platform="win32")

        startfile.assert_called_once_with(d)

    def test_a_folder_that_is_not_there_is_refused(self):
        with mock.patch.object(desktop.subprocess, "Popen") as popen:
            with self.assertRaises(FileNotFoundError):
                desktop.open_folder(Path("/no/such/folder"), platform="linux")
        popen.assert_not_called()


class EndpointTests(unittest.TestCase):
    def client(self, bind_host="127.0.0.1"):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:                            # pragma: no cover
            raise unittest.SkipTest(f"dashboard dependencies missing: {exc}")
        from webarc.server import create_app
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        return TestClient(create_app(str(root / "swm.db"), str(root / "warcs"),
                                     replay_root=str(root / "replay"), monitor_resources=False,
                                     bind_host=bind_host))

    def test_only_the_folder_the_server_knows_is_opened(self):
        client = self.client()
        made = client.post("/api/collections", json={"name": "QNL web"}).json()
        with mock.patch.object(desktop, "availability", return_value={"available": True, "reason": None}), \
                mock.patch.object(desktop, "open_folder") as opened:
            reply = client.post(f"/api/collections/{made['id']}/open-folder",
                                json={"path": "/etc"})          # a path sent along is ignored
            unknown = client.post("/api/collections/999/open-folder")
            no_job = client.post("/api/crawls/7/open-folder")

        self.assertEqual(reply.status_code, 200)
        opened.assert_called_once_with(Path(made["root_dir"]).resolve())
        self.assertEqual((unknown.status_code, no_job.status_code), (404, 404))

    def test_a_dashboard_reached_over_the_network_does_not_open_folders(self):
        client = self.client(bind_host="0.0.0.0")
        made = client.post("/api/collections", json={"name": "QNL web"}).json()
        capability = client.get("/api/capabilities").json()["open_folder"]
        with mock.patch.object(desktop, "availability", return_value={"available": True, "reason": None}), \
                mock.patch.object(desktop, "open_folder") as opened:
            reply = client.post(f"/api/collections/{made['id']}/open-folder")

        self.assertFalse(capability["available"])
        self.assertIn("network", capability["reason"])
        self.assertEqual(reply.status_code, 409)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
