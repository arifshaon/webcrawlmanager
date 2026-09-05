"""The X capture: targets, X's payload shapes, the engine with a stand-in
client, the reader pages, and the server's job endpoints."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Iterator, Optional

from webarc.x import (RateLimited, XArchive, XCaptureConfig, XCaptureSession,
                      parse_x_target)
from webarc.x_extract import (XMediaItem, XPost, XUser, describe_graphql_request,
                              original_image_url, read_timeline, snowflake_time,
                              user_from_result, x_time_to_iso)

from tests.fixtures.x import serve


class TargetTests(unittest.TestCase):
    def test_a_handle_with_or_without_the_at_sign_is_a_profile(self):
        for raw in ("qnl", "@qnl", "https://x.com/qnl", "x.com/qnl/with_replies",
                    "https://twitter.com/qnl/media", "https://mobile.x.com/qnl"):
            with self.subTest(raw=raw):
                target = parse_x_target(raw)
                self.assertEqual(target.kind, "profile")
                self.assertEqual(target.handle, "qnl")
                self.assertEqual(target.url, "https://x.com/qnl")
                self.assertEqual(target.key, "x:@qnl")

    def test_a_status_address_is_a_post(self):
        target = parse_x_target("https://x.com/qnl/status/1234567890123456789?s=20")
        self.assertEqual((target.kind, target.post_id, target.handle),
                         ("post", "1234567890123456789", "qnl"))
        self.assertEqual(parse_x_target("https://x.com/i/status/99").post_id, "99")
        self.assertEqual(target.key, "x:/1234567890123456789")

    def test_a_hashtag_or_a_search_is_a_search(self):
        tag = parse_x_target("#books")
        self.assertEqual((tag.kind, tag.query, tag.product), ("search", "#books", "Latest"))
        by_url = parse_x_target("https://x.com/hashtag/books?src=hashtag_click", product="Top")
        self.assertEqual((by_url.query, by_url.product), ("#books", "Top"))
        typed = parse_x_target("search: qatar library")
        self.assertEqual(typed.query, "qatar library")
        self.assertIn("q=qatar%20library", typed.url)
        from_url = parse_x_target("https://x.com/search?q=%23doha&src=typed_query&f=top")
        self.assertEqual((from_url.query, from_url.product), ("#doha", "Top"))
        self.assertEqual(typed.key, "x:search:latest:qatar library")

    def test_the_viewers_own_pages_and_other_sites_are_refused(self):
        for raw in ("home", "https://x.com/home", "https://x.com/notifications",
                    "https://x.com/messages", "https://x.com/i/bookmarks",
                    "https://instagram.com/qnl", "https://x.com/", "",
                    "this-is-not-a-handle-at-all", "https://x.com/qnl/likes/extra"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_x_target(raw)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_deduplication(self):
        config = XCaptureConfig.from_dict({"targets": "qnl\n@qnl\n#books"})
        self.assertEqual(config.targets, ["https://x.com/qnl",
                                          "https://x.com/search?q=%23books&src=typed_query&f=live"])
        self.assertEqual(config.mode, "latest_n")
        self.assertEqual(config.surfaces, ("posts",))
        self.assertTrue(config.keep_reposts)
        self.assertGreaterEqual(config.consecutive_older, 2)

    def test_validation(self):
        with self.assertRaises(ValueError):
            XCaptureConfig.from_dict({"targets": ["qnl"], "mode": "date_range"})
        with self.assertRaises(ValueError):
            XCaptureConfig.from_dict({"targets": ["qnl"], "surfaces": ["stories"]})
        with self.assertRaises(ValueError):
            XCaptureConfig.from_dict({"targets": ["qnl"], "mode": "since_last"})
        with self.assertRaises(ValueError):
            XCaptureConfig.from_dict({"targets": ["qnl"], "search_product": "Media"})
        allowed = XCaptureConfig.from_dict({
            "targets": ["qnl", "https://x.com/qnl/status/5"], "mode": "since_last",
            "prior_newest": {"x:@qnl": {"post_id": "3", "date": "2026-01-01T00:00:00Z"}}})
        self.assertEqual(allowed.mode, "since_last")


class ExtractionTests(unittest.TestCase):
    """Reading X's payload shapes without a browser."""

    def fixture(self, obj):
        return json.loads(json.dumps(obj).replace("MEDIAHOST", "pbs.example"))

    def first_page(self) -> dict:
        first = [serve.entry(self.fixture(t)) for t in serve.TIMELINE[:serve.PAGE_SIZE]]
        return serve.timeline_response([
            {"type": "TimelinePinEntry", "entry": serve.entry(self.fixture(serve.PINNED))},
            {"type": "TimelineAddEntries", "entries":
                first[:2] + [serve.promoted_entry(self.fixture(serve.PROMOTED)),
                             serve.who_to_follow()] + first[2:]
                + [serve.cursor("Top", "t1"), serve.cursor("Bottom", "b1")]}])

    def test_a_request_is_recognised_from_its_url(self):
        asked = describe_graphql_request(
            "https://x.com/i/api/graphql/abc123/UserTweets?variables=%7B%22userId%22%3A%22100%22%2C%22cursor%22%3A%22b1%22%7D&features=%7B%7D")
        self.assertEqual(asked["operation"], "UserTweets")
        self.assertEqual(asked["query_id"], "abc123")
        self.assertEqual((asked["user_id"], asked["cursor"]), ("100", "b1"))
        self.assertTrue(asked["listing"])
        self.assertIsNone(describe_graphql_request("https://x.com/qnl"))
        self.assertIsNone(describe_graphql_request("https://x.com/i/api/1.1/jot/client_event.json"))
        search = describe_graphql_request(
            "https://x.com/i/api/graphql/q/SearchTimeline?variables=%7B%22rawQuery%22%3A%22%23books%22%2C%22product%22%3A%22Latest%22%7D")
        self.assertEqual((search["raw_query"], search["product"]), ("#books", "Latest"))

    def test_a_timeline_is_read_in_order_with_the_pinned_post_first(self):
        read = read_timeline([self.first_page()], {"operation": "UserTweets"})

        ids = [p.post_id for p in read.posts]
        self.assertEqual(ids[0], serve.PINNED["rest_id"])
        self.assertTrue(read.posts[0].is_pinned)
        self.assertEqual(ids[1:], [t["rest_id"] for t in serve.TIMELINE[:serve.PAGE_SIZE]])
        self.assertEqual(read.promoted_skipped, 1)
        self.assertGreaterEqual(read.injected_skipped, 1)
        self.assertEqual({c.cursor_type for c in read.cursors}, {"Top", "Bottom"})
        self.assertNotIn(serve.PROMOTED["rest_id"], ids)

    def test_every_record_says_which_entry_and_instruction_it_came_from(self):
        read = read_timeline([self.first_page()], {"operation": "UserTweets", "response": "r1"})
        pinned = read.posts[0]
        self.assertEqual(pinned.provenance["instruction"], "TimelinePinEntry")
        self.assertEqual(pinned.provenance["entry_id"], f"tweet-{serve.PINNED['rest_id']}")
        self.assertEqual(pinned.provenance["response"], "r1")
        self.assertEqual(pinned.provenance["operation"], "UserTweets")
        self.assertTrue(read.posts[1].provenance["path"].endswith("instructions"))

    def test_a_repost_keeps_the_original_and_its_author_inside(self):
        read = read_timeline([serve.timeline_response([{"type": "TimelineAddEntries",
                                                        "entries": [serve.entry(self.fixture(serve.REPOST))]}])])
        post = read.posts[0]
        self.assertEqual(post.relationship, "repost")
        self.assertEqual(post.author_handle, "qnl")
        self.assertEqual(post.original_post["author_handle"], "someone_else")
        self.assertEqual(post.original_post["post_id"], serve.ORIGINAL_BY_OTHER["rest_id"])
        self.assertEqual(post.text, "Something worth sharing")
        self.assertEqual(len(post.media), 1)

    def test_a_quote_carries_the_quoted_post(self):
        read = read_timeline([serve.entry(self.fixture(serve.QUOTE))["content"]["itemContent"]
                              ["tweet_results"]["result"]] and
                             [serve.timeline_response([{"type": "TimelineAddEntries",
                                                        "entries": [serve.entry(self.fixture(serve.QUOTE))]}])])
        post = read.posts[0]
        self.assertEqual(post.relationship, "quote")
        self.assertEqual(post.quoted_post["author_handle"], "someone_else")
        self.assertEqual(post.quoted_post["text"], "Quoted words")

    def test_a_long_post_keeps_its_whole_text(self):
        read = read_timeline([serve.timeline_response([{"type": "TimelineAddEntries",
                                                        "entries": [serve.entry(self.fixture(serve.LONG))]}])])
        self.assertEqual(read.posts[0].text, "This is the whole of a long post, kept in full.")
        self.assertEqual(read.posts[0].urls[0]["expanded_url"], "https://example.org/long")

    def test_media_is_asked_for_at_orig_and_as_the_best_mp4(self):
        read = read_timeline([serve.timeline_response([{"type": "TimelineAddEntries", "entries": [
            serve.entry(self.fixture(t)) for t in (serve.PHOTO, serve.VIDEO, serve.GIF)]}])])
        by_id = {p.post_id: p for p in read.posts}
        photos = by_id[serve.PHOTO["rest_id"]].media
        self.assertEqual([m.url for m in photos],
                         ["http://pbs.example/media/one?format=jpg&name=orig",
                          "http://pbs.example/media/two?format=jpg&name=orig"])
        self.assertEqual(photos[0].page_url, "http://pbs.example/media/one.jpg")
        self.assertEqual(photos[0].fallback_urls[0], "http://pbs.example/media/one?format=jpg&name=4096x4096")
        self.assertEqual(photos[0].alt_text, "alt one")
        clip = by_id[serve.VIDEO["rest_id"]].media[0]
        self.assertEqual(clip.kind, "video")
        self.assertTrue(clip.url.endswith("1280x720.mp4"))
        self.assertEqual((clip.bitrate, clip.requested_variant), (2176000, "mp4:2176000"))
        loop = by_id[serve.GIF["rest_id"]].media[0]
        self.assertEqual((loop.kind, loop.url.endswith("loop.mp4")), ("gif", True))

    def test_a_tombstone_is_an_absence_not_a_post(self):
        read = read_timeline([{"data": {"threaded_conversation_with_injections_v2": {"instructions": [
            {"type": "TimelineAddEntries", "entries": [serve.tombstone("Gone")]}]}}}])
        self.assertEqual(read.posts, [])
        self.assertEqual(read.absences[0].reason, "Gone")
        self.assertEqual(read.absences[0].kind, "tombstone")

    def test_a_thread_module_yields_its_members_with_the_module_named(self):
        read = read_timeline([serve.timeline_response([{"type": "TimelineAddEntries", "entries": [
            serve.module("profile-conversation-70", [self.fixture(serve.READER_POST),
                                                     self.fixture(serve.REPLY_BY_ACCOUNT)])]}])])
        self.assertEqual([p.author_handle for p in read.posts], ["someone_else", "qnl"])
        self.assertEqual(read.posts[1].relationship, "reply")
        self.assertEqual(read.posts[1].provenance["module"], "profile-conversation-70")

    def test_the_user_is_read_from_a_lookup(self):
        read = read_timeline([{"data": {"user": {"result": self.fixture(serve.USER)}}}])
        self.assertEqual(read.users[0].user_id, "100")
        self.assertEqual(read.users[0].pinned_post_ids, [serve.PINNED["rest_id"]])
        self.assertEqual(read.users[0].followers_count, 1200)
        self.assertIsNone(user_from_result({"__typename": "UserUnavailable"}))

    def test_times_are_read_from_created_at_and_from_the_id(self):
        self.assertEqual(x_time_to_iso("Wed Oct 10 20:19:24 +0000 2018"), "2018-10-10T20:19:24Z")
        self.assertIsNone(x_time_to_iso("nonsense"))
        self.assertEqual(snowflake_time("1050118621198921728"), "2018-10-10T20:19:24Z")

    def test_the_original_image_request_names_the_format(self):
        url, fallbacks = original_image_url("https://pbs.twimg.com/media/AbC.png")
        self.assertEqual(url, "https://pbs.twimg.com/media/AbC?format=png&name=orig")
        self.assertEqual(fallbacks[-1], "https://pbs.twimg.com/media/AbC.png")


