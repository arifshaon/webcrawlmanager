"""Background Instagram capture for SWM.

Instagram is captured as a preservation package, in this order of primacy:

1. original media at the highest resolution Instagram serves;
2. the untouched structured payloads Instagram returned, under ``raw/``;
3. normalised posts, profiles and comments as JSONL/CSV;
4. optionally, a rendered WARC of the same posts, for how Instagram presented
   them;
5. a manifest stating what was asked for, what was reached, what failed and
   what was not attempted; and SHA-256 fixity for every file written.

A browser-driven WARC is deliberately not the primary record here. A browser
requests only what its rendering happened to need -- a thumbnail size, the
first video segment, ten comments -- which makes it a sample of the raw
layer rather than the raw layer itself.

Extraction runs in the background through Instaloader over a session the
curator created once, by signing in to the dedicated Instagram browser
profile. That session is read from the profile when a job starts and handed
to the client in memory; it is never written anywhere else. The visible
browser opens only when a person is needed: to sign in, or to clear a
checkpoint.

The account, not the tool, is what Instagram's automation detection acts on.
Every request here goes through Instaloader's rate controller, targets are
worked one at a time, and a run is built to be interrupted by Instagram --
held on a rate limit, held for the curator on a checkpoint -- and resumed,
not restarted.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Protocol
from urllib.parse import urlsplit

from .facebook import (_append_jsonl, _atomic_json, _date_bound, _iso_now,
                       _media_suffix, _normalise_datetime)

log = logging.getLogger(__name__)

RECORDING = "recording"
PAUSED = "paused"
BLOCKED = "blocked"
STOPPED = "stopped"

CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_STOP = "stop"

INSTAGRAM_MODES = {
    "single_post", "date_range", "latest_n", "until_stopped",
    "end_of_timeline", "since_last",
}

# Instagram allows at most three pinned posts, shown before the rest. A
# stopping rule that needs more than three consecutive older posts before it
# fires cannot be tripped by pinned posts alone, whether or not they are
# recognised as pinned.
_MAX_PINNED = 3


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
_REJECTED_PREFIXES = (
    "/direct/", "/explore/", "/stories/", "/accounts/", "/reels/audio/",
    "/challenge/",
)
_REJECTED_TOP = {"explore", "direct", "stories", "accounts", "reels", "p",
                 "tv", "reel", "challenge", "about", "legal", "developer"}


@dataclass(frozen=True)
class InstagramTarget:
    """What a curator asked for, once the URL has been read."""
    kind: str                     # "profile" | "post" | "reel"
    username: Optional[str] = None
    shortcode: Optional[str] = None
    url: str = ""

    @property
    def key(self) -> str:
        if self.kind == "profile":
            return f"instagram:@{self.username}"
        return f"instagram:/{self.shortcode}"

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.kind == "profile" else \
            f"{self.kind} {self.shortcode}"


def parse_instagram_target(raw: object) -> InstagramTarget:
    """Read a profile URL, a bare username, or a post/reel URL.

    Surfaces that cannot honestly be presented as an archive -- explore,
    hashtag and location results are algorithmically selected -- and ones
    that are private to the account -- direct messages, stories -- are
    refused with the reason rather than captured as something else.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Enter an Instagram profile, post or reel.")
    if "://" not in text and "/" not in text:
        username = text.lstrip("@")
        if not _USERNAME_RE.match(username):
            raise ValueError(f"{text!r} is not an Instagram username.")
        return InstagramTarget("profile", username=username,
                               url=f"https://www.instagram.com/{username}/")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host not in _INSTAGRAM_HOSTS:
        raise ValueError(f"{text} is not an Instagram URL.")
    path = parts.path or "/"
    if path.startswith("/explore/"):
        raise ValueError(
            "Explore, hashtag and location results are selected by "
            "Instagram's ranking and cannot be presented as an archive of "
            "anything. Capture the profiles that posted instead.")
    for prefix in _REJECTED_PREFIXES:
        if path.startswith(prefix):
            raise ValueError(
                f"{path} is not something SWM captures: direct messages, "
                "stories, explore and account pages are outside the "
                "supported profile, post and reel targets.")
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise ValueError("Enter a profile, post or reel URL, not the "
                         "Instagram home page.")
    if segments[0] in ("p", "reel", "tv") and len(segments) >= 2:
        code = segments[1]
        if not _SHORTCODE_RE.match(code):
            raise ValueError(f"{code!r} is not a valid post code.")
        kind = "reel" if segments[0] == "reel" else "post"
        return InstagramTarget(kind, shortcode=code,
                               url=f"https://www.instagram.com/{segments[0]}/{code}/")
    if segments[0] == "explore" or segments[0] in _REJECTED_TOP:
        raise ValueError(
            f"/{segments[0]}/ is not a profile, post or reel. Hashtag, "
            "location and explore results are selected by Instagram's "
            "ranking and cannot be presented as an archive of anything.")
    username = segments[0]
    if not _USERNAME_RE.match(username):
        raise ValueError(f"{username!r} is not an Instagram username.")
    if len(segments) >= 2 and segments[1] not in ("", "reels", "tagged"):
        raise ValueError(
            f"/{'/'.join(segments)} is not a profile, post or reel URL.")
    return InstagramTarget("profile", username=username,
                           url=f"https://www.instagram.com/{username}/")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """One piece of a post's media, in the order Instagram shows it."""
    url: str
    kind: str                      # "image" | "video"
    position: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    thumbnail_url: Optional[str] = None


@dataclass
class InstagramPost:
    media_id: str
    shortcode: str
    owner_username: Optional[str] = None
    owner_id: Optional[str] = None
    kind: str = "image"           # image | video | carousel | reel
    created_time: Optional[str] = None
    caption: Optional[str] = None
    permalink_url: Optional[str] = None
    likes_count: Optional[int] = None
    comments_count: Optional[int] = None
    video_view_count: Optional[int] = None
    media: list[MediaItem] = field(default_factory=list)
    is_pinned: bool = False
    surface: str = "posts"        # posts | reels | direct
    source: str = "instaloader"
    raw: dict = field(default_factory=dict)


@dataclass
class InstagramComment:
    comment_id: str
    post_shortcode: str
    parent_comment_id: Optional[str] = None
    author_id: Optional[str] = None
    author_username: Optional[str] = None
    text: Optional[str] = None
    created_time: Optional[str] = None
    likes_count: Optional[int] = None
    depth: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class InstagramProfile:
    user_id: str
    username: str
    full_name: Optional[str] = None
    biography: Optional[str] = None
    is_private: bool = False
    is_verified: bool = False
    followers_count: Optional[int] = None
    following_count: Optional[int] = None
    posts_count: Optional[int] = None
    profile_pic_url: Optional[str] = None
    external_url: Optional[str] = None
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The client the engine talks to
# ---------------------------------------------------------------------------

class InstagramError(Exception):
    """Base for the conditions the engine has a response to."""


class RateLimited(InstagramError):
    def __init__(self, wait_seconds: float = 300.0, detail: str = ""):
        super().__init__(detail or "Instagram is limiting requests.")
        self.wait_seconds = wait_seconds


class LoginRequired(InstagramError):
    """The session is missing, expired, or not enough for this target."""


class CheckpointRequired(InstagramError):
    """Instagram wants a person: a verification or a challenge."""


class TargetUnavailable(InstagramError):
    """Not found, private and not followed, or otherwise not servable."""


