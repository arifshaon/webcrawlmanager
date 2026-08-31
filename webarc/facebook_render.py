"""Render a Facebook capture as browsable static pages.

A Facebook feed cannot be re-driven in a replay browser: its timeline and
comment pages are fetched by GraphQL POSTs whose bodies carry per-session and
per-request-order values, so a replay client cannot reconstruct the request
that addressed a given response. The capture's records are complete even when
replay shows only the page as first loaded, so this module renders those
records directly -- posts, their media, and their comment threads -- as plain
HTML that needs no replay machinery and no network.

These pages are a rendering of extracted data, not the archived original.
Every page says so, and links to the WARC replay when one exists, so the two
are never confused.
"""

from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

SITE_DIR_NAME = "pages"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_facebook_capture(directory: Path) -> bool:
    """Whether this directory holds a Facebook capture's exports."""
    return (directory / "facebook-posts.jsonl").exists() or (
        directory / "facebook-manifest.json").exists()


def _text(value: object) -> str:
    return html.escape(str(value)) if value not in (None, "") else ""


def _readable_date(value: object) -> str:
    raw = str(value or "")
    if not raw:
        return "date not captured"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return html.escape(raw)
    return parsed.strftime("%d %B %Y, %H:%M UTC")


def _sort_key(post: dict) -> tuple[int, str]:
    """Newest first, with undated posts last rather than interleaved."""
    date = str(post.get("created_time") or "")
    return (0, "") if not date else (1, date)