# ---------------------------------------------------------------------------
# A stand-in for X
# ---------------------------------------------------------------------------

def _post(n: int, *, author: str = "qnl", author_id: str = "100", relationship="original",
          pinned=False, media: Optional[list] = None, reply_to: Optional[str] = None,
          conversation: Optional[str] = None, reply_count: int = 0, **extra) -> XPost:
    post_id = str(1000000000000000000 + (1000 - n))     # newer posts, larger ids
    from datetime import datetime, timedelta, timezone
    when = (datetime(2026, 3, 1, tzinfo=timezone.utc) - timedelta(days=n)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return XPost(post_id=post_id, author_id=author_id, author_handle=author,
                 author_name=author.title(), relationship=relationship, text=f"Post {n}",
                 created_time=when, conversation_id=conversation or post_id,
                 in_reply_to_post_id=reply_to,
                 permalink_url=f"https://x.com/{author}/status/{post_id}",
                 reply_count=reply_count, media=media or [], is_pinned=pinned,
                 raw={"rest_id": post_id}, provenance={"operation": "UserTweets",
                                                       "response": "raw/responses/response-000001.json"},
                 **extra)


def ids(*numbers: int) -> list[str]:
    return [_post(n).post_id for n in numbers]


class _Listing:
    def __init__(self, fake, items):
        self.fake, self.items, self.index = fake, items, 0
        self.closed = False

    def close(self):
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.items):
            raise StopIteration
        self.fake.calls += 1
        if self.fake.limit_at is not None and self.fake.calls == self.fake.limit_at:
            self.fake.limit_at = None
            raise RateLimited(5.0, "429")
        item = self.items[self.index]
        self.index += 1
        return item


