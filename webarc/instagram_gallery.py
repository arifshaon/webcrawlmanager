"""Listing a profile with gallery-dl, through the browser's own session.

The browser collector lists a profile by scrolling it and reading what
Instagram serves its own client. gallery-dl lists it the way Instaloader
did: it asks the per-user endpoint directly, in order, with pinned flags
and dates, using the signed-in browser's cookies lent to it in a private
temporary file for the length of the call. Where that is the listing the
curator wants, this module answers the listing part of the client protocol
and leaves everything else -- media requested by the page, comments, the
WARC, the raw responses -- to the browser client it wraps.

gallery-dl is an optional install and a separate program: it is run as a
subprocess with the user's own gallery-dl configuration ignored and every
setting SWM depends on given explicitly, and it is never imported. Its
output is read as it streams, one post at a time, so the engine's stopping
rules apply as they do to a scrolled listing and the program is stopped the
moment the engine has what it asked for. When Instagram refuses it part
way, the cursor gallery-dl reports is kept and the listing resumes from
there after the engine's wait, never from the first post again.

What gallery-dl writes is not Instagram's response: it is gallery-dl's own
reading of one, transformed. It is kept as such, under evidence/listings/,
apart from the browser's raw responses, and every post listed this way
says so in its provenance.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .instagram import (InstagramError, InstagramPost, LoginRequired, MediaItem,
                        RateLimited, TargetUnavailable)
from .redaction import redact_body

log = logging.getLogger(__name__)

GALLERY_DL_MODULE = "gallery_dl"
SCRATCH_PREFIX = "swm-gallery-dl-"
_CURSOR_RE = re.compile(r"Cursor: ([^'\"\s]+)|-o cursor=([^'\"\s]+)")
# Instagram's API answering a signed-out client: a redirect to the home page
_SIGNED_OUT_RE = re.compile(r'"(?:GET|POST) /api/v1/[^"]*" 302\b')


def gallery_dl_version() -> Optional[str]:
    try:
        return importlib.metadata.version("gallery-dl")
    except importlib.metadata.PackageNotFoundError:
        return None


def discovery_limit(mode: str, latest_n: Optional[int]) -> Optional[int]:
    """How far to list for a mode; None lists until the engine stops pulling.

    A profile can lead with pinned posts, so latest-N reads a buffer beyond
    N and lets the engine choose. The other modes have their own stopping
    rules, and the listing is stopped when the engine stops asking.
    """
    if mode == "latest_n" and latest_n:
        return max(latest_n + 12, latest_n * 2)
    return None


# ---------------------------------------------------------------------------
# The session, lent to gallery-dl
# ---------------------------------------------------------------------------

def lend_cookies(cookies: Iterable[dict]) -> list[dict]:
    """The browser's Instagram cookies, all of them.

    A session cookie alone is not accepted: Instagram ties a session to the
    browser it was made in through its identifying cookies (datr among
    them), and a client presenting the session without them is answered as
    signed out. Lending only the session's own cookies was tried and was
    refused; the whole jar, as the browser presents it, is accepted.
    """
    return [c for c in cookies if c.get("name") and c.get("value") is not None]


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


def clear_stale_scratch(scratch_dir: Path) -> int:
    """Remove lent-cookie folders a killed run left behind."""
    removed = 0
    try:
        for entry in Path(scratch_dir).glob(SCRATCH_PREFIX + "*"):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    except OSError:
        pass
    return removed


def gallery_command(cookie_file: Path, url: str, limit: Optional[int],
                    cursor: Optional[str] = None,
                    module: str = GALLERY_DL_MODULE) -> list[str]:
    """The gallery-dl command, with every setting SWM relies on stated.

    The user's own gallery-dl configuration is ignored so a listing means
    the same on every machine; output streams as JSON Lines; the log is
    verbose so the cursor after each page is reported; Instagram's refusals
    are not retried inside gallery-dl, since the engine waits and resumes.
    """
    command = [sys.executable, "-m", module, "--config-ignore", "--no-input",
               "-v", "-C", str(cookie_file), "-j",
               "-o", "output.jsonl=true", "-o", "output.private=false",
               "-o", "output.ascii=false",
               "-o", "extractor.instagram.api=rest",
               "-o", "extractor.instagram.pinned=true",
               "-o", "extractor.instagram.videos=true",
               "-o", "extractor.instagram.previews=false",
               "-o", "extractor.instagram.retries=0",
               "-o", "extractor.instagram.sleep-429=0"]
    if limit:
        command += ["-o", f"extractor.instagram.max-posts={limit}"]
    if cursor:
        command += ["-o", f"cursor={cursor}"]
    return command + [url]


def _elide_cookie_path(command: list[str]) -> list[str]:
    shown = list(command)
    if "-C" in shown:
        shown[shown.index("-C") + 1] = "<lent cookies>"
    return shown


# ---------------------------------------------------------------------------
# Reading gallery-dl's messages
# ---------------------------------------------------------------------------

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


def post_from_gallery(record: dict, username: str, provenance: dict) -> InstagramPost:
    """A post from gallery-dl's post-level message (its kind 2)."""
    code = str(record.get("post_shortcode"))
    pinned = record.get("pinned")
    likes = record.get("likes")
    comments = next((record[k] for k in ("comment_count", "comments_count")
                     if isinstance(record.get(k), int)), None)
    return InstagramPost(
        media_id=str(record.get("post_id") or code), shortcode=code,
        owner_username=str(record.get("username") or username),
        owner_id=str(record.get("owner_id")) if record.get("owner_id") else None,
        kind="image",
        created_time=_when(record.get("post_date") or record.get("date")),
        caption=(record.get("description") or None),
        permalink_url=str(record.get("post_url") or f"https://www.instagram.com/p/{code}/"),
        likes_count=likes if isinstance(likes, int) else None,
        comments_count=comments,
        is_pinned=bool(pinned) if isinstance(pinned, (list, bool)) else False,
        source="gallery-dl", raw={"gallery_dl": record, "gallery_dl_media": []},
        provenance=dict(provenance))


