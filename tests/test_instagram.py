"""The Instagram capture engine, against a stand-in for Instagram.

Nothing here reaches Instagram. The fake lays out profiles and posts in the
order Instagram would show them, and imposes the conditions Instagram does --
a rate limit, a login wall, a checkpoint -- so what is tested is the engine's
policy: what it selects, when it stops, what it writes, and what it does when
Instagram pushes back.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from webarc.instagram import (BLOCKED, InstagramCaptureConfig,
                              InstagramCaptureSession, MediaItem,
                              mark_pinned_by_order, parse_instagram_target)

from tests.instagram_fakes import FakeInstagram, comment, post


def day(n: int) -> str:
    return f"2026-03-{n:02d}T12:00:00Z"


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = FakeInstagram()
        self.commands: list = []
        self.progress: list[dict] = []
        self.opened: list[str] = []

    def session(self, targets, **overrides):
        raw = {"targets": targets, "mode": "latest_n", "latest_n": 100,
               **overrides}
        config = InstagramCaptureConfig.from_dict(raw)
        commands = iter(self.commands)

        def poll():
            try:
                return next(commands)
            except StopIteration:
                return None

        def open_browser(url):
            self.opened.append(url)
            return lambda: None

        return InstagramCaptureSession(
            config=config, client=self.fake, output_dir=self.tmp,
            crawl_id=7, crawl_name="test", control_poll=poll,
            on_progress=lambda **kw: self.progress.append(kw),
            open_browser=open_browser, sleep=lambda _s: None)

    def read_jsonl(self, name):
        path = self.tmp / name
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l]

    def manifest(self):
        return json.loads((self.tmp / "instagram-manifest.json").read_text())


class TargetTests(unittest.TestCase):
    def test_a_username_a_profile_url_and_a_post_url_are_told_apart(self):
        self.assertEqual(parse_instagram_target("qnl").kind, "profile")
        self.assertEqual(parse_instagram_target(
            "https://www.instagram.com/qnl/").kind, "profile")
        self.assertEqual(parse_instagram_target(
            "https://www.instagram.com/p/Cabc123/").shortcode, "Cabc123")
        self.assertEqual(parse_instagram_target(
            "https://www.instagram.com/reel/Cabc123/").kind, "reel")

    def test_what_cannot_be_an_archive_is_refused_with_the_reason(self):
        for url, reason in (
            ("https://www.instagram.com/explore/tags/doha/", "ranking"),
            ("https://www.instagram.com/direct/inbox/", "direct messages"),
            ("https://www.instagram.com/stories/qnl/1/", "stories"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    parse_instagram_target(url)
                self.assertIn(reason, str(caught.exception))

    def test_the_same_target_twice_is_one_target(self):
        config = InstagramCaptureConfig.from_dict({
            "targets": ["qnl", "https://www.instagram.com/qnl/"],
            "mode": "latest_n"})

        self.assertEqual(len(config.targets), 1)

    def test_targets_may_arrive_as_lines(self):
        config = InstagramCaptureConfig.from_dict({
            "targets": "qnl\nmanara\n", "mode": "latest_n"})

        self.assertEqual(len(config.targets), 2)


class PinnedByOrderTests(unittest.TestCase):
    """Instagram no longer says which posts are pinned. It still shows them
    first, so a post at the top that is older than one shown after it is
    what a pinned post looks like from the outside."""

    def test_an_old_post_shown_first_is_pinned(self):
        posts = [post("old", day(1)), post("a", day(20)), post("b", day(19)),
                 post("c", day(18)), post("d", day(17))]

        mark_pinned_by_order(posts)

        self.assertTrue(posts[0].is_pinned)
        self.assertFalse(any(p.is_pinned for p in posts[1:]))

    def test_a_chronological_head_has_no_pinned_posts(self):
        posts = [post(str(n), day(20 - n)) for n in range(6)]

        mark_pinned_by_order(posts)

        self.assertFalse(any(p.is_pinned for p in posts))

    def test_three_pinned_are_all_recognised(self):
        posts = [post("p1", day(3)), post("p2", day(1)), post("p3", day(2)),
                 post("a", day(20)), post("b", day(19)), post("c", day(18))]

        mark_pinned_by_order(posts)

        self.assertEqual([p.is_pinned for p in posts[:3]], [True] * 3)
        self.assertFalse(posts[3].is_pinned)


class LatestNTests(EngineTestCase):
    def test_the_newest_n_are_taken_and_pinned_posts_do_not_count(self):
        self.fake.add_profile("qnl", [post("pin", day(1))] + [
            post(f"n{n}", day(25 - n)) for n in range(10)])

        self.session(["qnl"], mode="latest_n", latest_n=3).run()

        codes = [p["shortcode"] for p in self.read_jsonl("instagram-posts.jsonl")]
        self.assertEqual(codes, ["pin", "n0", "n1", "n2"])

    def test_reels_are_walked_too_and_duplicates_are_one_post(self):
        shared = post("both", day(10), kind="reel")
        self.fake.add_profile("qnl", [shared, post("a", day(9))],
                              reels=[shared, post("r", day(8), kind="reel")])

        self.session(["qnl"], mode="latest_n", latest_n=10).run()

        codes = sorted(p["shortcode"] for p in self.read_jsonl("instagram-posts.jsonl"))
        self.assertEqual(codes, ["a", "both", "r"])
        self.assertEqual(self.manifest()["layers"]["normalised"][
            "selection_exclusions"]["duplicate_across_surfaces"], 1)


class DateRangeTests(EngineTestCase):
    def test_only_posts_in_the_range_are_exported(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(28, 0, -1)])

        self.session(["qnl"], mode="date_range", from_date="2026-03-10",
                     to_date="2026-03-15").run()

        codes = sorted(int(p["shortcode"]) for p in self.read_jsonl("instagram-posts.jsonl"))
        self.assertEqual(codes, [10, 11, 12, 13, 14, 15])

    def test_a_pinned_post_older_than_from_does_not_end_the_capture(self):
        self.fake.add_profile("qnl", [post("pin", day(1))] + [
            post(str(n), day(n)) for n in range(28, 0, -1)])

        self.session(["qnl"], mode="date_range", from_date="2026-03-20").run()

        codes = [p["shortcode"] for p in self.read_jsonl("instagram-posts.jsonl")]
        self.assertNotIn("pin", codes)         # outside the range
        self.assertIn("20", codes)             # the capture went on past it

    def test_the_walk_stops_after_consecutive_older_posts(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(28, 0, -1)])

        self.session(["qnl"], mode="date_range", from_date="2026-03-20").run()

        # 28..20 selected, then 19..15 are five older, then stop
        self.assertLessEqual(self.fake.calls["post_list"], 9 + 5 + 6)
        self.assertEqual(self.manifest()["counts"]["posts_exported"], 9)


class SinceLastTests(EngineTestCase):
    def test_only_posts_newer_than_the_last_capture_are_taken(self):
        self.fake.add_profile("qnl", [post(str(n), day(n), media_id=str(n))
                                      for n in range(28, 0, -1)])

        self.session(["qnl"], mode="since_last", prior_newest={
            "instagram:@qnl": {"media_id": "20", "date": day(20)}}).run()

        codes = sorted(int(p["shortcode"]) for p in self.read_jsonl("instagram-posts.jsonl"))
        self.assertEqual(codes, list(range(21, 29)))

    def test_since_last_without_prior_state_is_refused_before_it_starts(self):
        with self.assertRaises(ValueError) as caught:
            InstagramCaptureConfig.from_dict({"targets": ["qnl"],
                                              "mode": "since_last"})
        self.assertIn("No previous capture state", str(caught.exception))

    def test_the_newest_post_is_remembered_for_next_time(self):
        self.fake.add_profile("qnl", [post("pin", day(1)),
                                      post("new", day(25), media_id="25"),
                                      post("old", day(24), media_id="24")])
        session = self.session(["qnl"], mode="latest_n")

        session.run()

        self.assertEqual(session.newest_by_target["instagram:@qnl"]["media_id"], "25")


class SinglePostTests(EngineTestCase):
    def test_a_post_url_captures_that_post_only(self):
        self.fake.add_profile("qnl", [post("Cone1", day(5)), post("Ctwo2", day(4))])

        self.session(["https://www.instagram.com/p/Cone1/"]).run()

        codes = [p["shortcode"] for p in self.read_jsonl("instagram-posts.jsonl")]
        self.assertEqual(codes, ["Cone1"])
        self.assertEqual(self.fake.calls["post_list"], 0)


class MediaTests(EngineTestCase):
    def test_every_carousel_component_is_kept_in_order(self):
        items = [MediaItem(url=f"https://cdn.example/c{n}.jpg", kind="image",
                           position=n) for n in range(3)]
        self.fake.add_profile("qnl", [post("car", day(5), kind="carousel",
                                           media=items)])

        self.session(["qnl"]).run()

        row = self.read_jsonl("instagram-posts.jsonl")[0]
        self.assertEqual(row["media_urls"], [i.url for i in items])
        index = json.loads((self.tmp / "instagram-media.json").read_text())
        self.assertEqual(len(index), 3)

    def test_media_is_content_addressed_with_sha256(self):
        import hashlib
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["qnl"]).run()

        entry = list(json.loads((self.tmp / "instagram-media.json").read_text()).values())[0]
        body = (self.tmp / "media" / entry["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), entry["sha256"])
        self.assertIn(entry["sha256"], (self.tmp / "checksums.sha256").read_text())

    def test_a_failed_download_is_recorded_not_dropped(self):
        self.fake.add_profile("qnl", [post("a", day(5)), post("b", day(4))])
        self.fake.fail_media.add("https://cdn.example/a.jpg")

        self.session(["qnl"]).run()

        counts = self.manifest()["counts"]
        self.assertEqual(counts["media_failed"], 1)
        self.assertEqual(counts["media_captured"], 1)
        self.assertEqual(counts["posts_exported"], 2)      # the post is still there
        events = self.read_jsonl("instagram-events.jsonl")
        self.assertTrue(any(e["event"] == "media_failed" for e in events))

    def test_media_can_be_left_out(self):
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["qnl"], capture_media=False).run()

        self.assertEqual(self.fake.calls["fetch"], 0)


class RawLayerTests(EngineTestCase):
    def test_instagrams_payload_is_kept_as_received(self):
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["qnl"]).run()

        raw = json.loads((self.tmp / "raw" / "posts" / "a.json").read_text())
        self.assertEqual(raw, {"shortcode": "a", "fake": True})
        self.assertTrue((self.tmp / "raw" / "profiles" / "qnl.json").exists())

    def test_the_raw_layer_is_covered_by_fixity(self):
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["qnl"]).run()

        self.assertIn("raw/posts/a.json", (self.tmp / "checksums.sha256").read_text())


class CommentTests(EngineTestCase):
    def thread(self, n_top: int, replies_each: int = 0):
        out = []
        for i in range(n_top):
            out.append(comment(f"c{i}", "a", f"top {i}"))
            for r in range(replies_each):
                out.append(comment(f"c{i}r{r}", "a", f"reply {r}", parent=f"c{i}"))
        return out

    def test_comments_are_collected_when_enabled(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=3)])
        self.fake.add_comments("a", self.thread(3))

        self.session(["qnl"], include_comments=True).run()

        self.assertEqual(len(self.read_jsonl("instagram-comments.jsonl")), 3)

    def test_comments_are_not_requested_when_not_enabled(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=3)])
        self.fake.add_comments("a", self.thread(3))

        self.session(["qnl"]).run()

        self.assertEqual(self.fake.calls["comments"], 0)

    def test_the_per_post_maximum_is_enforced(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=50)])
        self.fake.add_comments("a", self.thread(50))

        self.session(["qnl"], include_comments=True, max_comments_per_post=7).run()

        self.assertEqual(len(self.read_jsonl("instagram-comments.jsonl")), 7)

    def test_replies_need_asking_for_and_have_their_own_cap(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=2)])
        self.fake.add_comments("a", self.thread(2, replies_each=5))

        self.session(["qnl"], include_comments=True).run()
        without = len(self.read_jsonl("instagram-comments.jsonl"))
        self.setUp()
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=2)])
        self.fake.add_comments("a", self.thread(2, replies_each=5))
        self.session(["qnl"], include_comments=True, include_replies=True,
                     max_replies_per_comment=2).run()
        with_replies = self.read_jsonl("instagram-comments.jsonl")

        self.assertEqual(without, 2)
        self.assertEqual(len(with_replies), 2 + 2 * 2)
        self.assertEqual(sum(1 for c in with_replies if c["depth"] == 1), 4)

    def test_the_shortfall_against_the_stated_thread_is_reported(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=455)])
        self.fake.add_comments("a", self.thread(19))

        self.session(["qnl"], include_comments=True, max_comments_per_post=500).run()

        coverage = self.manifest()["coverage"]
        self.assertEqual(coverage["comments_stated_on_posts"], 455)
        self.assertEqual(coverage["comments_not_collected"], 455 - 19)

    def test_raw_comment_payloads_are_kept(self):
        self.fake.add_profile("qnl", [post("a", day(5), comments_count=2)])
        self.fake.add_comments("a", self.thread(2))

        self.session(["qnl"], include_comments=True).run()

        self.assertTrue((self.tmp / "raw" / "comments" / "a.json").exists())


class WhenInstagramPushesBackTests(EngineTestCase):
    """A run is built to be interrupted by Instagram and resumed."""

    def test_a_rate_limit_is_waited_out_and_the_run_continues(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(9, 0, -1)])
        self.fake.rate_limit_after = 4

        self.session(["qnl"]).run()

        self.assertEqual(self.manifest()["counts"]["posts_exported"], 9)
        self.assertEqual(self.manifest()["counts"]["rate_limit_waits"], 1)
        self.assertTrue(any("limiting requests" in str(p["details"]["message"])
                            for p in self.progress))

    def test_repeated_limits_back_off_and_a_success_resets_them(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(9, 0, -1)])
        self.fake.rate_limit_after = 2
        self.fake.rate_limit_once = False       # every call limited...
        calls = {"n": 0}
        original = self.fake._tick

        def sometimes(what):
            calls["n"] += 1
            if calls["n"] in (3, 4, 5, 9):       # ...only these times
                self.fake.calls[what] += 1
                self.fake.calls["total"] += 1
                from webarc.instagram import RateLimited
                raise RateLimited(60.0, "limited")
            self.fake.rate_limit_after = None
            original(what)
        self.fake._tick = sometimes

        self.session(["qnl"]).run()

        waits = [e["wait_seconds"] for e in self.read_jsonl("instagram-events.jsonl")
                 if e["event"] == "rate_limited"]
        self.assertEqual(waits, [60.0, 120.0, 240.0, 60.0])

    def test_stop_is_answered_during_a_rate_limit_wait(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(9, 0, -1)])
        self.fake.rate_limit_after = 2
        self.fake.rate_limit_once = False
        self.commands.extend([None, None, "stop"])

        result = self.session(["qnl"]).run()

        self.assertEqual(result["stop_reason"], "curator_stop")

    def test_a_login_wall_opens_the_browser_and_holds_for_the_curator(self):
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.fake.login_wall = True
        self.commands.extend([None, "resume"])

        session = self.session(["qnl"])
        session.run()

        self.assertEqual(self.opened, ["https://www.instagram.com/qnl/"])
        self.assertTrue(any(p["state"] == BLOCKED for p in self.progress))
        self.assertEqual(self.fake.refreshed, 1)
        self.assertEqual(self.manifest()["counts"]["posts_exported"], 1)

    def test_a_checkpoint_is_the_same_hold(self):
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.fake.checkpoint = True
        self.commands.extend(["resume"])

        self.session(["qnl"]).run()

        self.assertEqual(len(self.opened), 1)
        self.assertTrue(any("verification" in str(p["details"]["message"])
                            for p in self.progress))

    def test_stopping_while_held_ends_the_run_cleanly(self):
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.fake.login_wall = True
        self.commands.extend(["stop"])

        result = self.session(["qnl"]).run()

        self.assertEqual(result["stop_reason"], "curator_stop")
        self.assertTrue((self.tmp / "instagram-manifest.json").exists())

    def test_a_private_profile_the_viewer_cannot_see_is_reported_not_crashed(self):
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))], private=True)
        self.commands.extend(["resume", "resume", "resume"])   # stays signed out

        self.session(["qnl"]).run()

        targets = self.manifest()["capture"]["targets"]
        self.assertEqual(targets[0]["status"], "unavailable")
        self.assertIn("private", targets[0]["reason"])

    def test_one_bad_target_does_not_stop_the_others(self):
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["nobody_here", "qnl"]).run()

        targets = {t["url"]: t["status"] for t in self.manifest()["capture"]["targets"]}
        self.assertEqual(targets["https://www.instagram.com/nobody_here/"], "unavailable")
        self.assertEqual(targets["https://www.instagram.com/qnl/"], "done")


class SignInFirstTests(EngineTestCase):
    """Instagram answers an anonymous client with "please wait a few minutes"
    and 429, never with "login required". Waiting for it to ask for a sign-in
    means waiting for a rate limit instead, so the browser opens for the
    curator before anything is asked of Instagram."""

    def test_with_no_session_the_browser_opens_before_any_request(self):
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.fake.sign_in_on_refresh = "curator"
        self.commands.extend(["resume"])

        self.session(["qnl"]).run()

        self.assertEqual(self.opened, ["https://www.instagram.com/accounts/login/"])
        self.assertEqual(self.fake.calls["profile"], 1)
        first_hold = next(p for p in self.progress if p["state"] == BLOCKED)
        self.assertIn("not signed in", first_hold["details"]["message"])

    def test_the_session_is_read_again_once_the_curator_continues(self):
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.fake.sign_in_on_refresh = "curator"
        self.commands.extend(["resume"])

        session = self.session(["qnl"])
        session.run()

        self.assertEqual(session.viewer_username, "curator")
        self.assertEqual(self.manifest()["capture"]["viewer"], "signed_in")
        self.assertEqual(self.fake.refreshed, 1)

    def test_a_curator_who_stops_at_the_sign_in_gets_a_manifest_not_a_crash(self):
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.commands.extend(["stop"])

        result = self.session(["qnl"]).run()

        self.assertEqual(result["stop_reason"], "curator_stop")
        self.assertEqual(self.fake.calls["profile"], 0)
        self.assertTrue((self.tmp / "instagram-manifest.json").exists())

    def test_continuing_signed_out_is_recorded_as_the_curators_decision(self):
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.commands.extend(["resume", "resume", "resume"])

        self.session(["qnl"]).run()

        events = [e["event"] for e in self.read_jsonl("instagram-events.jsonl")]
        self.assertIn("proceeding_signed_out", events)
        self.assertEqual(self.manifest()["capture"]["viewer"], "signed_out")


class ControlTests(EngineTestCase):
    def test_pause_holds_between_posts_and_resume_continues(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(5, 0, -1)])
        self.commands.extend([None, "pause", None, None, "resume"])

        self.session(["qnl"]).run()

        self.assertEqual(self.manifest()["counts"]["posts_exported"], 5)
        self.assertTrue(any(p["state"] == "paused" for p in self.progress))

    def test_stop_between_posts_keeps_what_was_collected(self):
        self.fake.add_profile("qnl", [post(str(n), day(n)) for n in range(5, 0, -1)])
        self.commands.extend([None, None, "stop"])

        result = self.session(["qnl"]).run()

        self.assertEqual(result["stop_reason"], "curator_stop")
        self.assertGreaterEqual(self.manifest()["counts"]["posts_exported"], 1)
        self.assertTrue((self.tmp / "instagram-posts.csv").exists())


class ManifestTests(EngineTestCase):
    def test_the_manifest_never_claims_everything(self):
        self.fake.add_profile("qnl", [post("a", day(5))])

        self.session(["qnl"]).run()

        claim = self.manifest()["completeness"]["claim"]
        self.assertIn("accessible to this session", claim)
        self.assertIn("does not claim", claim)

    def test_a_signed_out_capture_says_what_bounds_it(self):
        """The curator continued three times without signing in: their call."""
        self.fake = FakeInstagram(signed_in=None)
        self.fake.add_profile("qnl", [post("a", day(5))])
        self.commands.extend(["resume", "resume", "resume"])

        self.session(["qnl"]).run()

        manifest = self.manifest()
        self.assertEqual(manifest["capture"]["viewer"], "signed_out")
        self.assertIn("Signed out", manifest["completeness"]["signed_out_capture_meaning"])

    def test_end_of_timeline_is_named_as_such(self):
        self.fake.add_profile("qnl", [post("a", day(5)), post("b", day(4))])

        result = self.session(["qnl"], mode="end_of_timeline").run()

        self.assertEqual(result["stop_reason"], "end_of_available_timeline")

    def test_a_continued_package_does_not_duplicate_what_it_holds(self):
        self.fake.add_profile("qnl", [post("a", day(5)), post("b", day(4))])
        self.session(["qnl"]).run()

        self.progress.clear()
        self.session(["qnl"]).run()      # same directory, same posts

        self.assertEqual(len(self.read_jsonl("instagram-posts.jsonl")), 2)


if __name__ == "__main__":
    unittest.main()


class NativeChromeSessionTests(unittest.TestCase):
    """The system's own Chrome, attached over CDP, with and without a window."""

    @classmethod
    def setUpClass(cls):
        from tests.chrome_for_tests import find_chrome
        cls.chrome = find_chrome()
        if not cls.chrome:                                   # pragma: no cover
            raise unittest.SkipTest("no Chrome binary to drive natively")

    def native_client(self, cookies):
        from webarc.config import BrowserConfig
        from webarc.instagram_browser import InstagramBrowserClient
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = InstagramBrowserClient(
            BrowserConfig(mode="native", user_data_dir=tmp.name,
                          chrome_path=self.chrome),
            sleep=lambda s: None, headless=True, settle=(0.1, 0.2))
        client.start()
        self.addCleanup(client.close)
        if cookies:
            client._context.add_cookies(cookies)
            client.refresh()
        return client

    def test_a_signed_in_native_profile_yields_the_viewer(self):
        far = 4102444800
        client = self.native_client([
            {"name": "sessionid", "value": "7%3Axyz", "domain": ".instagram.com",
             "path": "/", "expires": far, "secure": True, "httpOnly": True},
            {"name": "ds_user_id", "value": "77", "domain": ".instagram.com",
             "path": "/", "expires": far}])

        self.assertEqual(client.viewer(), "77")
        self.assertTrue(client.user_agent.startswith("Mozilla/5.0"))

    def test_an_empty_native_profile_has_no_viewer(self):
        self.assertIsNone(self.native_client([]).viewer())

    def test_each_native_launch_takes_a_port_of_its_own(self):
        """So an Instagram job never collides with a recording's CDP port."""
        from webarc.instagram import _close_native_chrome, _launch_native_chrome
        tmp_a = tempfile.TemporaryDirectory(); self.addCleanup(tmp_a.cleanup)
        tmp_b = tempfile.TemporaryDirectory(); self.addCleanup(tmp_b.cleanup)
        first, port_a = _launch_native_chrome(tmp_a.name, True, self.chrome)
        try:
            second, port_b = _launch_native_chrome(tmp_b.name, True, self.chrome)
            try:
                self.assertNotEqual(port_a, port_b)
            finally:
                _close_native_chrome(port_b, second)
        finally:
            _close_native_chrome(port_a, first)


class BrowserChoiceTests(unittest.TestCase):
    def test_the_job_carries_the_browser_choice(self):
        config = InstagramCaptureConfig.from_dict({
            "targets": ["qnl"], "mode": "latest_n",
            "browser": {"mode": "native", "user_data_dir": "/tmp/p",
                        "chrome_path": "/opt/chrome"}})

        self.assertEqual(config.browser_mode, "native")
        self.assertEqual(config.browser_profile_dir, "/tmp/p")
        self.assertEqual(config.chrome_path, "/opt/chrome")

    def test_headed_with_a_window_is_the_default(self):
        config = InstagramCaptureConfig.from_dict({"targets": ["qnl"]})

        self.assertEqual(config.browser_mode, "headed")
        self.assertFalse(config.headless)

    def test_a_run_can_be_asked_to_have_no_window(self):
        config = InstagramCaptureConfig.from_dict({"targets": ["qnl"],
                                                   "headless": True})

        self.assertTrue(config.headless)

    def test_anything_else_is_refused(self):
        with self.assertRaises(ValueError):
            InstagramCaptureConfig.from_dict({
                "targets": ["qnl"], "browser": {"mode": "firefox"}})


