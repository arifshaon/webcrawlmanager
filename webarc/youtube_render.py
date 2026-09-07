"""Render a YouTube capture package as browsable static pages.

Built from the package's own records: the channel, its videos with the
files downloaded for them, its posts with their images, and the comments
under both. The pages play a downloaded video file where the browser can,
and say plainly that they are a rendering of extracted data, not YouTube.
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .facebook_render import (_description_table, _page, _read_json, _read_jsonl,
                              _readable_date, _text, _write)

log = logging.getLogger(__name__)

SITE_DIR_NAME = "pages"

_EXTRA_STYLE = """
.tabs { display: flex; gap: 1rem; margin: 1rem 0 .6rem; border-bottom: 1px solid var(--line); }
.tabs a { padding: .4rem 0; text-decoration: none; color: var(--soft); }
.tabs a.on { color: var(--ink); border-bottom: 2px solid var(--ink); }
.list { display: flex; flex-direction: column; gap: .6rem; }
.item { display: grid; grid-template-columns: 200px 1fr; gap: .9rem; border: 1px solid var(--line);
  border-radius: 10px; padding: .7rem; background: var(--paper, #fff); }
.item img { width: 100%; border-radius: 6px; border: 1px solid var(--line); display: block; }
.item .none { display: flex; align-items: center; justify-content: center; min-height: 100px;
  color: var(--soft); font-size: .8rem; border: 1px dashed var(--line); border-radius: 6px; }
.item .title { font-weight: 600; }
.item .kind { display: inline-block; font-size: .72rem; padding: .1rem .5rem; border-radius: 999px;
  border: 1px solid var(--line); color: var(--soft); margin-left: .4rem; }
.entry { border: 1px solid var(--line); border-radius: 10px; padding: .9rem 1rem; background: var(--paper, #fff); }
.entry .body { white-space: pre-wrap; margin: .5rem 0; }
.media { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: .6rem; margin-top: .6rem; }
.media img { width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--line); display: block; }
video.main { width: 100%; max-height: 70vh; background: #000; border-radius: 8px; margin: .6rem 0; }
.files { font-size: .85rem; margin: .4rem 0; }
.files a { margin-right: .8rem; }
.poll li { padding: .2rem 0; }
.reply { margin-left: 1.5rem; border-left: 3px solid var(--line); }
.profile { display: flex; gap: 1rem; align-items: center; }
.profile img { width: 72px; height: 72px; border-radius: 50%; border: 1px solid var(--line); }
"""

_GRADES = {
    "reported_count_reached": "complete against YouTube's reported count",
    "partial": "partial",
    "capped": "limited by the capture's comment cap",
    "no_comments_reported": "none reported",
    "exhausted_unverified": "all YouTube exposed; count unverified",
    "stopped_by_curator": "stopped by the curator",
    "disabled": "comments are turned off",
    "blocked": "YouTube refused the request",
}


def is_youtube_capture(directory: Path) -> bool:
    return (directory / "youtube-manifest.json").exists() or \
        (directory / "youtube-videos.jsonl").exists() or (directory / "youtube-posts.jsonl").exists()


def _notice(has_warc: bool) -> str:
    replay = (" A WARC of the browser's exchanges while reading the Posts tab sits alongside; "
              "it holds no video streams." if has_warc else
              " No WARC was made for this capture; the media files, evidence, raw responses "
              "and these pages are the whole of it.")
    return ('<div class="notice">These pages are built from the records and media SWM '
            'collected. They are not YouTube, and do not look like it.' + replay + '</div>')


def _grade(item: dict) -> str:
    capture = item.get("comment_capture") or {}
    status = capture.get("status")
    if not status:
        return ""
    reported = capture.get("reported")
    said = f" of {reported} reported" if isinstance(reported, int) else ""
    return f"{said}; {_GRADES.get(status, status)}"


def _files_of(media_index: dict, prefix: str, role: Optional[str] = None,
              video_id: Optional[str] = None, post_id: Optional[str] = None) -> list[dict]:
    found = []
    for rel, entry in media_index.items():
        if not isinstance(entry, dict):
            continue
        if role and entry.get("role") != role:
            continue
        if video_id and entry.get("video_id") != video_id:
            continue
        if post_id and entry.get("post_id") != post_id:
            continue
        found.append({**entry, "file": rel})
    return found


def _comment_markup(comment: dict) -> str:
    css = "entry reply" if comment.get("reply_depth") else "entry"
    who = _text(comment.get("author_name")) or "Unknown author"
    when = _text(comment.get("published_text")) or _readable_date(comment.get("published_time"))
    flags = "".join(f'<span class="kind">{f}</span>' for f in (
        ["pinned"] if comment.get("is_pinned") else []) + (
        ["channel"] if comment.get("author_is_uploader") else []))
    likes = comment.get("like_count")
    return (f'<div class="{css}"><div><span class="who">{who}</span>{flags} '
            f'<span class="when">{when}</span></div>'
            f'<div class="body">{_text(comment.get("text"))}</div>'
            f'{f"<div class=counts><div>{likes} likes</div></div>" if likes not in (None, "") else ""}</div>')


def _thread_markup(comments: list[dict]) -> str:
    roots = [c for c in comments if not c.get("reply_depth")]
    replies: dict[str, list[dict]] = defaultdict(list)
    for comment in comments:
        if comment.get("reply_depth"):
            replies[str(comment.get("parent_id") or comment.get("thread_root_id") or "")].append(comment)
    parts = []
    for root in roots:
        parts.append(_comment_markup(root))
        for reply in replies.get(str(root.get("comment_id")), []):
            parts.append(_comment_markup(reply))
    orphans = [c for c in comments if c.get("reply_depth")
               and str(c.get("parent_id") or "") not in {str(r.get("comment_id")) for r in roots}]
    parts.extend(_comment_markup(c) for c in orphans)
    return "".join(parts) or '<div class="empty">No comments were captured.</div>'


def _video_item(video: dict, media_index: dict) -> str:
    vid = html.escape(str(video.get("video_id") or ""))
    thumbs = _files_of(media_index, "../media/", "thumbnail", video_id=video.get("video_id"))
    pic = (f'<img src="../{html.escape(thumbs[0]["file"])}" alt="" loading="lazy">' if thumbs
           else '<div class="none">No thumbnail captured</div>')
    kind = str(video.get("kind") or "video")
    availability = str(video.get("availability") or "")
    tags = "".join(f'<span class="kind">{html.escape(t)}</span>' for t in
                   ([kind] if kind != "video" else []) +
                   ([availability] if availability not in ("", "public", "unknown") else []))
    duration = video.get("duration_seconds")
    length = f" &middot; {int(duration) // 60}:{int(duration) % 60:02d}" if isinstance(duration, (int, float)) else ""
    media = video.get("media_file")
    return (f'<div class="item"><a href="videos/{vid}.html">{pic}</a><div>'
            f'<div class="title"><a href="videos/{vid}.html">{_text(video.get("title")) or vid}</a>{tags}</div>'
            f'<div class="sub">{_readable_date(video.get("published_time"))}{length}'
            f'{" &middot; " + _text(video.get("media_resolution")) if video.get("media_resolution") else ""}'
            f'{" &middot; file captured" if media else " &middot; no file captured"}</div>'
            f'<div class="body">{_text((video.get("description") or "")[:240])}</div></div></div>')


def _post_item(post: dict, media_index: dict) -> str:
    pid = html.escape(str(post.get("post_id") or ""))
    images = _files_of(media_index, "../media/", "post_image", post_id=post.get("post_id"))
    pic = (f'<img src="../{html.escape(images[0]["file"])}" alt="" loading="lazy">' if images
           else f'<div class="none">{html.escape(str(post.get("kind") or "text"))} post</div>')
    kind = str(post.get("kind") or "text")
    return (f'<div class="item"><a href="posts/{pid}.html">{pic}</a><div>'
            f'<div class="title"><a href="posts/{pid}.html">{_text(post.get("published_text")) or pid}</a>'
            f'<span class="kind">{html.escape(kind)}</span></div>'
            f'<div class="body">{_text((post.get("text") or "")[:300])}</div></div></div>')


def build_site(capture_dir: Path, site_dir: Optional[Path] = None) -> Path:
    capture_dir = Path(capture_dir).resolve()
    site_dir = Path(site_dir) if site_dir else capture_dir / SITE_DIR_NAME
    (site_dir / "videos").mkdir(parents=True, exist_ok=True)
    (site_dir / "posts").mkdir(parents=True, exist_ok=True)

    videos = _read_jsonl(capture_dir / "youtube-videos.jsonl")
    posts = _read_jsonl(capture_dir / "youtube-posts.jsonl")
    comments = _read_jsonl(capture_dir / "youtube-comments.jsonl")
    manifest = _read_json(capture_dir / "youtube-manifest.json")
    media_index = _read_json(capture_dir / "youtube-media.json")
    channels = _read_json(capture_dir / "youtube-channels.json")
    has_warc = any(capture_dir.glob("*.warc.gz")) or any(capture_dir.glob("*.warc"))

    by_target: dict[tuple, list[dict]] = defaultdict(list)
    for comment in comments:
        by_target[(str(comment.get("target_type")), str(comment.get("target_id")))].append(comment)

    videos.sort(key=lambda v: str(v.get("published_time") or ""), reverse=True)
    capture = manifest.get("capture", {})
    targets = capture.get("targets", [])
    title = ", ".join(t.get("label", "") for t in targets if t.get("label")) or "YouTube capture"

    for video in videos:
        vid = str(video.get("video_id") or "")
        files = _files_of(media_index, "../../media/", video_id=vid)
        main = next((f for f in files if f.get("role") == "video"), None)
        player = ""
        if main and str(main["file"]).lower().endswith((".mp4", ".webm")):
            player = (f'<video class="main" controls preload="metadata" src="../../{html.escape(main["file"])}">'
                      f'<a href="../../{html.escape(main["file"])}">video file</a></video>')
        elif main:
            player = f'<div class="files">Video file: <a href="../../{html.escape(main["file"])}">{html.escape(main["file"].rsplit("/", 1)[-1])}</a> ({_text(main.get("resolution"))})</div>'
        else:
            player = '<div class="empty">No video file was captured for this item.</div>'
        others = "".join(
            f'<a href="../../{html.escape(f["file"])}">{html.escape(f.get("role") or "file")}'
            f'{" (" + html.escape(str(f.get("language"))) + ")" if f.get("language") else ""}</a>'
            for f in files if f.get("role") not in ("video",))
        thread = by_target.get(("video", vid), [])
        _write(site_dir / "videos" / f"{vid}.html", _page(
            f"{_text(video.get('title')) or vid} — {title}",
            '<a class="back" href="../index.html">&larr; All items</a>' + _notice(has_warc)
            + f'<div class="post"><h1>{_text(video.get("title")) or vid}</h1>'
            f'<div class="meta">{_readable_date(video.get("published_time"))}'
            f'{" &middot; " + _text(video.get("kind")) if video.get("kind") else ""}'
            f'{" &middot; " + _text(video.get("availability")) if video.get("availability") not in (None, "", "public") else ""}'
            f'{" &middot; <a href=" + chr(34) + html.escape(str(video.get("url"))) + chr(34) + ">on YouTube</a>" if video.get("url") else ""}</div>'
            f'{player}{f"<div class=files>{others}</div>" if others else ""}'
            f'<div class="body">{_text(video.get("description"))}</div>'
            f'<div class="counts">'
            + "".join(f"<div>{video.get(k)} {l}</div>" for l, k in (("views", "view_count"), ("likes", "like_count"), ("comments", "comment_count")) if video.get(k) not in (None, ""))
            + '</div></div>'
            + f"<h2>Comments ({len(thread)} captured{_grade(video)})</h2>{_thread_markup(thread)}"
        ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))

    for post in posts:
        pid = str(post.get("post_id") or "")
        images = _files_of(media_index, "../../media/", "post_image", post_id=pid)
        gallery = "".join(f'<a href="../../{html.escape(i["file"])}"><img src="../../{html.escape(i["file"])}" alt="" loading="lazy"></a>'
                          for i in images)
        poll = post.get("poll") or {}
        poll_markup = ""
        if poll:
            options = "".join(f"<li>{_text(o.get('text'))}</li>" for o in poll.get("options") or [] if isinstance(o, dict))
            poll_markup = (f'<div class="poll"><ul>{options}</ul><div class="sub">'
                           f'{_text(poll.get("total_votes_text")) or ""} Results are not available: '
                           f'{_text(poll.get("results_reason"))}.</div></div>')
        video_link = (f'<div class="sub">Attached video: <a href="../videos/{html.escape(str(post.get("attached_video_id")))}.html">'
                      f'{html.escape(str(post.get("attached_video_id")))}</a></div>') if post.get("attached_video_id") else ""
        thread = by_target.get(("post", pid), [])
        _write(site_dir / "posts" / f"{pid}.html", _page(
            f"post {pid} — {title}",
            '<a class="back" href="../index.html">&larr; All items</a>' + _notice(has_warc)
            + f'<div class="post"><div class="meta">{_text(post.get("author_name"))} &middot; '
            f'{_text(post.get("published_text")) or _readable_date(post.get("published_time"))}'
            f'{" &middot; <a href=" + chr(34) + html.escape(str(post.get("url"))) + chr(34) + ">on YouTube</a>" if post.get("url") else ""}</div>'
            f'<div class="body">{_text(post.get("text")) or "<span class=empty>No text.</span>"}</div>'
            f'{f"<div class=media>{gallery}</div>" if gallery else ""}{poll_markup}{video_link}'
            + "".join(f"<div class=counts><div>{post.get(k)} {l}</div></div>" for l, k in (("likes", "like_count"),) if post.get(k) not in (None, ""))
            + '</div>'
            + f"<h2>Comments ({len(thread)} captured{_grade(post)})</h2>{_thread_markup(thread)}"
        ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))

    heads = []
    for channel_id, channel in (channels or {}).items():
        avatars = _files_of(media_index, "../media/", "avatar")
        avatar = next((a for a in avatars if a.get("channel_id") == channel_id), None)
        pic = f'<img src="../{html.escape(avatar["file"])}" alt="">' if avatar else ""
        heads.append(
            f'<div class="head"><div class="profile">{pic}<div>'
            f'<h1>{_text(channel.get("name"))} <span class="handle">@{_text(channel.get("handle"))}</span></h1>'
            f'<div class="sub">{_text((channel.get("description") or "")[:400])}</div></div></div>'
            f'<div class="facts"><div><span>Subscribers</span>{_text(channel.get("subscriber_count"))}</div>'
            f'<div><span>Videos on channel</span>{_text(channel.get("video_count"))}</div></div></div>')

    counts = manifest.get("counts", {})
    facts = [("Videos captured", len(videos)), ("Posts captured", len(posts)),
             ("Comments", len(comments)), ("Media files", len(media_index)),
             ("Unavailable items", counts.get("items_unavailable", 0))]
    fact_markup = "".join(f"<div><span>{l}</span>{v}</div>" for l, v in facts)
    coverage = manifest.get("coverage", {})
    rows = [("Targets", ", ".join(t.get("url", "") for t in targets)),
            ("Capture mode", capture.get("mode", "")),
            ("Viewer", capture.get("viewer", "")),
            ("Newest captured video", coverage.get("newest_video") or "-"),
            ("Oldest captured video", coverage.get("oldest_video") or "-"),
            ("Stopped because", capture.get("stop_reason", "")),
            ("Captured by", capture.get("operator", ""))]
    table = "".join(f"<tr><td>{html.escape(l)}</td><td>{_text(v)}</td></tr>" for l, v in rows if v not in (None, ""))
    video_list = "".join(_video_item(v, media_index) for v in videos) or \
        '<div class="empty">No videos were captured.</div>'
    post_list = "".join(_post_item(p, media_index) for p in posts) or \
        '<div class="empty">No posts were captured.</div>'
    described = "".join(_description_table(manifest, t.get("url")) for t in targets) \
        or _description_table(manifest, None)
    _write(site_dir / "index.html", _page(
        f"{title} — captured items",
        _notice(has_warc) + "".join(heads) + described
        + f'<div class="post"><div class="facts">{fact_markup}</div>'
        f'<table class="meta-table" style="margin-top:.8rem">{table}</table></div>'
        + f'<h2 id="videos">Videos, Shorts and streams</h2><div class="list">{video_list}</div>'
        + f'<h2 id="posts">Posts</h2><div class="list">{post_list}</div>'
    ).replace("<style>", "<style>" + _EXTRA_STYLE, 1))
    log.info("Built YouTube pages for %d video(s) and %d post(s) at %s", len(videos), len(posts), site_dir)
    return site_dir
