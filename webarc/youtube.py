"""YouTube capture for SWM.

A channel, a video or a playlist is captured as a preservation package, in
the same order of primacy as the Instagram and X modes:

1. the media files themselves -- each video at the best rendition the
   curator allowed, its thumbnail, its captions and, for a past stream,
   its live-chat replay -- with SHA-256 fixity; images attached to Posts;
2. the evidence each record was read from: yt-dlp's own output for videos
   (the tool's reading of YouTube, kept in full and labelled as such) and
   YouTube's untouched responses for Posts and their comments;
3. normalised channels, videos, posts, playlists and comments as
   JSONL/CSV, one comment model for videos and posts alike;
4. optionally a WARC of the browser's exchanges while collecting Posts;
5. a manifest stating what was asked for, what was reached, what failed,
   what was not attempted, and what a replay can and cannot show.

Two acquisition paths sit behind one client protocol. yt-dlp lists a
channel's tabs and a playlist, reads a video's metadata and comments and
downloads its files; it is the tool the field maintains against YouTube.
A browser on a dedicated profile, driven the way the Instagram and X
collectors drive theirs, reads the Posts tab and its comments, which
yt-dlp does not cover: YouTube's own client makes the requests, SWM
observes the answers. Which path serves which method is a composition the
worker makes, not a choice the curator sees.

YouTube's checks land on the network and the account: a "confirm you're
not a bot" answer holds the run for the curator to sign in, the session
is lent to yt-dlp as a temporary cookie file that never enters the
package, and the walk resumes. Nothing here ever likes, votes, comments
or subscribes.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Protocol
from urllib.parse import parse_qs, urlsplit

from .facebook import (_append_jsonl, _atomic_json, _date_bound, _iso_now,
                       _media_suffix)
from .redaction import redact_body

log = logging.getLogger(__name__)

RECORDING = "recording"
PAUSED = "paused"
BLOCKED = "blocked"
STOPPED = "stopped"

CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_STOP = "stop"

YOUTUBE_MODES = {"date_range", "latest_n", "until_stopped", "end_of_listing", "since_last"}
YOUTUBE_SURFACES = ("videos", "shorts", "streams", "posts")
RESOLUTIONS = ("best", "2160", "1440", "1080", "720", "480", "360", "none")
COMMENT_SORTS = ("new", "top")

# yt-dlp's own availability vocabulary is kept verbatim; the two SWM adds
# are what a playlist entry or an extraction error can say about a video
# that is not served at all
AVAILABILITY = ("public", "unlisted", "private", "premium_only", "subscriber_only",
                "needs_auth", "deleted", "unavailable", "unknown")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
             "music.youtube.com"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_PLAYLIST_ID_RE = re.compile(r"^(?:PL|UU|OL|LL|RD|FL)[A-Za-z0-9_-]{10,}$")
_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._-]{3,30}$")
_TAB_SEGMENTS = {"videos", "shorts", "streams", "posts", "community", "featured",
                 "playlists", "about", "live", "podcasts", "releases", "membership"}


@dataclass(frozen=True)
class YouTubeTarget:
    """What a curator asked for, once the text has been read."""
    kind: str                       # "channel" | "video" | "playlist"
    handle: Optional[str] = None    # without the @
    channel_id: Optional[str] = None
    video_id: Optional[str] = None
    playlist_id: Optional[str] = None
    url: str = ""

    @property
    def key(self) -> str:
        if self.kind == "channel":
            return (f"youtube:@{self.handle.lower()}" if self.handle
                    else f"youtube:channel/{self.channel_id}")
        if self.kind == "video":
            return f"youtube:video/{self.video_id}"
        return f"youtube:playlist/{self.playlist_id}"

    @property
    def label(self) -> str:
        if self.kind == "channel":
            return f"@{self.handle}" if self.handle else f"channel {self.channel_id}"
        if self.kind == "video":
            return f"video {self.video_id}"
        return f"playlist {self.playlist_id}"


def parse_youtube_target(raw: object) -> YouTubeTarget:
    """Read a handle, a channel, video or playlist address, or a bare id.

    Accepted: ``@handle``; ``youtube.com/@handle`` with or without a tab
    (``/videos``, ``/shorts``, ``/streams``, ``/posts``, ``/community``);
    ``youtube.com/channel/UC…``; ``watch?v=``, ``youtu.be/``, ``/shorts/``
    and ``/live/`` addresses; ``playlist?list=``; and bare video, channel
    and playlist ids. The signed-in client's own pages (home, feed,
    subscriptions, history) are refused.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Enter a YouTube channel, video or playlist.")
    if "://" not in text and "/" not in text and "?" not in text:
        if _CHANNEL_ID_RE.match(text):
            return YouTubeTarget("channel", channel_id=text,
                                 url=f"https://www.youtube.com/channel/{text}")
        if _PLAYLIST_ID_RE.match(text):
            return YouTubeTarget("playlist", playlist_id=text,
                                 url=f"https://www.youtube.com/playlist?list={text}")
        if text.startswith("@") and _HANDLE_RE.match(text):
            handle = text[1:]
            return YouTubeTarget("channel", handle=handle,
                                 url=f"https://www.youtube.com/@{handle}")
        if _VIDEO_ID_RE.match(text):
            return YouTubeTarget("video", video_id=text,
                                 url=f"https://www.youtube.com/watch?v={text}")
        if _HANDLE_RE.match(text):
            return YouTubeTarget("channel", handle=text,
                                 url=f"https://www.youtube.com/@{text}")
        raise ValueError(f"{text!r} is not a YouTube handle, channel, video or playlist id.")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host not in _YT_HOSTS:
        raise ValueError(f"{text} is not a YouTube address.")
    params = parse_qs(parts.query)
    segments = [s for s in (parts.path or "/").split("/") if s]
    if host == "youtu.be":
        if segments and _VIDEO_ID_RE.match(segments[0]):
            return YouTubeTarget("video", video_id=segments[0],
                                 url=f"https://www.youtube.com/watch?v={segments[0]}")
        raise ValueError(f"{text} does not name a video.")
    if segments[:1] == ["watch"] or (not segments and params.get("v")):
        video_id = (params.get("v") or [""])[0]
        if not _VIDEO_ID_RE.match(video_id):
            raise ValueError(f"{text} does not name a video (v=).")
        return YouTubeTarget("video", video_id=video_id,
                             url=f"https://www.youtube.com/watch?v={video_id}")
    if segments[:1] == ["playlist"]:
        playlist_id = (params.get("list") or [""])[0]
        if not playlist_id:
            raise ValueError(f"{text} does not name a playlist (list=).")
        return YouTubeTarget("playlist", playlist_id=playlist_id,
                             url=f"https://www.youtube.com/playlist?list={playlist_id}")
    if segments[:1] in (["shorts"], ["live"], ["embed"], ["v"]) and len(segments) >= 2:
        video_id = segments[1]
        if not _VIDEO_ID_RE.match(video_id):
            raise ValueError(f"{video_id!r} is not a video id.")
        return YouTubeTarget("video", video_id=video_id,
                             url=f"https://www.youtube.com/watch?v={video_id}")
    if not segments:
        raise ValueError("Enter a channel, video or playlist address, not YouTube's home page.")
    head = segments[0]
    if head.startswith("@"):
        handle = head[1:]
        if not _HANDLE_RE.match(handle):
            raise ValueError(f"{head!r} is not a YouTube handle.")
        return YouTubeTarget("channel", handle=handle, url=f"https://www.youtube.com/@{handle}")
    if head == "channel" and len(segments) >= 2:
        if not _CHANNEL_ID_RE.match(segments[1]):
            raise ValueError(f"{segments[1]!r} is not a channel id.")
        return YouTubeTarget("channel", channel_id=segments[1],
                             url=f"https://www.youtube.com/channel/{segments[1]}")
    if head in ("c", "user") and len(segments) >= 2:
        # legacy custom and user names: yt-dlp resolves them; the key uses
        # the name until the channel id is read
        return YouTubeTarget("channel", handle=segments[1],
                             url=f"https://www.youtube.com/{head}/{segments[1]}")
    if head in ("feed", "results", "account", "signin", "premium", "gaming", "music",
                "reporthistory", "upload", "logout"):
        raise ValueError(f"/{head}/ is one of YouTube's own pages -- the viewer's feed, "
                         "search or account -- not a channel, video or playlist.")
    if head in _TAB_SEGMENTS:
        raise ValueError(f"{text} names a tab without a channel.")
    raise ValueError(f"{text} is not a channel, video or playlist address.")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class YouTubeChannel:
    channel_id: str
    handle: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    subscriber_count: Optional[int] = None
    video_count: Optional[int] = None
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    external_links: list[dict] = field(default_factory=list)
    source: str = "yt-dlp"
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class YouTubeVideo:
    video_id: str
    channel_id: Optional[str] = None
    channel_handle: Optional[str] = None
    channel_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    published_time: Optional[str] = None
    duration_seconds: Optional[int] = None
    kind: str = "video"                 # video | short | stream
    live_status: Optional[str] = None
    availability: str = "unknown"
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    # what was written under media/videos/<id>/: the video file, thumbnail,
    # captions, live chat, each with the rendition and fixity
    files: list[dict] = field(default_factory=list)
    surface: str = "videos"
    source: str = "yt-dlp"
    complete: bool = False              # read whole, not a listing's flat entry
    unavailable_reason: Optional[str] = None
    comment_capture: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class YouTubePost:
    post_id: str
    channel_id: Optional[str] = None
    channel_handle: Optional[str] = None
    author_name: Optional[str] = None
    kind: str = "text"                  # text | image | images | poll | quiz | video | shared
    text: Optional[str] = None
    published_text: Optional[str] = None    # YouTube shows "2 days ago", nothing better
    published_time: Optional[str] = None    # an estimate from the relative text, marked so
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    url: Optional[str] = None
    images: list[dict] = field(default_factory=list)   # {url, width, height, file?}
    poll: Optional[dict] = None
    attached_video_id: Optional[str] = None
    shared_post_id: Optional[str] = None
    source: str = "browser"
    comment_capture: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class YouTubeComment:
    """One comment model for videos and posts alike."""
    comment_id: str
    target_type: str                    # video | post
    target_id: str
    parent_id: Optional[str] = None
    thread_root_id: Optional[str] = None
    reply_depth: int = 0
    author_name: Optional[str] = None
    author_channel_id: Optional[str] = None
    author_is_uploader: bool = False
    text: Optional[str] = None
    published_time: Optional[str] = None
    published_text: Optional[str] = None
    like_count: Optional[int] = None
    is_pinned: bool = False
    is_favorited: bool = False
    source: str = "yt-dlp"
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass
class YouTubePlaylist:
    playlist_id: str
    title: Optional[str] = None
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    description: Optional[str] = None
    item_count: Optional[int] = None
    url: Optional[str] = None
    source: str = "yt-dlp"
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The client the engine talks to
# ---------------------------------------------------------------------------

