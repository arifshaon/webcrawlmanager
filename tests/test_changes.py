"""Page change detection: what a job found new, changed, unchanged or gone
against the collection's earlier captures, by the words and links of each
page rather than its bytes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from webarc import changes
from webarc import collections as colls
from webarc import server as srv
from webarc.capture import WarcSession
from webarc.config import WarcConfig
from webarc.dedup_index import CollectionIndex, note_page, rebuild
from webarc.store import Store

PAGE = ("<!doctype html><html><head><title>Home</title><script>var nonce='%s';</script>"
        "<link rel=stylesheet href=/s.css></head><body class='%s'>"
        "<!-- cached %s --><ul><li class='%s'><a href='/about/'>About</a></li>"
        "<li><a href='/%s/'>%s</a></li></ul><h1>Welcome</h1><p>%s</p></body></html>")


def page(nonce="a1", cls="home", stamp="10:41", current="", link="programme", label="Programme",
         text="News of the day."):
    return (PAGE % (nonce, cls, stamp, current, link, label, text)).encode()


class FingerprintTests(unittest.TestCase):
    def test_markup_noise_does_not_change_the_fingerprint(self):
        base = changes.page_fingerprint(page())
        self.assertEqual(changes.page_fingerprint(page(nonce="zz9", stamp="15:02", current="current",
                                                       cls="home page-loaded")), base)
        self.assertEqual(changes.page_fingerprint(page().replace(b"<p>", b"<p  >\n  ")), base)

    def test_words_or_links_change_it(self):
        base = changes.page_fingerprint(page())
        self.assertNotEqual(changes.page_fingerprint(page(text="Newer news.")), base)
        self.assertNotEqual(changes.page_fingerprint(page(link="virtual", label="Virtual")), base)
        self.assertNotEqual(changes.page_fingerprint(page(label="Programme overview")), base)

    def test_scripts_styles_and_comments_are_not_content(self):
        one = b"<html><body><script>x=1</script><style>a{}</style><!-- c --><p>Hi</p></body></html>"
        two = b"<html><body><script>x=2</script><style>b{}</style><!-- d --><p>Hi</p></body></html>"
        self.assertEqual(changes.page_fingerprint(one), changes.page_fingerprint(two))

    def test_classification(self):
        fp = changes.page_fingerprint(page())
        self.assertEqual(changes.classify(None, fp, 200), "new")
        self.assertEqual(changes.classify({"status": 200, "fingerprint": fp}, fp, 200), "unchanged")
        self.assertEqual(changes.classify({"status": 200, "fingerprint": "sha1:x"}, fp, 200), "changed")
        self.assertEqual(changes.classify({"status": 200, "fingerprint": fp}, None, 404), "gone")
        self.assertIsNone(changes.classify(None, None, 404))          # never held: no event
        self.assertIsNone(changes.classify({"status": 200, "fingerprint": fp}, None, 500))
        self.assertEqual(changes.classify({"status": 404}, fp, 200), "new")   # back after gone
        self.assertTrue(changes.is_page(200, "text/html; charset=utf-8"))
        self.assertFalse(changes.is_page(200, "text/css"))
        self.assertEqual(changes.charset_of('text/html; charset="ISO-8859-1"'), "ISO-8859-1")


class _WriterCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.index = CollectionIndex.for_collection(self.root)
        self.addCleanup(self.index.close)

    def session(self, job):
        return WarcSession(self.root / "jobs" / str(job), f"job{job}", "https://s/", 1, "op",
                           WarcConfig(), collection_index=self.index, crawl_id=job)

    @staticmethod
    def write(session, url, body, status=200, mime="text/html; charset=utf-8"):
        session.write_exchange(url=url, method="GET", req_headers={}, post_data=None,
                               status=status, status_text="OK",
                               resp_headers={"content-type": mime}, body=body)



class WriterTests(_WriterCase):
    """The WARC writer notes each page as it stores it."""

    def test_new_then_unchanged_changed_and_gone_across_jobs(self):
        first = self.session(1)
        self.write(first, "https://s/", page())
        self.write(first, "https://s/about/", page(text="About us."))
        self.write(first, "https://s/old/", page(text="Old."))
        self.write(first, "https://s/s.css", b"body{}", mime="text/css")    # not a page
        first.close()
        self.assertEqual(first.change_stats, {"new": 3})

        second = self.session(2)
        self.write(second, "https://s/?utm_source=x", page(nonce="b2", stamp="now"))   # same page
        self.write(second, "https://s/about/", page(text="About us, updated."))
        self.write(second, "https://s/old/", b"<h1>Not found</h1>", status=404)
        self.write(second, "https://s/fresh/", page(text="Fresh."))
        self.write(second, "https://s/never/", b"<h1>Not found</h1>", status=404)   # never held
        second.close()
        self.assertEqual(second.change_stats, {"unchanged": 1, "changed": 1, "gone": 1, "new": 1})

        report = changes.write_report(self.index, 2, self.root / "jobs" / "2")
        self.assertEqual(report["counts"], {"new": 1, "changed": 1, "unchanged": 1, "gone": 1,
                                            "not_visited": 0})
        self.assertEqual([e["url"] for e in report["changed"]], ["https://s/about/"])
        self.assertEqual(report["changed"][0]["previous_job"], 1)
        self.assertEqual([e["url"] for e in report["gone"]], ["https://s/old/"])
        self.assertTrue((self.root / "jobs" / "2" / "changes.json").is_file())

        third = self.session(3)
        self.write(third, "https://s/", page())
        third.close()
        report = changes.write_report(self.index, 3, self.root / "jobs" / "3")
        self.assertEqual(report["counts"]["unchanged"], 1)
        # held by earlier jobs, not reached by this one; the gone page is not listed
        self.assertEqual(sorted(e["url"] for e in report["not_visited"]),
                         ["https://s/about/", "https://s/fresh/"])

    def test_a_page_stored_as_a_revisit_is_still_a_page_event(self):
        first = self.session(1)
        self.write(first, "https://s/", page())
        first.close()
        second = self.session(2)
        self.write(second, "https://s/", page())                 # identical bytes: a revisit
        second.close()
        self.assertEqual(second.dedup_stats["revisits_across_jobs"], 1)
        self.assertEqual(second.change_stats, {"unchanged": 1})

    def test_forgetting_a_job_forgets_its_pages(self):
        first = self.session(1)
        self.write(first, "https://s/", page())
        first.close()
        self.index.forget_job(1)
        self.assertIsNone(self.index.last_page("https://s/"))
        second = self.session(2)
        self.write(second, "https://s/", page())
        second.close()
        self.assertEqual(second.change_stats, {"new": 1})

    def test_an_index_from_before_page_tracking_gains_the_table_on_open(self):
        import sqlite3
        from webarc.dedup_index import _SCHEMA
        old = self.root / "old" / "index.sqlite"
        old.parent.mkdir()
        conn = sqlite3.connect(old)
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema', '1')")
        conn.commit(); conn.close()
        index = CollectionIndex(old)
        try:
            self.assertIsNone(index.last_page("https://s/"))
            self.assertEqual(index.page_counts(1), {"new": 0, "changed": 0, "unchanged": 0, "gone": 0})
        finally:
            index.close()


class RebuildTests(_WriterCase):
    def test_a_rebuild_classifies_the_pages_too(self):
        first = self.session(1)
        self.write(first, "https://s/", page())
        self.write(first, "https://s/about/", page(text="About."))
        first.close()
        second = self.session(2)
        self.write(second, "https://s/", page())                          # revisit, unchanged
        self.write(second, "https://s/about/", page(text="About, new."))  # changed
        second.close()
        colls.remove_index(self.root)
        result = rebuild(self.root, [(1, self.root / "jobs" / "1"), (2, self.root / "jobs" / "2")])
        self.assertEqual(result["jobs"][1]["pages"], {"new": 2, "changed": 0, "unchanged": 0, "gone": 0})
        self.assertEqual(result["jobs"][2]["pages"], {"new": 0, "changed": 1, "unchanged": 1, "gone": 0})
        report = json.loads((self.root / "jobs" / "2" / "changes.json").read_text())
        self.assertEqual([e["url"] for e in report["unchanged"]], ["https://s/"])


class EndOfJobTests(unittest.TestCase):
    """The report is written when a job ends, by the worker and by the
    command line, and the dashboard shows the counts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)
        self.coll = self.client.post("/api/collections", json={"name": "QNL"}).json()

    def job(self, name):
        return self.client.post("/api/crawls", json={
            "name": name, "start": "wait", "collection_id": self.coll["id"],
            "config": {"seeds": [{"url": "https://s/"}]}}).json()

    def write(self, job, pairs):
        index = colls.open_index(self.coll)
        session = WarcSession(Path(job["output_dir"]), job["name"], "https://s/", 1, "op",
                              WarcConfig(), collection_index=index, crawl_id=job["id"])
        for url, body in pairs:
            session.write_exchange(url=url, method="GET", req_headers={}, post_data=None,
                                   status=200, status_text="OK",
                                   resp_headers={"content-type": "text/html"}, body=body)
        session.close(); index.close()

    def test_worker_and_command_line_write_the_report_and_the_row_shows_it(self):
        from webarc import worker
        from webarc.cli import _settle_registered_job

        first, second = self.job("first"), self.job("second")
        self.write(first, [("https://s/", page()), ("https://s/a/", page(text="A"))])
        store = srv._store()
        store.set_status(first["id"], "completed")
        worker._note_job_end(store, first["id"])
        self.write(second, [("https://s/", page()), ("https://s/a/", page(text="A2"))])
        _settle_registered_job((store, second["id"]), "completed")

        view = self.client.get(f"/api/crawls/{second['id']}").json()
        self.assertEqual(view["changes"], {"new": 0, "changed": 1, "unchanged": 1, "gone": 0,
                                           "not_visited": 0})
        self.assertEqual(self.client.get(f"/api/crawls/{first['id']}").json()["changes"]["new"], 2)
        report = self.client.get(f"/api/crawls/{second['id']}/changes")
        self.assertEqual(report.status_code, 200)
        self.assertEqual([e["url"] for e in report.json()["changed"]], ["https://s/a/"])
        lone = self.client.post("/api/crawls", json={
            "name": "lone", "start": "wait", "config": {"seeds": [{"url": "https://s/"}]}}).json()
        self.assertEqual(self.client.get(f"/api/crawls/{lone['id']}/changes").status_code, 404)
        self.assertIsNone(self.client.get(f"/api/crawls/{lone['id']}").json()["changes"])