_STYLE = """
:root {
  --paper: #ffffff; --ground: #f0f2f5; --ink: #1c1e21; --soft: #65676b;
  --line: #ced0d4; --accent: #1b74e4; --notice: #fff3cd; --notice-line: #d8b657;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #242526; --ground: #18191a; --ink: #e4e6eb; --soft: #b0b3b8;
    --line: #3e4042; --accent: #2d88ff; --notice: #3a3323; --notice-line: #6d5c2b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 44rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
a { color: var(--accent); }
.notice { background: var(--notice); border: 1px solid var(--notice-line);
  border-radius: 8px; padding: .7rem .9rem; font-size: .85rem; margin-bottom: 1.2rem; }
.head { background: var(--paper); border: 1px solid var(--line);
  border-radius: 10px; padding: 1.2rem 1.4rem; margin-bottom: 1.2rem; }
.head h1 { margin: 0 0 .3rem; font-size: 1.4rem; }
.head .sub { color: var(--soft); font-size: .9rem; }
.facts { display: flex; flex-wrap: wrap; gap: 1.2rem; margin-top: .9rem;
  padding-top: .9rem; border-top: 1px solid var(--line); font-size: .85rem; }
.facts div span { display: block; color: var(--soft); font-size: .75rem;
  text-transform: uppercase; letter-spacing: .05em; }
.post { background: var(--paper); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.2rem; margin-bottom: 1rem; }
.post .meta { color: var(--soft); font-size: .8rem; margin-bottom: .5rem; }
.post .body { white-space: pre-wrap; overflow-wrap: anywhere; }
.media { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .8rem; }
.media img { max-width: 100%; width: 220px; height: auto; border-radius: 8px;
  border: 1px solid var(--line); background: var(--ground); }
.media .missing { width: 220px; padding: .8rem; border: 1px dashed var(--line);
  border-radius: 8px; color: var(--soft); font-size: .78rem; overflow-wrap: anywhere; }
.counts { margin-top: .8rem; padding-top: .6rem; border-top: 1px solid var(--line);
  color: var(--soft); font-size: .82rem; display: flex; gap: 1rem; flex-wrap: wrap; }
.comment { border-left: 3px solid var(--line); padding: .5rem 0 .5rem .8rem;
  margin-top: .7rem; }
.comment .who { font-weight: 600; font-size: .88rem; }
.comment .when { color: var(--soft); font-size: .75rem; margin-left: .4rem; }
.comment .what { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .92rem; }
.reply { margin-left: 1.6rem; border-left-color: var(--accent); }
.empty { color: var(--soft); font-style: italic; font-size: .88rem; margin-top: .8rem; }
.back { display: inline-block; margin-bottom: 1rem; font-size: .9rem; }
table.meta-table { width: 100%; border-collapse: collapse; font-size: .85rem; }
table.meta-table td { padding: .3rem .5rem .3rem 0; border-bottom: 1px solid var(--line);
  vertical-align: top; }
table.meta-table td:first-child { color: var(--soft); width: 14rem; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<div class=\"wrap\">\n{body}\n</div>\n</body>\n</html>\n"
    )


def _notice(has_warc: bool) -> str:
    replay = (
        " The WARC alongside this capture holds the original exchanges; open "
        "it with the replay server to see the Page as it first loaded."
        if has_warc else
        " No WARC was written for this capture, so these pages are the whole "
        "of what was collected."
    )
    return (
        "<div class=\"notice\">These pages are built from the records SWM "
        "extracted during the capture. They are not the archived Facebook "
        "pages and do not look like Facebook did." + replay + "</div>"
    )


def _media_markup(post: dict, media_index: dict, prefix: str) -> str:
    """``prefix`` is the route back to the media folder from the page being
    written: the index sits one level below it, a post page two."""
    urls = post.get("media_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]
    if not urls:
        return ""
    items = []
    for url in urls:
        name = media_index.get(url)
        if name:
            safe = html.escape(name)
            items.append(
                f'<a href="{prefix}{safe}"><img src="{prefix}{safe}" '
                f'loading="lazy" alt=""></a>')
        else:
            items.append(
                '<div class="missing">Not captured. Facebook never served '
                f'this file during the session.<br><code>{_text(url[:90])}</code></div>')
    return f'<div class="media">{"".join(items)}</div>'


def _comment_markup(comment: dict) -> str:
    depth = int(comment.get("depth") or 0)
    css = "comment reply" if depth > 0 else "comment"
    who = _text(comment.get("author_name")) or "Unknown author"
    when = _readable_date(comment.get("created_time"))
    return (
        f'<div class="{css}"><div><span class="who">{who}</span>'
        f'<span class="when">{when}</span></div>'
        f'<div class="what">{_text(comment.get("text"))}</div></div>'
    )


def _counts_markup(post: dict) -> str:
    parts = []
    for label, key in (("reactions", "reactions_count"),
                       ("comments", "comments_count"),
                       ("shares", "shares_count")):
        value = post.get(key)
        if value not in (None, ""):
            parts.append(f"<div>{value} {label}</div>")
    if not parts:
        return ""
    return f'<div class="counts">{"".join(parts)}</div>'


def build_site(capture_dir: Path, site_dir: Optional[Path] = None) -> Path:
    """Write browsable pages for a Facebook capture. Returns the site directory."""
    capture_dir = Path(capture_dir).resolve()
    site_dir = Path(site_dir) if site_dir else capture_dir / SITE_DIR_NAME
    posts_dir = site_dir / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)

    posts = _read_jsonl(capture_dir / "facebook-posts.jsonl")
    comments = _read_jsonl(capture_dir / "facebook-comments.jsonl")
    manifest = _read_json(capture_dir / "facebook-manifest.json")
    media_index = _read_json(capture_dir / "facebook-media.json")
    has_warc = any(capture_dir.glob("*.warc.gz")) or any(
        capture_dir.glob("*.warc"))

    by_post: dict[str, list[dict]] = defaultdict(list)
    for comment in comments:
        by_post[str(comment.get("parent_post_id") or "")].append(comment)
    for thread in by_post.values():
        thread.sort(key=lambda c: (int(c.get("depth") or 0),
                                   str(c.get("created_time") or "")))

    posts.sort(key=_sort_key, reverse=True)
    capture = manifest.get("capture", {})
    coverage = manifest.get("coverage", {})
    page_name = capture.get("page_name") or capture.get("page_url") or "Facebook Page"

    cards = []
    for post in posts:
        post_id = str(post.get("post_id") or "")
        thread = by_post.get(post_id, [])
        body = _text(post.get("text")) or '<span class="empty">No post text captured.</span>'
        permalink = post.get("permalink_url")
        link = (f'<a href="{html.escape(str(permalink))}">on Facebook</a>'
                if permalink else "")
        cards.append(
            f'<div class="post"><div class="meta">'
            f'{_readable_date(post.get("created_time"))}'
            f'{" &middot; " + _text(post.get("author_name")) if post.get("author_name") else ""}'
            f'{" &middot; " + link if link else ""}</div>'
            f'<div class="body">{body}</div>'
            f'{_media_markup(post, media_index, "../media/")}'
            f'{_counts_markup(post)}'
            f'<div class="counts"><div>'
            f'<a href="posts/{html.escape(post_id)}.html">'
            f'{len(thread)} captured comment(s) &rarr;</a></div></div></div>'
        )

        thread_markup = "".join(_comment_markup(c) for c in thread) or (
            '<div class="empty">No comments were captured for this post.</div>')
        _write(posts_dir / f"{post_id}.html", _page(
            f"Post {post_id} — {page_name}",
            '<a class="back" href="../index.html">&larr; All posts</a>'
            + _notice(has_warc)
            + f'<div class="post"><div class="meta">'
            f'{_readable_date(post.get("created_time"))}'
            f'{" &middot; " + link if link else ""}</div>'
            f'<div class="body">{body}</div>'
            f'{_media_markup(post, media_index, "../../media/")}{_counts_markup(post)}</div>'
            + f"<h2>Comments ({len(thread)} captured)</h2>{thread_markup}"))

    facts = [
        ("Posts", len(posts)),
        ("Comments", len(comments)),
        ("Media files", len(media_index)),
    ]
    fact_markup = "".join(
        f"<div><span>{label}</span>{value}</div>" for label, value in facts)
    rows = [
        ("Page", capture.get("page_url", "")),
        ("Capture mode", capture.get("mode", "")),
        ("Requested range", " to ".join(
            str(v) for v in (capture.get("parameters", {}).get("from"),
                             capture.get("parameters", {}).get("to")) if v)
         or "not a date-bounded capture"),
        ("Newest captured post", coverage.get("exported_newest_post") or "-"),
        ("Oldest captured post", coverage.get("exported_oldest_post") or "-"),
        ("Requested range satisfied", coverage.get("requested_range_satisfied")),
        ("Stopped because", capture.get("stop_reason", "")),
        ("Captured by", capture.get("operator", "")),
    ]
    table = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{_text(value)}</td></tr>"
        for label, value in rows if value not in (None, ""))

    _write(site_dir / "index.html", _page(
        f"{page_name} — captured posts",
        _notice(has_warc)
        + f'<div class="head"><h1>{_text(page_name)}</h1>'
        f'<div class="sub">{_text(capture.get("page_url", ""))}</div>'
        f'<div class="facts">{fact_markup}</div></div>'
        + f'<div class="post"><table class="meta-table">{table}</table></div>'
        + ("".join(cards) or '<div class="empty">No posts were captured.</div>')))

    log.info("Built Facebook pages for %d post(s) at %s", len(posts), site_dir)
    return site_dir


def _write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