class YouTubeError(Exception):
    """Base for the conditions the engine has a response to."""


class RateLimited(YouTubeError):
    def __init__(self, wait_seconds: float = 300.0, detail: str = ""):
        super().__init__(detail or "YouTube is limiting requests.")
        self.wait_seconds = wait_seconds


class LoginRequired(YouTubeError):
    """A sign-in, or YouTube's "confirm you're not a bot" answer, which a
    signed-in session clears."""


class CheckpointRequired(YouTubeError):
    """YouTube wants a person: a consent page, a verification."""


class TargetUnavailable(YouTubeError):
    """Not found, private, members-only without membership, removed."""

    def __init__(self, message: str, availability: str = "unavailable"):
        super().__init__(message)
        self.availability = availability if availability in AVAILABILITY else "unavailable"


class DownloadInterrupted(YouTubeError):
    """The engine asked the downloader to stop mid-file; the partial file
    is kept and the download resumes later."""


class YouTubeClient(Protocol):
    """What the engine needs from YouTube, so a test can stand it in.

    ``list_items`` hands over videos as the tab or playlist lists them,
    newest first for a channel tab, in order for a playlist, with as much
    as the listing says; ``video`` reads a video in full. ``download``
    writes the video's files under ``dest`` and returns one entry per
    file. ``comments`` and ``post_comments`` yield the one comment model.
    """

    def channel(self, target: YouTubeTarget) -> YouTubeChannel: ...
    def list_items(self, target: YouTubeTarget, surface: str) -> Iterator[YouTubeVideo]: ...
    def playlist(self, target: YouTubeTarget) -> YouTubePlaylist: ...
    def video(self, video_id: str) -> YouTubeVideo: ...
    def download(self, video: YouTubeVideo, dest: Path,
                 on_progress: Callable[[dict], None]) -> list[dict]: ...
    def comments(self, video: YouTubeVideo) -> Iterator[YouTubeComment]: ...
    def posts(self, channel: YouTubeChannel) -> Iterator[YouTubePost]: ...
    def post_comments(self, post: YouTubePost) -> Iterator[YouTubeComment]: ...
    def fetch(self, url: str) -> tuple[bytes, str]: ...


NO_YTDLP = ("yt-dlp is not installed in the Python that runs SWM, so videos, Shorts, "
            "live streams and playlists cannot be listed, read or downloaded. Install it "
            "into that Python with: pip install yt-dlp (or reinstall SWM with the "
            "installer, which now includes it), then start the capture again.")
NO_BROWSER = ("No browser window is available on this server, so the Posts tab cannot "
              "be read.")


class ComposedClient:
    """One client for the engine, made of two: yt-dlp for the channel's
    tabs, playlists, videos and their comments; a browser for the Posts
    tab and its comments. Either may be absent; a missing browser means
    the Posts surface is reported as not attempted, a missing yt-dlp
    means the same for videos."""

    version = "composed"

    def __init__(self, videos=None, posts=None):
        self.videos = videos
        self.posts_client = posts
        self._last_comments_from = None

    @property
    def anomalies(self) -> list[dict]:
        found: list[dict] = []
        for part in (self.videos, self.posts_client):
            part_anomalies = getattr(part, "anomalies", None)
            if isinstance(part_anomalies, list):
                found.extend(part_anomalies)
        return found

    def comments_more(self) -> Optional[bool]:
        """Whether the client that last served comments had more to offer."""
        more = getattr(self._last_comments_from, "comments_more", None)
        return more() if callable(more) else None

    def _need(self, which: str):
        client = self.videos if which == "videos" else self.posts_client
        if client is None:
            raise TargetUnavailable(NO_YTDLP if which == "videos" else NO_BROWSER, "unknown")
        return client

    def channel(self, target):
        try:
            return self._need("videos").channel(target)
        except TargetUnavailable:
            if self.posts_client is None:
                raise
            return self.posts_client.channel(target)

    def list_items(self, target, surface):
        return self._need("videos").list_items(target, surface)

    def playlist(self, target):
        return self._need("videos").playlist(target)

    def video(self, video_id):
        return self._need("videos").video(video_id)

    def download(self, video, dest, on_progress):
        return self._need("videos").download(video, dest, on_progress)

    def comments(self, video):
        self._last_comments_from = self._need("videos")
        return self._last_comments_from.comments(video)

    def posts(self, channel):
        return self._need("posts").posts(channel)

    def post_comments(self, post):
        self._last_comments_from = self._need("posts")
        return self._last_comments_from.post_comments(post)

    def fetch(self, url):
        client = self.posts_client or self.videos
        return client.fetch(url)

    # the browser's session is lent to the downloader, never the other way
    def use_cookies(self, cookies) -> bool:
        use = getattr(self.videos, "use_cookies", None)
        if not callable(use):
            return False
        use(cookies)
        return True

    def forget_cookies(self) -> None:
        forget = getattr(self.videos, "forget_cookies", None)
        if callable(forget):
            forget()

    @property
    def versions(self) -> dict:
        return {"videos": getattr(self.videos, "version", None),
                "posts": getattr(self.posts_client, "version", None)}

    def __getattr__(self, name):
        # the engine's optional hooks (record_responses_to, cookie_jar,
        # use_cookies, show, refresh ...) go to whichever part has them
        for part in (self.posts_client, self.videos):
            if part is not None and hasattr(part, name):
                return getattr(part, name)
        raise AttributeError(name)


