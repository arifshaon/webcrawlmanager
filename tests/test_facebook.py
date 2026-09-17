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

import json
import re
import tempfile
import unittest
from pathlib import Path

from webarc.config import BrowserConfig
from webarc.facebook import (BLOCKED, PAUSED, RECORDING, STOPPED,
                             FacebookCaptureConfig, FacebookCaptureSession,
                             FacebookComment, FacebookPost,
                             canonical_facebook_page_url,
                             decode_graphql_documents, extract_graphql_records,
                             extract_embedded_documents,
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

    def test_a_harvest_with_nowhere_to_go_says_so(self):
        session = make_session(self.tmp, include_comments=True)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))

        class _NoPages:
            def new_page(self):
                raise AssertionError("no post has a permalink to visit")

        session._harvest_comments(_NoPages())

        self.assertIn("No comments collected", session.phase_detail)
        self.assertEqual(
            session._progress_details()["posts_without_permalink"], 1)

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


class ScrollProgressTests(SessionTestCase):
    """What the capture reports while it is scrolling.

    Posts arrive in GraphQL responses as Facebook answers each scroll, so the
    response count shows collection is moving even through a stretch where no
    new post qualifies for export.
    """

    def test_the_line_reports_posts_and_api_activity(self):
        session = make_session(self.tmp)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))
        session.counters["graphql_responses"] = 14

        line = session._scrolling_summary()

        self.assertIn("1 posts", line)
        self.assertIn("14 API responses", line)

    def test_progress_towards_a_post_count_is_shown(self):
        session = make_session(self.tmp, mode="latest_n", latest_n=100,
                               from_date=None)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))

        self.assertIn("1 of 100 posts", session._scrolling_summary())

    def test_progress_towards_a_date_is_shown(self):
        session = make_session(self.tmp)
        session._consider_post(post("1", date="2026-05-01T09:00:00Z"))

        line = session._scrolling_summary()
        self.assertIn("collecting back to 2026-01-01", line)
        self.assertIn("reached 01 May 2026", line)

    def test_failures_are_surfaced_not_buried(self):
        session = make_session(self.tmp)
        session.counters["pagination_failures"] = 2

        self.assertIn("2 failed", session._scrolling_summary())

    def test_api_activity_reaches_the_dashboard(self):
        session = make_session(self.tmp)
        session.counters["graphql_responses"] = 9
        session.counters["graphql_errors"] = 1

        details = session._progress_details()

        self.assertEqual(details["graphql_responses"], 9)
        self.assertEqual(details["graphql_errors"], 1)


class FieldSpellingTests(unittest.TestCase):
    """Facebook mixes naming conventions inside one payload.

    wwwURL sits beside creation_time, legacyFBID beside post_id. Matching a
    field only by its exact spelling missed the same field written another
    way -- and because the permalink is what the comment pass navigates to,
    missing wwwURL meant no post had a permalink, every post was skipped, and
    the only comments captured were the one or two Facebook previews in the
    feed.
    """

    SEGMENT = "ucalgaryqatar"
    LINK = "https://www.facebook.com/ucalgaryqatar/posts/12345"

    def story(self, **fields):
        node = {"__typename": "Story", "message": {"text": "A post"}}
        node.update(fields)
        return {"data": {"node": {
            "__typename": "Page", "name": "UCQ",
            "timeline_feed_units": {"edges": [{"node": node}]}}}}

    def only_post(self, document):
        posts, _c, _t, _n = extract_graphql_records([document], self.SEGMENT)
        self.assertEqual(len(posts), 1, "expected exactly one post")
        return posts[0]

    def test_permalink_is_found_however_it_is_spelled(self):
        for key in ("wwwURL", "www_url", "permalink_url", "permalinkURL",
                    "url", "storyURL", "story_url"):
            with self.subTest(key=key):
                found = self.only_post(self.story(
                    post_id="12345", creation_time=1756000000, **{key: self.LINK}))
                self.assertEqual(found.permalink_url, self.LINK)

    def test_identifiers_and_times_are_found_in_camel_case(self):
        found = self.only_post(self.story(
            postID="999", creationTime=1756000000, wwwURL=self.LINK))

        self.assertEqual(found.post_id, "999")
        self.assertTrue(found.created_time)
        self.assertEqual(found.permalink_url, self.LINK)

    def test_comments_are_found_in_camel_case(self):
        edges = [{"node": {"legacyFBID": str(1000 + n),
                           "body": {"text": f"Comment {n}"},
                           "author": {"id": str(900 + n), "name": f"P{n}"},
                           "createdTime": 1756000000 + n, "depth": 0}}
                 for n in range(5)]
        document = {"data": {"node": {"comment_rendering_instance": {
            "comments": {"edges": edges}}}}}

        _posts, comments, _t, _n = extract_graphql_records(
            [document], self.SEGMENT)

        self.assertEqual(len(comments), 5)
        self.assertTrue(all(c.created_time for c in comments))
        self.assertTrue(all(c.author_name for c in comments))

    def test_a_whole_comment_page_is_extracted_not_a_preview(self):
        edges = [{"node": {"legacy_fbid": str(1000 + n),
                           "body": {"text": f"Comment {n}"},
                           "created_time": 1756000000 + n, "depth": 0}}
                 for n in range(25)]
        document = {"data": {"node": {"comment_rendering_instance": {
            "comments": {"edges": edges, "total_count": 42}}}}}

        _posts, comments, _t, _n = extract_graphql_records(
            [document], self.SEGMENT)

        self.assertEqual(len(comments), 25)


POST_URL = ("https://www.facebook.com/Yusuffali.MA/posts/i-am-thankful-to-hh"
            "-sheikh-tamim-bin-hamad/1593564465471991")