class InstagramClient(Protocol):
    """What the engine needs from Instagram, so a test can stand it in.

    Every method returns plain records carrying the untouched payload in
    ``raw``; the client does not decide what to keep. Errors are raised as the
    classes above so the engine's response -- wait, hold for the curator, or
    move on -- is the same whatever produced them.
    """

    def viewer(self) -> Optional[str]: ...
    def profile(self, username: str) -> InstagramProfile: ...
    def profile_posts(self, username: str) -> Iterator[InstagramPost]: ...
    def profile_reels(self, username: str) -> Iterator[InstagramPost]: ...
    def post(self, shortcode: str) -> InstagramPost: ...
    def comments(self, shortcode: str,
                 include_replies: bool) -> Iterator[InstagramComment]: ...
    def fetch(self, url: str) -> tuple[bytes, str]: ...


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
class InstagramCaptureConfig:
    targets: list[str]
    mode: str = "latest_n"
    requested_mode: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    latest_n: int = 100
    consecutive_older: int = 5
    surfaces: tuple[str, ...] = ("posts", "reels")
    capture_media: bool = True
    include_comments: bool = False
    max_comments_per_post: int = 25
    include_replies: bool = False
    max_replies_per_comment: int = 10
    write_warc: bool = False
    end_stall_rounds: int = 3
    prior_newest: dict = field(default_factory=dict)   # key -> {media_id, date}
    continuation_of: Optional[int] = None
    operator: str = "webarc"
    browser_profile_dir: Optional[str] = None
    # "headed" is SWM's managed Chrome with a dedicated profile; "native" is
    # the system's own Chrome with a dedicated profile, attached over CDP.
    # Either way the profile is where the curator signs in and where the
    # session is read from.
    browser_mode: str = "headed"
    chrome_path: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "InstagramCaptureConfig":
        targets_raw = raw.get("targets")
        if isinstance(targets_raw, str):
            targets_raw = [line for line in re.split(r"[\n,\s]+", targets_raw)
                           if line.strip()]
        if not isinstance(targets_raw, list) or not targets_raw:
            raise ValueError("Add at least one Instagram profile, post or reel.")
        targets: list[str] = []
        for item in targets_raw:
            target = parse_instagram_target(item)
            if target.url not in targets:
                targets.append(target.url)
        mode = str(raw.get("mode") or "latest_n")
        if mode not in INSTAGRAM_MODES:
            raise ValueError(f"Unsupported Instagram capture mode: {mode}")
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
        surfaces_raw = raw.get("surfaces") or ["posts", "reels"]
        if isinstance(surfaces_raw, str):
            surfaces_raw = [surfaces_raw]
        surfaces = tuple(s for s in ("posts", "reels") if s in surfaces_raw)
        if not surfaces:
            raise ValueError("Select at least one of Posts and Reels.")
        prior = raw.get("prior_newest") or {}
        if mode == "since_last":
            missing = [u for u in targets
                       if parse_instagram_target(u).kind == "profile"
                       and not (prior.get(parse_instagram_target(u).key) or {}).get("media_id")]
            if missing:
                raise ValueError(
                    "No previous capture state exists for "
                    + ", ".join(parse_instagram_target(u).label for u in missing)
                    + ". Run another capture mode first.")
        return cls(
            targets=targets,
            mode=mode,
            requested_mode=mode,
            from_date=from_date,
            to_date=to_date,
            latest_n=_whole(raw.get("latest_n"), 100, "Latest N", 1, 100_000),
            consecutive_older=max(_MAX_PINNED + 1, _whole(
                raw.get("consecutive_older"), 5, "Consecutive older posts",
                2, 25)),
            surfaces=surfaces,
            capture_media=bool(raw.get("capture_media", True)),
            include_comments=bool(raw.get("include_comments", False)),
            max_comments_per_post=_whole(
                raw.get("max_comments_per_post"), 25,
                "Maximum comments per post", 1, 5_000),
            include_replies=bool(raw.get("include_replies", False)),
            max_replies_per_comment=_whole(
                raw.get("max_replies_per_comment"), 10,
                "Maximum replies per comment", 1, 1_000),
            write_warc=bool(raw.get("write_warc", False)),
            end_stall_rounds=max(1, _whole(
                raw.get("end_stall_rounds"), 3, "Stall rounds", 1, 20)),
            prior_newest=dict(prior),
            continuation_of=raw.get("continuation_of"),
            operator=str(raw.get("operator") or "webarc"),
            browser_profile_dir=(raw.get("browser_profile_dir")
                                 or (raw.get("browser") or {}).get("user_data_dir")),
            browser_mode=(str((raw.get("browser") or {}).get("mode")
                              or raw.get("browser_mode") or "headed")),
            chrome_path=(raw.get("browser") or {}).get("chrome_path"),
        )


# ---------------------------------------------------------------------------
# Archive: everything written to disk
# ---------------------------------------------------------------------------

