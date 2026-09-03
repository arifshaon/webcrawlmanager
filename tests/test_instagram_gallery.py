"""The gallery-dl listing source: streamed, resumable, kept as evidence."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from webarc.instagram import (InstagramCaptureConfig, InstagramCaptureSession,
                              LoginRequired, RateLimited, TargetUnavailable)
from webarc.instagram_gallery import (SCRATCH_PREFIX, GalleryListingClient,
                                      clear_stale_scratch, discovery_limit,
                                      gallery_command, lend_cookies,
                                      posts_from_gallery_lines,
                                      write_netscape_cookies)

from tests.instagram_fakes import FakeInstagram, post, user_id_of

FIXTURE = Path(__file__).parent / "fixtures" / "instagram" / "gallery-dl-posts.jsonl"
FAKE_MODULE = "tests.fixtures.instagram.fake_gallery_dl"
FIXTURE_CODES = ["CpXIBzFtUrl", "Dbv_BWsNcdK", "DbqZzEmCN0k"]   # pinned, pinned, post


class RecordedOutputTests(unittest.TestCase):
    """gallery-dl 1.32.10's own output for a real profile, unmodified."""

    def test_posts_come_out_in_gallery_dls_order_with_pins_dates_and_media(self):
        posts = posts_from_gallery_lines(FIXTURE.read_text(encoding="utf-8").splitlines(),
                                         "qatarballers", {"listing_evidence": "e"})

        self.assertEqual([p.shortcode for p in posts], FIXTURE_CODES)
        first = posts[0]
        self.assertTrue(first.is_pinned)
        self.assertEqual(first.created_time, "2023-03-04T09:05:19Z")
        self.assertEqual(first.owner_id, "4267196155")
        self.assertEqual(first.owner_username, "qatarballers")
        self.assertEqual(first.likes_count, 321)
        self.assertTrue(first.caption)
        self.assertEqual(first.source, "gallery-dl")
        self.assertEqual(first.provenance["listing_evidence"], "e")
        self.assertEqual(first.provenance["line"], 1)
        self.assertEqual([m.position for m in first.media], list(range(len(first.media))))
        kinds = {p.shortcode: p.kind for p in posts}
        self.assertIn(kinds["DbqZzEmCN0k"], ("carousel",))
        self.assertTrue(all(p.media for p in posts))

    def test_a_reels_video_carries_its_poster(self):
        posts = posts_from_gallery_lines(FIXTURE.read_text(encoding="utf-8").splitlines(),
                                         "qatarballers")
        videos = [m for p in posts for m in p.media if m.kind == "video"]

        self.assertTrue(videos)
        self.assertTrue(all(m.thumbnail_url for m in videos))


class CommandTests(unittest.TestCase):
    def test_the_command_ignores_the_users_config_and_states_every_setting(self):
        command = gallery_command(Path("/tmp/c.txt"), "https://www.instagram.com/qnl/posts/",
                                 17, cursor="abc123")

        self.assertIn("--config-ignore", command)
        self.assertIn("--no-input", command)
        self.assertIn("-v", command)
        self.assertEqual(command[command.index("-C") + 1], "/tmp/c.txt")
        self.assertIn("-j", command)
        for setting in ("output.jsonl=true", "output.private=false",
                        "extractor.instagram.api=rest", "extractor.instagram.pinned=true",
                        "extractor.instagram.retries=0", "extractor.instagram.max-posts=17",
                        "cursor=abc123"):
            self.assertIn(setting, command)
        self.assertEqual(command[-1], "https://www.instagram.com/qnl/posts/")

    def test_only_the_cookies_gallery_dl_needs_are_lent(self):
        jar = [{"name": "sessionid", "value": "s", "domain": ".instagram.com"},
               {"name": "csrftoken", "value": "c", "domain": ".instagram.com"},
               {"name": "datr", "value": "fingerprint", "domain": ".instagram.com"},
               {"name": "ps_l", "value": "x", "domain": ".instagram.com"}]

        self.assertEqual([c["name"] for c in lend_cookies(jar)], ["sessionid", "csrftoken"])

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

    def test_stale_lent_cookie_folders_are_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / f"{SCRATCH_PREFIX}1-1"
            stale.mkdir()
            (stale / "cookies.txt").write_text("secret")
            (Path(folder) / "unrelated").mkdir()

            self.assertEqual(clear_stale_scratch(Path(folder)), 1)
            self.assertFalse(stale.exists())
            self.assertTrue((Path(folder) / "unrelated").exists())

    def test_latest_n_lists_a_buffer_beyond_n_and_other_modes_list_until_stopped(self):
        self.assertEqual(discovery_limit("latest_n", 5), 17)
        self.assertEqual(discovery_limit("latest_n", 100), 200)
        self.assertIsNone(discovery_limit("date_range", 5))
        self.assertIsNone(discovery_limit("until_stopped", None))


class _Inner(FakeInstagram):
    """The browser client's part: everything but the listing."""

    session_value = "1%3Aabc"        # what the browser's sessionid cookie holds now

    def cookie_jar(self):
        return [{"name": "sessionid", "value": self.session_value, "domain": ".instagram.com",
                 "path": "/", "secure": True},
                {"name": "datr", "value": "not lent", "domain": ".instagram.com"}]


