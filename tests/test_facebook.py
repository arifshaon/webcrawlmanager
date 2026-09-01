"""Regressions for curator-controlled Facebook Page capture.

Facebook streams one post across several GraphQL fragments, so the same
post_id is observed repeatedly and a later fragment can supply the timestamp,
the pinned flag or the timeline context an earlier one lacked. These tests pin
down the consequences of that: selection has to be re-assessed as records are
enriched, counters must not double-count, and a post whose date has not
arrived must not be treated as evidence about where the timeline has reached.

Everything here runs without a browser or any Facebook access.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from webarc.config import BrowserConfig
from webarc.facebook import (BLOCKED, PAUSED, RECORDING, STOPPED,
                             FacebookCaptureConfig, FacebookCaptureSession,
                             FacebookComment, FacebookPost,
                             canonical_facebook_page_url,
                             decode_graphql_documents, extract_graphql_records,
                             _page_path_segment, _redact_post_data)

PAGE = "https://www.facebook.com/qatarnationallibrary"


class DummyWarc:
    """Stands in for WarcSession; the capture logic never inspects it."""

    def __init__(self):
        self.writes: list[dict] = []
        self.total_bytes = 0

    def write_exchange(self, **kwargs):
        self.writes.append(kwargs)

    def close(self):
        pass


def make_session(tmp: Path, **overrides) -> FacebookCaptureSession:
    raw = {"page_url": PAGE, "mode": "date_range", "from_date": "2026-01-01"}
    raw.update(overrides)
    return FacebookCaptureSession(
        config=FacebookCaptureConfig.from_dict(raw),
        browser_cfg=BrowserConfig(mode="headed"),
        warc=DummyWarc(),
        output_dir=tmp,
        crawl_id=1,
        crawl_name="fb-test",
        operator="tester",
    )


def post(post_id: str, *, date: str | None = None, pinned: bool = False,
         timeline: bool = True) -> FacebookPost:
    return FacebookPost(post_id=post_id, created_time=date, is_pinned=pinned,
                        timeline_item=timeline)


class _FakeResponse:
    """Minimal stand-in for a Playwright response object."""

    url = "https://www.facebook.com/api/graphql/"
    status = 200
    status_text = "OK"
    headers = {"content-type": "application/json"}

    class request:
        method = "POST"
        headers: dict = {}
        post_data_buffer = None
        resource_type = "xhr"


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class LateArrivingFieldTests(SessionTestCase):
    """A post must not be judged forever on its first, partial fragment."""

    def test_post_is_exported_once_its_date_arrives(self):
        session = make_session(self.tmp)
        session._consider_post(post("100"))                      # no date yet
        self.assertEqual(len(session.archive.posts), 0)
        self.assertEqual(session.exclusions.get("date_unavailable"), 1)

        session._consider_post(post("100", date="2026-05-01T09:00:00Z"))

        self.assertIn("100", session.archive.posts)
        self.assertEqual(session.counters["posts_exported"], 1)
        # the earlier exclusion must be withdrawn, not left standing
        self.assertNotIn("date_unavailable", session.exclusions)

    def test_post_is_exported_once_it_is_seen_in_the_timeline(self):
        session = make_session(self.tmp)
        session._consider_post(
            post("101", date="2026-05-01T09:00:00Z", timeline=False))
        self.assertEqual(len(session.archive.posts), 0)
        self.assertEqual(session.counters["non_timeline_post_candidates"], 1)

        session._consider_post(post("101", date="2026-05-01T09:00:00Z"))

        self.assertIn("101", session.archive.posts)
        self.assertEqual(session.counters["posts_observed"], 1)
        # reclassified, so it must no longer be counted as a non-timeline item
        self.assertEqual(session.counters["non_timeline_post_candidates"], 0)

    def test_repeat_observations_do_not_inflate_counters(self):
        session = make_session(self.tmp)
        for _ in range(4):
            session._consider_post(post("102", date="2026-05-01T09:00:00Z"))

        self.assertEqual(session.counters["posts_observed"], 1)
        self.assertEqual(session.counters["posts_exported"], 1)
        self.assertEqual(session.counters["duplicate_post_observations"], 3)
        self.assertEqual(len(session.archive.posts), 1)

    def test_out_of_range_post_stays_excluded_across_observations(self):
        session = make_session(self.tmp)
        for _ in range(3):
            session._consider_post(post("103", date="2025-06-01T09:00:00Z"))

        self.assertEqual(len(session.archive.posts), 0)
        self.assertEqual(session.exclusions["older_than_from"], 1)


class StoppingBoundaryTests(SessionTestCase):
    def test_undated_posts_neither_advance_nor_reset_the_counter(self):
        session = make_session(self.tmp, consecutive_older=3)
        session._consider_post(post("1", date="2025-12-31T09:00:00Z"))
        session._consider_post(post("2", date="2025-12-30T09:00:00Z"))
        session._consider_post(post("3"))            # no date: says nothing
        self.assertIsNone(session._pending_stop)
        self.assertEqual(session._old_consecutive, 2)

        session._consider_post(post("4", date="2025-12-29T09:00:00Z"))

        self.assertIsNotNone(session._pending_stop)
        self.assertEqual(session._pending_stop[0], "date_range_boundary_reached")

    def test_a_newer_post_resets_the_counter(self):
        session = make_session(self.tmp, consecutive_older=3)
        session._consider_post(post("1", date="2025-12-31T09:00:00Z"))
        session._consider_post(post("2", date="2025-12-30T09:00:00Z"))
        session._consider_post(post("3", date="2026-03-01T09:00:00Z"))
        session._consider_post(post("4", date="2025-12-29T09:00:00Z"))

        self.assertIsNone(session._pending_stop)
        self.assertEqual(session._old_consecutive, 1)

    def test_pinned_posts_do_not_advance_the_boundary(self):
        session = make_session(self.tmp, consecutive_older=2)
        session._consider_post(post("p1", date="2019-01-01T09:00:00Z",
                                    pinned=True))
        session._consider_post(post("p2", date="2019-01-02T09:00:00Z",
                                    pinned=True))

        self.assertIsNone(session._pending_stop)
        self.assertEqual(session._old_consecutive, 0)

    def test_boundary_applies_once_per_post(self):
        session = make_session(self.tmp, consecutive_older=3)
        for _ in range(5):
            session._consider_post(post("1", date="2025-12-31T09:00:00Z"))

        self.assertEqual(session._old_consecutive, 1)
        self.assertIsNone(session._pending_stop)


class LatestNTests(SessionTestCase):
    def test_slot_is_not_consumed_twice_by_a_repeat_observation(self):
        session = make_session(self.tmp, mode="latest_n", latest_n=2,
                               from_date=None)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session._consider_post(post("2", date="2026-04-01T09:00:00Z"))

        self.assertEqual(len(session.archive.posts), 2)
        self.assertIsNotNone(session._pending_stop)
        self.assertEqual(session._pending_stop[0], "latest_n_reached")

    def test_posts_beyond_the_limit_are_excluded(self):
        session = make_session(self.tmp, mode="latest_n", latest_n=1,
                               from_date=None)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session._consider_post(post("2", date="2026-04-01T09:00:00Z"))

        self.assertEqual(list(session.archive.posts), ["1"])
        self.assertEqual(session.exclusions["beyond_latest_n"], 1)

    def test_pinned_posts_do_not_consume_a_slot(self):
        session = make_session(self.tmp, mode="latest_n", latest_n=1,
                               from_date=None)
        session._consider_post(post("pin", date="2019-01-01T09:00:00Z",
                                    pinned=True))
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))

        self.assertEqual(sorted(session.archive.posts), ["1", "pin"])


class CoverageTests(SessionTestCase):
    def test_exported_bounds_exclude_posts_scrolled_past(self):
        session = make_session(self.tmp)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session._consider_post(post("2", date="2025-06-01T09:00:00Z"))  # older

        coverage = session._coverage()

        self.assertEqual(coverage["exported_oldest_post"],
                         "2026-05-01T09:00:00Z")
        self.assertEqual(coverage["observed_oldest_post"],
                         "2025-06-01T09:00:00Z")

    def test_range_is_only_satisfied_when_the_stopping_rule_fired(self):
        session = make_session(self.tmp)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        self.assertFalse(session._coverage()["requested_range_satisfied"])

        session.stop_reason = "date_range_boundary_reached"
        self.assertTrue(session._coverage()["requested_range_satisfied"])

    def test_curator_stopped_run_does_not_claim_the_range(self):
        session = make_session(self.tmp)
        session.stop_reason = "curator_stopped"
        self.assertFalse(session._coverage()["requested_range_satisfied"])

    def test_open_ended_modes_make_no_range_claim(self):
        session = make_session(self.tmp, mode="until_stopped", from_date=None)
        self.assertIsNone(session._coverage()["requested_range_satisfied"])

    def test_manifest_carries_coverage(self):
        session = make_session(self.tmp)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        manifest = session._manifest_document(final=True)

        self.assertIn("coverage", manifest)
        self.assertEqual(manifest["coverage"]["exported_newest_post"],
                         "2026-05-01T09:00:00Z")
        self.assertNotIn("detected_gaps", manifest["counts"])


class TargetIdentificationTests(unittest.TestCase):
    """Which entity a response describes.

    A logged-in capture carries the curator's own account through many
    Facebook responses. Treating any User node in any response as the capture
    target aborted real Page captures, so identification is tied to the vanity
    segment of the requested URL.
    """

    SEGMENT = "qatarnationallibrary"

    def test_viewers_own_account_is_not_the_target(self):
        viewer = {"data": {"profile": {
            "__typename": "User",
            "name": "Arif Shaon",
            "url": "https://www.facebook.com/arif.shaon",
            "timeline_nav_app_sections": {"nodes": [{"id": "1"}]},
        }}}

        _posts, _comments, target_type, _name = extract_graphql_records(
            [viewer], self.SEGMENT)

        self.assertIsNone(target_type)

    def test_an_unidentifiable_user_node_is_not_the_target(self):
        anonymous = {"data": {"node": {
            "__typename": "User",
            "feed_units": {"edges": []},
        }}}

        _posts, _comments, target_type, _name = extract_graphql_records(
            [anonymous], self.SEGMENT)

        self.assertIsNone(target_type)

    def test_requested_page_is_identified(self):
        page = {"data": {"node": {
            "__typename": "Page",
            "name": "Qatar National Library",
            "url": "https://www.facebook.com/qatarnationallibrary",
        }}}

        _posts, _comments, target_type, name = extract_graphql_records(
            [page], self.SEGMENT)

        self.assertEqual(target_type, "Page")
        self.assertEqual(name, "Qatar National Library")

    def test_a_user_claiming_the_requested_url_is_the_target(self):
        profile = {"data": {"profile": {
            "__typename": "User",
            "name": "Someone",
            "url": "https://www.facebook.com/qatarnationallibrary",
        }}}

        _posts, _comments, target_type, _name = extract_graphql_records(
            [profile], self.SEGMENT)

        self.assertEqual(target_type, "User")

    def test_a_user_identified_by_vanity_field_is_the_target(self):
        profile = {"data": {"profile": {
            "__typename": "User", "vanity": "qatarnationallibrary"}}}

        _posts, _comments, target_type, _name = extract_graphql_records(
            [profile], self.SEGMENT)

        self.assertEqual(target_type, "User")

    def test_page_segment_is_taken_from_the_url(self):
        self.assertEqual(
            _page_path_segment("https://www.facebook.com/QatarNL"), "qatarnl")
        self.assertEqual(
            _page_path_segment("https://www.facebook.com/QatarNL/photos"),
            "qatarnl")


class SessionTargetTests(SessionTestCase):
    def test_capture_survives_the_viewers_own_account(self):
        session = make_session(self.tmp)
        session._consume_graphql(
            _FakeResponse(),
            b'{"data":{"profile":{"__typename":"User","name":"Curator",'
            b'"url":"https://www.facebook.com/curator.account"}}}')

        self.assertFalse(session._profile_rejected)
        self.assertIsNone(session._pending_stop)

    def test_capture_stops_when_the_requested_url_is_a_profile(self):
        session = make_session(self.tmp,
                               page_url="https://www.facebook.com/someone")
        session._consume_graphql(
            _FakeResponse(),
            b'{"data":{"profile":{"__typename":"User","name":"Someone",'
            b'"url":"https://www.facebook.com/someone"}}}')

        self.assertTrue(session._profile_rejected)
        self.assertEqual(session._pending_stop[0],
                         "unsupported_personal_profile")
        # a rejected target must not also be reported as a crash
        self.assertIsNone(session.failure)


class CommentAttributionTests(SessionTestCase):
    """The per-post comment limit has to be per post.

    When a comment's parent post cannot be identified, the fallback bucket is
    derived from its position in the response. Truncating that path at the
    first edge index put every post in a feed response into one bucket, so a
    single popular post could exhaust the limit for all of them.
    """

    def comment(self, comment_id: str, path: str) -> FacebookComment:
        return FacebookComment(comment_id=comment_id, text="hello",
                               source_path=path)

    def test_comments_on_different_feed_posts_get_their_own_budget(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=2)
        base = "data.node.timeline.feed_units.edges.{post}.node.comments.edges.{n}"
        for post_index in (0, 1):
            for n in range(3):
                session._consider_comment(self.comment(
                    f"c{post_index}-{n}",
                    base.format(post=post_index, n=n)))

        # two kept per post, the third of each refused -- not two in total
        self.assertEqual(session.counters["comments_exported"], 4)
        self.assertEqual(session.exclusions["comment_limit_reached"], 2)

    def test_an_identified_parent_post_is_preferred(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=1)
        first = FacebookComment(comment_id="a", text="x", parent_post_id="900")
        second = FacebookComment(comment_id="b", text="y", parent_post_id="901")
        session._consider_comment(first)
        session._consider_comment(second)

        self.assertEqual(session.counters["comments_exported"], 2)
        self.assertEqual(session.counters["comments_without_parent_post"], 0)

    def test_replies_are_skipped_unless_requested(self):
        session = make_session(self.tmp, include_comments=True)
        session._consider_comment(
            FacebookComment(comment_id="r", text="reply", depth=1))

        self.assertEqual(session.counters["comments_exported"], 0)
        self.assertEqual(session.exclusions["replies_not_requested"], 1)


class ManifestHonestyTests(SessionTestCase):
    def test_manifest_states_the_feed_cannot_be_replayed(self):
        session = make_session(self.tmp)
        raw = session._manifest_document(final=True)["layers"]["raw"]

        self.assertIn("replayability", raw)
        self.assertIn("cannot be re-driven", raw["replayability"])
        self.assertIn("media", raw)


class _FakeFetched:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self.ok = 200 <= status < 300
        self.status_text = "OK"
        self.headers = {"content-type": "image/jpeg"}
        self._body = body
        self.disposed = False

    def body(self):
        return self._body

    def dispose(self):
        self.disposed = True


class _FakeRequestContext:
    def __init__(self, body=b"\xff\xd8jpeg", status=200):
        self.requested: list[str] = []
        self._body = body
        self._status = status

    def get(self, url, **_kwargs):
        self.requested.append(url)
        return _FakeFetched(self._body, self._status)


class _FakeContext:
    def __init__(self, **kwargs):
        self.request = _FakeRequestContext(**kwargs)


class MediaCaptureTests(SessionTestCase):
    """Ticking "capture media" has to actually collect the media.

    Facebook lazy-loads, so images for posts scrolled past quickly are never
    requested by the browser; and the CDN URLs are signed, so they cannot be
    fetched from the exported records later.
    """

    def with_media(self, session, urls):
        item = post("1", date="2026-05-01T09:00:00Z")
        item.media_urls = list(urls)
        session._consider_post(item)
        return item

    def test_media_is_fetched_and_written_to_warc(self):
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext()
        self.with_media(session, ["https://scontent.example/a.jpg"])
        session._process_media_queue()

        self.assertEqual(session._context.request.requested,
                         ["https://scontent.example/a.jpg"])
        self.assertEqual(len(session.warc.writes), 1)
        self.assertEqual(session.warc.writes[0]["body"], b"\xff\xd8jpeg")
        self.assertEqual(session.counters["media_fetched_for_posts"], 1)

    def test_nothing_is_fetched_when_media_was_not_requested(self):
        session = make_session(self.tmp, capture_media=False)
        session._context = _FakeContext()
        self.with_media(session, ["https://scontent.example/a.jpg"])
        session._process_media_queue()

        self.assertEqual(session._context.request.requested, [])
        self.assertEqual(len(session.warc.writes), 0)

    def test_media_already_held_on_disk_is_not_fetched_again(self):
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext()
        session.archive.media_index["https://scontent.example/a.jpg"] = "a.jpg"
        self.with_media(session, ["https://scontent.example/a.jpg"])
        session._process_media_queue()

        self.assertEqual(session._context.request.requested, [])

    def test_media_the_browser_loaded_before_it_was_wanted_is_fetched(self):
        # The browser's own response was discarded, because nothing yet
        # identified the URL as belonging to a captured post.
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext()
        session._media_seen.add("https://scontent.example/a.jpg")
        self.with_media(session, ["https://scontent.example/a.jpg"])
        session._process_media_queue()

        self.assertEqual(session._context.request.requested,
                         ["https://scontent.example/a.jpg"])

    def test_fetched_media_is_written_to_disk(self):
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext()
        self.with_media(session, ["https://scontent.example/a.jpg"])
        session._process_media_queue()

        name = session.archive.media_index["https://scontent.example/a.jpg"]
        self.assertTrue(name.endswith(".jpg"))
        self.assertEqual((session.archive.media_dir / name).read_bytes(),
                         b"\xff\xd8jpeg")

    def test_the_same_image_on_two_posts_is_stored_once(self):
        session = make_session(self.tmp, capture_media=True)
        first = session.archive.save_media("https://a/1.jpg", b"same",
                                           "image/jpeg")
        second = session.archive.save_media("https://b/2.jpg", b"same",
                                            "image/jpeg")

        self.assertEqual(first, second)
        self.assertEqual(
            len(list(session.archive.media_dir.glob("*.jpg"))), 1)

    def test_a_failed_fetch_is_counted_not_raised(self):
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext(status=403)
        self.with_media(session, ["https://scontent.example/gone.jpg"])
        session._process_media_queue()

        self.assertEqual(session.counters["media_fetch_failures"], 1)
        self.assertEqual(len(session.warc.writes), 0)

    def test_each_url_is_queued_once(self):
        session = make_session(self.tmp, capture_media=True)
        session._context = _FakeContext()
        urls = ["https://scontent.example/a.jpg"] * 3
        self.with_media(session, urls)
        session._process_media_queue(budget=10)

        self.assertEqual(len(session._context.request.requested), 1)


class CommentHarvestTests(SessionTestCase):
    def test_comments_found_on_a_permalink_belong_to_that_post(self):
        session = make_session(self.tmp, include_comments=True)
        session._permalink_post_id = "555"
        session._consider_comment(
            FacebookComment(comment_id="c1", text="hello"))

        self.assertEqual(
            session.archive.comments["c1"].parent_post_id, "555")

    def test_a_payload_id_does_not_override_the_page_being_read(self):
        # Facebook labels comments with feedback ids that need not match the
        # post id. Trusting those split one post's comments across several
        # budgets, so the harvest read its own progress as nil and stopped.
        session = make_session(self.tmp, include_comments=True)
        session._permalink_post_id = "555"
        session._consider_comment(FacebookComment(
            comment_id="c1", text="hello", parent_post_id="feedback:999"))

        self.assertEqual(
            session.archive.comments["c1"].parent_post_id, "555")
        self.assertEqual(session._comment_counts["555"], 1)
        self.assertEqual(session._comment_counts["feedback:999"], 0)

    def test_the_whole_budget_is_reachable_on_one_permalink(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=25)
        session._permalink_post_id = "555"
        for n in range(25):
            session._consider_comment(FacebookComment(
                comment_id=f"c{n}", text="hi",
                parent_post_id=f"feedback:{n}"))

        self.assertEqual(session.counters["comments_exported"], 25)
        self.assertNotIn("comment_limit_reached", session.exclusions)

    def test_each_permalink_post_gets_its_own_budget(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=1)
        for post_id in ("555", "556"):
            session._permalink_post_id = post_id
            for n in range(2):
                session._consider_comment(
                    FacebookComment(comment_id=f"{post_id}-{n}", text="hi"))

        self.assertEqual(session.counters["comments_exported"], 2)
        self.assertEqual(session.exclusions["comment_limit_reached"], 2)

    def test_harvest_is_skipped_when_comments_were_not_requested(self):
        session = make_session(self.tmp, include_comments=False)
        session._harvest_comments(_FakeContext())

        self.assertFalse(session._harvest_done)
        self.assertEqual(session.counters["posts_comment_harvested"], 0)

    def test_posts_without_a_permalink_are_reported(self):
        session = make_session(self.tmp, include_comments=True)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))

        class _NoPages:
            def new_page(self):
                raise AssertionError("no post has a permalink to visit")

        session._harvest_comments(_NoPages())

        self.assertEqual(session.counters["posts_without_permalink"], 1)

    def test_manifest_reports_what_was_requested_and_delivered(self):
        session = make_session(self.tmp, include_comments=True,
                               include_replies=True, capture_media=True,
                               max_comments_per_post=25)
        work = session._manifest_document(final=True)["requested_work"]

        self.assertTrue(work["comments"]["requested"])
        self.assertTrue(work["comments"]["replies_requested"])
        self.assertEqual(work["comments"]["maximum_per_post"], 25)
        self.assertTrue(work["media"]["requested"])
        self.assertEqual(work["media"]["outstanding_at_close"], 0)


class _FakePage:
    """A browser page whose URL and visible text the test controls."""

    def __init__(self, url=PAGE, body="Posts"):
        self.url = url
        self._body = body

    def content(self):
        return f"<html><body>{self._body}</body></html>"

    def locator(self, _selector):
        page = self

        class _Locator:
            def inner_text(self, **_kwargs):
                return page._body
        return _Locator()

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_load_state(self, *_a, **_k):
        pass


class AutoStartTests(SessionTestCase):
    """The curator chose a mode and its criteria; collection should follow.

    Pressing a button afterwards adds nothing, so a capture starts by itself
    once the Page is on screen. What genuinely needs a person -- a login wall,
    a verification challenge, the wrong page -- waits and says so.
    """

    def test_collection_starts_once_the_page_is_open(self):
        session = make_session(self.tmp)
        self.assertEqual(session.state, PAUSED)

        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, RECORDING)
        self.assertTrue(session.started_scrolling)

    def test_the_chosen_mode_is_named_for_the_curator(self):
        session = make_session(self.tmp, mode="latest_n", latest_n=100,
                               from_date=None)
        session._maybe_auto_start(_FakePage())

        self.assertIn("latest 100 posts", session.phase_detail)

    def test_a_login_wall_waits_and_explains(self):
        session = make_session(self.tmp)
        session._maybe_auto_start(
            _FakePage(url="https://www.facebook.com/login/?next=x"))

        self.assertEqual(session.state, PAUSED)
        self.assertIn("Sign in", session.phase_detail)

    def test_another_page_being_open_waits_and_explains(self):
        session = make_session(self.tmp)
        session._maybe_auto_start(
            _FakePage(url="https://www.facebook.com/someoneelse"))

        self.assertEqual(session.state, PAUSED)
        self.assertIn("Waiting for", session.phase_detail)

    def test_collection_starts_when_the_curator_opens_the_page(self):
        session = make_session(self.tmp)
        session._maybe_auto_start(_FakePage(url="https://www.facebook.com/"))
        self.assertEqual(session.state, PAUSED)

        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, RECORDING)

    def test_a_pause_the_curator_asked_for_is_not_overridden(self):
        session = make_session(self.tmp)
        session._maybe_auto_start(_FakePage())
        session.apply("pause", actor="dashboard")
        self.assertEqual(session.state, PAUSED)

        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, PAUSED)

    def test_a_facebook_block_resumes_itself_once_resolved(self):
        session = make_session(self.tmp)
        session._maybe_auto_start(_FakePage())
        session._enter_blocked("Facebook is asking for verification.")
        self.assertEqual(session.state, BLOCKED)

        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, RECORDING)

    def test_opting_out_leaves_the_start_to_the_curator(self):
        session = make_session(self.tmp, auto_start=False)
        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, PAUSED)
        self.assertIn("Waiting for you to start", session.phase_detail)

    def test_nothing_starts_once_a_stop_is_pending(self):
        session = make_session(self.tmp)
        session._request_stop("curator_stop", "rule")
        session._maybe_auto_start(_FakePage())

        self.assertEqual(session.state, PAUSED)


class FinishedCaptureTests(SessionTestCase):
    """What a finished capture reports.

    The list keeps showing this line long after the run ends, so a completed
    capture must not describe itself as paused, nor leave behind a note about
    a step that is over.
    """

    def finish(self, session, reason="date_range_boundary_reached"):
        session.stop_reason = reason
        session.state = STOPPED
        session.phase_detail = session._closing_summary()
        return session._progress_details()

    def test_a_finished_capture_is_not_reported_as_paused(self):
        session = make_session(self.tmp)
        session.started_scrolling = True

        details = self.finish(session)

        self.assertEqual(details["phase"], "finished")

    def test_the_summary_says_what_was_collected(self):
        session = make_session(self.tmp, include_comments=True)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session._permalink_post_id = "1"
        session._consider_comment(
            FacebookComment(comment_id="c1", text="hi"))

        details = self.finish(session)

        self.assertIn("1 post", details["message"])
        self.assertIn("1 comment", details["message"])

    def test_the_summary_says_why_it_stopped(self):
        session = make_session(self.tmp)
        details = self.finish(session)
        self.assertIn("requested date range was covered", details["message"])

    def test_a_curator_stop_is_described_plainly(self):
        session = make_session(self.tmp)
        details = self.finish(session, reason="curator_stop")
        self.assertIn("you selected Stop and save", details["message"])

    def test_an_unknown_stop_reason_is_still_readable(self):
        session = make_session(self.tmp)
        details = self.finish(session, reason="some_new_reason")
        self.assertIn("some new reason", details["message"])
        self.assertNotIn("_", details["message"].split("because")[-1])

    def test_a_running_capture_is_still_reported_as_scrolling(self):
        session = make_session(self.tmp)
        session.state = RECORDING
        self.assertEqual(session._progress_details()["phase"], "scrolling")


class GraphQLDecodingTests(unittest.TestCase):
    def test_anti_json_prefix_is_stripped(self):
        self.assertEqual(decode_graphql_documents(b'for (;;);{"a":1}'),
                         [{"a": 1}])

    def test_multiple_streamed_documents_are_returned(self):
        body = b'{"a":1}\n{"b":2}\n{"c":3}'
        self.assertEqual(decode_graphql_documents(body),
                         [{"a": 1}, {"b": 2}, {"c": 3}])

    def test_an_unparseable_line_does_not_lose_the_rest(self):
        body = b'{"a":1}\nnot json at all\n{"b":2}'
        self.assertEqual(decode_graphql_documents(body),
                         [{"a": 1}, {"b": 2}])

    def test_empty_body_is_not_an_error(self):
        self.assertEqual(decode_graphql_documents(b""), [])


class RedactionTests(unittest.TestCase):
    FORM = {"content-type": "application/x-www-form-urlencoded"}

    def test_session_token_is_removed_from_a_form_body(self):
        body, changed = _redact_post_data(
            "https://www.facebook.com/api/graphql/",
            b"fb_dtsg=SECRET&av=1&fb_api_req_friendly_name=Timeline",
            self.FORM)

        self.assertTrue(changed)
        self.assertNotIn(b"SECRET", body)
        # non-secret fields must survive: they are the archival evidence
        self.assertIn(b"fb_api_req_friendly_name=Timeline", body)

    def test_capturing_account_identifier_is_removed(self):
        body, changed = _redact_post_data(
            "https://www.facebook.com/api/graphql/",
            b"__user=1234567890&jazoest=2915&doc_id=99", self.FORM)

        self.assertTrue(changed)
        self.assertNotIn(b"1234567890", body)
        self.assertIn(b"doc_id=99", body)

    def test_login_request_body_is_replaced_wholesale(self):
        body, changed = _redact_post_data(
            "https://www.facebook.com/login/device-based/regular/login/",
            b"\x00\x01binary-credentials", {})

        self.assertTrue(changed)
        self.assertIn(b"REDACTED", body)

    def test_ordinary_graphql_variables_are_preserved(self):
        original = b'{"variables":{"id":"123"},"doc_id":"456"}'
        body, changed = _redact_post_data(
            "https://www.facebook.com/api/graphql/", original,
            {"content-type": "application/json"})

        self.assertFalse(changed)
        self.assertEqual(body, original)


class PageUrlTests(unittest.TestCase):
    def test_page_url_is_normalised(self):
        self.assertEqual(
            canonical_facebook_page_url("http://facebook.com/QatarNL/"),
            "https://www.facebook.com/QatarNL")

    def test_personal_profiles_are_rejected(self):
        for value in ("https://www.facebook.com/profile.php?id=1",
                      "https://www.facebook.com/groups/123",
                      "https://www.facebook.com/people/someone/1"):
            with self.assertRaises(ValueError):
                canonical_facebook_page_url(value)

    def test_non_facebook_and_empty_urls_are_rejected(self):
        for value in ("https://example.org/page", "https://www.facebook.com/",
                      "", None):
            with self.assertRaises(ValueError):
                canonical_facebook_page_url(value)


class ConfigValidationTests(unittest.TestCase):
    def base(self, **over):
        raw = {"page_url": PAGE, "mode": "date_range",
               "from_date": "2026-01-01"}
        raw.update(over)
        return raw

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(self.base(mode="whatever"))

    def test_date_range_requires_a_from_date(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(self.base(from_date=None))

    def test_reversed_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(
                self.base(from_date="2026-08-01", to_date="2026-01-01"))

    def test_non_numeric_inputs_name_the_field(self):
        with self.assertRaises(ValueError) as caught:
            FacebookCaptureConfig.from_dict(
                self.base(max_comments_per_post="lots"))
        self.assertIn("Maximum comments per post", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            FacebookCaptureConfig.from_dict(self.base(end_stall_rounds="soon"))
        self.assertIn("Scroll attempts", str(caught.exception))

    def test_scroll_pauses_must_be_ordered(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(
                self.base(scroll_pause_min=5, scroll_pause_max=1))

    def test_latest_n_must_be_a_bounded_whole_number(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(
                self.base(mode="latest_n", latest_n="many"))
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(
                self.base(mode="latest_n", latest_n=0))

    def test_since_last_requires_a_previous_capture(self):
        with self.assertRaises(ValueError):
            FacebookCaptureConfig.from_dict(self.base(mode="since_last"))

    def test_defaults_survive_blank_optional_inputs(self):
        config = FacebookCaptureConfig.from_dict(
            self.base(max_comments_per_post="", scroll_pause_min=""))
        self.assertEqual(config.max_comments_per_post, 25)
        self.assertEqual(config.scroll_pause_min, 1.5)


if __name__ == "__main__":
    unittest.main()
