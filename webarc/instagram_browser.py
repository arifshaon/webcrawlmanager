"""Collecting Instagram through the browser Instagram is served to.

Instagram recognises and refuses other clients on sight -- a fresh,
signed-in session from a scraping library is answered with "please wait a
few minutes" before it has asked for anything. What Instagram cannot refuse
is its own client: a real Chrome, signed in, scrolling a profile the way a
person does. This module drives that browser and collects from what it
loads, with a window or without one.

It answers the client protocol the capture engine is written against --
stopping rules, media fixity, comment caps, holds, the manifest live there,
and are exercised with a stand-in. Where records come from:

* a profile page carries its first posts as JSON embedded in the HTML, and
  loads the rest through GraphQL as the page is scrolled; both are read off
  the browser's own responses, never requested separately;
* a post's page carries the post and its first comments the same way, and
  loads further comments as the thread is scrolled;
* media is fetched through the browser context, so it travels with the
  session and the browser's own fingerprint;
* every exchange the browser makes can be written to a WARC as it happens,
  with credentials redacted, which is the rendered record of how Instagram
  presented what was collected.

Instagram's payload shapes change; extraction is schema-tolerant and keeps
each record's source path and raw node, as the Facebook collector does.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlsplit

from .config import BrowserConfig
from .facebook import (_CAPTURE_ARGS, _walk, decode_graphql_documents,
                       extract_embedded_documents)
from .instagram import (CheckpointRequired, InstagramComment, InstagramError,
                        InstagramPost, InstagramProfile, LoginRequired,
                        MediaItem, RateLimited, TargetUnavailable)

log = logging.getLogger(__name__)

_INSTAGRAM_HOST = "instagram.com"
_API_MARKERS = ("/graphql/query", "/api/v1/", "/graphql")
_SESSION_COOKIES = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did")


# ---------------------------------------------------------------------------
# Reading Instagram's payloads
# ---------------------------------------------------------------------------

def _epoch_to_iso(value: object) -> Optional[str]:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10**11:          # milliseconds
        number /= 1000.0
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _text_of(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "content"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _best_image(node: dict) -> Optional[MediaItem]:
    versions = node.get("image_versions2")
    candidates = versions.get("candidates") if isinstance(versions, dict) else None
    if not isinstance(candidates, list) or not candidates:
        url = node.get("display_url") or node.get("thumbnail_src")
        return MediaItem(url=url, kind="image") if isinstance(url, str) else None
    best = None
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("url"):
            continue
        width = int(candidate.get("width") or 0)
        if best is None or width > (best.width or 0):
            best = MediaItem(url=str(candidate["url"]), kind="image",
                             width=width or None,
                             height=int(candidate.get("height") or 0) or None)
    return best


def _best_video(node: dict) -> Optional[MediaItem]:
    versions = node.get("video_versions")
    if isinstance(versions, list) and versions:
        best = None
        for version in versions:
            if not isinstance(version, dict) or not version.get("url"):
                continue
            width = int(version.get("width") or 0)
            if best is None or width > (best.width or 0):
                best = MediaItem(url=str(version["url"]), kind="video",
                                 width=width or None,
                                 height=int(version.get("height") or 0) or None)
        if best is not None:
            poster = _best_image(node)
            best.thumbnail_url = poster.url if poster else None
            return best
    url = node.get("video_url")
    if isinstance(url, str):
        poster = _best_image(node)
        return MediaItem(url=url, kind="video",
                         thumbnail_url=poster.url if poster else None)
    return None


def _media_of(node: dict) -> tuple[str, list[MediaItem]]:
    """The post's media in display order, and what kind of post it is."""
    carousel = node.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        items: list[MediaItem] = []
        for index, child in enumerate(carousel):
            if not isinstance(child, dict):
                continue
            item = (_best_video(child) if child.get("media_type") == 2
                    or child.get("video_versions") or child.get("is_video")
                    else _best_image(child))
            if item is not None:
                item.position = index
                items.append(item)
        return "carousel", items
    is_video = (node.get("media_type") == 2 or bool(node.get("video_versions"))
                or bool(node.get("is_video")))
    if is_video:
        item = _best_video(node)
        kind = "reel" if str(node.get("product_type") or "") == "clips" else "video"
        return kind, [item] if item else []
    item = _best_image(node)
    return "image", [item] if item else []


