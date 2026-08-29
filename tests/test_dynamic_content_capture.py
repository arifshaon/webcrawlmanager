"""Regressions for capturing sites that load their records dynamically
(search portals / infinite-scroll repositories, e.g. Figshare-based ones):
late XHR bodies must not be silently archived empty, WAF challenge verdicts
must be surfaced, and infinite-scroll walks must terminate.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from webarc.config import BehaviorConfig, BrowserConfig
from webarc.crawler import PageCapture
from webarc.detect import detect_block, is_waf_challenge


class DummyWarc:
    def __init__(self):
        self.writes: list[dict] = []
        self.total_bytes = 0

    def write_exchange(self, **kwargs):
        self.writes.append(kwargs)
        self.total_bytes += len(kwargs.get("body") or b"")

    def close(self):
        pass


class FakeRequest:
    """Identity-hashable stand-in for Playwright's Request object."""

    def __init__(self, *, method="GET", headers=None, post_data_buffer=None,
                 resource_type="xhr"):
        self.method = method
        self.headers = headers or {}
        self.post_data_buffer = post_data_buffer
        self.resource_type = resource_type


def make_response(request, *, url="https://example.org/api/search",
                  status=200, headers=None, body=b"{}",
                  body_error: Exception | None = None):
    def read_body():
        if body_error is not None:
            raise body_error
        return body

    return SimpleNamespace(
        request=request,
        url=url,
        status=status,
        status_text="OK",
        headers=headers if headers is not None
        else {"content-type": "application/json"},
        body=read_body,
    )


class PageCaptureTests(unittest.TestCase):
    def make_capture(self, warc=None):
        return PageCapture(warc or DummyWarc(), driver=None)

    def test_post_exchange_is_written_with_body_and_post_data(self):
        warc = DummyWarc()
        capture = self.make_capture(warc)
        request = FakeRequest(method="POST",
                              post_data_buffer=b'{"page":1}')
        capture.on_response(make_response(request, body=b'{"items":[1]}'))

        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["method"], "POST")
        self.assertEqual(warc.writes[0]["post_data"], b'{"page":1}')
        self.assertEqual(warc.writes[0]["body"], b'{"items":[1]}')

    def test_late_body_is_retried_at_requestfinished(self):
        """A body unreadable at the response event (streaming, or a scroll-
        triggered XHR still in flight) must be retried once the transfer
        finishes — not archived as an empty 200."""
        warc = DummyWarc()
        capture = self.make_capture(warc)
        request = FakeRequest()
        response = make_response(request, body=b'{"items":[1,2,3]}',
                                 body_error=RuntimeError("body not ready"))
        capture.on_response(response)
        self.assertEqual(len(warc.writes), 0)  # deferred, nothing written yet

        response.body = lambda: b'{"items":[1,2,3]}'
        capture.on_request_finished(request)

        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["body"], b'{"items":[1,2,3]}')
        self.assertNotIn("body-unavailable", capture.take_page_report())

    def test_unreadable_body_is_counted_not_silent(self):
        warc = DummyWarc()
        capture = self.make_capture(warc)
        request = FakeRequest()
        response = make_response(request,
                                 body_error=RuntimeError("gone"))
        capture.on_response(response)
        capture.on_request_finished(request)

        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["body"], b"")
        report = capture.take_page_report()
        self.assertEqual(report.get("body-unavailable"), 1)

    def test_cancelled_inflight_response_is_counted(self):
        capture = self.make_capture()
        request = FakeRequest()
        capture.on_response(make_response(
            request, body_error=RuntimeError("still streaming")))
        capture.on_request_failed(request)

        self.assertEqual(capture.take_page_report().get("lost-inflight"), 1)

    def test_redirects_are_written_immediately_with_empty_body(self):
        warc = DummyWarc()
        capture = self.make_capture(warc)
        request = FakeRequest(resource_type="document")
        capture.on_response(make_response(
            request, status=302, headers={"location": "/next"},
            body_error=RuntimeError("redirects have no body")))

        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["body"], b"")
        self.assertEqual(capture.take_page_report(), {})

    def test_waf_challenge_verdict_is_flagged(self):
        """AWS WAF answers challenged API calls with HTTP 202 +
        x-amzn-waf-action and an empty body; archiving that as content is
        exactly how 'live shows 13,275 posts, replay shows 0' happens."""
        warc = DummyWarc()
        capture = self.make_capture(warc)
        request = FakeRequest(method="POST")
        capture.on_response(make_response(
            request, status=202,
            headers={"x-amzn-waf-action": "challenge",
                     "content-type": "text/html; charset=UTF-8"},
            body=b""))

        self.assertEqual(len(warc.writes), 1)  # still archived faithfully
        report = capture.take_page_report()
        self.assertEqual(report.get("waf-challenged"), 1)

    def test_failed_api_subresource_is_flagged(self):
        capture = self.make_capture()
        request = FakeRequest(method="POST")
        capture.on_response(make_response(request, status=403))
        self.assertEqual(
            capture.take_page_report().get("subresource-error"), 1)

    def test_page_report_resets_between_pages(self):
        capture = self.make_capture()
        request = FakeRequest(method="POST")
        capture.on_response(make_response(request, status=500))
        self.assertTrue(capture.take_page_report())
        self.assertEqual(capture.take_page_report(), {})


