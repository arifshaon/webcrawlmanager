"""Collecting Instagram through the browser Instagram is served to.

A fresh session from a scraping library is often answered with "please wait
a few minutes" before it has asked for anything: Instagram recognises
clients that are not its own. Letting Instagram's own web application build
the requests in a real Chrome, signed in, scrolling a profile the way a
person does, keeps such failures to a minimum. Instagram can still
rate-limit or challenge the account, session or network, and the engine
treats those as the conditions they are. This module drives that browser and collects from what it
loads, with a window or without one.

It answers the client protocol the capture engine is written against --
stopping rules, media fixity, comment caps, holds, the manifest live there,
and are exercised with a stand-in. Where records come from:

* a profile page carries its first posts as JSON embedded in the HTML, and
  loads the rest through GraphQL as the page is scrolled; both are read off
  the browser's own responses, never requested separately;
* a post's page carries the post and its first comments the same way, and
  loads further comments as the thread is scrolled;
* media is requested by the page itself, so it arrives the way Instagram's
  own client requests it and passes through the same response hook as
  everything else; the driver's own HTTP client is a logged last resort;
* every exchange the browser makes can be written to a WARC as it happens,
  with credentials redacted, which is the rendered record of how Instagram
  presented what was collected.

What the browser observes is scoped to the navigation it was observed
under: each page opened is a new generation, and a listing hands over only
records from its own generation that belong to its target, so a profile
listed after another never inherits the other's posts, and a suggested post
by someone else is not the profile's. Every response a record is read from
can be handed to the package verbatim, and each record carries which
response, which decoded document and which path inside it the node came
from.

Instagram's payload shapes change; extraction is schema-tolerant.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

from .config import BrowserConfig
from .facebook import (_CAPTURE_ARGS, _walk, decode_graphql_documents,
                       extract_embedded_documents)
from .instagram import (CheckpointRequired, InstagramComment, InstagramError,
                        InstagramPost, InstagramProfile, LoginRequired,
                        MediaItem, RateLimited, TargetUnavailable)

log = logging.getLogger(__name__)

_INSTAGRAM_HOST = "instagram.com"
_API_MARKERS = ("/graphql/query", "/api/v1/", "/graphql")

# How Instagram's web client asks for a profile's own posts and reels: the
# query names it sends, the per-user endpoints of its older API, and the
# connection the answer comes back under. A post is a profile's listing only
# when read from such a request, under such a connection -- the rule the
# profile's own client follows, and the rule Instaloader follows by asking
# the per-user endpoint directly.
_LISTING_QUERY_RE = re.compile(
    r"Profile(Posts|Reels|Timeline|Clips|Grid)|UserTimeline|ProfilePostsTab"
    r"|ProfileReelsTab", re.I)
_PER_USER_API_RE = re.compile(
    r"/api/v1/(?:feed/user/(?P<user>[^/?]+)(?:/username)?/?|clips/user/?)", re.I)
_PROFILE_LISTING_KEYS = (
    "user_timeline", "profile_timeline", "edge_owner_to_timeline_media",
    "clips__user", "edge_felix_video_timeline", "profile_posts", "profile_reels",
)
_USER_VARIABLE_KEYS = ("username", "user_id", "target_user_id", "userid",
                       "id", "userID")


def describe_request(method: str, url: str, post_data: Optional[str]) -> dict:
    """What a request to Instagram was asking for, as far as it says.

    Returns ``query`` (the friendly name, if any), ``listing`` (whether it
    is a profile's posts or reels request) and ``user`` (the username or id
    it names, if any).
    """
    query: Optional[str] = None
    user: Optional[str] = None
    listing = False
    match = _PER_USER_API_RE.search(url or "")
    if match:
        listing = True
        user = match.group("user")
    form: dict = {}
    if post_data:
        try:
            form = {k: v[0] for k, v in parse_qs(post_data).items() if v}
        except Exception:
            form = {}
    if not form and post_data and post_data.lstrip().startswith("{"):
        try:
            form = json.loads(post_data)
        except Exception:
            form = {}
    name = form.get("fb_api_req_friendly_name") or form.get("query_name")
    if isinstance(name, str) and name:
        query = name
        if _LISTING_QUERY_RE.search(name):
            listing = True
    variables = form.get("variables")
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except Exception:
            variables = None
    if isinstance(variables, dict):
        named = _named_user(variables)
        if named:
            user = user or named
    if not query:
        params = parse_qs(urlsplit(url or "").query)
        for key in ("query_name", "fb_api_req_friendly_name"):
            if params.get(key):
                query = params[key][0]
                if _LISTING_QUERY_RE.search(query):
                    listing = True
        if params.get("username"):
            user = user or params["username"][0]
    return {"query": query, "listing": listing, "user": user}


def _named_user(variables: dict, depth: int = 0) -> Optional[str]:
    for key in _USER_VARIABLE_KEYS:
        value = variables.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    if depth < 3:
        for value in variables.values():
            if isinstance(value, dict):
                found = _named_user(value, depth + 1)
                if found:
                    return found
    return None


def document_names_listing(document: object, budget: int = 50_000) -> bool:
    """Whether a preloaded block carries a profile listing query's name.

    The page embeds each prefetched query under a cache key naming the
    query, so the block that holds the first posts says which query they
    answer, the same way a later request does in its form.
    """
    stack = [document]
    seen = 0
    while stack and seen < budget:
        value = stack.pop()
        seen += 1
        if isinstance(value, str):
            if len(value) < 400 and _LISTING_QUERY_RE.search(value):
                return True
        elif isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def _richer(candidate: InstagramPost, current: InstagramPost) -> bool:
    """Whether a later representation of a post says more than the kept one."""
    def score(post: InstagramPost) -> tuple:
        return (len(post.media), post.caption is not None,
                post.created_time is not None, post.owner_username is not None,
                post.comments_count is not None, len(post.raw))
    return score(candidate) > score(current)


def comments_page_state(documents) -> Optional[bool]:
    """Whether the last comments page in these documents says more follow.

    None when no comments connection with page info is present.
    """
    state: Optional[bool] = None
    for document in documents:
        for obj, path, _ancestors in _walk(document):
            if not isinstance(obj, dict) or "comment" not in ".".join(path).lower():
                continue
            info = obj.get("page_info")
            if isinstance(info, dict) and isinstance(info.get("has_next_page"), bool):
                state = info["has_next_page"]
    return state


def _connection_in(path) -> Optional[str]:
    for segment in path:
        lowered = str(segment).lower()
        for key in _PROFILE_LISTING_KEYS:
            if key in lowered:
                return str(segment)
    return None
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
    """A media record: a shortcode, an id, and a media or time signal.

    A page also carries other things that name a shortcode -- the route's
    parameters, a caption URL parameter -- so a shortcode beside a caption
    is not enough; the record must have Instagram's media id and something
    only a media record carries.
    """
    code = obj.get("code") or obj.get("shortcode")
    if not isinstance(code, str) or not code:
        return False
    if not (obj.get("pk") or obj.get("id")):
        return False
    return any(key in obj for key in (
        "taken_at", "taken_at_timestamp", "media_type", "image_versions2",
        "carousel_media", "video_versions", "display_url"))


def _is_carousel_child(obj: dict, ancestors) -> bool:
    """A carousel's components carry their own code and id; they are
    parts of the post above them, not posts."""
    if obj.get("carousel_parent_id"):
        return True
    return any(isinstance(a, dict) and _looks_like_post(a) for a in ancestors)


def _looks_like_comment(obj: dict) -> bool:
    """A comment names its author; a caption, which Instagram shapes the
    same way (pk, text, created_at), does not."""
    if "code" in obj or "shortcode" in obj:
        return False
    if not isinstance(obj.get("text"), str):
        return False
    if not (obj.get("pk") or obj.get("id")):
        return False
    author = obj.get("user") or obj.get("owner")
    if not isinstance(author, dict):
        return False
    return any(key in obj for key in ("created_at", "created_at_utc",
                                      "comment_like_count", "child_comment_count",
                                      "parent_comment_id"))


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
    pinned_by = node.get("timeline_pinned_user_ids")
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
        is_pinned=bool(pinned_by) if isinstance(pinned_by, list) else False,
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


def extract_instagram_records(documents, shortcode_hint: Optional[str] = None,
                              provenance: Optional[dict] = None
                              ) -> tuple[list[InstagramPost], list[InstagramComment],
                                         list[InstagramProfile]]:
    """Posts, comments and profiles found anywhere in decoded payloads.

    Order is preserved as Instagram delivered it -- a profile's connection
    lists pinned posts first, then newest first -- because the engine reads
    pinned posts off that order. A comment's post is the nearest post node
    above it, then the page it was loaded on; its parent is the nearest
    comment node above it.

    ``provenance`` describes where ``documents`` came from (the saved
    response, its URL, the decoder); each record gets a copy with the index
    of the document and the path of its node inside it, so the node can be
    found again in the response it was read from.
    """
    posts: "OrderedDict[str, InstagramPost]" = OrderedDict()
    comments: "OrderedDict[str, InstagramComment]" = OrderedDict()
    profiles: "OrderedDict[str, InstagramProfile]" = OrderedDict()
    base = dict(provenance or {})

    def origin(index: int, path) -> dict:
        return {**base, "document": index, "path": ".".join(path),
                "connection": _connection_in(path)}

    for index, document in enumerate(documents):
        for obj, path, ancestors in _walk(document):
            if not isinstance(obj, dict):
                continue
            if path and path[-1] == "caption":
                continue           # a caption is shaped like a comment
            if _looks_like_post(obj):
                if _is_carousel_child(obj, ancestors):
                    continue
                record = post_from_node(obj, ".".join(path))
                record.provenance = origin(index, path)
                current = posts.get(record.shortcode)
                if current is None or _richer(record, current):
                    # a page embeds several representations of one post;
                    # keep the fullest, not the first
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
                if not parent and obj.get("parent_comment_id"):
                    parent = str(obj["parent_comment_id"])
                depth = 1 if parent else 0
                record = comment_from_node(
                    obj, enclosing_post or shortcode_hint, parent, depth)
                record.provenance = origin(index, path)
                if record.comment_id not in comments:
                    comments[record.comment_id] = record
                continue
            if _looks_like_profile(obj):
                record = profile_from_node(obj)
                record.provenance = origin(index, path)
                if record.username not in profiles:
                    profiles[record.username] = record
    return list(posts.values()), list(comments.values()), list(profiles.values())


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

class _Observed:
    """What the browser has loaded so far, in the order it arrived.

    Records are kept under the navigation they arrived in -- each page the
    browser opens is a new generation -- as well as in one pool across the
    run. Listings read their own generation; the pool is for counting and
    for the curator's view of the run.
    """

    def __init__(self):
        self.posts: "OrderedDict[str, InstagramPost]" = OrderedDict()
        self.comments: "OrderedDict[str, InstagramComment]" = OrderedDict()
        self.profiles: dict[str, InstagramProfile] = {}
        self._posts_by_navigation: dict[int, "OrderedDict[str, InstagramPost]"] = {}
        self._comments_by_navigation: dict[int, "OrderedDict[str, InstagramComment]"] = {}
        # what each navigation asked Instagram for, for saying why a listing
        # came back empty
        self.queries_by_navigation: dict[int, list[str]] = {}
        # has_next_page of the last comments page seen, per navigation: a
        # later page's answer replaces an earlier one's
        self.comment_pages: dict[int, Optional[bool]] = {}
        self.responses = 0
        self.api_responses = 0

    def take(self, posts, comments, profiles, navigation: int = 0) -> int:
        added = 0
        bucket = self._posts_by_navigation.setdefault(navigation, OrderedDict())
        for post in posts:
            if post.shortcode not in bucket:
                bucket[post.shortcode] = post
                added += 1
            self.posts.setdefault(post.shortcode, post)
        threads = self._comments_by_navigation.setdefault(navigation, OrderedDict())
        for comment in comments:
            if comment.comment_id not in threads:
                threads[comment.comment_id] = comment
                added += 1
            self.comments.setdefault(comment.comment_id, comment)
        for profile in profiles:
            self.profiles[profile.username] = profile
        return added

    def posts_in(self, navigation: int) -> "OrderedDict[str, InstagramPost]":
        return self._posts_by_navigation.get(navigation) or OrderedDict()

    def comments_in(self, navigation: int) -> "OrderedDict[str, InstagramComment]":
        return self._comments_by_navigation.get(navigation) or OrderedDict()


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
        self.navigation = 0            # the generation of the page open now
        self.exchanges_written = 0
        self.page_fetches = 0          # media requested by the page itself
        self.cdn_tab_fetches = 0       # media requested from a tab on the CDN's origin
        self.fallback_fetches = 0      # media the driver had to request
        self._cdn_page = None
        self._cdn_origin: Optional[str] = None
        self._media_hosts: set[str] = set()   # hosts media was asked from
        self.anomalies: list[dict] = []   # what could not be made sense of
        self.last_fetch_via: Optional[str] = None
        self._response_sink: Optional[Callable[[dict, bytes], str]] = None
        self._awaited_url: Optional[str] = None
        self._awaited_seen = False

    def record_responses_to(self, sink: Callable[[dict, bytes], str]) -> None:
        """Hand every response a record is read from to ``sink`` verbatim.

        ``sink(meta, body)`` returns a reference (the package path) that is
        then carried in the provenance of each record read from that body.
        """
        self._response_sink = sink

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "InstagramBrowserClient":
        # A browser that fails to come up must not leave Playwright's
        # driver behind for the next attempt to trip over.
        try:
            return self._start()
        except Exception:
            self.close()
            self._pw = self._context = self._native = self._page = None
            raise

    def _start(self) -> "InstagramBrowserClient":
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
            try:
                _wait_for_cdp(port, process)
            except RuntimeError as exc:
                if "exited immediately" not in str(exc):
                    raise
                # the host refused Chrome's sandbox; once more without it
                log.warning("Native Chrome exited at once (%s); retrying "
                            "without its sandbox.", exc)
                self._native = _launch_native_chrome(
                    profile, self.headless, self.browser.chrome_path, no_sandbox=True)
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
            if self._cdn_page is not None:
                self._cdn_page.close()
        except Exception:
            pass
        self._cdn_page, self._cdn_origin = None, None
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

    def session_is_live(self) -> bool:
        """Whether Instagram still accepts the browser's session: asked of
        Instagram through the browser, not inferred from a cookie's presence.
        The cookies are re-read afterwards."""
        try:
            self._goto(self.base_url + "/")
        except LoginRequired:
            self.refresh()
            return False
        except InstagramError:
            pass
        self.refresh()
        return self.signed_in

    def cookie_jar(self) -> list[dict]:
        """The browser's Instagram cookies, for lending to a listing tool."""
        try:
            cookies = self._context.cookies([self.base_url + "/"])
        except Exception:
            return []
        host = urlsplit(self.base_url).hostname or ""
        return [c for c in cookies
                if host.endswith(str(c.get("domain") or "").lstrip("."))
                or str(c.get("domain") or "").lstrip(".") in host]

    def observed_post(self, shortcode: str) -> Optional[InstagramPost]:
        """The post as the open page presented it, if it did."""
        return self.observed.posts_in(self.navigation).get(shortcode)

    # -- what the browser loads ------------------------------------------------
    def _on_response(self, response) -> None:
        # The handler yields while it reads the body; a caller waiting for
        # this exchange must not be released until the handler is finished
        # with it, WARC write included.
        try:
            self._handle_response(response)
        finally:
            try:
                if response.url == self._awaited_url:
                    self._awaited_seen = True
            except Exception:
                pass

    def _handle_response(self, response) -> None:
        try:
            url = response.url
            netloc = (urlsplit(url).netloc or "").lower()
            host = urlsplit(url).hostname or ""
            ours = netloc == self.host or host.endswith(_INSTAGRAM_HOST)
            media_host = netloc in self._media_hosts
            if not ours and not media_host and "cdninstagram" not in host \
                    and "fbcdn" not in host:
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
                decoder = "embedded"
                documents = extract_embedded_documents(body)
            elif any(marker in lowered for marker in _API_MARKERS):
                self.observed.api_responses += 1
                decoder = "graphql"
                documents = decode_graphql_documents(body)
            else:
                return
            if not documents:
                return
            navigation = self.navigation
            asked = describe_request(request.method, url, request.post_data)
            if decoder == "embedded":
                asked["listing"] = any(document_names_listing(d) for d in documents)
                asked["query"] = asked["query"] or "page"
            origin = {"url": url, "decoder": decoder, "navigation": navigation,
                      "response": None, "query": asked["query"],
                      "listing_request": asked["listing"],
                      "listing_user": asked["user"]}
            self.observed.queries_by_navigation.setdefault(navigation, []).append(
                f"{asked['query'] or urlsplit(url).path}"
                + (" [listing]" if asked["listing"] else ""))
            if self._response_sink is not None:
                try:
                    origin["response"] = self._response_sink({
                        "url": url, "method": request.method,
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "resource_type": request.resource_type,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "navigation": navigation, "decoder": decoder,
                        "page_url": self.current_url,
                    }, body)
                except Exception as exc:
                    log.warning("Could not keep the response for %s: %s", url, exc)
            hint = _shortcode_in(self.current_url or url)
            self.observed.take(
                *extract_instagram_records(documents, hint, origin), navigation)
            more = comments_page_state(documents)
            if more is not None:
                self.observed.comment_pages[navigation] = more
        except Exception as exc:
            log.debug("Response handling failed: %s", exc)

    # -- navigation ------------------------------------------------------------
    def _goto(self, url: str) -> None:
        self.navigation += 1
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
            self.navigation += 1
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
        self._cdn_page, self._cdn_origin = None, None
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
        return _ScrollingListing(self, self.navigation, owner=username)

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        # Reels also appear in the posts grid for most profiles; the reels tab
        # exposes ones that were shared to reels only. Its shape differs, and
        # what is read here is whatever the page loads.
        self._goto(f"{self.base_url}/{username}/reels/")
        return _ScrollingListing(self, self.navigation, owner=username)

    def post(self, shortcode: str) -> InstagramPost:
        self._goto(f"{self.base_url}/p/{shortcode}/")
        for _ in range(8):
            found = self.observed.posts_in(self.navigation).get(shortcode)
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
        return _ScrollingComments(self, self.navigation, shortcode,
                                  include_replies)

    def comments_page_open(self) -> Optional[bool]:
        """Whether the last comments page seen on the open page said more
        follow; None when Instagram said nothing either way."""
        return self.observed.comment_pages.get(self.navigation)

    def fetch(self, url: str) -> tuple[bytes, str]:
        """Media, requested from inside the browser.

        A fetch from a page carries the session and Chrome's own network
        identity, and the response passes through the response hook like any
        other, so it reaches the WARC without a second request. Instagram's
        media hosts do not let a page on instagram.com read their bodies, so
        a same-origin URL is fetched by the open page and a CDN URL from a
        helper tab standing on the CDN's own origin, where the fetch is
        same-origin and readable. Only if neither can is the driver's HTTP
        client used, and that fallback is counted and logged.
        """
        page_host = (urlsplit(self._page.url or self.base_url).netloc or "").lower() \
            if self._page is not None else ""
        media_netloc = (urlsplit(url).netloc or "").lower()
        self._media_hosts.add(media_netloc)     # its exchanges belong in the WARC
        same_origin = media_netloc == page_host
        # A signed media URL needs no cookies, and Instagram's media hosts
        # allow a page's cross-origin read only when no credentials are
        # sent: they name the page's origin, or "*", and never allow
        # credentials. So the open page asks without them, with its own
        # Origin and Referer, as the closest thing to how it loads media.
        answer = self._fetch_in(self._page, url,
                                "same-origin" if same_origin else "omit")
        if answer is not None:
            self.page_fetches += 1
            self.last_fetch_via = "browser-page"
            return answer
        reason = self._last_fetch_error
        if not same_origin:
            helper = self._cdn_tab_for(url)
            if helper is not None:
                answer = self._fetch_in(helper, url, "same-origin")
                if answer is not None:
                    self.cdn_tab_fetches += 1
                    self.last_fetch_via = "browser-cdn-tab"
                    return answer
                reason = f"{reason}; from the CDN tab: {self._last_fetch_error}"
            else:
                reason = f"{reason}; {self._last_fetch_error}"
        log.warning("The browser could not fetch %s (%s); using the driver's "
                    "HTTP client instead.", url, reason)
        self.fallback_fetches += 1
        self.last_fetch_via = "playwright-api-request"
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


    _last_fetch_error: Optional[str] = None

    def _fetch_in(self, page, url: str,
                  credentials: str = "same-origin") -> Optional[tuple[bytes, str]]:
        """Fetch ``url`` from inside ``page``; None if the page could not."""
        self._awaited_url, self._awaited_seen = url, False
        try:
            answer = page.evaluate(_PAGE_FETCH_JS, {"url": url, "credentials": credentials})
        except Exception as exc:
            answer = {"error": str(exc)}
        if not (isinstance(answer, dict) and "status" in answer):
            self._awaited_url = None
            self._last_fetch_error = (answer or {}).get("error") \
                if isinstance(answer, dict) else str(answer)
            return None
        # The page has the bytes before the response event reaches the
        # hook; wait for the hook so the WARC holds this exchange before
        # the caller moves on (or closes the WARC).
        self._await_hook()
        status = int(answer.get("status") or 0)
        if status == 429:
            raise RateLimited(120.0, "429 on media")
        if status >= 400:
            raise TargetUnavailable(f"HTTP {status}")
        return (base64.b64decode(answer.get("body") or ""),
                str(answer.get("content_type") or ""))

    def _cdn_tab_for(self, url: str):
        """A tab standing on the URL's own origin, opened or moved there.

        Only the origin matters for a same-origin fetch, so the tab is sent
        to a path that cannot exist: Instagram's media hosts answer such a
        path with a small 403 page, which commits a document on the origin,
        whereas their root answers 204 No Content, which commits nothing and
        leaves the tab where it was. Failing that, the media URL itself is
        the second way to land. Media hosts vary, so the tab moves whenever
        the host does.
        """
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return None
        origin = f"{parts.scheme}://{parts.netloc}"
        try:
            if self._cdn_page is None or self._cdn_page.is_closed():
                self._cdn_page = self._context.new_page()
                self._cdn_origin = None
            if self._cdn_origin != origin:
                attempts = []
                for landing in (f"{origin}/swm-origin-probe", url):
                    problem = None
                    try:
                        self._cdn_page.goto(landing, wait_until="commit",
                                            timeout=int(self.page_timeout * 1000))
                    except Exception as exc:
                        problem = str(exc).split("\n")[0]
                    where = self._landed_origin(origin)
                    if where == origin:
                        break
                    attempts.append(f"{landing[:60]}: landed on {where or 'nothing'}"
                                    + (f" ({problem})" if problem else ""))
                else:
                    self._last_fetch_error = (f"could not stand on {origin}: "
                                              + "; ".join(attempts))
                    return None
                self._cdn_origin = origin
            return self._cdn_page
        except Exception as exc:
            self._last_fetch_error = str(exc)
            return None

    def _landed_origin(self, wanted: str, settle: float = 3.0) -> Optional[str]:
        """The origin the helper tab is on, asked of the page itself.

        The page's own location is the fact; the driver's record of the URL
        is a bookkeeping of events that can lag it. Both are consulted, for
        a moment, since a commit can arrive just after the navigation call
        returns.
        """
        deadline = time.monotonic() + settle
        seen: Optional[str] = None
        while True:
            try:
                seen = self._cdn_page.evaluate("location.origin")
            except Exception:
                seen = None
            if not seen or seen == "null":
                parts = urlsplit(self._cdn_page.url or "")
                seen = f"{parts.scheme}://{parts.netloc}" if parts.netloc else None
            if seen == wanted or time.monotonic() >= deadline:
                return seen
            try:
                self._cdn_page.wait_for_timeout(150)
            except Exception:
                time.sleep(0.15)

    def _await_hook(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        try:
            while not self._awaited_seen and time.monotonic() < deadline:
                self._page.wait_for_timeout(25)
        except Exception:
            pass
        finally:
            self._awaited_url = None


# Fetch a URL from inside the page and hand the bytes back as base64. A
# failed read (network error, a cross-origin body the page may not see)
# comes back as {"error"} rather than raising, so the caller can fall back.
_PAGE_FETCH_JS = """
async ({url, credentials}) => {
  try {
    const response = await fetch(url, {credentials, cache: 'no-store'});
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return {status: response.status,
            content_type: response.headers.get('content-type') || '',
            body: btoa(binary)};
  } catch (error) {
    return {error: String(error)};
  }
}
"""


def _shortcode_in(url: str) -> Optional[str]:
    match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else None


class _ScrollingListing:
    """Posts as the browser loads them, scrolling for more on demand.

    next() hands over the next post not yet handed over; when none is left it
    scrolls and waits, and after ``stall_rounds`` scrolls that load nothing
    new it ends. Instagram's own order is kept, so pinned posts come first.
    """

    def __init__(self, client: InstagramBrowserClient, navigation: int,
                 owner: Optional[str] = None):
        self.client = client
        # Only what this navigation loaded is the listing's: the page opened
        # its first posts before the listing existed, and they are its start;
        # what an earlier page loaded is not.
        self.navigation = navigation
        self.owner = (owner or "").lower() or None
        self.handed: set[str] = set()
        self.returned = 0
        self.stalls = 0

    def __iter__(self):
        return self

    def _profile_id(self) -> Optional[str]:
        profile = next((p for p in self.client.observed.profiles.values()
                        if p.username.lower() == self.owner), None)
        return (profile.user_id or None) if profile else None

    def _belongs(self, post: InstagramPost) -> bool:
        """Whether a post observed on this page is in this profile's listing.

        A signed-in page carries more than the profile: suggestions, a
        preload of the viewer's feed, the viewer's own posts, even the
        profile's own posts shown somewhere else. The rule is the one
        Instagram's client and Instaloader both follow: a post is the
        profile's when the profile's own posts or reels request returned it,
        under the profile's timeline connection, and it names no other owner.
        Where the request names a user, it must be this profile; where the
        post names an owner, the id decides, then the name.
        """
        if self.owner is None:
            return True
        origin = post.provenance or {}
        if not origin.get("connection") or not origin.get("listing_request"):
            return False
        profile_id = self._profile_id()
        named = origin.get("listing_user")
        if named and str(named).lower() not in {self.owner, str(profile_id or "").lower()}:
            return False
        if post.owner_id and profile_id:
            return str(post.owner_id) == str(profile_id)
        if post.owner_username:
            return post.owner_username.lower() == self.owner
        return True

    def _pool(self) -> "OrderedDict[str, InstagramPost]":
        return self.client.observed.posts_in(self.navigation)

    def _pending(self) -> Optional[InstagramPost]:
        for code, post in list(self._pool().items()):
            if code in self.handed:
                continue
            self.handed.add(code)
            if self._belongs(post):
                self.returned += 1
                return post
        return None

    def _finished(self) -> None:
        # Posts were seen but none was the profile's listing: say so, with
        # what the page asked for, rather than end quietly with nothing.
        if self.returned == 0 and self._pool():
            self.client.anomalies.append({
                "what": "no_listing_recognised", "profile": self.owner,
                "posts_observed": len(self._pool()),
                "requests": self.client.observed.queries_by_navigation.get(
                    self.navigation, [])[:40]})
            log.warning("No listing for %s recognised among %d observed posts; "
                        "the page asked for: %s", self.owner, len(self._pool()),
                        self.client.observed.queries_by_navigation.get(
                            self.navigation, []))

    def __next__(self) -> InstagramPost:
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self._pool())
            self.client._scroll()
            self.client._check_page_state()
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self._pool()) == before:
                self.stalls += 1
        self._finished()
        raise StopIteration


class _ScrollingComments:
    """A post's comments as the thread is scrolled, replies included."""

    def __init__(self, client: InstagramBrowserClient, navigation: int,
                 shortcode: str, include_replies: bool):
        self.client = client
        self.navigation = navigation
        self.shortcode = shortcode
        self.include_replies = include_replies
        self.handed: set[str] = set()
        self.stalls = 0

    def __iter__(self):
        return self

    def _pool(self) -> "OrderedDict[str, InstagramComment]":
        return self.client.observed.comments_in(self.navigation)

    def _pending(self) -> Optional[InstagramComment]:
        for cid, comment in list(self._pool().items()):
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
            before = len(self._pool())
            self._load_more()
            self.client._check_page_state()
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self._pool()) == before:
                self.stalls += 1
        raise StopIteration