def _looks_like_post(obj: dict) -> bool:
    code = obj.get("code") or obj.get("shortcode")
    if not isinstance(code, str) or not code:
        return False
    return any(key in obj for key in (
        "taken_at", "taken_at_timestamp", "media_type", "image_versions2",
        "carousel_media", "video_versions", "display_url", "caption"))


def _looks_like_comment(obj: dict) -> bool:
    if "code" in obj or "shortcode" in obj:
        return False
    if not isinstance(obj.get("text"), str):
        return False
    if not (obj.get("pk") or obj.get("id")):
        return False
    return any(key in obj for key in ("created_at", "created_at_utc", "user",
                                      "owner", "comment_like_count"))


def _looks_like_profile(obj: dict) -> bool:
    if not isinstance(obj.get("username"), str):
        return False
    if _looks_like_post(obj) or _looks_like_comment(obj):
        return False
    return any(key in obj for key in ("media_count", "follower_count",
                                      "edge_owner_to_timeline_media",
                                      "biography", "is_private"))


def _owner_of(node: dict) -> tuple[Optional[str], Optional[str]]:
    for key in ("user", "owner"):
        owner = node.get(key)
        if isinstance(owner, dict):
            return (str(owner.get("pk") or owner.get("id") or "") or None,
                    owner.get("username"))
    return None, None


def post_from_node(node: dict, source_path: str = "") -> InstagramPost:
    code = str(node.get("code") or node.get("shortcode"))
    kind, media = _media_of(node)
    owner_id, owner_name = _owner_of(node)
    caption = node.get("caption")
    if isinstance(caption, dict):
        caption_text = _text_of(caption)
    elif isinstance(caption, str):
        caption_text = caption.strip() or None
    else:
        edges = (node.get("edge_media_to_caption") or {}).get("edges") \
            if isinstance(node.get("edge_media_to_caption"), dict) else None
        caption_text = None
        if isinstance(edges, list) and edges:
            caption_text = _text_of((edges[0] or {}).get("node"))
    likes = node.get("like_count")
    if likes is None and isinstance(node.get("edge_media_preview_like"), dict):
        likes = node["edge_media_preview_like"].get("count")
    comments = node.get("comment_count")
    if comments is None and isinstance(node.get("edge_media_to_comment"), dict):
        comments = node["edge_media_to_comment"].get("count")
    views = node.get("play_count") or node.get("view_count") \
        or node.get("video_view_count")
    return InstagramPost(
        media_id=str(node.get("pk") or node.get("id") or code),
        shortcode=code, owner_username=owner_name, owner_id=owner_id,
        kind=kind,
        created_time=_epoch_to_iso(node.get("taken_at")
                                   or node.get("taken_at_timestamp")),
        caption=caption_text,
        permalink_url=f"https://www.instagram.com/p/{code}/",
        likes_count=likes if isinstance(likes, int) else None,
        comments_count=comments if isinstance(comments, int) else None,
        video_view_count=views if isinstance(views, int) else None,
        media=media, source="browser", raw=node)


def comment_from_node(node: dict, shortcode: Optional[str],
                      parent: Optional[str], depth: int) -> InstagramComment:
    owner_id, owner_name = _owner_of(node)
    likes = node.get("comment_like_count")
    if likes is None:
        likes = node.get("like_count")
    return InstagramComment(
        comment_id=str(node.get("pk") or node.get("id")),
        post_shortcode=shortcode or "", parent_comment_id=parent,
        author_id=owner_id, author_username=owner_name,
        text=node.get("text"),
        created_time=_epoch_to_iso(node.get("created_at")
                                   or node.get("created_at_utc")),
        likes_count=likes if isinstance(likes, int) else None,
        depth=depth, raw=node)


def profile_from_node(node: dict) -> InstagramProfile:
    counts = node
    followers = node.get("follower_count")
    if followers is None and isinstance(node.get("edge_followed_by"), dict):
        followers = node["edge_followed_by"].get("count")
    following = node.get("following_count")
    if following is None and isinstance(node.get("edge_follow"), dict):
        following = node["edge_follow"].get("count")
    posts = node.get("media_count")
    if posts is None and isinstance(node.get("edge_owner_to_timeline_media"), dict):
        posts = node["edge_owner_to_timeline_media"].get("count")
    return InstagramProfile(
        user_id=str(node.get("pk") or node.get("id") or ""),
        username=str(node.get("username")),
        full_name=node.get("full_name"), biography=node.get("biography"),
        is_private=bool(node.get("is_private")),
        is_verified=bool(node.get("is_verified")),
        followers_count=followers if isinstance(followers, int) else None,
        following_count=following if isinstance(following, int) else None,
        posts_count=posts if isinstance(posts, int) else None,
        profile_pic_url=(node.get("profile_pic_url_hd")
                         or node.get("profile_pic_url")),
        external_url=node.get("external_url"), raw=counts)


