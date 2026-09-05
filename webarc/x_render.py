"""Render an X capture package as browsable static pages.

The pages are built from the package's own records -- posts, media files,
conversations -- so they need no replay machinery and no network. Every
page says it is a rendering of extracted data, not the archived original.
A repost is shown as the account republishing someone else's post, and a
quote as the account's post with the quoted post inside it; neither is
ever presented as the account having written the original.
"""
from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .facebook_render import (_description_table, _page, _read_json, _read_jsonl,
                              _readable_date, _text, _write)

log = logging.getLogger(__name__)

SITE_DIR_NAME = "pages"

_EXTRA_STYLE = """
.timeline { display: flex; flex-direction: column; gap: .8rem; }
.entry { border: 1px solid var(--line); border-radius: 10px; padding: .9rem 1rem;
  background: var(--paper, #fff); }
.entry .who { font-weight: 600; }
.entry .handle { color: var(--soft); margin-left: .3rem; }
.entry .kind { display: inline-block; font-size: .72rem; padding: .1rem .5rem;
  border-radius: 999px; border: 1px solid var(--line); color: var(--soft); margin-left: .4rem; }
.entry .body { white-space: pre-wrap; margin: .5rem 0; }
.embedded { border: 1px solid var(--line); border-radius: 8px; padding: .7rem .8rem;
  margin: .6rem 0; background: var(--ground); }
.embedded .label { font-size: .75rem; color: var(--soft); margin-bottom: .3rem; }
.media { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: .6rem; margin-top: .6rem; }
.media img, .media video { width: 100%; height: auto; border-radius: 8px;
  border: 1px solid var(--line); background: var(--ground); display: block; }
.media .none { display: flex; align-items: center; justify-content: center;
  min-height: 80px; color: var(--soft); font-size: .8rem; padding: .6rem; text-align: center;
  border: 1px dashed var(--line); border-radius: 8px; }
.context { opacity: .9; margin-left: 1.2rem; border-left: 3px solid var(--line); }
.profile { display: flex; gap: 1rem; align-items: center; }
.profile img { width: 72px; height: 72px; border-radius: 50%; border: 1px solid var(--line); }
"""

_RELATIONSHIP_LABEL = {
    "repost": "reposted", "quote": "quote post", "reply": "reply", "original": "",
}

_GRADES = {
    "reported_count_reached": "complete against X's reported reply count",
    "partial": "partial",
    "capped": "limited by the capture's reply cap",
    "no_replies_reported": "none reported",
    "exhausted_unverified": "all X exposed; count unverified",
    "stopped_by_curator": "stopped by the curator",
}


def is_x_capture(directory: Path) -> bool:
    return (directory / "x-posts.jsonl").exists() or (directory / "x-manifest.json").exists()


def _media_file(media_index: dict, url: str) -> Optional[str]:
    entry = media_index.get(url)
    if isinstance(entry, dict):
        return entry.get("file")
    return entry if isinstance(entry, str) else None


def _media_element(name: Optional[str], url: str, prefix: str, kind: str) -> str:
    if not name:
        return ('<div class="none">Not captured.<br><code>'
                f'{_text(url[:70])}</code></div>')
    safe = html.escape(name)
    if kind in ("video", "gif") or name.endswith((".mp4", ".mov", ".webm")):
        loop = " loop muted autoplay" if kind == "gif" else ""
        return (f'<video controls preload="metadata"{loop} src="{prefix}{safe}">'
                f'<a href="{prefix}{safe}">video</a></video>')
    return f'<a href="{prefix}{safe}"><img src="{prefix}{safe}" loading="lazy" alt=""></a>'