class SinglePostTargetTests(SessionTestCase):
    """A post permalink is not a timeline.

    Facebook shows the Page's other posts beneath a permalink, so scrolling
    one as though it were a Page collected those neighbours and their
    comments -- content from a different capture than the one requested.
    """

    def test_a_post_url_is_recognised(self):
        config = FacebookCaptureConfig.from_dict(
            {"page_url": POST_URL, "mode": "date_range",
             "from_date": "2026-01-01"})

        self.assertEqual(config.target_kind, "post")
        self.assertEqual(config.mode, "single_post")
        self.assertEqual(config.target_post_id, "1593564465471991")

    def test_a_page_url_is_still_a_page(self):
        config = FacebookCaptureConfig.from_dict(
            {"page_url": PAGE, "mode": "latest_n", "latest_n": 20})

        self.assertEqual(config.target_kind, "page")
        self.assertEqual(config.mode, "latest_n")

    def test_query_style_post_urls_are_recognised(self):
        for url, expected in (
            ("https://www.facebook.com/photo?fbid=99887766&set=a.123", "99887766"),
            ("https://www.facebook.com/permalink.php?story_fbid=555&id=1", "555"),
            ("https://www.facebook.com/page/videos/778899", "778899"),
        ):
            with self.subTest(url=url):
                config = FacebookCaptureConfig.from_dict({"page_url": url})
                self.assertEqual(config.target_kind, "post")
                self.assertEqual(config.target_post_id, expected)

    def test_only_the_requested_post_is_exported(self):
        session = make_session(self.tmp, page_url=POST_URL)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))
        session._consider_post(post("999", date="2026-04-01T09:00:00Z"))

        self.assertEqual(list(session.archive.posts), ["1593564465471991"])
        self.assertEqual(session.exclusions["not_the_requested_post"], 1)

    def test_a_neighbouring_posts_comments_are_refused(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        session._consider_comment(FacebookComment(
            comment_id="mine", text="on the requested post",
            parent_post_id="1593564465471991"))
        session._consider_comment(FacebookComment(
            comment_id="theirs", text="on another post",
            parent_post_id="999"))

        self.assertEqual(list(session.archive.comments), ["mine"])
        self.assertEqual(session.exclusions["comment_on_another_post"], 1)

    def test_the_capture_reads_the_post_then_stops(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=False)

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertEqual(session._pending_stop[0], "single_post_captured")

    def test_the_post_is_read_only_once(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=False)
        page = _FakePage(url=POST_URL)
        session._capture_single_post(page)
        session._pending_stop = None
        session._capture_single_post(page)

        self.assertIsNone(session._pending_stop)

    def test_the_mode_is_described_for_the_curator(self):
        session = make_session(self.tmp, page_url=POST_URL)
        session._maybe_auto_start(_FakePage(url=POST_URL))

        self.assertIn("this post and its comments", session.phase_detail)


PFBID = "pfbid02oJRyAvFKc9Jt2x98L7wrYAuGCFDgc8Lj9Cv3v9xMgUcXDrwtia5XuXsy6TSJP3"


class PostIdentityTests(SessionTestCase):
    """One post, several names.

    Facebook names a post with a numeric id in payloads and a pfbid in URLs.
    Treated as separate posts, one capture produced two half-empty records for
    the same post -- the text on one, the date and media on the other, the
    permalink on neither -- and the comment pass skipped the record that had
    no permalink to open.
    """

    LINK = f"https://www.facebook.com/Yusuffali.MA/posts/{PFBID}"

    def session(self):
        return make_session(self.tmp, mode="until_stopped", from_date=None,
                            page_url="https://www.facebook.com/Yusuffali.MA")

    def test_fragments_under_different_names_become_one_post(self):
        session = self.session()
        session._consider_post(FacebookPost(
            post_id="1593564465471991", created_time="2026-07-17T15:36:40Z",
            timeline_item=True, media_urls=["https://scontent/photo.jpg"],
            aliases=["1593564465471991"]))
        session._consider_post(FacebookPost(
            post_id=PFBID, permalink_url=self.LINK, timeline_item=True,
            text="I am thankful", source="dom",
            aliases=[PFBID, "1593564465471991"]))

        self.assertEqual(len(session.archive.posts), 1)
        merged = next(iter(session.archive.posts.values()))
        self.assertEqual(merged.text, "I am thankful")
        self.assertEqual(merged.permalink_url, self.LINK)
        self.assertEqual(merged.created_time, "2026-07-17T15:36:40Z")
        self.assertEqual(merged.media_urls, ["https://scontent/photo.jpg"])
        self.assertEqual(session.counters["posts_merged_by_alias"], 1)

    def test_the_merged_post_has_a_permalink_to_harvest(self):
        session = self.session()
        session._consider_post(FacebookPost(
            post_id="1593564465471991", created_time="2026-07-17T15:36:40Z",
            timeline_item=True, aliases=["1593564465471991"]))
        session._consider_post(FacebookPost(
            post_id=PFBID, permalink_url=self.LINK, timeline_item=True,
            text="x", aliases=[PFBID, "1593564465471991"]))

        harvestable = [p for p in session.archive.posts.values()
                       if p.permalink_url]
        self.assertEqual(len(harvestable), 1)

    def test_genuinely_different_posts_stay_separate(self):
        session = self.session()
        session._consider_post(FacebookPost(
            post_id="111", timeline_item=True, text="one", aliases=["111"]))
        session._consider_post(FacebookPost(
            post_id="222", timeline_item=True, text="two", aliases=["222"]))

        self.assertEqual(len(session.archive.posts), 2)
        self.assertEqual(session.counters.get("posts_merged_by_alias", 0), 0)

    def test_aliases_are_read_from_a_permalink(self):
        from webarc.facebook import _post_aliases
        aliases = _post_aliases({"post_id": "1593564465471991"}, self.LINK)

        self.assertIn("1593564465471991", aliases)
        self.assertIn(PFBID, aliases)


class MediaShapeTests(unittest.TestCase):
    """Media has to be found in the shapes Facebook actually serves."""

    def urls(self, obj):
        from webarc.facebook import _media_urls
        return _media_urls(obj)

    def test_a_photo_attachment_is_found(self):
        self.assertEqual(self.urls({"attachments": [{"media": {
            "__typename": "Photo",
            "image": {"uri": "https://scontent/a.jpg"}}}]}),
            ["https://scontent/a.jpg"])

    def test_every_photo_in_an_album_is_found(self):
        found = self.urls({"attachments": [{"subattachments": {"nodes": [
            {"media": {"image": {"uri": "https://scontent/d1.jpg"}}},
            {"media": {"image": {"uri": "https://scontent/d2.jpg"}}}]}}]})
        self.assertEqual(len(found), 2)

    def test_a_video_is_found(self):
        self.assertEqual(self.urls({"attachments": [{"media": {
            "__typename": "Video",
            "playable_url": "https://video/e.mp4"}}]}),
            ["https://video/e.mp4"])


class RenderedPageTests(unittest.TestCase):
    """A permalink serves the post in the page, not over GraphQL.

    Facebook renders a single post -- its text, date, permalink and photos --
    into the initial HTML document, then uses GraphQL only for what comes
    afterwards. A capture that reads GraphQL alone collects the post's
    comments and reports the post itself as empty.
    """

    RENDERED = {'require': [['ScheduledServerJS', 'handle', None, [{'__bbox': {'require': [['RelayPrefetchedStreamCache', 'next', [], ['x', {'__bbox': {'result': {'data': {'node_v2': {'__typename': 'Story', 'post_id': '1593564465471991', 'creation_time': 1784000000, 'wwwURL': 'https://www.facebook.com/page/posts/pfbid02abc', 'message': {'text': 'I am thankful to H.H. Sheikh Tamim'}, 'attachments': [{'media': {'__typename': 'Photo', 'image': {'uri': 'https://scontent/photo.jpg'}}}]}}}}}]]]}}]]]}

    def document(self, payload, wrapper='<script type="application/json" '
                                        'data-sjs>%s</script>'):
        return ("<!DOCTYPE html><html><body>"
                + wrapper % json.dumps(payload)
                + "</body></html>").encode("utf-8")

    def test_embedded_payloads_are_decoded(self):
        found = extract_embedded_documents(self.document({"a": 1}))

        self.assertEqual(found, [{"a": 1}])

    def test_a_page_with_no_embedded_json_yields_nothing(self):
        self.assertEqual(
            extract_embedded_documents(b"<html><body>hello</body></html>"), [])

    def test_unparsable_blocks_are_skipped_not_fatal(self):
        body = (b'<script type="application/json">{"good": 1}</script>'
                b'<script type="application/json">{broken</script>')

        self.assertEqual(extract_embedded_documents(body), [{"good": 1}])

    def test_scripts_that_are_not_json_are_left_alone(self):
        body = b'<script>var x = {"not": "a payload"};</script>'

        self.assertEqual(extract_embedded_documents(body), [])

    def test_the_rendered_post_is_captured_whole(self):
        documents = extract_embedded_documents(self.document(self.RENDERED))
        posts, _, _, _ = extract_graphql_records(documents)

        self.assertEqual(len(posts), 1)
        found = posts[0]
        self.assertEqual(found.post_id, "1593564465471991")
        self.assertIn("thankful", found.text)
        self.assertTrue(found.created_time)
        self.assertTrue(found.permalink_url)
        self.assertEqual(found.media_urls, ["https://scontent/photo.jpg"])

    def test_the_rendered_post_counts_as_a_timeline_post(self):
        """A permalink serves the post at the response root, not in a feed."""
        documents = extract_embedded_documents(self.document(self.RENDERED))
        posts, _, _, _ = extract_graphql_records(documents)

        self.assertTrue(posts[0].timeline_item)

    def test_a_rendered_post_survives_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(Path(tmp), page_url=POST_URL)
            response = _FakeResponse()
            response.url = POST_URL
            response.headers = {"content-type": "text/html"}
            response.request = type("r", (), {"resource_type": "document"})

            session._consume_document(response, self.document(self.RENDERED))

            self.assertEqual(list(session.archive.posts), ["1593564465471991"])
            self.assertEqual(session.counters["non_timeline_post_candidates"], 0)

    def test_the_session_reads_the_page_it_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(Path(tmp))
            response = _FakeResponse()
            response.url = "https://www.facebook.com/page/posts/1593564465471991"
            response.headers = {"content-type": "text/html"}
            response.request = type("r", (), {"resource_type": "document"})

            self.assertTrue(session._is_page_document(response))
            session._consume_document(response, self.document(self.RENDERED))

            self.assertIn("1593564465471991", session.seen_this_run)
            self.assertIn("thankful",
                          session.seen_this_run["1593564465471991"].text)

    def test_xhr_responses_are_not_mined_as_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(Path(tmp))

            self.assertFalse(session._is_page_document(_FakeResponse()))


class CommentShapeTests(unittest.TestCase):
    """Comments Facebook labels twice, and comments with no words at all."""

    PAYLOAD = {'data': {'node': {'__typename': 'Feedback', 'comment_rendering_instance_for_feed_location': {'comments': {'edges': [{'node': {'__typename': 'Comment', 'id': '1727562635162792', 'created_time': 1786000000, 'author': {'id': '42', 'name': 'Someone'}, 'attachments': [{'media': {'__typename': 'Photo', 'image': {'uri': 'https://scontent/gif.gif'}}}], 'comet_comment_author_name_and_badges_renderer': {'comment': {'id': 'Y29tbWVudDoxNTkzNTY0NDY1NDcxOTkxXzE3Mjc1NjI2MzUxNjI3OTI=', 'author': {'id': '42', 'name': 'Someone'}, 'parent_post_story': {'id': 'UzpfSTkwMDoxNTkzNTY0NDY1NDcxOTkxOjE1OTM1NjQ0NjU0NzE5OTE=', 'attachments': []}}}, 'feedback': {'__typename': 'Feedback', 'plugins': [{'__typename': 'CommentComposerMentionsPlugin', 'post_id': '1593564465471991', 'context_id': '1593564465471991'}]}}}]}}}}}

    def records(self):
        return extract_graphql_records([self.PAYLOAD])

    def test_a_comment_whose_content_is_a_gif_is_still_a_comment(self):
        _, comments, _, _ = self.records()

        self.assertEqual([c.comment_id for c in comments], ["1727562635162792"])

    def test_a_wordless_comment_is_not_mistaken_for_a_post(self):
        posts, _, _, _ = self.records()

        self.assertEqual(posts, [])

    def test_the_relay_global_id_is_not_a_second_comment(self):
        _, comments, _, _ = self.records()

        self.assertEqual(len(comments), 1)

    def test_the_parent_post_stub_is_not_a_comment(self):
        _, comments, _, _ = self.records()

        self.assertNotIn("1593564465471991", [c.comment_id for c in comments])

    def test_a_composer_plugin_does_not_become_a_post(self):
        """It carries the post's id and would inherit the comment's date."""
        posts, _, _, _ = self.records()

        self.assertEqual([p.post_id for p in posts], [])

    def test_a_comment_names_its_post_through_an_encoded_feedback_id(self):
        """Left encoded it matches no post, so the comment loses its parent."""
        _, comments, _, _ = extract_graphql_records([{"data": {"comments": {
            "edges": [{"node": {
                "__typename": "Comment", "id": "1727562635162792",
                "body": {"text": "hello"},
                "feedback_target_id": "ZmVlZGJhY2s6MTU5MzU2NDQ2NTQ3MTk5MQ=="}}]}}}])

        self.assertEqual([c.parent_post_id for c in comments], ["1593564465471991"])

    def test_a_gif_comments_media_is_kept(self):
        _, comments, _, _ = self.records()

        self.assertEqual(comments[0].media_urls, ["https://scontent/gif.gif"])

    def test_a_relay_id_decodes_to_what_it_names(self):
        from webarc.facebook import _relay_global_id

        self.assertEqual(_relay_global_id("Y29tbWVudDoxNTkzNTY0NDY1NDcxOTkxXzE3Mjc1NjI2MzUxNjI3OTI="),
                         ("comment", "1727562635162792"))
        self.assertEqual(_relay_global_id("UzpfSTkwMDoxNTkzNTY0NDY1NDcxOTkxOjE1OTM1NjQ0NjU0NzE5OTE="),
                         ("story", "1593564465471991"))
        self.assertEqual(_relay_global_id("ZmVlZGJhY2s6MTU5MzU2NDQ2NTQ3MTk5MQ=="), ("post", "1593564465471991"))
        self.assertIsNone(_relay_global_id("1727562635162792"))
        self.assertIsNone(_relay_global_id(None))


class CommentExpansionTests(SessionTestCase):
    """The expansion is one page script, so one fault in it costs every click.

    A stray identifier in it threw on the first line, `page.evaluate` raised,
    the caller returned zero, and the capture finished reporting whatever
    handful of comments the page had already loaded as though that were the
    thread.
    """

    def script_of(self, session):
        import inspect
        return inspect.getsource(type(session)._expand_comments)

    def test_the_script_declares_every_value_it_is_given(self):
        session = make_session(self.tmp, include_comments=True)
        source = self.script_of(session)
        declared = set(re.findall(r"\(\{([^}]*)\}\) => \{", source)[0]
                       .replace(" ", "").split(","))
        passed = set(re.findall(r'"(\w+)":', source))

        self.assertTrue(passed, "no arguments found to check")
        self.assertEqual(passed - declared, set())

    def test_a_failing_expansion_is_reported_not_swallowed(self):
        session = make_session(self.tmp, include_comments=True)

        class _Throws:
            def evaluate(self, *_args, **_kwargs):
                raise RuntimeError("perPost is not defined")

        self.assertEqual(session._expand_comments(_Throws()), 0)
        self.assertEqual(session.counters["comment_expansion_failures"], 1)
        self.assertIn("comment_expansion_failed",
                      session.archive.events_path.read_text())

    def test_the_failure_is_reported_once_not_per_click(self):
        session = make_session(self.tmp, include_comments=True)

        class _Throws:
            def evaluate(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        for _ in range(4):
            session._expand_comments(_Throws())

        self.assertEqual(session.counters["comment_expansion_failures"], 4)
        self.assertEqual(
            session.archive.events_path.read_text().count(
                "comment_expansion_failed"), 1)

    def test_a_long_thread_gets_more_rounds_than_a_short_one(self):
        """One round clicks a bounded number of controls, so a thread of
        hundreds cannot be reached in the number of rounds a thread of ten
        needs."""
        def rounds_for(wanted):
            session = make_session(self.tmp, include_comments=True,
                                   max_comments_per_post=wanted)
            counted = {"n": 0}

            def expand(_page):
                # Always productive, so the stall rule never ends the loop
                # and what is measured is the ceiling itself.
                counted["n"] += 1
                session.archive.comments[str(counted["n"])] = object()
                return 1

            session._expand_comments = expand
            session._show_all_comments = lambda _p: None
            session._process_media_queue = lambda budget=0: None
            page = type("p", (), {
                "evaluate": lambda self, *a, **k: None,
                "wait_for_timeout": lambda self, ms: None})()
            session._read_comment_thread(page, "post")
            return counted["n"]

        self.assertEqual(rounds_for(25), 60)
        self.assertEqual(rounds_for(500), 250)


class CommentThreadScrollTests(SessionTestCase):
    """A post permalink opens in a dialog, so the window does not scroll.

    Facebook serves a permalink through CometSinglePostDialogRoute: the post
    sits in a dialog and the page behind it is frozen. Scrolling the window
    there loaded no further comments however many rounds it was given, and
    the harvest stalled out in eleven seconds reporting nineteen comments of
    a thread of hundreds.
    """

    class _Page:
        def __init__(self, containers=2, fail=False):
            self.containers = containers
            self.fail = fail
            self.scripts = []

        def evaluate(self, script, *args):
            self.scripts.append(script)
            if self.fail:
                raise RuntimeError("detached frame")
            return self.containers

        def wait_for_timeout(self, _ms):
            pass

    def test_the_thread_container_is_scrolled_not_only_the_window(self):
        session = make_session(self.tmp, include_comments=True)
        page = self._Page()

        session._scroll_comment_thread(page)

        script = page.scripts[0]
        self.assertIn("scrollIntoView", script)
        self.assertIn("scrollTop", script)
        self.assertIn("overflowY", script)
        self.assertEqual(session.counters["comment_containers_scrolled"], 2)

    def test_the_thread_is_scrolled_not_whatever_article_comes_last(self):
        """A permalink is followed by suggested posts and by the post itself;
        scrolling one of those into view scrolls away from the thread."""
        session = make_session(self.tmp, include_comments=True)
        page = self._Page()

        session._scroll_comment_thread(page)

        script = page.scripts[0]
        self.assertIn('[role="article"] [role="article"]', script)
        target = script.index("const last =")
        self.assertLess(script.index('[role="article"] [role="article"]'),
                        target)

    def test_a_failing_scroll_does_not_end_the_harvest(self):
        session = make_session(self.tmp, include_comments=True)

        session._scroll_comment_thread(self._Page(fail=True))

        self.assertEqual(session.counters["comment_containers_scrolled"], 0)

    def test_the_harvest_scrolls_the_thread(self):
        session = make_session(self.tmp, include_comments=True)
        called = {"n": 0}
        session._expand_comments = lambda _p: 0
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = lambda _p: called.__setitem__(
            "n", called["n"] + 1)

        session._read_comment_thread(self._Page(), "post")

        self.assertGreater(called["n"], 0)


class ExpansionSurveyTests(SessionTestCase):
    """What Facebook calls these controls decides whether any of this works.

    It cannot be known from outside a live session, so a run that clicks
    nothing has to say what it saw instead of leaving it to be guessed at.
    """

    class _Page:
        def __init__(self, result):
            self.result = result

        def evaluate(self, _script, *args):
            return self.result

    def test_the_first_look_at_the_controls_is_recorded(self):
        session = make_session(self.tmp, include_comments=True)
        page = self._Page({"clicked": 0, "matched": 0, "articles": 3,
                           "labels": ["Like", "Reply", "Most relevant"]})

        session._expand_comments(page)

        recorded = session.archive.events_path.read_text()
        self.assertIn("comment_controls_surveyed", recorded)
        self.assertIn("Most relevant", recorded)

    def test_the_survey_is_recorded_once_not_every_round(self):
        session = make_session(self.tmp, include_comments=True)
        page = self._Page({"clicked": 1, "matched": 1, "articles": 3,
                           "labels": []})

        for _ in range(5):
            session._expand_comments(page)

        self.assertEqual(
            session.archive.events_path.read_text().count(
                "comment_controls_surveyed"), 1)
        self.assertEqual(session.counters["comment_expansion_clicks"], 5)

    def test_the_dialog_is_searched_when_harvesting_one_post(self):
        session = make_session(self.tmp, include_comments=True)
        session._permalink_post_id = "1593564465471991"
        seen = {}

        class _Recorder:
            def evaluate(self, script, args):
                seen["script"] = script
                seen["args"] = args
                return {"clicked": 0, "matched": 0, "articles": 1,
                        "labels": []}

        session._expand_comments(_Recorder())

        self.assertTrue(seen["args"]["perPost"])
        self.assertIn('[role="dialog"]', seen["script"])


class CuratorDecisionTests(SessionTestCase):
    """Closing the browser takes the decision away from the curator.

    The session ends, the manifest is final, and whatever they could still
    have reached by hand is gone. A read that failed, and a thread Facebook
    itself says is longer than what arrived, are both cases where the person
    watching should choose what happens next.
    """

    def make(self, **kwargs):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True, **kwargs)
        session.started_scrolling = True
        session.state = RECORDING
        return session

    def test_a_failed_read_is_handed_back_not_declared_finished(self):
        session = self.make()

        class _Throws:
            def wait_for_timeout(self, _ms):
                raise RuntimeError("Target page crashed")

        session._read_comment_thread = lambda *a: (_ for _ in ()).throw(
            RuntimeError("Target page crashed"))
        session._capture_single_post(_Throws())

        self.assertEqual(session.state, BLOCKED)
        self.assertIsNone(session._pending_stop)
        self.assertIn("Target page crashed", session.phase_detail)

    def test_a_failed_read_can_be_retried(self):
        session = self.make()
        session._read_comment_thread = lambda *a: (_ for _ in ()).throw(
            RuntimeError("boom"))
        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertFalse(session._single_post_done)

    def test_a_failed_read_does_not_restart_itself_for_ever(self):
        session = self.make()
        session._read_comment_thread = lambda *a: (_ for _ in ()).throw(
            RuntimeError("boom"))
        session._capture_single_post(_FakePage(url=POST_URL))

        session._maybe_auto_start(_FakePage(url=POST_URL))

        self.assertEqual(session.state, BLOCKED)

    def test_the_curator_lifting_the_block_clears_the_hold(self):
        session = self.make()
        session._read_comment_thread = lambda *a: (_ for _ in ()).throw(
            RuntimeError("boom"))
        session._capture_single_post(_FakePage(url=POST_URL))

        session.apply("resume", actor="dashboard")

        self.assertEqual(session.state, RECORDING)
        self.assertFalse(session._awaiting_curator_decision)

    def test_until_i_stop_is_not_overridden_by_one_post(self):
        """Even a thread read out in full: they said when the session ends."""
        session = self.make()
        session.config.requested_mode = "until_stopped"
        session.archive.add_post(post("1593564465471991",
                                      date="2026-07-17T15:16:35Z"))
        session.archive.posts["1593564465471991"].comments_count = 2
        for n in range(2):
            session.archive.add_comment(FacebookComment(comment_id=str(n)))
        session._read_comment_thread = lambda *a: None
        self.assertEqual(session._comments_not_collected(), 0)

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertIsNone(session._pending_stop)
        self.assertEqual(session.state, PAUSED)
        self.assertIn("Stop and save", session.phase_detail)

    def test_a_thread_facebook_says_is_longer_holds_for_a_decision(self):
        session = self.make(max_comments_per_post=500)
        session.archive.add_post(post("1593564465471991",
                                      date="2026-07-17T15:16:35Z"))
        session.archive.posts["1593564465471991"].comments_count = 455
        for n in range(19):
            session.archive.add_comment(FacebookComment(comment_id=str(n)))
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertIsNone(session._pending_stop)
        self.assertEqual(session.state, PAUSED)
        self.assertIn("19 of 455 comments on this post",
                      session.phase_detail)

    def test_a_thread_that_was_read_out_finishes(self):
        session = self.make(max_comments_per_post=500)
        session.archive.add_post(post("1593564465471991",
                                      date="2026-07-17T15:16:35Z"))
        session.archive.posts["1593564465471991"].comments_count = 3
        for n in range(3):
            session.archive.add_comment(FacebookComment(comment_id=str(n)))
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertEqual(session._pending_stop[0], "single_post_captured")

    def test_the_shortfall_is_recorded_in_the_manifest(self):
        session = self.make(max_comments_per_post=500)
        session.archive.add_post(post("1593564465471991",
                                      date="2026-07-17T15:16:35Z"))
        session.archive.posts["1593564465471991"].comments_count = 455
        for n in range(19):
            session.archive.add_comment(FacebookComment(comment_id=str(n)))

        comments = session._manifest_document()["requested_work"]["comments"]

        self.assertEqual(comments["comments_stated_on_posts"], 455)
        self.assertEqual(comments["comments_not_collected"], 436)


class SignedOutCaptureTests(SessionTestCase):
    """Signed out, Facebook decides how much of a thread exists.

    It serves ten comments a page and offers no control to ask for the next,
    so a capture stops at around twenty however long the thread is, however
    long it runs and whatever maximum was requested. Captures 27 and 30 were
    both made signed out -- every request in them carries __user=0 -- and both
    stopped at 19 of a thread of 455.
    """

    class _Context:
        def __init__(self, cookies):
            self._cookies = cookies

        def cookies(self, _url=None):
            if self._cookies is None:
                raise RuntimeError("context closed")
            return self._cookies

    def session_with(self, cookies):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        session._context = self._Context(cookies)
        session.started_scrolling = False
        return session

    def test_a_signed_in_browser_is_recognised(self):
        session = self.session_with([{"name": "c_user", "value": "100044"}])

        self.assertTrue(session._viewer_is_signed_in())

    def test_a_signed_out_browser_is_recognised(self):
        session = self.session_with([{"name": "datr", "value": "x"}])

        self.assertFalse(session._viewer_is_signed_in())

    def test_an_empty_cookie_value_is_not_signed_in(self):
        session = self.session_with([{"name": "c_user", "value": ""}])

        self.assertFalse(session._viewer_is_signed_in())

    def test_an_unreadable_cookie_jar_is_unknown_not_signed_out(self):
        session = self.session_with(None)

        self.assertIsNone(session._viewer_is_signed_in())
        self.assertFalse(session._signed_out_blocks_start())

    def test_the_capture_says_so_before_it_starts(self):
        session = self.session_with([])

        self.assertTrue(session._signed_out_blocks_start())
        self.assertEqual(session.state, BLOCKED)
        self.assertIn("not signed in", session.phase_detail)
        self.assertIn("viewer_not_signed_in",
                      session.archive.events_path.read_text())

    def test_auto_start_does_not_run_past_the_warning(self):
        session = self.session_with([])
        session.state = PAUSED
        session._page_is_target = lambda _p: True
        session._detect_verification = lambda _p: None

        session._maybe_auto_start(_FakePage(url=POST_URL))

        self.assertEqual(session.state, BLOCKED)

    def test_the_curator_can_choose_to_capture_signed_out_anyway(self):
        session = self.session_with([])
        session._signed_out_blocks_start()

        session.apply("resume", actor="dashboard")

        self.assertEqual(session.state, RECORDING)
        self.assertTrue(session._signed_out_acknowledged)
        self.assertFalse(session._signed_out_blocks_start())

    def test_the_warning_is_not_dismissed_by_the_tool_itself(self):
        """Auto-start lifting its own warning would defeat the point."""
        session = self.session_with([])
        session._signed_out_blocks_start()

        session.apply("resume", actor="automatic")

        self.assertFalse(session._signed_out_acknowledged)
        self.assertTrue(session._signed_out_blocks_start())

    def test_the_manifest_records_which_view_was_captured(self):
        session = self.session_with([])
        session._signed_out_blocks_start()

        document = session._manifest_document()

        self.assertEqual(
            document["requested_work"]["comments"]["viewer"], "signed_out")
        self.assertIn("ten comments",
                      document["completeness"]["signed_out_capture_meaning"])

    def test_a_signed_in_manifest_makes_no_such_excuse(self):
        session = self.session_with([{"name": "c_user", "value": "1"}])
        session._viewer_signed_in = session._viewer_is_signed_in()

        document = session._manifest_document()

        self.assertEqual(
            document["requested_work"]["comments"]["viewer"], "signed_in")
        self.assertIsNone(
            document["completeness"]["signed_out_capture_meaning"])


class ThreadProgressTests(SessionTestCase):
    """Progress against the request answers the wrong question.

    "10 of up to 400 requested" tells a curator what they typed into a box.
    What they need to know is how much of the thread they have, and only the
    thread's own length says that.
    """

    def session(self, stated=None, collected=0, wanted=400):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=wanted)
        session.started_scrolling = True
        if stated is not None:
            session.archive.add_post(post("1593564465471991",
                                          date="2026-07-17T15:16:35Z"))
            session.archive.posts["1593564465471991"].comments_count = stated
        for n in range(collected):
            session.archive.add_comment(FacebookComment(comment_id=str(n)))
        return session

    def test_progress_is_reported_against_the_thread(self):
        session = self.session(stated=433, collected=10)

        self.assertIn("10 of 433 comments on this post",
                      session._comment_progress())

    def test_the_requested_maximum_is_named_when_it_binds(self):
        session = self.session(stated=433, collected=10, wanted=400)

        self.assertIn("up to 400", session._comment_progress())

    def test_the_requested_maximum_is_left_out_when_it_does_not(self):
        session = self.session(stated=433, collected=10, wanted=500)

        self.assertNotIn("up to 500", session._comment_progress())

    def test_an_unstated_thread_length_is_not_invented(self):
        session = self.session(stated=None, collected=10)
        progress = session._comment_progress()

        self.assertIn("has not said how long", progress)
        self.assertIn("10 comments collected", progress)

    def test_the_thread_length_travels_with_the_progress_report(self):
        session = self.session(stated=433, collected=10)

        self.assertEqual(
            session._progress_details()["comments_available"], 433)

    def test_progress_is_reported_while_the_thread_is_read(self):
        session = self.session(stated=433, collected=10)
        session._expand_comments = lambda _p: 0
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = lambda _p: None
        page = type("p", (), {"wait_for_timeout": lambda self, ms: None})()

        session._read_comment_thread(page, "1593564465471991")

        self.assertIn("10 of 433 comments", session.phase_detail)

    def test_carrying_on_reads_the_thread_again(self):
        """The hold offers "carry on"; it has to actually do something."""
        session = self.session(stated=433, collected=10)
        session._read_comment_thread = lambda *a: None
        session._capture_single_post(_FakePage(url=POST_URL))
        self.assertTrue(session._single_post_done)

        session.apply("resume", actor="dashboard")

        self.assertFalse(session._single_post_done)

    def test_the_hold_offers_both_ways_out(self):
        session = self.session(stated=433, collected=10)
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertIn("Resume scrolling", session.phase_detail)
        self.assertIn("Stop and save", session.phase_detail)


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


