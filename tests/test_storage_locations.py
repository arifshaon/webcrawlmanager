"""Where each capture is written, and where it is read back from.

Every path in the server used to be recomputed as <server root>/<crawl id>.
That held only while every crawl lived under the one root: a crawl given its
own storage location has to be read back from where it was written, or its
files, its size and its replay all point at an empty directory.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webarc import server as srv


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "warcs"
        self.app = srv.create_app(
            str(self.tmp / "swm.db"), str(self.root), simulate=True,
            replay_root=str(self.tmp / "replay"))
        self.client = TestClient(self.app)

    def crawl(self, **extra):
        payload = {
            "name": "example",
            "config": {
                "crawl_name": "example",
                "operator": "webarc",
                "seeds": [{"url": "https://example.org/"}],
            },
            **extra,
        }
        response = self.client.post("/api/crawls", json=payload)
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()

    def row(self, crawl_id):
        return self.client.get(f"/api/crawls/{crawl_id}").json()


class DefaultLocationTests(StorageTestCase):
    def test_a_crawl_with_no_location_uses_the_server_root(self):
        made = self.crawl()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         self.root / str(made["id"]))

    def test_the_default_can_be_changed_from_the_dashboard(self):
        elsewhere = self.tmp / "archive-drive"

        self.client.put("/api/settings",
                        json={"storage_root": str(elsewhere)})
        made = self.crawl()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         elsewhere.resolve() / str(made["id"]))

    def test_the_settings_report_what_is_actually_in_use(self):
        elsewhere = self.tmp / "archive-drive"
        self.client.put("/api/settings",
                        json={"storage_root": str(elsewhere)})

        settings = self.client.get("/api/settings").json()

        self.assertEqual(settings["storage_root"], str(elsewhere))
        self.assertEqual(Path(settings["effective_storage_root"]),
                         elsewhere.resolve())
        self.assertEqual(Path(settings["server_storage_root"]), self.root)

    def test_clearing_the_default_returns_to_the_server_root(self):
        self.client.put("/api/settings",
                        json={"storage_root": str(self.tmp / "elsewhere")})
        self.client.put("/api/settings", json={"storage_root": ""})

        made = self.crawl()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         self.root / str(made["id"]))

    def test_a_default_that_has_become_unusable_does_not_stop_captures(self):
        """An unplugged drive must not fail every new capture."""
        broken = self.tmp / "not-a-directory"
        broken.write_text("this is a file")
        self.client.put("/api/settings", json={"storage_root": ""})
        srv._store().set_setting(srv._STORAGE_ROOT_SETTING, str(broken))

        made = self.crawl()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         self.root / str(made["id"]))


class PerCrawlLocationTests(StorageTestCase):
    def test_a_crawl_can_name_its_own_location(self):
        mine = self.tmp / "qatar-collection"

        made = self.crawl(storage_dir=str(mine))

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         mine.resolve() / str(made["id"]))

    def test_a_recording_can_name_its_own_location(self):
        mine = self.tmp / "recordings"

        made = self.client.post("/api/recordings", json={
            "url": "https://example.org/", "storage_dir": str(mine)}).json()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         mine.resolve() / str(made["id"]))

    def test_a_facebook_capture_can_name_its_own_location(self):
        mine = self.tmp / "facebook"

        made = self.client.post("/api/facebook", json={
            "page_url": "https://www.facebook.com/qatarnationallibrary",
            "mode": "latest_n", "latest_n": 5,
            "storage_dir": str(mine)}).json()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         mine.resolve() / str(made["id"]))

    def test_the_directory_is_created_rather_than_required(self):
        mine = self.tmp / "deep" / "nested" / "place"

        self.crawl(storage_dir=str(mine))

        self.assertTrue(mine.is_dir())

    def test_a_file_is_refused_with_a_reason(self):
        blocker = self.tmp / "already-a-file"
        blocker.write_text("x")

        response = self.client.post("/api/crawls", json={
            "name": "example",
            "config": {"crawl_name": "example", "operator": "webarc",
                       "seeds": [{"url": "https://example.org/"}]},
            "storage_dir": str(blocker)})

        self.assertEqual(response.status_code, 400)
        self.assertIn("not a directory", response.text)

    def test_a_refused_location_leaves_no_crawl_behind(self):
        blocker = self.tmp / "already-a-file"
        blocker.write_text("x")

        self.client.post("/api/crawls", json={
            "name": "example",
            "config": {"crawl_name": "example", "operator": "webarc",
                       "seeds": [{"url": "https://example.org/"}]},
            "storage_dir": str(blocker)})

        self.assertEqual(self.client.get("/api/crawls").json(), [])

    @unittest.skipIf(os.geteuid() == 0, "root can write to any directory")
    def test_an_unwritable_location_is_refused(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)

        response = self.client.post("/api/crawls", json={
            "name": "example",
            "config": {"crawl_name": "example", "operator": "webarc",
                       "seeds": [{"url": "https://example.org/"}]},
            "storage_dir": str(locked / "inside")})

        self.assertEqual(response.status_code, 400)
        self.assertIn("not writable", response.text)


class ReadingBackTests(StorageTestCase):
    """A crawl elsewhere still has to be findable, sizeable and deletable."""

    def setUp(self):
        super().setUp()
        self.mine = self.tmp / "qatar-collection"
        self.made = self.crawl(storage_dir=str(self.mine))
        self.dir = self.mine.resolve() / str(self.made["id"])

    def test_its_size_is_counted_where_it_lives(self):
        (self.dir / "a.warc.gz").write_bytes(b"x" * 2048)

        storage = self.client.get("/api/storage").json()
        mine = [c for c in storage["per_crawl"]
                if c["id"] == self.made["id"]][0]

        self.assertEqual(mine["bytes"], 2048)
        self.assertEqual(storage["total_bytes"], 2048)

    def test_its_rendered_pages_are_served_from_where_it_lives(self):
        pages = self.dir / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        (pages / "index.html").write_text("<p>captured</p>")

        response = self.client.get(
            f"/captures/{self.made['id']}/pages/index.html")

        self.assertEqual(response.status_code, 200)
        self.assertIn("captured", response.text)

    def purge(self):
        """The worker is still running in these tests; stand it down first."""
        srv._store().set_pid(self.made["id"], None)
        response = self.client.delete(
            f"/api/crawls/{self.made['id']}?purge=true")
        self.assertEqual(response.status_code, 200, response.text)

    def test_purging_it_removes_the_files_where_they_live(self):
        (self.dir / "a.warc.gz").write_bytes(b"x")

        self.purge()

        self.assertFalse(self.dir.exists())

    def test_purging_it_does_not_touch_the_server_root(self):
        decoy = self.root / str(self.made["id"])
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "someone-elses.warc.gz").write_bytes(b"x")

        self.purge()

        self.assertTrue(decoy.exists())


class RemoteBindingTests(StorageTestCase):
    """Naming a path is the authority of the person at the machine.

    Reachable over a network it is not: anyone who could reach the port could
    write to any directory the server can.
    """

    def setUp(self):
        super().setUp()
        self._host = srv._BIND_HOST
        self.addCleanup(setattr, srv, "_BIND_HOST", self._host)
        self._allow = srv._ALLOW_REMOTE_RECORDING
        self.addCleanup(setattr, srv, "_ALLOW_REMOTE_RECORDING", self._allow)
        srv._BIND_HOST = "0.0.0.0"
        srv._ALLOW_REMOTE_RECORDING = False

    def test_a_network_bound_dashboard_refuses_a_named_location(self):
        response = self.client.post("/api/crawls", json={
            "name": "example",
            "config": {"crawl_name": "example", "operator": "webarc",
                       "seeds": [{"url": "https://example.org/"}]},
            "storage_dir": str(self.tmp / "anywhere")})

        self.assertEqual(response.status_code, 403)

    def test_it_refuses_to_change_the_default_too(self):
        response = self.client.put(
            "/api/settings", json={"storage_root": str(self.tmp / "anywhere")})

        self.assertEqual(response.status_code, 403)

    def test_captures_without_a_named_location_still_work(self):
        made = self.crawl()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         self.root / str(made["id"]))

    def test_the_dashboard_is_told_the_field_is_unavailable(self):
        capabilities = self.client.get("/api/capabilities").json()

        self.assertFalse(capabilities["storage"]["available"])
        self.assertIn("loopback", capabilities["storage"]["reason"])

    def test_the_override_restores_it(self):
        srv._ALLOW_REMOTE_RECORDING = True

        made = self.crawl(storage_dir=str(self.tmp / "anywhere"))

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         (self.tmp / "anywhere").resolve() / str(made["id"]))


if __name__ == "__main__":
    unittest.main()