class _ResumableListing:
    """A listing that opens itself again after a rate limit.

    An iterator that raised is finished, so waiting and calling ``next``
    again would end the listing quietly. This one re-opens the listing,
    skips the entries already taken and carries on. ``on_rate_limit``
    waits the limit out and raises when the curator stopped the run.
    """

    def __init__(self, open_listing: Callable[[], Iterator], on_rate_limit: Callable[[RateLimited], None],
                 first: Optional[Iterator] = None):
        self._open = open_listing
        self._on_rate_limit = on_rate_limit
        self._listing = first
        self.taken = 0
        self.reopened = 0

    def __iter__(self):
        return self

    def __next__(self):
        for _attempt in range(6):
            try:
                if self._listing is None:
                    self._listing = self._open()
                    self.reopened += 1
                    for _ in range(self.taken):
                        next(self._listing)
                item = next(self._listing)
            except RateLimited as exc:
                self.close()
                self._listing = None
                self._on_rate_limit(exc)
                continue
            self.taken += 1
            return item
        raise TargetUnavailable("The listing did not resume after repeated rate limits.")

    def close(self) -> None:
        close = getattr(self._listing, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _whole(value: object, default: int, label: str, low: int, high: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low:,} and {high:,}.")
    return number


@dataclass
class YouTubeCaptureConfig:
    targets: list[str]
    mode: str = "latest_n"
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    latest_n: int = 50
    consecutive_older: int = 5
    surfaces: tuple[str, ...] = ("videos", "shorts", "streams", "posts")
    capture_media: bool = True
    max_resolution: str = "1080"
    thumbnails: bool = True
    captions: bool = True
    auto_captions: bool = True
    live_chat: bool = True
    post_media: bool = True
    include_comments: bool = True
    max_comments_per_item: int = 1000
    include_replies: bool = True
    comment_sort: str = "new"
    write_warc: bool = False
    end_stall_rounds: int = 3
    prior_newest: dict = field(default_factory=dict)
    continuation_of: Optional[int] = None
    operator: str = "webarc"
    browser_profile_dir: Optional[str] = None
    browser_mode: str = "headed"
    chrome_path: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "YouTubeCaptureConfig":
        targets_raw = raw.get("targets")
        if isinstance(targets_raw, str):
            targets_raw = [line for line in re.split(r"[\n,]+", targets_raw) if line.strip()]
        if not isinstance(targets_raw, list) or not targets_raw:
            raise ValueError("Add at least one YouTube channel, video or playlist.")
        targets: list[str] = []
        for item in targets_raw:
            target = parse_youtube_target(item)
            if target.url not in targets:
                targets.append(target.url)
        mode = str(raw.get("mode") or "latest_n")
        if mode not in YOUTUBE_MODES:
            raise ValueError(f"Unsupported YouTube capture mode: {mode}")
        browser_mode = str((raw.get("browser") or {}).get("mode")
                           or raw.get("browser_mode") or "headed")
        if browser_mode not in ("headed", "native"):
            raise ValueError("browser must be 'headed' or 'native'")
        from_date = _date_bound(raw.get("from_date"))
        to_date = _date_bound(raw.get("to_date"), end=True)
        if from_date and to_date and from_date > to_date:
            raise ValueError("The From date must be on or before the To date.")
        if mode == "date_range" and not from_date:
            raise ValueError("Date range mode requires a From date.")
        surfaces_raw = raw.get("surfaces") or list(YOUTUBE_SURFACES)
        if isinstance(surfaces_raw, str):
            surfaces_raw = [surfaces_raw]
        surfaces = tuple(s for s in YOUTUBE_SURFACES if s in surfaces_raw)
        if not surfaces:
            raise ValueError("Select at least one of Videos, Shorts, Live streams and Posts.")
        resolution = str(raw.get("max_resolution") or "1080")
        if resolution not in RESOLUTIONS:
            raise ValueError("max_resolution must be one of " + ", ".join(RESOLUTIONS))
        sort = str(raw.get("comment_sort") or "new")
        if sort not in COMMENT_SORTS:
            raise ValueError("comment_sort must be 'new' or 'top'")
        prior = raw.get("prior_newest") or {}
        if mode == "since_last":
            missing = [u for u in targets
                       if parse_youtube_target(u).kind == "channel"
                       and not (prior.get(parse_youtube_target(u).key) or {}).get("item_id")]
            if missing:
                raise ValueError(
                    "No previous capture state exists for "
                    + ", ".join(parse_youtube_target(u).label for u in missing)
                    + ". Run another capture mode first.")
        return cls(
            targets=targets, mode=mode, from_date=from_date, to_date=to_date,
            latest_n=_whole(raw.get("latest_n"), 50, "Latest N", 1, 100_000),
            consecutive_older=_whole(raw.get("consecutive_older"), 5,
                                     "Consecutive older items", 2, 25),
            surfaces=surfaces,
            capture_media=bool(raw.get("capture_media", True)) and resolution != "none",
            max_resolution=resolution,
            thumbnails=bool(raw.get("thumbnails", True)),
            captions=bool(raw.get("captions", True)),
            auto_captions=bool(raw.get("auto_captions", True)),
            live_chat=bool(raw.get("live_chat", True)),
            post_media=bool(raw.get("post_media", True)),
            include_comments=bool(raw.get("include_comments", True)),
            max_comments_per_item=_whole(raw.get("max_comments_per_item"), 1000,
                                         "Maximum comments per item", 1, 1_000_000),
            include_replies=bool(raw.get("include_replies", True)),
            comment_sort=sort,
            write_warc=bool(raw.get("write_warc", False)),
            end_stall_rounds=max(1, _whole(raw.get("end_stall_rounds"), 3, "Stall rounds", 1, 20)),
            prior_newest=dict(prior),
            continuation_of=raw.get("continuation_of"),
            operator=str(raw.get("operator") or "webarc"),
            browser_profile_dir=(raw.get("browser_profile_dir")
                                 or (raw.get("browser") or {}).get("user_data_dir")),
            browser_mode=browser_mode,
            chrome_path=(raw.get("browser") or {}).get("chrome_path"),
        )

    @property
    def format_selector(self) -> str:
        """yt-dlp's format expression for the resolution the curator allowed."""
        if self.max_resolution == "best":
            return "bestvideo*+bestaudio/best"
        height = self.max_resolution
        return (f"bestvideo*[height<={height}]+bestaudio/best[height<={height}]"
                f"/bestvideo*+bestaudio/best")


# ---------------------------------------------------------------------------
# Archive: everything written to disk
# ---------------------------------------------------------------------------

class YouTubeArchive:
    """The capture package on disk, written incrementally.

    Principal records sit at the package root under the ``youtube-`` names
    the dashboard and replay endpoint look for. ``media/videos/<id>/`` and
    ``media/posts/<id>/`` hold the files, ``evidence/yt-dlp/`` the tool's
    output per video, ``raw/responses/`` YouTube's responses observed by
    the browser. Exports are appended as records arrive.
    """

    VIDEO_FIELDS = [
        "video_id", "channel_id", "channel_handle", "channel_name", "title",
        "published_time", "duration_seconds", "kind", "live_status", "availability",
        "view_count", "like_count", "comment_count", "url", "media_file",
        "media_resolution", "surface", "source", "unavailable_reason", "target_key",
    ]
    POST_FIELDS = [
        "post_id", "channel_id", "channel_handle", "author_name", "kind", "text",
        "published_text", "published_time", "like_count", "comment_count", "url",
        "image_files", "attached_video_id", "source", "target_key",
    ]
    COMMENT_FIELDS = [
        "comment_id", "target_type", "target_id", "parent_id", "thread_root_id",
        "reply_depth", "author_name", "author_channel_id", "author_is_uploader",
        "text", "published_time", "published_text", "like_count", "is_pinned", "source",
    ]

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.out_dir / "raw"
        self.responses_dir = self.raw_dir / "responses"
        self.evidence_dir = self.out_dir / "evidence" / "yt-dlp"
        self.media_dir = self.out_dir / "media"
        self.videos_path = self.out_dir / "youtube-videos.jsonl"
        self.posts_path = self.out_dir / "youtube-posts.jsonl"
        self.comments_path = self.out_dir / "youtube-comments.jsonl"
        self.channels_path = self.out_dir / "youtube-channels.json"
        self.playlists_path = self.out_dir / "youtube-playlists.jsonl"
        self.playlist_items_path = self.out_dir / "youtube-playlist-items.jsonl"
        self.media_path = self.out_dir / "youtube-media.json"
        self.events_path = self.out_dir / "youtube-events.jsonl"
        self.manifest_path = self.out_dir / "youtube-manifest.json"
        self.checkpoint_path = self.out_dir / "youtube-checkpoint.json"
        self.checksums_path = self.out_dir / "checksums.sha256"
        self.videos: dict[str, YouTubeVideo | dict] = {}
        self.posts: dict[str, YouTubePost | dict] = {}
        self.comments: dict[str, YouTubeComment | dict] = {}
        self.channels: dict[str, YouTubeChannel] = {}
        self.playlists: dict[str, YouTubePlaylist] = {}
        self.media_index: dict[str, dict] = {}    # package path -> entry
        self._checksums: dict[str, str] = {}
        self.responses_saved = 0
        self._response_serial = self._next_response_serial()
        self._load_existing()

    def _next_response_serial(self) -> int:
        highest = 0
        if self.responses_dir.is_dir():
            for existing in self.responses_dir.glob("response-*.json"):
                digits = existing.stem.rsplit("-", 1)[-1]
                if digits.isdigit():
                    highest = max(highest, int(digits))
        return highest + 1

    def _load_existing(self) -> None:
        if self.media_path.exists():
            try:
                loaded = json.loads(self.media_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.media_index = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, json.JSONDecodeError):
                pass
        for path, key, into in ((self.videos_path, "video_id", self.videos),
                                (self.posts_path, "post_id", self.posts),
                                (self.comments_path, "comment_id", self.comments)):
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get(key):
                        into[str(row[key])] = row   # type: ignore[assignment]

    # -- events -------------------------------------------------------------
    def event(self, event: str, **details: object) -> None:
        _append_jsonl(self.events_path, {"time": _iso_now(), "event": event, **details})

    # -- evidence -----------------------------------------------------------
    def save_evidence(self, name: str, payload: object) -> str:
        """yt-dlp's reading of a video, kept whole and labelled as the tool's."""
        target = self.evidence_dir / f"{_safe_name(name)}.info.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(target, payload)
        self._checksum(target)
        return f"evidence/yt-dlp/{target.name}"

    def save_response(self, meta: dict, body: bytes) -> str:
        """A response YouTube gave the browser, verbatim, session material removed."""
        name = f"response-{self._response_serial:06d}.json"
        self._response_serial += 1
        target = self.responses_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_body, redacted = redact_body(body, str(meta.get("content_type") or ""))
        _atomic_json(target, {
            **meta, "body_bytes": len(safe_body),
            "body_sha256": hashlib.sha256(safe_body).hexdigest(),
            "redacted_fields": redacted,
            "body": safe_body.decode("utf-8", errors="replace")})
        self._checksum(target)
        self.responses_saved += 1
        return f"raw/responses/{name}"

    # -- records ------------------------------------------------------------
    def add_channel(self, channel: YouTubeChannel) -> None:
        self.channels[channel.channel_id] = channel
        _atomic_json(self.channels_path, {
            cid: _without_raw(asdict(c)) for cid, c in self.channels.items()})
        if channel.raw:
            target = self.raw_dir / "channels" / f"{_safe_name(channel.channel_id)}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(target, channel.raw)
            self._checksum(target)

    def add_playlist(self, playlist: YouTubePlaylist, items: list[dict]) -> None:
        self.playlists[playlist.playlist_id] = playlist
        _append_jsonl(self.playlists_path, _without_raw(asdict(playlist)))
        for item in items:
            _append_jsonl(self.playlist_items_path, {"playlist_id": playlist.playlist_id, **item})

    def add_video(self, video: YouTubeVideo) -> bool:
        if video.video_id in self.videos:
            return False
        self.videos[video.video_id] = video
        _append_jsonl(self.videos_path, self._video_row(video))
        return True

    def update_video(self, video: YouTubeVideo) -> None:
        self.videos[video.video_id] = video

    def add_post(self, post: YouTubePost) -> bool:
        if post.post_id in self.posts:
            return False
        self.posts[post.post_id] = post
        _append_jsonl(self.posts_path, self._post_row(post))
        if post.raw:
            target = self.raw_dir / "posts" / f"{_safe_name(post.post_id)}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(target, post.raw)
            self._checksum(target)
        return True

    def add_comment(self, comment: YouTubeComment) -> bool:
        if comment.comment_id in self.comments:
            return False
        self.comments[comment.comment_id] = comment
        _append_jsonl(self.comments_path, _without_raw(asdict(comment)))
        return True

    def _video_row(self, video: "YouTubeVideo | dict") -> dict:
        if isinstance(video, dict):
            return dict(video)
        row = _without_raw(asdict(video))
        main = next((f for f in video.files if f.get("role") == "video"), None)
        row["media_file"] = main.get("file") if main else None
        row["media_resolution"] = main.get("resolution") if main else None
        row.setdefault("target_key", video.provenance.get("target_key"))
        return row

    def _post_row(self, post: "YouTubePost | dict") -> dict:
        if isinstance(post, dict):
            return dict(post)
        row = _without_raw(asdict(post))
        row["image_files"] = [i.get("file") for i in post.images]
        row.setdefault("target_key", post.provenance.get("target_key"))
        return row

    # -- media --------------------------------------------------------------
    def video_dir(self, video_id: str) -> Path:
        return self.media_dir / "videos" / _safe_name(video_id)

    def post_dir(self, post_id: str) -> Path:
        return self.media_dir / "posts" / _safe_name(post_id)

    def register_file(self, path: Path, entry: dict) -> dict:
        """Index a file a client wrote under media/, with its digest."""
        rel = str(path.relative_to(self.out_dir)).replace("\\", "/")
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.stat().st_size < 2**31 \
            else _sha256_of(path)
        record = {**entry, "file": rel, "sha256": digest, "bytes": path.stat().st_size}
        self.media_index[rel] = record
        self._checksums[rel] = digest
        return record

    def save_bytes(self, folder: Path, name: str, body: bytes, entry: dict) -> dict:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        temporary = target.with_name(name + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(target)
        return self.register_file(target, entry)

    # -- fixity -------------------------------------------------------------
    def _checksum(self, path: Path) -> None:
        try:
            self._checksums[str(path.relative_to(self.out_dir)).replace("\\", "/")] = \
                _sha256_of(path)
        except OSError:
            return

    def _referenced_responses(self) -> set[str]:
        refs: set[str] = set()
        for records in (self.posts.values(), self.comments.values(), self.channels.values()):
            for record in records:
                origin = (record.get("provenance") if isinstance(record, dict)
                          else getattr(record, "provenance", None)) or {}
                if isinstance(origin, dict) and origin.get("response"):
                    refs.add(str(origin["response"]))
        for entry in self.media_index.values():
            if entry.get("discovered_from"):
                refs.add(str(entry["discovered_from"]))
        return refs

    def prune_unreferenced_responses(self) -> int:
        if not self.responses_dir.is_dir():
            return 0
        keep = self._referenced_responses()
        removed = 0
        for saved in sorted(self.responses_dir.glob("response-*.json")):
            ref = f"raw/responses/{saved.name}"
            if ref in keep:
                continue
            try:
                saved.unlink()
            except OSError:
                continue
            self._checksums.pop(ref, None)
            removed += 1
        self.responses_saved = len(list(self.responses_dir.glob("response-*.json")))
        return removed

    def finalise(self, manifest: dict, checkpoint: dict) -> None:
        _atomic_json(self.media_path, self.media_index)
        for path, rows in ((self.videos_path, (self._video_row(v) for v in self.videos.values())),
                           (self.posts_path, (self._post_row(p) for p in self.posts.values()))):
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            temporary.replace(path)
        self._write_csv(self.out_dir / "youtube-videos.csv", self.VIDEO_FIELDS,
                        (self._video_row(v) for v in self.videos.values()))
        self._write_csv(self.out_dir / "youtube-posts.csv", self.POST_FIELDS,
                        (self._post_row(p) for p in self.posts.values()))
        self._write_csv(self.out_dir / "youtube-comments.csv", self.COMMENT_FIELDS,
                        (_without_raw(asdict(c)) if not isinstance(c, dict) else c
                         for c in self.comments.values()))
        _atomic_json(self.checkpoint_path, checkpoint)
        _atomic_json(self.manifest_path, manifest)
        for name in ("youtube-videos.jsonl", "youtube-videos.csv", "youtube-posts.jsonl",
                     "youtube-posts.csv", "youtube-comments.jsonl", "youtube-comments.csv",
                     "youtube-channels.json", "youtube-playlists.jsonl",
                     "youtube-playlist-items.jsonl", "youtube-media.json",
                     "youtube-manifest.json"):
            path = self.out_dir / name
            if path.exists():
                self._checksum(path)
        lines = [f"{digest}  {name}" for name, digest in sorted(self._checksums.items())]
        temporary = self.checksums_path.with_name("checksums.sha256.tmp")
        temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        temporary.replace(self.checksums_path)

    @staticmethod
    def _write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                current = dict(row)
                for key in ("image_files",):
                    if isinstance(current.get(key), list):
                        current[key] = json.dumps(current[key], ensure_ascii=False)
                writer.writerow(current)
        temporary.replace(path)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _without_raw(row: dict) -> dict:
    row = dict(row)
    row.pop("raw", None)
    return row


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))[:120] or "item"


