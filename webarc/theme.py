"""Theme-based selection for automated crawls.

A *theme* says which pages belong in a collection: news about one topic,
coverage of one event. The crawler fetches a page, a judge reads it, and
only then is the page's traffic committed to the WARC; a rejected page
leaves nothing in the archive but a line in ``selection.jsonl`` saying it
was looked at and why it was turned away. Links are triaged before they
are fetched, from what the parent page says about them, so requests go
where the theme is likely to be.

Two judges. The **rules judge** always runs: terms and phrases (Arabic
normalised, light stemming), URL and section rules, site metadata, a date
window, scored against the page's *main* content rather than its menus.
The **AI judge** is optional and answers the real question, "is this page
about this news?", from what SWM already holds; it never fetches anything
itself. By default it is sent the address, the headline and a short
excerpt and asked for one word, yes, no or unsure, which keeps a whole
crawl inside a small tokens-per-minute allowance; a theme can ask for the
address alone, or for the full text with reasons and quoted evidence.
Whatever it answers is recorded per page beside the rules' verdict, with
the model and a hash of the prompt, so a collection can be defended
later without the model.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import os
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

KEEP, REJECT, UNSURE = "keep", "reject", "unsure"
SKIP, FETCH, HUB = "skip", "fetch", "hub"
POLICIES = ("decide", "tie_break", "agree")
UNSURE_ACTIONS = ("review", "keep", "reject")
PROVIDERS = ("none", "anthropic", "openai_compatible", "azure_openai")
AI_INPUTS = ("url", "compact", "full")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_AZURE_API_VERSION = "2024-10-21"
SETTING_PREFIX = "theme.ai."
SETTING_KEYS = ("provider", "endpoint", "model", "api_key", "max_calls", "api_version",
                "tokens_per_minute", "max_prompt_tokens")
MIN_PROMPT_TOKENS = 200          # the address, the title and the theme's name always fit
QUESTIONS_PER_MINUTE = 20        # what a paced crawl asks, at most, when sizing questions
SELECTION_FILE = "selection.jsonl"
SUMMARY_FILE = "theme-summary.json"
PROMPT_VERSION = "swm-theme-prompt-1"


# ---------------------------------------------------------------------------
# The theme
# ---------------------------------------------------------------------------

def _lines(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = re.split(r"[\r\n]+", value)
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected a list of lines")
    return [str(v).strip() for v in value if str(v).strip()]


def _date_bound(value: object, *, end: bool = False) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        day = datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"dates must be YYYY-MM-DD, not {text!r}") from exc
    return day.isoformat() + ("T23:59:59Z" if end else "T00:00:00Z")


def _compiled(patterns: list[str], label: str) -> list[re.Pattern]:
    out = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern, re.I))
        except re.error as exc:
            raise ValueError(f"{label}: {pattern!r} is not a valid regular expression ({exc})") from exc
    return out


@dataclass
class ThemeConfig:
    """What the curator asked for. ``from_dict`` reads the ``theme:`` block
    of a crawl configuration or a recording's payload."""
    enabled: bool = False
    name: str = ""
    brief: str = ""
    languages: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    url_include: list[str] = field(default_factory=list)
    url_exclude: list[str] = field(default_factory=list)
    hub_patterns: list[str] = field(default_factory=list)
    keep_hubs: bool = True
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    min_score: int = 3
    unsure_action: str = "review"
    stop_after_misses: int = 0
    ai_enabled: bool = True
    ai_policy: str = "decide"
    ai_triage_links: bool = True
    ai_input: str = "compact"          # url | compact | full: what the model is sent
    examples: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: object) -> "ThemeConfig":
        if raw in (None, False):
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("theme must be a mapping")
        enabled = bool(raw.get("enabled", True))
        terms = _lines(raw.get("terms"))
        url_include = _lines(raw.get("url_include"))
        if enabled and not terms and not url_include and not str(raw.get("brief") or "").strip():
            raise ValueError("A theme needs at least one term, a URL rule, or a brief.")
        policy = str(raw.get("ai_policy") or "decide")
        if policy not in POLICIES:
            raise ValueError("ai_policy must be one of " + ", ".join(POLICIES))
        unsure = str(raw.get("unsure_action") or "review")
        if unsure not in UNSURE_ACTIONS:
            raise ValueError("unsure_action must be one of " + ", ".join(UNSURE_ACTIONS))
        ai_input = str(raw.get("ai_input") or "compact")
        if ai_input not in AI_INPUTS:
            raise ValueError("ai_input must be one of " + ", ".join(AI_INPUTS))
        date_from = _date_bound(raw.get("date_from"))
        date_to = _date_bound(raw.get("date_to"), end=True)
        if date_from and date_to and date_from > date_to:
            raise ValueError("The theme's From date must be on or before its To date.")
        try:
            min_score = int(3 if raw.get("min_score") in (None, "") else raw.get("min_score"))
            stop_after = int(raw.get("stop_after_misses") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_score and stop_after_misses must be whole numbers") from exc
        if min_score < 1:
            raise ValueError("min_score must be at least 1")
        examples = []
        for item in raw.get("examples") or []:
            if isinstance(item, dict) and (item.get("title") or item.get("text") or item.get("url")):
                examples.append({"url": str(item.get("url") or ""), "title": str(item.get("title") or ""),
                                 "text": str(item.get("text") or "")[:1500],
                                 "relevant": bool(item.get("relevant"))})
        theme = cls(
            enabled=enabled, name=str(raw.get("name") or "").strip()[:200],
            brief=str(raw.get("brief") or "").strip()[:4000],
            languages=_lines(raw.get("languages")),
            terms=terms, exclude_terms=_lines(raw.get("exclude_terms")),
            url_include=url_include, url_exclude=_lines(raw.get("url_exclude")),
            hub_patterns=_lines(raw.get("hub_patterns")),
            keep_hubs=bool(raw.get("keep_hubs", True)),
            date_from=date_from, date_to=date_to, min_score=min_score,
            unsure_action=unsure, stop_after_misses=max(0, stop_after),
            ai_enabled=bool(raw.get("ai_enabled", True)), ai_policy=policy,
            ai_triage_links=bool(raw.get("ai_triage_links", True)),
            ai_input=ai_input, examples=examples[:20])
        _compiled(theme.url_include, "url_include")
        _compiled(theme.url_exclude, "url_exclude")
        _compiled(theme.hub_patterns, "hub_patterns")
        return theme

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "name": self.name, "brief": self.brief,
            "languages": list(self.languages), "terms": list(self.terms),
            "exclude_terms": list(self.exclude_terms), "url_include": list(self.url_include),
            "url_exclude": list(self.url_exclude), "hub_patterns": list(self.hub_patterns),
            "keep_hubs": self.keep_hubs, "date_from": self.date_from, "date_to": self.date_to,
            "min_score": self.min_score, "unsure_action": self.unsure_action,
            "stop_after_misses": self.stop_after_misses, "ai_enabled": self.ai_enabled,
            "ai_policy": self.ai_policy, "ai_triage_links": self.ai_triage_links,
            "ai_input": self.ai_input, "examples": list(self.examples),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
                                         ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Text: normalisation and term matching
# ---------------------------------------------------------------------------

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC = re.compile(r"[؀-ۿ]")
_AR_PREFIX = r"(?:و|ف|ب|ك|ل|ال|وال|فال|بال|كال|لل|ولل)?"
_AR_SUFFIX = r"(?:ات|ين|ون|ية|يه|ها|هم|هن|كم|نا|ه|ي|ا|ة|ه)?"
_LATIN_SUFFIX = r"(?:'s|s|es|ed|ing)?"


def normalise(text: object) -> str:
    """Case-folded, Arabic-normalised text: alef forms to alef, taa marbuta
    to haa, alef maqsura to yaa, diacritics and tatweel removed, so
    ``الثقافة`` and ``ثقافية`` and ``ثَقافة`` meet."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _TASHKEEL.sub("", text)
    text = (text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
            .replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي"))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def term_pattern(term: str) -> re.Pattern:
    """A whole-word match for the term, tolerant of the clitics and plural
    endings Arabic and English attach to it."""
    words = normalise(term).split(" ")
    parts = []
    for word in words:
        if _ARABIC.search(word):
            # taa marbuta (normalised to haa) drops before the plural ending:
            # مكتبة, مكتبات and المكتبات are one word
            stem = word[:-1] if len(word) > 3 and word.endswith("ه") else word
            parts.append(_AR_PREFIX + re.escape(stem) + ("(?:ه)?" if stem != word else "") + _AR_SUFFIX)
        elif len(word) > 3 and word.endswith("y"):
            parts.append(re.escape(word[:-1]) + "(?:y|ies|y's)")
        else:
            parts.append(re.escape(word) + _LATIN_SUFFIX)
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.I)


def find_term(pattern: re.Pattern, text: str, *, limit: int = 3) -> list[dict]:
    """Where a term occurs in normalised text, with a snippet around each."""
    found = []
    for match in pattern.finditer(text):
        start, end = max(0, match.start() - 60), min(len(text), match.end() + 60)
        found.append({"at": match.start(), "snippet": text[start:end].strip()})
        if len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------
# The page as the judge sees it
# ---------------------------------------------------------------------------

@dataclass
class PageText:
    url: str
    title: str = ""
    headline: str = ""
    description: str = ""
    section: str = ""
    tags: list[str] = field(default_factory=list)
    keywords: str = ""
    published: Optional[str] = None
    language: str = ""
    body: str = ""
    main_found: bool = False

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "headline": self.headline,
                "description": self.description, "section": self.section, "tags": list(self.tags),
                "keywords": self.keywords, "published": self.published, "language": self.language,
                "words": self.word_count, "main_found": self.main_found}


_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
_CHROME_TAGS = {"nav", "header", "footer", "aside", "form", "menu"}
_MAIN_TAGS = {"article", "main"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "br", "tr", "td", "th", "blockquote", "pre", "figcaption", "dd", "dt", "summary"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.h1: list[str] = []
        self.meta: dict[str, list[str]] = {}
        self.times: list[str] = []
        self.jsonld: list[str] = []
        self.lang = ""
        self._body: list[str] = []
        self._main: list[str] = []
        self._stack: list[str] = []
        self._skip = 0
        self._chrome = 0
        self._main_depth = 0
        self._in_title = False
        self._in_h1 = False
        self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        attrs_d = {k: (v or "") for k, v in attrs}
        if tag == "html" and attrs_d.get("lang"):
            self.lang = attrs_d["lang"]
        if tag == "meta":
            key = (attrs_d.get("property") or attrs_d.get("name") or "").lower()
            if key and attrs_d.get("content"):
                self.meta.setdefault(key, []).append(attrs_d["content"])
            return
        if tag == "time" and attrs_d.get("datetime"):
            self.times.append(attrs_d["datetime"])
        if tag == "script":
            if "ld+json" in attrs_d.get("type", "").lower():
                self._in_jsonld = True
        self._stack.append(tag)
        if tag in _SKIP_TAGS:
            self._skip += 1
        role = attrs_d.get("role", "").lower()
        if tag in _CHROME_TAGS or role in ("navigation", "banner", "contentinfo", "complementary"):
            self._chrome += 1
        if tag in _MAIN_TAGS or role == "main":
            self._main_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
        if tag in _BLOCK_TAGS:
            self._body.append("\n")
            if self._main_depth:
                self._main.append("\n")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_jsonld = False
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _CHROME_TAGS and self._chrome:
            self._chrome -= 1
        if tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag in _BLOCK_TAGS:
            self._body.append("\n")
            if self._main_depth:
                self._main.append("\n")
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if self._in_title:
            self.title.append(data)
            return
        if self._skip:
            return
        if self._in_h1:
            self.h1.append(data)
        if self._chrome and not self._main_depth:
            return
        self._body.append(data)
        if self._main_depth:
            self._main.append(data)


def _clean_block(parts: list[str]) -> str:
    text = "".join(parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _first(meta: dict, *keys: str) -> str:
    for key in keys:
        values = meta.get(key)
        if values:
            return values[0].strip()
    return ""


def _iso_date(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", text)
    if not match:
        return None
    y, m, d, hh, mm, ss = match.groups()
    return f"{y}-{m}-{d}T{hh or '00'}:{mm or '00'}:{ss or '00'}Z"


def _jsonld_fields(blocks: list[str]) -> dict:
    found: dict = {}
    for raw in blocks:
        try:
            loaded = json.loads(raw)
        except ValueError:
            continue
        items = loaded if isinstance(loaded, list) else [loaded]
        queue = list(items)
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                queue.extend(item["@graph"])
            for key in ("headline", "datePublished", "articleSection", "keywords", "inLanguage",
                        "description"):
                value = item.get(key)
                if value and key not in found:
                    if isinstance(value, list):
                        value = ", ".join(str(v) for v in value)
                    found[key] = str(value)
            about = item.get("about")
            if about and "about" not in found:
                names = []
                for entry in about if isinstance(about, list) else [about]:
                    if isinstance(entry, dict) and entry.get("name"):
                        names.append(str(entry["name"]))
                    elif isinstance(entry, str):
                        names.append(entry)
                if names:
                    found["about"] = ", ".join(names)
    return found


def extract_page_text(html: str, url: str) -> PageText:
    """The article as a person reads it: headline, standfirst, section,
    tags, date, and the main text without menus, headers and footers."""
    parser = _Extractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:                 # a hostile document is still a page
        log.debug("HTML parse trouble for %s: %s", url, exc)
    ld = _jsonld_fields(parser.jsonld)
    main = _clean_block(parser._main)
    body = _clean_block(parser._body)
    main_found = len(main) >= 200 or (bool(main) and len(main) >= len(body) * 0.3)
    tags = [t.strip() for t in parser.meta.get("article:tag", []) if t.strip()]
    keywords = ", ".join(v for v in [_first(parser.meta, "keywords", "news_keywords"),
                                     ld.get("keywords", ""), ld.get("about", "")] if v)
    published = (_iso_date(_first(parser.meta, "article:published_time", "og:published_time",
                                  "date", "dc.date", "dcterms.created", "pubdate", "publishdate"))
                 or _iso_date(ld.get("datePublished")) or next(
                     (d for d in (_iso_date(t) for t in parser.times) if d), None))
    return PageText(
        url=url,
        title=html_lib.unescape("".join(parser.title)).strip(),
        headline=(html_lib.unescape("".join(parser.h1)).strip() or ld.get("headline", "")
                  or _first(parser.meta, "og:title")),
        description=_first(parser.meta, "description", "og:description") or ld.get("description", ""),
        section=_first(parser.meta, "article:section") or ld.get("articleSection", ""),
        tags=tags, keywords=keywords, published=published,
        language=(parser.lang or ld.get("inLanguage", "") or "").split("-")[0].lower(),
        body=(main if main_found else body)[:60000],
        main_found=main_found)


# ---------------------------------------------------------------------------
# The rules judge
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    decision: str                      # keep | reject | unsure
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    matched: list[dict] = field(default_factory=list)
    hard: bool = False                 # a rule that no other judge overrides

    def to_dict(self) -> dict:
        return {"decision": self.decision, "score": self.score, "reasons": self.reasons,
                "matched": self.matched, "hard": self.hard}


@dataclass
class LinkDecision:
    decision: str                      # skip | fetch | hub
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    confidence: Optional[float] = None
    judge: str = "rules"

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reasons": self.reasons, "score": self.score,
                "confidence": self.confidence, "judge": self.judge}


class RulesJudge:
    """Deterministic, explainable: every point in the score names the rule
    and the passage that earned it."""

    def __init__(self, theme: ThemeConfig):
        self.theme = theme
        self._terms = [(t, term_pattern(t)) for t in theme.terms]
        self._excluded = [(t, term_pattern(t)) for t in theme.exclude_terms]
        self._url_include = _compiled(theme.url_include, "url_include")
        self._url_exclude = _compiled(theme.url_exclude, "url_exclude")
        self._hubs = _compiled(theme.hub_patterns, "hub_patterns")

    # -- URLs ----------------------------------------------------------------
    def url_excluded(self, url: str) -> Optional[str]:
        for pattern in self._url_exclude:
            if pattern.search(url):
                return pattern.pattern
        return None

    def url_included(self, url: str) -> Optional[str]:
        for pattern in self._url_include:
            if pattern.search(url):
                return pattern.pattern
        return None

    def is_hub(self, url: str) -> bool:
        return any(p.search(url) for p in self._hubs)

    # -- pages ---------------------------------------------------------------
    def judge_page(self, page: PageText) -> Verdict:
        theme = self.theme
        excluded = self.url_excluded(page.url)
        if excluded:
            return Verdict(REJECT, 0, [f"URL matches the exclusion rule {excluded!r}"], hard=True)
        if page.published and theme.date_from and page.published < theme.date_from:
            return Verdict(REJECT, 0, [f"published {page.published[:10]}, before {theme.date_from[:10]}"],
                           hard=True)
        if page.published and theme.date_to and page.published > theme.date_to:
            return Verdict(REJECT, 0, [f"published {page.published[:10]}, after {theme.date_to[:10]}"],
                           hard=True)
        head = normalise(" | ".join(v for v in (page.title, page.headline) if v))
        meta = normalise(" | ".join(v for v in ([page.section, page.keywords, page.description]
                                                 + list(page.tags)) if v))
        body = normalise(page.body)
        for term, pattern in self._excluded:
            hit = find_term(pattern, head, limit=1)
            if hit:
                return Verdict(REJECT, 0, [f"excluded term {term!r} in the headline"],
                               [{"term": term, "where": "headline", "snippet": hit[0]["snippet"],
                                 "excluded": True}], hard=True)
            hits = find_term(pattern, body, limit=3)
            if len(hits) >= 3:
                return Verdict(REJECT, 0, [f"excluded term {term!r} throughout the text"],
                               [{"term": term, "where": "body", "snippet": hits[0]["snippet"],
                                 "excluded": True}], hard=True)
        score = 0
        reasons: list[str] = []
        matched: list[dict] = []
        included = self.url_included(page.url)
        if included:
            score += 3
            reasons.append(f"URL matches {included!r}")
        head_points = meta_points = body_points = 0
        for term, pattern in self._terms:
            in_head = find_term(pattern, head, limit=1)
            if in_head and head_points < 9:
                head_points += 3
                matched.append({"term": term, "where": "headline", "snippet": in_head[0]["snippet"]})
                continue
            in_meta = find_term(pattern, meta, limit=1)
            if in_meta and meta_points < 6:
                meta_points += 2
                matched.append({"term": term, "where": "section or tags", "snippet": in_meta[0]["snippet"]})
                continue
            in_body = find_term(pattern, body, limit=3)
            if in_body and body_points < 5:
                gained = min(len(in_body), 5 - body_points)
                body_points += gained
                matched.append({"term": term, "where": "text", "count": len(in_body),
                                "snippet": in_body[0]["snippet"]})
        score += head_points + meta_points + body_points
        if head_points:
            reasons.append(f"{head_points // 3} term(s) in the headline")
        if meta_points:
            reasons.append(f"{meta_points // 2} term(s) in the section, tags or description")
        if body_points:
            reasons.append(f"{body_points} mention(s) in the text")
        if score >= theme.min_score:
            return Verdict(KEEP, score, reasons or ["meets the score"], matched)
        if score > 0:
            return Verdict(UNSURE, score, reasons + [f"score {score} is below {theme.min_score}"], matched)
        return Verdict(REJECT, 0, ["no theme term or rule matched"], matched)

    # -- links ---------------------------------------------------------------
    def judge_link(self, url: str, text: str = "", context: str = "") -> LinkDecision:
        excluded = self.url_excluded(url)
        if excluded:
            return LinkDecision(SKIP, [f"URL matches the exclusion rule {excluded!r}"])
        if self.is_hub(url):
            return LinkDecision(HUB, ["matches a hub pattern"])
        score = 0
        reasons: list[str] = []
        if self.url_included(url):
            score += 3
            reasons.append("URL matches an include rule")
        said = normalise(" | ".join(v for v in (text, context) if v))
        for term, pattern in self._terms:
            if find_term(pattern, said, limit=1):
                score += 2
                reasons.append(f"link text mentions {term!r}")
        for term, pattern in self._excluded:
            if find_term(pattern, normalise(text), limit=1):
                return LinkDecision(SKIP, [f"link text mentions the excluded term {term!r}"])
        return LinkDecision(FETCH, reasons or ["no rule decides; fetch and read"], score)


# ---------------------------------------------------------------------------
# The AI judge
# ---------------------------------------------------------------------------

class AIJudgeError(Exception):
    """The judge could not answer; the rules verdict stands and the reason
    is recorded."""


_PAGE_SYSTEM_NOMINAL = """You judge whether a web page belongs in an archival collection about one
theme. You are given the theme (a name, a brief written by a curator, their terms) and what is
known of the page. Decide from that alone; do not assume anything about pages you cannot see.

Answer with exactly one word and nothing else: yes, no, or unsure.
"yes" means the page's own subject is the theme, not a passing mention. "no" means it is about
something else. "unsure" is the honest answer when what you were given is too little to tell."""

_PAGE_SYSTEM_FULL = """You judge whether a web page belongs in an archival collection about one theme.
You are given the theme (a name, a brief written by a curator, and their terms) and the page's
extracted content: URL, title, headline, section, tags, date and main text. Decide from the
content alone; do not assume anything about pages you cannot see.

Answer with one JSON object and nothing else:
{"relevant": "yes" | "no" | "unsure", "confidence": 0.0-1.0, "reasons": "one or two sentences",
 "quotes": ["short passages copied exactly from the page that support the verdict"]}

"yes" means the page's own subject is the theme, not a passing mention. "no" means it is about
something else. "unsure" is an honest answer when the content is thin or ambiguous. Quotes must
be verbatim from the text you were given."""

_LINKS_SYSTEM = """You help a web crawler decide which links on a page are worth fetching for an
archival collection about one theme. You are given the theme and a numbered list of links with
the text of each link and the words around it on the page. You cannot open the links.

Name only the links you are confident about. "skip": confidently not about the theme (another
subject, a log-in page, a legal notice). "hub": a listing, section, tag, search or pagination
page that may lead to theme pages. Every other link will be fetched and read, so leave out
anything you are unsure of: a page fetched needlessly costs one request, a page skipped
wrongly is lost.

Answer with one JSON object and nothing else: {"skip": [numbers], "hub": [numbers]}"""


def _theme_block(theme: ThemeConfig) -> str:
    parts = [f"Theme name: {theme.name or '(unnamed)'}"]
    if theme.brief:
        parts.append(f"Curator's brief: {theme.brief}")
    if theme.terms:
        parts.append("Terms and phrases: " + "; ".join(theme.terms[:60]))
    if theme.exclude_terms:
        parts.append("Not this: " + "; ".join(theme.exclude_terms[:30]))
    if theme.languages:
        parts.append("Languages: " + ", ".join(theme.languages))
    if theme.date_from or theme.date_to:
        parts.append(f"Date window: {theme.date_from or 'any'} to {theme.date_to or 'any'}")
    for example in theme.examples[:8]:
        label = "RELEVANT" if example.get("relevant") else "NOT RELEVANT"
        parts.append(f"Example ({label}): {example.get('title') or example.get('url')}"
                     + (f" -- {example['text'][:300]}" if example.get("text") else ""))
    return "\n".join(parts)


_EXCERPT_CHARS = {"url": 0, "compact": 600, "full": 12000}


def page_prompt(theme: ThemeConfig, page: PageText, mode: str = "compact",
                max_chars: int = 0) -> str:
    """What the model is sent about a page. ``url``: the address and the
    title. ``compact``: those plus the headline, section, date and the first
    few hundred characters of the text, a few hundred tokens in all.
    ``full``: the whole extracted content. ``max_chars`` caps the whole
    prompt: the excerpt gives way first, then the theme's own text."""
    fields = [f"URL: {page.url}", f"Title: {page.title}"]
    if mode != "url":
        fields += [f"Headline: {page.headline}", f"Section: {page.section}",
                   f"Tags: {', '.join(page.tags)}", f"Published: {page.published or 'unknown'}"]
    if mode == "full":
        fields += [f"Keywords: {page.keywords}", f"Description: {page.description}",
                   f"Language: {page.language}"]
    head = _theme_block(theme) + "\n\n" + "\n".join(fields)
    limit = _EXCERPT_CHARS.get(mode, 600)
    if max_chars:
        if len(head) > max_chars:
            head = head[:max_chars]                    # the theme's text, trimmed last
        limit = min(limit, max(0, max_chars - len(head) - 40))
    text = ""
    if limit:
        body = page.body[:limit]
        text = ("\n\nMain text:\n" + body + ("\n\n[text truncated]" if len(page.body) > limit else ""))
    return head + text


def _nominal(answer: str) -> str:
    """One word into a decision: yes, no, or anything else is unsure."""
    words = re.findall(r"[a-z]+", answer.lower())
    first = words[0] if words else ""
    if first in ("yes", "relevant", "keep", "true"):
        return KEEP
    if first in ("no", "irrelevant", "not", "reject", "false"):
        return REJECT
    return UNSURE


def links_prompt(theme: ThemeConfig, links: list[dict]) -> str:
    lines = [_theme_block(theme), "", "Links:"]
    for index, link in enumerate(links):
        lines.append(f"{index}. {link.get('url')}\n   text: {str(link.get('text') or '')[:200]}"
                     f"\n   around: {str(link.get('context') or '')[:300]}")
    return "\n".join(lines)


def _json_object(text: str) -> dict:
    """The first JSON object in a reply, tolerating fences and prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        loaded = json.loads(text[start:index + 1])
                        if isinstance(loaded, dict):
                            return loaded
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    raise AIJudgeError("the model's answer held no JSON object")


def _verify_quotes(quotes: object, page: PageText) -> tuple[list[str], list[str]]:
    haystack = normalise(" ".join([page.title, page.headline, page.description, page.body]))
    verified, unverified = [], []
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, str) or not quote.strip():
            continue
        (verified if normalise(quote) in haystack else unverified).append(quote.strip()[:300])
    return verified[:6], unverified[:6]


class AIJudge:
    """Talks to a model. Subclasses supply ``_complete(system, user)`` and
    ``describe()``; this class builds the prompts, paces the calls and
    reads the answers."""

    provider = "abstract"

    def __init__(self, model: str, *, timeout: float = 60.0, tokens_per_minute: int = 0,
                 max_prompt_tokens: int = 0, sleep: Callable[[float], None] = time.sleep):
        self.model = model
        self.timeout = timeout
        self.tokens_per_minute = max(0, int(tokens_per_minute or 0))
        self.max_prompt_tokens = max(0, int(max_prompt_tokens or 0))
        self.sleep = sleep
        self.calls = 0
        self.failures = 0
        self.tokens_estimated = 0
        self.waits = 0
        self._recent: deque = deque()          # (monotonic time, estimated tokens)

    def describe(self) -> dict:
        return {"provider": self.provider, "model": self.model, "prompt_version": PROMPT_VERSION,
                "tokens_per_minute": self.tokens_per_minute or None,
                "max_prompt_tokens": self.prompt_budget or None}

    @property
    def prompt_budget(self) -> int:
        """Tokens one question may take: the setting, or a share of the
        minute's allowance sized for a paced crawl, or no cap."""
        if self.max_prompt_tokens:
            return max(MIN_PROMPT_TOKENS, self.max_prompt_tokens)
        if self.tokens_per_minute:
            return max(MIN_PROMPT_TOKENS, self.tokens_per_minute // QUESTIONS_PER_MINUTE)
        return 0

    @property
    def budget_chars(self) -> int:
        return self.prompt_budget * 4 if self.prompt_budget else 0

    def _complete(self, system: str, user: str, *, max_tokens: int) -> str:
        raise NotImplementedError

    # -- pacing -------------------------------------------------------------------
    def _pace(self, system: str, user: str, max_tokens: int) -> None:
        """Stay under the provider's tokens-per-minute allowance: an estimate
        of this call's tokens (four characters each, plus the answer) against
        what the last minute has already used; wait when it would not fit."""
        estimate = (len(system) + len(user)) // 4 + max_tokens
        self.tokens_estimated += estimate
        if not self.tokens_per_minute:
            return
        now = time.monotonic()
        while self._recent and now - self._recent[0][0] > 60.0:
            self._recent.popleft()
        used = sum(t for _, t in self._recent)
        if self._recent and used + estimate > self.tokens_per_minute:
            wait = max(0.5, 60.0 - (now - self._recent[0][0]) + 0.1)
            self.waits += 1
            log.info("AI judge: %d of %d tokens used this minute; waiting %.0fs",
                     used, self.tokens_per_minute, wait)
            self.sleep(wait)
            now = time.monotonic()
            while self._recent and now - self._recent[0][0] > 60.0:
                self._recent.popleft()
        self._recent.append((now, estimate))

    def _ask(self, system: str, user: str, *, max_tokens: int) -> str:
        self._pace(system, user, max_tokens)
        self.calls += 1
        try:
            return self._complete(system, user, max_tokens=max_tokens)
        except AIJudgeError:
            self.failures += 1
            raise
        except Exception as exc:
            self.failures += 1
            raise AIJudgeError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

    # -- pages -------------------------------------------------------------------
    def judge_page(self, theme: ThemeConfig, page: PageText) -> dict:
        mode = theme.ai_input if theme.ai_input in AI_INPUTS else "compact"
        prompt = page_prompt(theme, page, mode, self.budget_chars)
        system = _PAGE_SYSTEM_FULL if mode == "full" else _PAGE_SYSTEM_NOMINAL
        answer = self._ask(system, prompt, max_tokens=600 if mode == "full" else 8)
        record = {"model": self.model, "provider": self.provider, "input": mode,
                  "prompt_hash": hashlib.sha256((PROMPT_VERSION + system + prompt)
                                                .encode("utf-8")).hexdigest()[:16]}
        if mode != "full":
            decision = _nominal(answer)
            return {**record, "decision": decision, "answer": answer.strip()[:40],
                    "confidence": None, "reasons": "", "quotes": [], "quotes_not_in_page": []}
        try:
            parsed = _json_object(answer)
        except AIJudgeError:
            self.failures += 1
            raise
        relevant = str(parsed.get("relevant") or "unsure").lower()
        decision = {"yes": KEEP, "no": REJECT}.get(relevant, UNSURE)
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        verified, unverified = _verify_quotes(parsed.get("quotes"), page)
        return {**record, "decision": decision, "confidence": confidence,
                "reasons": str(parsed.get("reasons") or "")[:600],
                "quotes": verified, "quotes_not_in_page": unverified}

    # -- links -------------------------------------------------------------------
    def triage_links(self, theme: ThemeConfig, links: list[dict]) -> list[dict]:
        """The links the model is confident about, as ``{"i", "decision"}``
        with decision skip or hub; every other link is fetched."""
        if not links:
            return []
        out: list[dict] = []
        for start, chunk in self._link_chunks(theme, links):
            prompt = links_prompt(theme, chunk)
            answer = self._ask(_LINKS_SYSTEM, prompt, max_tokens=6 * len(chunk) + 40)
            try:
                parsed = _json_object(answer)
            except AIJudgeError:
                self.failures += 1
                raise
            seen: set[int] = set()
            for decision in (SKIP, HUB):
                values = parsed.get(decision)
                for value in values if isinstance(values, list) else []:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= index < len(chunk) and index not in seen:
                        seen.add(index)
                        out.append({"i": start + index, "decision": decision})
        return out

    def _link_chunks(self, theme: ThemeConfig, links: list[dict]) -> list[tuple[int, list[dict]]]:
        """Batches of links whose prompt fits the question budget; one
        batch when there is no budget."""
        budget = self.budget_chars
        if not budget:
            return [(0, links)]
        chunks: list[tuple[int, list[dict]]] = []
        start = 0
        while start < len(links):
            size = 1
            while (start + size < len(links)
                   and len(links_prompt(theme, links[start:start + size + 1])) <= budget):
                size += 1
            chunks.append((start, links[start:start + size]))
            start += size
        return chunks


class AnthropicJudge(AIJudge):
    """Claude through the official SDK. The key comes from Settings or the
    ANTHROPIC_API_KEY environment variable; it is never written anywhere."""

    provider = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: Optional[str] = None,
                 *, timeout: float = 60.0, tokens_per_minute: int = 0, max_prompt_tokens: int = 0):
        super().__init__(model or DEFAULT_ANTHROPIC_MODEL, timeout=timeout,
                         tokens_per_minute=tokens_per_minute, max_prompt_tokens=max_prompt_tokens)
        self.api_key = api_key or None
        self._client = None

    def _sdk(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AIJudgeError("the anthropic package is not installed; install it with: "
                                   "pip install anthropic") from exc
            kwargs = {"timeout": self.timeout, "max_retries": 2}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _complete(self, system: str, user: str, *, max_tokens: int) -> str:
        import anthropic
        client = self._sdk()
        request = dict(model=self.model, max_tokens=max(256, max_tokens), system=system,
                       messages=[{"role": "user", "content": user}],
                       output_config={"effort": "medium"})
        try:
            try:
                # a refusal by the model's safety layer is answered by a
                # fallback model inside the same request
                response = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
            except TypeError:
                response = client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AIJudgeError("the API key was refused") from exc
        except anthropic.RateLimitError as exc:
            raise AIJudgeError("rate limited by the API") from exc
        except anthropic.APIStatusError as exc:
            raise AIJudgeError(f"API error {exc.status_code}: {getattr(exc, 'message', exc)}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIJudgeError(f"could not reach the API: {exc}") from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise AIJudgeError("the model declined to judge this page")
        return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")


class OpenAICompatibleJudge(AIJudge):
    """A model behind an OpenAI-style chat endpoint: a local Ollama or LM
    Studio, or any service speaking that shape. Nothing leaves the machine
    when the endpoint is local."""

    provider = "openai_compatible"

    def __init__(self, endpoint: str, model: str, api_key: Optional[str] = None,
                 *, timeout: float = 120.0, tokens_per_minute: int = 0, max_prompt_tokens: int = 0):
        super().__init__(model, timeout=timeout, tokens_per_minute=tokens_per_minute,
                         max_prompt_tokens=max_prompt_tokens)
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key or None

    def describe(self) -> dict:
        return {**super().describe(), "endpoint": self.endpoint}

    def _url(self) -> str:
        url = self.endpoint
        return url if url.endswith("/chat/completions") else url + "/chat/completions"

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _body(self, system: str, user: str, max_tokens: int) -> dict:
        return {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max(16, max_tokens)}

    def _complete(self, system: str, user: str, *, max_tokens: int) -> str:
        import httpx
        url = self._url()
        for attempt in range(4):
            try:
                response = httpx.post(url, json=self._body(system, user, max_tokens),
                                      headers=self._headers(), timeout=self.timeout)
            except httpx.HTTPError as exc:
                raise AIJudgeError(f"could not reach {url}: {exc}") from exc
            if response.status_code == 429 and attempt < 3:
                # the provider's own minute allowance: wait what it asks, then again
                retry_after = response.headers.get("retry-after", "")
                try:
                    wait = min(120.0, max(1.0, float(retry_after)))
                except ValueError:
                    wait = 10.0 * (attempt + 1)
                self.waits += 1
                log.info("AI judge: %s answered 429; waiting %.0fs", url, wait)
                self.sleep(wait)
                continue
            if response.status_code >= 400:
                raise AIJudgeError(f"HTTP {response.status_code} from {url}: {response.text[:200]}")
            try:
                payload = response.json()
                return str(payload["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise AIJudgeError("the endpoint's answer was not a chat completion") from exc
        raise AIJudgeError(f"{url} kept answering 429 Too Many Requests")


class AzureOpenAIJudge(OpenAICompatibleJudge):
    """Azure OpenAI: the resource's address, a deployment name in place of
    a model, the key in an api-key header, and an API version on the
    query string. Azure's tokens-per-minute allowance per deployment is
    what the pacing above is for."""

    provider = "azure_openai"

    def __init__(self, endpoint: str, deployment: str, api_key: Optional[str] = None,
                 *, api_version: str = DEFAULT_AZURE_API_VERSION, timeout: float = 120.0,
                 tokens_per_minute: int = 0, max_prompt_tokens: int = 0):
        super().__init__(endpoint, deployment, api_key, timeout=timeout,
                         tokens_per_minute=tokens_per_minute, max_prompt_tokens=max_prompt_tokens)
        self.api_version = api_version or DEFAULT_AZURE_API_VERSION

    def describe(self) -> dict:
        return {**super().describe(), "api_version": self.api_version, "deployment": self.model}

    def _url(self) -> str:
        base = self.endpoint
        if "/openai/deployments/" not in base:
            base = f"{base}/openai/deployments/{self.model}"
        if not base.endswith("/chat/completions"):
            base = base + "/chat/completions"
        return f"{base}?api-version={self.api_version}"

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        return headers

    def _body(self, system: str, user: str, max_tokens: int) -> dict:
        body = super()._body(system, user, max_tokens)
        body.pop("model", None)        # the deployment is in the address
        return body


def _whole(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ai_settings(get_setting: Callable[[str], Optional[str]]) -> dict:
    """The AI judge's settings as stored, the key replaced by whether one exists."""
    values = {key: (get_setting(SETTING_PREFIX + key) or "").strip() for key in SETTING_KEYS}
    provider = values.get("provider") or "none"
    if provider not in PROVIDERS:
        provider = "none"
    env_key = os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else ""
    key = values.pop("api_key", "") or env_key
    model = values.get("model") or (DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else "")
    return {"provider": provider, "endpoint": values.get("endpoint", ""),
            "model": model, "deployment": model if provider == "azure_openai" else None,
            "api_version": values.get("api_version") or DEFAULT_AZURE_API_VERSION,
            "has_key": bool(key), "key_from_environment": bool(env_key) and not values.get("api_key"),
            "max_calls": _whole(values.get("max_calls") or 2000, 2000),
            "tokens_per_minute": _whole(values.get("tokens_per_minute") or 0, 0),
            "max_prompt_tokens": _whole(values.get("max_prompt_tokens") or 0, 0)}


def make_ai_judge(get_setting: Callable[[str], Optional[str]]) -> Optional[AIJudge]:
    """The configured AI judge, or None when none is configured or usable."""
    settings = ai_settings(get_setting)
    provider = settings["provider"]
    model = settings["model"]
    key = ((get_setting(SETTING_PREFIX + "api_key") or "").strip()
           or (os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else ""))
    sizing = {"tokens_per_minute": settings["tokens_per_minute"],
              "max_prompt_tokens": settings["max_prompt_tokens"]}
    if provider == "anthropic":
        if not key:
            return None
        return AnthropicJudge(model or DEFAULT_ANTHROPIC_MODEL, key, **sizing)
    endpoint = settings["endpoint"]
    if provider == "openai_compatible":
        if not endpoint or not model:
            return None
        return OpenAICompatibleJudge(endpoint, model, key or None, **sizing)
    if provider == "azure_openai":
        if not endpoint or not model or not key:
            return None
        return AzureOpenAIJudge(endpoint, model, key, api_version=settings["api_version"], **sizing)
    return None


def ai_capability(get_setting: Callable[[str], Optional[str]]) -> dict:
    """What the dashboard says about the AI judge: available or why not."""
    settings = ai_settings(get_setting)
    provider = settings["provider"]
    if provider == "none":
        return {"available": False, "provider": "none", "model": None,
                "reason": "No AI judge is configured; themes use their rules alone."}
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return {"available": False, "provider": provider, "model": settings["model"],
                    "reason": "The anthropic package is not installed: pip install anthropic"}
        if not settings["has_key"]:
            return {"available": False, "provider": provider, "model": settings["model"],
                    "reason": "No API key is set for the Anthropic judge."}
        return {"available": True, "provider": provider, "model": settings["model"], "reason": None}
    if provider == "azure_openai":
        if not settings["endpoint"] or not settings["model"] or not settings["has_key"]:
            return {"available": False, "provider": provider, "model": settings["model"],
                    "reason": "The Azure OpenAI judge needs the resource address, a deployment "
                              "name and an API key."}
        return {"available": True, "provider": provider, "model": settings["model"], "reason": None}
    if not settings["endpoint"] or not settings["model"]:
        return {"available": False, "provider": provider, "model": settings["model"],
                "reason": "The OpenAI-compatible judge needs an endpoint and a model name."}
    return {"available": True, "provider": provider, "model": settings["model"], "reason": None}


# ---------------------------------------------------------------------------
# The selection log
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SelectionLog:
    """Every page judged and every link triaged, one line each, beside the
    WARC. This is what makes a thematic archive distinguishable from an
    incomplete one."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / SELECTION_FILE
        self.counts: dict[str, int] = {"pages_kept": 0, "pages_rejected": 0, "pages_unsure": 0,
                                       "links_skipped": 0, "links_fetch": 0, "links_hub": 0,
                                       "ai_page_calls": 0, "ai_link_calls": 0, "ai_failures": 0}

    def page(self, record: dict) -> None:
        decision = record.get("decision")
        if decision == KEEP:
            self.counts["pages_kept"] += 1
        elif decision == REJECT:
            self.counts["pages_rejected"] += 1
        elif decision == UNSURE:
            self.counts["pages_unsure"] += 1
        self._append({"kind": "page", "time": _iso_now(), **record})

    def links(self, records: Iterable[dict]) -> None:
        for record in records:
            decision = record.get("decision")
            if decision == SKIP:
                self.counts["links_skipped"] += 1
            elif decision == HUB:
                self.counts["links_hub"] += 1
            else:
                self.counts["links_fetch"] += 1
            self._append({"kind": "link", "time": _iso_now(), **record})

    def _append(self, record: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.warning("Could not write the selection log: %s", exc)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


# ---------------------------------------------------------------------------
# The judge the crawler and the recorder talk to
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    decision: str
    judge: str                          # rules | ai | both
    reason: str
    rules: dict
    ai: Optional[dict] = None
    hub: bool = False

    def to_dict(self) -> dict:
        return {"decision": self.decision, "judge": self.judge, "reason": self.reason,
                "rules": self.rules, "ai": self.ai, "hub": self.hub}


class ThemeJudge:
    """Rules first, the AI when configured, the policy in between."""

    def __init__(self, theme: ThemeConfig, ai: Optional[AIJudge] = None,
                 log_dir: Optional[Path] = None, *, max_ai_calls: int = 2000):
        self.theme = theme
        self.rules = RulesJudge(theme)
        self.ai = ai if (ai is not None and theme.ai_enabled) else None
        self.log = SelectionLog(log_dir) if log_dir is not None else None
        self.max_ai_calls = max_ai_calls
        self._triaged: dict[str, LinkDecision] = {}
        self._ai_exhausted_logged = False
        self.started_at = _iso_now()

    # -- describing -------------------------------------------------------------
    def describe(self) -> dict:
        return {"theme": self.theme.to_dict(), "theme_fingerprint": self.theme.fingerprint,
                "ai": self.ai.describe() if self.ai else None,
                "policy": self.theme.ai_policy if self.ai else "rules_only",
                "link_triage": "ai" if (self.ai and self.theme.ai_triage_links) else "rules",
                "started_at": self.started_at}

    def _ai_available(self) -> bool:
        if self.ai is None:
            return False
        if self.ai.calls >= self.max_ai_calls:
            if not self._ai_exhausted_logged:
                self._ai_exhausted_logged = True
                log.warning("The AI judge reached its %d calls for this run; the rules "
                            "decide from here", self.max_ai_calls)
            return False
        return True

    # -- pages ----------------------------------------------------------------------
    def judge_page(self, page: PageText, *, hub: bool = False, seed: Optional[str] = None,
                   depth: Optional[int] = None, via: Optional[str] = None) -> Decision:
        rules = self.rules.judge_page(page)
        decision = Decision(rules.decision, "rules", "; ".join(rules.reasons), rules.to_dict(), hub=hub)
        if not rules.hard and self._ai_available():
            policy = self.theme.ai_policy
            wanted = (policy == "decide" or policy == "agree"
                      or (policy == "tie_break" and rules.decision == UNSURE))
            if wanted:
                try:
                    verdict = self.ai.judge_page(self.theme, page)
                    if self.log:
                        self.log.counts["ai_page_calls"] += 1
                    decision.ai = verdict
                    decision.judge = "both"
                    decision.decision, decision.reason = self._combine(rules, verdict, policy)
                except AIJudgeError as exc:
                    if self.log:
                        self.log.counts["ai_failures"] += 1
                    decision.ai = {"error": str(exc)}
                    decision.reason = f"{decision.reason} (AI judge unavailable: {exc})"
        if hub and decision.decision != KEEP:
            decision.reason = ("hub page: " + ("kept as navigation context" if self.theme.keep_hubs
                                               else "followed, not kept") + "; " + decision.reason)
            decision.decision = KEEP if self.theme.keep_hubs else REJECT
        if self.log:
            self.log.page({"url": page.url, "seed": seed, "depth": depth, "via": via,
                           **decision.to_dict(), "page": page.to_dict()})
        return decision

    @staticmethod
    def _combine(rules: Verdict, ai: dict, policy: str) -> tuple[str, str]:
        ai_decision = ai["decision"]
        confidence = ai.get("confidence")
        ai_reason = (f"AI: {ai_decision}" + (f" ({confidence:.2f})" if isinstance(confidence, float) else "")
                     + (f" {ai['reasons']}" if ai.get("reasons") else "")).strip()
        rules_reason = "rules: " + "; ".join(rules.reasons)
        if policy == "decide":
            return ai_decision, f"{ai_reason} | {rules_reason}"
        if policy == "tie_break":
            return ai_decision, f"rules unsure; {ai_reason}"
        # agree
        if rules.decision == ai_decision:
            return ai_decision, f"rules and AI agree | {ai_reason} | {rules_reason}"
        if REJECT in (rules.decision, ai_decision) and KEEP in (rules.decision, ai_decision):
            return UNSURE, f"rules and AI disagree | {ai_reason} | {rules_reason}"
        return UNSURE, f"one judge unsure | {ai_reason} | {rules_reason}"

    # -- links ----------------------------------------------------------------------
    def triage_links(self, links: list[dict], *, from_url: Optional[str] = None) -> list[dict]:
        """Each link with its decision; ``links`` are ``{url, text, context}``.
        Rules first; the AI, in one call, only for links the rules left open,
        and it may only skip what it is confident about."""
        results: list[dict] = []
        open_indexes: list[int] = []
        for index, link in enumerate(links):
            url = link.get("url") or ""
            cached = self._triaged.get(url)
            if cached is not None:
                results.append({**link, **cached.to_dict(), "cached": True})
                continue
            verdict = self.rules.judge_link(url, link.get("text") or "", link.get("context") or "")
            results.append({**link, **verdict.to_dict()})
            if verdict.decision == FETCH and verdict.score == 0:
                open_indexes.append(index)
        if open_indexes and self.theme.ai_triage_links and self._ai_available():
            batch = [links[i] for i in open_indexes]
            try:
                answers = self.ai.triage_links(self.theme, batch)
                if self.log:
                    self.log.counts["ai_link_calls"] += 1
                for answer in answers:
                    target = results[open_indexes[answer["i"]]]
                    target["judge"] = "ai"
                    target["decision"] = answer["decision"]
                    target["reasons"] = ["AI: confidently not the theme's" if answer["decision"] == SKIP
                                         else "AI: a listing page"]
            except AIJudgeError as exc:
                if self.log:
                    self.log.counts["ai_failures"] += 1
                log.warning("AI link triage failed (%s); every open link is fetched", exc)
        fresh = []
        for record in results:
            if not record.get("cached"):
                self._triaged[record.get("url") or ""] = LinkDecision(
                    record["decision"], record.get("reasons", []), record.get("score", 0),
                    record.get("confidence"), record.get("judge", "rules"))
                fresh.append({**record, "from_url": from_url})
        if self.log and fresh:
            self.log.links(fresh)
        return results

    # -- summary ------------------------------------------------------------------------
    def summary(self, extra: Optional[dict] = None) -> dict:
        return {"schema": "swm-theme-summary-v1", **self.describe(),
                "counts": dict(self.log.counts) if self.log else {},
                "ai_calls": self.ai.calls if self.ai else 0,
                "ai_failures": self.ai.failures if self.ai else 0,
                "ai_tokens_estimated": self.ai.tokens_estimated if self.ai else 0,
                "ai_rate_waits": self.ai.waits if self.ai else 0,
                "finished_at": _iso_now(), **(extra or {})}

    def write_summary(self, out_dir: Path, extra: Optional[dict] = None) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / SUMMARY_FILE
        target.write_text(json.dumps(self.summary(extra), indent=2, ensure_ascii=False),
                          encoding="utf-8")
        return target


def build_theme_judge(raw: object, get_setting: Optional[Callable[[str], Optional[str]]],
                      log_dir: Optional[Path]) -> Optional[ThemeJudge]:
    """The judge for a job, from its theme block and the server's settings;
    None when the job has no theme."""
    theme = ThemeConfig.from_dict(raw)
    if not theme.enabled:
        return None
    ai = None
    max_calls = 2000
    if theme.ai_enabled and get_setting is not None:
        ai = make_ai_judge(get_setting)
        max_calls = ai_settings(get_setting)["max_calls"]
    return ThemeJudge(theme, ai, log_dir, max_ai_calls=max_calls)


# ---------------------------------------------------------------------------
# Holding a page until it is judged
# ---------------------------------------------------------------------------

class PageHold:
    """A WARC session's ``write_exchange`` interface that keeps the exchanges
    of one page until the judge has read it, then commits them in order to
    the real session or lets them go."""

    def __init__(self) -> None:
        self._held: list[dict] = []
        self.total_bytes = 0
        self.committed = 0
        self.discarded = 0

    def write_exchange(self, **kwargs) -> None:
        self._held.append(kwargs)

    def __len__(self) -> int:
        return len(self._held)

    def commit(self, warc) -> int:
        count = 0
        for exchange in self._held:
            try:
                warc.write_exchange(**exchange)
                count += 1
            except Exception as exc:
                log.debug("Held exchange not written for %s: %s", exchange.get("url"), exc)
        self._held = []
        self.committed += count
        return count

    def discard(self) -> int:
        count = len(self._held)
        self._held = []
        self.discarded += count
        return count


# ---------------------------------------------------------------------------
# A page for the curator
# ---------------------------------------------------------------------------

def render_selection_page(out_dir: Path) -> Optional[Path]:
    """pages/selection.html: what the theme kept, held for review and
    turned away, with the reasons, from the selection log."""
    out_dir = Path(out_dir)
    log_path = out_dir / SELECTION_FILE
    if not log_path.exists():
        return None
    rows = SelectionLog(out_dir).rows()
    summary = {}
    try:
        summary = json.loads((out_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    theme = (summary.get("theme") or {})
    esc = html_lib.escape

    def page_row(record: dict) -> str:
        ai = record.get("ai") or {}
        ai_text = ""
        if ai.get("error"):
            ai_text = f"AI unavailable: {esc(str(ai['error']))}"
        elif ai:
            quotes = "".join(f"<li>{esc(q)}</li>" for q in ai.get("quotes") or [])
            confidence = ai.get("confidence")
            ai_text = (f"AI {esc(str(ai.get('decision')))}"
                       + (f" ({float(confidence):.2f})" if isinstance(confidence, (int, float)) else "")
                       + (f" [{esc(str(ai.get('input')))}]" if ai.get("input") else "")
                       + f": {esc(str(ai.get('reasons') or ai.get('answer') or ''))}"
                       + (f"<ul>{quotes}</ul>" if quotes else ""))
        matched = "".join(f"<li>{esc(str(m.get('term')))} in {esc(str(m.get('where')))}: "
                          f"<span class=snip>{esc(str(m.get('snippet') or ''))}</span></li>"
                          for m in (record.get("rules") or {}).get("matched") or [])
        page = record.get("page") or {}
        return (f"<tr class='{esc(str(record.get('decision')))}'><td>{esc(str(record.get('decision')))}"
                f"{' (hub)' if record.get('hub') else ''}</td>"
                f"<td><a href='{esc(str(record.get('url')))}'>{esc(str(page.get('headline') or page.get('title') or record.get('url')))}</a>"
                f"<div class=url>{esc(str(record.get('url')))}</div></td>"
                f"<td>{esc(str(page.get('published') or ''))[:10]}</td>"
                f"<td>{esc(str(record.get('judge')))}: {esc(str(record.get('reason') or ''))}"
                f"{'<ul>' + matched + '</ul>' if matched else ''}{'<div class=ai>' + ai_text + '</div>' if ai_text else ''}</td></tr>")

    def link_row(record: dict) -> str:
        return (f"<tr class='{esc(str(record.get('decision')))}'><td>{esc(str(record.get('decision')))}</td>"
                f"<td>{esc(str(record.get('text') or ''))}<div class=url>{esc(str(record.get('url')))}</div></td>"
                f"<td>{esc(str(record.get('judge') or 'rules'))}: {esc('; '.join(str(r) for r in record.get('reasons') or []))}</td></tr>")

    pages = [r for r in rows if r.get("kind") == "page"]
    links = [r for r in rows if r.get("kind") == "link" and r.get("decision") != FETCH]
    counts = summary.get("counts") or {}
    ai = summary.get("ai") or {}
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Selection: {esc(theme.get('name') or 'theme')}</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin-bottom:28px}}
td,th{{border-top:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}}tr.keep td:first-child{{color:#137333;font-weight:600}}
tr.reject td:first-child,tr.skip td:first-child{{color:#a50e0e}}tr.unsure td:first-child{{color:#b06000;font-weight:600}}
.url{{color:#666;font-size:12px;word-break:break-all}}.snip{{color:#555}}.ai{{margin-top:4px;padding:4px 8px;background:#f3f6fb;border-radius:4px}}
ul{{margin:4px 0 0 18px;padding:0}}p.brief{{background:#fafafa;padding:10px;border-left:3px solid #999}}</style></head><body>
<h1>Selection for the theme “{esc(theme.get('name') or '')}”</h1>
<p class=brief>{esc(theme.get('brief') or '')}</p>
<p>Judge: {esc(str(summary.get('policy') or 'rules_only'))}{(' with ' + esc(str(ai.get('provider'))) + ' / ' + esc(str(ai.get('model')))) if ai else ''}.
Pages kept {counts.get('pages_kept', 0)}, held for review {counts.get('pages_unsure', 0)}, turned away {counts.get('pages_rejected', 0)};
links skipped before fetching {counts.get('links_skipped', 0)}.</p>
<h2>Pages judged</h2><table><tr><th>Decision</th><th>Page</th><th>Date</th><th>Why</th></tr>{''.join(page_row(r) for r in pages)}</table>
<h2>Links not fetched, and hubs</h2><table><tr><th>Decision</th><th>Link</th><th>Why</th></tr>{''.join(link_row(r) for r in links)}</table>
</body></html>"""
    target = out_dir / "pages" / "selection.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target
