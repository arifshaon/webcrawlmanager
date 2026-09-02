"""Where each capture is written, and where it is read back from.

Every path in the server used to be recomputed as <server root>/<crawl id>.
That held only while every crawl lived under the one root: a crawl given its
own storage location has to be read back from where it was written, or its
files, its size and its replay all point at an empty directory.
"""
from __future__ import annotations

import json
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


class BrowsingTests(StorageTestCase):
    """A browser cannot hand back a filesystem path.

    A native picker gives a handle or a relative name, neither of which the
    worker could write to, and captures are written by the server anyway --
    so the directories worth choosing among are the server's own.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.tmp / "collections"
        (self.tree / "qnl-2026").mkdir(parents=True)
        (self.tree / "manara").mkdir()
        (self.tree / ".hidden").mkdir()
        (self.tree / "notes.txt").write_text("not a directory")

    def browse(self, path=None, **params):
        query = dict(params)
        if path is not None:
            query["path"] = str(path)
        response = self.client.get("/api/browse", params=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_it_lists_the_directories_inside_one(self):
        listing = self.browse(self.tree)

        self.assertEqual([e["name"] for e in listing["entries"]],
                         ["manara", "qnl-2026"])

    def test_it_does_not_list_files(self):
        names = [e["name"] for e in self.browse(self.tree)["entries"]]

        self.assertNotIn("notes.txt", names)

    def test_hidden_directories_are_out_of_the_way_but_reachable(self):
        self.assertNotIn(
            ".hidden", [e["name"] for e in self.browse(self.tree)["entries"]])
        self.assertIn(
            ".hidden", [e["name"] for e in
                        self.browse(self.tree, show_hidden="true")["entries"]])

    def test_each_entry_carries_the_path_to_use(self):
        entry = self.browse(self.tree)["entries"][0]

        self.assertEqual(Path(entry["path"]), self.tree / "manara")

    def test_it_offers_the_way_back_up(self):
        listing = self.browse(self.tree / "qnl-2026")

        self.assertEqual(Path(listing["parent"]), self.tree)

    def test_the_top_of_the_tree_has_no_parent(self):
        self.assertIsNone(self.browse("/")["parent"])

    def test_it_says_whether_the_folder_can_be_written_to(self):
        self.assertTrue(self.browse(self.tree)["writable"])

    def test_it_offers_somewhere_to_start(self):
        places = self.browse(self.tree)["places"]

        self.assertTrue(places)
        self.assertIn(str(self.root), [p["path"] for p in places])

    def test_a_file_is_not_a_place_to_look(self):
        response = self.client.get(
            "/api/browse", params={"path": str(self.tree / "notes.txt")})

        self.assertEqual(response.status_code, 400)
        self.assertIn("not a directory", response.text)

    def test_somewhere_that_is_not_there_is_refused(self):
        response = self.client.get(
            "/api/browse", params={"path": str(self.tmp / "no-such-place")})

        self.assertEqual(response.status_code, 400)

    @unittest.skipIf(os.geteuid() == 0, "root can read any directory")
    def test_an_unreadable_folder_reports_rather_than_fails(self):
        """The curator can still go back up or type a path."""
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)

        listing = self.browse(locked)

        self.assertEqual(listing["entries"], [])
        self.assertIn("cannot be read", listing["unreadable"])

    def test_a_chosen_folder_is_one_a_capture_can_use(self):
        chosen = self.browse(self.tree)["entries"][0]["path"]

        made = self.crawl(storage_dir=chosen)

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         Path(chosen) / str(made["id"]))


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

    def test_it_will_not_list_the_servers_directories(self):
        """Listing them is the same authority as writing to them."""
        response = self.client.get("/api/browse", params={"path": "/"})

        self.assertEqual(response.status_code, 403)

    def test_the_override_restores_it(self):
        srv._ALLOW_REMOTE_RECORDING = True

        made = self.crawl(storage_dir=str(self.tmp / "anywhere"))

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         (self.tmp / "anywhere").resolve() / str(made["id"]))


if __name__ == "__main__":
    unittest.main()


class InstagramApiTests(StorageTestCase):
    """The Instagram job: targets as seeds, background by default."""

    def create(self, **payload):
        body = {"targets": ["qnl", "https://www.instagram.com/p/Cabc123/"],
                "mode": "latest_n", "latest_n": 5, **payload}
        return self.client.post("/api/instagram", json=body)

    def test_a_job_is_created_with_one_seed_per_target(self):
        response = self.create()

        self.assertEqual(response.status_code, 201, response.text)
        made = response.json()
        self.assertEqual(made["kind"], "instagram")
        self.assertEqual(made["seeds_total"], 2)
        self.assertEqual(made["name"], "ig-qnl-post-Cabc123")

    def test_a_bad_target_is_refused_with_the_reason(self):
        response = self.create(targets=["https://www.instagram.com/explore/tags/doha/"])

        self.assertEqual(response.status_code, 400)
        self.assertIn("ranking", response.text)

    def test_since_last_without_prior_state_is_refused(self):
        response = self.create(mode="since_last", targets=["qnl"])

        self.assertEqual(response.status_code, 400)
        self.assertIn("No previous capture state", response.text)

    def test_the_state_endpoint_reports_what_is_known(self):
        self.assertFalse(self.client.get(
            "/api/instagram/state", params={"target": "qnl"}).json()["available"])
        srv._store().record_instagram_capture(3, {
            "instagram:@qnl": {"media_id": "9", "date": "2026-03-01T00:00:00Z",
                               "username": "qnl", "url": "https://www.instagram.com/qnl/"}}, [])

        state = self.client.get("/api/instagram/state", params={"target": "qnl"}).json()

        self.assertTrue(state["available"])
        self.assertEqual(state["state"]["newest_media_id"], "9")

    def test_it_honours_a_storage_location(self):
        mine = self.tmp / "instagram-collection"

        made = self.create(storage_dir=str(mine)).json()

        self.assertEqual(Path(self.row(made["id"])["output_dir"]),
                         mine.resolve() / str(made["id"]))

    def test_the_session_comes_from_the_dedicated_profile(self):
        made = self.create().json()
        stored = json.loads(srv._store().get_crawl(made["id"])["config_json"])

        self.assertTrue(stored["instagram"]["browser_profile_dir"].endswith(
            str(Path("browser-profiles") / "instagram")))

    def test_the_capability_is_reported(self):
        capabilities = self.client.get("/api/capabilities").json()

        self.assertIn("instagram", capabilities)
        self.assertIn("available", capabilities["instagram"])

    def test_replay_offers_the_pages_of_a_package(self):
        made = self.create().json()
        crawl_dir = Path(self.row(made["id"])["output_dir"])
        srv._store().set_pid(made["id"], None)
        (crawl_dir / "instagram-posts.jsonl").write_text(json.dumps({
            "media_id": "1", "shortcode": "Cabc123", "kind": "image",
            "created_time": "2026-03-01T00:00:00Z", "caption": "hello",
            "media_urls": [], "media_files": []}) + "\n")
        (crawl_dir / "instagram-manifest.json").write_text(json.dumps({
            "capture": {"targets": [{"url": "https://www.instagram.com/qnl/",
                                     "label": "@qnl"}], "mode": "latest_n"}}))

        response = self.client.post(f"/api/crawls/{made['id']}/replay")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "capture_pages")
        served = self.client.get(response.json()["pages_url"])
        self.assertEqual(served.status_code, 200)
        self.assertIn("hello", self.client.get(
            f"/captures/{made['id']}/pages/posts/Cabc123.html").text)