def add_gallery_file(post: InstagramPost, record: dict) -> bool:
    """Attach one of gallery-dl's file messages (its kind 3) to its post."""
    url = record.get("video_url") or record.get("display_url")
    if not url or any(m.url == url for m in post.media):
        return False
    number = record.get("num")
    position = (int(number) - 1) if isinstance(number, int) and number > 0 \
        else len(post.media)
    post.media.append(MediaItem(
        url=str(url), kind="video" if record.get("video_url") else "image",
        position=position, width=record.get("width"), height=record.get("height"),
        thumbnail_url=record.get("display_url") if record.get("video_url") else None))
    post.raw["gallery_dl_media"].append(record)
    return True


def settle_kind(post: InstagramPost) -> InstagramPost:
    post.media.sort(key=lambda m: m.position)
    if len(post.media) > 1:
        post.kind = "carousel"
    elif post.media and post.media[0].kind == "video":
        kind = str((post.raw.get("gallery_dl") or {}).get("type") or "")
        post.kind = "reel" if "reel" in kind.lower() or "clip" in kind.lower() else "video"
    return post


def posts_from_gallery_lines(lines: Iterable[str], username: str,
                             provenance: Optional[dict] = None) -> list[InstagramPost]:
    """All posts in a recorded JSON Lines output, in gallery-dl's order."""
    posts: list[InstagramPost] = []
    current: Optional[InstagramPost] = None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, list) or not message or not isinstance(message[-1], dict):
            continue
        record = message[-1]
        if message[0] == 2 and record.get("post_shortcode"):
            if current is not None:
                posts.append(settle_kind(current))
            current = post_from_gallery(record, username,
                                        {**(provenance or {}), "line": number})
        elif message[0] == 3 and current is not None \
                and record.get("post_shortcode") == current.shortcode:
            add_gallery_file(current, record)
    if current is not None:
        posts.append(settle_kind(current))
    return posts


# ---------------------------------------------------------------------------
# One listing: a gallery-dl process read as it writes
# ---------------------------------------------------------------------------

def _start_process(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)


