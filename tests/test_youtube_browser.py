"""The YouTube Posts collector, against a site in YouTube's own shape.

These tests drive real Chromium against a local site that serves what
YouTube's web client serves -- ``ytInitialData`` embedded in the channel
and post pages, ``youtubei/v1/browse`` continuations when the Posts tab
is scrolled, ``youtubei/v1/next`` answers in the entity shape for comments
with a "View replies" control -- and check that the channel, the posts,
their images, the comments and the optional WARC all come out of what the
browser loaded.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.config import BrowserConfig
from webarc.youtube import (TargetUnavailable, YouTubeCaptureConfig, YouTubeCaptureSession,
                            parse_youtube_target)
from webarc.youtube_browser import YouTubeBrowserClient

from tests.fixtures.youtube import serve


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

    def client(self, warc=None) -> YouTubeBrowserClient:
        client = YouTubeBrowserClient(
            BrowserConfig(mode="headed", user_data_dir=self._profile.name,
                          chrome_path=self.chrome),
            warc=warc, sleep=lambda s: None, stall_rounds=2,
            base_url=f"http://{self.host}", headless=True, settle=(0.25, 0.4))
        client.start()
        self.addCleanup(client.close)
        return client

    def session(self, client, **raw) -> YouTubeCaptureSession:
        config = YouTubeCaptureConfig.from_dict({"targets": ["qnl"], "mode": "latest_n",
                                                 "latest_n": 100, "surfaces": ["posts"], **raw})
        return YouTubeCaptureSession(config=config, client=client, output_dir=self.out,
                                     crawl_id=1, crawl_name="t", sleep=lambda s: None)

    def rows(self, name: str):
        return [json.loads(line) for line in
                (self.out / name).read_text(encoding="utf-8").splitlines() if line]


class BrowserCollectorTests(BrowserCollectorTestCase):
    def test_the_browser_does_not_announce_itself_as_automated(self):
        client = self.client()
        self.assertIs(client._page.evaluate("navigator.webdriver"), False)

    def test_the_channel_is_read_from_the_pages_own_metadata(self):
        channel = self.client().channel(parse_youtube_target("@qnl"))

        self.assertEqual(channel.channel_id, serve.CHANNEL_ID)
        self.assertEqual((channel.handle, channel.name), ("qnl", "Qatar National Library"))
        self.assertEqual((channel.subscriber_count, channel.video_count), (1200, 45))
        self.assertTrue(channel.avatar_url.endswith("/avatar=s900"))
        self.assertTrue(channel.banner_url.endswith("/banner"))
        self.assertEqual(channel.source, "browser")

    def test_a_channel_youtube_does_not_have_is_unavailable(self):
        with self.assertRaises(TargetUnavailable) as caught:
            self.client().channel(parse_youtube_target("@nobody"))
        self.assertEqual(caught.exception.availability, "unavailable")

    def test_posts_arrive_from_the_page_and_then_from_scrolling_and_a_foreign_post_is_refused(self):
        client = self.client()
        channel = client.channel(parse_youtube_target("@qnl"))

        posts = list(client.posts(channel))

        self.assertEqual([p.post_id for p in posts],
                         [p["postId"] for p in serve.POSTS + serve.MORE_POSTS])
        self.assertNotIn(serve.FOREIGN["postId"], [p.post_id for p in posts])
        self.assertEqual([p.kind for p in posts[:5]], ["image", "poll", "video", "images", "text"])
        self.assertEqual(client.observed.endpoints["browse"], 1)         # the one continuation
        self.assertTrue(all(p.provenance["url"] for p in posts))
        self.assertEqual(posts[0].provenance["decoder"], "embedded")
        self.assertEqual(posts[5].provenance["decoder"], "youtubei")
        self.assertEqual(posts[5].provenance["continuation"], "posts-page-2")
        self.assertEqual(client.anomalies, [])

    def test_comments_are_read_with_their_replies_behind_the_view_replies_control(self):
        client = self.client()
        channel = client.channel(parse_youtube_target("@qnl"))
        first = next(iter(client.posts(channel)))

        comments = list(client.post_comments(first))

        self.assertEqual([c.comment_id for c in comments],
                         ["UgxrootA", "UgxrootB", "UgxrootA.reply1", "UgxrootA.reply2"])
        by_id = {c.comment_id: c for c in comments}
        self.assertEqual(by_id["UgxrootA"].text, "Wonderful post")
        self.assertTrue(by_id["UgxrootB"].author_is_uploader)
        reply = by_id["UgxrootA.reply2"]
        self.assertEqual((reply.parent_id, reply.thread_root_id, reply.reply_depth),
                         ("UgxrootA", "UgxrootA", 1))
        self.assertTrue(reply.author_is_uploader)
        self.assertEqual(reply.target_type, "post")
        self.assertEqual(reply.target_id, first.post_id)
        self.assertIs(client.comments_more(), False)
        self.assertEqual(client.observed.endpoints["next"], 2)

    def test_the_engine_writes_the_posts_their_images_and_the_responses_they_came_from(self):
        client = self.client()
        session = self.session(client, latest_n=3)

        result = session.run()

        self.assertEqual(result["posts"], 3)
        rows = self.rows("youtube-posts.jsonl")
        self.assertEqual([r["post_id"] for r in rows], [p["postId"] for p in serve.POSTS[:3]])
        image_post = rows[0]
        self.assertEqual(image_post["images"][0]["file"], f"media/posts/{image_post['post_id']}/image-1.png")
        self.assertTrue((self.out / image_post["images"][0]["file"]).exists())
        self.assertEqual(image_post["comment_capture"]["status"], "reported_count_reached")
        self.assertEqual(image_post["comment_capture"]["observed"], 4)
        self.assertEqual(image_post["comment_capture"]["reported"], 3)
        self.assertEqual(rows[1]["poll"]["options"][0]["text"], "Books")
        self.assertFalse(rows[1]["poll"]["results_available"])
        self.assertEqual(rows[2]["attached_video_id"], "wGA27zJEnaU")
        response = rows[0]["provenance"]["response"]
        self.assertTrue(response.startswith("raw/responses/"))
        self.assertTrue((self.out / response).exists())
        meta = json.loads((self.out / (response[:-len(".json")] + ".meta.json")).read_text()) \
            if (self.out / (response[:-len(".json")] + ".meta.json")).exists() else {}
        self.assertTrue(meta.get("evidence_type", "observed HTTP response"))
        index = json.loads((self.out / "youtube-media.json").read_text())
        image_file = index[image_post["images"][0]["file"]]
        self.assertEqual(image_file["fetched_via"], "browser-page")
        self.assertEqual(image_file["role"], "post_image")
        self.assertEqual(client.fallback_fetches, 0)
        manifest = json.loads((self.out / "youtube-manifest.json").read_text())
        self.assertEqual(manifest["counts"]["posts_exported"], 3)
        self.assertEqual(manifest["counts"]["comments_exported"], 4)   # the fixture serves the same comment ids on every post; one id is one record
        self.assertEqual(manifest["capture"]["clients"]["client"], "browser")
        self.assertGreater(manifest["layers"]["raw"]["responses_saved"], 0)
        channels = json.loads((self.out / "youtube-channels.json").read_text())
        self.assertEqual(channels[serve.CHANNEL_ID]["name"], "Qatar National Library")

    def test_a_warc_holds_the_exchanges_with_the_session_redacted(self):
        from warcio.archiveiterator import ArchiveIterator
        from webarc.config import WarcConfig
        from webarc.facebook import FacebookWarcSession

        warc = FacebookWarcSession(self.out, "youtube", f"http://{self.host}/@qnl", 1, "webarc",
                                   WarcConfig())
        client = self.client(warc=warc)
        client._context.add_cookies([
            {"name": "SAPISID", "value": "secret-sapisid", "domain": "127.0.0.1", "path": "/"}])
        self.session(client, latest_n=6, include_comments=False).run()   # past the first page
        from webarc.youtube import YouTubeVideo
        client.visit_video(YouTubeVideo(video_id="wGA27zJEnaU"))
        warc.close()

        requests = []
        urls = []
        bodies = b""
        for path in self.out.glob("*.warc.gz"):
            with path.open("rb") as handle:
                for record in ArchiveIterator(handle):
                    if record.rec_type == "request":
                        requests.append(record.http_headers.headers)
                    if record.rec_type == "response":
                        urls.append(record.rec_headers.get_header("WARC-Target-URI"))
                        bodies += record.content_stream().read()
        self.assertTrue(any(u.endswith("/posts") for u in urls))
        self.assertTrue(any("/watch?v=" in u for u in urls))         # the attached video's page
        self.assertTrue(any("/thumb/wGA27zJEnaU" in u for u in urls))
        self.assertFalse(any("/videoplayback" in u for u in urls))     # never the stream
        self.assertTrue(any("/youtubei/v1/browse" in u for u in urls))
        self.assertTrue(any("/img/one=" in u for u in urls))
        self.assertFalse(any(k.lower() in ("cookie", "authorization") for h in requests for k, _ in h))
        self.assertNotIn(b"secret-sapisid", bodies)
        self.assertEqual(json.loads((self.out / "youtube-manifest.json").read_text())
                         ["replay"]["expected"], "partial")


if __name__ == "__main__":
    unittest.main()