class StreamingTestCase(unittest.TestCase):
    """Against the fake gallery-dl program, run as a real subprocess."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.scratch = self.tmp / "scratch"
        self.inner = _Inner()
        self.inner.add_profile("qatarballers", [
            post(code, "2026-09-01T00:00:00Z", owner="qatarballers") for code in FIXTURE_CODES])
        # the recorded listing names the real account's id; the profile the
        # browser reads must agree, or the engine rightly skips the posts
        self.inner.profiles["qatarballers"].user_id = "4267196155"
        self._env = {}
        self.addCleanup(self._restore_env)

    def env(self, **values):
        for key, value in values.items():
            self._env[key] = os.environ.get(key)
            os.environ[key] = str(value)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def client(self, **kwargs) -> GalleryListingClient:
        client = GalleryListingClient(self.inner, module=FAKE_MODULE,
                                      scratch_dir=self.scratch, **kwargs)
        self.addCleanup(client.close)
        return client


class StreamingTests(StreamingTestCase):
    def test_posts_arrive_while_gallery_dl_is_still_running(self):
        self.env(FAKE_GALLERY_DL_DELAY="0.4")
        listing = self.client().profile_posts("qatarballers")

        started = time.monotonic()
        first = next(listing)
        elapsed = time.monotonic() - started

        self.assertEqual(first.shortcode, FIXTURE_CODES[0])
        self.assertIsNone(listing.process.poll())          # still listing
        self.assertLess(elapsed, 1.2)                       # not the whole run
        rest = [p.shortcode for p in listing]
        self.assertEqual(rest, FIXTURE_CODES[1:])
        self.assertEqual(listing.outcome, "exhausted")

    def test_closing_a_listing_stops_gallery_dl(self):
        self.env(FAKE_GALLERY_DL_DELAY="0.5")
        listing = self.client().profile_posts("qatarballers")
        next(listing)
        process = listing.process

        listing.close()

        self.assertIsNotNone(process.poll())                # gone
        self.assertEqual(listing.outcome, "stopped_by_engine")
        self.assertFalse(list(self.scratch.glob(SCRATCH_PREFIX + "*")))   # cookies gone

    def test_a_rate_limit_resumes_from_the_cursor_not_from_the_start(self):
        flag = self.tmp / "failed-once"
        self.env(FAKE_GALLERY_DL_FAIL_AFTER="1", FAKE_GALLERY_DL_FAIL_FLAG=str(flag))
        listing = self.client().profile_posts("qatarballers")

        first = next(listing)
        with self.assertRaises(RateLimited) as refused:
            next(listing)
        self.assertIn("cursor", str(refused.exception))
        second = next(listing)                              # after the engine's wait
        rest = [p.shortcode for p in listing]

        self.assertEqual([first.shortcode, second.shortcode] + rest, FIXTURE_CODES)
        self.assertEqual(len(listing.commands), 2)
        self.assertNotIn("cursor=", " ".join(listing.commands[0]))
        self.assertIn("cursor=1", " ".join(listing.commands[1]))
        self.assertEqual(listing.resumed_from, ["1"])

    def test_the_engine_waits_out_the_rate_limit_and_keeps_what_was_listed(self):
        flag = self.tmp / "failed-once"
        self.env(FAKE_GALLERY_DL_FAIL_AFTER="2", FAKE_GALLERY_DL_FAIL_FLAG=str(flag))
        client = self.client()
        cfg = InstagramCaptureConfig.from_dict({
            "targets": ["qatarballers"], "mode": "until_stopped", "surfaces": ["posts"],
            "listing": "gallery-dl", "capture_media": False})
        session = InstagramCaptureSession(config=cfg, client=client, output_dir=self.tmp / "out",
                                          crawl_id=1, crawl_name="t", sleep=lambda _s: None)

        session.run()

        self.assertEqual(list(session.archive.posts), FIXTURE_CODES)
        self.assertGreaterEqual(session.counters["rate_limit_waits"], 1)
        self.assertEqual(len(client.commands), 2)


class RecoveryTests(StreamingTestCase):
    def test_a_sign_in_the_curator_made_reaches_the_next_gallery_dl_run(self):
        """The first run is refused as signed out; the curator signs in,
        the browser's session changes; the next run is lent the new one."""
        self.inner.session_value = "old-session"
        self.env(FAKE_GALLERY_DL_REJECT_SESSION="old-session")
        listing = self.client().profile_posts("qatarballers")

        with self.assertRaises(LoginRequired):
            next(listing)
        self.inner.session_value = "new-session"           # the curator signed in
        codes = [p.shortcode for p in listing]

        self.assertEqual(codes, FIXTURE_CODES)
        self.assertEqual(len(listing.commands), 2)

    def test_the_engine_recovers_a_gallery_dl_sign_in_through_the_curator(self):
        self.inner.session_value = "old-session"
        self.env(FAKE_GALLERY_DL_REJECT_SESSION="old-session")
        answers = iter(["resume"])
        client = self.client()
        cfg = InstagramCaptureConfig.from_dict({
            "targets": ["qatarballers"], "mode": "until_stopped", "surfaces": ["posts"],
            "listing": "gallery-dl", "capture_media": False})

        def control_poll():
            # the curator resolves the hold by signing in, then continues
            answer = next(answers, None)
            if answer:
                self.inner.session_value = "new-session"
            return answer
        session = InstagramCaptureSession(config=cfg, client=client, output_dir=self.tmp / "out",
                                          crawl_id=1, crawl_name="t", sleep=lambda _s: None,
                                          control_poll=control_poll)

        session.run()

        self.assertEqual(list(session.archive.posts), FIXTURE_CODES)

    def test_stop_is_honoured_while_gallery_dl_is_waiting_on_instagram(self):
        self.env(FAKE_GALLERY_DL_DELAY="6")
        client = self.client()
        asked = {"at": time.monotonic()}
        client.attach_engine_controls(lambda: None,
                                      lambda: time.monotonic() - asked["at"] > 1.0)
        listing = client.profile_posts("qatarballers")

        started = time.monotonic()
        with self.assertRaises(TargetUnavailable) as stopped:
            next(listing)

        self.assertEqual(str(stopped.exception), "stopped")
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIsNone(listing.process)
        self.assertEqual(listing.outcome, "stopped_by_curator")


