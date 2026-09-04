"""The dashboard is one HTML file its own script drives by element id.

Moving markup between sections is the change most likely to leave a
`$("#thing")` pointing at nothing, and the page fails silently when it does:
the click handler is never bound, or the render throws into a catch. These
checks are cheap and catch that without a browser.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "webarc" / "dashboard.html"
HARDENING = DASHBOARD.parent / "dashboard_hardening.js"


class DashboardTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")
        cls.script = cls.html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        cls.markup = cls.html.split("<script>", 1)[0]
        cls.ids = set(re.findall(r'\bid="([^"]+)"', cls.markup))

    def views(self):
        return re.findall(r'<section class="view( hidden)?" id="view-([^"]+)"',
                          self.markup)

    def section(self, name):
        """The markup of one page of the dashboard."""
        start = self.markup.index(f'id="view-{name}"')
        following = [self.markup.index(f'id="view-{other}"')
                     for _, other in self.views()
                     if other != name
                     and self.markup.index(f'id="view-{other}"') > start]
        return self.markup[start:min(following)] if following \
            else self.markup[start:]


class ElementReferenceTests(DashboardTestCase):
    def referenced_ids(self, source):
        """Ids the script looks up literally, ignoring built-up selectors."""
        found = set()
        for selector in re.findall(r'\$\$?\("#([^"]+)"\)', source):
            if "$" in selector or "{" in selector:
                continue          # built from a variable; nothing to check
            # "#new-form .mode-tab" anchors on the id; the rest is a descendant
            found.add(re.split(r"[\s.:\[>]", selector, 1)[0])
        return found

    def test_every_id_the_page_script_uses_exists(self):
        missing = sorted(self.referenced_ids(self.script) - self.ids)

        self.assertEqual(missing, [])

    def test_every_id_the_hardening_script_uses_exists(self):
        source = HARDENING.read_text(encoding="utf-8")
        missing = sorted(self.referenced_ids(source) - self.ids)

        self.assertEqual(missing, [])

    def test_labels_point_at_fields_that_exist(self):
        targets = set(re.findall(r'<label[^>]+for="([^"]+)"', self.markup))
        missing = sorted(targets - self.ids)

        self.assertEqual(missing, [])


class NavigationTests(DashboardTestCase):
    """One page per job of work: set one up, watch them run, change settings."""

    def test_the_sidebar_names_a_section_that_exists(self):
        named = set(re.findall(r'class="nav-item[^"]*" data-view="([^"]+)"',
                               self.markup))
        present = {name for _, name in self.views()}

        self.assertTrue(named)
        self.assertEqual(named - present, set())

    def test_every_section_is_reachable_from_the_sidebar(self):
        named = set(re.findall(r'class="nav-item[^"]*" data-view="([^"]+)"',
                               self.markup))
        present = {name for _, name in self.views()}

        self.assertEqual(present - named, set())

    def test_exactly_one_section_starts_visible(self):
        visible = [name for hidden, name in self.views() if not hidden]

        self.assertEqual(visible, ["jobs"])

    def test_the_visible_section_is_the_one_the_sidebar_marks(self):
        active = re.findall(r'class="nav-item active" data-view="([^"]+)"',
                            self.markup)

        self.assertEqual(active, ["jobs"])

    def test_the_job_list_and_the_forms_are_not_on_the_same_page(self):
        """The reason for the split: a long list pushed the forms off screen."""
        jobs = self.section("jobs")
        new = self.section("new")

        self.assertIn('id="manifest"', jobs)
        self.assertNotIn('id="manifest"', new)
        self.assertIn('id="new-form"', new)
        self.assertNotIn('id="new-form"', jobs)

    def test_the_global_settings_live_on_their_own_page(self):
        settings = self.section("settings")

        self.assertIn('id="storage-root"', settings)
        self.assertNotIn('id="storage-root"', self.section("jobs"))



class DisabledJobTabTests(DashboardTestCase):
    """A disabled tab has to say why where it can be seen."""

    def test_the_reason_goes_to_a_note_outside_the_hidden_forms(self):
        self.assertIn('id="job-cap-note"', self.markup)
        tabs = self.markup.index('class="mode-tabs job-tabs"')
        note = self.markup.index('id="job-cap-note"')
        first_form = self.markup.index('id="new-form"')
        self.assertLess(tabs, note)
        self.assertLess(note, first_form)

    def test_every_job_type_is_covered(self):
        start = self.script.index("function switchJob(")
        body = self.script[start:self.script.index("\n}", start)]
        for job in ("record", "facebook", "instagram"):
            with self.subTest(job=job):
                self.assertIn(f"{job}:", body)
        self.assertIn('$("#job-cap-note")', body)


class StorageFieldTests(DashboardTestCase):
    def test_every_job_form_can_name_its_own_location(self):
        fields = re.findall(r'id="([a-z-]+)" class="storage-dir"', self.markup)

        self.assertEqual(sorted(fields), ["f-storage", "fb-storage",
                                          "ig-storage", "r-storage"])

    def test_every_storage_field_has_a_browse_button(self):
        """A path typed from memory is a path typed wrong."""
        fields = set(re.findall(r'id="([a-z-]+)" class="storage-dir"',
                                self.markup))
        fields.add("storage-root")
        browsed = set(re.findall(r'class="secondary browse-btn" data-target="([^"]+)"',
                                 self.markup))

        self.assertEqual(fields - browsed, set())

    def test_every_browse_button_points_at_a_real_field(self):
        browsed = set(re.findall(r'browse-btn" data-target="([^"]+)"',
                                 self.markup))

        self.assertTrue(browsed)
        self.assertEqual(browsed - self.ids, set())

    def test_choosing_a_folder_counts_as_editing_the_field(self):
        """The settings field is redrawn every two seconds; a path chosen
        with the mouse leaves it unfocused and was wiped before Save."""
        self.assertIn('dispatchEvent(new Event("input"', self.script)
        self.assertIn("if (!storageRootEdited)", self.script)

    def test_each_one_is_sent_when_it_is_filled_in(self):
        for field in ("f-storage", "fb-storage", "ig-storage", "r-storage"):
            with self.subTest(field=field):
                self.assertIn(f'$("#{field}").value.trim()', self.script)
        self.assertEqual(self.script.count("body.storage_dir = "), 4)


class HelpTextTests(DashboardTestCase):
    """The "?" beside each field reads its wording from help_text.yaml."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from webarc.help import load_help
        cls.texts = load_help()
        cls.keys = set(re.findall(r'class="tip"[^>]*data-help="([^"]+)"', cls.markup))

    def test_every_icon_has_wording(self):
        self.assertTrue(self.keys)
        self.assertEqual(sorted(self.keys - set(self.texts)), [])

    def test_every_wording_has_an_icon(self):
        self.assertEqual(sorted(set(self.texts) - self.keys), [])

    def test_wording_is_plain_text_a_sentence_or_three_long(self):
        for key, text in self.texts.items():
            with self.subTest(field=key):
                self.assertNotIn("<", text)
                self.assertLess(len(text), 400)
                self.assertTrue(text.endswith("."))

    def test_an_installation_can_override_one_entry(self):
        import tempfile
        from webarc.help import OVERRIDE_NAME, load_help
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / OVERRIDE_NAME).write_text(
                "f-max-depth: Our own wording.\nf-operator:\n", encoding="utf-8")

            texts = load_help(tmp)

        self.assertEqual(texts["f-max-depth"], "Our own wording.")
        self.assertNotIn("f-operator", texts)          # blank hides that icon
        self.assertEqual(texts["f-max-pages"], self.texts["f-max-pages"])

    def test_a_broken_override_leaves_the_packaged_wording(self):
        import tempfile
        from webarc.help import OVERRIDE_NAME, load_help
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / OVERRIDE_NAME).write_text("- not: [a mapping", encoding="utf-8")

            texts = load_help(tmp)

        self.assertEqual(texts, self.texts)

    def test_the_page_fills_the_icons_from_the_server(self):
        self.assertIn('api("/api/help")', self.script)
        self.assertIn("loadHelp();", self.script)


