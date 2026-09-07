"""Reading what YouTube's web client is served, for the Posts tab.

YouTube's client embeds the first page of every view in the HTML as
``ytInitialData`` and fetches the rest through ``youtubei/v1/browse`` and
``youtubei/v1/next``, POSTs whose JSON body carries a ``browseId`` or a
``continuation`` token. The payloads are trees of renderers; a post is a
``backstagePostRenderer``, a comment either a ``commentRenderer`` (the
older shape) or a ``commentViewModel`` whose fields live in a
``commentEntityPayload`` elsewhere in the same response (the shape served
since 2024). This module turns those into plain records and says nothing
about which to keep. Every record names the response, the renderer and
the path it was read from.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from .facebook import _walk
from .youtube import YouTubeChannel, YouTubeComment, YouTubePost

_INITIAL_RE = re.compile(rb"(?:var\s+ytInitialData|window\[\"ytInitialData\"\])\s*=\s*(\{.*?\})\s*;\s*</script>",
                         re.S)
_INITIAL_ANY_RE = re.compile(rb"ytInitialData\s*=\s*(\{.*?\});", re.S)


def read_initial_data(html: bytes) -> list[dict]:
    """The ``ytInitialData`` objects embedded in a YouTube page."""
    documents: list[dict] = []
    for pattern in (_INITIAL_RE, _INITIAL_ANY_RE):
        for match in pattern.finditer(html or b""):
            try:
                loaded = json.loads(match.group(1).decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(loaded, dict):
                documents.append(loaded)
        if documents:
            break
    return documents


def describe_youtubei_request(url: str, post_data: Optional[str]) -> Optional[dict]:
    """What a ``youtubei`` request asked for: the endpoint and its body's ids."""
    if "/youtubei/v1/" not in (url or ""):
        return None
    endpoint = url.split("/youtubei/v1/", 1)[1].split("?", 1)[0].strip("/")
    body: dict = {}
    if post_data:
        try:
            loaded = json.loads(post_data)
            if isinstance(loaded, dict):
                body = loaded
        except ValueError:
            body = {}
    return {"endpoint": endpoint,
            "browse_id": _string(body.get("browseId")),
            "params": _string(body.get("params")),
            "continuation": _string(body.get("continuation")),
            "video_id": _string(body.get("videoId"))}


def _string(value: object) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def text_of(node: object) -> Optional[str]:
    """The text of a ``{"runs": [...]}`` or ``{"simpleText": ...}`` node."""
    if isinstance(node, str):
        return node.strip() or None
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"].strip() or None
    runs = node.get("runs")
    if isinstance(runs, list):
        parts = []
        for run in runs:
            if isinstance(run, dict) and isinstance(run.get("text"), str):
                parts.append(run["text"])
        joined = "".join(parts)
        return joined.strip() or None
    if isinstance(node.get("content"), str):
        return node["content"].strip() or None
    return None


_COUNT_RE = re.compile(r"([\d][\d,.]*)\s*([KMB])?", re.I)


def parse_count(value: object) -> Optional[int]:
    """"1.2K likes", "3,456", "12M subscribers" as a number."""
    text = text_of(value) if not isinstance(value, str) else value
    if not text:
        return None
    match = _COUNT_RE.search(text.replace(" ", " "))
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get((match.group(2) or "").upper(), 1)
    return int(number * scale)


_RELATIVE_RE = re.compile(
    r"(?:edited\s+)?(?:streamed\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)


def relative_to_iso(text: object, now: Optional[datetime] = None) -> Optional[str]:
    """YouTube's "3 days ago" as an estimated ISO time.

    A week is seven days, a month thirty, a year three hundred and
    sixty-five; the record keeps the relative text beside the estimate.
    """
    if not isinstance(text, str):
        return None
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    number = int(match.group(1))
    unit = match.group(2).lower()
    seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
               "month": 2592000, "year": 31536000}[unit]
    when = (now or datetime.now(timezone.utc)) - timedelta(seconds=number * seconds)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _largest_thumbnail(node: object) -> Optional[dict]:
    thumbs = None
    if isinstance(node, dict):
        # renderers carry "thumbnails"; the newer view models carry "sources"
        thumbs = node.get("thumbnails") if node.get("thumbnails") is not None else node.get("sources")
        if thumbs is None and isinstance(node.get("image"), dict):
            image = node["image"]
            thumbs = image.get("thumbnails") if image.get("thumbnails") is not None else image.get("sources")
    if not isinstance(thumbs, list):
        return None
    best = None
    for thumb in thumbs:
        if isinstance(thumb, dict) and isinstance(thumb.get("url"), str):
            if best is None or (thumb.get("width") or 0) > (best.get("width") or 0):
                best = thumb
    if best is None:
        return None
    url = best["url"]
    if url.startswith("//"):
        url = "https:" + url
    return {"url": url, "width": best.get("width"), "height": best.get("height")}


