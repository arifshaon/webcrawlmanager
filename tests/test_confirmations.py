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
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        while not cls.server.started:
            time.sleep(0.05)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=chrome)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)      # stopped before the next test sets up its own dashboard
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

    def menu(self, collection_id, label):
        """Choose an item from a collection card's ⋮ menu."""
        card = self.page.locator(f".ccard[data-id='{collection_id}']")
        card.wait_for()
        card.locator(".ccard-more summary").click()
        card.locator(f".ccard-more .menu button:has-text('{label}')").click()

    def new_collection(self):
        self.page.goto(self.url("#/collections"))
        self.page.click("#c-new-btn")
        self.page.locator("#collection-overlay").wait_for(state="visible")

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
        self.new_collection()
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
        page.wait_for_selector("#coll-page-msg:has-text('Collection \"Asked first\" created.')")
        self.assertFalse(page.is_visible("#collection-overlay"))          # the pop-up closes
        made = next(c for c in self.api("/api/collections") if c["name"] == "Asked first")
        self.assertTrue(Path(made["root_dir"]).is_dir())

        self.menu(made["id"], "Delete")
        dialog = self.dialog()
        # a deletion starts on Cancel, and leaves the files unless ticked
        self.assertEqual(page.evaluate("document.activeElement.id"), "confirm-no")
        self.assertFalse(page.is_checked("#confirm-check"))
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        self.assertIn("Asked first", self.names())

        self.menu(made["id"], "Delete")
        self.dialog()
        page.click("#confirm-yes")
        page.wait_for_selector(f".ccard[data-id='{made['id']}']", state="detached")
        self.assertNotIn("Asked first", self.names())
        self.assertTrue(Path(made["root_dir"]).is_dir())      # unticked: the folder stays

    def test_a_name_already_taken_is_refused_before_any_confirmation(self):
        page = self.page
        self.api("/api/collections", {"name": "QNL Web"}, "POST")
        self.new_collection()
        page.fill("#c-name", "qnl  web!")                    # the identifier qnl-web again
        page.click("#c-create-btn")
        page.wait_for_function("document.querySelector('#c-msg').textContent.includes('already exists')")
        self.assertEqual(page.text_content("#c-msg"),
                         "Sorry, a collection with the identifier 'qnl-web' already exists. Choose another name.")
        self.assertFalse(page.locator("#confirm-overlay").is_visible())
        self.assertEqual(page.evaluate("document.activeElement.id"), "c-name")
        self.assertEqual(self.names().count("QNL Web"), 1)

    def test_editing_a_collection_checks_the_name_and_confirms_the_changes(self):
        page = self.page
        self.api("/api/collections", {"name": "Election 2026"}, "POST")
        mine = self.api("/api/collections", {"name": "Library news"}, "POST")
        page.goto(self.url("#/collections"))
        self.menu(mine["id"], "Edit")
        self.assertEqual(page.text_content("#collection-title"), "Edit collection: Library news")
        # the folder is fixed: no field or Browse to change it, even after the page refreshes
        page.wait_for_timeout(2500)
        self.assertFalse(page.is_visible("#c-storage"))
        self.assertFalse(page.is_visible(".browse-btn[data-target='c-storage']"))
        self.assertIn(mine["root_dir"], page.inner_text("#c-storage-fixed"))
        self.assertIn("create a new collection with that storage location", page.inner_text("#c-storage-fixed"))

        page.fill("#c-name", "election-2026")            # another collection's identifier
        page.click("#c-create-btn")
        page.wait_for_function("document.querySelector('#c-msg').textContent.includes('already has that name')")
        self.assertFalse(page.locator("#confirm-overlay").is_visible())

        page.fill("#c-name", "Library news, Qatar")
        page.click("#c-create-btn")
        dialog = self.dialog()
        self.assertIn("Library news → Library news, Qatar", dialog.text_content())
        self.assertIn(mine["root_dir"], dialog.text_content())
        page.click("#confirm-no")
        self.assertIn("Library news", self.names())
        page.click("#c-create-btn")
        self.dialog()
        page.click("#confirm-yes")
        page.wait_for_selector("#coll-page-msg:has-text('updated')")
        self.assertIn("Library news, Qatar", self.names())
        page.click("#c-new-btn")
        self.assertEqual(page.text_content("#collection-title"), "New collection")
        self.assertTrue(page.is_visible("#c-storage"))         # a new collection: the field returns

    def test_the_collections_page_finds_sorts_and_keeps_its_menu_open(self):
        page = self.page
        busy = self.api("/api/collections", {"name": "Zanzibar archive", "description": "Coastal towns"}, "POST")
        self.api("/api/collections", {"name": "Alpha sites"}, "POST")
        page.goto(self.url("#/collections"))
        page.wait_for_selector(f".ccard[data-id='{busy['id']}']")
        names = lambda: page.eval_on_selector_all(".ccard .ccard-name", "els => els.map(e => e.textContent)")

        page.select_option("#coll-sort", "name")
        listed = names()
        self.assertEqual(listed, sorted(listed, key=str.lower))
        self.assertLess(listed.index("Alpha sites"), listed.index("Zanzibar archive"))
        page.fill("#coll-search", "coastal")                  # the description is searched too
        self.assertEqual(names(), ["Zanzibar archive"])
        page.fill("#coll-search", "")
        page.select_option("#coll-filter", "running")
        self.assertIn("No collection matches", page.text_content("#collection-list"))
        page.click("#collection-list .linkish")               # Show all collections
        self.assertGreaterEqual(len(names()), 2)
        page.click(".coll-view[data-view=grid]")
        self.assertEqual(page.get_attribute("#collection-list", "data-view"), "grid")
        page.reload()
        page.wait_for_selector(".ccard")
        self.assertEqual(page.get_attribute("#collection-list", "data-view"), "grid")   # kept in this browser
        self.assertEqual(page.input_value("#coll-sort"), "name")

        card = page.locator(f".ccard[data-id='{busy['id']}']")
        card.locator(".ccard-more summary").click()
        page.wait_for_timeout(2500)                           # a refresh comes and goes
        self.assertTrue(card.locator(".ccard-more").evaluate("d => d.open"))
        page.keyboard.press("Escape")
        self.assertFalse(card.locator(".ccard-more").evaluate("d => d.open"))
        # an empty collection says so, rather than "not calculated"
        self.assertIn("None yet", card.inner_text())
        self.assertIn("Not indexed", card.inner_text())

    def test_a_size_still_being_measured_says_so(self):
        page = self.page
        page.goto(self.url("#/collections"))
        page.wait_for_function("typeof collCard === 'function'")
        html = page.evaluate("""() => collCard({id: 9, name: "Big", slug: "big", root_dir: "/x", jobs: 0, by_status: {},
            bytes: null, bytes_measuring: true, metadata_fields: 0, policy: {}, index: null, warc_index: null})""")
        self.assertIn("Calculating…", html)
        html = page.evaluate("""() => collCard({id: 9, name: "Big", slug: "big", root_dir: "/x", jobs: 0, by_status: {},
            bytes: 2048, bytes_measuring: true, metadata_fields: 0, policy: {}, index: null, warc_index: null})""")
        self.assertIn("2.0 KB", html)
        self.assertIn("recalculating", html)

    def test_a_settings_save_waits_for_a_yes(self):
        page = self.page
        page.goto(self.url("#/settings"))
        before = self.api("/api/settings")["effective_storage_root"]
        page.fill("#storage-root", str(self.root / "elsewhere"))
        page.click("#storage-save")
        dialog = self.dialog()
        self.assertIn("Nothing is moved", dialog.text_content())
        page.click("#confirm-no")
        self.assertEqual(self.api("/api/settings")["effective_storage_root"], before)

    def test_new_collection_beside_a_picker_asks_its_name_in_the_dialog(self):
        page = self.page
        self.api("/api/collections", {"name": "Taken"}, "POST")
        page.goto(self.url("#/new"))
        page.click('[data-job="crawl"]')
        page.click(".collection-new-btn[data-target='f-collection']")
        self.dialog()
        page.fill("#confirm-input", "TAKEN!")             # another spelling, the same identifier
        page.click("#confirm-yes")
        page.wait_for_function("document.querySelector('#confirm-msg').textContent.includes('already exists')")
        self.assertIn("Choose another name", page.text_content("#confirm-msg"))
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
        # what this machine has free is not this test's question: the check says there is room
        page.route("**/api/resources/check*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok": true, "warnings": []}'))
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
