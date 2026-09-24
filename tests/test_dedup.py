"""Deduplication across the jobs of a collection.

Within one job the WARC writer stores a repeated payload as a revisit record
pointing at the first copy. With a collection index that table is durable
and shared: a payload any job of the collection holds is referred to rather
than stored again, whichever job meets it. The index also answers what a
deletion breaks, and remembers the pages left without their original so
they can be crawled again.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from warcio.archiveiterator import ArchiveIterator

from webarc import collections as colls
from webarc import server as srv
from webarc.capture import WarcSession
from webarc.cli import main as cli_main
from webarc.config import WarcConfig
from webarc.dedup_index import CollectionIndex, url_key
from webarc.store import Store

CSS = b"body { background: rgb(1, 2, 3); }" + b" " * 4000
DIGEST = "sha1:" + hashlib.sha1(CSS).hexdigest()


def records_of(directory: Path) -> list[dict]:
    """(type, url, refers-to url, refers-to id) of every capture record."""
    out = []
    for warc in sorted(Path(directory).glob("*.warc.gz")):
        with open(warc, "rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type not in ("response", "revisit"):
                    continue
                h = record.rec_headers
                out.append({"type": record.rec_type, "url": h.get_header("WARC-Target-URI"),
                            "refers_to_url": h.get_header("WARC-Refers-To-Target-URI"),
                            "refers_to_id": h.get_header("WARC-Refers-To"),
                            "profile": h.get_header("WARC-Profile")})
    return out


class UrlKeyTests(unittest.TestCase):
    def test_the_same_page_has_one_key(self):
        self.assertEqual(url_key("HTTPS://Example.org/a?b=1&a=2#top"),
                         url_key("https://example.org/a?a=2&b=1"))
        self.assertEqual(url_key("https://example.org/a?utm_source=x&id=1"),
                         url_key("https://example.org/a?id=1"))
        self.assertEqual(url_key("https://example.org:443/"), "https://example.org/")
        self.assertNotEqual(url_key("https://example.org/a"), url_key("https://example.org/b"))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = CollectionIndex.for_collection(self._tmp.name)
        self.addCleanup(self.index.close)

    def store(self, crawl_id, url, digest="sha1:aaa", record_id="<urn:a>",
              warc_date="2026-09-01T00:00:00Z"):
        self.index.record_response(crawl_id=crawl_id, url=url, warc_date=warc_date,
                                   digest=digest, record_id=record_id, warc_file="a.warc.gz",
                                   status=200, mime="text/css", length=100)

    def test_a_payload_is_found_by_its_digest(self):
        self.assertIsNone(self.index.lookup("sha1:aaa"))
        self.store(1, "https://s/a.css")

        found = self.index.lookup("sha1:aaa")

        self.assertEqual((found["crawl_id"], found["url"], found["record_id"]),
                         (1, "https://s/a.css", "<urn:a>"))

    def test_the_earliest_original_wins(self):
        self.store(1, "https://s/first.css", record_id="<urn:first>")
        self.store(2, "https://s/second.css", record_id="<urn:second>")

        self.assertEqual(self.index.lookup("sha1:aaa")["record_id"], "<urn:first>")

    def test_revisits_count_against_the_job_they_point_into(self):
        self.store(1, "https://s/a.css")
        for n in range(3):
            self.index.record_revisit(
                crawl_id=2, url=f"https://s/b{n}.css", warc_date="2026-09-02T00:00:00Z",
                digest="sha1:aaa", record_id=f"<urn:b{n}>", warc_file="b.warc.gz",
                refers_to={"crawl_id": 1, "record_id": "<urn:a>", "url": "https://s/a.css",
                           "warc_date": "2026-09-01T00:00:00Z"}, length=100)

        self.assertEqual(self.index.referring_into(1), {"jobs": {2: 3}, "records": 3})
        self.assertEqual(self.index.referring_into(2), {"jobs": {}, "records": 0})
        self.assertEqual(self.index.referenced_jobs(2), [1])
        summary = self.index.summary(2)
        self.assertEqual(summary["revisits_across_jobs"], 3)
        self.assertEqual(summary["bytes_saved_across_jobs"], 300)
        self.assertEqual(summary["refers_to_jobs"], {"1": 3})

    def test_a_deleted_job_orphans_what_pointed_into_it(self):
        self.store(1, "https://s/a.css")
        self.index.record_revisit(
            crawl_id=2, url="https://s/page.css", warc_date="2026-09-02T00:00:00Z",
            digest="sha1:aaa", record_id="<urn:b>", warc_file="b.warc.gz",
            refers_to={"crawl_id": 1, "record_id": "<urn:a>", "url": "https://s/a.css",
                       "warc_date": "2026-09-01T00:00:00Z"}, length=100)

        self.assertEqual(self.index.forget_job(1), 1)

        self.assertIsNone(self.index.lookup("sha1:aaa"))
        self.assertEqual(self.index.orphan_urls(), ["https://s/page.css"])
        self.assertEqual(self.index.counts()["orphaned"], 1)
        self.assertEqual(self.index.referring_into(1), {"jobs": {}, "records": 0})

    def test_a_page_stored_again_is_no_longer_missing(self):
        self.test_a_deleted_job_orphans_what_pointed_into_it()

        self.store(3, "https://s/page.css", digest="sha1:bbb", record_id="<urn:c>",
                   warc_date="2026-09-03T00:00:00Z")

        self.assertEqual(self.index.orphan_urls(), [])
        self.assertEqual(self.index.last_seen("https://s/page.css")["crawl_id"], 3)


class WriterTests(unittest.TestCase):
    """The WARC writer consults the collection's index and feeds it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.index = CollectionIndex.for_collection(self.root)
        self.addCleanup(self.index.close)

    def session(self, job, index=True):
        return WarcSession(self.root / "jobs" / str(job), f"job{job}", "https://s/", 1, "op",
                           WarcConfig(), collection_index=self.index if index else None,
                           crawl_id=job)

    @staticmethod
    def write(session, url, body, mime="text/css"):
        session.write_exchange(url=url, method="GET", req_headers={}, post_data=None,
                               status=200, status_text="OK",
                               resp_headers={"content-type": mime}, body=body)

    def test_a_payload_another_job_holds_becomes_a_revisit_pointing_at_it(self):
        first = self.session(1)
        self.write(first, "https://s/style.css", CSS)
        first.close()
        second = self.session(2)
        self.write(second, "https://s/other/style.css", CSS)
        self.write(second, "https://s/new.css", b"fresh")
        second.close()

        ours = records_of(self.root / "jobs" / "2")
        self.assertEqual([r["type"] for r in ours], ["revisit", "response"])
        self.assertEqual(ours[0]["refers_to_url"], "https://s/style.css")
        self.assertIn("identical-payload-digest", ours[0]["profile"])
        theirs = records_of(self.root / "jobs" / "1")
        self.assertEqual(ours[0]["refers_to_id"], self.index.lookup(DIGEST)["record_id"])
        self.assertEqual(theirs[0]["type"], "response")

    def test_the_summary_says_what_was_reused_and_from_whom(self):
        first = self.session(1)
        self.write(first, "https://s/style.css", CSS)
        first.close()
        second = self.session(2)
        self.write(second, "https://s/a.css", CSS)
        self.write(second, "https://s/a.css", CSS)          # met again: the original is still job 1's
        self.write(second, "https://s/own.css", b"own" * 100)
        self.write(second, "https://s/own-again.css", b"own" * 100)   # within the job
        second.close()

        summary = json.loads((self.root / "jobs" / "2" / "dedup-summary.json").read_text())
        self.assertEqual(summary["revisits_across_jobs"], 2)
        self.assertEqual(summary["revisits_within_job"], 1)
        self.assertEqual(summary["refers_to_jobs"], {"1": 2})
        self.assertEqual(summary["bytes_saved"], 2 * len(CSS) + 300)
        self.assertTrue(summary["dedup_across_jobs"])

    def test_two_seeds_of_one_job_add_up(self):
        first = self.session(1)
        self.write(first, "https://s/style.css", CSS)
        first.close()
        again = self.session(1)
        self.write(again, "https://s/page.css", CSS)
        again.close()

        summary = json.loads((self.root / "jobs" / "1" / "dedup-summary.json").read_text())
        self.assertEqual(summary["responses"], 1)
        # the same job's second session finds its own earlier copy in the index
        self.assertEqual(summary["revisits_within_job"], 1)
        self.assertEqual(summary["revisits_across_jobs"], 0)

    def test_without_an_index_nothing_crosses_jobs(self):
        first = self.session(1, index=False)
        self.write(first, "https://s/style.css", CSS)
        first.close()
        second = self.session(2, index=False)
        self.write(second, "https://s/style.css", CSS)
        second.close()

        self.assertEqual([r["type"] for r in records_of(self.root / "jobs" / "2")], ["response"])
        self.assertIsNone(self.index.lookup(DIGEST))

    def test_dedup_off_for_the_job_writes_everything_in_full(self):
        cfg = WarcConfig(dedup=False)
        first = WarcSession(self.root / "jobs/1", "job1", "https://s/", 1, "op", cfg,
                            collection_index=self.index, crawl_id=1)
        self.write(first, "https://s/style.css", CSS)
        self.write(first, "https://s/style.css", CSS)
        first.close()

        self.assertEqual([r["type"] for r in records_of(self.root / "jobs" / "1")],
                         ["response", "response"])


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)

    def collection(self, **extra):
        made = self.client.post("/api/collections", json={"name": "QNL", **extra})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def job(self, collection, name="demo"):
        made = self.client.post("/api/crawls", json={
            "name": name, "start": "wait", "collection_id": collection["id"],
            "config": {"operator": "QNL", "seeds": [{"url": "https://s/"}]}})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def write(self, collection, job, pairs):
        """Write records for a job as its worker would, through the index."""
        index = colls.open_index(collection)
        session = WarcSession(Path(job["output_dir"]), job["name"], "https://s/", 1, "op",
                              WarcConfig(), collection_index=index, crawl_id=job["id"])
        for url, body in pairs:
            session.write_exchange(url=url, method="GET", req_headers={}, post_data=None,
                                   status=200, status_text="OK",
                                   resp_headers={"content-type": "text/css"}, body=body)
        session.close()
        if index is not None:
            index.close()


