"""The Instagram reader: pages built from a package's own records."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.instagram import (InstagramCaptureConfig, InstagramCaptureSession,
                              MediaItem)
from webarc.instagram_render import build_site, is_instagram_capture

from tests.instagram_fakes import FakeInstagram, comment, post


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = FakeInstagram()

    def package(self, posts, comments=None, **config):
        self.fake.add_profile("qnl", posts)
        for code, thread in (comments or {}).items():
            self.fake.add_comments(code, thread)
        cfg = InstagramCaptureConfig.from_dict({
            "targets": ["qnl"], "mode": "latest_n", **config})
        InstagramCaptureSession(config=cfg, client=self.fake,
                                output_dir=self.tmp, crawl_id=1,
                                crawl_name="t", sleep=lambda _s: None).run()
        return build_site(self.tmp)

    def read(self, *parts):
        return (self.tmp / "pages").joinpath(*parts).read_text(encoding="utf-8")


class ReaderTests(RenderTestCase):
    def test_a_package_is_recognised(self):
        self.package([post("Cabc01", "2026-03-01T00:00:00Z")])

        self.assertTrue(is_instagram_capture(self.tmp))
        self.assertFalse(is_instagram_capture(self.tmp / "media"))

    def test_the_index_is_a_grid_of_every_captured_post(self):
        self.package([post(f"Cpost{n}", f"2026-03-{n + 1:02d}T00:00:00Z")
                      for n in range(4)])

        index = self.read("index.html")

        self.assertEqual(index.count('class="tile"'), 4)
        for n in range(4):
            self.assertIn(f'href="posts/Cpost{n}.html"', index)

    def test_carousel_components_appear_in_order(self):
        items = [MediaItem(url=f"https://cdn.example/c{n}.jpg", kind="image",
                           position=n) for n in range(3)]
        for n, item in enumerate(items):
            self.fake.media[item.url] = f"distinct image {n}".encode()
        self.package([post("Ccar01", "2026-03-01T00:00:00Z", kind="carousel",
                           media=items)])
        media = json.loads((self.tmp / "instagram-media.json").read_text())

        page = self.read("posts", "Ccar01.html")

        self.assertIn("1 / 3", page)
        self.assertIn("3 / 3", page)
        for item in items:
            self.assertIn(media[item.url]["file"], page)
        self.assertLess(page.index(media[items[0].url]["file"]),
                        page.index(media[items[2].url]["file"]))

    def test_a_video_is_a_video_element(self):
        self.fake.media["https://cdn.example/v.mp4"] = b"\x00\x00\x00\x18ftypmp42"
        self.package([post("Cvid01", "2026-03-01T00:00:00Z", kind="reel", media=[
            MediaItem(url="https://cdn.example/v.mp4", kind="video")])])

        self.assertIn("<video", self.read("posts", "Cvid01.html"))

    def test_comments_and_replies_are_shown_under_the_post(self):
        self.package([post("Cabc01", "2026-03-01T00:00:00Z", comments_count=2)],
                     comments={"Cabc01": [
                         comment("c1", "Cabc01", "Lovely"),
                         comment("c1r", "Cabc01", "Thank you", parent="c1")]},
                     include_comments=True, include_replies=True)

        page = self.read("posts", "Cabc01.html")

        self.assertIn("Lovely", page)
        self.assertIn("Thank you", page)
        self.assertIn('class="comment reply"', page)

    def test_media_that_was_not_captured_is_said_so_not_broken(self):
        self.fake.fail_media.add("https://cdn.example/Cabc01.jpg")
        self.package([post("Cabc01", "2026-03-01T00:00:00Z")])

        self.assertIn("Not captured", self.read("posts", "Cabc01.html"))

    def test_every_page_says_what_it_is(self):
        self.package([post("Cabc01", "2026-03-01T00:00:00Z")])

        for page in ("index.html", "posts/Cabc01.html"):
            with self.subTest(page=page):
                self.assertIn("not Instagram", self.read(*page.split("/")))
                self.assertIn("No rendered WARC", self.read(*page.split("/")))

    def test_the_notice_changes_when_a_warc_is_present(self):
        self.package([post("Cabc01", "2026-03-01T00:00:00Z")])
        (self.tmp / "rendered.warc.gz").write_bytes(b"x")

        build_site(self.tmp)

        self.assertIn("rendered WARC sits alongside", self.read("index.html"))

    def test_the_profile_is_described(self):
        self.package([post("Cabc01", "2026-03-01T00:00:00Z")])

        self.assertIn("@qnl", self.read("index.html"))


if __name__ == "__main__":
    unittest.main()