class JobFilterTests(DashboardTestCase):
    """The job list can be narrowed by name, type, status and date, in the page."""

    def test_the_filter_bar_sits_above_the_list_on_the_jobs_page(self):
        jobs = self.section("jobs")
        for field in ("jf-search", "jf-kind", "jf-status", "jf-from", "jf-to", "jf-clear", "jf-count"):
            with self.subTest(field=field):
                self.assertIn(f'id="{field}"', jobs)
        self.assertLess(jobs.index('id="job-filters"'), jobs.index('id="manifest"'))

    def test_every_job_type_can_be_chosen(self):
        start = self.markup.index('id="jf-kind"')
        select = self.markup[start:self.markup.index("</select>", start)]
        options = set(re.findall(r'<option value="([a-z]+)"', select))
        self.assertEqual(options, {"crawl", "recording", "facebook", "instagram"})

    def test_the_refresh_renders_through_the_filter(self):
        start = self.script.index("async function refresh()")
        body = self.script[start:self.script.index("\n}", start)]
        self.assertIn("renderJobList()", body)
        self.assertNotIn("crawls.map(crawlRow)", body)

    def test_a_filter_survives_a_reload(self):
        self.assertIn('localStorage.setItem("swm-job-filter"', self.script)
        self.assertIn("restoreJobFilter();", self.script)


if __name__ == "__main__":
    unittest.main()
