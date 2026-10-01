"""A themed job's selection report: every page accepted or not, with its
score against the score needed and the reason, and the chosen pages
recrawled on their own, without the theme."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from webarc.store import Store


def write_log(job_dir: Path, rows: list[dict], theme: dict) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "selection.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (job_dir / "theme-summary.json").write_text(json.dumps({"theme": theme, "policy": "rules_only"}),
                                                encoding="utf-8")


PAGES = [
    {"kind": "page", "url": "https://news.example/", "decision": "keep", "hub": True,
     "rules": {"score": 0, "reasons": []}, "page": {"title": "News"}},
    {"kind": "page", "url": "https://news.example/library-opens", "decision": "keep",
     "rules": {"score": 4, "reasons": ["1 term(s) in the headline", "1 mention(s) in the text"]},
     "page": {"headline": "Library opens", "published": "2026-09-01T08:00:00Z"}},
    {"kind": "page", "url": "https://news.example/budget", "decision": "unsure",
     "rules": {"score": 2, "reasons": ["2 mention(s) in the text", "score 2 is below 3"],
               "matched": [{"term": "library", "where": "text"}]},
     "page": {"headline": "City budget"}},
    {"kind": "page", "url": "https://news.example/football", "decision": "reject",
     "rules": {"score": 0, "reasons": ["excluded term 'football' in the headline"], "hard": True},
     "page": {"headline": "Football final"}},
    {"kind": "link", "url": "https://news.example/login", "decision": "skip",
     "reasons": ["URL matches the exclusion rule '/login'"], "text": "Sign in"},
    {"kind": "link", "url": "https://news.example/budget", "decision": "skip", "reasons": ["x"]},
]


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.job = Path(self._dir.name) / "job"

    def test_every_page_says_whether_it_was_accepted_and_why(self):
        from webarc.theme import selection_report
        write_log(self.job, PAGES, {"name": "Libraries", "min_score": 3, "unsure_action": "reject"})
        report = selection_report(self.job)
        pages = {p["url"].rsplit("/", 1)[-1]: p for p in report["pages"]}

        self.assertEqual(report["counts"], {"accepted": 2, "not_accepted": 2, "links_not_followed": 1})
        self.assertEqual(pages["budget"]["why"], "Not enough evidence: score 2 (3 needed): 2 mention(s) in the text")
        self.assertEqual((pages["budget"]["score"], pages["budget"]["needed"]), (2, 3))
        self.assertEqual(pages["football"]["why"], "Excluded term 'football' in the headline")
        self.assertEqual(pages["library-opens"]["published"], "2026-09-01")
        self.assertTrue(pages[""]["why"].startswith("A hub page"))
        # a link that was fetched from another page after all is a page, not a link left out
        self.assertEqual([l["url"] for l in report["links"]], ["https://news.example/login"])
        self.assertFalse(report["review_folder"])

    def test_a_job_from_before_review_archives_were_retired_still_reads(self):
        from webarc.theme import selection_report
        write_log(self.job, PAGES, {"name": "Libraries", "min_score": 3, "unsure_action": "review"})
        (self.job / "review").mkdir()
        report = selection_report(self.job)
        budget = next(p for p in report["pages"] if p["url"].endswith("/budget"))

        self.assertFalse(budget["accepted"])
        self.assertTrue(budget["held_for_review"])
        self.assertTrue(report["review_folder"])

    def test_the_endpoint_names_the_job_and_its_collection_for_a_recrawl(self):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:                            # pragma: no cover
            raise unittest.SkipTest(f"dashboard dependencies missing: {exc}")
        from webarc.server import create_app
        root = Path(self._dir.name)
        client = TestClient(create_app(str(root / "swm.db"), str(root / "warcs"),
                                       replay_root=str(root / "replay"), monitor_resources=False))
        coll = client.post("/api/collections", json={"name": "Library news"}).json()
        store = Store(str(root / "swm.db"))
        themed = store.create_crawl("lib", {"seeds": [{"url": "https://news.example/"}]}, str(self.job), 1,
                                    collection_id=coll["id"])
        plain = store.create_crawl("plain", {"seeds": [{"url": "https://x.example/"}]},
                                   str(root / "plain"), 1)
        write_log(self.job, PAGES, {"name": "Libraries", "min_score": 3})

        report = client.get(f"/api/crawls/{themed}/selection").json()
        self.assertEqual(report["job"], {"id": themed, "name": "lib", "collection_id": coll["id"]})
        self.assertEqual(report["counts"]["not_accepted"], 2)
        self.assertEqual(client.get(f"/api/crawls/{plain}/selection").status_code, 404)
        jobs = {c["id"]: c for c in client.get("/api/crawls").json()}
        self.assertTrue(jobs[themed]["has_selection"])
        self.assertFalse(jobs[plain]["has_selection"])


class RecrawlTests(unittest.TestCase):
    """In a browser: the report opens from the job, and the pages chosen in
    it become a crawl of only those pages, without the theme."""

    @classmethod
    def setUpClass(cls):
        try:
            import uvicorn
            from playwright.sync_api import sync_playwright
        except ImportError as exc:                          # pragma: no cover
            raise unittest.SkipTest(f"browser test dependencies missing: {exc}")
        from tests.chrome_for_tests import find_chrome
        from webarc.server import create_app
        chrome = find_chrome()
        if not chrome:                                      # pragma: no cover
            raise unittest.SkipTest("no Chrome to drive")
        cls._dir = tempfile.TemporaryDirectory()
        root = Path(cls._dir.name)
        app = create_app(str(root / "swm.db"), str(root / "warcs"),
                         replay_root=str(root / "replay"), monitor_resources=False)
        from fastapi.testclient import TestClient
        coll = TestClient(app).post("/api/collections", json={"name": "Library news"}).json()
        job = Path(coll["root_dir"]) / "jobs" / "1"
        cls.job_id = Store(str(root / "swm.db")).create_crawl(
            "lib", {"seeds": [{"url": "https://news.example/"}]}, str(job), 1, collection_id=coll["id"])
        write_log(job, PAGES, {"name": "Libraries", "min_score": 3})
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        cls.port = probe.getsockname()[1]
        probe.close()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="error"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        while not cls.server.started:
            time.sleep(0.05)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=chrome)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.should_exit = True
        cls._dir.cleanup()

    def test_chosen_pages_become_a_crawl_of_only_those_pages_without_the_theme(self):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(page.close)
        page.goto(f"http://127.0.0.1:{self.port}/#/jobs")
        page.wait_for_function("typeof openSelection === 'function'")
        page.evaluate(f"openSelection({self.job_id})")
        page.wait_for_selector("#selection-overlay.open")

        self.assertEqual(page.locator("#selection-body tbody tr").count(), 2)       # not accepted, first
        self.assertIn("score 2 (3 needed)", page.text_content("#selection-body"))
        live = page.locator("#selection-body a").first
        self.assertEqual((live.get_attribute("target"), live.get_attribute("href")),
                         ("_blank", "https://news.example/budget"))
        page.check("#selection-body .sel-pick[data-url='https://news.example/budget']")
        page.click(".sel-tabs button[data-sel=links]")
        page.check("#selection-body .sel-pick")
        self.assertEqual(page.text_content("#selection-recrawl"), "Recrawl 2 selected")
        page.click(".sel-tabs button[data-sel=accepted]")
        self.assertEqual(page.locator("#selection-body .sel-pick").count(), 0)     # already archived
        page.click("#selection-recrawl")
        page.wait_for_function("document.querySelectorAll('.seed-url').length === 2")

        seeds = page.evaluate("[...document.querySelectorAll('.seed-url')].map(i => i.value)")
        self.assertEqual(sorted(seeds), ["https://news.example/budget", "https://news.example/login"])
        self.assertEqual(page.input_value("#f-max-depth"), "0")
        self.assertFalse(page.is_checked("#ct-enabled"))
        self.assertEqual(page.evaluate("document.querySelector('#f-collection').selectedOptions[0].textContent"),
                         "Library news")
        self.assertEqual(page.input_value("#f-crawl-name"), "lib-recrawl")


if __name__ == "__main__":
    unittest.main()