class InstagramArchive:
    """The capture package on disk, written incrementally.

    Raw payloads go under ``raw/`` exactly as received. Media is stored under
    a content-addressed name so a file referenced by several posts is kept
    once, with SHA-256 recorded for every file. Exports are appended as
    records arrive, so an interrupted run leaves a readable package.
    """

    POST_FIELDS = [
        "media_id", "shortcode", "owner_username", "owner_id", "kind",
        "created_time", "caption", "permalink_url", "likes_count",
        "comments_count", "video_view_count", "media_urls", "media_files",
        "is_pinned", "surface", "source",
    ]
    COMMENT_FIELDS = [
        "comment_id", "post_shortcode", "parent_comment_id", "author_id",
        "author_username", "text", "created_time", "likes_count", "depth",
    ]

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.out_dir / "raw"
        self.media_dir = self.out_dir / "media"
        self.posts_path = self.out_dir / "instagram-posts.jsonl"
        self.comments_path = self.out_dir / "instagram-comments.jsonl"
        self.profiles_path = self.out_dir / "instagram-profiles.json"
        self.media_path = self.out_dir / "instagram-media.json"
        self.events_path = self.out_dir / "instagram-events.jsonl"
        self.manifest_path = self.out_dir / "instagram-manifest.json"
        self.checkpoint_path = self.out_dir / "instagram-checkpoint.json"
        self.checksums_path = self.out_dir / "checksums.sha256"
        self.posts: dict[str, InstagramPost] = {}
        self.comments: dict[str, InstagramComment] = {}
        self.profiles: dict[str, InstagramProfile] = {}
        # url -> {"file", "sha256", "bytes", "content_type"}
        self.media_index: dict[str, dict] = {}
        self._checksums: dict[str, str] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """A continued run starts from what the package already holds."""
        if self.media_path.exists():
            try:
                loaded = json.loads(self.media_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.media_index = {
                        k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, json.JSONDecodeError):
                pass
        for path, key, into in (
            (self.posts_path, "shortcode", self.posts),
            (self.comments_path, "comment_id", self.comments),
        ):
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
        _append_jsonl(self.events_path, {
            "time": _iso_now(), "event": event, **details})

    # -- raw ----------------------------------------------------------------
    def save_raw(self, kind: str, name: str, payload: object) -> Path:
        target = self.raw_dir / kind / f"{_safe_name(name)}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(target, payload)
        self._checksum(target)
        return target

    # -- records ------------------------------------------------------------
    def add_profile(self, profile: InstagramProfile) -> None:
        self.profiles[profile.username] = profile
        _atomic_json(self.profiles_path, {
            name: _without_raw(asdict(p)) for name, p in self.profiles.items()})
        self.save_raw("profiles", profile.username, profile.raw)

    def add_post(self, post: InstagramPost) -> bool:
        if post.shortcode in self.posts:
            return False
        self.posts[post.shortcode] = post
        row = self._post_row(post)
        _append_jsonl(self.posts_path, row)
        if post.raw:
            self.save_raw("posts", post.shortcode, post.raw)
        return True

    def add_comment(self, comment: InstagramComment) -> bool:
        if comment.comment_id in self.comments:
            return False
        self.comments[comment.comment_id] = comment
        _append_jsonl(self.comments_path, _without_raw(asdict(comment)))
        return True

    def save_raw_comments(self, shortcode: str, payloads: list[dict]) -> None:
        if payloads:
            self.save_raw("comments", shortcode, payloads)

    def _post_row(self, post: "InstagramPost | dict") -> dict:
        if isinstance(post, dict):
            return dict(post)         # loaded from an earlier run's export
        row = _without_raw(asdict(post))
        row.pop("media", None)
        row["media_urls"] = [m.url for m in post.media]
        row["media_files"] = [
            (self.media_index.get(m.url) or {}).get("file") for m in post.media]
        return row

    # -- media --------------------------------------------------------------
    def save_media(self, url: str, body: bytes, content_type: str = "") -> dict:
        existing = self.media_index.get(url)
        if existing:
            return existing
        digest = hashlib.sha256(body).hexdigest()
        name = f"{digest}{_media_suffix(url, content_type)}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        target = self.media_dir / name
        if not target.exists():
            temporary = target.with_name(name + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
        entry = {"file": name, "sha256": digest, "bytes": len(body),
                 "content_type": content_type or None}
        self.media_index[url] = entry
        self._checksums[f"media/{name}"] = digest
        return entry

    # -- fixity -------------------------------------------------------------
    def _checksum(self, path: Path) -> None:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return
        self._checksums[str(path.relative_to(self.out_dir)).replace(
            "\\", "/")] = digest

    def finalise(self, manifest: dict, checkpoint: dict) -> None:
        """Write everything that is derived from the run's state."""
        _atomic_json(self.media_path, self.media_index)
        self._write_csv(self.out_dir / "instagram-posts.csv", self.POST_FIELDS,
                        (self._post_row(p) for p in self.posts.values()))
        self._write_csv(self.out_dir / "instagram-comments.csv",
                        self.COMMENT_FIELDS,
                        (_without_raw(asdict(c)) if not isinstance(c, dict)
                         else c for c in self.comments.values()))
        _atomic_json(self.checkpoint_path, checkpoint)
        _atomic_json(self.manifest_path, manifest)
        for name in ("instagram-posts.jsonl", "instagram-comments.jsonl",
                     "instagram-posts.csv", "instagram-comments.csv",
                     "instagram-profiles.json", "instagram-media.json",
                     "instagram-manifest.json"):
            path = self.out_dir / name
            if path.exists():
                self._checksum(path)
        lines = [f"{digest}  {name}" for name, digest
                 in sorted(self._checksums.items())]
        temporary = self.checksums_path.with_name("checksums.sha256.tmp")
        temporary.write_text("\n".join(lines) + ("\n" if lines else ""),
                             encoding="utf-8")
        temporary.replace(self.checksums_path)

    @staticmethod
    def _write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                current = dict(row)
                for key in ("media_urls", "media_files"):
                    if isinstance(current.get(key), list):
                        current[key] = json.dumps(current[key],
                                                  ensure_ascii=False)
                writer.writerow(current)
        temporary.replace(path)


def _without_raw(row: dict) -> dict:
    row = dict(row)
    row.pop("raw", None)
    return row


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))[:120] or "item"


# ---------------------------------------------------------------------------
# The capture itself
# ---------------------------------------------------------------------------

def _post_date_key(post: InstagramPost) -> str:
    return post.created_time or ""


def mark_pinned_by_order(posts: list[InstagramPost]) -> None:
    """Recognise pinned posts by where Instagram puts them, not by a flag.

    Instagram no longer says which posts are pinned, but it still shows them
    first. A post among the first few that is older than a post shown after
    it is out of chronological order, and that is what a pinned post looks
    like from the outside.
    """
    head = posts[:_MAX_PINNED + 1]
    for index, post in enumerate(head[:_MAX_PINNED]):
        later = [p.created_time for p in posts[index + 1:_MAX_PINNED + 3]
                 if p.created_time]
        if post.created_time and later and post.created_time < max(later):
            post.is_pinned = True