class ServerTests(ServerTestCase):
    def test_the_policy_is_on_by_default_and_can_be_turned_off(self):
        on = self.collection()
        self.assertTrue(on["policy"]["dedup_across_jobs"])
        off = self.client.post("/api/collections",
                               json={"name": "Full", "dedup_across_jobs": False}).json()
        self.assertFalse(off["policy"]["dedup_across_jobs"])
        job = self.job(off)
        self.assertFalse(job["collection"]["dedup_across_jobs"])
        self.assertIsNone(colls.open_index(job["collection"]))

        changed = self.client.put(f"/api/collections/{off['id']}", json={"dedup_across_jobs": True})
        self.assertTrue(changed.json()["policy"]["dedup_across_jobs"])

    def test_the_impact_of_deleting_a_job_counts_the_records_that_refer_into_it(self):
        made = self.collection()
        first = self.job(made, "first")
        second = self.job(made, "second")
        self.write(made, first, [("https://s/style.css", CSS)])
        self.write(made, second, [("https://s/a.css", CSS), ("https://s/b.css", CSS)])

        impact = self.client.get(f"/api/crawls/{first['id']}/impact").json()

        self.assertEqual(impact["referring_records"], 2)
        self.assertEqual(impact["referring_jobs"],
                         [{"id": second["id"], "name": "second", "records": 2}])
        self.assertTrue(impact["breaks_replay_elsewhere"])
        self.assertIn("replay without it", impact["note"])
        self.assertEqual(self.client.get(f"/api/crawls/{second['id']}/impact").json()
                         ["referring_records"], 0)

    def test_the_job_view_and_the_collection_say_what_was_reused(self):
        made = self.collection()
        first = self.job(made, "first")
        second = self.job(made, "second")
        self.write(made, first, [("https://s/style.css", CSS)])
        self.write(made, second, [("https://s/a.css", CSS)])

        view = self.client.get(f"/api/crawls/{second['id']}").json()
        self.assertEqual(view["dedup"]["revisits_across_jobs"], 1)
        self.assertEqual(view["dedup"]["refers_to_jobs"], {str(first["id"]): 1})
        listed = self.client.get("/api/collections").json()[0]
        self.assertEqual(listed["index"]["revisits"], 1)
        self.assertEqual(listed["index"]["originals"], 1)
        self.assertEqual(listed["index"]["bytes_saved"], len(CSS))

    def test_deleting_the_job_leaves_orphans_that_a_recrawl_restores(self):
        made = self.collection()
        first = self.job(made, "first")
        second = self.job(made, "second")
        self.write(made, first, [("https://s/style.css", CSS)])
        self.write(made, second, [("https://s/a.css", CSS)])

        gone = self.client.delete(f"/api/crawls/{first['id']}?purge=true").json()

        self.assertEqual(gone["orphaned_records"], 1)
        orphans = self.client.get(f"/api/collections/{made['id']}/orphans").json()
        self.assertEqual(orphans["urls"], ["https://s/a.css"])
        self.assertEqual(self.client.get("/api/collections").json()[0]["index"]["orphaned"], 1)

        third = self.job(made, "recrawl")
        self.write(made, third, [("https://s/a.css", CSS)])
        self.assertEqual(self.client.get(f"/api/collections/{made['id']}/orphans").json()["urls"], [])
        # and the payload is held again, in full, by the re-crawl
        self.assertEqual([r["type"] for r in records_of(Path(third["output_dir"]))], ["response"])

    def test_a_jobs_replay_brings_the_warcs_it_refers_into(self):
        made = self.collection()
        first = self.job(made, "first")
        second = self.job(made, "second")
        self.write(made, first, [("https://s/style.css", CSS)])
        self.write(made, second, [("https://s/a.css", CSS)])

        with mock.patch("webarc.replay.build_replay_site") as build_site, \
                mock.patch("webarc.replay.ReplayServer") as server_cls:
            server_cls.return_value.is_running.return_value = True
            server_cls.return_value.replay_url.return_value = "http://replay/"
            srv._PYWB = server_cls.return_value
            try:
                response = self.client.post(f"/api/crawls/{second['id']}/replay")
            finally:
                srv._PYWB = None
        self.assertEqual(response.status_code, 200, response.text)
        warcs = [Path(p).parent.name for p in build_site.call_args[0][0]]
        self.assertEqual(sorted(set(warcs)), sorted({str(first["id"]), str(second["id"])}))

    def test_a_collection_that_stores_in_full_refers_nowhere(self):
        made = self.collection(dedup_across_jobs=False)
        first = self.job(made, "first")
        second = self.job(made, "second")
        self.write(made, first, [("https://s/style.css", CSS)])
        self.write(made, second, [("https://s/a.css", CSS)])

        self.assertEqual([r["type"] for r in records_of(Path(second["output_dir"]))], ["response"])
        impact = self.client.get(f"/api/crawls/{first['id']}/impact").json()
        self.assertEqual(impact["referring_records"], 0)
        self.assertIn("does not deduplicate across jobs", impact["note"])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "swm.db")
        self.root = str(self.tmp / "warcs")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_policy_can_be_set_and_is_shown(self):
        code, out, _ = self.run_cli("collection", "create", "Full", "--db", self.db,
                                    "--warc-root", self.root, "--no-cross-job-dedup")
        self.assertEqual(code, 0, out)
        self.assertIn("stored in full", out)
        self.assertFalse(colls.policy_of(Store(self.db).find_collection("full"))["dedup_across_jobs"])

        code, out, _ = self.run_cli("collection", "create", "Once", "--db", self.db,
                                    "--warc-root", self.root)
        self.assertIn("stored once", out)

    def test_show_reports_the_index(self):
        self.run_cli("collection", "create", "Once", "--db", self.db, "--warc-root", self.root)
        collection = Store(self.db).find_collection("once")
        index = colls.open_index(collection)
        index.record_response(crawl_id=1, url="https://s/a.css", warc_date="t", digest="sha1:x",
                              record_id="<a>", warc_file="a.warc.gz", length=10)
        index.close()

        code, out, _ = self.run_cli("collection", "show", "once", "--db", self.db)

        self.assertEqual(code, 0, out)
        self.assertIn("1 original(s), 0 revisit(s)", out)


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTests(unittest.TestCase):
    """Two jobs of one collection at once, and the dashboard reading while
    they run: nobody waits on anybody's unfinished batch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_sibling_opens_and_writes_while_another_job_is_mid_run(self):
        import time
        first = CollectionIndex.for_collection(self.root)
        self.addCleanup(first.close)
        first.record_response(crawl_id=1, url="https://s/a.css", warc_date="2026-09-01T00:00:00Z",
                              digest="sha1:aaa", record_id="<urn:a>", warc_file="a.warc.gz")
        started = time.monotonic()
        second = CollectionIndex.for_collection(self.root)      # opens without a write lock
        self.addCleanup(second.close)
        second.record_response(crawl_id=2, url="https://s/b.css", warc_date="2026-09-01T00:00:01Z",
                               digest="sha1:bbb", record_id="<urn:b>", warc_file="b.warc.gz")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(first.lookup("sha1:bbb")["crawl_id"], 2)   # sees it at once
        self.assertEqual(second.lookup("sha1:aaa")["crawl_id"], 1)
        self.assertEqual(second.counts()["records"], 2)

    def test_a_revisit_into_a_job_deleted_meanwhile_is_orphaned_from_the_start(self):
        index = CollectionIndex.for_collection(self.root)
        self.addCleanup(index.close)
        index.record_response(crawl_id=1, url="https://s/a.css", warc_date="2026-09-01T00:00:00Z",
                              digest="sha1:aaa", record_id="<urn:a>", warc_file="a.warc.gz")
        index.forget_job(1)
        index.record_revisit(crawl_id=2, url="https://s/x/a.css", warc_date="2026-09-01T00:00:01Z",
                             digest="sha1:aaa", record_id="<urn:r>", warc_file="b.warc.gz",
                             refers_to={"crawl_id": 1, "record_id": "<urn:a>",
                                        "url": "https://s/a.css",
                                        "warc_date": "2026-09-01T00:00:00Z"})
        self.assertEqual(index.counts()["orphaned"], 1)
        self.assertEqual(index.orphan_urls(), ["https://s/x/a.css"])
        self.assertEqual(index.removed_jobs(), {1})

    def test_url_key_survives_a_port_that_is_not_a_number(self):
        self.assertEqual(url_key("http://h:abc/x"), "http://h:abc/x")


class WriterSafetyTests(WriterTests):
    """What the writer tells the index describes records that exist."""

    def test_a_failed_warc_write_leaves_no_index_row_and_no_digest_to_refer_to(self):
        session = self.session(1)
        with mock.patch.object(session._writer, "write_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.write(session, "https://s/style.css", CSS)
        self.assertIsNone(self.index.lookup(DIGEST))
        self.assertEqual(self.index.counts()["records"], 0)
        # The next attempt stores the payload, as no record holds it.
        self.write(session, "https://s/style.css", CSS)
        session.close()
        self.assertEqual([r["type"] for r in records_of(self.root / "jobs" / "1")], ["response"])
        self.assertEqual(self.index.lookup(DIGEST)["crawl_id"], 1)

    def test_an_original_deleted_during_the_run_is_not_referred_to_again(self):
        first = self.session(1)
        self.write(first, "https://s/style.css", CSS)
        first.close()
        second = self.session(2)
        self.write(second, "https://s/one/style.css", CSS)        # a revisit into job 1
        self.index.forget_job(1)                                    # job 1 deleted meanwhile
        self.write(second, "https://s/two/style.css", CSS)        # stored: nothing holds it now
        second.close()
        kinds = [r["type"] for r in records_of(self.root / "jobs" / "2")]
        self.assertEqual(kinds, ["revisit", "response"])
        self.assertEqual(self.index.counts()["orphaned"], 1)
        self.assertEqual(self.index.lookup(DIGEST)["crawl_id"], 2)


class DeletionOrderTests(ServerTestCase):
    """Deleting a job updates the index before anything is removed, and a
    collection deleted without purge leaves no index for a namesake."""

    def test_job_delete_forgets_the_job_in_the_index_before_removing_it(self):
        coll = self.collection()
        first, second = self.job(coll, "first"), self.job(coll, "second")
        self.write(coll, first, [("https://s/style.css", CSS)])
        self.write(coll, second, [("https://s/p/style.css", CSS)])
        with mock.patch.object(CollectionIndex, "forget_job", side_effect=RuntimeError("locked")):
            refused = self.client.delete(f"/api/crawls/{first['id']}?purge=true")
        self.assertEqual(refused.status_code, 503, refused.text)
        self.assertIn("nothing was deleted", refused.json()["detail"])
        self.assertTrue(Path(first["output_dir"]).exists())
        self.assertEqual(self.client.get(f"/api/crawls/{first['id']}").status_code, 200)

        done = self.client.delete(f"/api/crawls/{first['id']}?purge=true")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["orphaned_records"], 1)
        self.assertFalse(Path(first["output_dir"]).exists())
        index = colls.read_index(coll)
        try:
            self.assertIsNone(index.lookup(DIGEST))
            self.assertEqual(index.counts()["orphaned"], 1)
        finally:
            index.close()

    def test_a_collection_deleted_without_purge_leaves_no_index_behind(self):
        coll = self.collection()
        job = self.job(coll)
        self.write(coll, job, [("https://s/style.css", CSS)])
        root = Path(coll["root_dir"])
        self.assertTrue((root / "index.sqlite").exists())
        gone = self.client.delete(f"/api/collections/{coll['id']}")
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertTrue(Path(job["output_dir"]).exists())          # the WARCs stay
        self.assertFalse((root / "index.sqlite").exists())
        self.assertFalse((root / "index.sqlite-wal").exists())
        again = self.client.post("/api/collections", json={"name": "QNL"})
        self.assertEqual(again.status_code, 201, again.text)    # a namesake starts clean

    def test_a_leftover_index_at_the_target_root_refuses_the_new_collection(self):
        root = self.tmp / "warcs" / "collections" / "qnl"
        root.mkdir(parents=True)
        CollectionIndex.for_collection(root).close()
        refused = self.client.post("/api/collections", json={"name": "QNL"})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("earlier collection", refused.json()["detail"])
        self.assertEqual(self.client.get("/api/collections").json(), [])
