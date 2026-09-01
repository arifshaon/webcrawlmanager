"""Curator-controlled Facebook Page capture for SWM.

The Facebook collector deliberately separates two preservation layers:

* every eligible browser exchange is written to WARC, including traffic seen
  while scrolling is paused and posts outside a requested export range;
* posts and comments recognised in Facebook GraphQL responses are normalised
  to JSONL/CSV for discovery and review.

Facebook changes its internal GraphQL schemas frequently. Extraction is
therefore evidence-led and defensive: records retain their source path and
synthetic identifiers are labelled. The manifest never calls a run complete;
it records the exact stopping rule, pagination failures and detected gaps.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import random
import re
import time
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from .browser import _CAPTURE_ARGS
from .capture import WarcSession
from .config import BehaviorConfig, BrowserConfig
from .recorder import (CMD_PAUSE, CMD_RESUME, CMD_STOP, PAUSED, RECORDING,
                       STOPPED, RecordingSession, _is_closed_error)
from .recording_runtime import RecordingBrowserDriver

log = logging.getLogger(__name__)

BLOCKED = "blocked"
FACEBOOK_MODES = {
    "date_range",
    "latest_n",
    "until_stopped",
    "end_of_timeline",
    "since_last",
}

def _readable_day(value: object) -> str:
    """A date a curator can read at a glance, from an ISO timestamp."""
    raw = str(value or "")
    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return raw[:10] or "unknown"


def _mode_summary(config: "FacebookCaptureConfig") -> str:
    """One sentence naming what this capture will do, for the curator."""
    mode = config.mode
    if mode == "date_range":
        span = config.from_date or "the earliest post"
        return (f"Collecting posts back to {span[:10]}"
                + (f" and no newer than {config.to_date[:10]}"
                   if config.to_date else ""))
    if mode == "latest_n":
        return f"Collecting the latest {config.latest_n} posts"
    if mode == "since_last":
        return "Collecting posts published since the previous capture"
    if mode == "end_of_timeline":
        return "Collecting posts until Facebook stops offering older ones"
    return "Collecting posts until you select Stop and save"


_FACEBOOK_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "web.facebook.com",
}
_OBVIOUS_NON_PAGE_PREFIXES = (
    "/profile.php", "/people/", "/groups/", "/events/", "/marketplace/",
    "/watch/", "/gaming/", "/friends/", "/messages/",
)
_PIN_KEYS = {
    "is_pinned", "is_pinned_post", "is_pinned_content", "pinned",
    "pinned_post", "is_fixed",
}
_POST_ID_KEYS = (
    "post_id", "story_fbid", "legacy_story_hideable_id",
    "legacy_fbid", "story_id", "id",
)
_COMMENT_ID_KEYS = ("comment_id", "legacy_fbid", "id")
_TIME_KEYS = (
    "creation_time", "created_time", "publish_time", "published_time",
    "timestamp", "created_at",
)
_PERMALINK_KEYS = (
    "permalink_url", "permalink", "www_url", "story_url", "url",
)
_GRAPHQL_PATH_PARTS = ("/api/graphql", "/graphql")
_PRIVATE_REQUEST_HEADERS = {
    "authorization", "cookie", "proxy-authorization", "x-csrf-token",
    "x-fb-lsd",
}
_PRIVATE_RESPONSE_HEADERS = {"set-cookie", "set-cookie2"}
_PRIVATE_FORM_FIELDS = {
    "pass", "password", "email", "login", "approvals_code", "otp",
    "one_time_code", "fb_dtsg", "lsd", "access_token", "auth_token",
    "session_key",
    # Not credentials, but they identify the account doing the capturing and
    # are of no archival value, so they are redacted alongside the secrets.
    "__user", "jazoest",
}
_AUTH_PATH_MARKERS = (
    "/login/", "/checkpoint/", "/recover/", "/two_factor/",
    "/security/2fac/",
)


def canonical_facebook_page_url(value: object) -> str:
    """Validate and normalise an explicit Facebook Page URL.

    URL shape cannot distinguish every vanity-named Page from a personal
    profile. Obvious profile/non-Page routes are rejected here; the GraphQL
    root ``__typename`` is checked again after the visible browser loads.
    """
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in _FACEBOOK_HOSTS:
        raise ValueError("Enter a Facebook Page URL on facebook.com.")
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/")
    if not path or path == "/":
        raise ValueError("Enter a specific Facebook Page URL, not Facebook home.")
    lowered = path.lower()
    if lowered.startswith(_OBVIOUS_NON_PAGE_PREFIXES):
        raise ValueError(
            "Facebook capture supports Pages only; personal profiles, groups "
            "and other Facebook surfaces are not accepted."
        )
    return urlunsplit(("https", "www.facebook.com", path, "", ""))


def _page_path_segment(url: str) -> str:
    """The vanity segment of a Page URL: the part that names the entity."""
    path = re.sub(r"/{2,}", "/", urlsplit(url).path or "")
    return path.strip("/").split("/")[0].lower()


def _root_identity_segments(root: dict) -> set[str]:
    """Vanity segments a GraphQL root node claims for itself.

    Used to tell whether a node describes the Page that was requested or some
    other entity that happens to appear in the same response -- most often the
    logged-in curator's own account, which Facebook attaches to many replies.
    """
    segments: set[str] = set()
    for key in ("url", "profile_url", "permalink_url", "www_url", "page_url",
                "vanity", "username", "short_name"):
        value = _scalar_text(root.get(key))
        if not value:
            continue
        if "/" not in value:
            segments.add(value.strip().lower())
            continue
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if host and host not in _FACEBOOK_HOSTS:
            continue
        segment = _page_path_segment(value)
        if segment:
            segments.add(segment)
    return segments


def facebook_page_key(url: str) -> str:
    parts = urlsplit(canonical_facebook_page_url(url))
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/").lower()
    return f"facebook:{path}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise_datetime(value: object) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    parsed: Optional[datetime] = None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{9,13}", text):
            return _normalise_datetime(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(text),
                                          datetime_time.min,
                                          tzinfo=timezone.utc)
            except ValueError:
                return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _date_bound(value: object, *, end: bool = False) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid date {text!r}; use YYYY-MM-DD.") from exc
    edge = datetime_time.max if end else datetime_time.min
    return datetime.combine(day, edge, tzinfo=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=False))
        handle.write("\n")


def _redact_headers(headers: object, private_names: set[str]) -> dict[str, str]:
    if isinstance(headers, Mapping):
        source = headers.items()
    else:
        try:
            source = iter(headers or ())  # type: ignore[arg-type]
        except TypeError:
            source = iter(())
    result: dict[str, str] = {}
    for item in source:
        if isinstance(item, Mapping):
            name, value = item.get("name"), item.get("value", "")
        else:
            try:
                name, value = item
            except (TypeError, ValueError):
                continue
        if str(name or "").lower() not in private_names:
            result[str(name)] = str(value)
    return result


def _redact_json_secrets(value: object) -> tuple[object, bool]:
    changed = False
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            if str(key).lower() in _PRIVATE_FORM_FIELDS:
                output[key] = "[REDACTED BY SWM]"
                changed = True
            else:
                output[key], child_changed = _redact_json_secrets(child)
                changed = changed or child_changed
        return output, changed
    if isinstance(value, list):
        output = []
        for child in value:
            redacted, child_changed = _redact_json_secrets(child)
            output.append(redacted)
            changed = changed or child_changed
        return output, changed
    return value, False


def _redact_post_data(url: str, post_data: bytes | None,
                      headers: object) -> tuple[bytes | None, bool]:
    if not post_data:
        return post_data, False
    request_headers = _redact_headers(headers, set())
    content_type = next((value for key, value in request_headers.items()
                         if key.lower() == "content-type"), "").lower()
    path = urlsplit(url).path.lower()
    try:
        text = post_data.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        if any(marker in path for marker in _AUTH_PATH_MARKERS):
            return b"[AUTHENTICATION REQUEST BODY REDACTED BY SWM]", True
        return post_data, False

    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None:
            redacted, changed = _redact_json_secrets(decoded)
            if changed:
                return json.dumps(redacted, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"), True

    if ("application/x-www-form-urlencoded" in content_type
            or "=" in text[:2000]):
        try:
            fields = parse_qsl(text, keep_blank_values=True)
        except ValueError:
            fields = []
        if fields:
            changed = False
            redacted_fields = []
            for key, value in fields:
                if key.lower() in _PRIVATE_FORM_FIELDS:
                    redacted_fields.append((key, "[REDACTED BY SWM]"))
                    changed = True
                elif key.lower() == "variables" and value.lstrip().startswith(
                        ("{", "[")):
                    try:
                        decoded_variables = json.loads(value)
                    except json.JSONDecodeError:
                        redacted_fields.append((key, value))
                    else:
                        safe_variables, variables_changed = \
                            _redact_json_secrets(decoded_variables)
                        redacted_fields.append((
                            key,
                            json.dumps(safe_variables, ensure_ascii=False,
                                       separators=(",", ":")),
                        ))
                        changed = changed or variables_changed
                else:
                    redacted_fields.append((key, value))
            if changed:
                return urlencode(redacted_fields, doseq=True).encode("utf-8"), True

    if any(marker in path for marker in _AUTH_PATH_MARKERS):
        return b"[AUTHENTICATION REQUEST BODY REDACTED BY SWM]", True
    return post_data, False


class FacebookWarcSession(WarcSession):
    """WARC writer that omits reusable authentication secrets.

    The Page's content remains available for replay, but Cookie/Authorization
    request headers, Set-Cookie response headers and known login/CSRF fields
    are not persisted in the archive.
    """

    def write_exchange(self, *, url: str, method: str, req_headers: object,
                       post_data: bytes | None, status: int, status_text: str,
                       resp_headers: object, body: bytes,
                       http_version: str = "HTTP/1.1") -> None:
        safe_request_headers = _redact_headers(
            req_headers, _PRIVATE_REQUEST_HEADERS)
        safe_response_headers = _redact_headers(
            resp_headers, _PRIVATE_RESPONSE_HEADERS)
        safe_post_data, redacted = _redact_post_data(
            url, post_data, req_headers)
        if redacted:
            safe_request_headers["X-SWM-Redacted"] = (
                "authentication or session fields omitted")
        super().write_exchange(
            url=url, method=method, req_headers=safe_request_headers,
            post_data=safe_post_data, status=status, status_text=status_text,
            resp_headers=safe_response_headers, body=body,
            http_version=http_version,
        )


def _scalar_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "content", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _walk(value: object, path: tuple[str, ...] = (),
          ancestors: tuple[dict, ...] = ()):
    if isinstance(value, dict):
        yield value, path, ancestors
        next_ancestors = (*ancestors[-5:], value)
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)), next_ancestors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)), ancestors)


def _norm_key(key: object) -> str:
    """Compare field names ignoring case and underscores.

    Facebook mixes naming conventions within one payload -- wwwURL beside
    creation_time, legacy_fbid beside feedbackTargetID -- so matching a field
    by its exact spelling misses the same field written another way.
    """
    return str(key).replace("_", "").lower()


def _lookup(obj: dict, keys: Iterable[str]) -> object:
    """First present value among ``keys``, in priority order, spelling-tolerant."""
    keys = list(keys)
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    normalised: dict[str, object] = {}
    for key, value in obj.items():
        if value not in (None, "", [], {}):
            normalised.setdefault(_norm_key(key), value)
    for key in keys:
        value = normalised.get(_norm_key(key))
        if value is not None:
            return value
    return None


def _has_key(obj: dict, keys: Iterable[str]) -> bool:
    wanted = {_norm_key(key) for key in keys}
    return any(_norm_key(key) in wanted for key in obj)


def _find_value(obj: dict, keys: Iterable[str], max_depth: int = 3) -> object:
    wanted = list(keys)
    queue: deque[tuple[object, int]] = deque([(obj, 0)])
    while queue:
        current, depth = queue.popleft()
        if not isinstance(current, dict):
            continue
        found = _lookup(current, wanted)
        if found is not None:
            return found
        if depth >= max_depth:
            continue
        for value in current.values():
            if isinstance(value, dict):
                queue.append((value, depth + 1))
            elif isinstance(value, list):
                for item in value[:20]:
                    if isinstance(item, dict):
                        queue.append((item, depth + 1))
    return None


def _identifier(obj: dict, keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = _lookup(obj, (key,))
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _message_text(obj: dict) -> Optional[str]:
    for key in ("message", "body", "text", "title", "description"):
        text = _scalar_text(obj.get(key))
        if text:
            return text
    value = _find_value(obj, ("message", "body"), max_depth=2)
    return _scalar_text(value)


def _created_time(obj: dict, ancestors: tuple[dict, ...]) -> Optional[str]:
    for candidate in (obj, *reversed(ancestors[-3:])):
        for key in _TIME_KEYS:
            parsed = _normalise_datetime(candidate.get(key))
            if parsed:
                return parsed
    value = _find_value(obj, _TIME_KEYS, max_depth=3)
    return _normalise_datetime(value)


def _permalink(obj: dict) -> Optional[str]:
    preferred: list[str] = []
    fallback: list[str] = []
    for current, _path, _ancestors in _walk(obj):
        for key in _PERMALINK_KEYS:
            value = _lookup(current, (key,))
            if not isinstance(value, str) or not value.startswith("http"):
                continue
            if any(part in value for part in ("/posts/", "story_fbid=",
                                               "/permalink/", "/videos/")):
                preferred.append(value)
            elif "facebook.com" in value:
                fallback.append(value)
        if preferred:
            break
    return (preferred or fallback or [None])[0]


def _actor(obj: dict) -> tuple[Optional[str], Optional[str]]:
    candidates: list[dict] = []
    for key in ("author", "actor", "owner", "actors", "owning_profile"):
        value = obj.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(v for v in value[:3] if isinstance(v, dict))
    if not candidates:
        value = _find_value(obj, ("actors", "author", "actor"), max_depth=3)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(v for v in value[:3] if isinstance(v, dict))
    for candidate in candidates:
        actor_id = _identifier(candidate, ("id", "profile_id", "actor_id"))
        name = _scalar_text(candidate.get("name"))
        if actor_id or name:
            return actor_id, name
    return None, None


def _is_pinned(obj: dict, ancestors: tuple[dict, ...]) -> bool:
    for candidate in (obj, *reversed(ancestors[-4:])):
        for key in _PIN_KEYS:
            value = _lookup(candidate, (key,))
            if value is True or (isinstance(value, str)
                                 and value.lower() in ("true", "pinned")):
                return True
        for key in ("tracking", "serialized_frtp_identifiers", "label"):
            value = candidate.get(key)
            if isinstance(value, str) and re.search(
                    r"(?:is[_ -]?pinned|pinned[_ -]?post).{0,20}(?:true|1)",
                    value, re.I):
                return True
    return False


def _media_urls(obj: dict, limit: int = 50) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    keys = {
        "playable_url", "playable_url_quality_hd", "browser_native_hd_url",
        "browser_native_sd_url", "image_url", "uri", "src",
    }
    for current, path, _ancestors in _walk(obj):
        for key in keys:
            value = current.get(key)
            if not isinstance(value, str) or not value.startswith("http"):
                continue
            path_text = ".".join((*path, key)).lower()
            if not any(marker in path_text for marker in (
                    "image", "media", "video", "attachment", "photo")):
                continue
            if value not in seen:
                seen.add(value)
                result.append(value)
                if len(result) >= limit:
                    return result
    return result


def _metric(obj: dict, metric: str) -> Optional[int]:
    aliases = {
        "reactions": ("reaction_count", "reactors_count", "reaction_count_reduced"),
        "comments": ("comment_count", "comments_count", "total_comment_count"),
        "shares": ("share_count", "shares_count", "i18n_share_count"),
    }[metric]
    for current, path, _ancestors in _walk(obj):
        path_text = ".".join(path).lower()
        if metric[:-1] not in path_text and metric not in path_text:
            continue
        for key in aliases:
            value = current.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        value = current.get("count") or current.get("total_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _timeline_item(path: tuple[str, ...], obj: dict) -> bool:
    joined = ".".join(path).lower()
    has_feed_path = any(marker in joined for marker in (
        "timeline", "feed_units", "timeline_feed", "edges",
    ))
    has_direct_identity = _has_key(obj, _POST_ID_KEYS[:-1])
    typename = str(obj.get("__typename") or "").lower()
    return has_feed_path and (has_direct_identity or "story" in typename
                              or "post" in typename)


@dataclass
class FacebookPost:
    post_id: str
    created_time: Optional[str] = None
    permalink_url: Optional[str] = None
    author_id: Optional[str] = None
    author_name: Optional[str] = None
    text: Optional[str] = None
    is_pinned: bool = False
    timeline_item: bool = False
    reactions_count: Optional[int] = None
    comments_count: Optional[int] = None
    shares_count: Optional[int] = None
    media_urls: list[str] = field(default_factory=list)
    source: str = "graphql"
    source_path: Optional[str] = None
    synthetic_id: bool = False


@dataclass
class FacebookComment:
    comment_id: str
    created_time: Optional[str] = None
    parent_post_id: Optional[str] = None
    parent_comment_id: Optional[str] = None
    author_id: Optional[str] = None
    author_name: Optional[str] = None
    text: Optional[str] = None
    depth: int = 0
    source_path: Optional[str] = None


def _merge_post(existing: FacebookPost, incoming: FacebookPost) -> FacebookPost:
    for name in (
        "created_time", "permalink_url", "author_id", "author_name", "text",
        "reactions_count", "comments_count", "shares_count", "source_path",
    ):
        if getattr(existing, name) in (None, "") and getattr(incoming, name) not in (None, ""):
            setattr(existing, name, getattr(incoming, name))
    existing.is_pinned = existing.is_pinned or incoming.is_pinned
    existing.timeline_item = existing.timeline_item or incoming.timeline_item
    existing.synthetic_id = existing.synthetic_id and incoming.synthetic_id
    existing.media_urls = list(dict.fromkeys(
        [*existing.media_urls, *incoming.media_urls]))[:50]
    return existing


def _looks_like_comment(obj: dict, path: tuple[str, ...]) -> bool:
    typename = str(obj.get("__typename") or "").lower()
    joined = ".".join(path).lower()
    has_text = bool(_message_text(obj))
    return has_text and (
        "comment" in typename
        or "comment_depth" in obj
        or ("comments" in joined and _has_key(obj, _COMMENT_ID_KEYS))
    )


def extract_graphql_records(
        documents: Iterable[object],
        page_segment: Optional[str] = None) -> tuple[
        list[FacebookPost], list[FacebookComment], Optional[str], Optional[str]]:
    """Extract schema-tolerant Page records from decoded GraphQL documents.

    ``page_segment`` is the vanity segment of the requested Page. A ``User``
    root only counts as the capture target when it claims that segment as its
    own identity; without that check the logged-in curator's own account --
    which Facebook attaches to many responses, and which carries timeline
    references of its own -- is mistaken for the requested target.
    """
    posts: dict[str, FacebookPost] = {}
    comments: dict[str, FacebookComment] = {}
    target_type: Optional[str] = None
    page_name: Optional[str] = None

    for document in documents:
        if isinstance(document, dict):
            data = document.get("data")
            if isinstance(data, dict):
                for key in ("node", "profile", "page"):
                    root = data.get(key)
                    if isinstance(root, dict):
                        typename = str(root.get("__typename") or "")
                        if typename == "Page":
                            # A Page root can only make the target look more
                            # like a Page, so it needs no identity check.
                            target_type = typename
                            page_name = page_name or _scalar_text(root.get("name"))
                            break
                        if (typename == "User" and page_segment
                                and page_segment in _root_identity_segments(root)):
                            target_type = typename
                            page_name = page_name or _scalar_text(root.get("name"))
                            break

        for obj, path, ancestors in _walk(document):
            if _looks_like_comment(obj, path):
                comment_id = _identifier(obj, _COMMENT_ID_KEYS)
                if not comment_id:
                    continue
                author_id, author_name = _actor(obj)
                depth_raw = obj.get("comment_depth") or obj.get("depth") or 0
                try:
                    depth = max(0, int(depth_raw))
                except (TypeError, ValueError):
                    depth = 0
                if depth == 0 and any("repl" in segment.lower()
                                      for segment in path):
                    depth = 1
                parent_post = _identifier(obj, (
                    "parent_post_id", "feedback_target_id", "story_fbid",
                ))
                if not parent_post:
                    for ancestor in reversed(ancestors):
                        if _looks_like_comment(ancestor, ()):
                            continue
                        parent_post = _identifier(ancestor, _POST_ID_KEYS)
                        if parent_post:
                            break
                parent_comment = _identifier(obj, (
                    "parent_comment_id", "reply_parent_id",
                ))
                comments.setdefault(comment_id, FacebookComment(
                    comment_id=comment_id,
                    created_time=_created_time(obj, ancestors),
                    parent_post_id=parent_post,
                    parent_comment_id=parent_comment,
                    author_id=author_id,
                    author_name=author_name,
                    text=_message_text(obj),
                    depth=depth,
                    source_path=".".join(path),
                ))
                continue

            typename = str(obj.get("__typename") or "").lower()
            post_id = _identifier(obj, _POST_ID_KEYS)
            created = _created_time(obj, ancestors)
            text = _message_text(obj)
            permalink = _permalink(obj)
            post_signal = (
                any(key in obj for key in _POST_ID_KEYS[:-1])
                or "story" in typename or "post" in typename
            )
            if not post_id or not post_signal or not (created or text or permalink):
                continue
            author_id, author_name = _actor(obj)
            candidate = FacebookPost(
                post_id=post_id,
                created_time=created,
                permalink_url=permalink,
                author_id=author_id,
                author_name=author_name,
                text=text,
                is_pinned=_is_pinned(obj, ancestors),
                timeline_item=_timeline_item(path, obj),
                reactions_count=_metric(obj, "reactions"),
                comments_count=_metric(obj, "comments"),
                shares_count=_metric(obj, "shares"),
                media_urls=_media_urls(obj),
                source_path=".".join(path),
            )
            if post_id in posts:
                _merge_post(posts[post_id], candidate)
            else:
                posts[post_id] = candidate
    return list(posts.values()), list(comments.values()), target_type, page_name


def decode_graphql_documents(body: bytes) -> list[object]:
    """Decode Facebook's JSON, JSON-lines and anti-JSON-prefix responses."""
    try:
        text = body.decode("utf-8-sig", errors="replace").strip()
    except Exception:
        return []
    if not text:
        return []
    text = re.sub(r"^for\s*\(;;\)\s*;?", "", text).lstrip()
    decoder = json.JSONDecoder()
    documents: list[object] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        if position >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            next_line = text.find("\n", position)
            if next_line < 0:
                break
            position = next_line + 1
            continue
        documents.append(value)
        position = end
    return documents