def _urls_of(post: dict) -> list[str]:
    urls = post.get("media_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]
    return [u for u in urls if isinstance(u, str)]


def _media_markup(items: list[dict], media_index: dict, prefix: str) -> str:
    if not items:
        return ""
    slots = []
    for item in items:
        url = str(item.get("url") or "")
        name = item.get("file") or _media_file(media_index, url)
        kind = str(item.get("kind") or "image")
        slots.append(_media_element(name, url, prefix, kind))
    return f'<div class="media">{"".join(slots)}</div>'


def _post_media_items(post: dict) -> list[dict]:
    urls = _urls_of(post)
    files = post.get("media_files") or []
    kinds = []
    for url in urls:
        lowered = url.lower()
        kinds.append("video" if ".mp4" in lowered else "image")
    return [{"url": u, "file": (files[i] if i < len(files) else None), "kind": kinds[i]}
            for i, u in enumerate(urls)]


def _handle_markup(handle: str) -> str:
    return f'<span class="handle">@{handle}</span>' if handle else ""


def _embedded_markup(embedded: Optional[dict], label: str, media_index: dict,
                     prefix: str) -> str:
    if not embedded:
        return ""
    if embedded.get("unavailable"):
        return (f'<div class="embedded"><div class="label">{html.escape(label)}</div>'
                f'<div class="empty">Not available: {_text(embedded.get("unavailable"))}</div></div>')
    who = _text(embedded.get("author_name")) or ""
    handle = _text(embedded.get("author_handle"))
    when = _readable_date(embedded.get("created_time"))
    link = (f' &middot; <a href="{html.escape(str(embedded.get("permalink_url")))}">on X</a>'
            if embedded.get("permalink_url") else "")
    return (f'<div class="embedded"><div class="label">{html.escape(label)}</div>'
            f'<div><span class="who">{who}</span>'
            f'{_handle_markup(handle)}'
            f' &middot; {when}{link}</div>'
            f'<div class="body">{_text(embedded.get("text"))}</div>'
            f'{_media_markup(list(embedded.get("media") or []), media_index, prefix)}</div>')


def _counts_markup(post: dict) -> str:
    parts = []
    for label, key in (("replies", "reply_count"), ("reposts", "repost_count"),
                       ("likes", "like_count"), ("quotes", "quote_count"),
                       ("views", "view_count")):
        value = post.get(key)
        if value not in (None, ""):
            parts.append(f"<div>{value} {label}</div>")
    return f'<div class="counts">{"".join(parts)}</div>' if parts else ""


def _entry_markup(post: dict, media_index: dict, prefix: str, link_to_page: bool,
                  css: str = "entry") -> str:
    post_id = html.escape(str(post.get("post_id") or ""))
    relationship = str(post.get("relationship") or "original")
    who = _text(post.get("author_name")) or ""
    handle = _text(post.get("author_handle"))
    kind = _RELATIONSHIP_LABEL.get(relationship, relationship)
    tags = "".join(f'<span class="kind">{html.escape(t)}</span>' for t in
                   [kind] + (["pinned"] if post.get("is_pinned") else [])
                   + (["context, not the account's"] if post.get("capture_role") == "conversation_context" else [])
                   if t)
    when = _readable_date(post.get("created_time"))
    on_x = (f'<a href="{html.escape(str(post.get("permalink_url")))}">on X</a>'
            if post.get("permalink_url") else "")
    title = (f'<a href="posts/{post_id}.html">{when}</a>' if link_to_page else when)
    in_reply = ""
    if post.get("in_reply_to_post_id"):
        parent = html.escape(str(post["in_reply_to_post_id"]))
        target = f'<a href="{parent}.html">' if not link_to_page else f'<a href="posts/{parent}.html">'
        in_reply = (f'<div class="sub">Replying to @{_text(post.get("in_reply_to_handle")) or "…"} '
                    f'({target}{parent}</a>)</div>')
    if relationship == "repost":
        original = post.get("original_post") or {}
        head = (f'<div><span class="who">{who}</span>'
                f'{_handle_markup(handle)}{tags}'
                f' &middot; {title}{" &middot; " + on_x if on_x else ""}</div>'
                f'<div class="sub">Reposted a post by @{_text(original.get("author_handle")) or "an unknown author"}; '
                f'the account did not write it.</div>')
        body = _embedded_markup(original, "The post reposted", media_index, prefix)
        return f'<div class="{css}">{head}{body}{_counts_markup(post)}</div>'
    body = _text(post.get("text")) or '<span class="empty">No text.</span>'
    quoted = _embedded_markup(post.get("quoted_post"), "Quoted post, by its own author",
                              media_index, prefix)
    return (f'<div class="{css}"><div><span class="who">{who}</span>'
            f'{_handle_markup(handle)}{tags}'
            f' &middot; {title}{" &middot; " + on_x if on_x else ""}</div>'
            f'{in_reply}<div class="body">{body}</div>'
            f'{_media_markup(_post_media_items(post), media_index, prefix)}'
            f'{quoted}{_counts_markup(post)}</div>')


def _notice(has_warc: bool) -> str:
    replay = (" A WARC sits alongside; open it with the replay server to see how "
              "X presented these posts." if has_warc else
              " No WARC was made for this capture; the media files, raw responses "
              "and these pages are the whole of it.")
    return ('<div class="notice">These pages are built from the records and media '
            'SWM collected. They are not X, and do not look like it.' + replay + '</div>')


def _reply_grade(post: dict) -> str:
    capture = post.get("reply_capture") or {}
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

    posts = _read_jsonl(capture_dir / "x-posts.jsonl")
    manifest = _read_json(capture_dir / "x-manifest.json")
    media_index = _read_json(capture_dir / "x-media.json")
    users = _read_json(capture_dir / "x-users.json")
    has_warc = any(capture_dir.glob("*.warc.gz")) or any(capture_dir.glob("*.warc"))

    by_id = {str(p.get("post_id")): p for p in posts}
    targets_posts = [p for p in posts if p.get("capture_role") != "conversation_context"]
    context = [p for p in posts if p.get("capture_role") == "conversation_context"]
    by_context_for: dict[str, list[dict]] = defaultdict(list)
    for post in context:
        origin = post.get("provenance") or {}
        by_context_for[str(origin.get("context_for") or "")].append(post)
    for thread in by_context_for.values():
        thread.sort(key=lambda p: str(p.get("post_id") or ""))

    targets_posts.sort(key=lambda p: (0 if p.get("is_pinned") else 1,
                                      -int(str(p.get("post_id") or 0) or 0)))
    capture = manifest.get("capture", {})
    targets = capture.get("targets", [])
    title = ", ".join(t.get("label", "") for t in targets if t.get("label")) or "X capture"

    for post in posts:
        post_id = str(post.get("post_id") or "")
        parents = []
        parent_id = post.get("in_reply_to_post_id")
        while parent_id and str(parent_id) in by_id and len(parents) < 20:
            parent = by_id[str(parent_id)]
            parents.insert(0, parent)
            parent_id = parent.get("in_reply_to_post_id")
        thread = by_context_for.get(post_id, [])
        replies = [p for p in thread if p not in parents]
        above = "".join(_entry_markup(p, media_index, "../../media/", False, "entry context")
                        for p in parents)
        below = "".join(_entry_markup(p, media_index, "../../media/", False, "entry context")
                        for p in replies) or '<div class="empty">No replies were captured for this post.</div>'
        _write(posts_dir / f"{post_id}.html", _page(
            f"{post_id} — {title}",
            '<a class="back" href="../index.html">&larr; All posts</a>'
            + _notice(has_warc)
            + (f"<h2>What it replies to</h2>{above}" if above else "")
            + _entry_markup(post, media_index, "../../media/", False)
            + f"<h2>Replies ({len(replies)} captured{_reply_grade(post)})</h2>{below}"
        ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))

    heads = []
    for user_id, user in (users or {}).items():
        pic = _media_file(media_index, str(user.get("profile_image_url") or ""))
        pic_markup = f'<img src="../media/{html.escape(pic)}" alt="">' if pic else ""
        heads.append(
            f'<div class="head"><div class="profile">{pic_markup}<div>'
            f'<h1>{_text(user.get("name"))} <span class="handle">@{_text(user.get("handle"))}</span></h1>'
            f'<div class="sub">{_text(user.get("description"))}</div></div></div>'
            f'<div class="facts"><div><span>Posts on profile</span>{_text(user.get("posts_count"))}</div>'
            f'<div><span>Followers</span>{_text(user.get("followers_count"))}</div>'
            f'<div><span>Protected</span>{"yes" if user.get("is_protected") else "no"}</div>'
            f'</div></div>')

    counts = manifest.get("counts", {})
    facts = [("Posts captured", len(targets_posts)),
             ("of which reposts", counts.get("reposts", sum(
                 1 for p in targets_posts if p.get("relationship") == "repost"))),
             ("Context posts", len(context)), ("Media files", len(media_index))]
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
    table = "".join(f"<tr><td>{html.escape(l)}</td><td>{_text(v)}</td></tr>"
                    for l, v in rows if v not in (None, ""))
    timeline = "".join(_entry_markup(p, media_index, "../media/", True) for p in targets_posts) \
        or '<div class="empty">No posts were captured.</div>'
    described = "".join(_description_table(manifest, t.get("url")) for t in targets) \
        or _description_table(manifest, None)

    _write(site_dir / "index.html", _page(
        f"{title} — captured posts",
        _notice(has_warc) + "".join(heads) + described
        + f'<div class="post"><div class="facts">{fact_markup}</div>'
        f'<table class="meta-table" style="margin-top:.8rem">{table}</table></div>'
        + f'<div class="timeline">{timeline}</div>'
    ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))
    log.info("Built X pages for %d post(s) at %s", len(posts), site_dir)
    return site_dir
