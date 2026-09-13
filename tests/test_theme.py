"""Theme-based selection: the text a judge sees, the rules, the AI judge
against stand-ins, how the two combine, the hold that keeps a page out of
the WARC until it is judged, the log, and the crawl and recording that
use them."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from webarc import theme as T
from webarc.theme import (AIJudge, AIJudgeError, PageHold, PageText, RulesJudge, ThemeConfig,
                          ThemeJudge, extract_page_text, normalise, term_pattern)

from tests.fixtures.theme import serve

LIBRARIES = {"name": "libraries", "brief": "News about public libraries: openings, services, events.",
             "terms": ["library", "مكتبة"], "exclude_terms": ["football"],
             "url_exclude": ["/login"], "hub_patterns": ["/culture/$", "/sport/$"],
             "date_from": "2025-01-01"}


class FakeAI(AIJudge):
    """A judge that answers from a script instead of a model."""
    provider = "fake"

    def __init__(self, page_answers=None, link_answer=None, fail=False, link_skip=None):
        super().__init__("fake-model")
        self.page_answers = page_answers or {}
        self.link_answer = link_answer
        self.link_skip = link_skip          # skip, confidently, the link whose address holds this
        self.fail = fail
        self.prompts: list[tuple[str, str]] = []

    def _complete(self, system, user, *, max_tokens):
        self.prompts.append((system, user))
        if self.fail:
            raise RuntimeError("model down")
        if "Links:" in user and system.startswith("You help a web crawler"):
            if self.link_skip:
                links = []
                for match in re.finditer(r"^(\d+)\. (\S+)$", user, re.M):
                    if self.link_skip in match.group(2):
                        links.append({"i": int(match.group(1)), "decision": "skip", "confidence": 0.97,
                                      "reason": "not news"})
                return json.dumps({"links": links})
            return json.dumps(self.link_answer or {"links": []})
        for needle, answer in self.page_answers.items():
            if needle in user:
                return answer if isinstance(answer, str) else json.dumps(answer)
        return json.dumps({"relevant": "unsure", "confidence": 0.4, "reasons": "nothing scripted"})


class TextTests(unittest.TestCase):
    def test_arabic_and_english_variants_meet(self):
        self.assertEqual(normalise("ثَقافَةٌ"), normalise("ثقافه"))
        library = term_pattern("library")
        self.assertTrue(library.search(normalise("Libraries were open")))
        self.assertTrue(library.search(normalise("the library's hours")))
        self.assertFalse(library.search(normalise("librarian")))
        maktaba = term_pattern("مكتبة")
        for text in ("مكتبة", "المكتبات الفرعية", "ومكتبةٍ", "بالمكتبه"):
            self.assertTrue(maktaba.search(normalise(text)), text)
        self.assertFalse(maktaba.search(normalise("كتب")))
        self.assertTrue(term_pattern("heritage site").search(normalise("a Heritage  Site opened")))

    def test_the_article_is_read_without_its_menus(self):
        html = serve.ARTICLES["/news/2-football-final"].decode()
        page = extract_page_text(html, "http://x/news/2")
        self.assertTrue(page.main_found)
        self.assertEqual(page.headline, "Football cup final ends in penalties")
        self.assertEqual(page.section, "Sport")
        self.assertEqual(page.published, "2026-09-02T21:00:00Z")
        self.assertNotIn("Library card", page.body)      # the menu and the masthead
        self.assertIn("penalties", page.body)
        arabic = extract_page_text(serve.ARTICLES["/news/3-maktaba"].decode(), "http://x/news/3")
        self.assertEqual(arabic.language, "ar")
        self.assertEqual(arabic.section, "ثقافة")

    def test_metadata_comes_from_meta_tags_and_json_ld(self):
        html = """<html><head><title>T</title><meta name="description" content="A standfirst">
        <meta property="article:tag" content="one"><meta property="article:tag" content="two">
        <script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","headline":"LD headline",
        "datePublished":"2026-03-04","keywords":["k1","k2"],"about":[{"name":"Libraries"}]}]}</script>
        </head><body><p>short</p></body></html>"""
        page = extract_page_text(html, "http://x/")
        self.assertEqual(page.headline, "LD headline")
        self.assertEqual(page.tags, ["one", "two"])
        self.assertEqual(page.published, "2026-03-04T00:00:00Z")
        self.assertIn("k1", page.keywords)
        self.assertIn("Libraries", page.keywords)
        self.assertEqual(page.description, "A standfirst")
        self.assertFalse(page.main_found)
        self.assertEqual(page.body, "short")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        theme = ThemeConfig.from_dict(LIBRARIES)
        self.assertTrue(theme.enabled)
        self.assertEqual(theme.min_score, 3)
        self.assertEqual(theme.date_from, "2025-01-01T00:00:00Z")
        self.assertEqual(theme.unsure_action, "review")
        self.assertFalse(ThemeConfig.from_dict(None).enabled)
        self.assertFalse(ThemeConfig.from_dict({"enabled": False}).enabled)
        for bad in ({"name": "x"}, {"terms": ["a"], "ai_policy": "vote"}, {"terms": ["a"], "url_exclude": ["("]},
                    {"terms": ["a"], "date_from": "2026-02-01", "date_to": "2026-01-01"},
                    {"terms": ["a"], "min_score": 0}, {"terms": ["a"], "unsure_action": "ask"}):
            with self.assertRaises(ValueError, msg=bad):
                ThemeConfig.from_dict(bad)
        self.assertEqual(ThemeConfig.from_dict({"terms": "a\nb"}).terms, ["a", "b"])
        self.assertEqual(len(theme.fingerprint), 16)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = RulesJudge(ThemeConfig.from_dict(LIBRARIES))

    def page(self, path):
        return extract_page_text(serve.ARTICLES[path].decode(), "http://x" + path)

    def test_headline_hits_keep_a_page_and_menu_text_does_not(self):
        kept = self.rules.judge_page(self.page("/news/1-library-opens"))
        self.assertEqual(kept.decision, "keep")
        self.assertGreaterEqual(kept.score, 3)
        self.assertEqual(kept.matched[0]["where"], "headline")
        rejected = self.rules.judge_page(self.page("/news/2-football-final"))
        self.assertEqual(rejected.decision, "reject")
        self.assertTrue(rejected.hard)                    # "football" in the headline
        arabic = self.rules.judge_page(self.page("/news/3-maktaba"))
        self.assertEqual(arabic.decision, "keep")

    def test_a_single_mention_in_the_text_is_unsure(self):
        verdict = self.rules.judge_page(self.page("/news/4-city-budget"))
        self.assertEqual(verdict.decision, "unsure")
        self.assertEqual(verdict.score, 1)
        self.assertFalse(verdict.hard)

    def test_the_date_window_and_the_address_rules_are_hard(self):
        old = self.rules.judge_page(self.page("/news/5-old-library-story"))
        self.assertEqual((old.decision, old.hard), ("reject", True))
        self.assertIn("2019", old.reasons[0])
        login = self.rules.judge_page(PageText(url="http://x/login", title="Log in", body="library"))
        self.assertEqual((login.decision, login.hard), ("reject", True))
        self.assertEqual(self.rules.url_excluded("http://x/login?next=/"), "/login")
        self.assertTrue(self.rules.is_hub("http://x/culture/"))
        self.assertFalse(self.rules.is_hub("http://x/culture/2026"))

    def test_links_are_triaged_from_their_text(self):
        self.assertEqual(self.rules.judge_link("http://x/login", "Log in").decision, "skip")
        self.assertEqual(self.rules.judge_link("http://x/sport/", "Sport").decision, "hub")
        libr = self.rules.judge_link("http://x/news/9", "New library opens", "reading room")
        self.assertEqual((libr.decision, libr.score), ("fetch", 2))
        plain = self.rules.judge_link("http://x/news/8", "Council meets", "roads")
        self.assertEqual((plain.decision, plain.score), ("fetch", 0))
        self.assertEqual(self.rules.judge_link("http://x/news/7", "Football final").decision, "skip")


class AIJudgeTests(unittest.TestCase):
    def test_the_answer_is_read_whole_and_quotes_are_checked_against_the_page(self):
        theme = ThemeConfig.from_dict(LIBRARIES)
        page = extract_page_text(serve.ARTICLES["/news/1-library-opens"].decode(), "http://x/news/1-library-opens")
        ai = FakeAI({"library-opens": {"relevant": "yes", "confidence": 0.93, "reasons": "An opening.",
                                       "quotes": ["opened a new public library", "made-up passage"]}})
        verdict = ai.judge_page(theme, page)
        self.assertEqual(verdict["decision"], "keep")
        self.assertEqual(verdict["confidence"], 0.93)
        self.assertEqual(verdict["quotes"], ["opened a new public library"])
        self.assertEqual(verdict["quotes_not_in_page"], ["made-up passage"])
        self.assertEqual(verdict["model"], "fake-model")
        self.assertEqual(len(verdict["prompt_hash"]), 16)
        system, user = ai.prompts[0]
        self.assertIn("Curator's brief", user)
        self.assertIn("Main text:", user)
        self.assertNotIn("Library card", user)
        self.assertEqual(T._json_object('Answer:\n```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(AIJudgeError):
            T._json_object("no json here")

    def test_a_failing_model_is_an_error_the_rules_cover(self):
        theme = ThemeConfig.from_dict(LIBRARIES)
        page = PageText(url="http://x/a", title="A library", body="library")
        with self.assertRaises(AIJudgeError):
            FakeAI(fail=True).judge_page(theme, page)
        with self.assertRaises(AIJudgeError):
            FakeAI({"x/a": "not json"}).judge_page(theme, page)

    def test_link_triage_answers_are_bounded_to_the_batch(self):
        theme = ThemeConfig.from_dict(LIBRARIES)
        ai = FakeAI(link_answer={"links": [{"i": 0, "decision": "skip", "confidence": 0.95, "reason": "ads"},
                                           {"i": 1, "decision": "hub", "confidence": 0.7},
                                           {"i": 9, "decision": "skip", "confidence": 1.0},
                                           {"i": 0, "decision": "maybe"}]})
        answers = ai.triage_links(theme, [{"url": "http://x/ads", "text": "Deals"},
                                          {"url": "http://x/tag/x", "text": "More"}])
        self.assertEqual([(a["i"], a["decision"]) for a in answers], [(0, "skip"), (1, "hub")])
        self.assertEqual(ai.triage_links(theme, []), [])

    def test_the_openai_compatible_judge_speaks_the_chat_shape(self):
        import httpx
        seen = {}

        def post(url, json=None, headers=None, timeout=None):
            seen.update({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"relevant": "no", "confidence": 0.8, "reasons": "sport", "quotes": []}'}}]})
        original = httpx.post
        httpx.post = post
        try:
            judge = T.OpenAICompatibleJudge("http://127.0.0.1:11434/v1", "llama", "k")
            verdict = judge.judge_page(ThemeConfig.from_dict(LIBRARIES), PageText(url="http://x/2", title="Cup", body="goals"))
        finally:
            httpx.post = original
        self.assertEqual(verdict["decision"], "reject")
        self.assertEqual(seen["url"], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(seen["json"]["model"], "llama")
        self.assertEqual(seen["headers"]["authorization"], "Bearer k")
        self.assertEqual(judge.describe()["endpoint"], "http://127.0.0.1:11434/v1")

    def test_settings_choose_the_judge_and_never_echo_the_key(self):
        stored = {}
        get = lambda key: stored.get(key)
        self.assertIsNone(T.make_ai_judge(get))
        self.assertFalse(T.ai_capability(get)["available"])
        stored.update({"theme.ai.provider": "openai_compatible", "theme.ai.endpoint": "http://127.0.0.1:1/v1",
                       "theme.ai.model": "m", "theme.ai.api_key": "secret", "theme.ai.max_calls": "5"})
        judge = T.make_ai_judge(get)
        self.assertIsInstance(judge, T.OpenAICompatibleJudge)
        settings = T.ai_settings(get)
        self.assertTrue(settings["has_key"])
        self.assertEqual(settings["max_calls"], 5)
        self.assertNotIn("secret", json.dumps(settings))
        self.assertTrue(T.ai_capability(get)["available"])
        stored["theme.ai.provider"] = "anthropic"
        anthropic_judge = T.make_ai_judge(get)
        self.assertEqual((anthropic_judge.provider, anthropic_judge.model), ("anthropic", "m"))
        stored["theme.ai.model"] = ""
        self.assertEqual(T.make_ai_judge(get).model, T.DEFAULT_ANTHROPIC_MODEL)


class ComposedJudgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def page(self, path):
        return extract_page_text(serve.ARTICLES[path].decode(), "http://x" + path)

    def judge(self, ai=None, **overrides):
        return ThemeJudge(ThemeConfig.from_dict({**LIBRARIES, **overrides}), ai, self.out)

    def test_rules_alone_and_the_log(self):
        judge = self.judge()
        self.assertEqual(judge.judge_page(self.page("/news/1-library-opens")).decision, "keep")
        self.assertEqual(judge.judge_page(self.page("/news/4-city-budget")).decision, "unsure")
        self.assertEqual(judge.judge_page(self.page("/news/2-football-final")).decision, "reject")
        hub = judge.judge_page(self.page("/news/2-football-final"), hub=True)
        self.assertEqual(hub.decision, "keep")
        self.assertIn("hub page", hub.reason)
        rows = judge.log.rows()
        self.assertEqual([r["decision"] for r in rows], ["keep", "unsure", "reject", "keep"])
        self.assertEqual(rows[0]["judge"], "rules")
        self.assertEqual(rows[0]["page"]["headline"], "New public library opens in Doha")
        self.assertEqual(judge.log.counts["pages_kept"], 2)
        summary = json.loads(judge.write_summary(self.out).read_text())
        self.assertEqual(summary["policy"], "rules_only")
        self.assertEqual(summary["theme"]["name"], "libraries")
        self.assertIsNone(summary["ai"])
        page = T.render_selection_page(self.out)
        self.assertIn("New public library opens", page.read_text())

    def test_the_ai_decides_and_hard_rules_still_stand(self):
        ai = FakeAI({"city-budget": {"relevant": "yes", "confidence": 0.9, "reasons": "budget for libraries",
                                     "quotes": ["the library budget is unchanged"]},
                     "library-opens": {"relevant": "no", "confidence": 0.6, "reasons": "hmm"}})
        judge = self.judge(ai)
        budget = judge.judge_page(self.page("/news/4-city-budget"))
        self.assertEqual((budget.decision, budget.judge), ("keep", "both"))
        self.assertEqual(budget.ai["quotes"], ["the library budget is unchanged"])
        self.assertEqual(judge.judge_page(self.page("/news/1-library-opens")).decision, "reject")
        old = judge.judge_page(self.page("/news/5-old-library-story"))
        self.assertEqual((old.decision, old.judge), ("reject", "rules"))     # date window: no AI call
        self.assertEqual(ai.calls, 2)
        self.assertEqual(judge.summary()["ai"]["provider"], "fake")

    def test_tie_break_and_agree_policies(self):
        ai = FakeAI({"city-budget": {"relevant": "yes", "confidence": 0.9, "reasons": "r"},
                     "library-opens": {"relevant": "no", "confidence": 0.9, "reasons": "r"}})
        tie = self.judge(ai, ai_policy="tie_break")
        self.assertEqual(tie.judge_page(self.page("/news/1-library-opens")).judge, "rules")
        self.assertEqual(tie.judge_page(self.page("/news/4-city-budget")).decision, "keep")
        agree = self.judge(ai, ai_policy="agree")
        disagreement = agree.judge_page(self.page("/news/1-library-opens"))
        self.assertEqual(disagreement.decision, "unsure")
        self.assertIn("disagree", disagreement.reason)

    def test_a_silent_model_leaves_the_rules_in_charge_and_the_ceiling_holds(self):
        judge = self.judge(FakeAI(fail=True))
        decision = judge.judge_page(self.page("/news/1-library-opens"))
        self.assertEqual((decision.decision, decision.judge), ("keep", "rules"))
        self.assertIn("AI judge unavailable", decision.reason)
        self.assertEqual(judge.log.counts["ai_failures"], 1)
        capped = ThemeJudge(ThemeConfig.from_dict(LIBRARIES), FakeAI(), self.out, max_ai_calls=1)
        capped.judge_page(self.page("/news/1-library-opens"))
        second = capped.judge_page(self.page("/news/4-city-budget"))
        self.assertEqual(second.judge, "rules")

    def test_link_triage_skips_only_on_confidence_and_remembers(self):
        ai = FakeAI(link_answer={"links": [{"i": 0, "decision": "skip", "confidence": 0.95, "reason": "adverts"},
                                           {"i": 1, "decision": "skip", "confidence": 0.5, "reason": "maybe"}]})
        judge = self.judge(ai)
        links = [{"url": "http://x/deals", "text": "Deals of the week", "context": "sponsored"},
                 {"url": "http://x/news/9", "text": "Council meets", "context": ""},
                 {"url": "http://x/login", "text": "Log in"},
                 {"url": "http://x/news/10", "text": "Library hours change"}]
        results = judge.triage_links(links, from_url="http://x/")
        by_url = {r["url"]: r for r in results}
        self.assertEqual(by_url["http://x/deals"]["decision"], "skip")
        self.assertEqual(by_url["http://x/deals"]["judge"], "ai")
        self.assertEqual(by_url["http://x/news/9"]["decision"], "fetch")   # half-sure is not enough
        self.assertEqual(by_url["http://x/login"]["decision"], "skip")
        self.assertEqual(by_url["http://x/news/10"]["decision"], "fetch")
        self.assertEqual(ai.calls, 1)
        _, user = ai.prompts[0]
        self.assertIn("Deals of the week", user)
        self.assertNotIn("Library hours", user)        # decided by the rules, not sent
        again = judge.triage_links(links[:1])
        self.assertTrue(again[0]["cached"])
        self.assertEqual(ai.calls, 1)
        self.assertEqual(judge.log.counts["links_skipped"], 2)

    def test_the_hold_commits_in_order_or_lets_go(self):
        written = []

        class Warc:
            def write_exchange(self, **kw):
                written.append(kw["url"])
        hold = PageHold()
        hold.write_exchange(url="a", method="GET", req_headers={}, post_data=None, status=200,
                            status_text="OK", resp_headers={}, body=b"")
        hold.write_exchange(url="b", method="GET", req_headers={}, post_data=None, status=200,
                            status_text="OK", resp_headers={}, body=b"")
        self.assertEqual(len(hold), 2)
        self.assertEqual(hold.commit(Warc()), 2)
        self.assertEqual(written, ["a", "b"])
        hold.write_exchange(url="c", method="GET", req_headers={}, post_data=None, status=200,
                            status_text="OK", resp_headers={}, body=b"")
        self.assertEqual(hold.discard(), 1)
        self.assertEqual(written, ["a", "b"])
        self.assertEqual((hold.committed, hold.discarded), (2, 1))

    def test_build_from_a_job_and_settings(self):
        self.assertIsNone(T.build_theme_judge(None, lambda k: None, self.out))
        self.assertIsNone(T.build_theme_judge({"enabled": False}, lambda k: None, self.out))
        judge = T.build_theme_judge(LIBRARIES, lambda k: None, self.out)
        self.assertIsNone(judge.ai)
        stored = {"theme.ai.provider": "openai_compatible", "theme.ai.endpoint": "http://127.0.0.1:1/v1",
                  "theme.ai.model": "m", "theme.ai.max_calls": "7"}
        judge = T.build_theme_judge(LIBRARIES, stored.get, self.out)
        self.assertEqual((judge.ai.provider, judge.max_ai_calls), ("openai_compatible", 7))
        self.assertIsNone(T.build_theme_judge({**LIBRARIES, "ai_enabled": False}, stored.get, self.out).ai)


class CrawlTests(unittest.TestCase):
    """A real headless crawl of the fixture site with a theme."""

    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:                                # pragma: no cover
            raise unittest.SkipTest("playwright is not installed")
        cls.server, cls.base = serve.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        serve.Handler.requests = []

    def crawl(self, theme_raw, ai=None):
        from webarc.config import BehaviorConfig, BrowserConfig, CrawlConfig, ScopeConfig, SeedConfig, WarcConfig
        from webarc.control import NullController
        from webarc.crawler import crawl_seed
        seed = SeedConfig(url=self.base + "/", browser=BrowserConfig(mode="headless"),
                          scope=ScopeConfig(strategy="same-host", max_depth=2, max_pages=50),
                          behavior=BehaviorConfig(obey_robots=False, delay_range=(0, 0), scroll=False,
                                                  mouse_jitter=False, dismiss_consent=False,
                                                  detect_blocks=False, wait_until="load",
                                                  page_timeout=20, challenge_grace=0),
                          warc=WarcConfig())
        crawl = CrawlConfig(crawl_name="themed", output_dir=self.out, operator="test", seeds=[seed],
                            theme=theme_raw)
        judge = ThemeJudge(ThemeConfig.from_dict(theme_raw), ai, self.out)
        stats = crawl_seed(seed, crawl, 1, NullController(), theme_judge=judge)
        return stats, judge

    @staticmethod
    def urls_in(paths):
        from warcio.archiveiterator import ArchiveIterator
        found = []
        for path in paths:
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type == "response":
                        found.append(record.rec_headers.get_header("WARC-Target-URI"))
        return found

    def test_only_the_themes_pages_reach_the_warc_and_everything_is_accounted_for(self):
        stats, judge = self.crawl(LIBRARIES)

        kept = self.urls_in(self.out.glob("*.warc.gz"))
        paths = sorted({u.replace(self.base, "") for u in kept})
        self.assertIn("/news/1-library-opens", paths)
        self.assertIn("/news/3-maktaba", paths)
        self.assertIn("/", paths)                       # the seed is the way in
        self.assertIn("/culture/", paths)               # a hub, kept as context
        self.assertNotIn("/news/2-football-final", paths)
        self.assertNotIn("/news/4-city-budget", paths)
        self.assertNotIn("/news/5-old-library-story", paths)
        self.assertNotIn("/about", paths)
        review = self.urls_in((self.out / "review").glob("*.warc.gz"))
        self.assertEqual({u.replace(self.base, "") for u in review}, {"/news/4-city-budget"})
        self.assertNotIn("/login", serve.Handler.requests)          # never requested: an address rule
        self.assertNotIn("/news/2-football-final", serve.Handler.requests)   # never requested: its link text
        self.assertIn("/about", serve.Handler.requests)              # fetched, judged, dropped
        self.assertEqual(stats["theme"]["kept"], 5)       # two articles and three hubs (the seed among them)
        self.assertEqual(stats["theme"]["unsure"], 1)
        self.assertGreaterEqual(stats["theme"]["rejected"], 3)
        rows = judge.log.rows()
        by_url = {r["url"].replace(self.base, ""): r for r in rows if r["kind"] == "page"}
        self.assertEqual(by_url["/about"]["decision"], "reject")
        self.assertEqual(by_url["/news/5-old-library-story"]["decision"], "reject")
        self.assertIn("2019", by_url["/news/5-old-library-story"]["reason"])
        self.assertEqual(by_url["/files/report.pdf"]["decision"], "reject")
        links = {r["url"].replace(self.base, ""): r for r in rows if r["kind"] == "link"}
        self.assertEqual(links["/login"]["decision"], "skip")
        self.assertEqual(links["/news/2-football-final"]["decision"], "skip")
        self.assertIn("football", links["/news/2-football-final"]["reasons"][0])
        self.assertEqual(links["/culture/"]["decision"], "hub")
        summary = json.loads((self.out / "theme-summary.json").read_text())
        self.assertEqual(summary["counts"]["pages_kept"], 5)
        self.assertTrue((self.out / "pages" / "selection.html").exists())
        self.assertTrue((self.out / "selection.jsonl").exists())

    def test_an_ai_judge_keeps_what_the_rules_could_not_place_and_skips_links(self):
        ai = FakeAI({"city-budget": {"relevant": "yes", "confidence": 0.9, "reasons": "library funding",
                                     "quotes": ["library budget is unchanged"]},
                     "report.pdf": {"relevant": "no", "confidence": 0.7, "reasons": "a report file"},
                     "library-opens": {"relevant": "yes", "confidence": 0.95, "reasons": "an opening"},
                     "maktaba": {"relevant": "yes", "confidence": 0.95, "reasons": "an opening"}},
                    link_skip="/about")
        stats, judge = self.crawl(LIBRARIES, ai)

        kept = {u.replace(self.base, "") for u in self.urls_in(self.out.glob("*.warc.gz"))}
        self.assertIn("/news/4-city-budget", kept)
        self.assertFalse(list((self.out / "review").glob("*.warc.gz")))
        self.assertGreater(ai.calls, 0)
        rows = judge.log.rows()
        budget = next(r for r in rows if r["kind"] == "page" and r["url"].endswith("/news/4-city-budget"))
        self.assertEqual(budget["judge"], "both")
        self.assertEqual(budget["ai"]["quotes"], ["library budget is unchanged"])
        skipped = [r for r in rows if r["kind"] == "link" and r.get("judge") == "ai" and r["decision"] == "skip"]
        self.assertEqual({r["url"].replace(self.base, "") for r in skipped}, {"/about"})
        self.assertNotIn("/about", serve.Handler.requests)         # the AI's word, before any fetch
        summary = json.loads((self.out / "theme-summary.json").read_text())
        self.assertEqual(summary["ai"]["model"], "fake-model")
        self.assertEqual(summary["policy"], "decide")

    def test_a_run_stops_after_the_misses_it_was_told_to(self):
        stats, _ = self.crawl({**LIBRARIES, "terms": ["zzznothing"], "exclude_terms": [],
                               "hub_patterns": [], "stop_after_misses": 2, "keep_hubs": False})
        self.assertLess(stats["visited"], 6)
        self.assertEqual(stats["theme"]["kept"], 0)


class RecordingTests(unittest.TestCase):
    def test_each_page_opened_is_judged_and_the_widget_told(self):
        from webarc.recorder import RecordingSession
        from webarc.config import BrowserConfig
        from tests.test_recording_regressions import DummyWarc

        class Page:
            def __init__(self, html):
                self.html = html
                self.told = []

            def content(self):
                return self.html

            def evaluate(self, _script, arg=None):
                self.told.append(arg)

        class Frame:
            parent_frame = None

            def __init__(self, url, page):
                self.url, self.page = url, page

        with tempfile.TemporaryDirectory() as tmp:
            judge = ThemeJudge(ThemeConfig.from_dict(LIBRARIES), None, Path(tmp))
            session = RecordingSession("http://x/", BrowserConfig(mode="headed"), DummyWarc(Path(tmp)),
                                       theme_judge=judge)
            session.doc_grace = 0
            page = Page(serve.ARTICLES["/news/1-library-opens"].decode())
            session._on_frame_navigated(Frame("http://x/news/1-library-opens", page))
            session._check_nav_watch()
            other = Page(serve.ARTICLES["/news/2-football-final"].decode())
            session._on_frame_navigated(Frame("http://x/news/2-football-final", other))
            session._check_nav_watch()

            self.assertEqual(session.theme_counts, {"kept": 1, "rejected": 1, "unsure": 0})
            self.assertEqual(page.told[0]["decision"], "keep")
            self.assertTrue(page.told[0]["text"].startswith("Theme: matches"))
            self.assertEqual(other.told[0]["decision"], "reject")
            rows = judge.log.rows()
            self.assertEqual([r["via"] for r in rows], ["recording", "recording"])
            reports = []
            session.on_progress = lambda **kw: reports.append(kw)
            session._report()
            self.assertEqual(reports[0]["details"], {"theme": {"kept": 1, "rejected": 1, "unsure": 0}})


class ApiTests(unittest.TestCase):
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

    def test_the_ai_settings_round_trip_without_the_key(self):
        settings = self.client.get("/api/settings").json()
        self.assertEqual(settings["theme_ai"]["provider"], "none")
        self.assertIn("available", settings["theme_ai"]["capability"])
        saved = self.client.put("/api/settings", json={"theme_ai": {
            "provider": "openai_compatible", "endpoint": "http://127.0.0.1:11434/v1", "model": "llama3",
            "api_key": "s3cret", "max_calls": 40}})
        self.assertEqual(saved.status_code, 200, saved.text)
        ai = saved.json()["theme_ai"]
        self.assertEqual((ai["provider"], ai["model"], ai["max_calls"], ai["has_key"]),
                         ("openai_compatible", "llama3", 40, True))
        self.assertNotIn("s3cret", saved.text)
        self.assertTrue(ai["capability"]["available"])
        self.assertTrue(self.client.get("/api/capabilities").json()["theme_ai"]["available"])
        kept = self.client.put("/api/settings", json={"theme_ai": {"provider": "openai_compatible",
                                                                    "endpoint": "http://127.0.0.1:11434/v1", "model": "llama3"}})
        self.assertTrue(kept.json()["theme_ai"]["has_key"])           # no key sent: the stored one stays
        cleared = self.client.put("/api/settings", json={"theme_ai": {"provider": "none", "api_key": ""}})
        self.assertFalse(cleared.json()["theme_ai"]["has_key"])
        self.assertEqual(self.client.put("/api/settings", json={"theme_ai": {"provider": "openai_compatible",
                                                                              "endpoint": "ollama"}}).status_code, 400)
        self.assertEqual(self.client.post("/api/theme/ai/test").status_code, 409)

    def test_a_theme_can_be_checked_against_a_page_before_a_run(self):
        response = self.client.post("/api/theme/check", json={
            "theme": LIBRARIES, "page": {"url": "http://x/news/1", "html": serve.ARTICLES["/news/1-library-opens"].decode()}})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["decision"], "keep")
        self.assertEqual(body["page"]["headline"], "New public library opens in Doha")
        self.assertFalse(body["ai_configured"])
        plain = self.client.post("/api/theme/check", json={
            "theme": LIBRARIES, "page": {"title": "Cup final", "text": "goals and penalties"}})
        self.assertEqual(plain.json()["decision"], "reject")
        self.assertEqual(self.client.post("/api/theme/check", json={"theme": {"name": "x"}, "page": {}}).status_code, 400)

    def test_jobs_carry_their_theme_and_a_broken_one_is_refused(self):
        crawl = self.client.post("/api/crawls", json={"config": {
            "crawl_name": "t", "seeds": [{"url": "https://example.org/"}], "theme": LIBRARIES}})
        self.assertEqual(crawl.status_code, 201, crawl.text)
        self.assertEqual(crawl.json()["theme"], "libraries")
        stored = json.loads(self.srv._store().get_crawl(crawl.json()["id"])["config_json"])
        self.assertEqual(stored["theme"]["terms"], ["library", "مكتبة"])
        bad = self.client.post("/api/crawls", json={"config": {
            "seeds": [{"url": "https://example.org/"}], "theme": {"terms": ["a"], "url_exclude": ["("]}}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("theme", bad.text)
        recording = self.client.post("/api/recordings", json={"url": "https://example.org/", "theme": LIBRARIES})
        if recording.status_code == 201:
            stored = json.loads(self.srv._store().get_crawl(recording.json()["id"])["config_json"])
            self.assertEqual(stored["recording"]["theme"]["name"], "libraries")
        else:
            self.assertEqual(recording.status_code, 409)       # no display here: refused before the theme
        made = self.client.post("/api/crawls", json={"config": {"seeds": [{"url": "https://example.org/"}]}}).json()
        self.assertIsNone(made["theme"])
        self.assertFalse(made["has_selection"])


if __name__ == "__main__":
    unittest.main()