def extract_instagram_records(documents, shortcode_hint: Optional[str] = None
                              ) -> tuple[list[InstagramPost], list[InstagramComment],
                                         list[InstagramProfile]]:
    """Posts, comments and profiles found anywhere in decoded payloads.

    Order is preserved as Instagram delivered it -- a profile's connection
    lists pinned posts first, then newest first -- because the engine reads
    pinned posts off that order. A comment's post is the nearest post node
    above it, then the page it was loaded on; its parent is the nearest
    comment node above it.
    """
    posts: "OrderedDict[str, InstagramPost]" = OrderedDict()
    comments: "OrderedDict[str, InstagramComment]" = OrderedDict()
    profiles: "OrderedDict[str, InstagramProfile]" = OrderedDict()
    for document in documents:
        for obj, path, ancestors in _walk(document):
            if not isinstance(obj, dict):
                continue
            if _looks_like_post(obj):
                record = post_from_node(obj, ".".join(path))
                if record.shortcode not in posts:
                    posts[record.shortcode] = record
                continue
            if _looks_like_comment(obj):
                enclosing_post = next(
                    (str(a.get("code") or a.get("shortcode"))
                     for a in reversed(ancestors)
                     if isinstance(a, dict) and _looks_like_post(a)), None)
                parent = next(
                    (str(a.get("pk") or a.get("id")) for a in reversed(ancestors)
                     if isinstance(a, dict) and _looks_like_comment(a)), None)
                depth = 1 if parent else 0
                record = comment_from_node(
                    obj, enclosing_post or shortcode_hint, parent, depth)
                if record.comment_id not in comments:
                    comments[record.comment_id] = record
                continue
            if _looks_like_profile(obj):
                record = profile_from_node(obj)
                if record.username not in profiles:
                    profiles[record.username] = record
    return list(posts.values()), list(comments.values()), list(profiles.values())


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

class _Observed:
    """What the browser has loaded so far, in the order it arrived."""

    def __init__(self):
        self.posts: "OrderedDict[str, InstagramPost]" = OrderedDict()
        self.comments: "OrderedDict[str, InstagramComment]" = OrderedDict()
        self.profiles: dict[str, InstagramProfile] = {}
        self.responses = 0
        self.api_responses = 0

    def take(self, posts, comments, profiles) -> int:
        added = 0
        for post in posts:
            if post.shortcode not in self.posts:
                self.posts[post.shortcode] = post
                added += 1
        for comment in comments:
            if comment.comment_id not in self.comments:
                self.comments[comment.comment_id] = comment
                added += 1
        for profile in profiles:
            self.profiles[profile.username] = profile
        return added