class FakeX:
    version = "fake"

    def __init__(self, *, signed_in: Optional[str] = "424242"):
        self.signed_in = signed_in
        self.users: dict[str, XUser] = {}
        self.timelines: dict[tuple[str, str], list[XPost]] = {}
        self.conversations: dict[str, list[XPost]] = {}
        self.searches: dict[str, list[XPost]] = {}
        self.posts: dict[str, XPost] = {}
        self.media: dict[str, bytes] = {}
        self.calls = 0
        self.limit_at: Optional[int] = None
        self.fetched: list[str] = []
        self.last_fetch_via = "fake"
        self.operations_observed = {"UserTweets": 3, "HomeTimeline": 1}

    def add_user(self, handle: str, user_id: str, pinned: Optional[str] = None) -> XUser:
        user = XUser(user_id=user_id, handle=handle, name=handle.title(),
                     pinned_post_ids=[pinned] if pinned else [], raw={"rest_id": user_id})
        self.users[handle.lower()] = user
        return user

    def viewer(self):
        return self.signed_in

    def user(self, handle: str) -> XUser:
        user = self.users.get(handle.lower())
        if user is None:
            from webarc.x import TargetUnavailable
            raise TargetUnavailable("no such account")
        return user

    def timeline(self, user: XUser, surface: str) -> Iterator[XPost]:
        return _Listing(self, list(self.timelines.get((user.handle.lower(), surface), [])))

    def post(self, post_id: str) -> XPost:
        return self.posts[post_id]

    def conversation(self, post_id: str) -> Iterator[XPost]:
        return _Listing(self, list(self.conversations.get(post_id, [])))

    def conversation_more(self):
        return None

    def search(self, query: str, product: str) -> Iterator[XPost]:
        return _Listing(self, list(self.searches.get(query, [])))

    def fetch(self, url: str):
        self.fetched.append(url)
        if url not in self.media:
            from webarc.x import TargetUnavailable
            raise TargetUnavailable("HTTP 404")
        return self.media[url], "image/jpeg" if url.endswith("orig") else "video/mp4"


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._out = tempfile.TemporaryDirectory()
        self.addCleanup(self._out.cleanup)
        self.out = Path(self._out.name)
        self.fake = FakeX()
        self.fake.add_user("qnl", "100", pinned=_post(50).post_id)
        self.pinned = _post(50, pinned=True)
        self.timeline = [self.pinned] + [_post(n) for n in range(1, 9)]
        self.fake.timelines[("qnl", "posts")] = self.timeline
        self.persisted: dict = {}
        self.progress: list = []

    def run_session(self, **raw) -> XCaptureSession:
        config = XCaptureConfig.from_dict({"targets": ["qnl"], "mode": "latest_n",
                                           "latest_n": 3, **raw})
        session = XCaptureSession(
            config=config, client=self.fake, output_dir=self.out, crawl_id=7,
            crawl_name="t", sleep=lambda s: None,
            persist=lambda **kw: self.persisted.update(kw),
            on_progress=lambda **kw: self.progress.append(kw))
        session.run()
        return session

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in
                (self.out / "x-posts.jsonl").read_text(encoding="utf-8").splitlines()]

    def manifest(self) -> dict:
        return json.loads((self.out / "x-manifest.json").read_text(encoding="utf-8"))


