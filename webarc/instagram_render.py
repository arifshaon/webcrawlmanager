"""Render an Instagram capture package as browsable static pages.

The pages are built from the package's own records -- posts, media files,
comments and replies -- so they need no replay machinery and no network, and
they are the guaranteed way in whether or not a rendered WARC was made. Every
page says it is a rendering of extracted data, not the archived original.
"""
from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .facebook_render import (_STYLE, _page, _read_json, _read_jsonl,
                              _readable_date, _text, _write)

log = logging.getLogger(__name__)

SITE_DIR_NAME = "pages"

_EXTRA_STYLE = """
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: .6rem; }
.tile { display: block; position: relative; aspect-ratio: 1; overflow: hidden;
  border-radius: 8px; border: 1px solid var(--line); background: var(--ground); }
.tile img, .tile video { width: 100%; height: 100%; object-fit: cover; display: block; }
.tile .tag { position: absolute; top: .4rem; right: .4rem; background: rgba(0,0,0,.6);
  color: #fff; font-size: .7rem; padding: .1rem .4rem; border-radius: 4px; }
.tile .none { display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--soft); font-size: .8rem; padding: .6rem; text-align: center; }
.carousel { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: .6rem; margin-top: .8rem; }
.carousel img, .carousel video { width: 100%; height: auto; border-radius: 8px;
  border: 1px solid var(--line); background: var(--ground); display: block; }
.carousel .slot { position: relative; }
.carousel .slot .n { position: absolute; top: .4rem; left: .4rem; background: rgba(0,0,0,.6);
  color: #fff; font-size: .7rem; padding: .1rem .4rem; border-radius: 4px; }
.profile { display: flex; gap: 1rem; align-items: center; }
.profile img { width: 72px; height: 72px; border-radius: 50%; border: 1px solid var(--line); }
"""


def is_instagram_capture(directory: Path) -> bool:
    return (directory / "instagram-posts.jsonl").exists() or (
        directory / "instagram-manifest.json").exists()


def _media_file(media_index: dict, url: str) -> Optional[str]:
    entry = media_index.get(url)
    if isinstance(entry, dict):
        return entry.get("file")
    return entry if isinstance(entry, str) else None


def _media_element(name: Optional[str], url: str, prefix: str,
                   kind: str) -> str:
    if not name:
        return ('<div class="none">Not captured.<br><code>'
                f'{_text(url[:70])}</code></div>')
    safe = html.escape(name)
    if kind == "video" or name.endswith((".mp4", ".mov", ".webm")):
        return (f'<video controls preload="metadata" src="{prefix}{safe}">'
                f'<a href="{prefix}{safe}">video</a></video>')
    return f'<a href="{prefix}{safe}"><img src="{prefix}{safe}" loading="lazy" alt=""></a>'


