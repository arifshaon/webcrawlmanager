"""The browser collector, against a site in Instagram's own shape.

Instagram refuses Instaloader's requests on sight; what it cannot refuse is
its own client. These tests drive real Chromium against a local site that
serves what Instagram's web client serves -- posts embedded in the profile
page, more over GraphQL on scroll, a post page with its comments -- and
check that collection, ordering, media, comments and the optional WARC all
come out of what the browser loaded.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.config import BrowserConfig
from webarc.instagram import (InstagramCaptureConfig, InstagramCaptureSession,
                              LoginRequired, TargetUnavailable)
from webarc.instagram_browser import (InstagramBrowserClient,
                                      extract_instagram_records)

from tests.fixtures.instagram import serve


class ExtractionTests(unittest.TestCase):
    """Reading Instagram's payload shapes without a browser."""

    def fixture(self, obj):
        return json.loads(json.dumps(obj).replace("HOST", "h"))

    def test_a_profile_connection_yields_posts_in_instagrams_order(self):
        docs = [{"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": [{"node": self.fixture(n)} for n in serve.TIMELINE[:4]]}}}]

        posts, _, _ = extract_instagram_records(docs)

        self.assertEqual([p.shortcode for p in posts],
                         [n["code"] for n in serve.TIMELINE[:4]])
        self.assertEqual(posts[0].created_time, "2020-09-13T12:26:40Z")

    def test_the_largest_image_candidate_is_chosen(self):
        posts, _, _ = extract_instagram_records([self.fixture(serve.TIMELINE[3])])

        self.assertEqual(len(posts[0].media), 1)
        self.assertTrue(posts[0].media[0].url.endswith("-l.jpg"))
        self.assertEqual(posts[0].media[0].width, 1080)

    def test_a_carousel_keeps_every_component_in_order(self):
        posts, _, _ = extract_instagram_records([self.fixture(serve.TIMELINE[1])])

        self.assertEqual(posts[0].kind, "carousel")
        self.assertEqual([m.position for m in posts[0].media], [0, 1, 2])
        self.assertTrue(all(m.url.endswith("-l.jpg") for m in posts[0].media))

    def test_a_reel_is_a_video_with_a_poster(self):
        posts, _, _ = extract_instagram_records([self.fixture(serve.TIMELINE[2])])

        self.assertEqual(posts[0].kind, "reel")
        self.assertEqual(posts[0].media[0].kind, "video")
        self.assertTrue(posts[0].media[0].url.endswith(".mp4"))
        self.assertTrue(posts[0].media[0].thumbnail_url.endswith("-poster.jpg"))
        self.assertEqual(posts[0].video_view_count, 999)

    def test_comments_and_their_replies_are_read_with_their_parents(self):
        docs = [{"data": {"xdt_api__v1__media__media_id__comments__connection": {
            "edges": [{"node": c} for c in serve.COMMENTS]}}}]

        _, comments, _ = extract_instagram_records(docs, shortcode_hint="Cx")

        by_id = {c.comment_id: c for c in comments}
        self.assertEqual(by_id["9001"].depth, 0)
        self.assertEqual(by_id["9001_1"].depth, 1)
        self.assertEqual(by_id["9001_1"].parent_comment_id, "9001")
        self.assertEqual(by_id["9001"].post_shortcode, "Cx")
        self.assertEqual(by_id["9001"].author_username, "reader")

    def test_the_profile_is_read(self):
        _, _, profiles = extract_instagram_records([{"data": {"user": self.fixture(serve.PROFILE)}}])

        self.assertEqual(profiles[0].username, "qnl")
        self.assertEqual(profiles[0].followers_count, 1200)
        self.assertEqual(profiles[0].posts_count, 12)

    def test_a_comment_is_not_mistaken_for_a_post_or_a_profile(self):
        posts, comments, profiles = extract_instagram_records([serve.COMMENTS[1]])

        self.assertEqual(posts, [])
        self.assertEqual(profiles, [])
        self.assertEqual(len(comments), 1)


class BrowserCollectorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:                                # pragma: no cover
            raise unittest.SkipTest("playwright is not installed")
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

    def client(self, warc=None, signed_in=True):
        client = InstagramBrowserClient(
            BrowserConfig(mode="headed", user_data_dir=self._profile.name),
            warc=warc, sleep=lambda s: None, stall_rounds=2,
            base_url=f"http://{self.host}", headless=True, settle=(0.25, 0.4))
        client.start()
        self.addCleanup(client.close)
        if signed_in:
            client._context.add_cookies([
                {"name": "sessionid", "value": "1%3Aabc", "domain": "127.0.0.1",
                 "path": "/"},
                {"name": "ds_user_id", "value": "424242", "domain": "127.0.0.1",
                 "path": "/"}])
            client.refresh()
        return client


