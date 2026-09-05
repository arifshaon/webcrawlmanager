"""Reading what X's web client is served.

X's client asks its GraphQL endpoint with the operation's name in the URL
path (``/i/api/graphql/<queryId>/<OperationName>``) and its variables as a
JSON query parameter, and is answered with typed timelines: a list of
instructions, each carrying entries whose ``entryId`` prefix says what the
entry is. This module turns those responses into plain records -- posts,
users, absences, cursors -- and says nothing about which of them to keep;
that is the engine's job, and the collector's rules of attribution.

X's payload shapes change; the walk here is tolerant of where a timeline
sits inside a response, and every record says which entry and instruction
it was read from.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlsplit

_GRAPHQL_PATH_RE = re.compile(r"/i/api/graphql/(?P<id>[^/]+)/(?P<name>[A-Za-z0-9_]+)/?$")

# The operations a profile visit, a post page and a search make, and what a
# record read from each is evidence of. Anything else the signed-in client
# fetches (the home feed, notifications, recommendations) is recorded in the
# WARC but never produces a record.
PROFILE_LISTINGS = ("UserTweets", "UserTweetsAndReplies", "UserMedia")
SEARCH_LISTINGS = ("SearchTimeline",)
DETAIL_OPERATIONS = ("TweetDetail", "TweetResultByRestId")
USER_LOOKUPS = ("UserByScreenName", "UserByRestId")
TARGET_OPERATIONS = PROFILE_LISTINGS + SEARCH_LISTINGS + DETAIL_OPERATIONS + USER_LOOKUPS

SURFACE_OF_OPERATION = {
    "UserTweets": "posts", "UserTweetsAndReplies": "replies",
    "UserMedia": "media", "TweetDetail": "conversation",
    "TweetResultByRestId": "conversation", "SearchTimeline": "search",
}

# Entries injected into a timeline that are not posts of it.
_INJECTED_PREFIXES = ("who-to-follow-", "module-", "messageprompt-", "cursor-",
                      "toptabsrpusermodule-", "tweetdetailrelatedtweets-",
                      "trends-", "list-to-follow-")


def describe_graphql_request(url: str) -> Optional[dict]:
    """What a request to X's GraphQL endpoint asked for, from the URL alone.

    Returns None for anything that is not such a request; otherwise
    ``operation``, ``query_id``, ``variables`` (a dict, possibly empty) and,
    for the operations SWM cares about, ``user_id``, ``raw_query``,
    ``product``, ``focal_id``, ``cursor`` and ``screen_name``.
    """
    parts = urlsplit(url or "")
    match = _GRAPHQL_PATH_RE.search(parts.path or "")
    if not match:
        return None
    params = parse_qs(parts.query)
    variables: dict = {}
    raw = params.get("variables")
    if raw:
        try:
            decoded = json.loads(raw[0])
            if isinstance(decoded, dict):
                variables = decoded
        except (ValueError, TypeError):
            variables = {}
    name = match.group("name")
    described = {
        "operation": name, "query_id": match.group("id"), "variables": variables,
        "user_id": _string(variables.get("userId")),
        "screen_name": _string(variables.get("screen_name")),
        "raw_query": _string(variables.get("rawQuery")),
        "product": _string(variables.get("product")),
        "focal_id": _string(variables.get("focalTweetId") or variables.get("tweetId")),
        "cursor": _string(variables.get("cursor")),
        "listing": name in PROFILE_LISTINGS or name in SEARCH_LISTINGS,
        "target_operation": name in TARGET_OPERATIONS,
    }
    return described


def _string(value: object) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int)):
        text = str(value).strip()
        return text or None
    return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class XMediaItem:
    """One piece of a post's media, with the rendition SWM will ask for.

    ``url`` is what SWM fetches: for an image the ``name=orig`` rendition of
    ``media_url_https`` (the page loaded a sized one), for a video or GIF
    the highest-bitrate progressive MP4 variant X advertised. ``page_url``
    is the URL as it appeared in the payload.
    """
    url: str
    kind: str                          # image | video | gif
    position: int = 0
    page_url: Optional[str] = None
    requested_variant: str = "orig"
    fallback_urls: list[str] = field(default_factory=list)
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate: Optional[int] = None
    duration_ms: Optional[int] = None
    thumbnail_url: Optional[str] = None
    alt_text: Optional[str] = None
    media_key: Optional[str] = None


@dataclass
class XUser:
    user_id: str
    handle: str
    name: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    created_time: Optional[str] = None
    followers_count: Optional[int] = None
    following_count: Optional[int] = None
    posts_count: Optional[int] = None
    is_protected: bool = False
    is_verified: bool = False
    profile_image_url: Optional[str] = None
    pinned_post_ids: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class XPost:
    post_id: str
    author_id: Optional[str] = None
    author_handle: Optional[str] = None
    author_name: Optional[str] = None
    # original | reply | repost | quote
    relationship: str = "original"
    # target | conversation_context
    capture_role: str = "target"
    text: Optional[str] = None
    created_time: Optional[str] = None
    conversation_id: Optional[str] = None
    in_reply_to_post_id: Optional[str] = None
    in_reply_to_handle: Optional[str] = None
    permalink_url: Optional[str] = None
    lang: Optional[str] = None
    reply_count: Optional[int] = None
    repost_count: Optional[int] = None
    like_count: Optional[int] = None
    quote_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    view_count: Optional[int] = None
    urls: list[dict] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    media: list[XMediaItem] = field(default_factory=list)
    is_pinned: bool = False
    # a repost: the post the account republished, with its own author
    original_post: Optional[dict] = None
    # a quote: the post quoted, with its own author
    quoted_post: Optional[dict] = None
    surface: str = "posts"
    source: str = "browser"
    # what the conversation collection for this post can support as evidence
    reply_capture: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class XAbsence:
    """A place in a timeline where X said a post is not shown."""
    kind: str                           # tombstone | unavailable
    reason: Optional[str] = None
    post_id: Optional[str] = None
    entry_id: Optional[str] = None
    provenance: dict = field(default_factory=dict)


@dataclass
class XCursor:
    cursor_type: str                    # Top | Bottom | ShowMore | ShowMoreThreads | Gap
    value: str
    entry_id: Optional[str] = None
    stop_on_empty: Optional[bool] = None


@dataclass
class TimelineRead:
    posts: list[XPost] = field(default_factory=list)
    users: list[XUser] = field(default_factory=list)
    absences: list[XAbsence] = field(default_factory=list)
    cursors: list[XCursor] = field(default_factory=list)
    promoted_skipped: int = 0
    injected_skipped: int = 0
    terminated: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

_CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"


def x_time_to_iso(value: object) -> Optional[str]:
    """X's ``created_at`` ("Wed Oct 10 20:19:24 +0000 2018") as ISO 8601 UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.strptime(value.strip(), _CREATED_AT_FORMAT)
    except ValueError:
        try:
            when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def snowflake_time(post_id: object) -> Optional[str]:
    """The creation time carried in a post id, for ids without a record."""
    try:
        number = int(str(post_id))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    millis = (number >> 22) + 1288834974657
    return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _unwrap_result(result: object) -> tuple[Optional[dict], Optional[str]]:
    """The tweet under a result union, and the typename it was found as."""
    if not isinstance(result, dict):
        return None, None
    typename = str(result.get("__typename") or "")
    if typename == "TweetWithVisibilityResults" and isinstance(result.get("tweet"), dict):
        inner = result["tweet"]
        return inner, "TweetWithVisibilityResults"
    if typename in ("TweetTombstone", "TweetUnavailable"):
        return None, typename
    if typename == "Tweet" or ("legacy" in result and ("rest_id" in result or "id_str" in result.get("legacy", {}))):
        return result, typename or "Tweet"
    return None, typename or None