class ReviewFixTests(_WriterCase):
    def test_a_page_without_a_closing_head_tag_still_has_its_text_compared(self):
        one = b"<html><head><title>t</title><body><p>one text</p></body></html>"
        two = b"<html><head><title>t</title><body><p>completely different</p></body></html>"
        self.assertNotEqual(changes.page_fingerprint(one), changes.page_fingerprint(two))

    def test_an_unknown_charset_does_not_stop_the_fingerprint(self):
        body = "<html><body><p>café</p></body></html>".encode("utf-8")
        self.assertEqual(changes.page_fingerprint(body, "none"), changes.page_fingerprint(body))
        self.assertEqual(changes.page_fingerprint(body, "utf8mb4"), changes.page_fingerprint(body))

    def test_a_sibling_running_at_the_same_time_is_not_the_previous_capture(self):
        earlier = self.session(4)
        self.write(earlier, "https://s/", page(text="Old text."))
        earlier.close()
        later_sibling = self.session(6)                 # started first, higher number
        self.write(later_sibling, "https://s/", page(text="Old text."))
        self.write(later_sibling, "https://s/only-six/", page(text="Six."))
        later_sibling.close()
        this = self.session(5)
        self.write(this, "https://s/", page(text="Old text."))
        this.close()
        report = changes.write_report(self.index, 5, self.root / "jobs" / "5")
        self.assertEqual(report["counts"]["unchanged"], 1)
        self.assertEqual(report["unchanged"][0]["previous_job"], 4)     # not 6
        self.assertEqual(report["not_visited"], [])                     # job 6's page is not "earlier"

    def test_a_rebuild_resolves_a_revisit_into_a_higher_numbered_job(self):
        six = self.session(6)                           # started first, stored the original
        self.write(six, "https://s/", page())
        six.close()
        five = self.session(5)
        self.write(five, "https://s/", page())          # identical: a revisit into job 6
        five.close()
        self.assertEqual(five.dedup_stats["revisits_across_jobs"], 1)
        colls.remove_index(self.root)
        result = rebuild(self.root, [(5, self.root / "jobs" / "5"), (6, self.root / "jobs" / "6")])
        self.assertEqual(result["unresolved_revisits"], 0)
        self.assertEqual(result["orphaned"], 0)
        self.assertEqual(result["jobs"][5]["revisits_across_jobs"], 1)
        self.assertEqual(result["jobs"][5]["refers_to_jobs"], {"6": 1})
        self.assertGreater(result["jobs"][5]["bytes_saved"], 0)
        self.assertEqual(result["jobs"][5]["pages"]["new"], 1)          # nothing earlier than 5 held it