class EvidenceTests(StreamingTestCase):
    def run_engine(self, **config):
        client = self.client(limit=17)
        cfg = InstagramCaptureConfig.from_dict({
            "targets": ["qatarballers"], "mode": "latest_n", "latest_n": 5,
            "surfaces": ["posts"], "listing": "gallery-dl", **config})
        session = InstagramCaptureSession(config=cfg, client=client, output_dir=self.tmp / "out",
                                          crawl_id=1, crawl_name="t", sleep=lambda _s: None)
        session.run()
        return client, session

    def test_the_listing_is_kept_as_the_tools_output_not_as_a_response(self):
        client, session = self.run_engine()
        out = self.tmp / "out"

        rows = [json.loads(l) for l in (out / "instagram-posts.jsonl").read_text().splitlines()]
        self.assertEqual([r["shortcode"] for r in rows], FIXTURE_CODES)
        origin = rows[0]["provenance"]
        self.assertEqual(origin["listing_source"], "gallery-dl")
        self.assertFalse(origin["instagram_raw_response_available"])
        self.assertIsNone(origin["response"])
        evidence = out / origin["listing_evidence"]
        self.assertTrue(evidence.exists())
        self.assertTrue(str(evidence).replace("\\", "/").endswith(
            "evidence/listings/gallery-dl-000001.jsonl"))
        self.assertEqual(evidence.read_text(encoding="utf-8"),
                         FIXTURE.read_text(encoding="utf-8"))
        self.assertFalse(list((out / "raw" / "responses").glob("*")) if (out / "raw" / "responses").exists() else [])
        meta = json.loads(evidence.with_suffix(".json").read_text(encoding="utf-8"))
        events = {e["event"]: e for e in meta["events"]}
        self.assertIn("<lent cookies>", events["started"]["command"])
        self.assertNotIn("cookies.txt", " ".join(events["started"]["command"]))
        self.assertEqual(events["finished"]["outcome"], "exhausted")
        self.assertTrue(evidence.with_suffix(".log").exists())
        checksums = (out / "checksums.sha256").read_text()
        self.assertIn("evidence/listings/gallery-dl-000001.jsonl", checksums)
        manifest = json.loads((out / "instagram-manifest.json").read_text())
        self.assertIn("not the responses", manifest["layers"]["listings"]["meaning"])
        self.assertEqual(manifest["capture"]["listing_source"], "gallery-dl")

    def test_media_says_who_found_the_url_and_which_client_fetched_it(self):
        self.inner.last_fetch_via = "browser-page"
        posts = posts_from_gallery_lines(FIXTURE.read_text(encoding="utf-8").splitlines(),
                                         "qatarballers")
        for p in posts:
            for m in p.media:
                self.inner.media[m.url] = b"bytes"
        client, session = self.run_engine(capture_media=True)

        index = json.loads((self.tmp / "out" / "instagram-media.json").read_text())
        self.assertTrue(index)
        for entry in index.values():
            self.assertEqual(entry["discovered_by"], "gallery-dl")
            self.assertEqual(entry["fetched_via"], "browser-page")
            self.assertFalse(entry["browser_fallback"])

    def test_the_reported_comment_count_is_filled_from_the_posts_own_page(self):
        seen = post(FIXTURE_CODES[1], "2026-09-02T00:00:00Z", owner="qatarballers",
                    comments_count=7)
        self.inner.observed_post = lambda code: seen if code == FIXTURE_CODES[1] else None

        self.run_engine(include_comments=True)

        row = next(json.loads(l) for l in (self.tmp / "out" / "instagram-posts.jsonl").read_text().splitlines()
                   if json.loads(l)["shortcode"] == FIXTURE_CODES[1])
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