# ---------------------------------------------------------------------------
# The capture itself
# ---------------------------------------------------------------------------

class YouTubeCaptureSession:
    """Runs one YouTube job: several targets, one at a time.

    ``disk_check`` returns ``("ok" | "warning" | "critical", message)``; a
    warning holds the run before the next file, a critical level stops the
    downloader mid-file (the partial file is kept and resumed) and holds.
    ``session_cookies`` returns the browser's YouTube cookies after the
    curator signed in, for lending to yt-dlp; they never enter the package.
    """

    def __init__(self, *, config: YouTubeCaptureConfig, client, output_dir: Path,
                 crawl_id: int, crawl_name: str,
                 known_ids: Optional[dict[str, set[str]]] = None,
                 control_poll: Optional[Callable[[], Optional[str]]] = None,
                 on_progress: Optional[Callable[..., None]] = None,
                 persist: Optional[Callable[..., None]] = None,
                 open_browser: Optional[Callable[[str], Callable[[], None]]] = None,
                 disk_check: Optional[Callable[[], tuple[str, str]]] = None,
                 session_cookies: Optional[Callable[[], list[dict]]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.client = client
        self.archive = YouTubeArchive(output_dir)
        self.crawl_id = crawl_id
        self.crawl_name = crawl_name
        self.known_ids = known_ids or {}
        self.control_poll = control_poll or (lambda: None)
        self.on_progress = on_progress or (lambda **_kw: None)
        self.persist = persist or (lambda **_kw: None)
        self.open_browser = open_browser
        self.disk_check = disk_check or (lambda: ("ok", ""))
        self.session_cookies = session_cookies
        self.sleep = sleep

        self.state = RECORDING
        self.phase_detail = "Starting."
        self.stop_reason: Optional[str] = None
        self.stop_rule: Optional[str] = None
        self.counters: Counter = Counter()
        self.exclusions: Counter = Counter()
        self.targets = [parse_youtube_target(u) for u in config.targets]
        self.target_status: dict[str, dict] = {
            t.key: {"label": t.label, "kind": t.kind, "status": "pending",
                    "items_selected": 0, "items_encountered": 0}
            for t in self.targets}
        self.current_target: Optional[YouTubeTarget] = None
        self._stop_requested = False
        self._rate_limit_streak = 0
        self._last_report = 0.0
        self.newest_by_target: dict[str, dict] = {}
        self.absences: list[dict] = []
        self.download_state: dict = {}
        self.signed_in = False
        self._anomalies_drained = 0

    # -- control ------------------------------------------------------------
    def _check_control(self) -> None:
        command = self.control_poll()
        if command == CMD_STOP:
            self._request_stop("curator_stop", "curator_selected_stop_and_save")
            return
        if command == CMD_PAUSE:
            self.state = PAUSED
            self.phase_detail = "Paused. Nothing further is requested from YouTube until you resume."
            self.archive.event("paused", actor="dashboard")
            self._report(force=True)
            while True:
                self.sleep(1.0)
                command = self.control_poll()
                if command == CMD_STOP:
                    self._request_stop("curator_stop", "curator_selected_stop_and_save")
                    return
                if command == CMD_RESUME:
                    self.state = RECORDING
                    self.phase_detail = "Resumed."
                    self.archive.event("resumed", actor="dashboard")
                    self._report(force=True)
                    return

    def _request_stop(self, reason: str, rule: str) -> None:
        if not self._stop_requested:
            self._stop_requested = True
            self.stop_reason, self.stop_rule = reason, rule
            self.archive.event("stopping_rule_fired", reason=reason, rule=rule)

    def _hold_for_curator(self, why: str, url: str) -> bool:
        self.state = BLOCKED
        self.phase_detail = why
        self.archive.event("curator_needed", reason=why, url=url)
        self._report(force=True)
        close = None
        if self.open_browser is not None:
            try:
                close = self.open_browser(url)
            except Exception as exc:
                log.warning("Could not open the browser for the curator: %s", exc)
        try:
            while True:
                self.sleep(1.0)
                command = self.control_poll()
                if command == CMD_STOP:
                    self._request_stop("curator_stop", "curator_selected_stop_and_save")
                    return False
                if command == CMD_RESUME:
                    self.state = RECORDING
                    self.phase_detail = "Continuing."
                    self.archive.event("curator_resolved")
                    self._report(force=True)
                    return True
        finally:
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _hold_for_disk(self, level: str, message: str) -> bool:
        """Wait until the disk is above the warning level again. False on stop."""
        self.state = PAUSED
        self.counters["disk_holds"] += 1
        self.archive.event("disk_hold", level=level, detail=message)
        while True:
            self.phase_detail = (f"Holding: low disk space ({level}). {message} The run "
                                 "continues by itself once space is freed.")
            self._report(force=True)
            self.sleep(5.0)
            if self.control_poll() == CMD_STOP:
                self._request_stop("curator_stop", "curator_selected_stop_and_save")
                return False
            state, _ = self.disk_check()
            if state == "ok":
                self.state = RECORDING
                self.phase_detail = "Disk space is back above the warning level; continuing."
                self.archive.event("disk_hold_released")
                return True

    def _lend_session(self) -> bool:
        """After a sign-in, hand the browser's cookies to the video client."""
        if self.session_cookies is None:
            return False
        use = getattr(self.client, "use_cookies", None)
        if not callable(use):
            return False
        try:
            cookies = self.session_cookies()
        except Exception as exc:
            log.warning("Could not read the browser's cookies: %s", exc)
            return False
        if not cookies:
            return False
        if use(cookies) is False:
            return False
        self.signed_in = True
        self.archive.event("session_lent_to_downloader", cookies=len(cookies))
        return True

    def _wait_out_rate_limit(self, exc: RateLimited) -> bool:
        self._rate_limit_streak += 1
        wait = float(exc.wait_seconds) * (2 ** (self._rate_limit_streak - 1))
        wait = max(20.0, min(wait, 960.0))
        self.counters["rate_limit_waits"] += 1
        self.archive.event("rate_limited", wait_seconds=wait,
                           streak=self._rate_limit_streak, detail=str(exc))
        remaining = wait
        while remaining > 0:
            self.phase_detail = (f"YouTube is limiting requests. Waiting "
                                 f"{int(remaining)}s before continuing.")
            self._report(force=True)
            step = min(5.0, remaining)
            self.sleep(step)
            remaining -= step
            if self.control_poll() == CMD_STOP:
                self._request_stop("curator_stop", "curator_selected_stop_and_save")
                return False
        return True

    def _rate_limit_or_stop(self, exc: RateLimited) -> None:
        if not self._wait_out_rate_limit(exc):
            raise TargetUnavailable("stopped") from exc

    def _with_retries(self, action: Callable[[], object], url: str, what: str) -> object:
        for _attempt in range(6):
            if self._stop_requested:
                raise TargetUnavailable("stopped")
            try:
                result = action()
                self._rate_limit_streak = 0
                return result
            except RateLimited as exc:
                if not self._wait_out_rate_limit(exc):
                    raise TargetUnavailable("stopped") from exc
            except LoginRequired as exc:
                if not self._hold_for_curator(
                        f"YouTube asks for a signed-in session before {what}: {exc}. Sign in "
                        "in the browser window that has opened -- use a dedicated "
                        "institutional Google account, not a personal one -- then select "
                        "“I have resolved it — continue”. SWM lends that "
                        "session to the downloader and never writes it into the package.",
                        "https://accounts.google.com/ServiceLogin?service=youtube"
                        "&continue=https://www.youtube.com/"):
                    raise TargetUnavailable("stopped") from exc
                self._lend_session()
            except CheckpointRequired as exc:
                if not self._hold_for_curator(
                        f"YouTube is asking for something before {what}: {exc}. Resolve it "
                        "in the browser window that has opened, then select “I have "
                        "resolved it — continue”.", url):
                    raise TargetUnavailable("stopped") from exc
                self._lend_session()
        raise TargetUnavailable(f"{what} did not succeed after repeated tries.")

    # -- progress -----------------------------------------------------------
    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < 1.0:
            return
        self._last_report = now
        try:
            self.on_progress(state=self.state,
                             visited=len(self.archive.videos) + len(self.archive.posts),
                             failed=self.counters.get("items_failed", 0),
                             current_url=(self.current_target.url if self.current_target else None),
                             details=self._progress_details())
        except Exception as exc:
            log.debug("Progress report failed: %s", exc)

    def _progress_details(self) -> dict:
        done = sum(1 for t in self.target_status.values()
                   if t["status"] in ("done", "failed", "unavailable"))
        dates = [(v.published_time if isinstance(v, YouTubeVideo) else v.get("published_time"))
                 for v in self.archive.videos.values()]
        dates = [d for d in dates if d]
        return {
            "phase": ("finished" if self.state == STOPPED
                      else "verification_required" if self.state == BLOCKED
                      else "collecting" if self.state == RECORDING
                      else "paused"),
            "message": self.phase_detail,
            "viewer": "signed_in" if self.signed_in else "signed_out",
            "targets_total": len(self.targets),
            "targets_done": done,
            "current_target": self.current_target.label if self.current_target else None,
            "items_encountered": self.counters.get("items_encountered", 0),
            "videos_exported": len(self.archive.videos),
            "posts_exported": len(self.archive.videos) + len(self.archive.posts),
            "channel_posts_exported": len(self.archive.posts),
            "comments_exported": len(self.archive.comments),
            "users_exported": len(self.archive.channels),
            "media_expected": self.counters.get("media_expected", 0),
            "media_captured": len(self.archive.media_index),
            "media_failed": self.counters.get("media_failed", 0),
            "newest_post": max(dates) if dates else None,
            "oldest_post": min(dates) if dates else None,
            "rate_limit_waits": self.counters.get("rate_limit_waits", 0),
            "disk_holds": self.counters.get("disk_holds", 0),
            "download": dict(self.download_state),
            "warc_files": self.counters.get("warc_files") or len(
                list(self.archive.out_dir.glob("*.warc.gz"))),
            "targets": list(self.target_status.values()),
        }

    # -- the run --------------------------------------------------------------
    def run(self) -> dict:
        self.archive.event("capture_created", mode=self.config.mode,
                           targets=[t.url for t in self.targets],
                           continuation_of=self.config.continuation_of)
        record_responses = getattr(self.client, "record_responses_to", None)
        if callable(record_responses):
            record_responses(self.archive.save_response)
        for target in self.targets:
            if self._stop_requested:
                break
            self.current_target = target
            status = self.target_status[target.key]
            status["status"] = "running"
            self.phase_detail = f"Collecting {target.label}."
            self._report(force=True)
            try:
                if target.kind == "channel":
                    self._capture_channel(target)
                elif target.kind == "playlist":
                    self._capture_playlist(target)
                else:
                    self._capture_single(target)
                status["status"] = "done" if not self._stop_requested else "interrupted"
            except TargetUnavailable as exc:
                if str(exc) == "stopped":
                    status["status"] = "interrupted"
                    break
                status["status"] = "unavailable"
                status["reason"] = str(exc)
                status["availability"] = exc.availability
                self.counters["targets_unavailable"] += 1
                self.archive.event("target_unavailable", target=target.url, reason=str(exc),
                                   availability=exc.availability)
            except YouTubeError as exc:
                status["status"] = "failed"
                status["reason"] = str(exc)
                self.counters["targets_failed"] += 1
                self.archive.event("target_failed", target=target.url, error=str(exc))
            finally:
                self._drain_client_anomalies()
                self._checkpoint()
        self.current_target = None
        if not self._stop_requested:
            self.stop_reason = self.stop_reason or "targets_complete"
            self.stop_rule = self.stop_rule or "every_target_worked"
        return self._finish()

    def _finish(self) -> dict:
        self.state = STOPPED
        self.download_state = {}
        self.phase_detail = self._closing_summary()
        self.archive.prune_unreferenced_responses()
        self.archive.finalise(self.manifest_document(final=True), self._checkpoint_document())
        self._report(force=True)
        forget = getattr(self.client, "forget_cookies", None)
        if callable(forget):
            try:
                forget()
            except Exception:
                pass
        try:
            self.persist(targets=self.newest_by_target,
                         videos=[self.archive._video_row(v) for v in self.archive.videos.values()],
                         posts=[self.archive._post_row(p) for p in self.archive.posts.values()])
        except Exception as exc:
            log.warning("Could not persist capture state: %s", exc)
        return {"stop_reason": self.stop_reason, "stop_rule": self.stop_rule,
                "videos": len(self.archive.videos), "posts": len(self.archive.posts),
                "comments": len(self.archive.comments), "media": len(self.archive.media_index)}

    def _closing_summary(self) -> str:
        videos, posts = len(self.archive.videos), len(self.archive.posts)
        comments, media = len(self.archive.comments), len(self.archive.media_index)
        because = {
            "targets_complete": "every target was worked",
            "curator_stop": "you selected Stop and save",
        }.get(self.stop_reason or "", str(self.stop_reason or "").replace("_", " "))
        return (f"Collected {videos} video{'s' if videos != 1 else ''}, "
                f"{posts} post{'s' if posts != 1 else ''}, "
                f"{comments} comment{'s' if comments != 1 else ''}, "
                f"{media} media file{'s' if media != 1 else ''}. Stopped because {because}.")

    def _drain_client_anomalies(self) -> None:
        anomalies = getattr(self.client, "anomalies", None)
        if not isinstance(anomalies, list):
            return
        for anomaly in anomalies[self._anomalies_drained:]:
            if isinstance(anomaly, dict):
                self.archive.event("client_anomaly", **anomaly)
        self._anomalies_drained = len(anomalies)

    # -- targets ----------------------------------------------------------------
    def _capture_single(self, target: YouTubeTarget) -> None:
        video = self._with_retries(lambda: self.client.video(target.video_id or ""),
                                   target.url, f"reading {target.label}")
        assert isinstance(video, YouTubeVideo)
        video.surface = "direct"
        self.counters["items_encountered"] += 1
        self.target_status[target.key]["items_encountered"] += 1
        self._take_video(video, target, already_full=True)

    def _capture_playlist(self, target: YouTubeTarget) -> None:
        playlist = self._with_retries(lambda: self.client.playlist(target), target.url,
                                      f"reading {target.label}")
        assert isinstance(playlist, YouTubePlaylist)
        items: list[dict] = []
        listing = _ResumableListing(lambda: self.client.list_items(target, "playlist"),
                                    self._rate_limit_or_stop)
        try:
            self._walk_listing(target, "playlist", listing, items)
        finally:
            close = getattr(listing, "close", None)
            if callable(close):
                close()
        self.archive.add_playlist(playlist, items)

    def _capture_channel(self, target: YouTubeTarget) -> None:
        channel = self._with_retries(lambda: self.client.channel(target), target.url,
                                     f"reading {target.label}")
        assert isinstance(channel, YouTubeChannel)
        channel.provenance = {**channel.provenance, "target_key": target.key}
        self.archive.add_channel(channel)
        status = self.target_status[target.key]
        status["channel_id"] = channel.channel_id
        status["handle"] = channel.handle
        if self.config.post_media and channel.avatar_url:
            self._fetch_into(self.archive.media_dir / "channels" / _safe_name(channel.channel_id),
                             channel.avatar_url, "avatar", {"role": "avatar",
                                                            "channel_id": channel.channel_id})
        # Posts first: the browser does that part and must be finished with
        # YouTube before its session is lent to the downloader
        surfaces = sorted(self.config.surfaces, key=lambda s: 0 if s == "posts" else 1)
        for surface in surfaces:
            if self._stop_requested:
                return
            if surface == "posts":
                self._capture_posts(target, channel)
                continue
            listing = _ResumableListing(lambda s=surface: self.client.list_items(target, s),
                                        self._rate_limit_or_stop)
            try:
                self._walk_listing(target, surface, listing, None)
            finally:
                close = getattr(listing, "close", None)
                if callable(close):
                    close()
            self._drain_client_anomalies()

    # -- listings -----------------------------------------------------------------
    def _walk_listing(self, target: YouTubeTarget, surface: str,
                      listing: Iterator[YouTubeVideo], playlist_items: Optional[list]) -> None:
        status = self.target_status[target.key]
        consecutive_older = 0
        prior = self.config.prior_newest.get(target.key) or {}
        position = 0
        while True:
            if self._stop_requested:
                return
            try:
                video = self._with_retries(lambda: next(listing), target.url,
                                           f"listing {target.label} {surface}")
            except TargetUnavailable:
                raise
            except StopIteration:
                break
            if not isinstance(video, YouTubeVideo):
                break
            position += 1
            self._check_control()
            if self._stop_requested:
                return
            self.counters["items_encountered"] += 1
            status["items_encountered"] += 1
            video.surface = surface
            if playlist_items is not None:
                playlist_items.append({"video_id": video.video_id, "position": position,
                                       "title": video.title, "availability": video.availability})
            if video.availability in ("private", "deleted", "unavailable"):
                self._note_absence(video, target)
                continue
            if video.video_id in self.archive.videos:
                self.exclusions["duplicate_across_surfaces"] += 1
                continue
            # the listing rarely says when a video was published; the
            # date-bounded modes read the video first, then decide
            if self.config.mode in ("date_range", "since_last") and not video.published_time:
                try:
                    video = self._read_full(video, target)
                except TargetUnavailable as exc:
                    if str(exc) == "stopped":
                        return
                    video.availability = exc.availability
                    video.unavailable_reason = str(exc)
                    self._note_absence(video, target)
                    continue
            decision = self._decide(video, prior, target)
            if decision == "select":
                self._take_video(video, target, already_full=video.complete)
                consecutive_older = 0
            elif decision == "older":
                self.exclusions["older_than_requested"] += 1
                consecutive_older += 1
            elif decision == "newer":
                self.exclusions["newer_than_requested"] += 1
            elif decision == "already_captured":
                self.exclusions["captured_previously"] += 1
                consecutive_older += 1
            if self._boundary_reached(consecutive_older, target):
                return
        if status["items_encountered"] == 0:
            self.archive.event("listing_empty", target=target.url, surface=surface)
            status["empty_surfaces"] = sorted(set(status.get("empty_surfaces", [])) | {surface})
        if self.config.mode in ("until_stopped", "end_of_listing"):
            self.archive.event("listing_exhausted", target=target.url, surface=surface,
                               items_encountered=status["items_encountered"])

    def _decide(self, item, prior: dict, target: YouTubeTarget) -> str:
        mode = self.config.mode
        when = getattr(item, "published_time", None) or ""
        if mode == "date_range":
            if not when:
                return "select"        # undated: neutral, kept
            if self.config.to_date and when > self.config.to_date:
                return "newer"
            if self.config.from_date and when < self.config.from_date:
                return "older"
            return "select"
        if mode == "latest_n":
            return "select" if self._selected(target) < self.config.latest_n else "older"
        if mode == "since_last":
            item_id = getattr(item, "video_id", None) or getattr(item, "post_id", None)
            if item_id == prior.get("item_id"):
                return "already_captured"
            newest_date = str(prior.get("date") or "")
            if newest_date and when and when <= newest_date:
                return "already_captured"
            if item_id in self.known_ids.get(target.key, set()):
                return "already_captured"
            return "select"
        return "select"

    def _selected(self, target: YouTubeTarget) -> int:
        return self.target_status[target.key]["items_selected"]

    def _boundary_reached(self, consecutive_older: int, target: YouTubeTarget) -> bool:
        mode = self.config.mode
        if mode in ("date_range", "since_last") and consecutive_older >= self.config.consecutive_older:
            self.archive.event("lower_boundary_reached", consecutive_older=consecutive_older)
            return True
        if mode == "latest_n" and self._selected(target) >= self.config.latest_n:
            return True
        return False

    def _note_absence(self, video: YouTubeVideo, target: YouTubeTarget) -> None:
        record = {"video_id": video.video_id, "availability": video.availability,
                  "reason": video.unavailable_reason, "surface": video.surface,
                  "target": target.label, "title": video.title}
        self.absences.append(record)
        self.counters["items_unavailable"] += 1
        self.archive.event("item_unavailable", **record)

    # -- one video's package ---------------------------------------------------------
    def _read_full(self, video: YouTubeVideo, target: YouTubeTarget) -> YouTubeVideo:
        full = self._with_retries(lambda: self.client.video(video.video_id), video.url or "",
                                  f"reading video {video.video_id}")
        assert isinstance(full, YouTubeVideo)
        full.surface = video.surface
        full.complete = True
        if not full.kind or full.kind == "video":
            full.kind = video.kind
        return full

    def _take_video(self, video: YouTubeVideo, target: YouTubeTarget, *, already_full: bool) -> None:
        if not already_full:
            try:
                video = self._read_full(video, target)
            except TargetUnavailable as exc:
                if str(exc) == "stopped":
                    return
                video.availability = exc.availability
                video.unavailable_reason = str(exc)
                self._note_absence(video, target)
                return
        video.provenance = {**video.provenance, "target_key": target.key}
        if not self.archive.add_video(video):
            return
        status = self.target_status[target.key]
        status["items_selected"] += 1
        self._track_newest(target, video.video_id, video.published_time)
        if self.config.capture_media or self.config.thumbnails or self.config.captions \
                or self.config.live_chat:
            self._download(video)
        if self.config.include_comments:
            self._collect_comments(video, "video", video.video_id,
                                   lambda: self.client.comments(video))
        self.archive.update_video(video)
        self.phase_detail = (f"Collecting {target.label}: {len(self.archive.videos)} videos, "
                             f"{len(self.archive.posts)} posts, "
                             f"{len(self.archive.comments)} comments.")
        self._report()
        if (len(self.archive.videos) + len(self.archive.posts)) % 10 == 0:
            self._checkpoint()

    def _track_newest(self, target: YouTubeTarget, item_id: str, when: Optional[str]) -> None:
        if target.kind != "channel":
            return
        current = self.newest_by_target.get(target.key) or {}
        if not current or (when or "") > (current.get("date") or ""):
            self.newest_by_target[target.key] = {
                "item_id": item_id, "date": when, "handle": target.handle,
                "channel_id": self.target_status[target.key].get("channel_id"),
                "url": target.url}

    def _download(self, video: YouTubeVideo) -> None:
        """Ask the client for the video's files, holding for disk first."""
        state, message = self.disk_check()
        if state != "ok":
            if not self._hold_for_disk(state, message):
                return
        dest = self.archive.video_dir(video.video_id)
        self.counters["media_expected"] += 1
        title = video.title or video.video_id

        def on_progress(info: dict) -> None:
            self.download_state = {"video_id": video.video_id, "title": title, **info}
            self._report()
            if self.control_poll() == CMD_STOP:
                self._request_stop("curator_stop", "curator_selected_stop_and_save")
                raise DownloadInterrupted("stopped")
            level, _ = self.disk_check()
            if level == "critical":
                raise DownloadInterrupted("disk critical")

        for _attempt in range(4):
            if self._stop_requested:
                return
            try:
                files = self._with_retries(
                    lambda: self.client.download(video, dest, on_progress),
                    video.url or "", f"downloading {video.video_id}")
            except DownloadInterrupted as exc:
                self.download_state = {}
                if str(exc) == "stopped" or self._stop_requested:
                    self.archive.event("download_interrupted", video_id=video.video_id,
                                       reason="stopped", partial_kept=True)
                    return
                self.archive.event("download_interrupted", video_id=video.video_id,
                                   reason="disk_critical", partial_kept=True)
                if not self._hold_for_disk("critical", "The download was stopped mid-file; "
                                           "the partial file is kept and resumes."):
                    return
                continue
            except TargetUnavailable as exc:
                self.download_state = {}
                if str(exc) == "stopped":
                    return
                self.counters["media_failed"] += 1
                self.archive.event("media_failed", video_id=video.video_id, error=str(exc))
                return
            except YouTubeError as exc:
                self.download_state = {}
                self.counters["media_failed"] += 1
                self.archive.event("media_failed", video_id=video.video_id, error=str(exc))
                return
            self.download_state = {}
            for entry in files or []:
                path = Path(entry.pop("path"))
                if not path.exists():
                    continue
                record = self.archive.register_file(path, {
                    **entry, "video_id": video.video_id,
                    "fetch_initiator": "swm", "fetched_via": "yt-dlp"})
                video.files.append(record)
            return

    def _fetch_into(self, folder: Path, url: str, stem: str, entry: dict) -> Optional[dict]:
        try:
            body, content_type = self._with_retries(lambda: self.client.fetch(url), url,
                                                    f"downloading {stem}")
        except TargetUnavailable as exc:
            if str(exc) == "stopped":
                return None
            self.counters["media_failed"] += 1
            self.archive.event("media_failed", url=url, error=str(exc))
            return None
        except YouTubeError as exc:
            self.counters["media_failed"] += 1
            self.archive.event("media_failed", url=url, error=str(exc))
            return None
        if not body:
            self.counters["media_failed"] += 1
            self.archive.event("media_failed", url=url, error="empty response")
            return None
        name = f"{stem}{_media_suffix(url, content_type)}"
        return self.archive.save_bytes(folder, name, body, {
            **entry, "source_url": url, "content_type": content_type or None,
            "fetch_initiator": "swm", "fetched_via": getattr(self.client, "last_fetch_via", None)})

    # -- posts -----------------------------------------------------------------------
    def _capture_posts(self, target: YouTubeTarget, channel: YouTubeChannel) -> None:
        status = self.target_status[target.key]
        try:
            first = self.client.posts(channel)
        except TargetUnavailable as exc:
            status["posts"] = {"status": "not_attempted", "reason": str(exc)}
            self.archive.event("posts_not_attempted", target=target.label, reason=str(exc))
            return
        listing = _ResumableListing(lambda: self.client.posts(channel), self._rate_limit_or_stop,
                                    first=first)
        prior = self.config.prior_newest.get(target.key) or {}
        consecutive_older = 0
        seen_any = False
        try:
            while True:
                if self._stop_requested:
                    return
                try:
                    post = self._with_retries(lambda: next(listing), channel.url or target.url,
                                              f"listing the posts of {target.label}")
                except StopIteration:
                    break
                if not isinstance(post, YouTubePost):
                    break
                seen_any = True
                self._check_control()
                if self._stop_requested:
                    return
                self.counters["items_encountered"] += 1
                status["items_encountered"] += 1
                if post.channel_id and channel.channel_id and post.channel_id != channel.channel_id:
                    self.counters["foreign_posts_skipped"] += 1
                    continue
                decision = self._decide(post, prior, target)
                if decision == "select":
                    self._take_post(post, target)
                    consecutive_older = 0
                elif decision == "older":
                    self.exclusions["older_than_requested"] += 1
                    consecutive_older += 1
                elif decision == "newer":
                    self.exclusions["newer_than_requested"] += 1
                elif decision == "already_captured":
                    self.exclusions["captured_previously"] += 1
                    consecutive_older += 1
                if self._boundary_reached(consecutive_older, target):
                    return
        finally:
            close = getattr(listing, "close", None)
            if callable(close):
                close()
            self._drain_client_anomalies()
        if not seen_any:
            self.archive.event("listing_empty", target=target.url, surface="posts")
            status["empty_surfaces"] = sorted(set(status.get("empty_surfaces", [])) | {"posts"})

    def _take_post(self, post: YouTubePost, target: YouTubeTarget) -> None:
        post.provenance = {**post.provenance, "target_key": target.key}
        if not self.archive.add_post(post):
            return
        self.target_status[target.key]["items_selected"] += 1
        self._track_newest(target, post.post_id, post.published_time)
        if self.config.post_media:
            folder = self.archive.post_dir(post.post_id)
            for index, image in enumerate(post.images):
                url = image.get("url")
                if not url:
                    continue
                self.counters["media_expected"] += 1
                record = self._fetch_into(folder, url, f"image-{index + 1}", {
                    "role": "post_image", "post_id": post.post_id, "position": index,
                    "discovered_from": post.provenance.get("response")})
                if record:
                    image["file"] = record["file"]
        if self.config.include_comments:
            self._collect_comments(post, "post", post.post_id,
                                   lambda: self.client.post_comments(post))
        self.phase_detail = (f"Collecting {target.label}: {len(self.archive.videos)} videos, "
                             f"{len(self.archive.posts)} posts, "
                             f"{len(self.archive.comments)} comments.")
        self._report()

    # -- comments ---------------------------------------------------------------------
    def _collect_comments(self, item, target_type: str, target_id: str,
                          opener: Callable[[], Iterator[YouTubeComment]]) -> None:
        wanted = self.config.max_comments_per_item
        observed = 0
        top_level = 0
        stop_reason = "exhausted"
        try:
            iterator = self._with_retries(opener, getattr(item, "url", "") or "",
                                          f"reading comments on {target_type} {target_id}")
            for comment in iterator:      # type: ignore[union-attr]
                if self._stop_requested:
                    stop_reason = "stopped"
                    return
                if not isinstance(comment, YouTubeComment):
                    continue
                if comment.reply_depth and not self.config.include_replies:
                    self.exclusions["replies_not_requested"] += 1
                    continue
                if observed >= wanted:
                    self.exclusions["comment_limit_reached"] += 1
                    stop_reason = "comment_limit_reached"
                    break
                comment.target_type = target_type
                comment.target_id = target_id
                if self.archive.add_comment(comment):
                    observed += 1
                    if not comment.reply_depth:
                        top_level += 1
        except TargetUnavailable as exc:
            if str(exc) == "stopped":
                stop_reason = "stopped"
                return
            stop_reason = "disabled" if exc.availability == "unavailable" and \
                "disabled" in str(exc).lower() else "failed"
            self.counters["comment_threads_failed"] += 1
            self.archive.event("comments_failed", target_type=target_type, target_id=target_id,
                               error=str(exc))
        except YouTubeError as exc:
            stop_reason = "failed"
            self.counters["comment_threads_failed"] += 1
            self.archive.event("comments_failed", target_type=target_type, target_id=target_id,
                               error=str(exc))
        finally:
            self._grade_comments(item, observed, top_level, stop_reason)

    def _grade_comments(self, item, observed: int, top_level: int, stop_reason: str) -> None:
        reported = item.comment_count if isinstance(item.comment_count, int) else None
        more = getattr(self.client, "comments_more", lambda: None)()
        if stop_reason == "stopped":
            status = "stopped_by_curator"
        elif stop_reason == "disabled":
            status = "disabled"
        elif stop_reason == "failed":
            status = "partial" if observed else "blocked"
        elif reported == 0 and observed == 0:
            status = "no_comments_reported"
        elif stop_reason == "comment_limit_reached":
            status = "capped"
        elif reported is not None and observed >= reported:
            status = "reported_count_reached"
        elif more is True or reported is not None:
            status = "partial"
        else:
            status = "exhausted_unverified"
        item.comment_capture = {
            "status": status, "observed": observed, "top_level": top_level,
            "reported": reported, "stop_reason": stop_reason,
            "cap": self.config.max_comments_per_item, "sort": self.config.comment_sort,
            "replies_requested": self.config.include_replies,
        }
        self.counters[f"comments_{status}"] += 1

    def _comment_statuses(self) -> dict:
        statuses: Counter = Counter()
        for records in (self.archive.videos.values(), self.archive.posts.values()):
            for record in records:
                capture = (record.get("comment_capture") if isinstance(record, dict)
                           else record.comment_capture) or {}
                if capture.get("status"):
                    statuses[capture["status"]] += 1
        return dict(statuses)

    # -- checkpoint and manifest ----------------------------------------------------
    def _checkpoint(self) -> None:
        try:
            _atomic_json(self.archive.checkpoint_path, self._checkpoint_document())
        except OSError as exc:
            log.warning("Checkpoint failed: %s", exc)

    def _checkpoint_document(self) -> dict:
        return {
            "schema": "swm-youtube-checkpoint-v1",
            "updated_at": _iso_now(),
            "crawl_id": self.crawl_id,
            "mode": self.config.mode,
            "targets": list(self.target_status.values()),
            "newest_by_target": self.newest_by_target,
            "phase": self._progress_details()["phase"],
            "message": self.phase_detail,
            "videos_exported": len(self.archive.videos),
            "posts_exported": len(self.archive.posts),
            "comments_exported": len(self.archive.comments),
            "media_captured": len(self.archive.media_index),
            "stop_reason": self.stop_reason,
            "stop_rule": self.stop_rule,
        }

    def manifest_document(self, *, final: bool = False) -> dict:
        details = self._progress_details()
        from .metadata import manifest_section
        versions = getattr(self.client, "versions", None) or {"client": getattr(self.client, "version", None)}
        tools = getattr(self.client, "tool_report", None)
        tools = tools() if callable(tools) else {}
        has_warc = bool(self.counters.get("warc_files") or any(self.archive.out_dir.glob("*.warc.gz")))
        return {
            "schema": "swm-youtube-capture-manifest-v1",
            "metadata": manifest_section(self.archive.out_dir),
            "capture": {
                "crawl_id": self.crawl_id,
                "name": self.crawl_name,
                "operator": self.config.operator,
                "clients": versions,
                "targets": [{"url": t.url, "kind": t.kind, **self.target_status[t.key]}
                            for t in self.targets],
                "mode": self.config.mode,
                "parameters": {
                    "from": self.config.from_date, "to": self.config.to_date,
                    "latest_n": self.config.latest_n,
                    "consecutive_older_required": self.config.consecutive_older,
                    "surfaces": list(self.config.surfaces),
                    "capture_media": self.config.capture_media,
                    "max_resolution": self.config.max_resolution,
                    "thumbnails": self.config.thumbnails,
                    "captions": self.config.captions,
                    "auto_captions": self.config.auto_captions,
                    "live_chat": self.config.live_chat,
                    "post_media": self.config.post_media,
                    "include_comments": self.config.include_comments,
                    "maximum_comments_per_item": self.config.max_comments_per_item,
                    "include_replies": self.config.include_replies,
                    "comment_sort": self.config.comment_sort,
                    "write_warc": self.config.write_warc,
                },
                "viewer": "signed_in" if self.signed_in else "signed_out",
                "continuation_of": self.config.continuation_of,
                "state": self.state,
                "final": final,
                "stop_reason": self.stop_reason,
                "stopping_rule_fired": self.stop_rule,
            },
            "layers": {
                "media": {
                    "path": "media/",
                    "meaning": "Each video as the best rendition YouTube served within the "
                               "resolution the curator allowed, muxed by yt-dlp from the "
                               "separate video and audio streams YouTube serves; never the "
                               "upload. The rendition chosen is recorded per file. Thumbnails, "
                               "captions and live-chat replays as YouTube served them. Post "
                               "images at the largest size the page offered. SHA-256 for "
                               "every file.",
                },
                "evidence": {
                    "path": "evidence/yt-dlp/",
                    "source": "yt-dlp",
                    "evidence_type": "tool-derived metadata",
                    "verbatim_platform_response": False,
                    "meaning": "yt-dlp's reading of each video, kept whole: the tool's "
                               "normalisation of YouTube's responses, including the comments "
                               "it read. Not YouTube's response. Every video record and "
                               "video comment was derived from the file its provenance names.",
                },
                "raw": {
                    "path": "raw/",
                    "responses": "raw/responses/",
                    "responses_saved": self.archive.responses_saved,
                    "source": "youtube",
                    "evidence_type": "observed HTTP response",
                    "verbatim_platform_response": True,
                    "meaning": "YouTube's own responses as the browser received them while "
                               "reading the Posts tab and its comments, with the session's "
                               "material removed. Only responses a kept record was read from "
                               "are retained; each post and post comment names its response.",
                },
                "normalised": {
                    "channels": "youtube-channels.json",
                    "videos": ["youtube-videos.jsonl", "youtube-videos.csv"],
                    "posts": ["youtube-posts.jsonl", "youtube-posts.csv"],
                    "comments": ["youtube-comments.jsonl", "youtube-comments.csv"],
                    "playlists": ["youtube-playlists.jsonl", "youtube-playlist-items.jsonl"],
                    "comment_model": "one record type for video and post comments: target_type, "
                                     "target_id, parent_id, thread_root_id, reply_depth",
                    "availability_vocabulary": list(AVAILABILITY),
                    "selection_exclusions": dict(self.exclusions),
                },
                "web_context": {
                    "warc": "*.warc.gz" if has_warc else None,
                    "meaning": "Optional record of the browser's exchanges while reading the "
                               "Posts tab. Video streams are never in it: the downloaded files "
                               "are the objects, the WARC is evidence of presentation.",
                },
                "fixity": "checksums.sha256",
            },
            "replay": {
                "expected": "partial" if has_warc else "none",
                "preserves": ["captured page structure", "post text", "post images",
                              "thumbnails"] if has_warc else [],
                "not_expected": ["video streaming playback", "complete interactive comments",
                                 "session-dependent YouTube application behaviour"],
                "meaning": "A green replay load is not evidence of completeness; the records, "
                           "media and manifest are.",
            },
            "tools": {"swm_youtube": "1", **tools},
            "counts": dict(self.counters) | {
                "comment_statuses": self._comment_statuses(),
                "warc_files": self.counters.get("warc_files") or len(
                    list(self.archive.out_dir.glob("*.warc.gz"))),
                "videos_exported": len(self.archive.videos),
                "posts_exported": len(self.archive.posts),
                "comments_exported": len(self.archive.comments),
                "channels_exported": len(self.archive.channels),
                "playlists_exported": len(self.archive.playlists),
                "items_unavailable": len(self.absences),
                "media_captured": len(self.archive.media_index),
                "targets_total": len(self.targets),
                "targets_done": details["targets_done"],
            },
            "coverage": {
                "newest_video": details["newest_post"],
                "oldest_video": details["oldest_post"],
                "absences": self.absences[:500],
            },
            "completeness": {
                "claim": "This package holds what YouTube served this session for these "
                         "targets during this capture, within the limits the curator set. "
                         "It does not claim to hold everything YouTube holds.",
                "listing_end": "A channel tab or playlist enumeration ends when YouTube offers "
                               "no further entries; that is reported as listing_exhausted. A "
                               "date-bounded walk stops after the configured number of "
                               "consecutive older items.",
                "posts_dates": "YouTube shows a post's age as relative text only; "
                               "published_time on a post is an estimate from that text and "
                               "published_text is what YouTube said.",
                "polls": "Options are recorded; results require a vote, which the capture "
                         "never casts, so results are not available.",
                "engagement_figures": "View, like and comment counts are observations at "
                                      "capture time.",
                "signed_out_capture_meaning": (
                    "Signed out, YouTube may withhold age-restricted and members-only "
                    "content and answer some requests with a bot check; what is here is "
                    "bounded by that.") if not self.signed_in else None,
            },
            "updated_at": _iso_now(),
        }


def needs_posts_browser(config: YouTubeCaptureConfig) -> bool:
    """Whether this run will read a Posts tab, which only a channel has.

    The browser is opened up front only then; a video or playlist job
    opens one on demand, for a sign-in YouTube asks for, and otherwise
    never, so no empty window sits beside the run."""
    return "posts" in config.surfaces and any(
        parse_youtube_target(url).kind == "channel" for url in config.targets)


def free_disk_check(path: Path, warning_percent: float, critical_percent: float
                    ) -> Callable[[], tuple[str, str]]:
    """A disk check the worker hands the engine: free space at ``path``
    against the warning level from Settings and a critical level below it."""
    def check() -> tuple[str, str]:
        try:
            usage = shutil.disk_usage(str(path))
        except OSError:
            return "ok", ""
        free = usage.free / usage.total * 100.0 if usage.total else 100.0
        free_gb = usage.free / 1e9
        if free <= critical_percent:
            return "critical", f"{free:.1f}% ({free_gb:.1f} GB) free, below the critical level of {critical_percent:g}%."
        if free <= warning_percent:
            return "warning", f"{free:.1f}% ({free_gb:.1f} GB) free, below the warning level of {warning_percent:g}%."
        return "ok", ""
    return check
