"""Regressions for the browsable pages built from a Facebook capture.

A Facebook feed cannot be re-driven in a replay browser, so these pages are
how a capture is actually read. They must show every captured post, keep
comments under the right post, be honest about media that was never served,
and never present themselves as the archived Facebook page.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.facebook_render import build_site, is_facebook_capture

POST = {
    "post_id": "101",
    "created_time": "2026-08-27T09:15:00Z",
    "permalink_url": "https://www.facebook.com/ucalgaryqatar/posts/101",
    "author_name": "University of Calgary in Qatar",
    "text": "Nurses Week charity drive.",
    "reactions_count": 11, "comments_count": 2, "shares_count": 0,
    "media_urls": ["https://scontent.example/kept.jpg",
                   "https://scontent.example/never-served.jpg"],
}
OLDER = {
    "post_id": "102", "created_time": "2026-08-14T07:00:00Z",
    "text": "Class of 2026.", "media_urls": [],
}


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, posts=None, comments=(), media=None, manifest=None,
              warc=False):
        rows = [POST, OLDER] if posts is None else posts
        (self.dir / "facebook-posts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in rows), encoding="utf-8")
        (self.dir / "facebook-comments.jsonl").write_text(
            "\n".join(json.dumps(c) for c in comments), encoding="utf-8")
        (self.dir / "facebook-media.json").write_text(
            json.dumps(media if media is not None
                       else {"https://scontent.example/kept.jpg": "aa.jpg"}),
            encoding="utf-8")
        (self.dir / "facebook-manifest.json").write_text(
            json.dumps(manifest or {"capture": {
                "page_url": "https://www.facebook.com/ucalgaryqatar",
                "page_name": "University of Calgary in Qatar",
                "mode": "date_range", "stop_reason": "date_range_boundary_reached",
            }}), encoding="utf-8")
        if warc:
            (self.dir / "capture.warc.gz").write_bytes(b"\x1f\x8b")
        return build_site(self.dir)

    def read(self, site: Path, *parts: str) -> str:
        return site.joinpath(*parts).read_text(encoding="utf-8")


class SiteStructureTests(RenderTestCase):
    def test_a_page_is_written_for_every_post(self):
        site = self.write()
        self.assertTrue((site / "index.html").exists())
        self.assertTrue((site / "posts" / "101.html").exists())
        self.assertTrue((site / "posts" / "102.html").exists())

    def test_capture_directory_is_recognised(self):
        self.write()
        self.assertTrue(is_facebook_capture(self.dir))

    def test_an_unrelated_directory_is_not_recognised(self):
        self.assertFalse(is_facebook_capture(self.dir))

    def test_newest_post_is_listed_first(self):
        index = self.read(self.write(), "index.html")
        self.assertLess(index.index("Nurses Week"), index.index("Class of 2026"))

    def test_an_empty_capture_still_produces_a_page(self):
        index = self.read(self.write(posts=[]), "index.html")
        self.assertIn("No posts were captured", index)


class MediaTests(RenderTestCase):
    def test_media_resolves_from_the_page_that_references_it(self):
        site = self.write()
        # index sits one level below the media folder, a post page two; a
        # single shared prefix silently broke images on post pages.
        self.assertIn('src="../media/aa.jpg"', self.read(site, "index.html"))
        self.assertIn('src="../../media/aa.jpg"',
                      self.read(site, "posts", "101.html"))

    def test_media_never_served_is_named_rather_than_shown_broken(self):
        page = self.read(self.write(), "posts", "101.html")
        self.assertIn("Not captured", page)
        self.assertIn("never-served.jpg", page)
        self.assertNotIn('src="../../media/never-served.jpg"', page)

    def test_media_urls_stored_as_json_text_are_understood(self):
        post = dict(POST, media_urls=json.dumps(POST["media_urls"]))
        page = self.read(self.write(posts=[post]), "posts", "101.html")
        self.assertIn('src="../../media/aa.jpg"', page)


class CommentTests(RenderTestCase):
    COMMENTS = [
        {"comment_id": "c1", "parent_post_id": "101", "author_name": "Fatima",
         "text": "Wonderful", "depth": 0},
        {"comment_id": "c2", "parent_post_id": "101", "author_name": "UCQ",
         "text": "Thank you", "depth": 1, "parent_comment_id": "c1"},
        {"comment_id": "c3", "parent_post_id": "102", "author_name": "Omar",
         "text": "Congratulations", "depth": 0},
    ]

    def test_comments_appear_under_their_own_post(self):
        site = self.write(comments=self.COMMENTS)
        first = self.read(site, "posts", "101.html")
        second = self.read(site, "posts", "102.html")

        self.assertIn("Wonderful", first)
        self.assertNotIn("Congratulations", first)
        self.assertIn("Congratulations", second)

    def test_replies_are_marked_as_replies(self):
        page = self.read(self.write(comments=self.COMMENTS),
                         "posts", "101.html")
        self.assertIn('class="comment reply"', page)

    def test_a_post_with_no_comments_says_so(self):
        page = self.read(self.write(), "posts", "101.html")
        self.assertIn("No comments were captured", page)


class HonestyTests(RenderTestCase):
    def test_pages_say_they_are_not_the_archived_facebook_page(self):
        index = self.read(self.write(), "index.html")
        self.assertIn("not the archived Facebook pages", index)

    def test_a_capture_without_a_warc_says_the_pages_are_all_there_is(self):
        index = self.read(self.write(warc=False), "index.html")
        self.assertIn("No WARC was written", index)

    def test_a_capture_with_a_warc_points_at_replay(self):
        index = self.read(self.write(warc=True), "index.html")
        self.assertIn("replay server", index)

    def test_coverage_from_the_manifest_is_shown(self):
        site = self.write(manifest={
            "capture": {"page_url": "https://www.facebook.com/x",
                        "mode": "date_range"},
            "coverage": {"exported_newest_post": "2026-08-27T09:15:00Z",
                         "requested_range_satisfied": False}})
        index = self.read(site, "index.html")
        self.assertIn("2026-08-27T09:15:00Z", index)
        self.assertIn("Requested range satisfied", index)


class EscapingTests(RenderTestCase):
    def test_post_text_is_escaped(self):
        hostile = dict(POST, text="<script>alert(1)</script>",
                       author_name="<b>Name</b>", media_urls=[])
        page = self.read(self.write(posts=[hostile]), "posts", "101.html")

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<b>Name</b>", page)

    def test_comment_text_is_escaped(self):
        comment = {"comment_id": "c1", "parent_post_id": "101",
                   "author_name": "x", "text": "<img src=x onerror=1>",
                   "depth": 0}
        page = self.read(self.write(comments=[comment]), "posts", "101.html")

        self.assertNotIn("<img src=x onerror=1>", page)
        self.assertIn("&lt;img", page)


if __name__ == "__main__":
    unittest.main()