class InstagramCaptureSession:
    """Runs one Instagram job: several targets, one at a time.

    ``control_poll`` returns "pause", "resume", "stop" or None; the run
    checks it between posts and while waiting out a rate limit, so Stop
    always answers within seconds. ``on_progress`` receives the state and a
    details dict for the dashboard. ``open_browser`` is called with a URL when
    a person is needed and returns a callable that closes the window again.
    """

    def __init__(self, *, config: InstagramCaptureConfig, client: InstagramClient,
                 output_dir: Path, crawl_id: int, crawl_name: str,
                 known_ids: Optional[dict[str, set[str]]] = None,
                 control_poll: Optional[Callable[[], Optional[str]]] = None,
                 on_progress: Optional[Callable[..., None]] = None,
                 persist: Optional[Callable[..., None]] = None,
                 open_browser: Optional[Callable[[str], Callable[[], None]]] = None,
                 rendered_pass: Optional[Callable[[list[str]], int]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.client = client
        self.archive = InstagramArchive(output_dir)
        self.crawl_id = crawl_id
        self.crawl_name = crawl_name
        self.known_ids = known_ids or {}
        self.control_poll = control_poll or (lambda: None)
        self.on_progress = on_progress or (lambda **_kw: None)
        self.persist = persist or (lambda **_kw: None)
        self.open_browser = open_browser
        self.rendered_pass = rendered_pass
        self.sleep = sleep

        self.state = RECORDING
        self.phase_detail = "Starting."
        self.stop_reason: Optional[str] = None
        self.stop_rule: Optional[str] = None
        self.counters: Counter = Counter()
        self.exclusions: Counter = Counter()
        self.targets = [parse_instagram_target(u) for u in config.targets]
        self.target_status: dict[str, dict] = {
            t.key: {"label": t.label, "kind": t.kind, "status": "pending",
                    "posts_selected": 0, "posts_encountered": 0}
            for t in self.targets}
        self.current_target: Optional[InstagramTarget] = None
        self._stop_requested = False
        self._last_report = 0.0
        self.viewer_username: Optional[str] = None
        self.newest_by_target: dict[str, dict] = {}

    # -- control ------------------------------------------------------------
    def _check_control(self) -> None:
        """Honour pause and stop between units of work."""
        command = self.control_poll()
        if command == CMD_STOP:
            self._request_stop("curator_stop", "curator_selected_stop_and_save")
            return
        if command == CMD_PAUSE:
            self.state = PAUSED
            self.phase_detail = ("Paused. Nothing further is requested from "
                                 "Instagram until you resume.")
            self.archive.event("paused", actor="dashboard")
            self._report(force=True)
            while True:
                self.sleep(1.0)
                command = self.control_poll()
                if command == CMD_STOP:
                    self._request_stop("curator_stop",
                                       "curator_selected_stop_and_save")
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
        """Open the browser for a person and wait. Returns False on stop."""
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
                    self._request_stop("curator_stop",
                                       "curator_selected_stop_and_save")
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

    def _wait_out_rate_limit(self, exc: RateLimited) -> bool:
        """Sit out a rate limit, still answering Stop. False on stop."""
        wait = max(30.0, min(float(exc.wait_seconds), 1800.0))
        self.counters["rate_limit_waits"] += 1
        self.archive.event("rate_limited", wait_seconds=wait, detail=str(exc))
        remaining = wait
        while remaining > 0:
            self.phase_detail = (f"Instagram is limiting requests. Waiting "
                                 f"{int(remaining)}s before continuing.")
            self._report(force=True)
            step = min(5.0, remaining)
            self.sleep(step)
            remaining -= step
            if self.control_poll() == CMD_STOP:
                self._request_stop("curator_stop",
                                   "curator_selected_stop_and_save")
                return False
        return True

    def _with_retries(self, action: Callable[[], object],
                      url: str, what: str) -> object:
        """Run one Instagram call under the engine's response policy."""
        for _attempt in range(6):
            if self._stop_requested:
                raise TargetUnavailable("stopped")
            try:
                return action()
            except RateLimited as exc:
                if not self._wait_out_rate_limit(exc):
                    raise TargetUnavailable("stopped") from exc
            except LoginRequired as exc:
                if not self._hold_for_curator(
                        f"Instagram needs you to sign in before {what}: {exc}. "
                        "Sign in in the browser window that has opened, then "
                        "select “I have resolved it — continue”.",
                        url):
                    raise TargetUnavailable("stopped") from exc
                self._refresh_client()
            except CheckpointRequired as exc:
                if not self._hold_for_curator(
                        f"Instagram is asking for verification before {what}: "
                        f"{exc}. Resolve it in the browser window that has "
                        "opened, then select “I have resolved it — "
                        "continue”.", url):
                    raise TargetUnavailable("stopped") from exc
                self._refresh_client()
        raise TargetUnavailable(f"{what} did not succeed after repeated tries.")

    def _refresh_client(self) -> None:
        refresh = getattr(self.client, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:
                log.warning("Session refresh failed: %s", exc)

    # -- progress -----------------------------------------------------------
    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < 1.0:
            return
        self._last_report = now
        try:
            self.on_progress(state=self.state,
                             visited=len(self.archive.posts),
                             failed=self.counters.get("posts_failed", 0),
                             current_url=(self.current_target.url
                                          if self.current_target else None),
                             details=self._progress_details())
        except Exception as exc:
            log.debug("Progress report failed: %s", exc)

    def _progress_details(self) -> dict:
        done = sum(1 for t in self.target_status.values()
                   if t["status"] in ("done", "failed", "unavailable"))
        dates = [p.created_time for p in self.archive.posts.values()
                 if isinstance(p, InstagramPost) and p.created_time] + [
            p.get("created_time") for p in self.archive.posts.values()
            if isinstance(p, dict) and p.get("created_time")]
        return {
            "phase": ("finished" if self.state == STOPPED
                      else "verification_required" if self.state == BLOCKED
                      else "collecting" if self.state == RECORDING
                      else "paused"),
            "message": self.phase_detail,
            "viewer": self.viewer_username or "signed_out",
            "targets_total": len(self.targets),
            "targets_done": done,
            "current_target": self.current_target.label if self.current_target else None,
            "posts_encountered": self.counters.get("posts_encountered", 0),
            "posts_exported": len(self.archive.posts),
            "media_expected": self.counters.get("media_expected", 0),
            "media_captured": len(self.archive.media_index),
            "media_failed": self.counters.get("media_failed", 0),
            "comments_exported": len(self.archive.comments),
            "comments_available": self._comments_stated(),
            "newest_post": max(dates) if dates else None,
            "oldest_post": min(dates) if dates else None,
            "rate_limit_waits": self.counters.get("rate_limit_waits", 0),
            "pagination_failures": self.counters.get("pagination_failures", 0),
            "warc_files": self.counters.get("warc_files", 0),
            "targets": list(self.target_status.values()),
        }

    def _comments_stated(self) -> Optional[int]:
        total = 0
        found = False
        for post in self.archive.posts.values():
            count = post.comments_count if isinstance(post, InstagramPost) \
                else post.get("comments_count")
            if isinstance(count, int):
                total += count
                found = True
        return total if found else None

    # -- the run --------------------------------------------------------------
    def run(self) -> dict:
        self.archive.event("capture_created", mode=self.config.mode,
                           targets=[t.url for t in self.targets],
                           continuation_of=self.config.continuation_of)
        if not self._establish_viewer():
            self.state = STOPPED
            self.phase_detail = self._closing_summary()
            self.archive.finalise(self.manifest_document(final=True),
                                  self._checkpoint_document())
            self._report(force=True)
            return {"stop_reason": self.stop_reason, "stop_rule": self.stop_rule,
                    "posts": 0, "comments": 0, "media": 0}

        for target in self.targets:
            if self._stop_requested:
                break
            self.current_target = target
            status = self.target_status[target.key]
            status["status"] = "running"
            self.phase_detail = f"Collecting {target.label}."
            self._report(force=True)
            try:
                if target.kind == "profile":
                    self._capture_profile(target)
                else:
                    self._capture_single(target)
                if not self._stop_requested:
                    status["status"] = "done"
            except TargetUnavailable as exc:
                if str(exc) == "stopped":
                    status["status"] = "interrupted"
                    break
                status["status"] = "unavailable"
                status["reason"] = str(exc)
                self.counters["targets_unavailable"] += 1
                self.archive.event("target_unavailable", target=target.url,
                                   reason=str(exc))
            except InstagramError as exc:
                status["status"] = "failed"
                status["reason"] = str(exc)
                self.counters["targets_failed"] += 1
                self.archive.event("target_failed", target=target.url,
                                   error=str(exc))
            finally:
                self._checkpoint()
        self.current_target = None
        if (self.config.write_warc and self.rendered_pass is not None
                and self.archive.posts and not self._stop_requested):
            # Context, not the record: how Instagram presented what was
            # collected, captured after the collection itself is safe.
            self.phase_detail = "Recording how Instagram presents the captured posts."
            self._report(force=True)
            urls = [t.url for t in self.targets if t.kind == "profile"] + [
                (p.permalink_url if isinstance(p, InstagramPost)
                 else p.get("permalink_url")) for p in self.archive.posts.values()]
            try:
                self.counters["warc_files"] = int(self.rendered_pass(
                    [u for u in urls if u]) or 0)
            except Exception as exc:
                self.counters["rendered_pass_failed"] += 1
                self.archive.event("rendered_pass_failed", error=str(exc))
                log.warning("Rendered pass failed: %s", exc)
        if not self._stop_requested:
            self.stop_reason = self.stop_reason or "targets_complete"
            self.stop_rule = self.stop_rule or "every_target_worked"
        self.state = STOPPED
        self.phase_detail = self._closing_summary()
        manifest = self.manifest_document(final=True)
        self.archive.finalise(manifest, self._checkpoint_document())
        self._report(force=True)
        try:
            self.persist(targets=self.newest_by_target,
                         posts=[self.archive._post_row(p) for p in
                                self.archive.posts.values()],
                         profiles=self.archive.profiles)
        except Exception as exc:
            log.warning("Could not persist capture state: %s", exc)
        return {"stop_reason": self.stop_reason, "stop_rule": self.stop_rule,
                "posts": len(self.archive.posts),
                "comments": len(self.archive.comments),
                "media": len(self.archive.media_index)}

    def _establish_viewer(self) -> bool:
        """Make sure there is a signed-in session before asking anything.

        Instagram answers an anonymous client with 401 "please wait a few
        minutes" and 429, not with "login required", so waiting for it to
        say sign in would mean waiting for a rate limit instead. The browser
        opens for the curator first, on Instagram's own sign-in page, and the
        session is read from the profile once they continue. A curator who
        continues still signed out has decided; the run goes on, and the
        manifest says what that bounds. Returns False on Stop.
        """
        for attempt in range(3):
            try:
                self.viewer_username = self.client.viewer()
            except InstagramError as exc:
                log.info("Viewer unknown: %s", exc)
                self.viewer_username = None
            if self.viewer_username:
                break
            still = " still" if attempt else ""
            held = self._hold_for_curator(
                f"The capture browser is{still} not signed in to Instagram. "
                "Sign in in the window that has opened -- use a dedicated "
                "institutional account, not a personal one -- then select "
                "\u201cI have resolved it \u2014 continue\u201d. SWM reads "
                "the session from that browser profile; it never sees the "
                "password.",
                "https://www.instagram.com/accounts/login/")
            if not held:
                return False
            self._refresh_client()
            if attempt == 2:
                self.archive.event("proceeding_signed_out")
        self.archive.event("viewer", username=self.viewer_username,
                           signed_in=bool(self.viewer_username))
        self._report(force=True)
        return True

    def _closing_summary(self) -> str:
        posts = len(self.archive.posts)
        comments = len(self.archive.comments)
        media = len(self.archive.media_index)
        because = {
            "targets_complete": "every target was worked",
            "curator_stop": "you selected Stop and save",
        }.get(self.stop_reason or "", str(self.stop_reason or "").replace("_", " "))
        return (f"Collected {posts} post{'s' if posts != 1 else ''}, "
                f"{comments} comment{'s' if comments != 1 else ''}, "
                f"{media} media file{'s' if media != 1 else ''}. "
                f"Stopped because {because}.")

    # -- a single post or reel ------------------------------------------------
    def _capture_single(self, target: InstagramTarget) -> None:
        post = self._with_retries(
            lambda: self.client.post(target.shortcode or ""),
            target.url, f"reading {target.label}")
        assert isinstance(post, InstagramPost)
        post.surface = "direct"
        self.counters["posts_encountered"] += 1
        self.target_status[target.key]["posts_encountered"] += 1
        self._take_post(post, target)

    # -- a profile --------------------------------------------------------------
    def _capture_profile(self, target: InstagramTarget) -> None:
        username = target.username or ""
        profile = self._with_retries(
            lambda: self.client.profile(username), target.url,
            f"reading {target.label}")
        assert isinstance(profile, InstagramProfile)
        self.archive.add_profile(profile)
        if profile.is_private and not self.viewer_username:
            raise TargetUnavailable(
                f"{target.label} is private, and the capture browser is not "
                "signed in to an account that follows it.")
        seen: set[str] = set()
        for surface in self.config.surfaces:
            if self._stop_requested:
                return
            iterator = (self.client.profile_posts(username) if surface == "posts"
                        else self.client.profile_reels(username))
            self._walk_surface(target, surface, iterator, seen)

    def _walk_surface(self, target: InstagramTarget, surface: str,
                      iterator: Iterator[InstagramPost], seen: set[str]) -> None:
        """Walk one of a profile's surfaces, applying the stopping rule.

        Pinned posts are recognised by order (see mark_pinned_by_order), which
        needs a small lookahead, so posts are taken through a short buffer.
        """
        status = self.target_status[target.key]
        buffer: deque[InstagramPost] = deque()
        consecutive_older = 0
        selected_non_pinned = 0
        exhausted = False
        stalled = 0
        head_marked = False
        prior = self.config.prior_newest.get(target.key) or {}

        def pull() -> bool:
            nonlocal exhausted
            if exhausted:
                return False
            try:
                post = self._with_retries(
                    lambda: next(iterator), target.url,
                    f"listing {target.label} {surface}")
            except TargetUnavailable:
                raise
            except StopIteration:
                exhausted = True
                return False
            if isinstance(post, InstagramPost):
                post.surface = surface
                buffer.append(post)
                return True
            exhausted = True
            return False

        while True:
            if self._stop_requested:
                return
            # keep a lookahead so pinned posts can be recognised by order
            while len(buffer) < _MAX_PINNED + 3 and pull():
                pass
            if not buffer:
                break
            if not head_marked:
                mark_pinned_by_order(list(buffer))
                head_marked = True
            post = buffer.popleft()
            self._check_control()
            if self._stop_requested:
                return
            self.counters["posts_encountered"] += 1
            status["posts_encountered"] += 1

            if post.shortcode in seen:
                self.exclusions["duplicate_across_surfaces"] += 1
                continue
            seen.add(post.shortcode)

            decision = self._decide(post, prior, consecutive_older,
                                    selected_non_pinned)
            if decision == "select":
                self._take_post(post, target)
                if not post.is_pinned:
                    selected_non_pinned += 1
                    consecutive_older = 0
            elif decision == "older":
                self.exclusions["older_than_requested"] += 1
                if not post.is_pinned:
                    consecutive_older += 1
            elif decision == "newer":
                self.exclusions["newer_than_requested"] += 1
            elif decision == "already_captured":
                self.exclusions["captured_previously"] += 1
                if not post.is_pinned:
                    consecutive_older += 1

            if self._boundary_reached(consecutive_older, selected_non_pinned):
                return
        # the iterator ran dry
        if self.config.mode in ("until_stopped", "end_of_timeline"):
            self.archive.event("timeline_exhausted", target=target.url,
                               surface=surface,
                               posts_encountered=status["posts_encountered"])
            if self.config.mode == "end_of_timeline":
                self._request_stop("end_of_available_timeline",
                                   "no_further_posts_offered")

    def _decide(self, post: InstagramPost, prior: dict,
                consecutive_older: int, selected: int) -> str:
        mode = self.config.mode
        when = post.created_time or ""
        if mode == "date_range":
            if self.config.to_date and when and when > self.config.to_date:
                return "newer"
            if self.config.from_date and when and when < self.config.from_date:
                return "older"
            return "select"
        if mode == "latest_n":
            if post.is_pinned:
                return "select"
            return "select" if selected < self.config.latest_n else "older"
        if mode == "since_last":
            newest_id = str(prior.get("media_id") or "")
            newest_date = str(prior.get("date") or "")
            if post.media_id == newest_id:
                return "already_captured"
            if newest_date and when and when <= newest_date:
                return "already_captured"
            known = self.known_ids.get(self.current_target.key if
                                       self.current_target else "", set())
            if post.media_id in known:
                return "already_captured"
            return "select"
        return "select"        # until_stopped, end_of_timeline

    def _boundary_reached(self, consecutive_older: int, selected: int) -> bool:
        mode = self.config.mode
        if mode in ("date_range", "since_last"):
            if consecutive_older >= self.config.consecutive_older:
                self.archive.event("lower_boundary_reached",
                                   consecutive_older=consecutive_older)
                return True
        if mode == "latest_n" and selected >= self.config.latest_n:
            return True
        return False

    # -- one post's package -------------------------------------------------------
    def _take_post(self, post: InstagramPost, target: InstagramTarget) -> None:
        added = self.archive.add_post(post)
        if not added:
            return
        self.target_status[target.key]["posts_selected"] += 1
        self._track_newest(target, post)
        if self.config.capture_media:
            self._collect_media(post)
        if self.config.include_comments:
            self._collect_comments(post)
        self.phase_detail = (
            f"Collecting {target.label}: {len(self.archive.posts)} posts, "
            f"{len(self.archive.media_index)} media files"
            + (f", {len(self.archive.comments)} comments"
               if self.config.include_comments else "") + ".")
        self._report()
        if len(self.archive.posts) % 10 == 0:
            self._checkpoint()

    def _track_newest(self, target: InstagramTarget, post: InstagramPost) -> None:
        current = self.newest_by_target.get(target.key) or {}
        if post.is_pinned:
            return
        if not current or (post.created_time or "") > (current.get("date") or ""):
            self.newest_by_target[target.key] = {
                "media_id": post.media_id, "date": post.created_time,
                "username": target.username}

    def _collect_media(self, post: InstagramPost) -> None:
        for item in post.media:
            self.counters["media_expected"] += 1
            if item.url in self.archive.media_index:
                continue
            try:
                body, content_type = self._with_retries(
                    lambda: self.client.fetch(item.url), post.permalink_url or "",
                    f"downloading media for {post.shortcode}")
            except TargetUnavailable as exc:
                if str(exc) == "stopped":
                    return
                self.counters["media_failed"] += 1
                self.archive.event("media_failed", shortcode=post.shortcode,
                                   url=item.url, error=str(exc))
                continue
            except InstagramError as exc:
                self.counters["media_failed"] += 1
                self.archive.event("media_failed", shortcode=post.shortcode,
                                   url=item.url, error=str(exc))
                continue
            if not body:
                self.counters["media_failed"] += 1
                self.archive.event("media_failed", shortcode=post.shortcode,
                                   url=item.url, error="empty response")
                continue
            self.archive.save_media(item.url, body, content_type)

    def _collect_comments(self, post: InstagramPost) -> None:
        wanted = self.config.max_comments_per_post
        top_level = 0
        replies_for: Counter = Counter()
        raw_payloads: list[dict] = []
        try:
            iterator = self._with_retries(
                lambda: self.client.comments(post.shortcode,
                                             self.config.include_replies),
                post.permalink_url or "", f"reading comments on {post.shortcode}")
            assert isinstance(iterator, Iterator) or hasattr(iterator, "__iter__")
            for comment in iterator:      # type: ignore[union-attr]
                if self._stop_requested:
                    return
                if comment.depth == 0:
                    if top_level >= wanted:
                        self.exclusions["comment_limit_reached"] += 1
                        break
                    top_level += 1
                else:
                    if not self.config.include_replies:
                        self.exclusions["replies_not_requested"] += 1
                        continue
                    parent = comment.parent_comment_id or ""
                    if replies_for[parent] >= self.config.max_replies_per_comment:
                        self.exclusions["reply_limit_reached"] += 1
                        continue
                    replies_for[parent] += 1
                if self.archive.add_comment(comment):
                    if comment.raw:
                        raw_payloads.append(comment.raw)
        except TargetUnavailable as exc:
            if str(exc) == "stopped":
                return
            self.counters["comment_threads_failed"] += 1
            self.archive.event("comments_failed", shortcode=post.shortcode,
                               error=str(exc))
        except InstagramError as exc:
            self.counters["comment_threads_failed"] += 1
            self.archive.event("comments_failed", shortcode=post.shortcode,
                               error=str(exc))
        finally:
            self.archive.save_raw_comments(post.shortcode, raw_payloads)

    # -- checkpoint and manifest ------------------------------------------------
    def _checkpoint(self) -> None:
        try:
            _atomic_json(self.archive.checkpoint_path, self._checkpoint_document())
        except OSError as exc:
            log.warning("Checkpoint failed: %s", exc)

    def _checkpoint_document(self) -> dict:
        return {
            "schema": "swm-instagram-checkpoint-v1",
            "updated_at": _iso_now(),
            "crawl_id": self.crawl_id,
            "mode": self.config.mode,
            "targets": list(self.target_status.values()),
            "newest_by_target": self.newest_by_target,
            "phase": self._progress_details()["phase"],
            "message": self.phase_detail,
            "posts_exported": len(self.archive.posts),
            "comments_exported": len(self.archive.comments),
            "media_captured": len(self.archive.media_index),
            "stop_reason": self.stop_reason,
            "stop_rule": self.stop_rule,
        }

    def manifest_document(self, *, final: bool = False) -> dict:
        details = self._progress_details()
        stated = self._comments_stated()
        collected = len(self.archive.comments)
        return {
            "schema": "swm-instagram-capture-manifest-v1",
            "capture": {
                "crawl_id": self.crawl_id,
                "name": self.crawl_name,
                "operator": self.config.operator,
                "targets": [
                    {"url": t.url, "kind": t.kind, **self.target_status[t.key]}
                    for t in self.targets],
                "mode": self.config.mode,
                "parameters": {
                    "from": self.config.from_date, "to": self.config.to_date,
                    "latest_n": self.config.latest_n,
                    "consecutive_older_required": self.config.consecutive_older,
                    "surfaces": list(self.config.surfaces),
                    "capture_media": self.config.capture_media,
                    "include_comments": self.config.include_comments,
                    "maximum_comments_per_post": self.config.max_comments_per_post,
                    "include_replies": self.config.include_replies,
                    "maximum_replies_per_comment": self.config.max_replies_per_comment,
                    "write_warc": self.config.write_warc,
                },
                "viewer": ("signed_in" if self.viewer_username else "signed_out"),
                "continuation_of": self.config.continuation_of,
                "state": self.state,
                "final": final,
                "stop_reason": self.stop_reason,
                "stopping_rule_fired": self.stop_rule,
            },
            "layers": {
                "media": {
                    "path": "media/",
                    "meaning": "Original files at the highest resolution "
                               "Instagram served, content-addressed by "
                               "SHA-256; every carousel component in order.",
                },
                "raw": {
                    "path": "raw/",
                    "meaning": "Instagram's own payloads as received, before "
                               "any normalisation. The primary evidence.",
                },
                "normalised": {
                    "posts": ["instagram-posts.jsonl", "instagram-posts.csv"],
                    "comments": ["instagram-comments.jsonl",
                                 "instagram-comments.csv"],
                    "profiles": "instagram-profiles.json",
                    "selection_exclusions": dict(self.exclusions),
                },
                "web_context": {
                    "warc": "*.warc.gz" if self.counters.get("warc_files") else None,
                    "meaning": "Optional rendered capture of the same posts, "
                               "for how Instagram presented them. A rendering "
                               "requests only what it needs, so it is context, "
                               "not the record.",
                },
                "fixity": "checksums.sha256",
            },
            "counts": dict(self.counters) | {
                "posts_exported": len(self.archive.posts),
                "comments_exported": collected,
                "media_captured": len(self.archive.media_index),
                "targets_total": len(self.targets),
                "targets_done": details["targets_done"],
            },
            "coverage": {
                "newest_post": details["newest_post"],
                "oldest_post": details["oldest_post"],
                "comments_stated_on_posts": stated,
                "comments_not_collected": (max(0, min(
                    stated, self.config.max_comments_per_post * len(self.archive.posts))
                    - collected) if stated is not None and
                    self.config.include_comments else None),
            },
            "completeness": {
                "claim": "This package holds all content accessible to this "
                         "session during this capture. It does not claim to "
                         "hold everything Instagram holds.",
                "signed_out_capture_meaning": (
                    "Signed out, Instagram serves little or nothing of a "
                    "profile. What is here is bounded by that, not by the "
                    "request.") if not self.viewer_username else None,
                "engagement_figures": "Likes, comment counts and view counts "
                                      "are observations at capture time.",
                "pinned_detection": "Instagram no longer marks pinned posts; "
                                    "they are recognised by appearing before "
                                    "newer posts, and never end a date-bounded "
                                    "capture on their own.",
            },
            "tools": {"swm_instagram": "1", "client": getattr(
                self.client, "version", "unknown")},
            "updated_at": _iso_now(),
        }


# ---------------------------------------------------------------------------
# Instaloader adapter
# ---------------------------------------------------------------------------

_SESSION_COOKIES = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did")


def _session_from_context(context) -> Optional[dict]:
    page = context.pages[0] if context.pages else context.new_page()
    try:
        user_agent = page.evaluate("navigator.userAgent")
    except Exception:
        user_agent = None
    cookies = {c["name"]: c["value"]
               for c in context.cookies("https://www.instagram.com/")
               if c.get("name") in _SESSION_COOKIES}
    if not cookies.get("sessionid"):
        return None
    return {"cookies": cookies, "user_agent": user_agent}


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_managed_profile(pw, user_data_dir: str | Path, headless: bool):
    """SWM's managed Chrome on the dedicated profile.

    The profile is written by Google Chrome when one is installed, so it is
    read with the same channel where possible; the bundled Chromium is the
    fallback on a machine without Chrome.
    """
    try:
        return pw.chromium.launch_persistent_context(
            str(user_data_dir), headless=headless, channel="chrome")
    except Exception:
        return pw.chromium.launch_persistent_context(
            str(user_data_dir), headless=headless)


def _launch_native_chrome(user_data_dir: str | Path, headless: bool,
                          chrome_path: Optional[str], url: str = "about:blank"):
    """The system's own Chrome on the dedicated profile, attached over CDP.

    Returns (process, cdp_port). Chrome ignores --remote-debugging-port on
    its default profile, so a dedicated one is always used; the port is
    chosen fresh so an Instagram job never collides with a recording or a
    Facebook capture that owns another.
    """
    import subprocess

    from .browser import _CAPTURE_ARGS, _find_chrome

    chrome = _find_chrome(chrome_path)
    port = _free_port()
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run", "--no-default-browser-check", *_CAPTURE_ARGS]
    if headless:
        args.append("--headless=new")
    try:
        import os
        if os.name == "posix" and os.geteuid() == 0:
            # Chrome exits at once as root without this; a container is the
            # one place SWM runs as root, and the only place it is needed.
            args.append("--no-sandbox")
    except AttributeError:
        pass
    args.append(url)
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    return process, port


def _close_native_chrome(port: int, process, browser=None) -> None:
    """Shut the system's Chrome down the way it shuts itself down.

    A terminated Chrome has not necessarily written its cookies: they are
    flushed on an orderly shutdown, and a SIGTERM is not one. Killing it after
    the curator signed in would lose the very session the sign-in was for, so
    Chrome is asked to close over CDP and given time to finish before force
    is used.
    """
    from playwright.sync_api import sync_playwright

    try:
        if browser is not None:
            browser.new_browser_cdp_session().send("Browser.close")
        else:
            with sync_playwright() as pw:
                attached = pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}")
                attached.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        process.wait(timeout=15)
        return
    except Exception:
        pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except Exception:
        process.kill()


def _wait_for_cdp(port: int, process, timeout: float = 20.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Chrome exited immediately (code {process.returncode}); "
                "is the profile already in use by another Chrome?")
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1.0):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Chrome's CDP endpoint did not come up on port {port}.")


def read_session_from_profile(user_data_dir: str | Path,
                              headless: bool = True,
                              browser_mode: str = "headed",
                              chrome_path: Optional[str] = None) -> Optional[dict]:
    """Read the Instagram session out of the dedicated browser profile.

    The curator signed in once, in that browser. This opens the profile
    without a window -- SWM's managed Chrome, or the system's own Chrome over
    CDP, whichever the job chose -- copies the session cookies and the exact
    user agent, and closes it again. The cookies live in this process for the
    job and are written nowhere. Returns None when the profile holds no
    session.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if browser_mode == "native":
            process, port = _launch_native_chrome(user_data_dir, headless,
                                                  chrome_path)
            browser = None
            try:
                _wait_for_cdp(port, process)
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = (browser.contexts[0] if browser.contexts
                           else browser.new_context())
                return _session_from_context(context)
            finally:
                _close_native_chrome(port, process, browser)
        context = _launch_managed_profile(pw, user_data_dir, headless)
        try:
            return _session_from_context(context)
        finally:
            context.close()


def open_profile_for_curator(user_data_dir: str | Path, url: str,
                             browser_mode: str = "headed",
                             chrome_path: Optional[str] = None) -> Callable[[], None]:
    """Show the dedicated profile to the curator on one page.

    Returns what closes the window again. In native mode the window is the
    system's own Chrome, which the curator may prefer for signing in; in
    headed mode it is SWM's managed Chrome.
    """
    if browser_mode == "native":
        process, port = _launch_native_chrome(user_data_dir, False,
                                              chrome_path, url)
        _wait_for_cdp(port, process)

        def close_native():
            _close_native_chrome(port, process)
        return close_native

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        context = _launch_managed_profile(pw, user_data_dir, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pw.stop()
        raise

    def close_managed():
        try:
            context.close()
        finally:
            pw.stop()
    return close_managed


def _make_rate_controller(instaloader_module):
    base = instaloader_module.RateController

    class HandBack(base):  # type: ignore[misc, valid-type]
        """Never sleep out a 429 inside the library; raise it instead."""

        def handle_429(self, query_type: str) -> None:
            raise instaloader_module.exceptions.TooManyRequestsException(
                "Instagram responded with HTTP 429 (too many requests).")

        def sleep(self, secs: float) -> None:
            # Pacing between queries stays, but bounded: a long sleep here is
            # invisible to the dashboard and deaf to Stop.
            super().sleep(min(float(secs), 15.0))

    return HandBack


class _HandBackRateController:
    """Placeholder resolved at build time, once instaloader is imported."""

    def __new__(cls, context):
        import instaloader
        return _make_rate_controller(instaloader)(context)


class _RetryingIterator:
    """An iterator whose next() can be asked again after it raised."""

    def __init__(self, start: Callable[[], Iterator], guard: Callable,
                 convert: Callable):
        self._start = start
        self._guard = guard
        self._convert = convert
        self._inner: Optional[Iterator] = None

    def __iter__(self):
        return self

    def __next__(self):
        if self._inner is None:
            self._inner = self._start()
        item = self._guard(lambda: next(self._inner))
        return self._convert(item)


class InstaloaderClient:
    """Instaloader behind the engine's client protocol.

    Errors are translated into the engine's own conditions. Every record
    carries Instaloader's node dict as ``raw`` -- that is Instagram's
    payload, not Instaloader's interpretation of it.
    """

    def __init__(self, session: Optional[dict] = None,
                 profile_dir: Optional[str | Path] = None,
                 browser_mode: str = "headed",
                 chrome_path: Optional[str] = None):
        import instaloader
        self._instaloader = instaloader
        self.version = getattr(instaloader, "__version__", "unknown")
        self.profile_dir = profile_dir
        self.browser_mode = browser_mode
        self.chrome_path = chrome_path
        self._session = session
        self.loader = None
        self._build()

    def _build(self) -> None:
        il = self._instaloader
        user_agent = (self._session or {}).get("user_agent")
        # Instaloader's own rate controller sleeps for many minutes inside
        # the library when Instagram answers 429, where Stop cannot reach it
        # and the dashboard cannot say what is happening. This one hands the
        # condition back to the engine, which waits it out while still
        # answering Stop and reporting the countdown.
        self.loader = il.Instaloader(
            sleep=True, quiet=True, user_agent=user_agent,
            download_pictures=False, download_videos=False,
            download_video_thumbnails=False, download_comments=False,
            save_metadata=False, compress_json=False, iphone_support=True,
            max_connection_attempts=1,
            rate_controller=lambda context: _HandBackRateController(context))
        cookies = (self._session or {}).get("cookies") or {}
        self.signed_in = bool(cookies.get("sessionid"))
        if self.signed_in:
            self.loader.load_session(cookies.get("ds_user_id") or "", cookies)

    def refresh(self) -> None:
        """Re-read the session after the curator signed in or resolved a hold."""
        if self.profile_dir:
            self._session = read_session_from_profile(
                self.profile_dir, browser_mode=self.browser_mode,
                chrome_path=self.chrome_path)
        self._build()

    # -- translation ----------------------------------------------------------
    def _guard(self, action: Callable[[], object]) -> object:
        ex = self._instaloader.exceptions
        try:
            return action()
        except ex.TooManyRequestsException as exc:
            raise RateLimited(600.0, str(exc)) from exc
        except ex.LoginRequiredException as exc:
            raise LoginRequired(str(exc)) from exc
        except ex.TwoFactorAuthRequiredException as exc:
            raise CheckpointRequired(str(exc)) from exc
        except ex.PrivateProfileNotFollowedException as exc:
            raise TargetUnavailable(
                "This profile is private and the signed-in account does not "
                "follow it.") from exc
        except ex.ProfileNotExistsException as exc:
            raise TargetUnavailable("This profile does not exist.") from exc
        except ex.QueryReturnedNotFoundException as exc:
            raise TargetUnavailable("Instagram reports it does not exist.") from exc
        except ex.QueryReturnedForbiddenException as exc:
            raise LoginRequired(str(exc)) from exc
        except ex.QueryReturnedBadRequestException as exc:
            text = str(exc).lower()
            if "checkpoint" in text or "challenge" in text:
                raise CheckpointRequired(str(exc)) from exc
            raise InstagramError(str(exc)) from exc
        except ex.ConnectionException as exc:
            text = str(exc).lower()
            if "wait a few minutes" in text or "429" in text or "401" in text:
                raise RateLimited(600.0, str(exc)) from exc
            if "checkpoint" in text or "challenge" in text:
                raise CheckpointRequired(str(exc)) from exc
            if "login" in text and "required" in text:
                raise LoginRequired(str(exc)) from exc
            raise InstagramError(str(exc)) from exc
        except ex.InstaloaderException as exc:
            raise InstagramError(str(exc)) from exc

    # -- the protocol ---------------------------------------------------------
    def viewer(self) -> Optional[str]:
        if not self.signed_in:
            # An anonymous test_login is a request Instagram answers with
            # "please wait a few minutes": there is nothing to learn from it.
            return None
        return self._guard(lambda: self.loader.test_login())  # type: ignore[return-value]

    def profile(self, username: str) -> InstagramProfile:
        il = self._instaloader

        def read():
            profile = il.Profile.from_username(self.loader.context, username)
            node = dict(getattr(profile, "_node", {}) or {})
            return InstagramProfile(
                user_id=str(profile.userid), username=profile.username,
                full_name=profile.full_name, biography=profile.biography,
                is_private=bool(profile.is_private),
                is_verified=bool(profile.is_verified),
                followers_count=profile.followers,
                following_count=profile.followees,
                posts_count=profile.mediacount,
                profile_pic_url=profile.profile_pic_url,
                external_url=profile.external_url, raw=node)
        return self._guard(read)  # type: ignore[return-value]

    def _iterate(self, make_iterator: Callable[[], Iterable]) -> Iterator[InstagramPost]:
        # Not a generator: a generator that raises is finished, and the engine
        # retries next() after a rate limit or a hold. Instaloader's
        # NodeIterator fetches lazily in __next__, so asking it again is a
        # fresh request rather than a dead end.
        return _RetryingIterator(
            lambda: iter(self._guard(make_iterator)),   # type: ignore[arg-type]
            self._guard, self._post_record)

    def profile_posts(self, username: str) -> Iterator[InstagramPost]:
        il = self._instaloader
        return self._iterate(lambda: il.Profile.from_username(
            self.loader.context, username).get_posts())

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        il = self._instaloader
        return self._iterate(lambda: il.Profile.from_username(
            self.loader.context, username).get_reels())

    def post(self, shortcode: str) -> InstagramPost:
        il = self._instaloader
        return self._post_record(self._guard(
            lambda: il.Post.from_shortcode(self.loader.context, shortcode)))

    def _post_record(self, post) -> InstagramPost:
        typename = str(getattr(post, "typename", "") or "")
        is_video = bool(getattr(post, "is_video", False))
        media: list[MediaItem] = []
        kind = "image"
        if typename == "GraphSidecar":
            kind = "carousel"
            for index, node in enumerate(self._guard(
                    lambda: list(post.get_sidecar_nodes()))):  # type: ignore[union-attr]
                media.append(MediaItem(
                    url=node.video_url if node.is_video else node.display_url,
                    kind="video" if node.is_video else "image",
                    position=index,
                    thumbnail_url=node.display_url if node.is_video else None))
        elif is_video:
            kind = "reel" if "clips" in json.dumps(
                getattr(post, "_node", {}) or {}).lower() else "video"
            media.append(MediaItem(url=post.video_url, kind="video",
                                   thumbnail_url=post.url))
        else:
            media.append(MediaItem(url=post.url, kind="image"))
        when = getattr(post, "date_utc", None)
        created = (when.replace(tzinfo=timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
            if isinstance(when, datetime) else None)
        node = dict(getattr(post, "_node", {}) or {})
        return InstagramPost(
            media_id=str(post.mediaid), shortcode=post.shortcode,
            owner_username=getattr(post, "owner_username", None),
            owner_id=str(getattr(post, "owner_id", "") or "") or None,
            kind=kind, created_time=created,
            caption=getattr(post, "caption", None),
            permalink_url=f"https://www.instagram.com/p/{post.shortcode}/",
            likes_count=getattr(post, "likes", None),
            comments_count=getattr(post, "comments", None),
            video_view_count=(getattr(post, "video_view_count", None)
                              if is_video else None),
            media=media, raw=node)

    def comments(self, shortcode: str,
                 include_replies: bool) -> Iterator[InstagramComment]:
        il = self._instaloader
        post = self._guard(lambda: il.Post.from_shortcode(
            self.loader.context, shortcode))
        iterator = iter(self._guard(lambda: post.get_comments()))  # type: ignore[union-attr]
        while True:
            try:
                comment = self._guard(lambda: next(iterator))
            except StopIteration:
                return
            yield self._comment_record(comment, shortcode, None, 0)
            if include_replies and getattr(comment, "answers", None):
                answers = iter(comment.answers)
                while True:
                    try:
                        answer = self._guard(lambda: next(answers))
                    except StopIteration:
                        break
                    yield self._comment_record(answer, shortcode,
                                               str(comment.id), 1)

    def _comment_record(self, comment, shortcode: str,
                        parent: Optional[str], depth: int) -> InstagramComment:
        owner = getattr(comment, "owner", None)
        when = getattr(comment, "created_at_utc", None)
        created = (when.replace(tzinfo=timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
            if isinstance(when, datetime) else None)
        return InstagramComment(
            comment_id=str(comment.id), post_shortcode=shortcode,
            parent_comment_id=parent,
            author_id=str(getattr(owner, "userid", "") or "") or None,
            author_username=getattr(owner, "username", None),
            text=getattr(comment, "text", None), created_time=created,
            likes_count=getattr(comment, "likes_count", None), depth=depth,
            raw={"id": comment.id, "text": getattr(comment, "text", None),
                 "created_at_utc": created,
                 "owner": getattr(owner, "username", None),
                 "likes_count": getattr(comment, "likes_count", None),
                 "parent_comment_id": parent})

    def fetch(self, url: str) -> tuple[bytes, str]:
        def read():
            response = self.loader.context.get_raw(url)
            return response.content, response.headers.get("Content-Type", "")
        return self._guard(read)  # type: ignore[return-value]