class _StreamingListing:
    """Posts from gallery-dl as it writes them, resumable from its cursor.

    Each pull hands over the next complete post. The process is started on
    the first pull, stopped when the listing is closed -- the engine closes
    a listing it stops pulling from -- and, when Instagram refused it part
    way, restarted from the last cursor it reported on the pull after the
    engine's wait. Posts already handed over are not handed over again.
    """

    def __init__(self, client: "GalleryListingClient", username: str, url: str,
                 surface: str):
        self.client = client
        self.username = username
        self.url = url
        self.surface = surface
        self.process: Optional[subprocess.Popen] = None
        self.cursor: Optional[str] = None
        self.resumed_from: list[str] = []
        self.handed: set[str] = set()
        self.listed = 0
        self.lines = 0
        self.done = False
        self.closed = False
        self._pending: Optional[InstagramPost] = None
        self._stderr_tail: deque = deque(maxlen=400)
        self._stderr_thread: Optional[threading.Thread] = None
        self._scratch: Optional[Path] = None
        self.evidence = None
        self.commands: list[list[str]] = []
        self.outcome: Optional[str] = None
        self._deferred_status: Optional[int] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._cursor_history: list[str] = []
        self._buffer: list[InstagramPost] = []     # complete posts held over a pause
        self._suspended = False

    # -- the process --------------------------------------------------------
    def _start(self) -> None:
        opener = self.client._evidence_opener
        if self.evidence is None and opener is not None:
            try:
                self.evidence = opener("gallery-dl")
            except Exception as exc:
                log.warning("Could not open listing evidence: %s", exc)
        if self._scratch is None:
            root = Path(self.client.scratch_dir)
            root.mkdir(parents=True, exist_ok=True)
            self._scratch = root / f"{SCRATCH_PREFIX}{os.getpid()}-{int(time.time() * 1000)}"
            self._scratch.mkdir(mode=0o700)
        # The cookies are lent afresh for every start: after a sign-in the
        # curator made, or a wait, the browser's session is not what it was.
        cookies = lend_cookies(self.client.inner.cookie_jar()
                               if hasattr(self.client.inner, "cookie_jar") else [])
        write_netscape_cookies(cookies, self._scratch / "cookies.txt")
        command = gallery_command(self._scratch / "cookies.txt", self.url,
                                  self.client.limit, self.cursor, self.client.module)
        self.commands.append(command)
        self.client.commands.append(command)
        self.process = self.client.launcher(command)
        self._stderr_tail.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self.process,), daemon=True)
        self._stderr_thread.start()
        self._lines = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, args=(self.process, self._lines), daemon=True)
        self._stdout_thread.start()
        self._note("started", command=_elide_cookie_path(command),
                   resumed_from_cursor=self.cursor)

    def _drain_stdout(self, process: subprocess.Popen, lines: "queue.Queue") -> None:
        # Read on a thread so the pull can keep answering the curator's
        # controls while gallery-dl is waiting on Instagram.
        try:
            for line in process.stdout:
                lines.put(line)
        except Exception:
            pass
        finally:
            lines.put(None)

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr:
                self._stderr_tail.append(line.rstrip("\n"))
                match = _CURSOR_RE.search(line)
                if match:
                    self.cursor = match.group(1) or match.group(2)
                    self._cursor_history.append(self.cursor)
                if self.evidence is not None:
                    self.evidence.log(line)
        except Exception:
            pass

    def _finish_process(self) -> int:
        process = self.process
        self.process = None
        if process is None:
            return 0
        try:
            status = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            status = process.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=5)
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
        return status

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._finish_process()

    def suspend(self) -> None:
        """The curator paused: stop gallery-dl now, keep what it listed.

        Nothing further is requested from Instagram until the curator
        resumes. Posts already complete in the pipe are held over; the
        pending one is dropped and the resumed run starts from the cursor
        before the last one reported, so the page it was on is listed again
        -- one page's cost -- and posts already handed over are not handed
        over twice.
        """
        if self.process is None or self.closed:
            return
        self._stop_process()
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            self.lines += 1
            if self.evidence is not None:
                self.evidence.write_line(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, list) and message and isinstance(message[-1], dict):
                try:
                    completed = self._take(message)
                except InstagramError:
                    continue
                if completed is not None and completed.shortcode not in self.handed:
                    self._buffer.append(completed)
        self._pending = None
        self.cursor = self._cursor_history[-2] if len(self._cursor_history) >= 2 else None
        self._cursor_history = [self.cursor] if self.cursor else []
        self._suspended = True
        self.outcome = None
        self._note("paused", cursor=self.cursor, held=len(self._buffer),
                   posts=self.listed)

    def _note(self, event: str, **details) -> None:
        if self.evidence is not None:
            try:
                self.evidence.note(event, **details)
            except Exception:
                pass

    # -- reading ------------------------------------------------------------
    def _read_message(self) -> Optional[list]:
        """The next message from gallery-dl, or None at the end of output.

        While gallery-dl is waiting on Instagram, the curator's controls are
        consulted every half second: a pause holds here, a stop ends the
        listing and the program at once.
        """
        assert self.process is not None
        while True:
            try:
                line = self._lines.get(timeout=0.5)
            except queue.Empty:
                self.client.tick()               # a pause holds in here
                if self.client.stopping():
                    self._stop_process()
                    self.outcome = "stopped_by_curator"
                    raise TargetUnavailable("stopped")
                if self._suspended:
                    return None
                continue
            if line is None:
                return None
            self.lines += 1
            if self.evidence is not None:
                self.evidence.write_line(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, list) and message and isinstance(message[-1], dict):
                return message

    def _take(self, message: list) -> Optional[InstagramPost]:
        """Feed one message; returns a post once it is complete."""
        record = message[-1]
        completed = None
        if message[0] == 2 and record.get("post_shortcode"):
            completed = self._pending
            self._pending = post_from_gallery(record, self.username, self._provenance())
        elif message[0] == 3 and self._pending is not None \
                and record.get("post_shortcode") == self._pending.shortcode:
            add_gallery_file(self._pending, record)
        elif message[0] == -1:
            raise InstagramError(f"gallery-dl: {record.get('error')}: {record.get('message')}")
        return settle_kind(completed) if completed is not None else None

    def _provenance(self) -> dict:
        return {
            "listing_source": "gallery-dl",
            "gallery_dl_version": gallery_dl_version(),
            "listing_evidence": self.evidence.ref if self.evidence is not None else None,
            "line": self.lines,
            "instagram_raw_response_available": False,
            "decoder": "gallery-dl", "listing_request": True,
            "connection": "gallery-dl", "listing_user": self.username,
            "response": None,
        }

    def _signed_out(self) -> bool:
        return any(_SIGNED_OUT_RE.search(line) for line in self._stderr_tail)

    def _failed(self, status: int) -> None:
        """Raise the engine's condition for how gallery-dl ended."""
        text = "\n".join(self._stderr_tail).lower()
        self._note("failed", status=status, cursor=self.cursor,
                   log_tail=list(self._stderr_tail)[-20:])
        if "429" in text or "too many requests" in text or "rate limit" in text \
                or "please wait" in text:
            raise RateLimited(120.0, "Instagram rate-limited gallery-dl"
                              + (f" (resuming from cursor {self.cursor})" if self.cursor else ""))
        if "401" in text or "login required" in text or "not logged in" in text \
                or "authentication" in text:
            raise LoginRequired("gallery-dl was not accepted as signed in")
        if "404" in text or "does not exist" in text or "not found" in text:
            raise TargetUnavailable("gallery-dl: the profile could not be read")
        raise InstagramError(f"gallery-dl exited with {status}: "
                             + " | ".join(list(self._stderr_tail)[-3:]))

    def __iter__(self):
        return self

    def _hand(self, completed: InstagramPost) -> InstagramPost:
        self.handed.add(completed.shortcode)
        self.listed += 1
        completed.surface = self.surface
        return completed

    def __next__(self) -> InstagramPost:
        while True:
            if self.done or self.closed:
                raise StopIteration
            if self.process is None:
                if self._buffer:
                    held = self._buffer.pop(0)
                    if held.shortcode in self.handed:
                        continue
                    return self._hand(held)
                if self._deferred_status is not None:
                    # the last run failed after a post the engine has now
                    # had; its refusal is raised here, and the resumed run
                    # begins after that post
                    status, self._deferred_status = self._deferred_status, None
                    self._failed(status)
                self._pending = None
                if self.listed and self.cursor:
                    self.resumed_from.append(self.cursor)
                self._start()
            message = self._read_message()
            if message is None and self._suspended:
                self._suspended = False          # paused, not finished
                continue
            if message is None:
                status = self._finish_process()
                errored = any("error" in line.lower() for line in self._stderr_tail)
                if status == 0 and not errored and self.listed == 0 \
                        and self._pending is None and self._signed_out():
                    # gallery-dl was answered as signed out and ended with
                    # nothing: not an empty profile, a refused session
                    self._note("failed", status=status, reason="signed_out",
                               log_tail=list(self._stderr_tail)[-20:])
                    raise LoginRequired(
                        "Instagram answered gallery-dl's listing request with "
                        "a sign-out redirect: the browser's session was not "
                        "accepted from it. Sign in again in the browser, then "
                        "continue; the next run is lent the fresh session.")
                if status != 0 or errored:
                    # a post complete when the run failed -- its files come
                    # right after it, before the next page is asked for --
                    # goes to the engine first; the failure waits a pull
                    if self._pending is not None and self._pending.shortcode not in self.handed:
                        completed, self._pending = settle_kind(self._pending), None
                        self._deferred_status = status
                        return self._hand(completed)
                    self._failed(status)          # raises; process stays None
                completed = settle_kind(self._pending) if self._pending is not None else None
                self._pending = None
                self.done = True
                self.outcome = "exhausted"
                self._note("finished", outcome="exhausted", posts=self.listed,
                           lines=self.lines)
                if completed is not None and completed.shortcode not in self.handed:
                    return self._hand(completed)
                raise StopIteration
            try:
                completed = self._take(message)
            except InstagramError:
                self._stop_process()
                raise
            if completed is None or completed.shortcode in self.handed:
                continue
            return self._hand(completed)

    def close(self) -> None:
        """Stop gallery-dl; the engine has what it asked for."""
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            self._stop_process()
            self.outcome = self.outcome or "stopped_by_engine"
            self._note("finished", outcome=self.outcome, posts=self.listed,
                       lines=self.lines, cursor=self.cursor)
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
        if self.evidence is not None:
            try:
                self.evidence.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The client: gallery-dl for the listing, the browser for the rest
