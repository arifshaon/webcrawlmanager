"""Nothing starts, is created or is deleted without a yes; and a browser that
has not chosen a theme shows the default one, Nebula.

Driven in a real browser against a running dashboard: the dialog is the
page's own, so only a page can show that Cancel leaves everything as it was.
"""
from __future__ import annotations

import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "webarc" / "dashboard.html"


class NoNativeDialogTests(unittest.TestCase):
    def test_every_question_goes_through_the_dashboards_own_dialog(self):
        script = DASHBOARD.read_text(encoding="utf-8").split("<script>", 1)[1]
        self.assertEqual(re.findall(r"(?<![\w.])(confirm|prompt)\(", script), [])
        for start in ('confirmStart("f"', 'confirmStart("r"', 'confirmStart("fb"',
                      'confirmStart("ig"', 'confirmStart("x"', 'confirmStart("yt"'):
            with self.subTest(start):
                self.assertIn(start, script)


class ConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import uvicorn
            from playwright.sync_api import sync_playwright
        except ImportError as exc:                          # pragma: no cover
            raise unittest.SkipTest(f"browser test dependencies missing: {exc}")
        from tests.chrome_for_tests import find_chrome
        from webarc.server import create_app
        chrome = find_chrome()
        if not chrome:                                      # pragma: no cover
            raise unittest.SkipTest("no Chrome to drive")
        cls._dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls._dir.name)
        app = create_app(str(cls.root / "swm.db"), str(cls.root / "warcs"),
                         replay_root=str(cls.root / "replay"), monitor_resources=False)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        cls.port = probe.getsockname()[1]
        probe.close()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="error"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        while not cls.server.started:
            time.sleep(0.05)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=chrome)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.should_exit = True
        cls._dir.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)

    def url(self, route=""):
        return f"http://127.0.0.1:{self.port}/{route}"

    def api(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path.lstrip("/")), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as reply:
            return json.loads(reply.read() or b"null")

    def names(self):
        return sorted(c["name"] for c in self.api("/api/collections"))

    def dialog(self):
        overlay = self.page.locator("#confirm-overlay")
        overlay.wait_for(state="visible")
        return overlay

    def test_a_fresh_browser_shows_nebula_and_a_chosen_theme_is_kept(self):
        self.page.goto(self.url("#/overview"))
        self.page.wait_for_function("document.querySelectorAll('#ui-theme-select option').length > 1")
        root = self.page.locator("html")
        self.assertEqual(root.get_attribute("data-ui-theme"), "nebula")
        self.assertEqual(root.get_attribute("data-layout"), "board")
        self.assertEqual(self.page.input_value("#ui-theme-select"), "nebula")
        self.assertIn("(default)", self.page.text_content("#ui-theme-select option[value=nebula]"))

        self.page.evaluate("""localStorage.setItem('swm.appearance', JSON.stringify(
            {uiTheme: 'default', iconStyle: 'mono', iconSize: 'normal', layout: 'classic', font: 'system'}))""")
        self.page.reload()
        self.page.wait_for_function("document.querySelectorAll('#ui-theme-select option').length > 1")
        self.assertEqual(root.get_attribute("data-ui-theme"), "default")
        self.assertIsNone(root.get_attribute("data-layout"))

    def test_creating_and_deleting_a_collection_each_wait_for_a_yes(self):
        page = self.page
        page.goto(self.url("#/collections"))
        page.fill("#c-name", "Asked first")
        page.wait_for_function("document.querySelector('#c-saves-to code')")
        folder = page.text_content("#c-saves-to code")
        page.click("#c-create-btn")
        dialog = self.dialog()
        self.assertIn('Create the collection "Asked first"?', dialog.text_content())
        self.assertIn(folder, dialog.locator(".confirm-facts").text_content())
        page.click("#confirm-no")
        self.assertNotIn("Asked first", self.names())

        page.click("#c-create-btn")
        self.dialog()
        page.click("#confirm-yes")
        page.wait_for_selector("#c-msg:has-text('Collection created.')")
        made = next(c for c in self.api("/api/collections") if c["name"] == "Asked first")
        self.assertTrue(Path(made["root_dir"]).is_dir())

        delete = f"#collection-list button[onclick='delCollection({made['id']})']"
        page.wait_for_selector(delete)
        page.click(delete)
        dialog = self.dialog()
        # a deletion starts on Cancel, and leaves the files unless ticked
        self.assertEqual(page.evaluate("document.activeElement.id"), "confirm-no")
        self.assertFalse(page.is_checked("#confirm-check"))
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        self.assertIn("Asked first", self.names())

        page.click(delete)
        self.dialog()
        page.click("#confirm-yes")
        page.wait_for_function("() => !document.querySelector(\"" + delete.replace('"', '\\"') + "\")")
        self.assertNotIn("Asked first", self.names())
        self.assertTrue(Path(made["root_dir"]).is_dir())      # unticked: the folder stays

    def test_new_collection_beside_a_picker_asks_its_name_in_the_dialog(self):
        page = self.page
        self.api("/api/collections", {"name": "Taken"}, "POST")
        page.goto(self.url("#/new"))
        page.click('[data-job="crawl"]')
        page.click(".collection-new-btn[data-target='f-collection']")
        self.dialog()
        page.fill("#confirm-input", "taken")
        page.click("#confirm-yes")
        self.assertIn("exists", page.text_content("#confirm-msg"))
        page.fill("#confirm-input", "Picked here")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#f-collection').selectedOptions[0].textContent.includes('Picked here')")
        self.assertIn("Picked here", self.names())

    def test_starting_a_crawl_shows_what_and_where_and_cancel_starts_nothing(self):
        page = self.page
        posted = []
        page.route("**/api/crawls", lambda route: (
            posted.append(route.request.post_data_json),
            route.fulfill(status=200, content_type="application/json", body='{"id": 1}'))
            if route.request.method == "POST" else route.continue_())
        page.goto(self.url("#/new"))
        page.click('[data-job="crawl"]')
        page.fill("#seed-list .seed-url", "https://example.org/")
        page.wait_for_function("document.querySelector('#f-saves-to code')")
        page.wait_for_timeout(600)          # a person's pause between typing and clicking
        page.click("#start-btn")
        dialog = self.dialog()
        text = dialog.text_content()
        self.assertIn("Start this crawl?", text)
        self.assertIn("https://example.org/", text)
        self.assertIn(page.text_content("#f-saves-to code"), text)
        self.assertEqual(page.text_content("#confirm-yes"), "Start crawl")
        page.click("#confirm-no")
        self.assertEqual(page.text_content("#form-msg"), "Cancelled.")
        self.assertEqual(posted, [])

        page.click("#start-btn")
        self.dialog()
        page.click("#confirm-yes")
        page.wait_for_function("document.querySelector('#form-msg').textContent.includes('started')")
        self.assertEqual(len(posted), 1)


if __name__ == "__main__":
    unittest.main()