class BrowserCollectorTests(BrowserCollectorTestCase):
    def test_the_viewer_comes_from_the_browsers_cookies(self):
        self.assertEqual(self.client().viewer(), "424242")

    def test_no_session_cookie_means_no_viewer(self):
        self.assertIsNone(self.client(signed_in=False).viewer())

    def test_a_profile_is_read_from_the_page_the_browser_loaded(self):
        profile = self.client().profile("qnl")

        self.assertEqual(profile.username, "qnl")
        self.assertEqual(profile.posts_count, 12)

    def test_posts_arrive_from_the_page_and_then_from_scrolling(self):
        client = self.client()
        client.profile("qnl")

        codes = [p.shortcode for p in client.profile_posts("qnl")]

        self.assertEqual(codes, [n["code"] for n in serve.TIMELINE])
        self.assertGreaterEqual(client.observed.api_responses, 1)   # the scroll fetch

    def test_the_engine_recognises_the_pinned_post_by_order(self):
        client = self.client()
        config = InstagramCaptureConfig.from_dict({
            "targets": ["qnl"], "mode": "latest_n", "latest_n": 3,
            "surfaces": ["posts"]})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()

        rows = [json.loads(l) for l in (self.out / "instagram-posts.jsonl").read_text().splitlines()]
        self.assertEqual([r["shortcode"] for r in rows],
                         [serve.PINNED["code"]] + [n["code"] for n in serve.TIMELINE[1:4]])
        self.assertTrue(rows[0]["is_pinned"])

    def test_media_is_fetched_through_the_browser_context(self):
        client = self.client()
        config = InstagramCaptureConfig.from_dict({
            "targets": [f"http://{self.host}/p/{serve.TIMELINE[1]['code']}/".replace(
                f"http://{self.host}", "https://www.instagram.com")],
            "mode": "latest_n"})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()

        index = json.loads((self.out / "instagram-media.json").read_text())
        self.assertEqual(len(index), 3)                      # the carousel
        for entry in index.values():
            self.assertTrue((self.out / "media" / entry["file"]).exists())

    def test_comments_come_from_the_post_page_and_from_scrolling_it(self):
        client = self.client()
        code = serve.TIMELINE[1]["code"]

        comments = list(client.comments(code, include_replies=True))

        ids = [c.comment_id for c in comments]
        self.assertIn("9001", ids)
        self.assertIn("9001_1", ids)                          # the reply
        self.assertIn("9003", ids)                            # loaded on scroll
        self.assertTrue(all(c.post_shortcode == code for c in comments))

    def test_replies_are_withheld_when_not_asked_for(self):
        client = self.client()
        code = serve.TIMELINE[1]["code"]

        ids = [c.comment_id for c in client.comments(code, include_replies=False)]

        self.assertIn("9001", ids)
        self.assertNotIn("9001_1", ids)

    def test_a_missing_post_is_reported_not_invented(self):
        with self.assertRaises(TargetUnavailable):
            self.client().post("Cnope0000")

    def test_instagrams_sign_in_page_is_the_engines_login_condition(self):
        client = self.client(signed_in=False)

        with self.assertRaises(LoginRequired):
            client._goto(f"http://{self.host}/accounts/login/")

    def test_every_exchange_is_written_to_the_warc_when_asked(self):
        from warcio.archiveiterator import ArchiveIterator
        from webarc.config import WarcConfig
        from webarc.facebook import FacebookWarcSession

        warc = FacebookWarcSession(self.out, "ig", f"http://{self.host}/qnl/", 1,
                                   "webarc", WarcConfig())
        client = self.client(warc=warc)
        client.profile("qnl")
        list(client.profile_posts("qnl"))
        warc.close()

        files = list(self.out.glob("*.warc.gz"))
        self.assertEqual(len(files), 1)
        with files[0].open("rb") as handle:
            records = list(ArchiveIterator(handle))
        uris = [r.rec_headers.get_header("WARC-Target-URI") for r in records]
        self.assertIn(f"http://{self.host}/qnl/", uris)
        self.assertTrue(any(u and u.endswith("/graphql/query") for u in uris))
        # the session cookie never reaches the WARC
        for record in records:
            if record.rec_type == "request":
                self.assertIsNone(record.http_headers.get_header("Cookie"))

    def test_no_warc_is_written_when_not_asked(self):
        client = self.client()
        client.profile("qnl")

        self.assertEqual(list(self.out.glob("*.warc*")), [])
        self.assertEqual(client.exchanges_written, 0)


if __name__ == "__main__":
    unittest.main()
