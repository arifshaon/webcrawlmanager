"""The browser collector, against a site in Instagram's own shape.

Instagram refuses other clients on sight; what it cannot refuse is its own. These tests drive real Chromium against a local site that
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
from webarc.instagram_browser import (InstagramBrowserClient, describe_request,
                                      document_names_listing,
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

    def test_every_record_says_which_document_and_path_it_was_read_from(self):
        docs = [{"unrelated": True},
                {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
                    "edges": [{"node": self.fixture(n)} for n in serve.TIMELINE[:3]]}}}]

        posts, _, _ = extract_instagram_records(
            docs, provenance={"response": "raw/responses/response-000007.json"})

        origin = posts[2].provenance
        self.assertEqual(origin["response"], "raw/responses/response-000007.json")
        self.assertEqual(origin["document"], 1)
        node = docs[1]
        for step in origin["path"].split("."):
            node = node[int(step)] if isinstance(node, list) else node[step]
        self.assertEqual(node["code"], posts[2].shortcode)


class RequestAttributionTests(unittest.TestCase):
    """Which requests are a profile's own listing, read off the request."""

    def test_the_profile_posts_query_names_the_user_it_lists(self):
        asked = describe_request(
            "POST", "https://www.instagram.com/graphql/query",
            "fb_api_req_friendly_name=PolarisProfilePostsQuery&doc_id=1&variables="
            + json.dumps({"data": {"count": 12}, "username": "qnl"}))

        self.assertTrue(asked["listing"])
        self.assertEqual(asked["user"], "qnl")
        self.assertEqual(asked["query"], "PolarisProfilePostsQuery")

    def test_the_feed_query_is_not_a_listing(self):
        asked = describe_request(
            "POST", "https://www.instagram.com/graphql/query",
            "fb_api_req_friendly_name=PolarisFeedRootQuery&variables=%7B%7D")

        self.assertFalse(asked["listing"])
        self.assertIsNone(asked["user"])

    def test_the_older_per_user_endpoint_is_a_listing_for_that_id(self):
        asked = describe_request(
            "GET", "https://www.instagram.com/api/v1/feed/user/100/?count=12", None)

        self.assertTrue(asked["listing"])
        self.assertEqual(asked["user"], "100")

    def test_a_preloaded_block_says_which_query_it_answers(self):
        self.assertTrue(document_names_listing(
            {"require": [["x", ["adp_PolarisProfilePostsTabContentQuery_connectionrelayprovider_0", {}]]]}))
        self.assertFalse(document_names_listing(
            {"require": [["x", ["adp_PolarisFeedRootQueryrelayprovider_0", {}]]]}))


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

    def client(self, warc=None, signed_in=True):
        client = InstagramBrowserClient(
            BrowserConfig(mode="headed", user_data_dir=self._profile.name,
                          chrome_path=self.chrome),
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

    def test_media_is_requested_by_the_page_itself(self):
        """The page's own fetch carries the browser's identity and passes
        through the response hook, so the WARC holds it without a second
        request from the driver."""
        from warcio.archiveiterator import ArchiveIterator
        from webarc.config import WarcConfig
        from webarc.facebook import FacebookWarcSession

        warc = FacebookWarcSession(self.out, "ig", f"http://{self.host}/qnl/", 1,
                                   "webarc", WarcConfig())
        client = self.client(warc=warc)
        config = InstagramCaptureConfig.from_dict({
            "targets": [f"http://{self.host}/p/{serve.TIMELINE[1]['code']}/".replace(
                f"http://{self.host}", "https://www.instagram.com")],
            "mode": "latest_n"})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()
        warc.close()

        index = json.loads((self.out / "instagram-media.json").read_text())
        self.assertEqual(len(index), 3)                      # the carousel
        for entry in index.values():
            self.assertTrue((self.out / "media" / entry["file"]).exists())
        self.assertEqual(client.page_fetches, 3)
        self.assertEqual(client.fallback_fetches, 0)
        with next(self.out.glob("*.warc.gz")).open("rb") as handle:
            media_requests = [
                r for r in ArchiveIterator(handle)
                if r.rec_type == "request"
                and "-l.jpg" in (r.rec_headers.get_header("WARC-Target-URI") or "")]
        self.assertEqual(len(media_requests), 3)
        for record in media_requests:
            self.assertIn("Chrome", record.http_headers.get_header("User-Agent") or "")

    def test_a_single_post_capture_holds_that_post_alone(self):
        """A post page also carries more posts from the account and loads
        their thumbnails; none of that is the post asked for."""
        client = self.client()
        code = serve.TIMELINE[3]["code"]
        config = InstagramCaptureConfig.from_dict({
            "targets": [f"https://www.instagram.com/p/{code}/"], "mode": "latest_n",
            "include_comments": True})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()

        self.assertGreater(len(client.observed.posts), 1)     # the page carried more
        rows = [json.loads(l) for l in (self.out / "instagram-posts.jsonl").read_text().splitlines()]
        self.assertEqual([r["shortcode"] for r in rows], [code])
        index = json.loads((self.out / "instagram-media.json").read_text())
        self.assertEqual(list(index), [f"http://{self.host}/pic/{code}-l.jpg"])
        self.assertEqual([p.name for p in (self.out / "raw" / "posts").iterdir()],
                         [f"{code}.json"])
        comments = [json.loads(l) for l in (self.out / "instagram-comments.jsonl").read_text().splitlines()]
        self.assertTrue(comments)
        self.assertTrue(all(c["post_shortcode"] == code for c in comments))
        manifest = json.loads((self.out / "instagram-manifest.json").read_text())
        self.assertEqual(manifest["counts"]["posts_exported"], 1)
        # every response kept is one a kept record was read from
        kept = {r["provenance"]["response"] for r in rows + comments}
        saved = {f"raw/responses/{p.name}"
                 for p in (self.out / "raw" / "responses").iterdir()}
        self.assertEqual(saved, kept)

    def test_a_second_profile_sees_only_its_own_posts(self):
        """What one page loaded is not the next page's: a listing hands over
        only what its own navigation observed."""
        client = self.client()
        client.profile("qnl")
        first = [p.shortcode for p in client.profile_posts("qnl")]
        self.assertEqual(first, [n["code"] for n in serve.TIMELINE])

        client.profile("qbl")
        second = [p.shortcode for p in client.profile_posts("qbl")]

        self.assertEqual(second, [n["code"] for n in serve.TIMELINE_B])
        self.assertFalse(set(first) & set(second))

    def test_isolation_holds_when_the_earlier_posts_name_no_owner(self):
        """Owner scoping cannot catch an owner-less post; the navigation
        scope must, on its own."""
        client = self.client()
        client.profile("noname")
        first = [p.shortcode for p in client.profile_posts("noname")]
        self.assertEqual(first, [n["code"] for n in serve.TIMELINE_C])
        self.assertTrue(all(p.owner_username is None
                            for p in client.observed.posts.values()))

        client.profile("qbl")
        second = [p.shortcode for p in client.profile_posts("qbl")]

        self.assertEqual(second, [n["code"] for n in serve.TIMELINE_B])

    def test_posts_preloaded_for_the_viewer_are_not_the_profiles(self):
        """The signed-in page also carries the viewer's feed, whose posts
        name their owner by id alone or not at all -- and one of the
        profile's own posts, which only the profile's listing may hand over."""
        client = self.client()
        client.profile("qnl")

        codes = [p.shortcode for p in client.profile_posts("qnl")]

        self.assertEqual(codes, [n["code"] for n in serve.TIMELINE])
        for stray in serve.VIEWER_FEED:
            self.assertIn(stray["code"], client.observed.posts)   # seen, not handed
            self.assertNotIn(stray["code"], codes)

    def test_reels_come_from_the_reels_listing(self):
        client = self.client()
        client.profile("qnl")

        codes = [p.shortcode for p in client.profile_reels("qnl")]

        self.assertEqual(codes, [serve.TIMELINE[2]["code"]])

    def test_an_unrecognised_listing_is_reported_not_silent(self):
        """If Instagram's listing request is not recognised, the run says so
        with what the page asked for, rather than ending with nothing."""
        from unittest import mock
        client = self.client()
        client.profile("qnl")
        with mock.patch("webarc.instagram_browser._LISTING_QUERY_RE",
                        __import__("re").compile("NeverMatches")):
            client._goto(f"http://{self.host}/qbl/")
            codes = [p.shortcode for p in client.profile_posts("qbl")]

        self.assertEqual(codes, [])
        self.assertEqual(client.anomalies[-1]["what"], "no_listing_recognised")
        self.assertEqual(client.anomalies[-1]["profile"], "qbl")
        self.assertTrue(client.anomalies[-1]["requests"])

    def test_a_suggested_post_by_someone_else_is_not_the_profiles(self):
        client = self.client()
        client.profile("qnl")

        codes = [p.shortcode for p in client.profile_posts("qnl")]

        self.assertNotIn(serve.SUGGESTED["code"], codes)
        # it was observed -- the page carried it -- just not handed over
        self.assertIn(serve.SUGGESTED["code"], client.observed.posts)

    def test_a_run_over_two_targets_attributes_each_post_to_its_own_profile(self):
        client = self.client()
        config = InstagramCaptureConfig.from_dict({
            "targets": ["qnl", "qbl"], "mode": "latest_n", "latest_n": 3,
            "surfaces": ["posts"], "capture_media": False})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()

        rows = [json.loads(l) for l in (self.out / "instagram-posts.jsonl").read_text().splitlines()]
        by_owner = {}
        for row in rows:
            by_owner.setdefault(row["owner_username"], []).append(row["shortcode"])
        self.assertEqual(set(by_owner), {"qnl", "qbl"})
        self.assertTrue(all(c.startswith("C") for c in by_owner["qnl"]))
        self.assertEqual(by_owner["qbl"], [n["code"] for n in serve.TIMELINE_B[:3]])

    def test_every_response_a_record_came_from_is_kept_with_provenance(self):
        """With no WARC, the package still holds the bytes each record was
        read from, and the record says where in them it sits."""
        from webarc.facebook import decode_graphql_documents, extract_embedded_documents

        client = self.client()
        config = InstagramCaptureConfig.from_dict({
            "targets": ["qnl"], "mode": "latest_n", "latest_n": 8,
            "surfaces": ["posts"], "capture_media": False})
        session = InstagramCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None)

        session.run()

        responses = sorted((self.out / "raw" / "responses").glob("response-*.json"))
        self.assertGreaterEqual(len(responses), 2)      # the page and the scroll fetch
        rows = [json.loads(l) for l in (self.out / "instagram-posts.jsonl").read_text().splitlines()]
        decoders = set()
        for row in rows:
            origin = row["provenance"]
            saved = json.loads((self.out / origin["response"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["url"], origin["url"])
            body = saved["body"].encode("utf-8")
            documents = (extract_embedded_documents(body) if origin["decoder"] == "embedded"
                         else decode_graphql_documents(body))
            node = documents[origin["document"]]
            for step in origin["path"].split("."):
                node = node[int(step)] if isinstance(node, list) else node[step]
            self.assertEqual(node["code"], row["shortcode"])
            decoders.add(origin["decoder"])
        self.assertEqual(decoders, {"embedded", "graphql"})
        checksums = (self.out / "checksums.sha256").read_text()
        self.assertIn("raw/responses/response-000001.json", checksums)
        manifest = json.loads((self.out / "instagram-manifest.json").read_text())
        self.assertEqual(manifest["layers"]["raw"]["responses_saved"], len(responses))

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

    def test_a_background_run_opens_a_window_when_a_person_is_needed(self):
        """Chrome will not open a profile twice, so the same profile is
        relaunched visibly; what was observed so far is kept."""
        from tests.chrome_for_tests import ensure_display
        close_display = ensure_display()
        if close_display is None:                          # pragma: no cover
            raise unittest.SkipTest("no display for a visible browser")
        self.addCleanup(close_display)
        client = self.client()
        client.profile("qnl")
        before = len(client.observed.posts)
        self.assertTrue(client.headless)

        client.show(f"http://{self.host}/accounts/login/")

        self.assertFalse(client.headless)
        self.assertEqual(len(client.observed.posts), before)
        self.assertTrue(client._page.url.endswith("/accounts/login/"))

    def test_no_warc_is_written_when_not_asked(self):
        client = self.client()
        client.profile("qnl")

        self.assertEqual(list(self.out.glob("*.warc*")), [])
        self.assertEqual(client.exchanges_written, 0)


if __name__ == "__main__":
    unittest.main()