class EngineTests(EngineTestCase):
    def test_latest_n_keeps_the_pinned_post_without_counting_it(self):
        self.run_session(latest_n=3)

        rows = self.rows()
        self.assertEqual([r["post_id"] for r in rows], ids(50, 1, 2, 3))
        self.assertTrue(rows[0]["is_pinned"])
        self.assertEqual(self.manifest()["counts"]["posts_exported"], 4)
        self.assertEqual(self.manifest()["capture"]["viewer"], "signed_in")

    def test_the_package_is_written_with_fixity_and_provenance(self):
        self.run_session()

        for name in ("x-posts.jsonl", "x-posts.csv", "x-users.json", "x-media.json",
                     "x-manifest.json", "x-checkpoint.json", "x-events.jsonl",
                     "checksums.sha256"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertTrue((self.out / "raw" / "posts").is_dir())
        self.assertEqual(self.rows()[1]["provenance"]["target_key"], "x:@qnl")
        self.assertEqual(self.manifest()["operations_observed"]["HomeTimeline"], 1)
        users = json.loads((self.out / "x-users.json").read_text())
        self.assertEqual(users["100"]["handle"], "qnl")

    def test_a_post_by_someone_else_in_the_listing_is_not_the_accounts(self):
        self.fake.timelines[("qnl", "posts")] = [
            _post(1), _post(2, author="someone_else", author_id="777"), _post(3)]

        self.run_session(latest_n=10)

        self.assertEqual([r["author_handle"] for r in self.rows()], ["qnl", "qnl"])
        self.assertEqual(self.manifest()["counts"]["foreign_posts_skipped"], 1)

    def test_a_repost_is_kept_with_the_original_and_never_as_the_accounts_words(self):
        original = {"post_id": "9", "author_id": "777", "author_handle": "someone_else",
                    "text": "theirs", "media": [{"url": "https://pbs/x?format=jpg&name=orig",
                                                 "kind": "image", "position": 0}]}
        self.fake.media["https://pbs/x?format=jpg&name=orig"] = b"jpeg"
        self.fake.timelines[("qnl", "posts")] = [
            _post(1, relationship="repost", original_post=original), _post(2)]

        self.run_session(latest_n=5)

        row = self.rows()[0]
        self.assertEqual(row["relationship"], "repost")
        self.assertEqual(row["author_handle"], "qnl")
        self.assertEqual(row["original_author_handle"], "someone_else")
        self.assertEqual(row["original_post"]["media"][0]["file"][-4:], ".jpg")
        counts = self.manifest()["counts"]
        self.assertEqual((counts["reposts"], counts["authored_posts"]), (1, 1))

    def test_reposts_can_be_left_out(self):
        self.fake.timelines[("qnl", "posts")] = [
            _post(1, relationship="repost", original_post={"post_id": "9"}), _post(2)]

        self.run_session(latest_n=5, keep_reposts=False)

        self.assertEqual([r["post_id"] for r in self.rows()], ids(2))
        self.assertEqual(self.manifest()["layers"]["normalised"]["selection_exclusions"],
                         {"reposts_not_requested": 1})

    def test_media_is_fetched_at_the_requested_rendition_with_fallbacks(self):
        wanted = XMediaItem(url="https://pbs/a?format=jpg&name=orig", kind="image",
                            page_url="https://pbs/a.jpg", requested_variant="orig",
                            fallback_urls=["https://pbs/a?format=jpg&name=large"])
        refused = XMediaItem(url="https://pbs/b?format=jpg&name=orig", kind="image",
                             page_url="https://pbs/b.jpg", requested_variant="orig",
                             fallback_urls=["https://pbs/b?format=jpg&name=large"])
        self.fake.media["https://pbs/a?format=jpg&name=orig"] = b"a-orig"
        self.fake.media["https://pbs/b?format=jpg&name=large"] = b"b-large"
        self.fake.timelines[("qnl", "posts")] = [_post(1, media=[wanted, refused])]

        self.run_session(latest_n=5)

        index = json.loads((self.out / "x-media.json").read_text())
        first = index["https://pbs/a?format=jpg&name=orig"]
        self.assertEqual((first["page_loaded_variant"], first["requested_variant"],
                          first["fetch_initiator"]), ("https://pbs/a.jpg", "orig", "swm"))
        second = index["https://pbs/b?format=jpg&name=orig"]
        self.assertEqual(second["fetched_url"], "https://pbs/b?format=jpg&name=large")
        self.assertIn("refused", second["requested_variant"])
        self.assertEqual(self.rows()[0]["media_files"], [first["file"], second["file"]])
        events = [json.loads(l)["event"] for l in (self.out / "x-events.jsonl").read_text().splitlines()]
        self.assertIn("media_fallback", events)

    def test_a_conversation_keeps_replies_as_context_and_grades_them(self):
        focal = _post(1, reply_count=2)
        self.fake.timelines[("qnl", "posts")] = [focal]
        self.fake.conversations[focal.post_id] = [
            _post(60, author="someone_else", author_id="777", reply_to=focal.post_id,
                  conversation=focal.post_id),
            _post(61, author="reader", author_id="8", reply_to=focal.post_id,
                  conversation=focal.post_id)]

        self.run_session(latest_n=5, include_conversation=True, max_replies_per_post=10)

        rows = self.rows()
        self.assertEqual([r["capture_role"] for r in rows],
                         ["target", "conversation_context", "conversation_context"])
        self.assertEqual(rows[0]["reply_capture"]["status"], "reported_count_reached")
        self.assertEqual(self.manifest()["counts"]["posts_exported"], 1)
        self.assertEqual(self.manifest()["counts"]["context_posts"], 2)

    def test_the_reply_cap_grades_the_conversation_as_capped(self):
        focal = _post(1, reply_count=3)
        self.fake.timelines[("qnl", "posts")] = [focal]
        self.fake.conversations[focal.post_id] = [
            _post(60 + i, author="r", author_id="8", reply_to=focal.post_id,
                  conversation=focal.post_id) for i in range(3)]

        self.run_session(latest_n=5, include_conversation=True, max_replies_per_post=2)

        self.assertEqual(self.rows()[0]["reply_capture"]["status"], "capped")
        self.assertEqual(len(self.rows()), 3)

    def test_since_last_stops_past_the_previous_newest_post(self):
        prior = {"x:@qnl": {"post_id": _post(4).post_id, "date": _post(4).created_time}}

        session = self.run_session(mode="since_last", prior_newest=prior,
                                   consecutive_older=2)

        self.assertEqual([r["post_id"] for r in self.rows()], ids(50, 1, 2, 3))
        self.assertEqual(session.exclusions["captured_previously"], 2)
        self.assertEqual(self.persisted["targets"]["x:@qnl"]["post_id"], _post(1).post_id)

    def test_a_date_range_passes_newer_posts_and_stops_after_older_ones(self):
        self.run_session(mode="date_range", from_date="2026-02-24", to_date="2026-02-27",
                         consecutive_older=2)

        self.assertEqual([r["post_id"] for r in self.rows()], ids(2, 3, 4, 5))

    def test_a_rate_limit_is_waited_out_and_the_listing_resumed(self):
        self.fake.limit_at = 3

        session = self.run_session(latest_n=5)

        self.assertEqual(len(self.rows()), 6)
        self.assertEqual(session.counters["rate_limit_waits"], 1)

    def test_a_signed_out_browser_holds_for_the_curator_then_continues(self):
        self.fake.signed_in = None
        opened: list[str] = []
        answers = iter(["resume", "resume", "resume"])
        config = XCaptureConfig.from_dict({"targets": ["qnl"], "latest_n": 2})
        session = XCaptureSession(
            config=config, client=self.fake, output_dir=self.out, crawl_id=1,
            crawl_name="t", sleep=lambda s: None,
            control_poll=lambda: next(answers, None),
            open_browser=lambda url: opened.append(url) or (lambda: None))

        session.run()

        self.assertEqual(opened[0], "https://x.com/i/flow/login")
        self.assertEqual(self.manifest()["capture"]["viewer"], "signed_out")
        self.assertIsNotNone(self.manifest()["completeness"]["signed_out_capture_meaning"])

    def test_a_search_keeps_every_authors_posts_and_says_what_it_is(self):
        self.fake.searches["#books"] = [_post(30, author="someone_else", author_id="777"),
                                        _post(31)]
        config = XCaptureConfig.from_dict({"targets": ["#books"], "mode": "latest_n",
                                           "latest_n": 5})
        XCaptureSession(config=config, client=self.fake, output_dir=self.out, crawl_id=1,
                        crawl_name="t", sleep=lambda s: None).run()

        self.assertEqual([r["author_handle"] for r in self.rows()], ["someone_else", "qnl"])
        self.assertEqual(self.rows()[0]["surface"], "search")
        self.assertIn("what X served", self.manifest()["completeness"]["search_meaning"])
        self.assertEqual(self.manifest()["capture"]["targets"][0]["kind"], "search")

    def test_a_single_post_target_collects_the_post_and_its_conversation(self):
        focal = _post(1, reply_count=1)
        self.fake.posts[focal.post_id] = focal
        self.fake.conversations[focal.post_id] = [
            _post(60, author="someone_else", author_id="777", reply_to=focal.post_id,
                  conversation=focal.post_id)]
        config = XCaptureConfig.from_dict({"targets": [focal.permalink_url]})
        XCaptureSession(config=config, client=self.fake, output_dir=self.out, crawl_id=1,
                        crawl_name="t", sleep=lambda s: None).run()

        rows = self.rows()
        self.assertEqual(rows[0]["surface"], "direct")
        self.assertEqual(rows[1]["capture_role"], "conversation_context")
        self.assertEqual(rows[0]["reply_capture"]["status"], "reported_count_reached")

    def test_unreferenced_responses_are_pruned_at_the_end(self):
        archive = XArchive(self.out)
        kept = archive.save_response({"url": "https://x.com/i/api/graphql/a/UserTweets"}, b'{"a":1}')
        archive.save_response({"url": "https://x.com/i/api/graphql/b/HomeTimeline"}, b'{"b":2}')
        post = _post(1)
        post.provenance = {"response": kept}
        archive.add_post(post)

        removed = archive.prune_unreferenced_responses()

        self.assertEqual(removed, 1)
        self.assertTrue((self.out / kept).exists())


class RenderTests(EngineTestCase):
    def test_pages_show_a_repost_as_republished_not_authored(self):
        original = {"post_id": "9", "author_id": "777", "author_handle": "someone_else",
                    "author_name": "Someone", "text": "theirs", "media": []}
        self.fake.timelines[("qnl", "posts")] = [
            _post(1, relationship="repost", original_post=original), _post(2)]
        self.run_session(latest_n=5)
        from webarc.x_render import build_site, is_x_capture

        self.assertTrue(is_x_capture(self.out))
        site = build_site(self.out)

        index = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("Reposted a post by @someone_else", index)
        self.assertIn("did not write it", index)
        self.assertIn("They are not X", index)
        page = (site / "posts" / f"{_post(2).post_id}.html").read_text(encoding="utf-8")
        self.assertIn("Post 2", page)
        self.assertIn("No replies were captured", page)


class ApiTests(unittest.TestCase):
    """The X job endpoints, on the server with simulated workers."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from webarc import server as srv
        self.srv = srv
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"))
        self.client = TestClient(self.app)

    def create(self, **payload):
        body = {"targets": ["qnl", "https://x.com/qnl/status/1234567890"],
                "mode": "latest_n", "latest_n": 5, **payload}
        return self.client.post("/api/x", json=body)

    def row(self, crawl_id):
        return self.client.get(f"/api/crawls/{crawl_id}").json()

    def test_a_job_is_created_with_one_seed_per_target(self):
        response = self.create()

        self.assertEqual(response.status_code, 201, response.text)
        made = response.json()
        self.assertEqual(made["kind"], "x")
        self.assertEqual(made["seeds_total"], 2)
        self.assertEqual(made["name"], "x-qnl-post-1234567890")
        stored = json.loads(self.srv._store().get_crawl(made["id"])["config_json"])
        self.assertTrue(stored["x"]["browser_profile_dir"].endswith(
            str(Path("browser-profiles") / "x")))
        self.assertEqual(stored["seeds"][0]["url"], "https://x.com/qnl")

    def test_a_bad_target_is_refused_with_the_reason(self):
        response = self.create(targets=["https://x.com/home"])

        self.assertEqual(response.status_code, 400)
        self.assertIn("own pages", response.text)

    def test_since_last_without_prior_state_is_refused_and_the_state_endpoint_reports(self):
        self.assertEqual(self.create(mode="since_last", targets=["qnl"]).status_code, 400)
        self.assertFalse(self.client.get("/api/x/state", params={"target": "qnl"}).json()["available"])

        self.srv._store().record_x_capture(3, {"x:@qnl": {
            "post_id": "1000000000000000001", "date": "2026-03-01T00:00:00Z",
            "handle": "qnl", "user_id": "100", "url": "https://x.com/qnl"}}, [
            {"post_id": "1000000000000000001", "target_key": "x:@qnl",
             "capture_role": "target", "created_time": "2026-03-01T00:00:00Z"}])

        state = self.client.get("/api/x/state", params={"target": "@QNL"}).json()
        self.assertTrue(state["available"])
        self.assertEqual(state["state"]["newest_post_id"], "1000000000000000001")
        self.assertEqual(self.srv._store().get_x_post_ids("x:@qnl"), {"1000000000000000001"})
        self.assertEqual(self.create(mode="since_last", targets=["qnl"]).status_code, 201)

    def test_the_capability_is_reported_and_metadata_is_described(self):
        capabilities = self.client.get("/api/capabilities").json()
        self.assertIn("available", capabilities["x"])
        made = self.create(metadata={"job": {"Title": "QNL on X"}}).json()
        seen = self.client.get(f"/api/crawls/{made['id']}/metadata").json()
        effective = {f["name"]: f["value"] for f in seen["seeds"][0]["effective"]}
        self.assertEqual(effective["Title"], "QNL on X")
        self.assertEqual(effective["Type"], "Social media account")

    def test_replay_offers_the_pages_of_a_package(self):
        made = self.create().json()
        crawl_dir = Path(self.row(made["id"])["output_dir"])
        self.srv._store().set_pid(made["id"], None)
        (crawl_dir / "x-posts.jsonl").write_text(json.dumps({
            "post_id": "1234567890", "author_handle": "qnl", "relationship": "original",
            "capture_role": "target", "created_time": "2026-03-01T00:00:00Z",
            "text": "hello there", "media_urls": [], "media_files": []}) + "\n")
        (crawl_dir / "x-manifest.json").write_text(json.dumps({
            "capture": {"targets": [{"url": "https://x.com/qnl", "label": "@qnl"}],
                        "mode": "latest_n"}}))

        response = self.client.post(f"/api/crawls/{made['id']}/replay")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "capture_pages")
        self.assertIn("hello there", self.client.get(
            f"/captures/{made['id']}/pages/posts/1234567890.html").text)


if __name__ == "__main__":
    unittest.main()
