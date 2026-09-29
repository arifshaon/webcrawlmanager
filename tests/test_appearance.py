"""Dashboard themes: folders of colours and job-type icons."""
from __future__ import annotations

import io
import json
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from webarc import appearance
from webarc.appearance import (ICON_ROLES, PACKAGED_DIR, ThemeError, ThemeExists,
                               contrast, install_zip, list_themes, load_theme,
                               remove_theme, stylesheet, svg_problems, valid_colour)

ICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        b'<circle cx="12" cy="12" r="9"/></svg>')
DASHBOARD = Path(appearance.__file__).resolve().parent / "dashboard.html"


def theme_zip(manifest: dict, icons: dict | None = None, folder: str = "",
              extra: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(folder + "theme.json", json.dumps(manifest))
        for role, data in (icons if icons is not None else {"crawl": ICON}).items():
            archive.writestr(f"{folder}icons/{role}.svg", data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


class BuiltInThemeTests(unittest.TestCase):
    def test_the_shipped_themes_load_whole_and_read_well(self):
        themes = list_themes(None)

        self.assertEqual([t.id for t in themes][:1], ["default"])
        self.assertIn("midnight", [t.id for t in themes])
        for theme in themes:
            with self.subTest(theme=theme.id):
                self.assertEqual(theme.problems, [])
                self.assertEqual(theme.warnings, [])
        self.assertEqual(sorted(themes[0].icons), sorted(ICON_ROLES))

    def test_the_tile_themes_draw_their_own_large_colour_icons(self):
        themes = {t.id: t for t in list_themes(None)}

        for theme_id in ("aurora", "nebula"):
            with self.subTest(theme_id):
                theme = themes[theme_id]
                self.assertEqual((theme.icon_style, theme.icon_size), ("color", "large"))
                self.assertEqual((theme.layout, theme.font), ("board", "inter"))
                self.assertEqual(sorted(theme.icons), sorted(ICON_ROLES))
                self.assertEqual(sorted(theme.colors), ["dark", "light"])
                self.assertEqual(theme.describe()["icon_size"], "large")

    def test_the_standard_theme_lists_the_dashboards_own_colours(self):
        """Theme authors start from it, so it must match the page exactly."""
        html = DASHBOARD.read_text(encoding="utf-8")

        def block(start: str) -> dict:
            i = html.index(start)
            return dict(re.findall(r"--([a-z-]+):\s*([^;]+);", html[i:html.index("}", i)]))

        page = {"light": block("  :root {\n    color-scheme: light;"),
                "dark": block('  :root[data-theme="dark"] {')}
        theme = load_theme(PACKAGED_DIR / "default", builtin=True)
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                for token, value in theme.colors[mode].items():
                    self.assertEqual(page[mode][token].strip(), value, token)

    def test_the_page_knows_every_icon_role(self):
        html = DASHBOARD.read_text(encoding="utf-8")
        lists = re.findall(r'\[("crawl",[^\]]*)\]', html)

        self.assertGreaterEqual(len(lists), 2)          # the boot script and the page's
        for found in lists:
            self.assertEqual(tuple(json.loads(f"[{found}]")), ICON_ROLES)


class BoardLayoutTests(unittest.TestCase):
    """The board layout a theme may choose, as the page wires it."""

    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")
        cls.rows = (DASHBOARD.parent / "dashboard_hardening.js").read_text(encoding="utf-8")

    def test_the_layout_and_font_are_applied_before_the_first_paint(self):
        boot = self.html[:self.html.index("</script>")]
        self.assertIn('saved.layout === "board"', boot)
        self.assertIn('saved.font === "inter"', boot)

    def test_the_bundled_typeface_is_the_one_the_page_asks_for(self):
        for name in appearance.FONT_FILES:
            self.assertIn(f"/appearance/fonts/{name}", self.html)
            self.assertTrue((appearance.FONTS_DIR / name).is_file())
        self.assertTrue((appearance.FONTS_DIR / "Inter-OFL.txt").is_file())

    def test_a_job_row_reports_captured_out_of_reported(self):
        self.assertIn("ofReported(fb.comments_exported, fb.comments_available", self.rows)
        self.assertIn("ofReported(fb.media_captured, fb.media_expected", self.rows)

    def test_no_job_list_container_clips_its_more_menu(self):
        """A job's More menu opens past the row's (and the list's) bottom edge:
        neither may clip what overflows it."""
        css = self.html[:self.html.index("</style>")]
        for rule in re.findall(r"([^{}]*\.(?:crawl|manifest)\b[^{}]*)\{([^}]*)\}", css):
            selector, body = rule
            if "overflow" in body and ".brow" not in selector and ".row" not in selector \
                    and ".seed" not in selector and ".cur" not in selector:
                with self.subTest(selector=selector.strip()):
                    self.assertNotRegex(body, r"overflow\s*:\s*(hidden|clip|auto|scroll)")

    def test_one_job_search_and_new_job_in_the_header(self):
        self.assertNotIn('id="board-search"', self.html)
        header = self.html[self.html.index("<header"):self.html.index("</header>")]
        self.assertIn('class="new-job-btn board-only"', header)

    def test_every_tile_and_panel_can_be_moved_hidden_and_brought_back(self):
        cards = re.findall(r'\{id: "([a-z]+)", group: "(tiles|panels)"', self.html)
        self.assertEqual({g for _, g in cards}, {"tiles", "panels"})
        for id_, _ in cards:
            self.assertIn(f'"{id_}":', self.html)          # it has a body to render
        for hook in ("boardMove(", "boardHide(", "boardShow(", "boardReset()", "wireBoardDrag(",
                     'id="board-customise"', 'draggable="true"'):
            self.assertIn(hook, self.html)
        # the refresh must not rebuild the board under a drag
        self.assertIn("if (!boardDragging && (force || html !== lastBoardHtml))", self.html)

    def test_the_status_chart_uses_its_validated_colours_and_labels_every_segment(self):
        for token in ("--viz-run", "--viz-done", "--viz-pause", "--viz-stop"):
            self.assertIn(f"var({token})", self.html)
        self.assertIn('<ul class="legend">', self.html)
        self.assertIn("Show as table", self.html)


class CheckTests(unittest.TestCase):
    def test_only_plain_colour_values_are_colours(self):
        for good in ("#fff", "#0E7490", "#0e749080", "rgb(1, 2, 3)", "rgba(34, 211, 238, .35)",
                     "hsl(200 50% 40%)"):
            self.assertTrue(valid_colour(good), good)
        for bad in ("red; background: url(http://x)", "url(http://x)", "#fff}", "expression(1)",
                    "var(--x)", "#ggg", "", 12, "rgb(1,2,3); x: y"):
            self.assertFalse(valid_colour(bad), bad)

    def test_a_plain_drawing_passes(self):
        self.assertEqual(svg_problems(ICON), [])
        self.assertEqual(svg_problems((PACKAGED_DIR / "default" / "icons" / "youtube.svg").read_bytes()), [])

    def test_an_icon_that_could_do_anything_is_refused(self):
        cases = {
            "script": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "handler": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
            "outside": b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
                       b'<use x:href="http://evil.test/a.svg#i"/></svg>',
            "foreign": b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
            "entities": b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]>'
                        b'<svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
            "css": b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://x);</style></svg>',
            "style": b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(http://x)"/></svg>',
            "javascript": b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"/></svg>',
            "not svg": b'<html xmlns="http://www.w3.org/1999/xhtml"/>',
            "too big": b"<svg>" + b" " * (appearance.MAX_ICON_BYTES + 1) + b"</svg>",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertNotEqual(svg_problems(data), [])

    def test_a_reference_inside_the_file_is_fine(self):
        self.assertEqual(svg_problems(
            b'<svg xmlns="http://www.w3.org/2000/svg"><defs><path id="p" d="M0 0"/></defs>'
            b'<use href="#p"/><rect style="fill:url(#g)"/></svg>'), [])

    def test_contrast_is_measured_as_wcag_does(self):
        self.assertAlmostEqual(contrast("#000", "#FFFFFF"), 21.0, places=1)
        self.assertAlmostEqual(contrast("#777777", "#FFFFFF"), 4.48, places=1)
        self.assertIsNone(contrast("hsl(0 0% 0%)", "#fff"))


class HandMadeFolderTests(unittest.TestCase):
    """A theme copied in by hand keeps what passes and says what was left out."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.base = load_theme(PACKAGED_DIR / "default", builtin=True)

    def folder(self, name: str, manifest: dict, icons: dict) -> Path:
        path = self.root / name
        (path / "icons").mkdir(parents=True)
        (path / "theme.json").write_text(json.dumps(manifest), encoding="utf-8")
        for role, data in icons.items():
            (path / "icons" / f"{role}.svg").write_bytes(data)
        return path

    def test_what_fails_is_left_out_and_named(self):
        path = self.folder("harbour", {
            "name": "Harbour", "icon_style": "sparkly", "icon_size": "huge",
            "layout": "<style>", "font": "Comic Sans",
            "colors": {"light": {"accent": "#0E7490", "ink": "url(http://x)", "glow": "#fff"}}},
            {"crawl": ICON, "instagram": b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>'})

        theme = load_theme(path, base=self.base)

        self.assertEqual(theme.colors["light"], {"accent": "#0E7490"})
        self.assertEqual(sorted(theme.icons), ["crawl"])
        self.assertEqual(theme.icon_style, "mono")
        joined = " ".join(theme.problems)
        self.assertEqual((theme.icon_size, theme.layout, theme.font), ("normal", "classic", "system"))
        for named in ("colors.light.ink", "colors.light.glow", "icons/instagram.svg", "icon_style",
                      "icon_size", "layout", "font"):
            self.assertIn(named, joined)

    def test_a_missing_or_refused_icon_comes_from_the_standard_theme(self):
        self.folder("harbour", {"name": "Harbour"}, {"crawl": ICON})

        self.assertEqual(appearance.icon_path("harbour", "crawl", self.root),
                         self.root / "harbour" / "icons" / "crawl.svg")
        self.assertEqual(appearance.icon_path("harbour", "youtube", self.root),
                         PACKAGED_DIR / "default" / "icons" / "youtube.svg")
        self.assertEqual(appearance.icon_path("gone", "youtube", self.root),
                         PACKAGED_DIR / "default" / "icons" / "youtube.svg")
        self.assertIsNone(appearance.icon_path("harbour", "../../etc/passwd", self.root))

    def test_hard_to_read_colours_are_flagged_not_refused(self):
        theme = load_theme(self.folder("pale", {"name": "Pale", "colors": {
            "light": {"accent": "#9CC7FF"}}}, {}), base=self.base)

        self.assertEqual(theme.colors["light"], {"accent": "#9CC7FF"})
        self.assertTrue(any("accent on paper" in w for w in theme.warnings))

    def test_a_folder_taking_a_shipped_themes_name_is_left_out(self):
        self.folder("midnight", {"name": "Not midnight"}, {})
        self.folder("broken", {}, {})

        themes = list_themes(self.root)

        self.assertEqual([t.name for t in themes if t.id == "midnight"], ["Midnight"])
        self.assertNotIn("broken", [t.id for t in themes])

    def test_the_stylesheet_leaves_high_contrast_alone(self):
        theme = load_theme(self.folder("harbour", {"name": "Harbour", "colors": {
            "light": {"accent": "#0E7490"}, "dark": {"accent": "#67E8F9"}}}, {}), base=self.base)

        css = stylesheet(theme)

        self.assertIn(':root:not([data-contrast="high"]) {\n  --accent: #0E7490;', css)
        self.assertIn(':root[data-theme="dark"]:not([data-contrast="high"]) {\n  --accent: #67E8F9;', css)
        self.assertEqual(css.count("--accent:"), 3)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "ui-themes"

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []

    def test_a_zip_of_the_folder_installs_under_its_name(self):
        theme = install_zip(theme_zip({"name": "Harbour Blue", "colors": {"light": {"accent": "#0E7490"}}},
                                      folder="harbour/"), self.root)

        self.assertEqual(theme.id, "harbour-blue")
        self.assertEqual(self.leftovers(), ["harbour-blue"])
        self.assertEqual(theme.colors["light"], {"accent": "#0E7490"})
        self.assertIn("harbour-blue", [t.id for t in list_themes(self.root)])

    def test_the_same_name_again_is_refused_unless_replacing(self):
        install_zip(theme_zip({"id": "harbour", "name": "Harbour", "version": "1"}), self.root)

        with self.assertRaises(ThemeExists):
            install_zip(theme_zip({"id": "harbour", "name": "Harbour", "version": "2"}), self.root)
        replaced = install_zip(theme_zip({"id": "harbour", "name": "Harbour", "version": "2"}),
                               self.root, replace=True)

        self.assertEqual(replaced.version, "2")
        self.assertEqual(self.leftovers(), ["harbour"])

    def test_one_bad_part_refuses_the_whole_theme(self):
        cases = {
            "a scripted icon": theme_zip({"name": "Bad"}, {
                "crawl": b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'}),
            "a colour that is not one": theme_zip({"name": "Bad", "colors": {
                "light": {"accent": "red;}body{display:none"}}}),
            "a file a theme does not use": theme_zip({"name": "Bad"}, extra={"run.js": "x"}),
            "a path out of the folder": theme_zip({"name": "Bad"}, extra={"../escape.svg": ICON}),
            "a shipped theme's name": theme_zip({"id": "midnight", "name": "Midnight"}),
            "no name": theme_zip({"version": "1"}),
            "another schema": theme_zip({"name": "Bad", "schema": "swm-ui-theme-v9"}),
            "not a zip": b"PK not really",
        }
        for why, data in cases.items():
            with self.subTest(why):
                with self.assertRaises(ThemeError):
                    install_zip(data, self.root)
                self.assertEqual(self.leftovers(), [])

    def test_a_path_out_of_the_folder_is_refused_for_that_reason(self):
        for name in ("../escape.svg", "/etc/theme.json", "icons/../../x.svg", "C:/x/theme.json"):
            with self.subTest(name):
                with self.assertRaises(ThemeError) as refused:
                    install_zip(theme_zip({"name": "Bad"}, extra={name: ICON}), self.root)
                self.assertIn("outside the theme", str(refused.exception))

    def test_a_link_inside_the_zip_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("theme.json", json.dumps({"name": "Linked"}))
            link = zipfile.ZipInfo("icons/crawl.svg")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, ICON)          # a drawing: only the link is wrong

        with self.assertRaises(ThemeError):
            install_zip(buffer.getvalue(), self.root)

    def test_an_oversized_zip_is_refused_before_it_is_opened(self):
        with self.assertRaises(ThemeError):
            install_zip(b"0" * (appearance.MAX_ZIP_BYTES + 1), self.root)

    def test_installed_themes_can_be_removed_shipped_ones_cannot(self):
        install_zip(theme_zip({"id": "harbour", "name": "Harbour"}), self.root)

        remove_theme("harbour", self.root)

        self.assertEqual(self.leftovers(), [])
        for theme_id in ("default", "midnight", "harbour", "../x"):
            with self.subTest(theme_id), self.assertRaises(ThemeError):
                remove_theme(theme_id, self.root)

    def test_a_downloaded_theme_installs_again_under_a_new_name(self):
        standard = load_theme(PACKAGED_DIR / "default", builtin=True)
        archive = zipfile.ZipFile(io.BytesIO(appearance.zip_theme(standard)))
        self.assertIn("default/theme.json", archive.namelist())
        self.assertIn("default/icons/instagram.svg", archive.namelist())

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as copy:
            for name in archive.namelist():
                data = archive.read(name)
                if name.endswith("theme.json"):
                    doc = json.loads(data)
                    doc["name"] = "Reading Room"
                    data = json.dumps(doc).encode()
                copy.writestr(name, data)

        theme = install_zip(buffer.getvalue(), self.root)

        self.assertEqual(theme.id, "reading-room")
        self.assertEqual(sorted(theme.icons), sorted(ICON_ROLES))
        self.assertEqual(theme.problems, [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:                            # pragma: no cover
            raise unittest.SkipTest(f"dashboard dependencies missing: {exc}")
        from webarc.server import create_app
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.client = TestClient(create_app(str(root / "swm.db"), str(root / "warcs"),
                                            replay_root=str(root / "replay"),
                                            monitor_resources=False))
        self.installed = root / "ui-themes"

    def test_the_list_names_the_themes_and_where_installed_ones_go(self):
        found = self.client.get("/api/appearance/themes").json()

        self.assertEqual([t["id"] for t in found["themes"]], ["default", "aurora", "midnight", "nebula"])
        self.assertEqual(found["roles"], list(ICON_ROLES))
        self.assertEqual(Path(found["folder"]), self.installed.resolve())

    def test_an_icon_is_served_as_an_image_that_cannot_run_anything(self):
        reply = self.client.get("/appearance/themes/midnight/icons/instagram.svg")

        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.headers["content-type"], "image/svg+xml")
        self.assertIn("default-src 'none'", reply.headers["content-security-policy"])
        self.assertEqual(reply.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/appearance/themes/default/icons/nope.svg").status_code, 404)

    def test_install_use_and_remove_through_the_api(self):
        data = theme_zip({"id": "harbour", "name": "Harbour",
                          "colors": {"light": {"accent": "#0E7490"}}})

        made = self.client.post("/api/appearance/themes", content=data)
        again = self.client.post("/api/appearance/themes", content=data)
        refused = self.client.post("/api/appearance/themes", content=b"not a zip")
        css = self.client.get("/appearance/themes/harbour/theme.css")
        removed = self.client.delete("/api/appearance/themes/harbour")

        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()["id"], "harbour")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("--accent: #0E7490;", css.text)
        self.assertEqual(css.headers["content-type"].split(";")[0], "text/css")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/appearance/themes/harbour/theme.css").status_code, 404)
        self.assertEqual(self.client.delete("/api/appearance/themes/default").status_code, 400)

    def test_only_the_fonts_swm_ships_are_served(self):
        font = self.client.get("/appearance/fonts/inter-latin.woff2")

        self.assertEqual(font.status_code, 200)
        self.assertEqual(font.headers["content-type"], "font/woff2")
        self.assertEqual(font.content[:4], b"wOF2")
        for name in ("Inter-OFL.txt", "../appearance.py", "..%2Fappearance.py", "missing.woff2"):
            with self.subTest(name):
                self.assertEqual(self.client.get(f"/appearance/fonts/{name}").status_code, 404)

    def test_a_theme_downloads_as_a_zip(self):
        reply = self.client.get("/api/appearance/themes/midnight/download")

        self.assertEqual(reply.headers["content-type"], "application/zip")
        self.assertIn("midnight/theme.json", zipfile.ZipFile(io.BytesIO(reply.content)).namelist())


if __name__ == "__main__":
    unittest.main()
