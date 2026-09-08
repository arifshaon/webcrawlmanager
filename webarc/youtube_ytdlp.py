"""YouTube through yt-dlp, as a library.

yt-dlp is the tool the field maintains against YouTube. It lists a
channel's tabs and a playlist, reads a video's metadata and comments, and
downloads the best rendition within the resolution the curator allowed,
muxing the separate video and audio streams YouTube serves. Everything it
returns is the tool's reading of YouTube, kept whole under
``evidence/yt-dlp/`` and labelled as such; the browser collector is the
one that keeps YouTube's own responses.

YouTube answers anonymous per-video requests from many networks with
"Sign in to confirm you're not a bot". That is raised as LoginRequired;
the engine holds for the curator to sign in in the capture browser and
lends the session here as a temporary cookie file that is deleted when
the run ends and never enters the package. The listing of a channel's
tabs needs no session.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from .youtube import (DownloadInterrupted, LoginRequired, RateLimited, TargetUnavailable,
                      YouTubeChannel, YouTubeComment, YouTubeError, YouTubePlaylist,
                      YouTubeTarget, YouTubeVideo)

log = logging.getLogger(__name__)

_TAB_OF_SURFACE = {"videos": "videos", "shorts": "shorts", "streams": "streams"}
_KIND_OF_SURFACE = {"videos": "video", "shorts": "short", "streams": "stream"}
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".opus", ".mp3")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_SUBTITLE_EXTENSIONS = (".vtt", ".srt", ".ttml", ".json3", ".srv3", ".srv2", ".srv1", ".ass")


def ytdlp_version() -> Optional[str]:
    try:
        import yt_dlp
    except ImportError:
        return None
    return getattr(getattr(yt_dlp, "version", None), "__version__", None) or "installed"


def _tool(name: str) -> Optional[str]:
    """A helper program on the PATH, or under SWM's own tools directory
    (``SWM_TOOLS_DIR``, which the installer's launchers set), where an
    installation without administrator rights can keep it."""
    found = shutil.which(name)
    if found:
        return found
    tools_dir = os.environ.get("SWM_TOOLS_DIR")
    if tools_dir:
        for candidate in (Path(tools_dir) / name, Path(tools_dir) / name / name,
                          Path(tools_dir) / name / "bin" / name):
            for suffix in ("", ".exe"):
                path = candidate.with_name(candidate.name + suffix)
                if path.is_file():
                    return str(path)
    return None


def ffmpeg_path() -> Optional[str]:
    return _tool("ffmpeg")


def js_runtime() -> Optional[tuple[str, str]]:
    """The JavaScript runtime yt-dlp can use here: deno first, then node."""
    for name in ("deno", "node"):
        found = _tool(name)
        if found:
            return name, found
    return None


def single_file_selector(format_selector: str) -> str:
    """The format expression for a machine without ffmpeg.

    YouTube serves its better renditions as separate video and audio
    streams that only ffmpeg can join; without it, yt-dlp would leave two
    files. This asks for the best rendition that already carries both,
    within the same height bound, so a download is still one playable file
    (720p or less on most videos).
    """
    match = re.search(r"height<=(\d+)", format_selector)
    bound = f"[height<={match.group(1)}]" if match else ""
    return (f"best{bound}[vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]"
            f"/best{bound}/best")


def po_token_provider_available() -> bool:
    try:
        import importlib
        importlib.import_module("yt_dlp_plugins.extractor.getpot_bgutil_script")
        return True
    except Exception:
        return False


def _translate(exc: BaseException) -> YouTubeError:
    """yt-dlp's message as the engine's condition."""
    text = str(exc)
    lowered = text.lower()
    if "not a bot" in lowered or "sign in to confirm" in lowered:
        return LoginRequired("YouTube asked to confirm the request is not from a bot.")
    if "sign in" in lowered and ("age" in lowered or "confirm your age" in lowered):
        return LoginRequired("YouTube requires a signed-in session for this age-restricted video.")
    if "private video" in lowered:
        return TargetUnavailable("YouTube reports this video is private.", "private")
    if "members-only" in lowered or "join this channel" in lowered or "membership" in lowered:
        return TargetUnavailable("YouTube reports this content is for channel members only.",
                                 "subscriber_only")
    if "premium" in lowered and "only" in lowered:
        return TargetUnavailable("YouTube reports this content is for Premium members.",
                                 "premium_only")
    if "comments are turned off" in lowered or "disabled comments" in lowered:
        return TargetUnavailable("Comments are disabled on this item.", "unavailable")
    if "429" in text or "too many requests" in lowered:
        return RateLimited(300.0, "YouTube answered 429 Too Many Requests.")
    if "needs to be reloaded" in lowered or "please reload" in lowered:
        return RateLimited(60.0, "YouTube asked for the page to be reloaded.")
    if ("has been removed" in lowered or "video unavailable" in lowered
            or "is not available" in lowered or "does not exist" in lowered
            or "no longer available" in lowered or "was deleted" in lowered):
        return TargetUnavailable(text.split("\n")[0][:300], "unavailable")
    if "unsupported url" in lowered or "no video" in lowered:
        return TargetUnavailable(text.split("\n")[0][:300], "unavailable")
    return YouTubeError(text.split("\n")[0][:300])


def _iso_from(info: dict) -> Optional[str]:
    stamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    day = info.get("upload_date") or info.get("release_date")
    if isinstance(day, str) and len(day) == 8 and day.isdigit():
        return f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00Z"
    return None


def _int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _availability(entry: dict) -> str:
    stated = entry.get("availability")
    if isinstance(stated, str) and stated:
        return stated
    title = str(entry.get("title") or "")
    if title in ("[Private video]",):
        return "private"
    if title in ("[Deleted video]",):
        return "deleted"
    return "unknown"


def video_from_info(info: dict, *, surface: str = "videos", source: str = "yt-dlp",
                    provenance: Optional[dict] = None, trimmed_raw: bool = True,
                    complete: bool = False) -> YouTubeVideo:
    """A video record from a yt-dlp info dict, flat or full."""
    kind = _KIND_OF_SURFACE.get(surface, "video")
    if info.get("live_status") in ("was_live", "is_live", "post_live"):
        kind = "stream"
    elif "/shorts/" in str(info.get("url") or info.get("webpage_url") or ""):
        kind = "short"
    thumbs = info.get("thumbnails") if isinstance(info.get("thumbnails"), list) else []
    best_thumb = None
    for thumb in thumbs:
        if isinstance(thumb, dict) and thumb.get("url"):
            if best_thumb is None or (thumb.get("width") or 0) > (best_thumb.get("width") or 0):
                best_thumb = thumb
    raw = dict(info)
    if trimmed_raw:
        for heavy in ("formats", "requested_formats", "requested_downloads", "automatic_captions",
                      "subtitles", "thumbnails", "comments", "heatmap", "_format_sort_fields",
                      "http_headers", "url"):
            raw.pop(heavy, None)
    video_id = str(info.get("id") or "")
    return YouTubeVideo(
        video_id=video_id,
        channel_id=info.get("channel_id") or info.get("uploader_id"),
        channel_handle=(str(info.get("uploader_id") or "").lstrip("@") or None)
        if str(info.get("uploader_id") or "").startswith("@") else None,
        channel_name=info.get("channel") or info.get("uploader"),
        title=info.get("title"),
        description=info.get("description"),
        published_time=_iso_from(info),
        duration_seconds=_int(info.get("duration")),
        kind=kind,
        live_status=info.get("live_status"),
        availability=_availability(info),
        view_count=_int(info.get("view_count")),
        like_count=_int(info.get("like_count")),
        comment_count=_int(info.get("comment_count")),
        url=info.get("webpage_url") or info.get("original_url")
        or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
        thumbnail_url=(best_thumb or {}).get("url") or info.get("thumbnail"),
        categories=[c for c in (info.get("categories") or []) if isinstance(c, str)],
        tags=[t for t in (info.get("tags") or []) if isinstance(t, str)],
        chapters=[c for c in (info.get("chapters") or []) if isinstance(c, dict)],
        surface=surface, source=source, complete=complete, raw=raw,
        provenance=dict(provenance or {}))


def subtitle_languages(info: dict, *, captions: bool, auto_captions: bool, live_chat: bool
                       ) -> tuple[list[str], set[str]]:
    """Which subtitle tracks to ask for, by name, and which are automatic.

    "all" is the wrong request: YouTube's automatic captions include
    machine translations into some 170 languages, and asking for every one
    is 170 requests per video that YouTube answers with 429. A video's
    manual tracks are few and wanted; of the automatic ones only the
    original language (yt-dlp names it ``<lang>-orig``) is the video's
    own. Without a record to read from, only the original-language
    automatic track and the live chat can be named.
    """
    manual = {lang for lang in (info.get("subtitles") or {}) if lang != "live_chat"}
    automatic = info.get("automatic_captions") or {}
    wanted: list[str] = sorted(manual) if captions else []
    auto: set[str] = set()
    if auto_captions:
        original = sorted(lang for lang in automatic if lang.endswith("-orig"))
        if original:
            auto = set(original)
        elif not info:
            auto = {".*-orig"}
        else:
            language = info.get("language")
            if language and language in automatic and language not in manual:
                auto = {language}
    wanted += sorted(auto)
    if live_chat and (not info or "live_chat" in (info.get("subtitles") or {})):
        wanted.append("live_chat")
    return wanted, auto


def comments_from_info(info: dict, target_id: str, *, provenance: Optional[dict] = None
                       ) -> list[YouTubeComment]:
    """yt-dlp's comment list as the one comment model.

    yt-dlp's ``parent`` is "root" for a top-level comment and the parent's
    id for a reply; YouTube nests replies one level deep, so the thread
    root of a reply is its parent.
    """
    found: list[YouTubeComment] = []
    for entry in info.get("comments") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        parent = entry.get("parent")
        parent_id = None if parent in (None, "", "root") else str(parent)
        stamp = entry.get("timestamp")
        when = (datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")
                .replace("+00:00", "Z") if isinstance(stamp, (int, float)) and stamp > 0 else None)
        found.append(YouTubeComment(
            comment_id=str(entry["id"]), target_type="video", target_id=target_id,
            parent_id=parent_id, thread_root_id=parent_id or str(entry["id"]),
            reply_depth=1 if parent_id else 0,
            author_name=entry.get("author"),
            author_channel_id=entry.get("author_id"),
            author_is_uploader=bool(entry.get("author_is_uploader")),
            text=entry.get("text"), published_time=when,
            published_text=entry.get("_time_text") or entry.get("time_text"),
            like_count=_int(entry.get("like_count")),
            is_pinned=bool(entry.get("is_pinned")),
            is_favorited=bool(entry.get("is_favorited")),
            source="yt-dlp", raw=entry, provenance=dict(provenance or {})))
    return found


def netscape_cookie_lines(cookies: list[dict]) -> str:
    """The browser's cookies in the format yt-dlp reads."""
    lines = ["# Netscape HTTP Cookie File", "# Written by SWM for one capture; deleted after."]
    for cookie in cookies:
        name, value = cookie.get("name"), cookie.get("value")
        if not name:
            continue
        domain = str(cookie.get("domain") or "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires")
        try:
            expiry = int(expires) if expires and float(expires) > 0 else 0
        except (TypeError, ValueError):
            expiry = 0
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        lines.append("\t".join([prefix + domain, include_subdomains, path, secure,
                                str(expiry), str(name), str(value or "")]))
    return "\n".join(lines) + "\n"


class _Listing:
    """A lazy channel-tab or playlist enumeration the engine can close."""

    def __init__(self, entries, translate: Callable[[BaseException], YouTubeError],
                 make: Callable[[dict], YouTubeVideo]):
        self._entries = iter(entries)
        self._translate = translate
        self._make = make
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> YouTubeVideo:
        if self.closed:
            raise StopIteration
        try:
            entry = next(self._entries)
        except StopIteration:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc
        while not isinstance(entry, dict):
            entry = next(self._entries)
        return self._make(entry)

    def close(self) -> None:
        self.closed = True
        close = getattr(self._entries, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class YtDlpClient:
    """Channel tabs, playlists, videos, downloads and video comments through yt-dlp.

    ``ydl_factory`` builds a ``YoutubeDL`` from an options dict; a test hands
    in a stand-in. ``evidence_sink(name, payload)`` receives each video's full
    info dict and returns the package path it was kept under.
    """

    version = "yt-dlp"

    def __init__(self, *, format_selector: str = "bestvideo*[height<=1080]+bestaudio/best",
                 capture_media: bool = True, thumbnails: bool = True, captions: bool = True,
                 auto_captions: bool = True, live_chat: bool = True,
                 comments: bool = True, max_comments: int = 1000, comment_sort: str = "new",
                 include_replies: bool = True,
                 evidence_sink: Optional[Callable[[str, object], str]] = None,
                 scratch_dir: Optional[Path] = None,
                 po_token_script: Optional[str] = None,
                 ydl_factory: Optional[Callable[[dict], object]] = None):
        self.format_selector = format_selector
        self.capture_media = capture_media
        self.thumbnails = thumbnails
        self.captions = captions
        self.auto_captions = auto_captions
        self.live_chat = live_chat
        self.want_comments = comments
        self.max_comments = max_comments
        self.comment_sort = comment_sort
        self.include_replies = include_replies
        self.evidence_sink = evidence_sink
        self.scratch_dir = Path(scratch_dir) if scratch_dir else None
        self.po_token_script = po_token_script
        self._ydl_factory = ydl_factory
        self._cookie_file: Optional[Path] = None
        self._comments: dict[str, list[YouTubeComment]] = {}
        self._infos: dict[str, dict] = {}
        self.anomalies: list[dict] = []
        self.last_fetch_via = "yt-dlp"
        self.signed_in = False

    # -- yt-dlp ------------------------------------------------------------------
    def _factory(self):
        if self._ydl_factory is not None:
            return self._ydl_factory
        try:
            import yt_dlp
        except ImportError as exc:
            raise YouTubeError("yt-dlp is not installed. Install it with: pip install yt-dlp") from exc
        return yt_dlp.YoutubeDL

    def _options(self, **extra) -> dict:
        options: dict = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "logger": _YtDlpLogger(), "ignoreerrors": False, "retries": 3,
            "extractor_args": {"youtube": {
                "comment_sort": [self.comment_sort],
                "max_comments": [str(self.max_comments), "all",
                                 "all" if self.include_replies else "0",
                                 "all" if self.include_replies else "0"],
            }},
        }
        if self.po_token_script:
            options["extractor_args"]["youtubepot-bgutilscript"] = {
                "script_path": [self.po_token_script]}
        runtime = js_runtime()
        if runtime is not None:
            # yt-dlp solves YouTube's player challenges in a JavaScript
            # runtime and fetches its solver scripts from its own releases
            options["js_runtimes"] = {runtime[0]: {"path": runtime[1]}}
            options["remote_components"] = ["ejs:github"]
        ffmpeg = ffmpeg_path()
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg
        if self._cookie_file is not None:
            options["cookiefile"] = str(self._cookie_file)
        options.update(extra)
        return options

    @property
    def effective_format_selector(self) -> str:
        """The curator's selector, or its single-file form without ffmpeg."""
        if ffmpeg_path():
            return self.format_selector
        return single_file_selector(self.format_selector)

    def _ydl(self, **extra):
        return self._factory()(self._options(**extra))

    def tool_report(self) -> dict:
        runtime = js_runtime()
        ffmpeg = ffmpeg_path()
        return {"yt_dlp": ytdlp_version(), "ffmpeg": ffmpeg,
                "js_runtime": f"{runtime[0]} ({runtime[1]})" if runtime else None,
                "po_token_provider": bool(self.po_token_script) or po_token_provider_available(),
                "format_selector": self.effective_format_selector,
                "merging": "ffmpeg" if ffmpeg else
                "unavailable: no ffmpeg, so single-file renditions were requested"}

    # -- the session lent by the browser ------------------------------------------
    def use_cookies(self, cookies: list[dict]) -> None:
        """Write the browser's cookies to a private temporary file for yt-dlp.

        The file lives outside the package, is readable by the current user
        only, and is deleted by ``forget_cookies`` when the run ends.
        """
        self.forget_cookies()
        base = self.scratch_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix="swm-youtube-", suffix=".cookies", dir=str(base))
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(netscape_cookie_lines(cookies))
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        self._cookie_file = Path(name)
        self.signed_in = True

    def forget_cookies(self) -> None:
        if self._cookie_file is not None:
            try:
                self._cookie_file.unlink()
            except OSError:
                pass
            self._cookie_file = None

    # -- the protocol ----------------------------------------------------------------
    def channel(self, target: YouTubeTarget) -> YouTubeChannel:
        url = target.url.rstrip("/") + "/videos"
        try:
            with self._ydl(extract_flat=True, playlistend=1) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise _translate(exc) from exc
        if not isinstance(info, dict):
            raise TargetUnavailable(f"YouTube served nothing for {target.label}.", "unavailable")
        thumbs = [t for t in (info.get("thumbnails") or []) if isinstance(t, dict)]
        avatar = next((t.get("url") for t in thumbs if str(t.get("id") or "") == "avatar_uncropped"), None) \
            or next((t.get("url") for t in thumbs if "avatar" in str(t.get("id") or "")), None)
        banner = next((t.get("url") for t in thumbs if "banner" in str(t.get("id") or "")), None)
        channel_id = str(info.get("channel_id") or info.get("id") or "")
        if not channel_id:
            raise TargetUnavailable(f"YouTube served no channel id for {target.label}.", "unavailable")
        handle = str(info.get("uploader_id") or "")
        raw = {k: v for k, v in info.items() if k not in ("entries", "requested_entries", "thumbnails")}
        return YouTubeChannel(
            channel_id=channel_id, handle=handle.lstrip("@") or target.handle,
            name=info.get("channel") or info.get("uploader") or info.get("title"),
            url=info.get("channel_url") or info.get("uploader_url") or target.url,
            description=info.get("description"),
            subscriber_count=_int(info.get("channel_follower_count")),
            video_count=_int(info.get("playlist_count")),
            avatar_url=avatar, banner_url=banner,
            source="yt-dlp", raw=raw,
            provenance={"source": "yt-dlp", "listing_url": url})

    def playlist(self, target: YouTubeTarget) -> YouTubePlaylist:
        try:
            with self._ydl(extract_flat=True, playlistend=1) as ydl:
                info = ydl.extract_info(target.url, download=False)
        except Exception as exc:
            raise _translate(exc) from exc
        if not isinstance(info, dict):
            raise TargetUnavailable(f"YouTube served nothing for {target.label}.", "unavailable")
        raw = {k: v for k, v in info.items() if k not in ("entries", "requested_entries")}
        return YouTubePlaylist(
            playlist_id=str(info.get("id") or target.playlist_id), title=info.get("title"),
            channel_id=info.get("channel_id"), channel_name=info.get("channel") or info.get("uploader"),
            description=info.get("description"), item_count=_int(info.get("playlist_count")),
            url=info.get("webpage_url") or target.url, source="yt-dlp", raw=raw,
            provenance={"source": "yt-dlp"})

    def list_items(self, target: YouTubeTarget, surface: str) -> Iterator[YouTubeVideo]:
        if target.kind == "playlist" or surface == "playlist":
            url = target.url
        else:
            url = target.url.rstrip("/") + "/" + _TAB_OF_SURFACE.get(surface, "videos")
        try:
            ydl = self._ydl(extract_flat=True, lazy_playlist=True)
            info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise _translate(exc) from exc
        entries = (info or {}).get("entries") if isinstance(info, dict) else None
        if entries is None:
            entries = [info] if isinstance(info, dict) and info.get("id") else []

        def make(entry: dict) -> YouTubeVideo:
            return video_from_info(entry, surface=surface,
                                   provenance={"source": "yt-dlp", "listing_url": url,
                                               "listing_position": entry.get("playlist_index")})
        return _Listing(entries, _translate, make)

    def video(self, video_id: str) -> YouTubeVideo:
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with self._ydl(getcomments=self.want_comments) as ydl:
                info = ydl.extract_info(url, download=False)
                info = ydl.sanitize_info(info)
        except Exception as exc:
            raise _translate(exc) from exc
        if not isinstance(info, dict) or not info.get("id"):
            raise TargetUnavailable(f"YouTube served no record for video {video_id}.", "unavailable")
        evidence = None
        if self.evidence_sink is not None:
            try:
                evidence = self.evidence_sink(str(info["id"]), info)
            except Exception as exc:
                log.warning("Could not keep yt-dlp's output for %s: %s", video_id, exc)
        provenance = {"source": "yt-dlp", "evidence": evidence,
                      "evidence_type": "tool-derived metadata", "verbatim_platform_response": False}
        self._infos[str(info["id"])] = info
        self._comments[str(info["id"])] = comments_from_info(info, str(info["id"]),
                                                              provenance=provenance)
        return video_from_info(info, provenance=provenance, complete=True)

    def comments(self, video: YouTubeVideo) -> Iterator[YouTubeComment]:
        found = self._comments.get(video.video_id)
        if found is None:
            self.video(video.video_id)
            found = self._comments.get(video.video_id, [])
        return iter(found)

    def comments_more(self) -> Optional[bool]:
        return None

    def download(self, video: YouTubeVideo, dest: Path,
                 on_progress: Callable[[dict], None]) -> list[dict]:
        """Write the video's files under ``dest`` and describe each one."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        interrupted: dict = {}

        def hook(status: dict) -> None:
            if status.get("status") != "downloading":
                return
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            try:
                on_progress({
                    "downloaded_bytes": int(done),
                    "total_bytes": int(total) if total else None,
                    "percent": round(done / total * 100.0, 1) if total else None,
                    "speed": status.get("speed"), "eta": status.get("eta"),
                    "filename": os.path.basename(str(status.get("filename") or "")),
                })
            except DownloadInterrupted as exc:
                interrupted["reason"] = str(exc)
                try:
                    from yt_dlp.utils import DownloadCancelled
                except ImportError:       # a stand-in downloader is in use
                    raise
                raise DownloadCancelled(str(exc)) from exc

        languages, automatic = subtitle_languages(
            self._infos.get(video.video_id) or {}, captions=self.captions,
            auto_captions=self.auto_captions, live_chat=self.live_chat)
        self._automatic_languages = automatic
        options = {
            "format": self.effective_format_selector,
            "outtmpl": {"default": str(dest / "%(id)s.%(ext)s")},
            "paths": {"home": str(dest)},
            "skip_download": not self.capture_media,
            "writethumbnail": self.thumbnails,
            "writesubtitles": bool(languages),
            "writeautomaticsub": bool(automatic),
            "subtitleslangs": languages,
            "continuedl": True, "nooverwrites": False, "overwrites": False,
            "progress_hooks": [hook],
            "noplaylist": True,
        }
        try:
            with self._ydl(**options) as ydl:
                info = ydl.extract_info(video.url or f"https://www.youtube.com/watch?v={video.video_id}",
                                        download=True)
                info = ydl.sanitize_info(info) if isinstance(info, dict) else {}
        except Exception as exc:
            if interrupted:
                raise DownloadInterrupted(interrupted["reason"]) from exc
            name = type(exc).__name__
            if name == "DownloadCancelled":
                raise DownloadInterrupted("stopped") from exc
            raise _translate(exc) from exc
        return self._describe_files(dest, info or {}, video)

    def _describe_files(self, dest: Path, info: dict, video: YouTubeVideo) -> list[dict]:
        downloads = [d for d in (info.get("requested_downloads") or []) if isinstance(d, dict)]
        main = downloads[0] if downloads else {}
        resolution = None
        if main.get("height"):
            resolution = f"{main.get('width') or '?'}x{main.get('height')}"
        elif info.get("height"):
            resolution = f"{info.get('width') or '?'}x{info.get('height')}"
        found: list[dict] = []
        for path in sorted(dest.iterdir()):
            if not path.is_file() or path.name.endswith((".part", ".ytdl", ".tmp")):
                continue
            suffix = path.suffix.lower()
            name = path.name
            entry: dict
            if name.endswith(".live_chat.json"):
                entry = {"role": "live_chat", "meaning": "live-chat replay as yt-dlp wrote it"}
            elif suffix in _VIDEO_EXTENSIONS:
                entry = {"role": "video", "resolution": resolution,
                         "format_id": main.get("format_id") or info.get("format_id"),
                         "format": main.get("format") or info.get("format"),
                         "vcodec": main.get("vcodec") or info.get("vcodec"),
                         "acodec": main.get("acodec") or info.get("acodec"),
                         "requested_variant": self.effective_format_selector,
                         "meaning": "best rendition YouTube served within the allowed "
                                    "resolution, muxed by yt-dlp; not the upload"}
            elif suffix in _IMAGE_EXTENSIONS:
                entry = {"role": "thumbnail"}
            elif suffix in _SUBTITLE_EXTENSIONS or ".live_chat" in name:
                lang = name[len(video.video_id) + 1:-len(suffix)] if name.startswith(video.video_id + ".") else None
                automatic = bool(lang) and (lang.endswith("-orig") or lang in getattr(
                    self, "_automatic_languages", set()))
                entry = {"role": "captions", "language": lang, "automatic": automatic,
                         "meaning": ("YouTube's automatic captions in the video's original "
                                     "language" if automatic else "a caption track the uploader published")}
            else:
                entry = {"role": "other"}
            entry["path"] = str(path)
            found.append(entry)
        return found

    def fetch(self, url: str) -> tuple[bytes, str]:
        """A small file (a thumbnail, an avatar) through yt-dlp's networking."""
        try:
            with self._ydl() as ydl:
                response = ydl.urlopen(url)
                body = response.read()
                content_type = ""
                try:
                    content_type = response.headers.get("content-type", "")
                except Exception:
                    pass
        except Exception as exc:
            raise _translate(exc) from exc
        self.last_fetch_via = "yt-dlp"
        return body, content_type

    # posts are not yt-dlp's
    def posts(self, channel: YouTubeChannel):
        raise TargetUnavailable("yt-dlp does not read the Posts tab; a browser does.", "unknown")

    def post_comments(self, post):
        raise TargetUnavailable("yt-dlp does not read post comments; a browser does.", "unknown")


class _YtDlpLogger:
    """yt-dlp's messages into SWM's log, at their own levels."""

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            return
        log.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        log.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        log.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        log.error("yt-dlp: %s", message)