class WafDetectionTests(unittest.TestCase):
    def test_header_verdict_detected(self):
        self.assertTrue(is_waf_challenge({"x-amzn-waf-action": "challenge"}))
        self.assertFalse(is_waf_challenge({"content-type": "text/html"}))
        self.assertFalse(is_waf_challenge(None))

    def test_detect_block_via_waf_header(self):
        blocked, reason = detect_block(
            202, "", "", headers={"x-amzn-waf-action": "challenge"})
        self.assertTrue(blocked)
        self.assertIn("x-amzn-waf-action", reason)

    def test_detect_block_via_aws_waf_interstitial_markers(self):
        html = ('<html><head><script src="https://x.token.awswaf.com/x/'
                'challenge.js"></script><script>window.gokuProps={}</script>')
        blocked, reason = detect_block(202, "", html)
        self.assertTrue(blocked)
        self.assertIn("signature", reason)

    def test_ordinary_page_not_blocked(self):
        blocked, _ = detect_block(200, "Repository", "<html>13,275 posts")
        self.assertFalse(blocked)


class FakeScrollPage:
    """Emulates an infinite-scroll page: scrollHeight grows every probe."""

    def __init__(self, viewport=800):
        self.viewport = viewport
        self.height = viewport * 3
        self.scroll_calls = 0

    def evaluate(self, script):
        if "innerHeight" in script:
            return self.viewport
        if "scrollHeight" in script:
            self.height += self.viewport * 2  # feed keeps growing
            return self.height
        if "scrollTo" in script:
            self.scroll_calls += 1
            return None
        return None


class ScrollBudgetTests(unittest.TestCase):
    def make_driver(self, **behavior_overrides):
        from webarc.browser import BrowserDriver
        behavior = BehaviorConfig(scroll_pause=(0.0, 0.0),
                                  **behavior_overrides)
        return BrowserDriver(BrowserConfig(), behavior)

    def test_infinite_scroll_terminates_at_budget(self):
        driver = self.make_driver(scroll_max_screens=5)
        page = FakeScrollPage()
        driver._human_scroll(page)
        # 0.6..0.95 viewport per step: the budget of 5 screens allows at most
        # ceil(5 / 0.6) + 1 = 10 scroll steps (plus the final scroll-to-top)
        self.assertLessEqual(page.scroll_calls, 11)

    def test_zero_budget_means_unlimited_but_finite_page_completes(self):
        driver = self.make_driver(scroll_max_screens=0)

        class FinitePage(FakeScrollPage):
            def evaluate(self, script):
                if "scrollHeight" in script:
                    return self.viewport * 3  # fixed-height page
                return super().evaluate(script)

        page = FinitePage()
        driver._human_scroll(page)
        self.assertGreaterEqual(page.scroll_calls, 3)


class FakeChallengePage:
    """Serves the AWS WAF interstitial for the first N content polls, then
    the real (large) page — like the live auto-solve-and-reload flow."""

    INTERSTITIAL = ('<html><script src="https://x.token.awswaf.com/x/'
                    'challenge.js"></script></html>')
    REAL = "<html>" + "records " * 8000 + "</html>"

    def __init__(self, polls_until_clear=3):
        self.polls_until_clear = polls_until_clear
        self.polls = 0
        self.url = "https://portal.example.org/"
        self.idle_waits = 0

    def content(self):
        self.polls += 1
        if self.polls > self.polls_until_clear:
            return self.REAL
        return self.INTERSTITIAL

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_load_state(self, *_a, **_k):
        self.idle_waits += 1


