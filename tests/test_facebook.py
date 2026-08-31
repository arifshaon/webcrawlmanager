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
from webarc.facebook import (FacebookCaptureConfig, FacebookCaptureSession,
                             FacebookPost, canonical_facebook_page_url,
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
