"""Listing a profile with gallery-dl, through the browser's own session.

The browser collector lists a profile by scrolling it and reading what
Instagram serves its own client. gallery-dl lists it the way Instaloader
did: it asks the per-user endpoint directly, in order, with pinned flags
and dates, using the signed-in browser's cookies exported to a temporary
file for the length of the call. Where that is the listing the curator
wants, this module answers the listing part of the client protocol and
leaves everything else -- media requested by the page, comments, the
WARC, the raw responses -- to the browser client it wraps.

gallery-dl is an optional install and a separate program: it is run as a
subprocess and never imported, and its output is kept in the package as
the response the listing was read from, so each post's provenance points
at it like any other.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .instagram import (InstagramError, InstagramPost, MediaItem, RateLimited,
                        TargetUnavailable)

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], str]


def gallery_dl_version() -> Optional[str]:
    try:
        return importlib.metadata.version("gallery-dl")
    except importlib.metadata.PackageNotFoundError:
        return None


def discovery_limit(mode: str, latest_n: Optional[int]) -> Optional[int]:
    """How far to list for a mode; None lists the whole profile.

    A profile can lead with pinned posts, so latest-N reads a buffer beyond
    N and lets the engine choose; the other modes need the whole listing.
    """
    if mode == "latest_n" and latest_n:
        return max(latest_n + 12, latest_n * 2)
    return None


# ---------------------------------------------------------------------------
# The session, lent to gallery-dl
# ---------------------------------------------------------------------------

def write_netscape_cookies(cookies: Iterable[dict], path: Path) -> int:
    """Write browser cookies in the file format gallery-dl reads."""
    lines = ["# Netscape HTTP Cookie File",
             "# The signed-in browser's Instagram session, lent to gallery-dl "
             "for one listing. Treat like a password.", ""]
    written = 0
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if not domain or not name:
            continue
        try:
            expires = max(0, int(float(cookie.get("expires") or 0)))
        except (TypeError, ValueError):
            expires = 0
        lines.append("\t".join([
            f"#HttpOnly_{domain}" if cookie.get("httpOnly") else domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expires), name, str(cookie.get("value") or "")]))
        written += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return written


def gallery_command(cookie_file: Path, url: str, limit: Optional[int]) -> list[str]:
    command = [sys.executable, "-m", "gallery_dl", "--no-input",
               "-C", str(cookie_file), "-j"]
    if limit:
        command += ["-o", f"extractor.instagram.max-posts={limit}"]
    return command + [url]


def run_gallery(command: list[str]) -> str:
    """Run gallery-dl and return its JSON output; Instagram's refusals are
    raised as the engine's conditions."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=1800)
    except FileNotFoundError as exc:
        raise InstagramError("gallery-dl could not be started: " + str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstagramError("gallery-dl did not finish within 30 minutes") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "")[-3000:]
        raise_for_output(detail, completed.returncode)
    return completed.stdout


def raise_for_output(detail: str, returncode: int) -> None:
    lowered = detail.lower()
    if "429" in lowered or "please wait" in lowered or "rate limit" in lowered:
        raise RateLimited(120.0, "gallery-dl was rate limited by Instagram")
    if "401" in lowered or "login required" in lowered or "not logged in" in lowered:
        from .instagram import LoginRequired
        raise LoginRequired("gallery-dl was not accepted as signed in")
    if "404" in lowered or "does not exist" in lowered or "not found" in lowered:
        raise TargetUnavailable("gallery-dl: " + detail.strip()[-300:])
    raise InstagramError(f"gallery-dl exited with {returncode}: {detail.strip()[-500:]}")


# ---------------------------------------------------------------------------
# Reading gallery-dl's output
# ---------------------------------------------------------------------------

def _dicts_in(value) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _dicts_in(child)