def _whole_number(value: object, default: int, label: str) -> int:
    """Parse a curator-supplied whole number, naming the field when it fails."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc


def _positive_number(value: object, default: float, label: str) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if number <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return number


@dataclass
class FacebookCaptureConfig:
    page_url: str
    page_key: str
    mode: str
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    latest_n: Optional[int] = None
    consecutive_older: int = 5
    capture_media: bool = True
    write_warc: bool = True
    auto_start: bool = True
    include_comments: bool = False
    max_comments_per_post: int = 25
    include_replies: bool = False
    scroll_pause_min: float = 1.5
    scroll_pause_max: float = 3.0
    end_stall_rounds: int = 8
    prior_newest_post_id: Optional[str] = None
    prior_newest_post_date: Optional[str] = None
    continuation_of: Optional[int] = None
    root_capture_id: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "FacebookCaptureConfig":
        page_url = canonical_facebook_page_url(raw.get("page_url"))
        page_key = str(raw.get("page_key") or facebook_page_key(page_url))
        mode = str(raw.get("mode") or "date_range")
        if mode not in FACEBOOK_MODES:
            raise ValueError(f"Unsupported Facebook capture mode: {mode}")
        from_date = _date_bound(raw.get("from_date"))
        to_date = _date_bound(raw.get("to_date"), end=True)
        if from_date and to_date and from_date > to_date:
            raise ValueError("The From date must be on or before the To date.")
        latest_n = raw.get("latest_n")
        if mode == "latest_n":
            try:
                latest_n = int(latest_n)
            except (TypeError, ValueError) as exc:
                raise ValueError("Latest N must be a whole number.") from exc
            if not 1 <= latest_n <= 100_000:
                raise ValueError("Latest N must be between 1 and 100,000.")
        if mode == "date_range" and not from_date:
            raise ValueError("Date range mode requires a From date.")
        if mode == "since_last" and not raw.get("prior_newest_post_date"):
            raise ValueError("No previous capture date is available for this Page.")
        maximum = _whole_number(
            raw.get("max_comments_per_post"), 25, "Maximum comments per post")
        if not 1 <= maximum <= 5_000:
            raise ValueError("Maximum comments per post must be between 1 and 5,000.")
        consecutive = _whole_number(
            raw.get("consecutive_older"), 5, "Consecutive older posts")
        stall_rounds = _whole_number(
            raw.get("end_stall_rounds"), 8, "Scroll attempts before stopping")
        pause_min = _positive_number(
            raw.get("scroll_pause_min"), 1.5, "Minimum scroll pause")
        pause_max = _positive_number(
            raw.get("scroll_pause_max"), 3.0, "Maximum scroll pause")
        if pause_min > pause_max:
            raise ValueError(
                "The minimum scroll pause must not be longer than the maximum.")
        return cls(
            page_url=page_url,
            page_key=page_key,
            mode=mode,
            from_date=from_date,
            to_date=to_date,
            latest_n=latest_n,
            consecutive_older=max(2, min(consecutive, 25)),
            capture_media=bool(raw.get("capture_media", True)),
            write_warc=bool(raw.get("write_warc", True)),
            auto_start=bool(raw.get("auto_start", True)),
            include_comments=bool(raw.get("include_comments", False)),
            max_comments_per_post=maximum,
            include_replies=bool(raw.get("include_replies", False)),
            scroll_pause_min=max(0.5, pause_min),
            scroll_pause_max=max(0.5, pause_max),
            end_stall_rounds=max(3, stall_rounds),
            prior_newest_post_id=raw.get("prior_newest_post_id"),
            prior_newest_post_date=_normalise_datetime(
                raw.get("prior_newest_post_date")),
            continuation_of=raw.get("continuation_of"),
            root_capture_id=raw.get("root_capture_id"),
        )


_MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "image/heic": ".heic",
}


def _media_suffix(url: str, content_type: str) -> str:
    """A sensible file extension, preferring what the server said it sent."""
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind in _MEDIA_SUFFIXES:
        return _MEDIA_SUFFIXES[kind]
    path = urlsplit(url).path
    for suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"):
        if path.lower().endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".bin"


class FacebookArchive:
    """Incremental normalised exports and preservation manifest."""

    POST_FIELDS = [
        "post_id", "created_time", "permalink_url", "author_id",
        "author_name", "text", "is_pinned", "timeline_item",
        "reactions_count", "comments_count", "shares_count", "media_urls",
        "source", "source_path", "synthetic_id",
    ]
    COMMENT_FIELDS = [
        "comment_id", "created_time", "parent_post_id", "parent_comment_id",
        "author_id", "author_name", "text", "depth", "source_path",
    ]

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.posts_path = out_dir / "facebook-posts.jsonl"
        self.comments_path = out_dir / "facebook-comments.jsonl"
        self.events_path = out_dir / "facebook-events.jsonl"
        self.manifest_path = out_dir / "facebook-manifest.json"
        self.checkpoint_path = out_dir / "facebook-checkpoint.json"
        self.posts: dict[str, FacebookPost] = {}
        self.comments: dict[str, FacebookComment] = {}
        # Media is kept as files as well as in WARC, so the capture can be
        # read without a replay browser.
        self.media_dir = out_dir / "media"
        self.media_path = out_dir / "facebook-media.json"
        self.media_index: dict[str, str] = {}

    def event(self, event: str, **details: object) -> None:
        _append_jsonl(self.events_path, {
            "time": _iso_now(), "event": event, **details,
        })

    def add_post(self, post: FacebookPost) -> bool:
        if post.post_id in self.posts:
            _merge_post(self.posts[post.post_id], post)
            return False
        self.posts[post.post_id] = post
        _append_jsonl(self.posts_path, asdict(post))
        return True

    def save_media(self, url: str, body: bytes,
                   content_type: str = "") -> Optional[str]:
        """Store one media object under a content-addressed name.

        Returns the file name, or None when there is nothing to store. Naming
        by digest means the same image referenced by several posts is kept
        once, and a repeated capture overwrites identical bytes harmlessly.
        """
        if not body:
            return None
        existing = self.media_index.get(url)
        if existing:
            return existing
        digest = hashlib.sha1(body).hexdigest()
        suffix = _media_suffix(url, content_type)
        name = f"{digest}{suffix}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        target = self.media_dir / name
        if not target.exists():
            temporary = target.with_name(name + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
        self.media_index[url] = name
        return name

    def add_comment(self, comment: FacebookComment) -> bool:
        if comment.comment_id in self.comments:
            return False
        self.comments[comment.comment_id] = comment
        _append_jsonl(self.comments_path, asdict(comment))
        return True

    @staticmethod
    def _write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                current = dict(row)
                if isinstance(current.get("media_urls"), list):
                    current["media_urls"] = json.dumps(
                        current["media_urls"], ensure_ascii=False)
                writer.writerow(current)
        temporary.replace(path)

    def write_exports(self) -> None:
        def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False,
                                            sort_keys=False))
                    handle.write("\n")
            temporary.replace(path)

        if self.media_index:
            _atomic_json(self.media_path, self.media_index)
        write_jsonl(
            self.posts_path, (asdict(post) for post in self.posts.values()))
        self._write_csv(
            self.out_dir / "facebook-posts.csv", self.POST_FIELDS,
            (asdict(post) for post in self.posts.values()),
        )
        if self.comments:
            write_jsonl(
                self.comments_path,
                (asdict(comment) for comment in self.comments.values()),
            )
            self._write_csv(
                self.out_dir / "facebook-comments.csv", self.COMMENT_FIELDS,
                (asdict(comment) for comment in self.comments.values()),
            )

    def checkpoint(self, checkpoint: dict, manifest: dict) -> None:
        _atomic_json(self.checkpoint_path, checkpoint)
        _atomic_json(self.manifest_path, manifest)


_FACEBOOK_WIDGET_JS = r"""
(() => {
  if (window.__swmFacebookWidgetInstalled) return;
  window.__swmFacebookWidgetInstalled = true;
  let state = "paused";
  let detail = "Log in if needed, open the Page, then start scrolling.";
  let root = null;
  const LABELS = {
    recording: "● Scrolling and capturing",
    paused: "⏸ Scrolling paused — capture remains on",
    blocked: "⚠ Facebook verification required",
    stopped: "Stopped"
  };
  function controls() {
    if (state === "recording") return [["pause", "Pause scrolling"], ["stop", "Stop and save"]];
    if (state === "paused") return [["resume", "Start / resume scrolling"], ["stop", "Stop and save"]];
    if (state === "blocked") return [["resume", "Verification resolved — resume"], ["stop", "Stop and save"]];
    return [];
  }
  function render() {
    if (!root) return;
    const status = root.querySelector(".swm-facebook-state");
    status.textContent = LABELS[state] || state;
    status.className = "swm-facebook-state " + state;
    root.querySelector(".swm-facebook-detail").textContent = detail || "";
    const bar = root.querySelector(".swm-facebook-buttons");
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    for (const [command, label] of controls()) {
      const button = document.createElement("button");
      button.textContent = label;
      button.addEventListener("click", () => {
        if (window.swmFacebookControl)
          window.swmFacebookControl(command).then(result => {
            state = result.state; detail = result.detail || detail; render();
          });
      });
      bar.appendChild(button);
    }
  }
  window.__swmSetFacebookState = value => {
    if (value && typeof value === "object") {
      state = value.state || state; detail = value.detail || "";
    }
    if (!root) install();
    render();
  };
  function install() {
    if (!document.documentElement || root) return;
    try {
      const host = document.createElement("div");
      const shadow = host.attachShadow({mode: "open"});
      const style = document.createElement("style");
      style.textContent = [
        ".box{position:fixed;right:16px;bottom:16px;z-index:2147483647;",
        "font:12px/1.4 system-ui,sans-serif;background:#1b1e23;color:#fff;",
        "border-radius:8px;padding:11px 12px;box-shadow:0 4px 16px rgba(0,0,0,.35);",
        "min-width:260px;max-width:340px}",
        ".title{font-weight:700;opacity:.75;font-size:10px;text-transform:uppercase;",
        "letter-spacing:.08em;margin-bottom:5px}",
        ".swm-facebook-state{font-weight:600;margin-bottom:4px}",
        ".recording{color:#58d5c9}.paused{color:#ffbd2e}.blocked{color:#ff8a80}",
        ".swm-facebook-detail{opacity:.75;font-size:11px;margin-bottom:8px}",
        ".swm-facebook-buttons{display:flex;gap:6px;flex-wrap:wrap}",
        "button{font:11px system-ui,sans-serif;border:1px solid rgba(255,255,255,.25);",
        "background:rgba(255,255,255,.08);color:#fff;border-radius:5px;",
        "padding:5px 8px;cursor:pointer}button:hover{background:rgba(255,255,255,.18)}"
      ].join(" ");
      const box = document.createElement("div"); box.className = "box";
      const title = document.createElement("div"); title.className = "title";
      title.textContent = "SWM Facebook Page capture";
      const status = document.createElement("div"); status.className = "swm-facebook-state";
      const detailEl = document.createElement("div"); detailEl.className = "swm-facebook-detail";
      const buttons = document.createElement("div"); buttons.className = "swm-facebook-buttons";
      box.appendChild(title); box.appendChild(status); box.appendChild(detailEl); box.appendChild(buttons);
      shadow.appendChild(style); shadow.appendChild(box); root = shadow;
      document.documentElement.appendChild(host); render();
      if (window.swmFacebookControl)
        window.swmFacebookControl("state").then(window.__swmSetFacebookState);
    } catch (_) { root = null; }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, {once:true});
  else install();
})();
"""


class FacebookBrowserDriver(RecordingBrowserDriver):
    """Visible Chrome with a durable SWM-owned Facebook login profile."""

    def _start(self) -> None:
        if self.cfg.mode == "native":
            super()._start()
            return
        profile = self.cfg.user_data_dir or str(
            Path("./facebook-profile-swm").resolve())
        Path(profile).mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict[str, Any] = {
            "headless": False,
            "channel": "chrome",
            "args": list(_CAPTURE_ARGS),
            "no_viewport": True,
            "service_workers": "allow",
            "ignore_https_errors": bool(self.cfg.proxy),
        }
        if self.cfg.proxy:
            launch_kwargs["proxy"] = {"server": self.cfg.proxy}
        if self.cfg.user_agent:
            launch_kwargs["user_agent"] = self.cfg.user_agent
        self._context = self._pw.chromium.launch_persistent_context(
            profile, **launch_kwargs)
        self._browser = self._context.browser
        log.info("Facebook capture uses persistent Chrome profile %s", profile)


class FacebookCaptureSession(RecordingSession):
    """Automatic Page scroller with always-on WARC capture and honest stops."""

    def __init__(self, *, config: FacebookCaptureConfig,
                 browser_cfg: BrowserConfig, warc: WarcSession,
                 output_dir: Path, crawl_id: int, crawl_name: str,
                 operator: str,
                 known_post_ids: Optional[set[str]] = None,
                 control_poll: Optional[Callable[[], Optional[str]]] = None,
                 on_progress: Optional[Callable[..., None]] = None,
                 persist_posts: Optional[Callable[[list[dict], Optional[str]], None]] = None):
        super().__init__(
            config.page_url, browser_cfg, warc,
            control_poll=control_poll, on_progress=on_progress,
            tick_seconds=0.35, page_timeout=60.0,
        )
        self.config = config
        self.output_dir = output_dir
        self.crawl_id = crawl_id
        self.crawl_name = crawl_name
        self.operator = operator
        self.archive = FacebookArchive(output_dir)
        self.state = PAUSED
        self.started_scrolling = False
        self.known_post_ids = set(known_post_ids or ())
        self.seen_this_run: dict[str, FacebookPost] = {}
        self._persist_batch: list[dict] = []
        self.persist_posts = persist_posts or (lambda posts, page_name: None)
        self.page_name: Optional[str] = None
        self.target_type: Optional[str] = None
        self.stop_reason: Optional[str] = None
        self.stop_rule: Optional[str] = None
        self.failure: Optional[str] = None
        self.phase_detail = (
            f"Opening the Page. {_mode_summary(config)} will begin "
            "automatically once the Page is on screen."
            if config.auto_start else
            "Waiting for you to start. Open the Page, then select Start "
            "scrolling. Capture is already active."
        )
        self.counters: Counter = Counter()
        self.exclusions: Counter = Counter()
        self._comment_counts: defaultdict[str, int] = defaultdict(int)
        self._commands = deque()
        self._pending_stop: Optional[tuple[str, str]] = None
        self._pending_block_reason: Optional[str] = None
        self._old_consecutive = 0
        # Per-post bookkeeping. Facebook streams a post across several GraphQL
        # fragments, so the same post_id is observed repeatedly and later
        # fragments enrich earlier ones. Selection, counting, persistence and
        # the stopping boundary therefore have to be idempotent per post while
        # still reacting to fields that only arrive on a later observation.
        self._timeline_posts: set[str] = set()
        self._nontimeline_posts: set[str] = set()
        self._pinned_posts: set[str] = set()
        self._synthetic_posts: set[str] = set()
        self._latest_admitted: set[str] = set()
        self._boundary_applied: set[str] = set()
        self._exclusion_reason: dict[str, str] = {}
        self._persisted_dates: dict[str, Optional[str]] = {}
        self._scrolls = 0
        self._stagnant_rounds = 0
        self._last_scroll_observed = 0
        self._last_scroll_marker: Optional[dict] = None
        self._next_scroll_at = 0.0
        self._last_dom_check = 0.0
        self._last_manual_position: Optional[float] = None
        self._last_manual_event = 0.0
        self._last_cursor: Optional[str] = None
        self._last_checkpoint_at = 0.0
        self._profile_rejected = False
        self._exhaustion_notified = False
        self._page_segment = _page_path_segment(config.page_url)
        # Media the browser already fetched, so an explicit fetch does not
        # duplicate it, plus the queue of media still to be collected.
        self._media_seen: set[str] = set()
        self._media_wanted: set[str] = set()
        self._media_queue: deque[tuple[str, str]] = deque()
        # While harvesting one post's permalink, every comment found belongs
        # to that post; this both attributes them and keeps the per-post
        # comment budget separate from other posts' budgets.
        self._permalink_post_id: Optional[str] = None
        self._harvest_done = False
        self.archive.event(
            "capture_created", mode=config.mode, page_url=config.page_url,
            continuation_of=config.continuation_of,
        )

    # -- controls ---------------------------------------------------------
    def _widget_state(self) -> dict:
        return {"state": self.state, "detail": self.phase_detail}

    def _on_widget_command(self, source, command: str = "state") -> dict:
        if command in (CMD_PAUSE, CMD_RESUME, CMD_STOP):
            page = source.get("page") if isinstance(source, dict) else None
            self._commands.append((command, page, "browser_widget"))
        return self._widget_state()

    def apply(self, command: str, page=None, actor: str = "dashboard") -> None:
        if command == CMD_PAUSE and self.state == RECORDING:
            self.state = PAUSED
            self.phase_detail = (
                "Automatic scrolling is paused. WARC capture and extraction "
                "remain active, including any manual browsing."
            )
            self._state_dirty = True
            self.archive.event("scrolling_paused", actor=actor,
                               current_url=self.current_url)
        elif command == CMD_RESUME and self.state in (PAUSED, BLOCKED):
            previous = self.state
            self.state = RECORDING
            self.started_scrolling = True
            self.phase_detail = "Automatic scrolling and capture are active."
            self._pending_block_reason = None
            self._next_scroll_at = 0.0
            self._state_dirty = True
            self.archive.event(
                "scrolling_resumed", actor=actor, previous_state=previous,
                current_url=self.current_url,
            )
        elif command == CMD_STOP and self.state != STOPPED:
            self._request_stop("curator_stop", "curator_selected_stop_and_save")
            self.archive.event("stop_requested", actor=actor,
                               current_url=self.current_url)

    def _request_stop(self, reason: str, rule: str) -> None:
        if self._pending_stop is None and self.state != STOPPED:
            self._pending_stop = (reason, rule)

    def _enter_blocked(self, reason: str) -> None:
        if self.state == STOPPED:
            return
        if self.state != BLOCKED or self.phase_detail != reason:
            self.archive.event("verification_or_block_detected", reason=reason,
                               current_url=self.current_url)
        self.state = BLOCKED
        self.phase_detail = reason
        self._state_dirty = True

    # -- network capture and extraction ----------------------------------
    @staticmethod
    def _is_graphql_url(url: str) -> bool:
        lowered = url.lower()
        return "facebook.com" in lowered and any(
            marker in lowered for marker in _GRAPHQL_PATH_PARTS)

    def _on_request(self, request) -> None:
        # Pause means pause *scrolling*, never pause preservation. Manual clicks
        # and navigation while paused stay eligible for WARC and extraction.
        if (not self.config.capture_media
                and request.resource_type in ("image", "media")):
            self.counters["media_requests_excluded"] += 1
            return
        self._eligible.add(request)
        if self._is_graphql_url(request.url):
            self._remember_request_cursor(request)

    def _on_download(self, download) -> None:
        # RecordingSession's hardened runtime normally keys eligibility to its
        # recording state. Facebook capture is always on, even while scrolling
        # is paused, so every browser download remains eligible.
        self._download_queue.append((download, True))

    def _on_request_failed(self, request) -> None:
        if self._is_graphql_url(request.url):
            self.counters["pagination_failures"] += 1
            self.archive.event("graphql_request_failed", url=request.url)
        super()._on_request_failed(request)

    def _remember_request_cursor(self, request) -> None:
        try:
            raw = request.post_data or ""
            form = parse_qs(raw, keep_blank_values=True)
            variables = json.loads((form.get("variables") or ["{}"]) [0])
        except Exception:
            return
        if isinstance(variables, dict):
            cursor = variables.get("cursor") or variables.get("after")
            if isinstance(cursor, str) and cursor:
                self._last_cursor = cursor

    def _write_exchange(self, response, body: bytes) -> None:
        try:
            if response.request.resource_type in ("image", "media"):
                self._media_seen.add(response.url)
                if body:
                    self.counters["media_captured"] += 1
                    if (self.config.capture_media
                            and response.url in self._media_wanted):
                        # The browser already produced these bytes; keep them
                        # rather than spending a second request on the URL.
                        self.archive.save_media(
                            response.url, body,
                            response.headers.get("content-type", ""))
        except Exception:
            pass
        if self._is_graphql_url(response.url):
            try:
                self._consume_graphql(response, body)
            except Exception as exc:
                self.counters["graphql_extraction_failures"] += 1
                self.archive.event(
                    "graphql_extraction_failed", url=response.url,
                    error=str(exc),
                )
                log.warning("Facebook GraphQL extraction failed for %s: %s",
                            response.url, exc)
        super()._write_exchange(response, body)

    def _consume_graphql(self, response, body: bytes) -> None:
        self.counters["graphql_responses"] += 1
        if response.status >= 400:
            self.counters["pagination_failures"] += 1
            self.archive.event("graphql_http_error", status=response.status,
                               url=response.url)
            if response.status in (401, 403, 429):
                self._pending_block_reason = (
                    f"Facebook returned HTTP {response.status}. Resolve any "
                    "verification in the browser, then resume scrolling."
                )
        documents = decode_graphql_documents(body)
        if not documents:
            self.counters["graphql_unparsed"] += 1
            return
        error_count = sum(
            len(document.get("errors") or [])
            for document in documents if isinstance(document, dict)
        )
        if error_count:
            self.counters["graphql_errors"] += error_count
            self.counters["pagination_failures"] += 1
            self.archive.event("graphql_payload_errors", count=error_count)
        posts, comments, target_type, page_name = extract_graphql_records(
            documents, self._page_segment)
        if target_type:
            self.target_type = target_type
            if target_type == "User" and not self._profile_rejected:
                self._profile_rejected = True
                # Stop rather than raise: whatever was captured before this
                # point is still written out, and the curator gets a plain
                # reason instead of a traceback.
                self.phase_detail = (
                    f"{self.config.page_url} resolved to a personal Facebook "
                    "profile. SWM Facebook capture supports Pages only, so "
                    "scrolling has stopped."
                )
                self._state_dirty = True
                self.archive.event("target_is_personal_profile",
                                   page_url=self.config.page_url)
                self._request_stop("unsupported_personal_profile",
                                   "graphql_root_identified_requested_target_"
                                   "as_user")
        if page_name:
            self.page_name = page_name
        for post in posts:
            self._consider_post(post)
        if self.config.include_comments:
            for comment in comments:
                self._consider_comment(comment)

    def _consider_post(self, post: FacebookPost) -> None:
        """Record an observation of a post and (re)assess what to do with it.

        Facebook delivers one post across several GraphQL fragments, so the
        same post_id arrives repeatedly and a later fragment may supply the
        timestamp, the pinned flag or the timeline context that the first one
        lacked. Assessment therefore runs on every observation against the
        merged record, and each side effect is guarded so it happens once.
        """
        existing = self.seen_this_run.get(post.post_id)
        if existing is not None:
            record = _merge_post(existing, post)
            self.counters["duplicate_post_observations"] += 1
        else:
            self.seen_this_run[post.post_id] = post
            record = post
        self._assess_post(record)

    def _assess_post(self, post: FacebookPost) -> None:
        """Idempotent per post; safe to re-run as later fragments enrich it."""
        post_id = post.post_id
        if not post.timeline_item:
            # Might still be promoted by a later fragment that carries the
            # timeline context, so keep it out of the observed count for now.
            self._nontimeline_posts.add(post_id)
            self._refresh_observation_counters()
            return

        if post_id not in self._timeline_posts:
            self._timeline_posts.add(post_id)
            self._nontimeline_posts.discard(post_id)
        if post.is_pinned:
            self._pinned_posts.add(post_id)
        else:
            self._pinned_posts.discard(post_id)
        if post.synthetic_id:
            self._synthetic_posts.add(post_id)
        else:
            self._synthetic_posts.discard(post_id)
        self._refresh_observation_counters()

        # Persist on first sight, and again once a timestamp appears, so the
        # incremental state used by since_last is not left holding a null date.
        if (post_id not in self._persisted_dates
                or (self._persisted_dates[post_id] is None
                    and post.created_time is not None)):
            self._persisted_dates[post_id] = post.created_time
            self._persist_batch.append(asdict(post))

        should_export, exclusion = self._select_post(post)
        if should_export:
            if self.archive.add_post(post):
                self.counters["posts_exported"] += 1
            self._queue_media(post)
            self._record_exclusion(post_id, None)
        else:
            self._record_exclusion(post_id, exclusion)

        if not post.is_pinned and post_id not in self._boundary_applied \
                and self._boundary_ready(post):
            self._boundary_applied.add(post_id)
            self._apply_stopping_boundary(post)

    def _refresh_observation_counters(self) -> None:
        """Derive observation counters from sets so that a post reclassified
        by a later fragment is not counted under both classifications."""
        self.counters["posts_observed"] = len(self._timeline_posts)
        self.counters["non_timeline_post_candidates"] = len(
            self._nontimeline_posts)
        self.counters["pinned_posts_observed"] = len(self._pinned_posts)
        self.counters["synthetic_post_ids"] = len(self._synthetic_posts)

    def _record_exclusion(self, post_id: str, reason: Optional[str]) -> None:
        """Track one current exclusion reason per post, so a post that later
        qualifies stops being counted under its earlier reason."""
        previous = self._exclusion_reason.get(post_id)
        if previous == reason:
            return
        if previous:
            self.exclusions[previous] -= 1
            if self.exclusions[previous] <= 0:
                del self.exclusions[previous]
        if reason:
            self._exclusion_reason[post_id] = reason
            self.exclusions[reason] += 1
        else:
            self._exclusion_reason.pop(post_id, None)

    def _boundary_ready(self, post: FacebookPost) -> bool:
        """Whether this post can be judged against the stopping rule yet.

        A post whose timestamp has not arrived tells us nothing about where we
        are in the timeline, so it must neither advance nor reset the
        consecutive counter. Leaving it unjudged keeps it eligible for a later
        fragment that supplies the date.
        """
        if self.config.mode == "date_range":
            return post.created_time is not None
        if self.config.mode == "since_last":
            return (post.created_time is not None
                    or post.post_id == self.config.prior_newest_post_id)
        return True

    def _select_post(self, post: FacebookPost) -> tuple[bool, Optional[str]]:
        mode = self.config.mode
        date_value = post.created_time
        if self.config.continuation_of and post.post_id in self.known_post_ids:
            return False, "already_captured_before_continuation"
        if mode == "since_last":
            if post.post_id in self.known_post_ids:
                return False, "captured_before_since_last"
            boundary = self.config.prior_newest_post_date
            if date_value and boundary and date_value < boundary:
                return False, "older_than_previous_capture"
            return True, None
        if mode == "date_range":
            if not date_value:
                return False, "date_unavailable"
            if self.config.from_date and date_value < self.config.from_date:
                return False, "older_than_from"
            if self.config.to_date and date_value > self.config.to_date:
                return False, "newer_than_to"
            return True, None
        if mode == "latest_n":
            if post.is_pinned:
                return True, None
            # Admission is recorded per post so re-assessing an already
            # admitted post cannot consume a second slot.
            if post.post_id in self._latest_admitted:
                return True, None
            if len(self._latest_admitted) < int(self.config.latest_n or 0):
                self._latest_admitted.add(post.post_id)
                return True, None
            return False, "beyond_latest_n"
        return True, None

    def _apply_stopping_boundary(self, post: FacebookPost) -> None:
        """Advance the stopping rule for one non-pinned timeline post.

        Called at most once per post, and only once _boundary_ready() says the
        post carries enough information to be judged, so a post with no
        timestamp neither advances nor resets the consecutive counter.
        """
        mode = self.config.mode
        if mode == "date_range":
            if post.created_time and self.config.from_date \
                    and post.created_time < self.config.from_date:
                self._old_consecutive += 1
            else:
                self._old_consecutive = 0
            if self._old_consecutive >= self.config.consecutive_older:
                self._request_stop(
                    "date_range_boundary_reached",
                    f"{self.config.consecutive_older}_consecutive_non_pinned_"
                    "timeline_posts_older_than_from",
                )
        elif mode == "latest_n" and len(self._latest_admitted) >= int(
                self.config.latest_n or 0):
            self._request_stop(
                "latest_n_reached",
                "latest_n_non_pinned_timeline_posts_observed",
            )
        elif mode == "since_last":
            boundary = self.config.prior_newest_post_date
            old = (
                post.post_id == self.config.prior_newest_post_id
                or bool(post.created_time and boundary
                        and post.created_time <= boundary)
            )
            self._old_consecutive = self._old_consecutive + 1 if old else 0
            if self._old_consecutive >= self.config.consecutive_older:
                self._request_stop(
                    "previous_capture_boundary_reached",
                    f"{self.config.consecutive_older}_consecutive_non_pinned_"
                    "timeline_posts_at_or_before_previous_capture",
                )

    def _consider_comment(self, comment: FacebookComment) -> None:
        if comment.depth > 0 and not self.config.include_replies:
            self.exclusions["replies_not_requested"] += 1
            return
        if self._permalink_post_id:
            # Found while that post's own permalink was open, so it is that
            # post's comment. Facebook labels comments with feedback ids that
            # need not match the post id, and trusting those split one post's
            # comments across several budgets and made the harvest loop read
            # its own progress as nil.
            comment.parent_post_id = self._permalink_post_id
        bucket = comment.parent_post_id
        if not bucket:
            # Fall back to the comment's position in the response. Only the
            # comment's own index is dropped: outer edge indices identify
            # which post in a feed the comment hangs off, and truncating at
            # the first index instead collapsed every post in the response
            # into one bucket, so the per-post limit applied across all of
            # them together.
            path = comment.source_path or ""
            last = None
            for last in re.finditer(r"\.edges\.\d+", path):
                pass
            bucket = (path[:last.start()] + ".edges") if last else path
            bucket = bucket or "unknown-parent"
            self.counters["comments_without_parent_post"] += 1
        if self._comment_counts[bucket] >= self.config.max_comments_per_post:
            self.exclusions["comment_limit_reached"] += 1
            return
        if self.archive.add_comment(comment):
            self._comment_counts[bucket] += 1
            self.counters["comments_exported"] += 1

    # -- page observation and automatic work -----------------------------
    def _on_frame_navigated(self, frame) -> None:
        if frame.parent_frame is not None:
            return
        url = frame.url
        if not url or url in ("about:blank", "about:srcdoc"):
            return
        previous = self.current_url
        self.current_url = url
        self.visited += 1
        self.archive.event(
            "top_level_navigation", from_url=previous, to_url=url,
            scrolling_state=self.state,
            note="Navigation may be curator-driven or page-driven.",
        )

    def _active_page(self, context):
        pages = [page for page in context.pages if not page.is_closed()]
        for page in reversed(pages):
            if "facebook.com" in (urlsplit(page.url).hostname or ""):
                return page
        return pages[-1] if pages else None

    def _collect_dom_posts(self, page) -> None:
        script = r"""
        () => Array.from(document.querySelectorAll('[role="article"]'))
          .filter(el => !el.parentElement || !el.parentElement.closest('[role="article"]'))
          .slice(-80)
          .map(article => {
            const links = Array.from(article.querySelectorAll('a[href]'));
            const permalink = links.map(a => a.href).find(h =>
              /\/posts\/|story_fbid=|\/permalink\/|\/videos\//.test(h)) || null;
            const timed = article.querySelector('abbr[data-utime], time[datetime]');
            const rawTime = timed ? (timed.getAttribute('data-utime') ||
              timed.getAttribute('datetime')) : null;
            const heading = article.querySelector('h2 a, h3 a, h4 a, strong a');
            const text = (article.innerText || '').trim().slice(0, 20000);
            return {permalink, raw_time: rawTime,
              author_name: heading ? (heading.textContent || '').trim() : null,
              text, is_pinned: /(^|\n)Pinned post(\n|$)/i.test(text)};
          })
        """
        try:
            rows = page.evaluate(script)
        except Exception:
            return
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            permalink = row.get("permalink")
            text = str(row.get("text") or "").strip() or None
            match = re.search(
                r"(?:/posts/|story_fbid=|/permalink/|/videos/)([A-Za-z0-9_.:-]+)",
                str(permalink or ""),
            )
            synthetic = not bool(match)
            raw_time = row.get("raw_time")
            if synthetic and not (permalink or raw_time):
                self.counters["dom_articles_without_stable_identity"] += 1
                continue
            post_id = (match.group(1) if match else "dom:" + hashlib.sha256(
                f"{permalink or ''}\n{raw_time or ''}\n"
                f"{row.get('author_name') or ''}\n{(text or '')[:500]}".encode(
                    "utf-8")
            ).hexdigest()[:24])
            post = FacebookPost(
                post_id=post_id,
                created_time=_normalise_datetime(raw_time),
                permalink_url=permalink,
                author_name=row.get("author_name"),
                text=text,
                is_pinned=bool(row.get("is_pinned")),
                timeline_item=True,
                source="dom",
                source_path="visible_top_level_article",
                synthetic_id=synthetic,
            )
            self._consider_post(post)

    # -- requested media ---------------------------------------------------
    def _queue_media(self, post: FacebookPost) -> None:
        """Note a post's media for collection.

        Facebook's CDN URLs are signed and time-limited, so media is collected
        during the run rather than from the exported records afterwards, by
        which time the URLs no longer resolve.
        """
        if not self.config.capture_media:
            return
        for url in post.media_urls:
            if not url or url in self._media_wanted:
                continue
            self._media_wanted.add(url)
            # Queued even when the browser has already requested the URL: that
            # earlier response was discarded, because nothing yet said this
            # media belonged to a captured post.
            self._media_queue.append((post.post_id, url))

    def _process_media_queue(self, budget: int = 3) -> None:
        """Fetch a few queued media objects, without stalling the scroll loop."""
        if not self._context:
            return
        while self._media_queue and budget > 0:
            post_id, url = self._media_queue.popleft()
            if url in self.archive.media_index:
                continue          # the browser's own copy was kept
            budget -= 1
            fetched = None
            try:
                fetched = self._context.request.get(url, timeout=30_000)
                if not fetched.ok:
                    raise RuntimeError(f"HTTP {fetched.status}")
                body = fetched.body()
                self.warc.write_exchange(
                    url=url, method="GET", req_headers={}, post_data=None,
                    status=fetched.status,
                    status_text=fetched.status_text or "",
                    resp_headers=fetched.headers, body=body,
                )
                self.archive.save_media(
                    url, body, fetched.headers.get("content-type", ""))
                self.counters["media_captured"] += 1
                self.counters["media_fetched_for_posts"] += 1
            except Exception as exc:
                self.counters["media_fetch_failures"] += 1
                self.archive.event("media_fetch_failed", url=url,
                                   post_id=post_id, error=str(exc))
            finally:
                if fetched is not None:
                    try:
                        fetched.dispose()
                    except Exception:
                        pass

    # -- requested comments ------------------------------------------------
    def _harvest_comments(self, context) -> None:
        """Collect comments from each captured post's own permalink.

        A Page feed never exposes a post's full comment thread, so honouring
        "maximum comments per post" means opening each post where its comments
        are actually paginated. Runs once the scrolling phase has finished, so
        it does not disturb the feed's scroll position.
        """
        if self._harvest_done or not self.config.include_comments:
            return
        self._harvest_done = True
        targets = [post for post in self.archive.posts.values()
                   if post.permalink_url]
        without = len(self.archive.posts) - len(targets)
        if without:
            self.counters["posts_without_permalink"] += without
        if not targets:
            # Without permalinks there is nowhere to go, and the only comments
            # in the capture are the one or two Facebook previews in the feed.
            # Say so: silence here reads as "this Page has few comments".
            self.phase_detail = (
                f"No comments collected: none of the {len(self.archive.posts)} "
                "captured posts carried a link to open. Comments cannot be "
                "read from the feed alone."
            )
            self._state_dirty = True
            self.archive.event("comment_harvest_impossible",
                               posts=len(self.archive.posts))
            log.warning("Facebook comment harvest skipped: no captured post "
                        "has a permalink to open")
            return
        if without:
            log.warning("%d of %d captured posts have no permalink and will "
                        "contribute no comments", without,
                        len(self.archive.posts))

        self.archive.event("comment_harvest_started", posts=len(targets),
                           maximum_per_post=self.config.max_comments_per_post,
                           include_replies=self.config.include_replies)
        page = None
        try:
            page = context.new_page()
            for index, post in enumerate(targets, start=1):
                if self._closed or self._stop_requested_during_harvest():
                    self.archive.event(
                        "comment_harvest_interrupted",
                        posts_completed=index - 1, posts_total=len(targets))
                    break
                self.phase_detail = (
                    f"Collecting comments: post {index} of {len(targets)}."
                )
                self._state_dirty = True
                self._harvest_one_post(page, post)
                self.counters["posts_comment_harvested"] += 1
                self._report_facebook()
                self._checkpoint()
        except Exception as exc:
            self.counters["comment_harvest_failures"] += 1
            self.archive.event("comment_harvest_failed", error=str(exc))
        finally:
            self._permalink_post_id = None
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            self.archive.event(
                "comment_harvest_finished",
                comments_exported=self.counters.get("comments_exported", 0))

    def _drain_requested_work(self, context) -> None:
        """Finish the collection the curator asked for before closing.

        Media queued while scrolling is fetched first, because its signed URLs
        expire; the per-post comment pass follows.
        """
        try:
            while self._media_queue:
                if self._closed or self._stop_requested_during_harvest():
                    self.archive.event(
                        "media_collection_interrupted",
                        outstanding=len(self._media_queue))
                    break
                self.phase_detail = (
                    f"Collecting media: {len(self._media_queue)} remaining."
                )
                self._state_dirty = True
                self._process_media_queue(budget=5)
                self._report_facebook()
        except Exception as exc:
            self.archive.event("media_collection_failed", error=str(exc))
        try:
            self._harvest_comments(context)
        except Exception as exc:
            self.counters["comment_harvest_failures"] += 1
            self.archive.event("comment_harvest_failed", error=str(exc))

    def _stop_requested_during_harvest(self) -> bool:
        """A second stop, given while comments are being collected, ends it."""
        try:
            command = self.control_poll()
        except Exception:
            return False
        if command == CMD_STOP:
            return True
        while self._commands:
            widget_command, _page, _actor = self._commands.popleft()
            if widget_command == CMD_STOP:
                return True
        return False

    def _show_all_comments(self, page) -> None:
        """Switch the thread from Facebook's default filtered view.

        A permalink opens on "Most relevant", which shows a fraction of the
        thread and will never yield the requested number however long it is
        paginated. Selecting "All comments" is what makes the rest reachable.
        """
        script = r"""
        () => {
          const opener = Array.from(
            document.querySelectorAll('[role="button"], button')).find(el => {
              const label = ((el.innerText || '') + ' ' +
                (el.getAttribute('aria-label') || '')).toLowerCase();
              return /most relevant|top comments|relevant/.test(label);
            });
          if (!opener) return {opened: false, chose: false};
          opener.click();
          return {opened: true, chose: false};
        }
        """
        chooser = r"""
        () => {
          const option = Array.from(document.querySelectorAll(
            '[role="menuitem"], [role="menuitemradio"], [role="option"]'))
            .find(el => /all comments/i.test(el.innerText || ''));
          if (!option) return false;
          option.click();
          return true;
        }
        """
        try:
            opened = page.evaluate(script)
            if not (opened or {}).get("opened"):
                return
            page.wait_for_timeout(700)
            if page.evaluate(chooser):
                self.counters["comment_filter_set_to_all"] += 1
                page.wait_for_timeout(1500)
        except Exception as exc:
            log.debug("Comment ordering unchanged: %s", exc)

    def _harvest_one_post(self, page, post: FacebookPost) -> None:
        self._permalink_post_id = post.post_id
        try:
            page.goto(post.permalink_url, wait_until="domcontentloaded",
                      timeout=int(self.page_timeout * 1000))
        except Exception as exc:
            self.counters["comment_page_failures"] += 1
            self.archive.event("comment_page_failed",
                               post_id=post.post_id, error=str(exc))
            self._permalink_post_id = None
            return
        try:
            page.wait_for_timeout(1500)
            self._process_media_queue(budget=6)
            self._show_all_comments(page)
            wanted = self.config.max_comments_per_post
            stalled = 0
            for _ in range(60):
                collected = len(self.archive.comments)
                if (self._comment_counts.get(post.post_id, 0) >= wanted
                        or stalled >= 5):
                    break
                if self._closed or self._stop_requested_during_harvest():
                    break
                clicked = self._expand_comments(page)
                # Comments arrive on scroll as well as on click, and the
                # thread is usually below the fold on a permalink.
                try:
                    page.evaluate(
                        "() => window.scrollBy({top: window.innerHeight * 0.8,"
                        " left: 0, behavior: 'auto'})")
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                if len(self.archive.comments) > collected:
                    stalled = 0
                elif not clicked:
                    stalled += 1
        except Exception as exc:
            self.counters["comment_page_failures"] += 1
            self.archive.event("comment_expansion_failed",
                               post_id=post.post_id, error=str(exc))
        finally:
            self._permalink_post_id = None

    def _expand_comments(self, page) -> int:
        """Click whatever exposes more comments, returning how many controls
        were clicked. Used both while scrolling the feed and, more
        productively, on an individual post's permalink."""
        if not self.config.include_comments:
            return 0
        script = r"""
        ({maximum, includeReplies}) => {
          const top = Array.from(document.querySelectorAll('[role="article"]'))
            .filter(el => !el.parentElement || !el.parentElement.closest('[role="article"]'));
          let clicked = 0;
          const budget = perPost ? 12 : 4;
          const scope = perPost ? top : top.slice(-30);
          for (const post of scope) {
            const comments = post.querySelectorAll('[role="article"] [role="article"]').length;
            if (!perPost && comments >= maximum) continue;
            const controls = Array.from(post.querySelectorAll('button, [role="button"]'));
            for (const control of controls) {
              if (clicked >= budget) break;
              if (control.dataset && control.dataset.swmClicked === '1') continue;
              const label = ((control.innerText || '') + ' ' +
                (control.getAttribute('aria-label') || '')).trim();
              const commentMore = /view (all|more|previous).*comments|more comments|previous comments/i.test(label);
              // "3 replies", "View 2 replies", "View more replies"
              const replyMore = /view (all|more).*repl|more repl|^\d[\d,.]*\s+repl/i.test(label);
              if (commentMore || (includeReplies && replyMore)) {
                try { control.dataset.swmClicked = '1'; } catch (_) {}
                control.click(); clicked += 1;
              }
            }
            if (clicked >= budget) break;
          }
          return {clicked, posts_examined: top.length};
        }
        """
        try:
            result = page.evaluate(script, {
                "maximum": self.config.max_comments_per_post,
                "includeReplies": self.config.include_replies,
                "perPost": self._permalink_post_id is not None,
            })
        except Exception:
            return 0
        clicked = int((result or {}).get("clicked") or 0)
        if clicked:
            self.counters["comment_expansion_clicks"] += clicked
        return clicked

    def _page_marker(self, page) -> dict:
        try:
            return page.evaluate("""
              () => ({y: window.scrollY, height: document.documentElement.scrollHeight,
                      viewport: window.innerHeight,
                      at_bottom: window.scrollY + window.innerHeight >=
                        document.documentElement.scrollHeight - 12})
            """)
        except Exception:
            return {"y": 0, "height": 0, "viewport": 0, "at_bottom": False}

    def _scroll_once(self, page) -> None:
        self._collect_dom_posts(page)
        self._expand_comments(page)
        marker = self._page_marker(page)
        observed = int(self.counters.get("posts_observed", 0))
        if self._last_scroll_marker is not None:
            no_posts = observed <= self._last_scroll_observed
            no_growth = marker.get("height", 0) <= self._last_scroll_marker.get(
                "height", 0) + 5
            at_bottom = bool(marker.get("at_bottom"))
            if no_posts and (no_growth or at_bottom):
                self._stagnant_rounds += 1
            else:
                self._stagnant_rounds = 0
        self._last_scroll_marker = marker
        self._last_scroll_observed = observed

        if self._pending_stop:
            self._checkpoint(force=True)
            return

        if self._stagnant_rounds >= self.config.end_stall_rounds:
            if self.config.mode == "until_stopped":
                # The curator asked to run until they stop it. A stall can be
                # temporary -- throttling, a slow network -- so report it and
                # keep trying rather than deciding the run is over for them.
                if not self._exhaustion_notified:
                    self._exhaustion_notified = True
                    self.phase_detail = (
                        "Facebook has stopped returning new posts after "
                        f"{self.config.end_stall_rounds} scroll attempts. "
                        "Scrolling continues in case more appear -- use Stop "
                        "and save when you have gone far enough."
                    )
                    self._state_dirty = True
                    self.archive.event(
                        "timeline_appears_exhausted",
                        scroll_attempts=self._scrolls,
                        posts_observed=self.counters.get("posts_observed", 0),
                    )
            else:
                self._request_stop(
                    "end_of_available_timeline",
                    f"no_new_posts_after_{self.config.end_stall_rounds}_scroll_attempts",
                )
                return
        elif self._exhaustion_notified:
            # New posts arrived after all; withdraw the exhaustion notice.
            self._exhaustion_notified = False
            self.phase_detail = "Scrolling and collecting posts."
            self._state_dirty = True
        try:
            # 'auto' rather than 'smooth': smooth scrolling animates
            # asynchronously, so the marker read at the start of the next
            # cycle can land mid-animation and misreport both position and
            # height, which is what the stagnation check depends on.
            page.evaluate("""
              () => window.scrollBy({top: Math.max(640, window.innerHeight * 0.86),
                                     left: 0, behavior: 'auto'})
            """)
        except Exception as exc:
            self.counters["scroll_failures"] += 1
            self.archive.event("scroll_failed", error=str(exc))
            if self.counters["scroll_failures"] >= 3:
                self._enter_blocked(
                    "Automatic scrolling failed repeatedly. You can navigate "
                    "manually, then resume or stop and save."
                )
            return
        self._scrolls += 1
        self.counters["scroll_attempts"] = self._scrolls
        if not self._exhaustion_notified:
            self.phase_detail = self._scrolling_summary()
            self._state_dirty = True
        self._process_media_queue()
        self._next_scroll_at = time.monotonic() + random.uniform(
            self.config.scroll_pause_min, self.config.scroll_pause_max)
        self._checkpoint()

    def _maybe_auto_start(self, page) -> None:
        """Begin the chosen mode as soon as the Page is genuinely on screen.

        The curator picked a mode and its criteria; making them press a button
        afterwards adds nothing. What can genuinely need a person -- a login
        wall, a verification challenge, or the wrong page being open -- is
        reported instead, and scrolling starts by itself once that clears.
        """
        if not self.config.auto_start or self._pending_stop:
            return
        if self.state == PAUSED and self.started_scrolling:
            # A pause the curator asked for stays until they lift it.
            return
        if self.state not in (PAUSED, BLOCKED):
            return
        blocker = self._detect_verification(page)
        if blocker:
            # _detect_verification's own message already says what to do.
            if self.phase_detail != blocker:
                self.phase_detail = blocker
                self._state_dirty = True
            return
        if not self._page_is_target(page):
            waiting = (
                f"Waiting for {self.config.page_url} to open in the browser. "
                f"{_mode_summary(self.config)} will begin automatically."
            )
            if self.phase_detail != waiting:
                self.phase_detail = waiting
                self._state_dirty = True
            return
        self.archive.event(
            "auto_start" if not self.started_scrolling else "auto_resumed",
            mode=self.config.mode, previous_state=self.state,
            current_url=page.url)
        self.apply(CMD_RESUME, actor="automatic")
        self.phase_detail = f"{_mode_summary(self.config)}."
        self._state_dirty = True

    def _page_is_target(self, page) -> bool:
        """Whether the browser is showing the Page this capture is for."""
        try:
            current = page.url or ""
        except Exception:
            return False
        if not current.startswith(("http://", "https://")):
            return False
        segment = _page_path_segment(current)
        return bool(segment) and segment == self._page_segment

    def _detect_verification(self, page) -> Optional[str]:
        url = page.url.lower()
        path = urlsplit(url).path.rstrip("/") or "/"
        if any(marker in url for marker in ("/login/", "/login.php")):
            return (
                "Facebook login is required. Sign in directly in the browser, "
                "return to the requested Page, then resume scrolling."
            )
        if path in ("/", "/home.php"):
            return (
                "The requested Facebook Page is not open. Navigate to the Page "
                "in this browser window, then resume scrolling."
            )
        if any(marker in url for marker in ("/checkpoint/", "/challenge/")):
            return (
                "Facebook is asking for verification. Resolve it in the "
                "browser window; capture remains active, then resume scrolling."
            )
        try:
            body = page.locator("body").inner_text(timeout=750).lower()[:12000]
        except Exception:
            return None
        markers = (
            "confirm it's you", "confirm your identity", "security check",
            "we need to verify", "account temporarily locked",
            "enter the code we sent",
        )
        if any(marker in body for marker in markers):
            return (
                "Facebook is asking for verification. Resolve it in the "
                "browser window; capture remains active, then resume scrolling."
            )
        return None

    def _check_manual_activity(self, page) -> None:
        marker = self._page_marker(page)
        current = float(marker.get("y") or 0)
        now = time.monotonic()
        if (self._last_manual_position is not None
                and abs(current - self._last_manual_position) > 80
                and now - self._last_manual_event > 2.0):
            self.archive.event(
                "curator_scroll_while_automatic_scrolling_inactive",
                scroll_y=current, state=self.state,
                note="Traffic and extracted records remained in the capture.",
            )
            self._last_manual_event = now
        self._last_manual_position = current

    # -- reporting and durable checkpoints -------------------------------
    def _coverage(self) -> dict:
        """Date bounds actually achieved, and whether the request was met.

        Bounds are reported for the exported dataset and, separately, for
        everything observed while scrolling. In date_range mode the observed
        span necessarily reaches past both ends of the request, so only the
        exported span describes the dataset a reader is holding.

        requested_range_satisfied is deliberately conservative: it is True only
        when the run ended because its own stopping rule fired. A run that
        ended because the curator stopped it, because the feed stalled, or
        because Facebook interrupted it, reports False -- the requested range
        may still be complete, but this capture cannot demonstrate it.
        """
        exported = [post.created_time for post in self.archive.posts.values()
                    if post.created_time]
        observed = [post.created_time for post in self.seen_this_run.values()
                    if post.created_time]
        mode = self.config.mode
        if mode == "date_range":
            satisfied = self.stop_reason == "date_range_boundary_reached"
        elif mode == "since_last":
            satisfied = self.stop_reason == "previous_capture_boundary_reached"
        elif mode == "latest_n":
            satisfied = len(self.archive.posts) >= int(self.config.latest_n or 0)
        else:
            # until_stopped and end_of_timeline make no range request, so there
            # is nothing to satisfy.
            satisfied = None
        return {
            "exported_newest_post": max(exported) if exported else None,
            "exported_oldest_post": min(exported) if exported else None,
            "observed_newest_post": max(observed) if observed else None,
            "observed_oldest_post": min(observed) if observed else None,
            "exported_posts_without_date": sum(
                1 for post in self.archive.posts.values()
                if not post.created_time),
            "requested_range_satisfied": satisfied,
        }

    _STOP_REASONS = {
        "date_range_boundary_reached": "the requested date range was covered",
        "latest_n_reached": "the requested number of posts was reached",
        "previous_capture_boundary_reached":
            "it reached the previous capture of this Page",
        "end_of_available_timeline":
            "Facebook stopped offering older posts",
        "curator_stop": "you selected Stop and save",
        "browser_closed": "the browser was closed",
        "unsupported_personal_profile":
            "the requested URL is a personal profile, not a Page",
        "capture_failed": "the capture failed",
    }

    def _scrolling_summary(self) -> str:
        """What the capture is doing right now, refreshed on every scroll.

        Posts arrive in GraphQL responses as Facebook answers each scroll, so
        the response count is the honest signal that collection is moving even
        during a stretch where no new post qualifies for export.
        """
        posts = len(self.archive.posts)
        responses = self.counters.get("graphql_responses", 0)
        mode = self.config.mode
        if mode == "latest_n":
            head = f"{posts} of {self.config.latest_n} posts"
        elif mode == "date_range" and self.config.from_date:
            head = f"{posts} posts, collecting back to {self.config.from_date[:10]}"
        else:
            head = f"{posts} posts"
        oldest = self._coverage()["exported_oldest_post"]
        parts = [head]
        if oldest:
            parts.append(f"reached {_readable_day(oldest)}")
        parts.append(f"{responses} API response"
                     f"{'' if responses == 1 else 's'}")
        failures = self.counters.get("pagination_failures", 0)
        if failures:
            parts.append(f"{failures} failed")
        comments = len(self.archive.comments)
        if comments:
            parts.append(f"{comments} comments so far")
        return " · ".join(parts)

    def _closing_summary(self) -> str:
        """What a finished capture leaves on screen.

        The list shows this line long after the run ends, so it says what was
        collected and why it stopped rather than describing a step that is
        over.
        """
        posts = len(self.archive.posts)
        comments = len(self.archive.comments)
        media = len(self.archive.media_index)
        collected = f"{posts} post{'' if posts == 1 else 's'}"
        if self.config.include_comments or comments:
            collected += f", {comments} comment{'' if comments == 1 else 's'}"
        if self.config.capture_media or media:
            collected += f", {media} media file{'' if media == 1 else 's'}"
        because = self._STOP_REASONS.get(self.stop_reason or "")
        if not because and self.stop_reason:
            because = str(self.stop_reason).replace("_", " ")
        return (f"Collected {collected}."
                + (f" Stopped because {because}." if because else ""))

    def _progress_details(self) -> dict:
        coverage = self._coverage()
        return {
            "phase": (
                "finished" if self.state == STOPPED
                else "verification_required" if self.state == BLOCKED
                else "scrolling" if self.state == RECORDING
                else "waiting_to_start" if not self.started_scrolling
                else "scrolling_paused"
            ),
            "message": self.phase_detail,
            "posts_observed": self.counters.get("posts_observed", 0),
            "posts_exported": self.counters.get("posts_exported", 0),
            "comments_exported": self.counters.get("comments_exported", 0),
            "pinned_posts": self.counters.get("pinned_posts_observed", 0),
            # Bounds of the exported dataset. Observed bounds are wider in
            # date_range mode, because posts outside the range are still seen
            # while scrolling past them; reporting those here would overstate
            # what the exports actually contain.
            "newest_post": coverage["exported_newest_post"],
            "oldest_post": coverage["exported_oldest_post"],
            "observed_newest_post": coverage["observed_newest_post"],
            "observed_oldest_post": coverage["observed_oldest_post"],
            "requested_range_satisfied": coverage["requested_range_satisfied"],
            "posts_without_permalink": self.counters.get(
                "posts_without_permalink", 0),
            "graphql_responses": self.counters.get("graphql_responses", 0),
            "graphql_errors": self.counters.get("graphql_errors", 0),
            "graphql_unparsed": self.counters.get("graphql_unparsed", 0),
            "media_captured": self.counters.get("media_captured", 0),
            "pagination_failures": self.counters.get("pagination_failures", 0),
            "scroll_attempts": self._scrolls,
            "consecutive_older_posts": self._old_consecutive,
            "stop_reason": self.stop_reason,
            "stop_rule": self.stop_rule,
            "continuation_of": self.config.continuation_of,
        }

    def _checkpoint_document(self) -> dict:
        details = self._progress_details()
        return {
            "schema": "swm-facebook-checkpoint-v1",
            "updated_at": _iso_now(),
            "crawl_id": self.crawl_id,
            "page_key": self.config.page_key,
            "page_url": self.config.page_url,
            "mode": self.config.mode,
            "last_graphql_cursor": self._last_cursor,
            "known_post_ids_before_run": len(self.known_post_ids),
            **details,
        }

    def _manifest_document(self, *, final: bool = False) -> dict:
        return {
            "schema": "swm-facebook-capture-manifest-v1",
            "capture": {
                "crawl_id": self.crawl_id,
                "name": self.crawl_name,
                "operator": self.operator,
                "page_url": self.config.page_url,
                "page_key": self.config.page_key,
                "page_name": self.page_name,
                "target_type": self.target_type,
                "mode": self.config.mode,
                "parameters": {
                    "from": self.config.from_date,
                    "to": self.config.to_date,
                    "latest_n": self.config.latest_n,
                    "consecutive_older_required": self.config.consecutive_older,
                    "capture_media": self.config.capture_media,
                    "write_warc": self.config.write_warc,
                    "auto_start": self.config.auto_start,
                    "include_comments": self.config.include_comments,
                    "maximum_comments_per_post": self.config.max_comments_per_post,
                    "include_replies": self.config.include_replies,
                },
                "continuation_of": self.config.continuation_of,
                "root_capture_id": self.config.root_capture_id,
                "state": self.state,
                "final": final,
                "stop_reason": self.stop_reason,
                "stopping_rule_fired": self.stop_rule,
                "failure": self.failure,
            },
            "layers": {
                "raw": {
                    "format": "WARC 1.1",
                    "replayability": (
                        "The WARC preserves the exchanges as served, but a "
                        "Facebook feed cannot be re-driven in a replay "
                        "browser. Timeline and comment pages are fetched by "
                        "GraphQL POSTs whose bodies carry per-session and "
                        "per-request-order values, and the reusable secrets "
                        "among those are redacted before writing, so a replay "
                        "client cannot reproduce the request that addressed a "
                        "given response. Replay shows the page as first "
                        "loaded; the normalised exports, not the replay, are "
                        "the record of what was collected."
                    ),
                    "media": (
                        "Media is captured as the browser loaded it while "
                        "scrolling. Images Facebook never requested -- lazy "
                        "loads that did not trigger -- are absent from the "
                        "WARC even when their URLs appear in post records."
                    ),
                    "policy": (
                        "Eligible network exchanges are retained even while "
                        "automatic scrolling is paused and when posts fall "
                        "outside the requested normalised-export range."
                    ),
                    "newer_than_to_posts": (
                        "retained in WARC; excluded from normalised exports"
                    ),
                    "credential_redaction": (
                        "Cookie and Authorization request headers, Set-Cookie "
                        "response headers, and recognised login/session fields "
                        "are omitted or redacted before WARC writing."
                    ),
                },
                "normalised": {
                    "posts": ["facebook-posts.jsonl", "facebook-posts.csv"],
                    "comments": (
                        ["facebook-comments.jsonl", "facebook-comments.csv"]
                        if self.archive.comments else []
                    ),
                    "selection_exclusions": dict(self.exclusions),
                },
            },
            "counts": {
                **dict(self.counters),
                "normalised_posts": len(self.archive.posts),
                "normalised_comments": len(self.archive.comments),
                "pagination_failures": self.counters.get(
                    "pagination_failures", 0),
            },
            "coverage": self._coverage(),
            "requested_work": {
                "comments": {
                    "requested": self.config.include_comments,
                    "replies_requested": self.config.include_replies,
                    "maximum_per_post": self.config.max_comments_per_post,
                    "posts_visited_for_comments": self.counters.get(
                        "posts_comment_harvested", 0),
                    "posts_without_permalink": self.counters.get(
                        "posts_without_permalink", 0),
                    "comments_exported": len(self.archive.comments),
                    "note": (
                        "Comments are collected from each post's own "
                        "permalink after scrolling ends, because a Page feed "
                        "never exposes a full comment thread. A post with no "
                        "permalink in its record cannot be visited."
                    ),
                },
                "media": {
                    "requested": self.config.capture_media,
                    "captured": self.counters.get("media_captured", 0),
                    "fetched_for_posts": self.counters.get(
                        "media_fetched_for_posts", 0),
                    "fetch_failures": self.counters.get(
                        "media_fetch_failures", 0),
                    "outstanding_at_close": len(self._media_queue),
                    "note": (
                        "Media referenced by captured posts is fetched during "
                        "the run, while its signed URLs still resolve, in "
                        "addition to whatever the browser loaded by itself."
                    ),
                },
            },
            "completeness": {
                "claim": "No claim of complete Facebook Page capture is made.",
                "requested_range_satisfied_meaning": (
                    "True only when this capture ended because its own "
                    "stopping rule fired. A curator-stopped, stalled or "
                    "interrupted run reports False even if the requested "
                    "range happens to be complete."
                ),
                "known_gaps": (
                    "pagination_failures counts GraphQL responses that could "
                    "not be read. Gaps Facebook never disclosed cannot be "
                    "detected and are not counted."
                ),
                "end_of_available_timeline_meaning": (
                    "Facebook stopped exposing additional posts to this "
                    "browser session after repeated scroll attempts; it does "
                    "not mean all posts ever published by the Page."
                ),
                "comment_limit_is_best_effort": bool(
                    self.config.include_comments),
            },
            "files": {
                "warc": "*.warc.gz",
                "events": "facebook-events.jsonl",
                "checkpoint": "facebook-checkpoint.json",
            },
            "updated_at": _iso_now(),
        }

    def _flush_persist_batch(self) -> None:
        if not self._persist_batch:
            return
        batch = self._persist_batch
        self._persist_batch = []
        try:
            self.persist_posts(batch, self.page_name)
        except Exception as exc:
            self.counters["state_persistence_failures"] += 1
            self.archive.event("state_persistence_failed", error=str(exc),
                               posts=len(batch))

    def _checkpoint(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_checkpoint_at < 1.0:
            return
        self._flush_persist_batch()
        self.archive.checkpoint(
            self._checkpoint_document(), self._manifest_document(final=False))
        self._last_checkpoint_at = now

    def _report_facebook(self) -> None:
        try:
            self.on_progress(
                state=self.state,
                visited=int(self.counters.get("posts_exported", 0)),
                queued=0,
                failed=int(self.counters.get("pagination_failures", 0)),
                bytes_written=self.warc.total_bytes,
                current_url=self.current_url,
                details=self._progress_details(),
            )
        except Exception as exc:
            log.debug("Facebook progress report failed: %s", exc)

    # -- lifecycle --------------------------------------------------------
    def _ensure_facebook_widgets(self) -> None:
        if not self._context:
            return
        payload = self._widget_state()
        for page in list(self._context.pages):
            try:
                page.evaluate(_FACEBOOK_WIDGET_JS)
                page.evaluate(
                    "value => window.__swmSetFacebookState && "
                    "window.__swmSetFacebookState(value)", payload)
            except Exception:
                pass
        self._state_dirty = False

    def run(self) -> dict:
        if self.browser_cfg.mode not in ("headed", "native"):
            raise ValueError("Facebook capture requires a visible browser.")
        self.browser_cfg.viewport = None
        result: dict = {}
        try:
            with FacebookBrowserDriver(
                    self.browser_cfg, BehaviorConfig()) as driver:
                result = self.run_with_context(driver.context)
            return result
        except Exception as exc:
            self.failure = self.failure or str(exc)
            if not self.stop_reason:
                self.stop_reason = "capture_failed"
                self.stop_rule = "unhandled_runtime_error"
            raise
        finally:
            self.state = STOPPED
            self._flush_persist_batch()
            try:
                self.archive.write_exports()
                self.archive.checkpoint(
                    self._checkpoint_document(),
                    self._manifest_document(final=True),
                )
                self._build_reader_pages()
            finally:
                self.warc.close()

    def _build_reader_pages(self) -> None:
        """Render the captured records as browsable pages.

        Built at the end of every capture, because the records are what a
        Facebook capture can actually show: replay reaches only the page as
        first loaded, and a capture written without a WARC has no replay at
        all.
        """
        try:
            from .facebook_render import build_site
            build_site(self.output_dir)
            self.archive.event("reader_pages_built",
                               posts=len(self.archive.posts),
                               comments=len(self.archive.comments))
        except Exception as exc:
            self.counters["reader_pages_failures"] += 1
            self.archive.event("reader_pages_failed", error=str(exc))
            log.warning("Could not build Facebook reader pages: %s", exc)

    def run_with_context(self, context, navigate: bool = True) -> dict:
        self._context = context
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_request_finished)
        context.on("requestfailed", self._on_request_failed)
        context.on("page", self._on_page)
        context.on("close", self._mark_closed)
        try:
            context.expose_binding(
                "swmFacebookControl", self._on_widget_command)
            context.add_init_script(_FACEBOOK_WIDGET_JS)
        except Exception as exc:
            log.warning("Facebook in-browser controls unavailable: %s", exc)
        for existing in context.pages:
            self._on_page(existing)
        page = context.pages[0] if context.pages else context.new_page()
        if navigate:
            try:
                page.goto(self.config.page_url, wait_until="load",
                          timeout=int(self.page_timeout * 1000))
            except Exception as exc:
                log.warning("Initial Facebook navigation failed: %s", exc)
                self.archive.event("initial_navigation_failed", error=str(exc))

        self.archive.event(
            "browser_ready",
            instruction=(
                "Log in if required, ensure the requested Page is open, then "
                "select Start / resume scrolling."
            ),
        )
        self._checkpoint(force=True)
        last_report = 0.0
        while self.state != STOPPED and not self._closed:
            try:
                command = self.control_poll()
                if command:
                    self.apply(command, actor="dashboard")
                while self._commands:
                    widget_command, source_page, actor = self._commands.popleft()
                    self.apply(widget_command, source_page, actor=actor)
                if self._pending_block_reason:
                    self._enter_blocked(self._pending_block_reason)
                    self._pending_block_reason = None
                if self._pending_stop:
                    self.stop_reason, self.stop_rule = self._pending_stop
                    self.archive.event(
                        "stopping_rule_fired", reason=self.stop_reason,
                        rule=self.stop_rule,
                    )
                    # Scrolling is over, but the curator asked for comments and
                    # media; deliver those before the session closes. Both
                    # honour a further stop command, so this stays interruptible.
                    if self.stop_reason not in ("unsupported_personal_profile",
                                                "browser_closed"):
                        self._drain_requested_work(context)
                    self.state = STOPPED
                    self.phase_detail = "Capture stopped; finalising files."
                    continue

                active = self._active_page(context)
                now = time.monotonic()
                if active is not None:
                    verification = self._detect_verification(active)
                    if verification and self.state == RECORDING:
                        self._enter_blocked(verification)
                    if self.state == RECORDING and now >= self._next_scroll_at:
                        self._scroll_once(active)
                    elif self.state in (PAUSED, BLOCKED) \
                            and now - self._last_dom_check >= 2.0:
                        self._check_manual_activity(active)
                        self._collect_dom_posts(active)
                        self._maybe_auto_start(active)
                        self._last_dom_check = now

                if self._state_dirty:
                    self._ensure_facebook_widgets()
                if now - last_report >= 1.0:
                    self._report_facebook()
                    self._ensure_facebook_widgets()
                    self._checkpoint()
                    last_report = now
                if not context.pages:
                    self.stop_reason = "browser_closed"
                    self.stop_rule = "all_browser_tabs_closed"
                    break
                self._pump(context)
            except Exception as exc:
                if self._closed or _is_closed_error(exc):
                    self.stop_reason = self.stop_reason or "browser_closed"
                    self.stop_rule = self.stop_rule or "browser_context_closed"
                    break
                raise

        if not self.stop_reason:
            self.stop_reason = "browser_closed" if self._closed else "stopped"
            self.stop_rule = self.stop_rule or "session_loop_ended"
        self.state = STOPPED
        self.phase_detail = self._closing_summary()
        self._report_facebook()
        self._checkpoint(force=True)
        if self.failure:
            raise ValueError(self.failure)
        return {
            "visited": int(self.counters.get("posts_exported", 0)),
            "bytes": self.warc.total_bytes,
            "current_url": self.current_url,
            "stop_reason": self.stop_reason,
            "detail": self.phase_detail,
        }