class InstagramBrowserClient:
    """Instagram through a signed-in browser, answering the client protocol.

    ``warc`` is a WarcSession (the redacting FacebookWarcSession in practice)
    or None. ``sleep`` is injectable so tests do not wait for real seconds.
    """

    version = "browser"

    def __init__(self, browser: BrowserConfig, warc=None,
                 sleep: Callable[[float], None] = time.sleep,
                 stall_rounds: int = 3, page_timeout: float = 60.0,
                 base_url: str = "https://www.instagram.com",
                 headless: bool = False,
                 settle: tuple[float, float] = (1.2, 2.2)):
        self.browser = browser
        self.warc = warc
        self.sleep = sleep
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(self.base_url).netloc.lower()
        self.headless = headless
        self.settle = settle
        self.stall_rounds = max(1, stall_rounds)
        self.page_timeout = page_timeout
        self.observed = _Observed()
        self._pw = None
        self._context = None
        self._native = None
        self._page = None
        self.signed_in = False
        self.user_agent: Optional[str] = None
        self._session: Optional[dict] = None
        self.current_url: Optional[str] = None
        self.exchanges_written = 0

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "InstagramBrowserClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile = self.browser.user_data_dir or str(
            Path("./instagram-profile-swm").resolve())
        Path(profile).mkdir(parents=True, exist_ok=True)
        if self.browser.mode == "native":
            from .instagram import _launch_native_chrome, _wait_for_cdp
            self._native = _launch_native_chrome(profile, self.headless,
                                                 self.browser.chrome_path)
            process, port = self._native
            _wait_for_cdp(port, process)
            attached = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}")
            self._context = (attached.contexts[0] if attached.contexts
                             else attached.new_context())
        else:
            launch_kwargs: dict = {
                "headless": self.headless, "args": list(_CAPTURE_ARGS),
                "no_viewport": not self.headless, "service_workers": "allow",
            }
            if self.browser.proxy:
                launch_kwargs["proxy"] = {"server": self.browser.proxy}
            self._context = self._launch_managed(profile, launch_kwargs)
        self._context.on("response", self._on_response)
        self._page = (self._context.pages[0] if self._context.pages
                      else self._context.new_page())
        try:
            self.user_agent = self._page.evaluate("navigator.userAgent")
        except Exception:
            self.user_agent = None
        self.refresh()
        return self

    def _launch_managed(self, profile: str, launch_kwargs: dict):
        """Open the profile in a Chrome Playwright manages.

        A Chrome the curator pointed at is used as it is; otherwise the
        installed Google Chrome, and failing that Playwright's own Chromium.
        """
        attempts: list[dict] = []
        if self.browser.chrome_path:
            attempts.append({"executable_path": self.browser.chrome_path})
        attempts += [{"channel": "chrome"}, {}]
        failures: list[Exception] = []
        for extra in attempts:
            try:
                return self._pw.chromium.launch_persistent_context(
                    profile, **extra, **launch_kwargs)
            except Exception as exc:
                failures.append(exc)
        for later in failures[1:]:
            log.debug("Fallback Chrome launch also failed: %s", later)
        raise failures[0]

    def close(self) -> None:
        try:
            if self._context is not None and self.browser.mode != "native":
                self._context.close()
        except Exception:
            pass
        if self._native is not None:
            from .instagram import _close_native_chrome
            process, port = self._native
            _close_native_chrome(port, process)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

    def refresh(self) -> None:
        """Re-read the session from the live browser (after a sign-in)."""
        cookies = {}
        try:
            for cookie in self._context.cookies(self.base_url + "/"):
                if cookie.get("name") in _SESSION_COOKIES:
                    cookies[cookie["name"]] = cookie["value"]
        except Exception:
            pass
        self.signed_in = bool(cookies.get("sessionid"))
        self._session = ({"cookies": cookies, "user_agent": self.user_agent}
                         if self.signed_in else None)

    @property
    def session(self) -> Optional[dict]:
        return self._session

    # -- what the browser loads ------------------------------------------------
    def _on_response(self, response) -> None:
        try:
            url = response.url
            netloc = (urlsplit(url).netloc or "").lower()
            host = urlsplit(url).hostname or ""
            ours = netloc == self.host or host.endswith(_INSTAGRAM_HOST)
            if not ours and "cdninstagram" not in host and "fbcdn" not in host:
                return
            request = response.request
            body = b""
            wants_body = (request.resource_type in ("document", "xhr", "fetch")
                          or self.warc is not None)
            if wants_body and not (300 <= response.status < 400):
                try:
                    body = response.body()
                except Exception:
                    body = b""
            self.observed.responses += 1
            if self.warc is not None:
                try:
                    self.warc.write_exchange(
                        url=url, method=request.method,
                        req_headers=request.headers,
                        post_data=request.post_data_buffer or None,
                        status=response.status,
                        status_text=response.status_text or "",
                        resp_headers=response.headers, body=body)
                    self.exchanges_written += 1
                except Exception as exc:
                    log.debug("WARC write skipped for %s: %s", url, exc)
            if not body or not ours:
                return
            lowered = url.lower()
            if request.resource_type == "document":
                documents = extract_embedded_documents(body)
            elif any(marker in lowered for marker in _API_MARKERS):
                self.observed.api_responses += 1
                documents = decode_graphql_documents(body)
            else:
                return
            if documents:
                hint = _shortcode_in(self.current_url or url)
                self.observed.take(*extract_instagram_records(documents, hint))
        except Exception as exc:
            log.debug("Response handling failed: %s", exc)

    # -- navigation ------------------------------------------------------------
    def _goto(self, url: str) -> None:
        self.current_url = url
        try:
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=int(self.page_timeout * 1000))
        except Exception as exc:
            raise InstagramError(f"Could not open {url}: {exc}") from exc
        self._settle()
        self._check_page_state()

    def _check_page_state(self) -> None:
        """Instagram's answers that are not content: a login wall, a
        challenge, a rate-limit page. Raised as the engine's conditions."""
        try:
            url = self._page.url
            title = (self._page.title() or "").lower()
        except Exception:
            return
        lowered = url.lower()
        if "/accounts/login" in lowered:
            raise LoginRequired("Instagram is showing its sign-in page.")
        if "/challenge/" in lowered or "checkpoint" in lowered:
            raise CheckpointRequired("Instagram is asking for verification.")
        if "please wait a few minutes" in title:
            raise RateLimited(90.0, "Instagram asked to wait a few minutes.")
        try:
            text = self._page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 4000)")
        except Exception:
            text = ""
        lowered_text = str(text).lower()
        if "sorry, this page isn't available" in lowered_text:
            raise TargetUnavailable("Instagram reports this page is not available.")
        if "please wait a few minutes before you try again" in lowered_text:
            raise RateLimited(90.0, "Instagram asked to wait a few minutes.")

    def show(self, url: str) -> Callable[[], None]:
        """Bring the curator to a page in a window they can see.

        A run without a window opens one here: the same profile is relaunched
        visibly, since Chrome will not open a profile twice. The window then
        stays for the rest of the run -- the curator is present, and reopening
        headless would only cost another sign-in check. The window is not
        closed on continue, so the closer returned does nothing.
        """
        if self.headless:
            self.headless = False
            self._relaunch()
        try:
            self.current_url = url
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=int(self.page_timeout * 1000))
            self._page.bring_to_front()
        except Exception as exc:
            log.warning("Could not show %s: %s", url, exc)
        return lambda: None

    def _relaunch(self) -> None:
        """Close the current browser and open the same profile again."""
        observed = self.observed
        self.close()
        self._pw = None
        self._context = None
        self._native = None
        self._page = None
        self.start()
        self.observed = observed

    def _scroll(self) -> dict:
        """Scroll to the bottom, where Instagram loads the next page.

        Instagram fetches on nearing the end of what it has shown; a partial
        scroll that never gets there loads nothing. The settle wait is real
        browser time, separate from the injectable sleep the engine uses for
        rate limits, because the fetch has to actually happen.
        """
        try:
            result = self._page.evaluate("""
              () => { const before = window.scrollY;
                      window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: 'auto'});
                      window.dispatchEvent(new Event('scroll'));
                      return {before, after: window.scrollY,
                              height: document.documentElement.scrollHeight}; }""")
        except Exception:
            result = {}
        self._settle()
        return result

    def _settle(self, seconds: Optional[float] = None) -> None:
        wait = seconds if seconds is not None else random.uniform(*self.settle)
        try:
            self._page.wait_for_timeout(int(wait * 1000))
        except Exception:
            self.sleep(wait)

    # -- the protocol ------------------------------------------------------------
    def viewer(self) -> Optional[str]:
        self.refresh()
        if not self.signed_in:
            return None
        return str((self._session or {}).get("cookies", {}).get("ds_user_id")
                   or "signed-in")

    def profile(self, username: str) -> InstagramProfile:
        self._goto(f"{self.base_url}/{username}/")
        for _ in range(6):
            found = self.observed.profiles.get(username)
            if found is None:
                found = next((p for p in self.observed.profiles.values()
                              if p.username.lower() == username.lower()), None)
            if found is not None:
                return found
            self.sleep(0.5)
        # The page opened and said nothing that reads as a profile: say so
        # rather than inventing one, but keep the target usable.
        return InstagramProfile(user_id="", username=username, raw={})

    def profile_posts(self, username: str) -> Iterator[InstagramPost]:
        url = f"{self.base_url}/{username}/"
        if self.current_url != url:
            self._goto(url)
        return _ScrollingListing(self, lambda p: p.kind != "reel" or True)

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        # Reels also appear in the posts grid for most profiles; the reels tab
        # exposes ones that were shared to reels only. Its shape differs, and
        # what is read here is whatever the page loads.
        self._goto(f"{self.base_url}/{username}/reels/")
        return _ScrollingListing(self, lambda p: True)

    def post(self, shortcode: str) -> InstagramPost:
        self._goto(f"{self.base_url}/p/{shortcode}/")
        for _ in range(8):
            found = self.observed.posts.get(shortcode)
            if found is not None:
                return found
            self.sleep(0.5)
        raise TargetUnavailable(
            f"Instagram opened /p/{shortcode}/ but served no post record.")

    def comments(self, shortcode: str,
                 include_replies: bool) -> Iterator[InstagramComment]:
        url = f"{self.base_url}/p/{shortcode}/"
        if self.current_url != url:
            self._goto(url)
        return _ScrollingComments(self, shortcode, include_replies)

    def fetch(self, url: str) -> tuple[bytes, str]:
        try:
            response = self._context.request.get(url, timeout=60_000)
        except Exception as exc:
            raise InstagramError(f"Media request failed: {exc}") from exc
        if response.status == 429:
            raise RateLimited(120.0, "429 on media")
        if response.status >= 400:
            raise TargetUnavailable(f"HTTP {response.status}")
        body = response.body()
        if self.warc is not None:
            try:
                self.warc.write_exchange(
                    url=url, method="GET", req_headers={}, post_data=None,
                    status=response.status, status_text=response.status_text or "",
                    resp_headers=response.headers, body=body)
            except Exception:
                pass
        return body, response.headers.get("content-type", "")