def _browse_id(node: object) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    endpoint = node.get("browseEndpoint")
    if isinstance(endpoint, dict):
        return _string(endpoint.get("browseId"))
    return None


def _handle_of(node: object) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    endpoint = node.get("browseEndpoint")
    if isinstance(endpoint, dict):
        base = _string(endpoint.get("canonicalBaseUrl"))
        if base and base.startswith("/@"):
            return base[2:]
    return None


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def post_from_renderer(renderer: dict, provenance: Optional[dict] = None,
                       now: Optional[datetime] = None) -> Optional[YouTubePost]:
    post_id = _string(renderer.get("postId"))
    if not post_id:
        return None
    author = renderer.get("authorEndpoint")
    published_text = text_of(renderer.get("publishedTimeText"))
    post = YouTubePost(
        post_id=post_id,
        channel_id=_browse_id(author),
        channel_handle=_handle_of(author),
        author_name=text_of(renderer.get("authorText")),
        text=text_of(renderer.get("contentText")),
        published_text=published_text,
        published_time=relative_to_iso(published_text, now),
        like_count=parse_count(renderer.get("voteCount"))
        if renderer.get("voteCount") is not None else None,
        url=f"https://www.youtube.com/post/{post_id}",
        source="browser", raw=renderer, provenance=dict(provenance or {}))
    buttons = renderer.get("actionButtons")
    if isinstance(buttons, dict):
        reply = ((buttons.get("commentActionButtonsRenderer") or {}).get("replyButton") or {})
        label = (reply.get("buttonRenderer") or {}).get("text")
        count = parse_count(label)
        if count is not None:
            post.comment_count = count
        like_button = ((buttons.get("commentActionButtonsRenderer") or {}).get("likeButton") or {})
        if post.like_count is None:
            label = (((like_button.get("toggleButtonRenderer") or {}).get("defaultText") or {})
                     .get("accessibility") or {}).get("accessibilityData", {}).get("label")
            post.like_count = parse_count(label) if label else None
    attachment = renderer.get("backstageAttachment")
    if isinstance(attachment, dict):
        _read_attachment(post, attachment)
    if post.kind == "text" and not post.text:
        post.kind = "text"
    return post


def _read_attachment(post: YouTubePost, attachment: dict) -> None:
    if "backstageImageRenderer" in attachment:
        image = _largest_thumbnail(attachment["backstageImageRenderer"])
        if image:
            post.images.append(image)
            post.kind = "image"
    if "postMultiImageRenderer" in attachment:
        for item in (attachment["postMultiImageRenderer"].get("images") or []):
            if isinstance(item, dict) and "backstageImageRenderer" in item:
                image = _largest_thumbnail(item["backstageImageRenderer"])
                if image:
                    post.images.append(image)
        post.kind = "images"
    if "pollRenderer" in attachment:
        poll = attachment["pollRenderer"]
        choices = []
        for choice in poll.get("choices") or []:
            if isinstance(choice, dict):
                entry = {"text": text_of(choice.get("text"))}
                image = _largest_thumbnail(choice.get("image"))
                if image:
                    entry["image"] = image
                    post.images.append(image)
                choices.append(entry)
        post.poll = {"options": choices, "total_votes_text": text_of(poll.get("totalVotes")),
                     "results_available": False,
                     "results_reason": "results require a vote, which the capture never casts"}
        post.kind = "poll"
    if "quizRenderer" in attachment:
        quiz = attachment["quizRenderer"]
        post.poll = {"options": [{"text": text_of(c.get("text"))} for c in quiz.get("choices") or []
                                 if isinstance(c, dict)],
                     "results_available": False,
                     "results_reason": "answers require an interaction the capture never makes"}
        post.kind = "quiz"
    if "videoRenderer" in attachment:
        post.attached_video_id = _string(attachment["videoRenderer"].get("videoId"))
        post.kind = "video"
    if "postRenderer" in attachment:
        # a shared post: the original is embedded
        original = post_from_renderer(attachment["postRenderer"], post.provenance)
        if original is not None:
            post.shared_post_id = original.post_id
            post.kind = "shared"