def viewer_document(photo_id="1596470645181373", post_id="1596471388514632",
                    album="229529241875527", uri="https://scontent/full.jpg",
                    text="I am very happy to visit", created=1784624095):
    """A CometPhotoRootContentQuery response, as Facebook shapes it."""
    story = {
        "__typename": "Story",
        "id": "UzpfSTEwMDA0NDU1MzU3NDM0NToxNTk2NDcxMzg4NTE0NjMyOjE1OTY0NzEzODg1MTQ2MzI=",
        "post_id": post_id,
        "url": f"https://www.facebook.com/Yusuffali.MA/posts/pfbid0{post_id}",
        "message": {"text": text},
        "actors": [{"__typename": "User", "name": "Yusuff Ali M.A",
                    "id": "100044553574345"}],
    }
    return {"data": {
        "currMedia": {
            "__typename": "Photo", "id": photo_id,
            "created_time": created,
            "accessibility_caption": "May be an image of text",
            "image": {"uri": uri, "width": 1051, "height": 701},
            "owner": {"__typename": "User", "id": "100044553574345"},
            "container_story": story,
            "creation_story": dict(story),
            "feedback": {"id": "ZmVlZGJhY2s6MTU5NjQ3MDY0NTE4MTM3Mw=="},
            "default_mediaset": {"id": album, "__typename": "Album"},
        },
        "mediaset": {"__typename": "Album", "nextMedia": {"edges": [
            {"node": {"__typename": "Photo", "id": "1593563378805433"}}]}},
    }}