def _when(value) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def posts_from_gallery(output: str, username: str,
                       provenance: Optional[dict] = None) -> list[InstagramPost]:
    """gallery-dl's records for a profile, as the engine's posts, in
    gallery-dl's order (Instagram's: pinned first, then newest first)."""
    try:
        data = json.loads(output) if output.strip() else []
    except json.JSONDecodeError as exc:
        raise InstagramError("gallery-dl output was not JSON") from exc
    posts: dict[str, InstagramPost] = {}
    order: list[str] = []
    seen_media: dict[str, set] = {}
    for index, record in enumerate(_dicts_in(data)):
        code = record.get("post_shortcode")
        if not isinstance(code, str) or not code:
            continue
        post = posts.get(code)
        if post is None:
            pinned = record.get("pinned")
            likes = record.get("likes")
            comments = next((record[k] for k in ("comment_count", "comments_count")
                             if isinstance(record.get(k), int)), None)
            post = InstagramPost(
                media_id=str(record.get("post_id") or code), shortcode=code,
                owner_username=str(record.get("username") or username),
                owner_id=str(record.get("owner_id")) if record.get("owner_id") else None,
                kind="image",
                created_time=_when(record.get("post_date") or record.get("date")),
                caption=(record.get("description") or None),
                permalink_url=str(record.get("post_url")
                                  or f"https://www.instagram.com/p/{code}/"),
                likes_count=likes if isinstance(likes, int) else None,
                comments_count=comments,
                is_pinned=bool(pinned) if isinstance(pinned, (list, bool)) else False,
                source="gallery-dl",
                raw={"gallery_dl": record},
                provenance={**(provenance or {}), "path": str(index),
                            "listing_request": True, "connection": "gallery-dl",
                            "listing_user": username})
            posts[code] = post
            order.append(code)
            seen_media[code] = set()
        url = record.get("video_url") or record.get("display_url")
        if not url or url in seen_media[code]:
            continue
        seen_media[code].add(url)
        number = record.get("num")
        position = (int(number) - 1) if isinstance(number, int) and number > 0 \
            else len(post.media)
        post.media.append(MediaItem(
            url=str(url), kind="video" if record.get("video_url") else "image",
            position=position, width=record.get("width"), height=record.get("height"),
            thumbnail_url=record.get("display_url") if record.get("video_url") else None))
        post.raw.setdefault("gallery_dl_media", []).append(record)
    for post in posts.values():
        post.media.sort(key=lambda m: m.position)
        if len(post.media) > 1:
            post.kind = "carousel"
        elif post.media and post.media[0].kind == "video":
            kind = str((post.raw.get("gallery_dl") or {}).get("type") or "")
            post.kind = "reel" if "reel" in kind.lower() or "clip" in kind.lower() else "video"
    return [posts[code] for code in order]


# ---------------------------------------------------------------------------
# The client: gallery-dl for the listing, the browser for the rest
# ---------------------------------------------------------------------------

class _LazyListing:
    """A listing run on the first pull, and run again if that pull failed.

    The engine wraps each pull in its retry and rate-limit handling and
    asks the same iterator again afterwards; a generator that raised would
    be finished, so the listing is loaded here instead and stays unloaded
    until a run succeeds.
    """

    def __init__(self, load: Callable[[], list[InstagramPost]]):
        self._load = load
        self._items: Optional[list[InstagramPost]] = None
        self._index = 0

    def __iter__(self):
        return self

    def __next__(self) -> InstagramPost:
        if self._items is None:
            self._items = self._load()          # raises: still unloaded
        if self._index >= len(self._items):
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        return item


class GalleryListingClient:
    """The browser client, with profile listings answered by gallery-dl.

    ``inner`` is the browser client (any object answering the client
    protocol with a ``cookie_jar()``); ``runner`` runs a gallery-dl command
    and returns its output, injectable for tests.
    """

    version = "browser+gallery-dl"

    def __init__(self, inner, *, limit: Optional[int] = None,
                 runner: Runner = run_gallery,
                 base_url: str = "https://www.instagram.com"):
        self.inner = inner
        self.limit = limit
        self.runner = runner
        self.base_url = base_url.rstrip("/")
        self._sink: Optional[Callable[[dict, bytes], str]] = None
        self.commands: list[list[str]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def record_responses_to(self, sink) -> None:
        self._sink = sink
        forward = getattr(self.inner, "record_responses_to", None)
        if callable(forward):
            forward(sink)

    def profile_posts(self, username: str) -> Iterator[InstagramPost]:
        return _LazyListing(
            lambda: self._list(username, f"{self.base_url}/{username}/posts/", "posts"))

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        return _LazyListing(
            lambda: self._list(username, f"{self.base_url}/{username}/reels/", "reels"))

    def _list(self, username: str, url: str, surface: str) -> list[InstagramPost]:
        cookies = list(self.inner.cookie_jar()) if hasattr(self.inner, "cookie_jar") else []
        with tempfile.TemporaryDirectory(prefix="swm-gallery-dl-") as folder:
            cookie_file = Path(folder) / "cookies.txt"
            write_netscape_cookies(cookies, cookie_file)
            command = gallery_command(cookie_file, url, self.limit)
            self.commands.append(command)
            output = self.runner(command)
        reference = None
        if self._sink is not None:
            try:
                reference = self._sink({
                    "url": url, "method": "gallery-dl", "status": 0,
                    "content_type": "application/json", "resource_type": "listing",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "decoder": "gallery-dl", "tool": "gallery-dl",
                    "tool_version": gallery_dl_version(),
                    "limit": self.limit, "surface": surface,
                }, output.encode("utf-8"))
            except Exception as exc:
                log.warning("Could not keep the gallery-dl listing: %s", exc)
        posts = posts_from_gallery(output, username, {
            "response": reference, "url": url, "decoder": "gallery-dl"})
        for post in posts:
            post.surface = surface
        log.info("gallery-dl listed %d %s for @%s", len(posts), surface, username)
        return posts
