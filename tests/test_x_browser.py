"""The X browser collector, against a site in X's own shape.

These tests drive real Chromium against a local site that serves what X's
web client serves -- GraphQL operations named in their URLs, typed
timelines with a pinned entry, injected modules, a promoted item, a thread
module, a tombstone and cursors -- and check that collection, attribution,
paging, conversations, searches, media and the optional WARC all come out
of what the browser loaded.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.config import BrowserConfig
from webarc.x import (LoginRequired, TargetUnavailable, XCaptureConfig,
                      XCaptureSession)
from webarc.x_browser import XBrowserClient

from tests.fixtures.x import serve


class BrowserCollectorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:                                # pragma: no cover
            raise unittest.SkipTest("playwright is not installed")
        from tests.chrome_for_tests import find_chrome
        cls.chrome = find_chrome()
        cls.server, cls.host = serve.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self._profile = tempfile.TemporaryDirectory()
        self.addCleanup(self._profile.cleanup)
        self._out = tempfile.TemporaryDirectory()
        self.addCleanup(self._out.cleanup)
        self.out = Path(self._out.name)
        serve.Handler.rate_limit_once = False

    def client(self, warc=None, signed_in=True) -> XBrowserClient:
        client = XBrowserClient(
            BrowserConfig(mode="headed", user_data_dir=self._profile.name,
                          chrome_path=self.chrome),
            warc=warc, sleep=lambda s: None, stall_rounds=2,
            base_url=f"http://{self.host}", headless=True, settle=(0.25, 0.4))
        client.start()
        self.addCleanup(client.close)
        if signed_in:
            client._context.add_cookies([
                {"name": "auth_token", "value": "abc", "domain": "127.0.0.1", "path": "/"},
                {"name": "ct0", "value": "csrf", "domain": "127.0.0.1", "path": "/"},
                {"name": "twid", "value": "u%3D424242", "domain": "127.0.0.1", "path": "/"}])
            client.refresh()
        return client

    def session(self, client, **raw) -> XCaptureSession:
        config = XCaptureConfig.from_dict({"targets": ["qnl"], "mode": "latest_n",
                                           "latest_n": 100, **raw})
        return XCaptureSession(config=config, client=client, output_dir=self.out,
                               crawl_id=1, crawl_name="t", sleep=lambda s: None)

    def rows(self):
        return [json.loads(line) for line in
                (self.out / "x-posts.jsonl").read_text(encoding="utf-8").splitlines()]


class BrowserCollectorTests(BrowserCollectorTestCase):
    def test_the_viewer_comes_from_the_browsers_cookies(self):
        client = self.client()
        self.assertEqual(client.viewer(), "424242")
        client._context.clear_cookies()
        self.assertIsNone(client.viewer())

    def test_the_account_is_read_from_the_pages_own_lookup(self):
        user = self.client().user("qnl")

        self.assertEqual((user.user_id, user.handle, user.posts_count), ("100", "qnl", 12))
        self.assertEqual(user.pinned_post_ids, [serve.PINNED["rest_id"]])

    def test_posts_arrive_from_the_listing_operation_and_then_from_scrolling(self):
        client = self.client()
        user = client.user("qnl")

        ids = [p.post_id for p in client.timeline(user, "posts")]

        self.assertEqual(ids, [serve.PINNED["rest_id"]] + [t["rest_id"] for t in serve.TIMELINE])
        self.assertGreaterEqual(client.observed.operations["UserTweets"], 2)   # the scroll
        # what the signed-in client fetched beside the target is counted, never listed
        self.assertEqual(client.observed.operations["HomeTimeline"], 1)
        self.assertNotIn(serve.READER_POST["rest_id"], ids)
        self.assertNotIn(serve.PROMOTED["rest_id"], ids)
        self.assertEqual(client.observed.promoted_skipped, 1)

    def test_the_replies_tab_yields_the_accounts_reply_with_its_neighbour_as_context(self):
        client = self.client()
        session = self.session(client, surfaces=["replies"])

        session.run()

        rows = self.rows()
        by_id = {r["post_id"]: r for r in rows}
        mine = by_id[serve.REPLY_BY_ACCOUNT["rest_id"]]
        self.assertEqual((mine["capture_role"], mine["relationship"]), ("target", "reply"))
        context = by_id[serve.READER_POST["rest_id"]]
        self.assertEqual(context["capture_role"], "conversation_context")
        self.assertEqual(context["provenance"]["context_for"], serve.REPLY_BY_ACCOUNT["rest_id"])
        self.assertEqual(session.manifest_document()["counts"]["posts_exported"], 2)

    def test_the_conversation_of_a_post_is_read_with_show_more_and_the_tombstone(self):
        client = self.client()
        session = self.session(client, surfaces=["media"], include_conversation=True,
                               max_replies_per_post=10)

        session.run()

        by_id = {r["post_id"]: r for r in self.rows()}
        photo = by_id[serve.PHOTO["rest_id"]]
        replies = [r for r in self.rows() if r["capture_role"] == "conversation_context"
                   and (r.get("provenance") or {}).get("context_for") == serve.PHOTO["rest_id"]]
        self.assertEqual(sorted(r["post_id"][-4:] for r in replies), ["1080", "1081", "1082"])
        self.assertEqual(photo["reply_capture"]["status"], "reported_count_reached")
        events = [json.loads(l) for l in (self.out / "x-events.jsonl").read_text().splitlines()]
        gone = [e for e in events if e["event"] == "post_unavailable"]
        self.assertEqual(gone[0]["reason"], "This Post was deleted by the Post author.")

    def test_a_search_lists_every_author_from_the_query_operation(self):
        client = self.client()
        config = XCaptureConfig.from_dict({"targets": ["#books"], "mode": "latest_n"})
        XCaptureSession(config=config, client=client, output_dir=self.out, crawl_id=1,
                        crawl_name="t", sleep=lambda s: None).run()

        rows = self.rows()
        self.assertEqual({r["author_handle"] for r in rows}, {"someone_else", "qnl"})
        self.assertEqual(rows[0]["provenance"]["raw_query"], "#books")
        self.assertGreaterEqual(client.observed.operations["SearchTimeline"], 2)

    def test_media_is_fetched_by_the_page_at_orig_with_a_fallback_when_refused(self):
        client = self.client()
        self.session(client, surfaces=["media"]).run()

        index = json.loads((self.out / "x-media.json").read_text())
        one = next(v for k, v in index.items() if "/media/one?" in k)
        self.assertEqual(one["requested_variant"], "orig")
        self.assertEqual(one["fetched_via"], "browser-page")
        two = next(v for k, v in index.items() if "/media/two?" in k)
        self.assertIn("name=4096x4096", two["fetched_url"])
        clip = next(v for k, v in index.items() if k.endswith("1280x720.mp4"))
        self.assertEqual(clip["requested_variant"], "mp4:2176000")
        self.assertEqual(client.fallback_fetches, 0)

    def test_a_warc_holds_the_exchanges_with_the_session_redacted(self):
        from warcio.archiveiterator import ArchiveIterator
        from webarc.config import WarcConfig
        from webarc.facebook import FacebookWarcSession

        warc = FacebookWarcSession(self.out, "x", f"http://{self.host}/qnl", 1, "webarc",
                                   WarcConfig())
        client = self.client(warc=warc)
        self.session(client, latest_n=3).run()
        warc.close()

        requests = []
        urls = []
        for path in self.out.glob("*.warc.gz"):
            with path.open("rb") as handle:
                for record in ArchiveIterator(handle):
                    if record.rec_type == "request":
                        requests.append(record.http_headers.headers)
                    if record.rec_type == "response":
                        urls.append(record.rec_headers.get_header("WARC-Target-URI"))
        self.assertTrue(any("/i/api/graphql/" in u and "UserTweets" in u for u in urls))
        self.assertTrue(any("/media/" in u for u in urls))
        graphql_requests = [h for h in requests if any(
            k.lower() == "x-csrf-token" for k, _ in h)]
        self.assertEqual(graphql_requests, [])
        self.assertFalse(any(k.lower() in ("cookie", "authorization") for h in requests for k, _ in h))

    def test_a_429_is_waited_out_using_the_reset_header(self):
        serve.Handler.rate_limit_once = True
        client = self.client()
        session = self.session(client, latest_n=100)

        session.run()

        self.assertEqual(session.counters["rate_limit_waits"], 1)
        self.assertEqual(len([r for r in self.rows() if r["capture_role"] == "target"]),
                         len(serve.TIMELINE) + 1)

    def test_a_signed_out_browser_sees_the_sign_in_page(self):
        client = self.client(signed_in=False)

        with self.assertRaises(LoginRequired):
            client._goto(f"http://{self.host}/")

    def test_an_account_that_does_not_exist_is_reported(self):
        client = self.client()

        with self.assertRaises(TargetUnavailable):
            client.user("nobody")

    def test_a_full_run_leaves_a_package_that_prunes_unreferenced_responses(self):
        client = self.client()
        session = self.session(client, latest_n=4)

        session.run()

        manifest = json.loads((self.out / "x-manifest.json").read_text())
        self.assertEqual(manifest["capture"]["state"], "stopped")
        self.assertEqual(manifest["counts"]["posts_exported"], 5)
        self.assertEqual(manifest["counts"]["reposts"], 1)
        self.assertEqual(manifest["counts"]["quotes"], 1)
        self.assertIn("HomeTimeline", manifest["operations_observed"])
        kept = sorted(p.name for p in (self.out / "raw" / "responses").glob("*.json"))
        self.assertTrue(kept)
        for name in kept:
            body = json.loads((self.out / "raw" / "responses" / name).read_text())
            self.assertNotIn("HomeTimeline", body["url"])
        checksums = (self.out / "checksums.sha256").read_text()
        self.assertIn("x-posts.jsonl", checksums)
        self.assertIn("media/", checksums)


if __name__ == "__main__":
    unittest.main()