class PhotoViewerTests(SessionTestCase):
    """The photo viewer walks the album, not the post.

    Opening a post's photo and pressing the arrow shows the next photo in
    the album, which belongs to another post. Each viewer response carried
    that other post's id, permalink and first line, and the capture minted
    a post from every one -- twelve posts with no date and no media, for a
    capture of one post -- while the full-size images the viewer served
    were never kept at all.
    """

    def test_a_viewer_response_is_recognised(self):
        from webarc.facebook import is_viewer_document
        self.assertTrue(is_viewer_document(viewer_document()))
        self.assertFalse(is_viewer_document({"data": {"node": {"id": "1"}}}))
        self.assertFalse(is_viewer_document({"data": {"currMedia": None}}))

    def test_the_photo_is_read_with_its_post_album_and_date(self):
        from webarc.facebook import extract_viewer_photos
        [photo] = extract_viewer_photos([viewer_document()])

        self.assertEqual(photo.photo_id, "1596470645181373")
        self.assertEqual(photo.image_url, "https://scontent/full.jpg")
        self.assertEqual((photo.width, photo.height), (1051, 701))
        self.assertEqual(photo.post_id, "1596471388514632")
        self.assertIn("/posts/pfbid0", photo.post_url)
        self.assertEqual(photo.post_text, "I am very happy to visit")
        self.assertEqual(photo.album_id, "229529241875527")
        self.assertEqual(photo.created_time, "2026-07-21T08:54:55Z")
        self.assertEqual(photo.caption, "May be an image of text")
        self.assertEqual(photo.owner_id, "100044553574345")
        self.assertEqual(
            photo.photo_url,
            "https://www.facebook.com/photo/?fbid=1596470645181373"
            "&set=a.229529241875527")

    def test_no_post_is_minted_from_a_viewer_response(self):
        session = make_session(self.tmp, page_url=POST_URL)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))

        session._ingest_records([viewer_document(),
                                 viewer_document(photo_id="2", post_id="3")])

        self.assertEqual(list(session.seen_this_run), ["1593564465471991"])
        self.assertEqual(session.counters["viewer_posts_suppressed"], 2)
        self.assertEqual(session.exclusions["not_the_requested_post"], 0)

    def test_the_photo_is_kept_as_album_context(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=True)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))

        session._ingest_records([viewer_document()])

        [photo] = session.archive.viewer_photos.values()
        self.assertEqual(photo.opened_from_post_id, "1593564465471991")
        self.assertFalse(photo.belongs_to_captured_post)
        self.assertEqual(session.counters["viewer_photos_observed"], 1)
        self.assertEqual(session.counters["album_photos_from_other_posts"], 1)
        self.assertEqual(list(session._media_queue),
                         [("photo-1596470645181373", "https://scontent/full.jpg")])
        self.assertIn("viewer_photo_observed",
                      session.archive.events_path.read_text())

    def test_the_captured_posts_own_photo_is_marked_as_its(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=True)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))

        session._ingest_records([viewer_document(
            photo_id="1593563378805433", post_id="1593564465471991")])

        [photo] = session.archive.viewer_photos.values()
        self.assertTrue(photo.belongs_to_captured_post)
        self.assertEqual(session.counters["album_photos_from_other_posts"], 0)

    def test_a_photo_seen_twice_is_kept_once(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=True)
        session._ingest_records([viewer_document()])
        session._ingest_records([viewer_document()])

        self.assertEqual(len(session.archive.viewer_photos), 1)
        self.assertEqual(len(session._media_queue), 1)
        self.assertEqual(session.counters["viewer_photos_repeated"], 1)

    def test_no_image_is_fetched_when_media_was_not_requested(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=False)
        session._ingest_records([viewer_document()])

        self.assertEqual(len(session.archive.viewer_photos), 1)
        self.assertEqual(list(session._media_queue), [])

    def test_the_photos_comments_are_still_read(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        document = viewer_document()
        document["data"]["currMedia"]["feedback"]["comments"] = {"edges": [{
            "node": {"__typename": "Comment", "id": "77",
                     "body": {"text": "lovely"},
                     "feedback_target_id": "ZmVlZGJhY2s6MTU5MzU2NDQ2NTQ3MTk5MQ=="}}]}

        session._ingest_records([document])

        self.assertIn("77", session.archive.comments)

    def test_the_context_is_exported_and_described(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=True)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))
        session._ingest_records([viewer_document()])
        session.archive.media_index["https://scontent/full.jpg"] = "full.jpg"
        session.archive.write_exports()

        rows = [json.loads(line) for line in
                (self.tmp / "facebook-album-context.jsonl").read_text()
                .splitlines() if line.strip()]
        self.assertEqual([row["photo_id"] for row in rows],
                         ["1596470645181373"])
        self.assertTrue((self.tmp / "facebook-album-context.csv").exists())
        manifest = session._manifest_document(final=True)
        section = manifest["requested_work"]["album_context"]
        self.assertEqual(section["photos_viewed"], 1)
        self.assertEqual(section["of_other_posts"], 1)
        self.assertEqual(section["images_downloaded"], 1)
        self.assertEqual(section["albums"], ["229529241875527"])
        self.assertEqual(section["opened_from_posts"], ["1593564465471991"])
        self.assertIn("not posts of this capture", section["note"])
        self.assertEqual(manifest["layers"]["normalised"]["album_context"],
                         ["facebook-album-context.jsonl",
                          "facebook-album-context.csv"])
        self.assertEqual(
            session._progress_details()["album_photos_viewed"], 1)

    def test_the_reader_page_shows_the_album_around_the_post(self):
        from webarc.facebook_render import build_site
        session = make_session(self.tmp, page_url=POST_URL,
                               capture_media=True)
        session._consider_post(post("1593564465471991",
                                    date="2026-05-01T09:00:00Z"))
        session._ingest_records([
            viewer_document(photo_id="1593563378805433",
                            post_id="1593564465471991",
                            uri="https://scontent/own.jpg"),
            viewer_document()])
        session.archive.media_index["https://scontent/own.jpg"] = "own.jpg"
        session.archive.write_exports()
        session.archive.checkpoint(session._checkpoint_document(),
                                   session._manifest_document(final=True))

        site = build_site(self.tmp)
        page = (site / "posts" / "1593564465471991.html").read_text()

        self.assertIn("Photos opened in the viewer", page)
        self.assertIn("This post's photo", page)
        self.assertIn("From another post in the same album", page)
        self.assertIn('src="../../media/own.jpg"', page)
        self.assertIn("Not captured", page)
        index = (site / "index.html").read_text()
        self.assertIn("Album photos viewed", index)
        # The index is the page a reader opens; the photos are not to be
        # found only behind the comments link.
        self.assertIn("Photos opened in the viewer", index)
        self.assertIn('src="../media/own.jpg"', index)


