"""The gallery-dl listing source, on the browser's session."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.instagram import (InstagramCaptureConfig, InstagramCaptureSession,
                              RateLimited)
from webarc.instagram_gallery import (GalleryListingClient, discovery_limit,
                                      gallery_command, posts_from_gallery,
                                      write_netscape_cookies)

from tests.instagram_fakes import FakeInstagram, post, user_id_of

# gallery-dl's -j output for a profile, in the shape it really has: one
# entry per post, then one per media file, pinned as the pinning user's id
DISCOVERY = [
    [2, {"post_shortcode": "CpXIBzFtUrl", "post_id": "3050942580514966245",
         "pinned": [4267196155], "post_date": "2023-03-04 09:05:19",
         "username": "qatarballers", "owner_id": "4267196155",
         "fullname": "Qatar Ballers", "description": "Welcome", "likes": 321,
         "post_url": "https://www.instagram.com/p/CpXIBzFtUrl/", "type": "post"}],
    [3, {"post_shortcode": "CpXIBzFtUrl", "post_id": "3050942580514966245",
         "pinned": [4267196155], "post_date": "2023-03-04 09:05:19",
         "username": "qatarballers", "owner_id": "4267196155", "num": 2,
         "media_id": "3050942580514966246", "display_url": "https://cdn/pin-2.jpg",
         "width": 1080, "height": 1350}],
    [3, {"post_shortcode": "CpXIBzFtUrl", "post_id": "3050942580514966245",
         "username": "qatarballers", "owner_id": "4267196155", "num": 1,
         "media_id": "3050942580514966245", "display_url": "https://cdn/pin-1.jpg",
         "width": 1080, "height": 1350}],
    [2, {"post_shortcode": "DcwC7FmjYdk", "post_id": "3976691327525816164",
         "pinned": [], "post_date": "2026-09-01 16:02:49", "username": "qatarballers",
         "owner_id": "4267196155", "description": "Banger", "likes": 1606,
         "post_url": "https://www.instagram.com/p/DcwC7FmjYdk/", "type": "reel"}],
    [3, {"post_shortcode": "DcwC7FmjYdk", "post_id": "3976691327525816164",
         "username": "qatarballers", "owner_id": "4267196155", "num": 1,
         "media_id": "3976691264275724139", "display_url": "https://cdn/reel-poster.jpg",
         "video_url": "https://cdn/reel.mp4", "width": 720, "height": 1280}],
]


class MappingTests(unittest.TestCase):
    def test_posts_come_out_in_gallery_dls_order_with_pins_dates_and_media(self):
        posts = posts_from_gallery(json.dumps(DISCOVERY), "qatarballers",
                                   {"response": "raw/responses/response-000001.json"})

        self.assertEqual([p.shortcode for p in posts], ["CpXIBzFtUrl", "DcwC7FmjYdk"])
        pinned, reel = posts
        self.assertTrue(pinned.is_pinned)
        self.assertFalse(reel.is_pinned)
        self.assertEqual(pinned.created_time, "2023-03-04T09:05:19Z")
        self.assertEqual(pinned.owner_id, "4267196155")
        self.assertEqual(pinned.owner_username, "qatarballers")
        self.assertEqual(pinned.caption, "Welcome")
        self.assertEqual(pinned.likes_count, 321)
        self.assertEqual(pinned.kind, "carousel")
        self.assertEqual([m.url for m in pinned.media],
                         ["https://cdn/pin-1.jpg", "https://cdn/pin-2.jpg"])
        self.assertEqual(reel.kind, "reel")
        self.assertEqual(reel.media[0].kind, "video")
        self.assertEqual(reel.media[0].thumbnail_url, "https://cdn/reel-poster.jpg")
        self.assertEqual(pinned.source, "gallery-dl")
        self.assertEqual(pinned.provenance["response"], "raw/responses/response-000001.json")
        self.assertTrue(pinned.provenance["listing_request"])

    def test_the_cookie_file_is_in_the_format_gallery_dl_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.txt"
            written = write_netscape_cookies([
                {"name": "sessionid", "value": "1%3Aabc", "domain": ".instagram.com",
                 "path": "/", "secure": True, "httpOnly": True, "expires": 1800000000.5},
                {"name": "broken"}], path)
            lines = path.read_text().splitlines()

        self.assertEqual(written, 1)
        self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
        self.assertEqual(lines[-1].split("\t"),
                         ["#HttpOnly_.instagram.com", "TRUE", "/", "TRUE",
                          "1800000000", "sessionid", "1%3Aabc"])

    def test_the_command_lends_the_cookies_and_caps_the_listing(self):
        command = gallery_command(Path("/tmp/c.txt"),
                                  "https://www.instagram.com/qnl/posts/", 17)

        self.assertIn("-C", command)
        self.assertIn("/tmp/c.txt", command)
        self.assertIn("-j", command)
        self.assertIn("extractor.instagram.max-posts=17", command)
        self.assertEqual(command[-1], "https://www.instagram.com/qnl/posts/")

    def test_latest_n_lists_a_buffer_beyond_n_and_other_modes_list_everything(self):
        self.assertEqual(discovery_limit("latest_n", 5), 17)
        self.assertEqual(discovery_limit("latest_n", 100), 200)
        self.assertIsNone(discovery_limit("date_range", 5))
        self.assertIsNone(discovery_limit("until_stopped", None))


class _Inner(FakeInstagram):
    """The browser client's part: everything but the listing."""

    def cookie_jar(self):
        return [{"name": "sessionid", "value": "1%3Aabc", "domain": ".instagram.com",
                 "path": "/", "secure": True}]