def _user_of(tweet: dict) -> Optional[dict]:
    core = tweet.get("core")
    if isinstance(core, dict):
        results = core.get("user_results")
        if isinstance(results, dict) and isinstance(results.get("result"), dict):
            return results["result"]
    return None


def user_from_result(result: object, provenance: Optional[dict] = None) -> Optional[XUser]:
    """A user record from a ``User`` result, wherever it sits."""
    if not isinstance(result, dict):
        return None
    if str(result.get("__typename") or "User") not in ("User",):
        return None
    user_id = _string(result.get("rest_id")) or _string((result.get("legacy") or {}).get("id_str"))
    legacy = result.get("legacy") if isinstance(result.get("legacy"), dict) else {}
    core = result.get("core") if isinstance(result.get("core"), dict) else {}
    handle = _string(legacy.get("screen_name")) or _string(core.get("screen_name"))
    if not user_id or not handle:
        return None
    avatar = result.get("avatar") if isinstance(result.get("avatar"), dict) else {}
    location = result.get("location") if isinstance(result.get("location"), dict) else {}
    privacy = result.get("privacy") if isinstance(result.get("privacy"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    pinned = legacy.get("pinned_tweet_ids_str")
    return XUser(
        user_id=user_id, handle=handle,
        name=_string(legacy.get("name")) or _string(core.get("name")),
        description=_string(legacy.get("description")),
        location=_string(legacy.get("location")) or _string(location.get("location")),
        url=_string(legacy.get("url")),
        created_time=x_time_to_iso(legacy.get("created_at") or core.get("created_at")),
        followers_count=_int(legacy.get("followers_count")),
        following_count=_int(legacy.get("friends_count")),
        posts_count=_int(legacy.get("statuses_count")),
        is_protected=bool(legacy.get("protected") or privacy.get("protected")),
        is_verified=bool(result.get("is_blue_verified") or legacy.get("verified")
                         or verification.get("verified")),
        profile_image_url=_string(legacy.get("profile_image_url_https"))
        or _string(avatar.get("image_url")),
        pinned_post_ids=[str(p) for p in pinned if _string(p)] if isinstance(pinned, list) else [],
        raw=result, provenance=dict(provenance or {}))


_IMAGE_FORMAT_RE = re.compile(r"\.(jpg|jpeg|png|webp|gif)$", re.I)


def original_image_url(media_url: str) -> tuple[str, list[str]]:
    """The ``name=orig`` request for an image, and what to try if refused.

    The page loads sized renditions; the original is a separate request
    with ``format`` naming the upload's own format, which the bare URL's
    extension carries. A bare URL answers with the medium rendition.
    """
    parts = urlsplit(media_url)
    match = _IMAGE_FORMAT_RE.search(parts.path or "")
    if not match:
        return media_url, []
    fmt = match.group(1).lower()
    fmt = "jpg" if fmt == "jpeg" else fmt
    stem = f"{parts.scheme}://{parts.netloc}{parts.path[:match.start()]}"
    return (f"{stem}?format={fmt}&name=orig",
            [f"{stem}?format={fmt}&name=4096x4096", f"{stem}?format={fmt}&name=large",
             media_url])


def media_items_of(tweet: dict) -> list[XMediaItem]:
    """The post's media in display order, from ``extended_entities``."""
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else tweet
    extended = legacy.get("extended_entities")
    media = extended.get("media") if isinstance(extended, dict) else None
    if not isinstance(media, list) or not media:
        entities = legacy.get("entities")
        media = entities.get("media") if isinstance(entities, dict) else None
    if not isinstance(media, list):
        return []
    items: list[XMediaItem] = []
    for index, entry in enumerate(media):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "photo")
        page_url = _string(entry.get("media_url_https")) or _string(entry.get("media_url"))
        original = entry.get("original_info") if isinstance(entry.get("original_info"), dict) else {}
        width, height = _int(original.get("width")), _int(original.get("height"))
        alt = _string(entry.get("ext_alt_text"))
        key = _string(entry.get("media_key"))
        if kind in ("video", "animated_gif"):
            info = entry.get("video_info") if isinstance(entry.get("video_info"), dict) else {}
            best = None
            for variant in (info.get("variants") or []):
                if not isinstance(variant, dict):
                    continue
                if "mp4" not in str(variant.get("content_type") or "").lower():
                    continue        # HLS carries video and audio apart; an MP4 is one file
                bitrate = _int(variant.get("bitrate")) or 0
                url = _string(variant.get("url"))
                if url and (best is None or bitrate > (best.bitrate or 0)):
                    best = XMediaItem(
                        url=url, kind="gif" if kind == "animated_gif" else "video",
                        position=index, page_url=page_url,
                        requested_variant=f"mp4:{bitrate}", bitrate=bitrate,
                        width=width, height=height,
                        duration_ms=_int(info.get("duration_millis")),
                        thumbnail_url=page_url, alt_text=alt, media_key=key)
            if best is not None:
                items.append(best)
            continue
        if not page_url:
            continue
        url, fallbacks = original_image_url(page_url)
        items.append(XMediaItem(url=url, kind="image", position=index, page_url=page_url,
                                requested_variant="orig", fallback_urls=fallbacks,
                                width=width, height=height, alt_text=alt, media_key=key))
    return items


def _entities_of(legacy: dict, note: Optional[dict]) -> tuple[list[dict], list[str], list[str]]:
    urls: list[dict] = []
    tags: list[str] = []
    mentions: list[str] = []
    seen: set[str] = set()
    for source in (note or {}, legacy):
        entities = source.get("entity_set") if source is note else source.get("entities")
        if not isinstance(entities, dict):
            continue
        for item in entities.get("urls") or []:
            if isinstance(item, dict) and _string(item.get("expanded_url")):
                expanded = str(item["expanded_url"])
                if expanded in seen:
                    continue
                seen.add(expanded)
                urls.append({"url": _string(item.get("url")), "expanded_url": expanded,
                             "display_url": _string(item.get("display_url"))})
        for item in entities.get("hashtags") or []:
            if isinstance(item, dict) and _string(item.get("text")):
                tag = str(item["text"])
                if tag not in tags:
                    tags.append(tag)
        for item in entities.get("user_mentions") or []:
            if isinstance(item, dict) and _string(item.get("screen_name")):
                handle = str(item["screen_name"])
                if handle not in mentions:
                    mentions.append(handle)
    return urls, tags, mentions


def _full_text(tweet: dict, legacy: dict) -> tuple[Optional[str], Optional[dict]]:
    note = tweet.get("note_tweet")
    results = note.get("note_tweet_results") if isinstance(note, dict) else None
    result = results.get("result") if isinstance(results, dict) else None
    if isinstance(result, dict) and _string(result.get("text")):
        return str(result["text"]), result
    return _string(legacy.get("full_text")) or _string(legacy.get("text")), None


def _summary(post: XPost) -> dict:
    """A post embedded in another's record: enough to show it honestly."""
    return {
        "post_id": post.post_id, "author_id": post.author_id,
        "author_handle": post.author_handle, "author_name": post.author_name,
        "text": post.text, "created_time": post.created_time,
        "permalink_url": post.permalink_url,
        "media": [{"url": m.url, "kind": m.kind, "position": m.position,
                   "page_url": m.page_url, "requested_variant": m.requested_variant}
                  for m in post.media],
    }


def post_from_result(result: object, provenance: Optional[dict] = None,
                     surface: str = "posts") -> tuple[Optional[XPost], Optional[XAbsence]]:
    """A post record from a tweet result union, or an absence."""
    tweet, typename = _unwrap_result(result)
    provenance = dict(provenance or {})
    if tweet is None:
        if typename in ("TweetTombstone", "TweetUnavailable") and isinstance(result, dict):
            tomb = result.get("tombstone") if isinstance(result.get("tombstone"), dict) else {}
            text = tomb.get("text") if isinstance(tomb.get("text"), dict) else {}
            reason = _string(text.get("text")) or _string(result.get("reason"))
            return None, XAbsence(kind="tombstone" if typename == "TweetTombstone" else "unavailable",
                                  reason=reason, provenance=provenance)
        return None, None
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    post_id = _string(tweet.get("rest_id")) or _string(legacy.get("id_str"))
    if not post_id:
        return None, None
    author = user_from_result(_user_of(tweet), provenance)
    text, note = _full_text(tweet, legacy)
    urls, tags, mentions = _entities_of(legacy, note)
    views = tweet.get("views") if isinstance(tweet.get("views"), dict) else {}
    handle = author.handle if author else _string(legacy.get("screen_name"))
    post = XPost(
        post_id=post_id,
        author_id=author.user_id if author else _string(legacy.get("user_id_str")),
        author_handle=handle,
        author_name=author.name if author else None,
        text=text,
        created_time=x_time_to_iso(legacy.get("created_at")) or snowflake_time(post_id),
        conversation_id=_string(legacy.get("conversation_id_str")),
        in_reply_to_post_id=_string(legacy.get("in_reply_to_status_id_str")),
        in_reply_to_handle=_string(legacy.get("in_reply_to_screen_name")),
        permalink_url=(f"https://x.com/{handle}/status/{post_id}" if handle
                       else f"https://x.com/i/status/{post_id}"),
        lang=_string(legacy.get("lang")),
        reply_count=_int(legacy.get("reply_count")),
        repost_count=_int(legacy.get("retweet_count")),
        like_count=_int(legacy.get("favorite_count")),
        quote_count=_int(legacy.get("quote_count")),
        bookmark_count=_int(legacy.get("bookmark_count")),
        view_count=_int(views.get("count")),
        urls=urls, hashtags=tags, mentions=mentions,
        media=media_items_of(tweet),
        surface=surface,
        raw=tweet,
        provenance={**provenance, "result_typename": typename},
    )
    # a repost: the original sits inside, with its own author
    reposted = legacy.get("retweeted_status_result")
    inner = reposted.get("result") if isinstance(reposted, dict) else None
    if inner is not None:
        original, _absence = post_from_result(inner, {**provenance, "nested": "retweeted_status_result"}, surface)
        post.relationship = "repost"
        if original is not None:
            post.original_post = _summary(original)
            if not post.text or post.text.startswith("RT @"):
                post.text = original.text
            post.media = list(original.media)
        elif _string(legacy.get("retweeted_status_id_str")):
            post.original_post = {"post_id": str(legacy["retweeted_status_id_str"])}
    elif post.in_reply_to_post_id:
        post.relationship = "reply"
    quoted = tweet.get("quoted_status_result")
    inner = quoted.get("result") if isinstance(quoted, dict) else None
    if inner is not None:
        quoted_post, absence = post_from_result(inner, {**provenance, "nested": "quoted_status_result"}, surface)
        if quoted_post is not None:
            post.quoted_post = _summary(quoted_post)
        elif absence is not None:
            post.quoted_post = {"post_id": _string(legacy.get("quoted_status_id_str")),
                                "unavailable": absence.reason or absence.kind}
        if post.relationship == "original":
            post.relationship = "quote"
    elif legacy.get("is_quote_status") and _string(legacy.get("quoted_status_id_str")):
        post.quoted_post = {"post_id": str(legacy["quoted_status_id_str"])}
        if post.relationship == "original":
            post.relationship = "quote"
    return post, None


# ---------------------------------------------------------------------------
# Walking a timeline
# ---------------------------------------------------------------------------

def find_instruction_lists(document: object, budget: int = 200_000) -> Iterator[tuple[tuple, list]]:
    """Every ``instructions`` list in a response, wherever the timeline sits.

    A profile's timeline is under ``data.user.result.timeline.timeline``, a
    conversation under ``data.threaded_conversation_with_injections_v2``, a
    search under ``data.search_by_raw_query.search_timeline.timeline``; the
    list is what matters, not the path to it.
    """
    stack: list[tuple[tuple, object]] = [((), document)]
    seen = 0
    while stack and seen < budget:
        path, value = stack.pop()
        seen += 1
        if isinstance(value, dict):
            instructions = value.get("instructions")
            if isinstance(instructions, list) and all(
                    isinstance(i, dict) and "type" in i for i in instructions):
                yield path + ("instructions",), instructions
                continue
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    stack.append((path + (str(key),), child))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    stack.append((path + (str(index),), child))


def _is_injected(entry_id: str) -> bool:
    lowered = entry_id.lower()
    return any(lowered.startswith(prefix) for prefix in _INJECTED_PREFIXES)


def _item_contents(entry: dict) -> list[tuple[dict, Optional[str]]]:
    """The item contents of an entry: one for an item, many for a module."""
    content = entry.get("content") if isinstance(entry.get("content"), dict) else entry.get("item")
    if not isinstance(content, dict):
        return []
    found: list[tuple[dict, Optional[str]]] = []
    item = content.get("itemContent")
    if isinstance(item, dict):
        found.append((item, None))
    items = content.get("items")
    if isinstance(items, list):
        for member in items:
            if not isinstance(member, dict):
                continue
            inner = member.get("item")
            inner_content = inner.get("itemContent") if isinstance(inner, dict) else None
            if isinstance(inner_content, dict):
                found.append((inner_content, _string(member.get("entryId"))))
    return found


def _cursor_of(entry: dict) -> Optional[XCursor]:
    content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
    kind = _string(content.get("cursorType"))
    value = _string(content.get("value"))
    if content.get("entryType") == "TimelineTimelineCursor" or (kind and value):
        if value and kind:
            return XCursor(cursor_type=kind, value=value,
                           entry_id=_string(entry.get("entryId")),
                           stop_on_empty=content.get("stopOnEmptyResponse")
                           if isinstance(content.get("stopOnEmptyResponse"), bool) else None)
    item = content.get("itemContent") if isinstance(content.get("itemContent"), dict) else {}
    if item.get("itemType") == "TimelineTimelineCursor" and _string(item.get("value")):
        return XCursor(cursor_type=str(item.get("cursorType") or "Bottom"),
                       value=str(item["value"]), entry_id=_string(entry.get("entryId")))
    return None


def read_timeline(documents: list, origin: Optional[dict] = None,
                  surface: str = "posts") -> TimelineRead:
    """Walk every timeline in ``documents`` and read its entries.

    ``origin`` is carried into each record's provenance beside the entry
    id, the instruction type, the module the entry sat in, and the path of
    the instruction list inside its document.
    """
    read = TimelineRead()
    origin = dict(origin or {})
    seen_posts: dict[str, XPost] = {}
    seen_users: dict[str, XUser] = {}
    for doc_index, document in enumerate(documents):
        # a lone user lookup carries no timeline; its user is still a record
        for user in _standalone_users(document):
            user.provenance = {**origin, "document": doc_index, "path": "data.user.result"}
            seen_users.setdefault(user.user_id, user)
        for path, instructions in find_instruction_lists(document):
            for instruction in instructions:
                kind = str(instruction.get("type") or "")
                entries: list[dict] = []
                if kind in ("TimelineAddEntries",):
                    entries = [e for e in (instruction.get("entries") or []) if isinstance(e, dict)]
                elif kind in ("TimelinePinEntry", "TimelineReplaceEntry"):
                    if isinstance(instruction.get("entry"), dict):
                        entries = [instruction["entry"]]
                elif kind == "TimelineAddToModule":
                    entries = [{"entryId": _string(instruction.get("moduleEntryId")) or "module",
                                "content": {"items": [m for m in (instruction.get("moduleItems") or [])
                                                      if isinstance(m, dict)]}}]
                elif kind == "TimelineTerminateTimeline":
                    read.terminated.append(str(instruction.get("direction") or ""))
                    continue
                else:
                    continue
                for entry in entries:
                    entry_id = _string(entry.get("entryId")) or ""
                    cursor = _cursor_of(entry)
                    if cursor is not None:
                        read.cursors.append(cursor)
                        continue
                    if _is_injected(entry_id):
                        read.injected_skipped += 1
                        continue
                    for item, member_id in _item_contents(entry):
                        item_type = str(item.get("itemType") or "")
                        if item_type == "TimelineTimelineCursor" and _string(item.get("value")):
                            read.cursors.append(XCursor(
                                cursor_type=str(item.get("cursorType") or "ShowMore"),
                                value=str(item["value"]), entry_id=member_id or entry_id))
                            continue
                        if item_type and item_type != "TimelineTweet":
                            read.injected_skipped += 1
                            continue
                        provenance = {
                            **origin, "document": doc_index, "path": ".".join(path),
                            "instruction": kind, "entry_id": member_id or entry_id,
                            "module": entry_id if member_id else None,
                        }
                        if item.get("promotedMetadata") is not None:
                            read.promoted_skipped += 1
                            continue
                        results = item.get("tweet_results")
                        result = results.get("result") if isinstance(results, dict) else None
                        post, absence = post_from_result(result, provenance, surface)
                        if absence is not None:
                            absence.entry_id = member_id or entry_id
                            absence.post_id = _post_id_in(member_id or entry_id)
                            read.absences.append(absence)
                            continue
                        if post is None:
                            continue
                        if kind == "TimelinePinEntry":
                            post.is_pinned = True
                        author = user_from_result(_user_of(post.raw), provenance)
                        if author is not None:
                            seen_users.setdefault(author.user_id, author)
                        current = seen_posts.get(post.post_id)
                        if current is None or _richer(post, current):
                            if current is not None and current.is_pinned:
                                post.is_pinned = True
                            seen_posts[post.post_id] = post
                        elif post.is_pinned:
                            current.is_pinned = True
    read.posts = list(seen_posts.values())
    read.users = list(seen_users.values())
    return read


def _post_id_in(entry_id: str) -> Optional[str]:
    match = re.search(r"(\d{6,})", entry_id or "")
    return match.group(1) if match else None


def _standalone_users(document: object) -> list[XUser]:
    """The user of a ``UserByScreenName`` answer: ``data.user.result``."""
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, dict):
        return []
    found: list[XUser] = []
    for key in ("user", "user_result", "user_result_by_screen_name"):
        holder = data.get(key)
        if isinstance(holder, dict) and isinstance(holder.get("result"), dict):
            user = user_from_result(holder["result"])
            if user is not None:
                found.append(user)
    return found


def _richer(candidate: XPost, current: XPost) -> bool:
    def score(post: XPost) -> tuple:
        return (len(post.media), post.text is not None, post.author_id is not None,
                post.reply_count is not None, len(post.raw))
    return score(candidate) > score(current)


def rate_limit_reset(headers: dict) -> Optional[float]:
    """The epoch second a limited operation reopens, from X's headers."""
    for name, value in (headers or {}).items():
        if str(name).lower() == "x-rate-limit-reset":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None