def _post_media_markup(post: dict, media_index: dict, prefix: str) -> str:
    urls = post.get("media_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]
    if not urls:
        return '<div class="empty">No media recorded for this post.</div>'
    kind = str(post.get("kind") or "image")
    slots = []
    for index, url in enumerate(urls):
        name = _media_file(media_index, url)
        item_kind = "video" if kind in ("video", "reel") else "image"
        if name and name.endswith((".mp4", ".mov", ".webm")):
            item_kind = "video"
        number = (f'<span class="n">{index + 1} / {len(urls)}</span>'
                  if len(urls) > 1 else "")
        slots.append(f'<div class="slot">{number}'
                     f'{_media_element(name, url, prefix, item_kind)}</div>')
    return f'<div class="carousel">{"".join(slots)}</div>'


def _tile(post: dict, media_index: dict) -> str:
    urls = post.get("media_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]
    code = html.escape(str(post.get("shortcode") or ""))
    kind = str(post.get("kind") or "image")
    tag = {"carousel": "carousel", "video": "video", "reel": "reel"}.get(kind, "")
    inner = '<div class="none">No media</div>'
    if urls:
        name = _media_file(media_index, urls[0])
        if name and name.endswith((".mp4", ".mov", ".webm")):
            inner = f'<video muted preload="metadata" src="../media/{html.escape(name)}"></video>'
        elif name:
            inner = f'<img src="../media/{html.escape(name)}" loading="lazy" alt="">'
        else:
            inner = '<div class="none">Media not captured</div>'
    if len(urls) > 1:
        tag = f"{len(urls)} items"
    tag_markup = f'<span class="tag">{html.escape(tag)}</span>' if tag else ""
    return f'<a class="tile" href="posts/{code}.html">{inner}{tag_markup}</a>'


def _comment_markup(comment: dict) -> str:
    depth = int(comment.get("depth") or 0)
    css = "comment reply" if depth > 0 else "comment"
    who = _text(comment.get("author_username")) or "Unknown author"
    when = _readable_date(comment.get("created_time"))
    return (f'<div class="{css}"><div><span class="who">@{who}</span>'
            f'<span class="when">{when}</span></div>'
            f'<div class="what">{_text(comment.get("text"))}</div></div>')


def _counts_markup(post: dict) -> str:
    parts = []
    for label, key in (("likes", "likes_count"), ("comments", "comments_count"),
                       ("views", "video_view_count")):
        value = post.get(key)
        if value not in (None, ""):
            parts.append(f"<div>{value} {label}</div>")
    return f'<div class="counts">{"".join(parts)}</div>' if parts else ""


def _notice(has_warc: bool) -> str:
    replay = (" A rendered WARC sits alongside; open it with the replay server "
              "to see how Instagram presented these posts."
              if has_warc else
              " No rendered WARC was made for this capture; the media files, "
              "raw payloads and these pages are the whole of it.")
    return ('<div class="notice">These pages are built from the records and '
            'media SWM collected. They are not Instagram, and do not look '
            'like it.' + replay + '</div>')


_GRADES = {
    "reported_count_reached": "complete against Instagram's reported count",
    "partial": "partial",
    "capped": "limited by the capture's comment cap",
    "no_comments_reported": "none reported",
    "exhausted_unverified": "all Instagram exposed; count unverified",
    "stopped_by_curator": "stopped by the curator",
}


def _comment_grade(post: dict) -> str:
    capture = post.get("comment_capture") or {}
    status = capture.get("status")
    if not status:
        return ""
    reported = capture.get("reported")
    said = f" of {reported} reported" if isinstance(reported, int) else ""
    return f"{said}; {_GRADES.get(status, status)}"


def build_site(capture_dir: Path, site_dir: Optional[Path] = None) -> Path:
    capture_dir = Path(capture_dir).resolve()
    site_dir = Path(site_dir) if site_dir else capture_dir / SITE_DIR_NAME
    posts_dir = site_dir / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)

    posts = _read_jsonl(capture_dir / "instagram-posts.jsonl")
    comments = _read_jsonl(capture_dir / "instagram-comments.jsonl")
    manifest = _read_json(capture_dir / "instagram-manifest.json")
    media_index = _read_json(capture_dir / "instagram-media.json")
    profiles = _read_json(capture_dir / "instagram-profiles.json")
    has_warc = any(capture_dir.glob("*.warc.gz")) or any(capture_dir.glob("*.warc"))

    by_post: dict[str, list[dict]] = defaultdict(list)
    for comment in comments:
        by_post[str(comment.get("post_shortcode") or "")].append(comment)
    for thread in by_post.values():
        thread.sort(key=lambda c: (str(c.get("parent_comment_id") or c.get("comment_id")),
                                   int(c.get("depth") or 0),
                                   str(c.get("created_time") or "")))

    posts.sort(key=lambda p: (str(p.get("created_time") or "")), reverse=True)
    capture = manifest.get("capture", {})
    targets = capture.get("targets", [])
    title = ", ".join(t.get("label", "") for t in targets if t.get("label")) \
        or "Instagram capture"

    for post in posts:
        code = str(post.get("shortcode") or "")
        thread = by_post.get(code, [])
        caption = _text(post.get("caption")) or '<span class="empty">No caption.</span>'
        link = (f'<a href="{html.escape(str(post.get("permalink_url")))}">on Instagram</a>'
                if post.get("permalink_url") else "")
        owner = _text(post.get("owner_username"))
        thread_markup = "".join(_comment_markup(c) for c in thread) or (
            '<div class="empty">No comments were captured for this post.</div>')
        _write(posts_dir / f"{code}.html", _page(
            f"{code} — {title}",
            '<a class="back" href="../index.html">&larr; All posts</a>'
            + _notice(has_warc)
            + f'<div class="post"><div class="meta">'
            f'{_readable_date(post.get("created_time"))}'
            f'{" &middot; @" + owner if owner else ""}'
            f'{" &middot; " + link if link else ""}'
            f'{" &middot; pinned" if post.get("is_pinned") else ""}</div>'
            f'<div class="body">{caption}</div>'
            f'{_post_media_markup(post, media_index, "../../media/")}'
            f'{_counts_markup(post)}</div>'
            + f"<h2>Comments ({len(thread)} captured{_comment_grade(post)})</h2>{thread_markup}"
        ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))

    heads = []
    for username, profile in (profiles or {}).items():
        pic = _media_file(media_index, str(profile.get("profile_pic_url") or ""))
        pic_markup = f'<img src="../media/{html.escape(pic)}" alt="">' if pic else ""
        heads.append(
            f'<div class="head"><div class="profile">{pic_markup}<div>'
            f'<h1>@{_text(username)}</h1>'
            f'<div class="sub">{_text(profile.get("full_name"))}</div>'
            f'<div class="sub">{_text(profile.get("biography"))}</div></div></div>'
            f'<div class="facts"><div><span>Posts on profile</span>'
            f'{_text(profile.get("posts_count"))}</div>'
            f'<div><span>Followers</span>{_text(profile.get("followers_count"))}</div>'
            f'<div><span>Private</span>{"yes" if profile.get("is_private") else "no"}</div>'
            f'</div></div>')

    facts = [("Posts captured", len(posts)), ("Comments", len(comments)),
             ("Media files", len(media_index))]
    fact_markup = "".join(f"<div><span>{l}</span>{v}</div>" for l, v in facts)
    coverage = manifest.get("coverage", {})
    rows = [
        ("Targets", ", ".join(t.get("url", "") for t in targets)),
        ("Capture mode", capture.get("mode", "")),
        ("Viewer", capture.get("viewer", "")),
        ("Newest captured post", coverage.get("newest_post") or "-"),
        ("Oldest captured post", coverage.get("oldest_post") or "-"),
        ("Stopped because", capture.get("stop_reason", "")),
        ("Captured by", capture.get("operator", "")),
    ]
    table = "".join(
        f"<tr><td>{html.escape(l)}</td><td>{_text(v)}</td></tr>"
        for l, v in rows if v not in (None, ""))
    grid = "".join(_tile(p, media_index) for p in posts) or \
        '<div class="empty">No posts were captured.</div>'

    _write(site_dir / "index.html", _page(
        f"{title} — captured posts",
        _notice(has_warc) + "".join(heads)
        + f'<div class="post"><div class="facts">{fact_markup}</div>'
        f'<table class="meta-table" style="margin-top:.8rem">{table}</table></div>'
        + f'<div class="grid">{grid}</div>'
    ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))
    log.info("Built Instagram pages for %d post(s) at %s", len(posts), site_dir)
    return site_dir