def _discovery_for(inner: FakeInstagram) -> str:
    """gallery-dl output listing the inner fake's posts, pinned first."""
    entries = []
    owner = user_id_of("qatarballers")
    for p in inner.posts_by_user["qatarballers"]:
        entries.append([2, {"post_shortcode": p.shortcode, "post_id": p.media_id,
                            "pinned": [owner] if p.is_pinned else [],
                            "post_date": p.created_time.replace("T", " ").rstrip("Z"),
                            "username": "qatarballers", "owner_id": owner,
                            "description": p.caption, "likes": p.likes_count,
                            "post_url": p.permalink_url}])
        for n, m in enumerate(p.media, 1):
            entries.append([3, {"post_shortcode": p.shortcode, "post_id": p.media_id,
                                "num": n, "media_id": f"{p.media_id}_{n}",
                                "display_url": m.url, "username": "qatarballers",
                                "owner_id": owner}])
    return json.dumps(entries)


class ListingClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.inner = _Inner()
        self.inner.add_profile("qatarballers", [
            post("Cpin01", "2023-03-04T09:05:19Z", pinned=True, owner="qatarballers"),
            post("Cnew02", "2026-09-02T00:00:00Z", owner="qatarballers"),
            post("Cnew01", "2026-09-01T00:00:00Z", owner="qatarballers"),
        ])
        self.inner.add_comments("Cnew02", [])
        self.runs = 0

    def runner(self, command):
        self.runs += 1
        self.assertTrue(Path(command[command.index("-C") + 1]).exists())  # cookies lent
        return _discovery_for(self.inner)

    def session(self, client, **config):
        cfg = InstagramCaptureConfig.from_dict({
            "targets": ["qatarballers"], "mode": "latest_n", "latest_n": 2,
            "surfaces": ["posts"], "listing": "gallery-dl", **config})
        return InstagramCaptureSession(config=cfg, client=client, output_dir=self.tmp,
                                       crawl_id=1, crawl_name="t", sleep=lambda _s: None)

    def test_the_listing_comes_from_gallery_dl_and_the_rest_from_the_browser(self):
        client = GalleryListingClient(self.inner, limit=14, runner=self.runner)

        self.session(client).run()

        rows = [json.loads(l) for l in (self.tmp / "instagram-posts.jsonl").read_text().splitlines()]
        self.assertEqual([r["shortcode"] for r in rows], ["Cpin01", "Cnew02", "Cnew01"])
        self.assertTrue(rows[0]["is_pinned"])
        self.assertEqual({r["source"] for r in rows}, {"gallery-dl"})
        self.assertEqual(self.runs, 1)
        self.assertIn("extractor.instagram.max-posts=14", client.commands[0])
        self.assertEqual(self.inner.calls["post_list"], 0)        # not scrolled
        self.assertGreaterEqual(self.inner.calls["fetch"], 3)     # media via the browser
        # the listing is kept as the response the posts were read from
        listing = self.tmp / rows[0]["provenance"]["response"]
        self.assertTrue(listing.exists())
        saved = json.loads(listing.read_text(encoding="utf-8"))
        self.assertEqual(saved["tool"], "gallery-dl")
        self.assertIn("Cpin01", saved["body"])
        manifest = json.loads((self.tmp / "instagram-manifest.json").read_text())
        self.assertEqual(manifest["capture"]["listing_source"], "gallery-dl")
        self.assertEqual(manifest["capture"]["client"], "browser+gallery-dl")

    def test_a_rate_limit_from_gallery_dl_is_waited_out_and_retried(self):
        attempts = []
        def flaky(command):
            attempts.append(command)
            if len(attempts) == 1:
                raise RateLimited(120.0, "gallery-dl was rate limited by Instagram")
            return _discovery_for(self.inner)
        client = GalleryListingClient(self.inner, runner=flaky)
        session = self.session(client)

        session.run()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(session.archive.posts), 3)
        self.assertGreaterEqual(session.counters["rate_limit_waits"], 1)

    def test_the_reported_comment_count_is_filled_from_the_posts_own_page(self):
        """gallery-dl does not report comment counts; the browser has the
        post's page open for the comments and can say."""
        seen = post("Cnew02", "2026-09-02T00:00:00Z", owner="qatarballers", comments_count=7)
        self.inner.observed_post = lambda code: seen if code == "Cnew02" else None
        client = GalleryListingClient(self.inner, runner=self.runner)

        self.session(client, include_comments=True).run()

        row = next(json.loads(l) for l in (self.tmp / "instagram-posts.jsonl").read_text().splitlines()
                   if json.loads(l)["shortcode"] == "Cnew02")
        self.assertEqual(row["comments_count"], 7)
        self.assertIn("comments_count", row["provenance"]["filled_from_page"]["fields"])


class ConfigTests(unittest.TestCase):
    def test_the_listing_source_is_a_choice_of_two(self):
        cfg = InstagramCaptureConfig.from_dict({"targets": ["qnl"], "listing": "gallery-dl"})
        self.assertEqual(cfg.listing, "gallery-dl")
        self.assertEqual(InstagramCaptureConfig.from_dict({"targets": ["qnl"]}).listing,
                         "browser")
        with self.assertRaises(ValueError):
            InstagramCaptureConfig.from_dict({"targets": ["qnl"], "listing": "instaloader"})


if __name__ == "__main__":
    unittest.main()