def _shortcode_in(url: str) -> Optional[str]:
    match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else None


class _ScrollingListing:
    """Posts as the browser loads them, scrolling for more on demand.

    next() hands over the next post not yet handed over; when none is left it
    scrolls and waits, and after ``stall_rounds`` scrolls that load nothing
    new it ends. Instagram's own order is kept, so pinned posts come first.
    """

    def __init__(self, client: InstagramBrowserClient, accept):
        self.client = client
        self.accept = accept
        # Everything observed so far counts: the profile page loaded its first
        # posts before this listing existed, and they are the listing's start.
        self.handed: set[str] = set()
        self.stalls = 0

    def __iter__(self):
        return self

    def _pending(self) -> Optional[InstagramPost]:
        for code, post in list(self.client.observed.posts.items()):
            if code not in self.handed and self.accept(post):
                self.handed.add(code)
                return post
        return None

    def __next__(self) -> InstagramPost:
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self.client.observed.posts)
            self.client._scroll()
            self.client._check_page_state()
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self.client.observed.posts) == before:
                self.stalls += 1
        raise StopIteration


class _ScrollingComments:
    """A post's comments as the thread is scrolled, replies included."""

    def __init__(self, client: InstagramBrowserClient, shortcode: str,
                 include_replies: bool):
        self.client = client
        self.shortcode = shortcode
        self.include_replies = include_replies
        self.handed: set[str] = set()
        self.stalls = 0

    def __iter__(self):
        return self

    def _pending(self) -> Optional[InstagramComment]:
        for cid, comment in list(self.client.observed.comments.items()):
            if cid in self.handed:
                continue
            if comment.post_shortcode and comment.post_shortcode != self.shortcode:
                continue
            if comment.depth and not self.include_replies:
                self.handed.add(cid)
                continue
            self.handed.add(cid)
            if not comment.post_shortcode:
                comment.post_shortcode = self.shortcode
            return comment
        return None

    def _load_more(self) -> None:
        # The thread lives in its own scroll container on a permalink; the
        # window may not move. Scroll the deepest scrollable region as well,
        # and press any "load more comments" control that is offered.
        try:
            self.client._page.evaluate("""
              () => {
                const controls = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const c of controls) {
                  const label = (c.innerText || c.getAttribute('aria-label') || '').toLowerCase();
                  if (/load more comments|view more comments|more comments|view all \\d+ comments/.test(label)) { c.click(); break; }
                }
                const boxes = Array.from(document.querySelectorAll('div, ul, section'))
                  .filter(el => el.scrollHeight > el.clientHeight + 80 &&
                                /auto|scroll/.test(getComputedStyle(el).overflowY));
                for (const el of boxes.slice(-3)) el.scrollTop = el.scrollHeight;
                window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: 'auto'});
                window.dispatchEvent(new Event('scroll'));
              }""")
        except Exception:
            pass
        self.client._settle()

    def __next__(self) -> InstagramComment:
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self.client.observed.comments)
            self._load_more()
            self.client._check_page_state()
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self.client.observed.comments) == before:
                self.stalls += 1
        raise StopIteration