# ---------------------------------------------------------------------------

class GalleryListingClient:
    """The browser client, with profile listings answered by gallery-dl.

    ``inner`` is the browser client (any object answering the client
    protocol with a ``cookie_jar()``). ``launcher`` starts a command and
    returns a process with piped output; ``module`` is the Python module run
    as gallery-dl -- both injectable for tests.
    """

    version = "browser+gallery-dl"

    def __init__(self, inner, *, limit: Optional[int] = None,
                 scratch_dir: Optional[Path] = None,
                 launcher: Callable[[list[str]], subprocess.Popen] = _start_process,
                 module: str = GALLERY_DL_MODULE,
                 base_url: str = "https://www.instagram.com"):
        self.inner = inner
        self.limit = limit
        self.launcher = launcher
        self.module = module
        self.base_url = base_url.rstrip("/")
        self.scratch_dir = Path(scratch_dir) if scratch_dir else Path(
            os.environ.get("SWM_SCRATCH") or Path.home() / ".swm" / "tmp")
        clear_stale_scratch(self.scratch_dir)
        self._evidence_opener: Optional[Callable[[str], object]] = None
        self.commands: list[list[str]] = []
        self._active: Optional[_StreamingListing] = None
        # the engine's controls, consulted while a pull waits on gallery-dl
        self.tick: Callable[[], None] = lambda: None
        self.stopping: Callable[[], bool] = lambda: False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def record_responses_to(self, sink) -> None:
        forward = getattr(self.inner, "record_responses_to", None)
        if callable(forward):
            forward(sink)

    def attach_engine_controls(self, tick: Callable[[], None],
                               stopping: Callable[[], bool]) -> None:
        """``tick()`` honours pause and stop between units of work;
        ``stopping()`` says whether a stop was requested."""
        self.tick = tick
        self.stopping = stopping

    def record_listings_to(self, opener: Callable[[str], object]) -> None:
        """``opener(tool)`` gives a place in the package for one listing's
        evidence, with write_line(), log(), note() and close()."""
        self._evidence_opener = opener

    def suspend(self) -> None:
        """The curator paused: gallery-dl must stop asking Instagram."""
        if self._active is not None:
            self._active.suspend()

    def profile_posts(self, username: str) -> Iterator[InstagramPost]:
        return self._listing(username, f"{self.base_url}/{username}/posts/", "posts")

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        return self._listing(username, f"{self.base_url}/{username}/reels/", "reels")

    def _listing(self, username: str, url: str, surface: str) -> _StreamingListing:
        if self._active is not None:
            self._active.close()
        self._active = _StreamingListing(self, username, url, surface)
        return self._active

    def close(self) -> None:
        if self._active is not None:
            self._active.close()
            self._active = None
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()