def posts_from(documents: list, origin: Optional[dict] = None,
               now: Optional[datetime] = None) -> list[YouTubePost]:
    """Every post renderer in the documents, in the order YouTube listed them."""
    found: dict[str, YouTubePost] = {}
    origin = dict(origin or {})
    for index, document in enumerate(documents):
        for obj, path, _ancestors in _walk(document):
            if not isinstance(obj, dict) or not isinstance(obj.get("backstagePostRenderer"), dict):
                continue
            renderer = obj["backstagePostRenderer"]
            # a post shown inside another (a share) is the share's business
            if path and path[-1] == "postRenderer":
                continue
            post = post_from_renderer(renderer, {**origin, "document": index,
                                                 "renderer": "backstagePostRenderer",
                                                 "path": ".".join(path)}, now)
            if post is not None and post.post_id not in found:
                found[post.post_id] = post
    return list(found.values())


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def _comment_ids(comment_id: str) -> tuple[Optional[str], str]:
    """YouTube writes a reply's id as ``<parent>.<reply>``."""
    if "." in comment_id:
        parent, _rest = comment_id.split(".", 1)
        return parent, parent
    return None, comment_id


def comment_from_renderer(renderer: dict, target_id: str, target_type: str,
                          provenance: Optional[dict] = None,
                          now: Optional[datetime] = None) -> Optional[YouTubeComment]:
    """The older ``commentRenderer`` shape."""
    comment_id = _string(renderer.get("commentId"))
    if not comment_id:
        return None
    parent, root = _comment_ids(comment_id)
    published = text_of(renderer.get("publishedTimeText"))
    author = renderer.get("authorEndpoint")
    return YouTubeComment(
        comment_id=comment_id, target_type=target_type, target_id=target_id,
        parent_id=parent, thread_root_id=root, reply_depth=1 if parent else 0,
        author_name=text_of(renderer.get("authorText")),
        author_channel_id=_browse_id(author),
        author_is_uploader=bool(renderer.get("authorIsChannelOwner")),
        text=text_of(renderer.get("contentText")),
        published_text=published, published_time=relative_to_iso(published, now),
        like_count=parse_count(renderer.get("voteCount")) if renderer.get("voteCount") else None,
        is_pinned=bool(renderer.get("pinnedCommentBadge")),
        source="browser", raw=renderer, provenance=dict(provenance or {}))


def comment_from_entity(payload: dict, target_id: str, target_type: str,
                        provenance: Optional[dict] = None,
                        now: Optional[datetime] = None) -> Optional[YouTubeComment]:
    """The ``commentEntityPayload`` shape served since 2024."""
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    comment_id = _string(properties.get("commentId"))
    if not comment_id:
        return None
    parent, root = _comment_ids(comment_id)
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    toolbar = payload.get("toolbar") if isinstance(payload.get("toolbar"), dict) else {}
    content = properties.get("content")
    published = _string(properties.get("publishedTime"))
    likes = toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked")
    level = properties.get("replyLevel")
    depth = int(level) if isinstance(level, int) else (1 if parent else 0)
    return YouTubeComment(
        comment_id=comment_id, target_type=target_type, target_id=target_id,
        parent_id=parent, thread_root_id=root, reply_depth=depth,
        author_name=_string(author.get("displayName")),
        author_channel_id=_string(author.get("channelId")),
        author_is_uploader=bool(author.get("isCreator")),
        text=text_of(content) if isinstance(content, dict) else _string(content),
        published_text=published, published_time=relative_to_iso(published, now),
        like_count=parse_count(likes) if likes else None,
        is_pinned=bool(properties.get("pinnedText")) or bool(payload.get("pinnedText")),
        source="browser", raw=payload, provenance=dict(provenance or {}))