class _WheelPage:
    """A page with a mouse, as a real Playwright page has."""

    def __init__(self, target=None, fail_wheel=False):
        self.target = target if target is not None else {"x": 300.0, "y": 500.0}
        self.scripts = []
        self.moves = []
        self.wheels = []
        page = self

        class _Mouse:
            def move(self, x, y):
                page.moves.append((x, y))

            def wheel(self, dx, dy):
                if fail_wheel:
                    raise RuntimeError("no page")
                page.wheels.append((dx, dy))
        self.mouse = _Mouse()

    def evaluate(self, script, *args):
        self.scripts.append(script)
        if "getBoundingClientRect" in script:
            return self.target
        return 1

    def wait_for_timeout(self, _ms):
        pass


class ThreadStallTests(SessionTestCase):
    """Auto-scrolling gave up on threads a hand could still scroll.

    The read stalled after eight quiet rounds, under ten seconds, while
    Facebook was merely slow or a comment page was still on its way; it
    scrolled only from script, which Facebook does not always answer; and
    it clicked each "View more comments" control once, though Facebook
    keeps the same element for the next page. The curator then scrolled
    by hand and the thread carried on where the capture had said it ended.
    """

    def reading(self, session, expand=lambda _p: 0):
        session._expand_comments = expand
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = lambda _p: None
        session._ensure_facebook_widgets = lambda: None

    def test_the_thread_is_given_thirty_quiet_seconds_not_ten(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=400)
        rounds = {"n": 0}
        self.reading(session, expand=lambda _p: rounds.__setitem__(
            "n", rounds["n"] + 1) or 0)

        session._read_comment_thread(_WheelPage(), "post")

        # 1.2 s of nominal waiting per quiet round.
        self.assertEqual(rounds["n"], 25)
        self.assertGreaterEqual(session._thread_quiet_seconds, 30.0)

    def test_a_comment_page_still_in_flight_is_not_a_stall(self):
        session = make_session(self.tmp, include_comments=True,
                               max_comments_per_post=400)
        rounds = {"n": 0}
        pending = object()

        def expand(_p):
            rounds["n"] += 1
            if rounds["n"] == 3:
                session._comment_requests_in_flight.add(pending)
            if rounds["n"] == 30:
                session._comment_requests_in_flight.discard(pending)
            return 0
        self.reading(session, expand=expand)

        session._read_comment_thread(_WheelPage(), "post")

        self.assertGreater(rounds["n"], 50)

    def test_a_comment_page_request_is_noticed_and_released(self):
        session = make_session(self.tmp, include_comments=True)

        class _Request:
            url = "https://www.facebook.com/api/graphql/"
            resource_type = "xhr"
            post_data = ("fb_api_req_friendly_name=CommentsListComponents"
                         "PaginationQuery&variables=%7B%22cursor%22%3A%22abc%22%7D")
        request = _Request()

        session._on_request(request)
        self.assertTrue(session._comment_page_pending())
        self.assertEqual(session.counters["comment_pages_requested"], 1)
        self.assertEqual(session._last_cursor, "abc")

        session._on_request_finished(request)
        self.assertFalse(session._comment_page_pending())

    def test_a_failed_comment_page_request_is_released_too(self):
        session = make_session(self.tmp, include_comments=True)

        class _Request:
            url = "https://www.facebook.com/api/graphql/"
            resource_type = "xhr"
            post_data = "fb_api_req_friendly_name=CommentListComponentsRootQuery"
        request = _Request()
        session._on_request(request)
        session._on_request_failed(request)

        self.assertFalse(session._comment_page_pending())

    def test_other_requests_are_not_mistaken_for_comment_pages(self):
        session = make_session(self.tmp, include_comments=True)

        class _Request:
            url = "https://www.facebook.com/api/graphql/"
            resource_type = "xhr"
            post_data = "fb_api_req_friendly_name=CometPhotoRootContentQuery"
        session._on_request(_Request())

        self.assertFalse(session._comment_page_pending())

    def test_the_thread_is_scrolled_with_the_wheel_first(self):
        session = make_session(self.tmp, include_comments=True)
        page = _WheelPage()

        session._scroll_comment_thread(page)

        self.assertEqual(page.moves, [(300.0, 500.0)])
        self.assertEqual(len(page.wheels), 3)
        self.assertTrue(all(dy > 0 for _dx, dy in page.wheels))
        self.assertIn("getBoundingClientRect", page.scripts[0])
        self.assertIn('[role="article"] [role="article"]', page.scripts[0])
        self.assertIn("scrollTop", page.scripts[1])
        self.assertEqual(session.counters["comment_wheel_scrolls"], 1)

    def test_a_page_without_a_mouse_is_still_scrolled_from_script(self):
        session = make_session(self.tmp, include_comments=True)
        page = CommentThreadScrollTests._Page()

        session._scroll_comment_thread(page)

        self.assertEqual(session.counters["comment_containers_scrolled"], 2)

    def test_a_thread_with_nothing_on_screen_is_not_wheeled(self):
        session = make_session(self.tmp, include_comments=True)
        page = _WheelPage(target=None)
        page.target = None

        session._scroll_comment_thread(page)

        self.assertEqual(page.wheels, [])
        self.assertEqual(session.counters["comment_wheel_scrolls"], 0)

    def test_a_failing_wheel_does_not_end_the_scroll(self):
        session = make_session(self.tmp, include_comments=True)
        page = _WheelPage(fail_wheel=True)

        session._scroll_comment_thread(page)

        self.assertEqual(session.counters["comment_wheel_scrolls"], 0)
        self.assertIn("scrollTop", page.scripts[-1])

    def test_a_more_comments_control_is_clicked_again_after_a_while(self):
        import inspect
        source = inspect.getsource(FacebookCaptureSession._expand_comments)

        self.assertNotIn("swmClicked === '1'", source)
        self.assertIn("swmClickedAt", source)
        self.assertIn("cooldown", source)

    def test_the_hold_says_how_long_the_thread_was_given(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        item = post("1593564465471991", date="2026-05-01T09:00:00Z")
        item.comments_count = 433
        session._consider_post(item)
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertEqual(session.state, PAUSED)
        self.assertIn("30 seconds", session.phase_detail)
        self.assertIn("Resume scrolling", session.phase_detail)


class ReadProgressReachesTheDashboardTests(SessionTestCase):
    """Resuming from a hold left the dashboard saying "paused".

    The read holds the session's only thread for as long as it runs and the
    main loop reports once a second between its own turns, so nothing said
    "scrolling" until the read ended -- by which time it had held again.
    """

    def reports_of(self, session):
        seen = []
        session.on_progress = lambda **kw: seen.append(kw["state"])
        session._ensure_facebook_widgets = lambda: None
        session._checkpoint = lambda force=False: None
        return seen

    def test_the_read_reports_the_running_state_while_it_runs(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        seen = self.reports_of(session)
        session._expand_comments = lambda _p: 0
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = lambda _p: None
        session.state = RECORDING

        session._read_comment_thread(_WheelPage(), "1593564465471991")

        self.assertIn(RECORDING, seen)

    def test_a_resume_from_the_widget_is_reported_at_once(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        seen = self.reports_of(session)
        session.state = PAUSED
        session.started_scrolling = True

        session._on_widget_command({}, "resume")
        command, page, actor = session._commands.popleft()
        session.apply(command, page, actor=actor)
        session._pulse(force=True)

        self.assertEqual(seen[-1], RECORDING)

    def test_the_pulse_is_throttled_to_once_a_second(self):
        session = make_session(self.tmp, page_url=POST_URL)
        seen = self.reports_of(session)

        session._pulse(force=True)
        session._pulse()
        session._pulse()

        self.assertEqual(len(seen), 1)


class ProfileInUseTests(SessionTestCase):
    """A launch on a profile another Chrome holds does not fail as itself.

    Chrome hands the Page to the running instance and exits, so the new
    capture's job fails with "target closed" while the old window -- whose
    capture ended long ago -- shows the SWM panel with a Start button that
    does nothing. The launch failure has to name the window to close, and
    the panel has to say when nothing is behind it.
    """

    def driver(self, error):
        from webarc.facebook import FacebookBrowserDriver

        driver = FacebookBrowserDriver.__new__(FacebookBrowserDriver)
        driver.cfg = BrowserConfig(mode="headed", user_data_dir=str(self.tmp))

        class _Chromium:
            def launch_persistent_context(self, *_a, **_k):
                raise error

        driver._pw = type("pw", (), {"chromium": _Chromium()})()
        return driver

    def test_the_failure_names_the_window_to_close(self):
        driver = self.driver(RuntimeError(
            "BrowserType.launch_persistent_context: Opening in existing "
            "browser session. This usually means that the profile is "
            "already in use by another instance of Chromium."))

        with self.assertRaises(RuntimeError) as caught:
            driver._start()

        message = str(caught.exception)
        self.assertIn("already open in another Chrome window", message)
        self.assertIn("buttons do nothing", message)
        self.assertIn(str(self.tmp), message)

    def test_other_launch_failures_are_left_as_they_are(self):
        driver = self.driver(RuntimeError("Executable doesn't exist"))

        with self.assertRaises(RuntimeError) as caught:
            driver._start()

        self.assertEqual(str(caught.exception), "Executable doesn't exist")

    def test_the_panel_is_live_at_first_and_gives_up_only_on_silence(self):
        """At start-up the capture is busy loading Facebook and answers
        late; a panel that said "not connected" then was wrong, and hid
        the Start button the curator was waiting for."""
        from webarc.facebook import _FACEBOOK_WIDGET_JS as widget

        self.assertIn("let connected = null", widget)
        self.assertIn("if (connected === false) return [];", widget)
        self.assertIn("ANSWER_WAIT = 45000", widget)
        self.assertIn("not connected to an SWM capture", widget)
        self.assertIn("Date.now() - askedAt > ANSWER_WAIT", widget)

    def test_the_navigation_does_not_wait_for_the_full_load_event(self):
        """The loop that feeds the panel does not run until navigation
        returns; waiting for Facebook's late "load" event kept the panel
        unfed long enough to declare itself disconnected."""
        import inspect
        source = inspect.getsource(
            FacebookCaptureSession.run_with_context)

        self.assertIn('wait_until="domcontentloaded"', source)
        self.assertNotIn('wait_until="load"', source)


class _FakeWidgetPage:
    """A page whose widget script the session evaluates."""

    def __init__(self):
        self.evaluated = []

    def evaluate(self, script, *args):
        self.evaluated.append((script, args))


class WidgetPushTests(SessionTestCase):
    def test_every_page_of_the_context_is_told_the_state(self):
        session = make_session(self.tmp)
        pages = [_FakeWidgetPage(), _FakeWidgetPage()]
        session._context = type("ctx", (), {"pages": pages})()
        session._state_dirty = True

        session._ensure_facebook_widgets()

        for page in pages:
            # The thread helper, the panel, then the state.
            self.assertEqual(len(page.evaluated), 3)
            self.assertEqual(page.evaluated[2][1][0]["state"], PAUSED)
        self.assertFalse(session._state_dirty)


FEEDBACK_ID = "ZmVlZGJhY2s6MTU5MzU2NDQ2NTQ3MTk5MQ=="   # feedback:1593564465471991


def comment_page(has_next=True, cursor="CURSOR", comments=("11", "12")):
    """A CommentsListComponentsPaginationQuery response, as Facebook shapes it."""
    return {"data": {"node": {
        "__typename": "Feedback", "id": FEEDBACK_ID,
        "comment_rendering_instance_for_feed_location": {"comments": {
            "edges": [{"node": {
                "__typename": "Comment", "id": cid, "body": {"text": "hi"},
                "feedback": {"replies_connection": {
                    "edges": [], "page_info": {
                        "has_next_page": False, "end_cursor": None}}},
            }} for cid in comments],
            "page_info": {"has_next_page": has_next, "end_cursor": cursor,
                          "has_previous_page": True},
        }}}}}


class ThreadPageInfoTests(SessionTestCase):
    """Facebook says on every comment page whether another follows.

    The read used to infer the end of a thread from silence: nothing new
    for a while, so hold and ask the curator. Every page Facebook served
    in the 1 September capture said has_next_page true, all 24 of them,
    and the capture stopped asking anyway. The thread's own word is what
    should decide between "Facebook ended it" and "the page stopped asking".
    """

    def test_the_threads_page_info_is_read_with_its_post(self):
        from webarc.facebook import extract_comment_page_info
        [info] = extract_comment_page_info([comment_page()])

        self.assertEqual(info["post_id"], "1593564465471991")
        self.assertTrue(info["has_next_page"])
        self.assertEqual(info["end_cursor"], "CURSOR")
        self.assertEqual(info["comments_on_page"], 2)

    def test_reply_connections_are_not_the_thread(self):
        from webarc.facebook import extract_comment_page_info
        self.assertEqual(len(extract_comment_page_info([comment_page()])), 1)

    def test_the_page_rendered_into_the_permalink_counts_too(self):
        from webarc.facebook import extract_comment_page_info
        document = {"require": [{"__bbox": {"result": {"data": {"node": {
            "feedback": {"__typename": "Feedback", "id": FEEDBACK_ID,
                         "comment_rendering_instance_for_feed_location": {
                             "comments": {"edges": [], "page_info": {
                                 "has_next_page": False, "end_cursor": "X"}}}}}}}}}]}

        [info] = extract_comment_page_info([document])

        self.assertEqual(info["post_id"], "1593564465471991")
        self.assertFalse(info["has_next_page"])

    def test_the_session_remembers_what_facebook_last_said(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True)
        self.assertIsNone(session._thread_exhausted("1593564465471991"))

        session._ingest_records([comment_page(has_next=True)])
        self.assertFalse(session._thread_exhausted("1593564465471991"))

        session._ingest_records([comment_page(has_next=False,
                                              comments=("13",))])
        self.assertTrue(session._thread_exhausted("1593564465471991"))
        self.assertEqual(session._thread_pages["1593564465471991"]["pages"], 2)

    def stated(self, session, count=433):
        item = post("1593564465471991", date="2026-05-01T09:00:00Z")
        item.comments_count = count
        session._consider_post(item)

    def test_a_thread_facebook_ended_is_not_held_for_the_curator(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        self.stated(session)
        session._ingest_records([comment_page(has_next=False)])
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertEqual(session._pending_stop,
                         ("single_post_captured",
                          "facebook_reported_no_further_comment_page"))
        self.assertIn("comment_thread_ended_by_facebook",
                      session.archive.events_path.read_text())

    def test_a_thread_facebook_still_offers_is_held_and_said_so(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        self.stated(session)
        session._ingest_records([comment_page(has_next=True)])
        session._read_comment_thread = lambda *a: None
        session.counters["comment_pages_requested"] = 3
        session.counters["comment_pages_after_click"] = 3

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertEqual(session.state, PAUSED)
        self.assertIn("still reports further comment pages",
                      session.phase_detail)
        self.assertIn("3 after a click and 0 after a scroll",
                      session.phase_detail)
        manifest = session._manifest_document()["requested_work"]["comments"]
        self.assertTrue(manifest["facebook_reports_more_comments"])
        self.assertTrue(manifest["next_comment_cursor_known"])
        self.assertEqual(manifest["thread_pages_after_click"], 3)

    def test_an_unheard_thread_keeps_the_plain_message(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        self.stated(session)
        session._read_comment_thread = lambda *a: None

        session._capture_single_post(_FakePage(url=POST_URL))

        self.assertIn("stopped returning comments", session.phase_detail)
        manifest = session._manifest_document()["requested_work"]["comments"]
        self.assertIsNone(manifest["facebook_reports_more_comments"])

    def reading(self, session, expand=lambda _p: 0, scroll=lambda _p: None):
        session._expand_comments = expand
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = scroll
        session._ensure_facebook_widgets = lambda: None

    def test_the_read_stops_once_facebook_has_ended_the_thread(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        rounds = {"n": 0}
        self.reading(session, expand=lambda _p: rounds.__setitem__(
            "n", rounds["n"] + 1) or 0)
        session._ingest_records([comment_page(has_next=False)])

        session._read_comment_thread(_WheelPage(), "1593564465471991")

        self.assertEqual(rounds["n"], 0)

    def test_a_click_that_fetches_nothing_is_not_progress(self):
        """Clicking a dead control every round kept the read going to the
        round ceiling: minutes of clicking with nothing to show."""
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        rounds = {"n": 0}
        self.reading(session, expand=lambda _p: rounds.__setitem__(
            "n", rounds["n"] + 1) or 1)

        session._read_comment_thread(_WheelPage(), "1593564465471991")

        # 1.6 s of nominal waiting per clicking round; 30 s of patience.
        self.assertEqual(rounds["n"], 19)

    def test_pages_are_laid_at_the_door_of_what_fetched_them(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        state = {"round": 0}

        def expand(_p):
            state["round"] += 1
            if state["round"] == 1:
                session.counters["comment_pages_requested"] += 1
            return 1

        def scroll(_p):
            if state["round"] == 2:
                session.counters["comment_pages_requested"] += 2
        self.reading(session, expand=expand, scroll=scroll)

        session._read_comment_thread(_WheelPage(), "1593564465471991")

        self.assertEqual(session.counters["comment_pages_after_click"], 1)
        self.assertEqual(session.counters["comment_pages_after_scroll"], 2)
        events = [json.loads(line) for line in
                  session.archive.events_path.read_text().splitlines()]
        rounds = [e for e in events if e["event"] == "comment_round"]
        self.assertEqual(rounds[0]["pages_after_click"], 1)
        self.assertEqual(rounds[1]["pages_after_scroll"], 2)
        self.assertIn("facebook_reports_more", rounds[0])

    def test_round_records_are_bounded(self):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=400)
        counted = {"n": 0}

        def expand(_p):
            counted["n"] += 1
            session.archive.comments[str(counted["n"])] = object()
            return 1
        self.reading(session, expand=expand)

        session._read_comment_thread(_WheelPage(), "1593564465471991")

        events = [json.loads(line) for line in
                  session.archive.events_path.read_text().splitlines()]
        rounds = [e for e in events if e["event"] == "comment_round"]
        self.assertEqual(counted["n"], 200)
        self.assertEqual(len(rounds), 40 + 16)


class _ContinuingPage:
    """A page whose client has made a comment request, standing in for
    Facebook as well: each continued page is answered by ingesting the
    next comment page into the session."""

    def __init__(self, session, pages, seen=True, status=200):
        self.session = session
        self.pages = list(pages)      # (has_next, cursor, comment ids)
        self.seen = seen
        self.status = status
        self.cursors_asked = []
        self.mouse = _WheelPage().mouse

    def evaluate(self, script, *args):
        if "__swmCommentRequestSeen" in script:
            return self.seen
        if "__swmContinueComments" in script:
            self.cursors_asked.append(args[0])
            if self.status != 200:
                return {"status": self.status, "length": 0}
            if self.pages:
                has_next, cursor, ids = self.pages.pop(0)
                self.session._ingest_records([comment_page(
                    has_next=has_next, cursor=cursor, comments=ids)])
            return {"status": 200, "length": 1000}
        if "getBoundingClientRect" in script:
            return None
        return 1

    def wait_for_timeout(self, _ms):
        pass


class ThreadContinuationTests(SessionTestCase):
    """When scrolling no longer makes the page ask, the page asks anyway.

    Facebook's own client pages a thread with one request per page, the
    cursor from the last response naming the next. When scrolling and
    clicking stop producing that request while Facebook still reports a
    further page, the page is asked to repeat its own last request with
    the new cursor. The curator is troubled only when that fails too.
    """

    POST = "1593564465471991"

    def session(self, wanted=400, stated=433):
        session = make_session(self.tmp, page_url=POST_URL,
                               include_comments=True,
                               max_comments_per_post=wanted)
        session.CONTINUATION_PAUSE_SECONDS = (0, 0)
        item = post(self.POST, date="2026-05-01T09:00:00Z")
        item.comments_count = stated
        session._consider_post(item)
        session._show_all_comments = lambda _p: None
        session._process_media_queue = lambda budget=0: None
        session._scroll_comment_thread = lambda _p: None
        session._expand_comments = lambda _p: 0
        session._ensure_facebook_widgets = lambda: None
        return session

    def first_page(self, session, has_next=True):
        session._ingest_records([comment_page(
            has_next=has_next, cursor="C1", comments=("1", "2"))])

    def test_the_thread_is_continued_from_the_last_cursor_until_it_ends(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [
            (True, "C2", ("3", "4")), (True, "C3", ("5",)),
            (False, "C4", ("6",))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(page.cursors_asked, ["C1", "C2", "C3"])
        self.assertEqual(sorted(session.archive.comments), ["1", "2", "3", "4", "5", "6"])
        self.assertIsNone(session._continuation_outcome)
        self.assertEqual(session.counters["comment_pages_continued"], 3)
        self.assertTrue(session._thread_exhausted(self.POST))

    def test_continuation_stops_at_the_requested_count(self):
        session = self.session(wanted=4)
        self.first_page(session)
        page = _ContinuingPage(session, [
            (True, "C2", ("3", "4")), (True, "C3", ("5", "6"))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(page.cursors_asked, ["C1"])
        self.assertEqual(len(session.archive.comments), 4)

    def test_a_thread_read_to_its_end_is_not_held(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [(False, "C2", ("3",))])
        session._read_comment_thread(page, self.POST)

        session._capture_single_post(page)

        self.assertEqual(session._pending_stop[1],
                         "facebook_reported_no_further_comment_page")

    def test_a_page_that_never_asked_leaves_the_curator_a_way(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [], seen=False)

        session._capture_single_post(page)

        self.assertEqual(session._continuation_outcome, "no_request_observed")
        self.assertEqual(page.cursors_asked, [])
        self.assertEqual(session.state, PAUSED)
        self.assertIn("Load one more page of comments by hand",
                      session.phase_detail)
        self.assertIn("comment_continuation_impossible",
                      session.archive.events_path.read_text())

    def test_a_refused_continuation_is_held_and_said(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [], status=403)

        session._capture_single_post(page)

        self.assertEqual(session._continuation_outcome, "http_403")
        self.assertEqual(session.state, PAUSED)
        self.assertIn("Facebook answered HTTP 403", session.phase_detail)
        self.assertIn("1 by the page repeating its own request",
                      session.phase_detail)
        manifest = session._manifest_document()["requested_work"]["comments"]
        self.assertEqual(manifest["continuation_outcome"], "http_403")
        self.assertEqual(manifest["thread_pages_continued_by_swm"], 1)
        self.assertIn("not rendered on the page", manifest["continuation_note"])

    def test_a_page_that_adds_nothing_ends_the_continuation(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [
            (True, "C2", ("3",)), (True, "C3", ("3",)), (True, "C4", ("9",))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(session._continuation_outcome, "page_added_nothing")
        self.assertEqual(page.cursors_asked, ["C1", "C2"])

    def test_a_cursor_that_does_not_advance_ends_the_continuation(self):
        session = self.session()
        self.first_page(session)
        page = _ContinuingPage(session, [(True, "C1", ("3",))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(session._continuation_outcome, "cursor_did_not_advance")

    def test_a_resumed_read_continues_before_scrolling_again(self):
        session = self.session()
        self.first_page(session)
        rounds = {"n": 0}
        session._expand_comments = lambda _p: rounds.__setitem__(
            "n", rounds["n"] + 1) or 0
        page = _ContinuingPage(session, [(False, "C2", ("3",))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(page.cursors_asked, ["C1"])
        self.assertEqual(rounds["n"], 0)

    def test_nothing_is_continued_when_facebook_has_not_spoken(self):
        session = self.session()
        page = _ContinuingPage(session, [(False, "C2", ("3",))])

        session._read_comment_thread(page, self.POST)

        self.assertEqual(page.cursors_asked, [])
        self.assertEqual(session._continuation_outcome, "no_cursor")

    def test_the_pace_is_the_clients(self):
        self.assertEqual(FacebookCaptureSession.CONTINUATION_PAUSE_SECONDS,
                         (1.5, 2.5))

    def test_the_helper_is_installed_alongside_the_panel(self):
        from webarc.facebook import _FACEBOOK_THREAD_JS as helper

        self.assertIn("__swmContinueComments", helper)
        self.assertIn("__swmCommentRequestSeen", helper)
        self.assertIn("commentsAfterCursor", helper)
        self.assertIn('credentials: "include"', helper)


if __name__ == "__main__":
    unittest.main()