class ChallengeWaitTests(unittest.TestCase):
    def make_driver(self, **behavior_overrides):
        from webarc.browser import BrowserDriver
        return BrowserDriver(BrowserConfig(),
                             BehaviorConfig(**behavior_overrides))

    def test_waits_until_interstitial_clears(self):
        driver = self.make_driver(challenge_grace=10)
        page = FakeChallengePage(polls_until_clear=3)
        resp = SimpleNamespace(headers={"x-amzn-waf-action": "challenge"})

        seen = driver._wait_out_challenge(page, resp)

        self.assertTrue(seen)
        self.assertGreater(page.polls, 3)       # kept polling past clearance
        self.assertEqual(page.idle_waits, 1)    # then let late XHRs settle

    def test_no_challenge_returns_immediately(self):
        driver = self.make_driver(challenge_grace=10)
        page = FakeChallengePage(polls_until_clear=0)
        resp = SimpleNamespace(headers={"content-type": "text/html"})
        # first content() poll already returns the real page
        self.assertFalse(driver._wait_out_challenge(page, resp))

    def test_real_page_embedding_waf_sdk_is_not_an_interstitial(self):
        driver = self.make_driver(challenge_grace=10)

        class SdkPage(FakeChallengePage):
            def content(self):
                self.polls += 1
                # a large real page that references the WAF SDK permanently
                return ('<html><script src="https://x.token.awswaf.com/x/'
                        'challenge.js"></script>' + "records " * 8000)

        page = SdkPage()
        resp = SimpleNamespace(headers={"content-type": "text/html"})
        self.assertFalse(driver._wait_out_challenge(page, resp))

    def test_grace_zero_disables_wait(self):
        driver = self.make_driver(challenge_grace=0)
        page = FakeChallengePage()
        resp = SimpleNamespace(headers={"x-amzn-waf-action": "challenge"})
        self.assertFalse(driver._wait_out_challenge(page, resp))
        self.assertEqual(page.polls, 0)


class ReplaySiteFilterTests(unittest.TestCase):
    """The replay copy must not contain the WAF challenge SDK or challenge
    verdicts: replayed, the SDK's fresh token calls match nothing in the
    archive and the archived application hangs on it, and a 202 interstitial
    can shadow the real 200 document captured for the same URL."""

    def _write_source_warc(self, out_dir):
        from webarc.capture import WarcSession
        from webarc.config import WarcConfig

        warc = WarcSession(out_dir, "t", "https://portal.example.org/", 1,
                           "op", WarcConfig())
        # the challenge interstitial served first for the page URL
        warc.write_exchange(
            url="https://portal.example.org/", method="GET", req_headers={},
            post_data=None, status=202, status_text="",
            resp_headers={"x-amzn-waf-action": "challenge",
                          "content-type": "text/html"},
            body=b"<html>challenge</html>")
        # the WAF SDK and its token calls
        warc.write_exchange(
            url="https://abc.def.eu-west-1.token.awswaf.com/abc/challenge.js",
            method="GET", req_headers={}, post_data=None, status=200,
            status_text="OK",
            resp_headers={"content-type": "text/javascript"},
            body=b"// sdk")
        # the real page and its API data
        warc.write_exchange(
            url="https://portal.example.org/", method="GET", req_headers={},
            post_data=None, status=200, status_text="OK",
            resp_headers={"content-type": "text/html"},
            body=b"<html>real page</html>")
        warc.write_exchange(
            url="https://portal.example.org/api/graphql?operation=search",
            method="GET", req_headers={}, post_data=None, status=200,
            status_text="OK",
            resp_headers={"content-type": "application/json"},
            body=b'{"items":[1,2,3]}')
        warc.close()
        return sorted(out_dir.glob("*.warc.gz"))

    def test_challenge_records_are_excluded_from_replay_copy(self):
        import tempfile

        from warcio.archiveiterator import ArchiveIterator

        from webarc.replay import build_replay_site

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            warcs = self._write_source_warc(tmp / "warcs")
            site = build_replay_site(warcs, tmp / "site")

            kept = []
            archive = next(site.glob("archive-*.warc.gz"))
            with open(archive, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response":
                        continue
                    kept.append((
                        record.rec_headers.get_header("WARC-Target-URI"),
                        record.http_headers.get_statuscode(),
                    ))

            urls = [u for u, _ in kept]
            self.assertNotIn(
                "https://abc.def.eu-west-1.token.awswaf.com/abc/challenge.js",
                urls)
            # the 202 verdict for the page URL is gone; the real 200 stays
            self.assertEqual(
                [s for u, s in kept
                 if u == "https://portal.example.org/"], ["200"])
            self.assertIn(
                "https://portal.example.org/api/graphql?operation=search",
                urls)
            # the source WARC is untouched: all four responses still there
            with open(warcs[0], "rb") as fh:
                originals = sum(
                    1 for r in ArchiveIterator(fh)
                    if r.rec_type == "response")
            self.assertEqual(originals, 4)


class ConfigDefaultsTests(unittest.TestCase):
    def test_new_behavior_defaults_present(self):
        behavior = BehaviorConfig()
        self.assertGreater(behavior.scroll_max_screens, 0)
        self.assertGreater(behavior.challenge_grace, 0)

    def test_example_config_documents_new_keys(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("scroll_max_screens", text)
        self.assertIn("challenge_grace", text)


if __name__ == "__main__":
    unittest.main()