def comments_from(documents: list, target_id: str, target_type: str = "post",
                  origin: Optional[dict] = None,
                  now: Optional[datetime] = None) -> list[YouTubeComment]:
    """Every comment in the documents, either shape, in listing order."""
    found: dict[str, YouTubeComment] = {}
    origin = dict(origin or {})
    for index, document in enumerate(documents):
        # the entity shape: the view models set the order, the payloads
        # carry the fields; a payload without a view model is still a
        # comment (a reply page carries payloads alone)
        for obj, path, _ancestors in _walk(document):
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("commentEntityPayload"), dict):
                comment = comment_from_entity(obj["commentEntityPayload"], target_id, target_type,
                                              {**origin, "document": index,
                                               "renderer": "commentEntityPayload",
                                               "path": ".".join(path)}, now)
                if comment is not None and comment.comment_id not in found:
                    found[comment.comment_id] = comment
            elif isinstance(obj.get("commentRenderer"), dict):
                comment = comment_from_renderer(obj["commentRenderer"], target_id, target_type,
                                                {**origin, "document": index,
                                                 "renderer": "commentRenderer",
                                                 "path": ".".join(path)}, now)
                if comment is not None and comment.comment_id not in found:
                    found[comment.comment_id] = comment
    return list(found.values())


def continuation_tokens(documents: list) -> list[str]:
    """Every continuation token offered in the documents."""
    tokens: list[str] = []
    for document in documents:
        for obj, _path, _ancestors in _walk(document):
            if isinstance(obj, dict) and isinstance(obj.get("continuationCommand"), dict):
                token = _string(obj["continuationCommand"].get("token"))
                if token and token not in tokens:
                    tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

def channel_from(documents: list, origin: Optional[dict] = None) -> Optional[YouTubeChannel]:
    """The channel a page describes, from its metadata and header renderers."""
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        metadata = ((document.get("metadata") or {}).get("channelMetadataRenderer")
                    if isinstance(document.get("metadata"), dict) else None)
        if not isinstance(metadata, dict):
            continue
        channel_id = _string(metadata.get("externalId"))
        if not channel_id:
            continue
        vanity = _string(metadata.get("vanityChannelUrl")) or ""
        handle = vanity.rsplit("/@", 1)[1] if "/@" in vanity else None
        avatar = _largest_thumbnail(metadata.get("avatar"))
        channel = YouTubeChannel(
            channel_id=channel_id, handle=handle, name=_string(metadata.get("title")),
            url=_string(metadata.get("channelUrl")) or (f"https://www.youtube.com/@{handle}" if handle else None),
            description=_string(metadata.get("description")),
            avatar_url=avatar["url"] if avatar else None,
            source="browser", raw=metadata,
            provenance={**(origin or {}), "document": index, "renderer": "channelMetadataRenderer"})
        header = document.get("header") if isinstance(document.get("header"), dict) else {}
        for obj, _path, _ancestors in _walk(header):
            if not isinstance(obj, dict):
                continue
            if "subscriberCountText" in obj:
                channel.subscriber_count = parse_count(obj["subscriberCountText"])
            if "videosCountText" in obj:
                channel.video_count = parse_count(obj["videosCountText"])
            if "banner" in obj and isinstance(obj["banner"], dict):
                banner = _largest_thumbnail(obj["banner"])
                if banner:
                    channel.banner_url = banner["url"]
            parts = obj.get("metadataParts")
            if isinstance(parts, list):
                for part in parts:
                    text = text_of((part or {}).get("text")) if isinstance(part, dict) else None
                    if not text:
                        continue
                    lowered = text.lower()
                    if "subscriber" in lowered and channel.subscriber_count is None:
                        channel.subscriber_count = parse_count(text)
                    elif "video" in lowered and channel.video_count is None:
                        channel.video_count = parse_count(text)
            if obj.get("imageBannerViewModel") and isinstance(obj["imageBannerViewModel"], dict):
                banner = _largest_thumbnail(obj["imageBannerViewModel"].get("image"))
                if banner:
                    channel.banner_url = banner["url"]
        return channel
    return None


def iter_documents(body: bytes) -> Iterator[dict]:
    """A JSON body (a youtubei answer) as documents, tolerant of the
    ``)]}'`` prefix YouTube sometimes sends."""
    text = (body or b"").decode("utf-8", errors="replace").lstrip()
    if text.startswith(")]}'"):
        text = text[4:]
    try:
        loaded = json.loads(text)
    except ValueError:
        return iter(())
    if isinstance(loaded, dict):
        return iter([loaded])
    if isinstance(loaded, list):
        return iter([d for d in loaded if isinstance(d, dict)])
    return iter(())
