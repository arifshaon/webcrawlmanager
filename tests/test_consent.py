"""Consent dismissal, driven against real pages in a real browser.

Reading this code cannot tell you whether it works: the failure mode is a
search that runs, finds nothing and reports success. So these run Chromium
against fixture pages built in the shapes consent platforms actually use --
a plain overlay, a known platform's markup, a shadow root, an iframe, a
scroll-locked modal, an Arabic banner -- and check the overlay is gone
afterwards, not merely that something was clicked.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from webarc.consent import (PREFER_ACCEPT, PREFER_DECLINE, dismiss_consent,
                            find_consent_control)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "consent"

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT = True
except ImportError:                                   # pragma: no cover
    _PLAYWRIGHT = False


@unittest.skipUnless(_PLAYWRIGHT, "playwright is not installed")
class ConsentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:                      # pragma: no cover
            cls._pw.stop()
            raise unittest.SkipTest(f"no chromium available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def open(self, name):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.goto((FIXTURES / name).as_uri(), wait_until="load")
        return page

    def dismiss(self, name, preference=PREFER_DECLINE):
        page = self.open(name)
        return page, dismiss_consent(page, preference, settle_ms=150)


class OverlayShapeTests(ConsentTestCase):
    """Each shape is a way for a naive search to find nothing at all."""

    def test_a_plain_fixed_overlay(self):
        page, record = self.dismiss("plain.html")

        self.assertTrue(record["clicked"])
        self.assertTrue(record["dismissed"])
        self.assertEqual(page.locator("#cc").count(), 0)

    def test_a_known_platforms_markup(self):
        page, record = self.dismiss("onetrust.html")

        self.assertEqual(record["kind"], "known_decline")
        self.assertTrue(record["dismissed"])

    def test_a_banner_inside_a_shadow_root(self):
        """A document query never reaches these, and reports nothing wrong."""
        page, record = self.dismiss("shadow.html")

        self.assertTrue(record["clicked"], record)
        self.assertTrue(record["dismissed"])

    def test_a_banner_inside_an_iframe(self):
        page, record = self.dismiss("iframe.html")

        self.assertTrue(record["clicked"], record)
        self.assertGreater(record["frames"], 1)

    def test_a_modal_that_locks_the_page_behind_it(self):
        page, record = self.dismiss("scroll-locked.html")

        self.assertTrue(record["clicked"], record)
        self.assertEqual(page.locator("#m").count(), 0)

    def test_a_banner_that_is_not_in_english(self):
        """A collection spanning Arabic sites needs the Arabic wording."""
        page, record = self.dismiss("arabic.html")

        self.assertTrue(record["clicked"], record)
        self.assertEqual(record["label"], "رفض الكل")
        self.assertTrue(record["dismissed"])


class WhatGetsClickedTests(ConsentTestCase):
    """What is clicked is a curatorial act, not a technicality."""

    def test_refusal_is_taken_over_acceptance_by_default(self):
        _, record = self.dismiss("plain.html")

        self.assertEqual(record["label"], "Reject all")

    def test_acceptance_can_be_asked_for_instead(self):
        _, record = self.dismiss("plain.html", PREFER_ACCEPT)

        self.assertEqual(record["label"], "Accept all cookies")

    def test_a_known_platform_is_preferred_to_reading_labels(self):
        """Its controls are stable; the words on them are not."""
        _, record = self.dismiss("onetrust.html")

        self.assertEqual(record["kind"], "known_decline")
        self.assertEqual(record["label"], "Continue")


class RestraintTests(ConsentTestCase):
    """A button saying "I agree" is not necessarily a consent control."""

    def test_an_ordinary_form_is_left_alone(self):
        page, record = self.dismiss("ordinary-form.html")

        self.assertFalse(record["clicked"])
        self.assertEqual(page.locator("#agree").count(), 1)

    def test_what_was_on_offer_is_reported_when_nothing_matched(self):
        _, record = self.dismiss("ordinary-form.html")

        self.assertIn("I agree", record["labels"])


class HonestReportingTests(ConsentTestCase):
    """A click is not a dismissal."""

    def test_a_banner_that_ignores_the_click_is_reported_not_assumed_away(self):
        page, record = self.dismiss("stubborn.html")

        self.assertTrue(record["clicked"])
        self.assertFalse(record["dismissed"])
        self.assertEqual(page.locator("#cc").count(), 1)

    def test_a_page_with_no_banner_reports_nothing_clicked(self):
        _, record = self.dismiss("ordinary-form.html")

        self.assertFalse(record["clicked"])
        self.assertIsNone(record["kind"])
        self.assertIsNone(record["error"])

    def test_looking_does_not_click(self):
        page = self.open("plain.html")

        found = find_consent_control(page.main_frame)

        self.assertEqual(found["kind"], "decline")
        self.assertEqual(page.locator("#cc").count(), 1)


if __name__ == "__main__":
    unittest.main()
