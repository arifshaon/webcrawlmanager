"""Collecting a channel's Posts tab through the browser YouTube is served to.

yt-dlp does not read the Posts tab. A Chrome on a dedicated profile, the
same profile the curator signs in to when yt-dlp is refused, opens the
tab the way a person does and this module reads what YouTube's own client
brings back: the first page embedded in the HTML as ``ytInitialData``,
the rest through ``youtubei/v1/browse`` continuations as the tab is
scrolled, a post's page and its comments through ``youtubei/v1/next``,
replies through the "View replies" control the page offers. SWM clicks
and scrolls; it never builds a ``youtubei`` request of its own.

What the browser observes is scoped to the navigation it was observed
under, as in the Instagram and X collectors, and every response a record
was read from can be handed to the package verbatim. Video streams are
never written to the WARC: the downloaded files are the objects.
"""
from __future__ import annotations

import base64
import logging
import random
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlsplit

from .browser import operator_launch_kwargs
from .config import BrowserConfig
from .instagram_browser import _PAGE_FETCH_JS
from .youtube import (CheckpointRequired, LoginRequired, RateLimited, TargetUnavailable,
                      YouTubeChannel, YouTubeComment, YouTubeError, YouTubePost, YouTubeTarget,
                      YouTubeVideo)
from .youtube_extract import (channel_from, comments_from, continuation_tokens,
                              describe_youtubei_request, iter_documents, posts_from,
                              read_initial_data)

log = logging.getLogger(__name__)

_YT_HOSTS = ("youtube.com", "youtu.be")
_MEDIA_HOST_MARKERS = ("ggpht.com", "ytimg.com", "googleusercontent.com")
# streams are the downloaded files' business, never the WARC's
_EXCLUDED_WARC_MARKERS = ("googlevideo.com",)
_EXCLUDED_WARC_PATHS = ("/videoplayback",)      # the stream itself, wherever it is served from
_SESSION_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-3PSID", "SID", "HSID", "SSID",
                    "APISID", "LOGIN_INFO", "__Secure-1PSID")


class _Observed:
    def __init__(self) -> None:
        self._posts_by_navigation: dict[int, OrderedDict[str, YouTubePost]] = {}
        self._comments_by_navigation: dict[int, OrderedDict[str, YouTubeComment]] = {}
        self._tokens_by_navigation: dict[int, list[str]] = {}
        self.channels: dict[str, YouTubeChannel] = {}
        self.queries_by_navigation: dict[int, list[str]] = {}
        self.endpoints: Counter = Counter()
        self.responses = 0
        self.api_responses = 0

    def take_posts(self, posts: list[YouTubePost], navigation: int) -> None:
        pool = self._posts_by_navigation.setdefault(navigation, OrderedDict())
        for post in posts:
            if post.post_id not in pool:
                pool[post.post_id] = post

    def take_comments(self, comments: list[YouTubeComment], navigation: int) -> None:
        pool = self._comments_by_navigation.setdefault(navigation, OrderedDict())
        for comment in comments:
            if comment.comment_id not in pool:
                pool[comment.comment_id] = comment

    def posts_in(self, navigation: int) -> "OrderedDict[str, YouTubePost]":
        return self._posts_by_navigation.get(navigation) or OrderedDict()

    def comments_in(self, navigation: int) -> "OrderedDict[str, YouTubeComment]":
        return self._comments_by_navigation.get(navigation) or OrderedDict()

    def tokens_in(self, navigation: int) -> list[str]:
        return self._tokens_by_navigation.get(navigation) or []


class YouTubeBrowserClient:
    """The Posts tab and post comments through a browser on a dedicated profile.

    Answers the parts of the client protocol yt-dlp does not; ``channel``
    is answered too, from the page, so a Posts-only run needs no yt-dlp.
    """

    version = "browser"

    def __init__(self, browser: BrowserConfig, warc=None,
                 sleep: Callable[[float], None] = time.sleep,
                 stall_rounds: int = 3, page_timeout: float = 60.0,
                 base_url: str = "https://www.youtube.com", headless: bool = False,
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
        self.current_url: Optional[str] = None
        self.navigation = 0
        self.exchanges_written = 0
        self.page_fetches = 0
        self.fallback_fetches = 0
        self._media_hosts: set[str] = set()
        self.anomalies: list[dict] = []
        self.last_fetch_via: Optional[str] = None
        self._response_sink: Optional[Callable[[dict, bytes], str]] = None
        self._awaited_url: Optional[str] = None
        self._awaited_seen = False
        self._last_fetch_error: Optional[str] = None
        self._rate_limited = False

    def record_responses_to(self, sink: Callable[[dict, bytes], str]) -> None:
        self._response_sink = sink

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "YouTubeBrowserClient":
        try:
            return self._start()
        except Exception:
            self.close()
            self._pw = self._context = self._native = self._page = None
            raise

    def _start(self) -> "YouTubeBrowserClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile = self.browser.user_data_dir or str(Path("./youtube-profile-swm").resolve())
        Path(profile).mkdir(parents=True, exist_ok=True)
        if self.browser.mode == "native":
            from .instagram import _launch_native_chrome, _wait_for_cdp
            self._native = _launch_native_chrome(profile, self.headless, self.browser.chrome_path)
            process, port = self._native
            try:
                _wait_for_cdp(port, process)
            except RuntimeError as exc:
                if "exited immediately" not in str(exc):
                    raise
                self._native = _launch_native_chrome(profile, self.headless,
                                                     self.browser.chrome_path, no_sandbox=True)
                process, port = self._native
                _wait_for_cdp(port, process)
            attached = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self._context = attached.contexts[0] if attached.contexts else attached.new_context()
        else:
            launch_kwargs: dict = {
                "headless": self.headless, **operator_launch_kwargs(),
                "no_viewport": not self.headless, "service_workers": "allow",
                "locale": "en-US",
            }
            if self.browser.proxy:
                launch_kwargs["proxy"] = {"server": self.browser.proxy}
            self._context = self._launch_managed(profile, launch_kwargs)
        self._context.on("response", self._on_response)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.refresh()
        return self

    def _launch_managed(self, profile: str, launch_kwargs: dict):
        attempts: list[dict] = []
        if self.browser.chrome_path:
            attempts.append({"executable_path": self.browser.chrome_path})
        attempts += [{"channel": "chrome"}, {}]
        failures: list[Exception] = []
        for extra in attempts:
            try:
                return self._pw.chromium.launch_persistent_context(profile, **extra, **launch_kwargs)
            except Exception as exc:
                failures.append(exc)
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
        names: set[str] = set()
        try:
            for cookie in self._context.cookies(self.base_url + "/"):
                names.add(str(cookie.get("name")))
        except Exception:
            pass
        self.signed_in = any(name in names for name in _SESSION_COOKIES)

    def viewer(self) -> Optional[str]:
        self.refresh()
        return "signed-in" if self.signed_in else None

    def cookie_jar(self) -> list[dict]:
        """The browser's YouTube cookies, for lending to the downloader."""
        try:
            return list(self._context.cookies([self.base_url + "/", "https://www.google.com/"]))
        except Exception:
            return []

    # -- what the browser loads ------------------------------------------------
    def _on_response(self, response) -> None:
        try:
            self._handle_response(response)
        finally:
            try:
                if response.url == self._awaited_url:
                    self._awaited_seen = True
            except Exception:
                pass

    def _is_ours(self, netloc: str, host: str) -> bool:
        return netloc == self.host or any(host == h or host.endswith("." + h) for h in _YT_HOSTS)

    def _handle_response(self, response) -> None:
        try:
            url = response.url
            netloc = (urlsplit(url).netloc or "").lower()
            host = urlsplit(url).hostname or ""
            ours = self._is_ours(netloc, host)
            media_host = netloc in self._media_hosts or any(m in host for m in _MEDIA_HOST_MARKERS)
            if not ours and not media_host:
                return
            if any(m in host for m in _EXCLUDED_WARC_MARKERS) or \
                    (urlsplit(url).path or "").startswith(_EXCLUDED_WARC_PATHS):
                return
            request = response.request
            body = b""
            wants_body = request.resource_type in ("document", "xhr", "fetch") or self.warc is not None
            if wants_body and not (300 <= response.status < 400):
                try:
                    body = response.body()
                except Exception:
                    body = b""
            self.observed.responses += 1
            if self.warc is not None:
                try:
                    self.warc.write_exchange(
                        url=url, method=request.method, req_headers=request.headers,
                        post_data=request.post_data_buffer or None, status=response.status,
                        status_text=response.status_text or "", resp_headers=response.headers,
                        body=body)
                    self.exchanges_written += 1
                except Exception as exc:
                    log.debug("WARC write skipped for %s: %s", url, exc)
            if not ours or not body:
                return
            navigation = self.navigation
            if response.status == 429:
                self._rate_limited = True
                return
            if request.resource_type == "document":
                documents = read_initial_data(body)
                asked = {"endpoint": "page", "browse_id": None, "continuation": None}
                decoder = "embedded"
            else:
                asked = describe_youtubei_request(url, request.post_data)
                if asked is None:
                    return
                self.observed.api_responses += 1
                self.observed.endpoints[asked["endpoint"]] += 1
                documents = list(iter_documents(body))
                decoder = "youtubei"
            if not documents:
                return
            self.observed.queries_by_navigation.setdefault(navigation, []).append(
                asked["endpoint"] + (" [continuation]" if asked.get("continuation") else ""))
            origin = {"url": url, "decoder": decoder, "navigation": navigation, "response": None,
                      "endpoint": asked["endpoint"], "browse_id": asked.get("browse_id"),
                      "continuation": asked.get("continuation")}
            if self._response_sink is not None:
                try:
                    origin["response"] = self._response_sink({
                        "url": url, "method": request.method, "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "resource_type": request.resource_type,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "navigation": navigation, "endpoint": asked["endpoint"],
                        "page_url": self.current_url, "source": "youtube",
                        "evidence_type": "observed HTTP response",
                    }, body)
                except Exception as exc:
                    log.warning("Could not keep the response for %s: %s", url, exc)
            channel = channel_from(documents, origin)
            if channel is not None and channel.channel_id not in self.observed.channels:
                self.observed.channels[channel.channel_id] = channel
            self.observed.take_posts(posts_from(documents, origin), navigation)
            post_id = _post_id_in(self.current_url or "")
            if post_id:
                self.observed.take_comments(comments_from(documents, post_id, "post", origin),
                                            navigation)
            self.observed._tokens_by_navigation[navigation] = continuation_tokens(documents)
        except Exception as exc:
            log.debug("Response handling failed: %s", exc)

    # -- navigation ------------------------------------------------------------
    def _local(self, url: str) -> str:
        """The address on the host this client talks to (a test's stand-in
        for youtube.com, or youtube.com itself)."""
        if url.startswith(self.base_url):
            return url
        parts = urlsplit(url)
        return self.base_url + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    def _goto(self, url: str) -> None:
        self.navigation += 1
        self.current_url = url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=int(self.page_timeout * 1000))
        except Exception as exc:
            raise YouTubeError(f"Could not open {url}: {exc}") from exc
        self._settle()
        self._check_page_state()

    def _check_page_state(self) -> None:
        if self._rate_limited:
            self._rate_limited = False
            raise RateLimited(120.0, "YouTube answered 429.")
        try:
            url = self._page.url
        except Exception:
            return
        lowered = url.lower()
        if "consent.youtube.com" in lowered or "consent.google" in lowered:
            raise CheckpointRequired("YouTube is asking for consent choices.")
        if "accounts.google.com" in lowered or "/signin" in lowered:
            raise LoginRequired("YouTube is showing its sign-in page.")
        if "/sorry/" in lowered:
            raise CheckpointRequired("YouTube is asking to confirm the request is not automated.")
        try:
            text = self._page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 6000)")
        except Exception:
            text = ""
        lowered_text = str(text).lower().replace("’", "'")
        if "this channel doesn't exist" in lowered_text or "this page isn't available" in lowered_text:
            raise TargetUnavailable("YouTube reports this page does not exist.", "unavailable")
        if "confirm you're not a bot" in lowered_text or "unusual traffic" in lowered_text:
            raise CheckpointRequired("YouTube is asking to confirm the request is not automated.")

    def show(self, url: str) -> Callable[[], None]:
        if self.headless:
            self.headless = False
            self._relaunch()
        try:
            self.navigation += 1
            self.current_url = url
            self._page.goto(url, wait_until="domcontentloaded", timeout=int(self.page_timeout * 1000))
            self._page.bring_to_front()
        except Exception as exc:
            log.warning("Could not show %s: %s", url, exc)
        return lambda: None

    def _relaunch(self) -> None:
        observed = self.observed
        self.close()
        self._pw = self._context = self._native = self._page = None
        self.start()
        self.observed = observed

    def _scroll(self) -> None:
        try:
            self._page.evaluate("""
              () => { window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: 'auto'});
                      window.dispatchEvent(new Event('scroll')); }""")
        except Exception:
            pass
        self._settle()

    def _press_replies(self) -> int:
        """Click every "View replies" / "Show more replies" control offered."""
        try:
            return int(self._page.evaluate("""
              () => {
                let pressed = 0;
                const controls = Array.from(document.querySelectorAll('button, [role="button"], a, yt-button-shape'));
                for (const c of controls) {
                  const label = (c.innerText || c.getAttribute('aria-label') || '').trim().toLowerCase();
                  if (/^(view|show)\\s+(\\d+\\s+)?(more\\s+)?repl(y|ies)$|^\\d+\\s+repl(y|ies)$|^show more replies$/.test(label)) {
                    try { c.click(); pressed += 1; } catch (_) {}
                  }
                }
                return pressed;
              }""") or 0)
        except Exception:
            return 0

    def _settle(self, seconds: Optional[float] = None) -> None:
        wait = seconds if seconds is not None else random.uniform(*self.settle)
        try:
            self._page.wait_for_timeout(int(wait * 1000))
        except Exception:
            self.sleep(wait)

    # -- the protocol ------------------------------------------------------------
    def channel(self, target: YouTubeTarget) -> YouTubeChannel:
        self._goto(self._local(target.url))
        for _ in range(8):
            found = self._channel_for(target)
            if found is not None:
                return found
            self._settle(0.4)
            self._check_page_state()
        self.anomalies.append({"what": "no_channel_observed", "target": target.label,
                               "requests": self.observed.queries_by_navigation.get(self.navigation, [])[:40]})
        raise TargetUnavailable(f"YouTube opened {target.label} but served no channel record.",
                                "unavailable")

    def _channel_for(self, target: YouTubeTarget) -> Optional[YouTubeChannel]:
        if target.channel_id and target.channel_id in self.observed.channels:
            return self.observed.channels[target.channel_id]
        if target.handle:
            wanted = target.handle.lower()
            for channel in self.observed.channels.values():
                if (channel.handle or "").lower() == wanted:
                    return channel
        # the page opened is the channel's; its metadata is what it carries
        return next(iter(self.observed.channels.values()), None) if self.observed.channels else None

    def posts(self, channel: YouTubeChannel) -> Iterator[YouTubePost]:
        base = self._local(channel.url or f"{self.base_url}/channel/{channel.channel_id}")
        self._goto(base.rstrip("/") + "/posts")
        return _ScrollingPosts(self, self.navigation, channel.channel_id, channel.handle)

    def post_comments(self, post: YouTubePost) -> Iterator[YouTubeComment]:
        self._goto(f"{self.base_url}/post/{post.post_id}")
        return _ScrollingComments(self, self.navigation, post.post_id)

    def comments_more(self) -> Optional[bool]:
        tokens = self.observed.tokens_in(self.navigation)
        return bool(tokens) if tokens is not None else None

    def visit_video(self, video: YouTubeVideo) -> str:
        """Load the video's watch page so the WARC holds it as YouTube
        presented it: the page, its scripts, thumbnails and the comments
        it loads. Playback is paused at once; the streams are never
        recorded, the downloaded file is the object."""
        url = f"{self.base_url}/watch?v={video.video_id}"
        self._goto(url)
        try:
            self._page.evaluate("() => { for (const v of document.querySelectorAll('video')) "
                                "{ try { v.pause(); v.muted = true; } catch (_) {} } }")
        except Exception:
            pass
        for _ in range(2):          # far enough down for the comments to be asked for
            self._scroll()
        self._check_page_state()
        return url

    def fetch(self, url: str) -> tuple[bytes, str]:
        page_host = (urlsplit(self._page.url or self.base_url).netloc or "").lower() \
            if self._page is not None else ""
        media_netloc = (urlsplit(url).netloc or "").lower()
        self._media_hosts.add(media_netloc)
        same_origin = media_netloc == page_host
        answer = self._fetch_in(self._page, url, "same-origin" if same_origin else "omit")
        if answer is not None:
            self.page_fetches += 1
            self.last_fetch_via = "browser-page"
            return answer
        log.warning("The browser could not fetch %s (%s); using the driver's HTTP client.",
                    url, self._last_fetch_error)
        self.fallback_fetches += 1
        self.last_fetch_via = "playwright-api-request"
        try:
            response = self._context.request.get(url, timeout=60_000)
        except Exception as exc:
            raise YouTubeError(f"Media request failed: {exc}") from exc
        if response.status == 429:
            raise RateLimited(120.0, "429 on media")
        if response.status >= 400:
            raise TargetUnavailable(f"HTTP {response.status}", "unavailable")
        body = response.body()
        if self.warc is not None:
            try:
                self.warc.write_exchange(url=url, method="GET", req_headers={}, post_data=None,
                                         status=response.status, status_text=response.status_text or "",
                                         resp_headers=response.headers, body=body)
            except Exception:
                pass
        return body, response.headers.get("content-type", "")

    def _fetch_in(self, page, url: str, credentials: str) -> Optional[tuple[bytes, str]]:
        self._awaited_url, self._awaited_seen = url, False
        try:
            answer = page.evaluate(_PAGE_FETCH_JS, {"url": url, "credentials": credentials})
        except Exception as exc:
            answer = {"error": str(exc)}
        if not (isinstance(answer, dict) and "status" in answer):
            self._awaited_url = None
            self._last_fetch_error = (answer or {}).get("error") if isinstance(answer, dict) else str(answer)
            return None
        deadline = time.monotonic() + 5.0
        try:
            while not self._awaited_seen and time.monotonic() < deadline:
                self._page.wait_for_timeout(25)
        except Exception:
            pass
        finally:
            self._awaited_url = None
        status = int(answer.get("status") or 0)
        if status == 429:
            raise RateLimited(120.0, "429 on media")
        if status >= 400:
            raise TargetUnavailable(f"HTTP {status}", "unavailable")
        return base64.b64decode(answer.get("body") or ""), str(answer.get("content_type") or "")


def _post_id_in(url: str) -> Optional[str]:
    parts = urlsplit(url or "")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] == "post":
        return segments[1]
    return None


class _ScrollingPosts:
    """Posts as the tab loads them, scrolling for more on demand; only the
    channel's own, by its id where the post names one."""

    def __init__(self, client: YouTubeBrowserClient, navigation: int,
                 channel_id: Optional[str], handle: Optional[str]):
        self.client = client
        self.navigation = navigation
        self.channel_id = channel_id
        self.handle = (handle or "").lower() or None
        self.handed: set[str] = set()
        self.returned = 0
        self.stalls = 0
        self.refused: dict[str, int] = {}

    def __iter__(self):
        return self

    def _refusal(self, post: YouTubePost) -> Optional[str]:
        if self.channel_id and post.channel_id and post.channel_id != self.channel_id:
            return "another_channel"
        if not post.channel_id and self.handle and post.channel_handle \
                and post.channel_handle.lower() != self.handle:
            return "another_channel"
        return None

    def _pool(self):
        return self.client.observed.posts_in(self.navigation)

    def _pending(self) -> Optional[YouTubePost]:
        for post_id, post in list(self._pool().items()):
            if post_id in self.handed:
                continue
            self.handed.add(post_id)
            reason = self._refusal(post)
            if reason is None:
                self.returned += 1
                return post
            self.refused[reason] = self.refused.get(reason, 0) + 1
        return None

    def __next__(self) -> YouTubePost:
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
        if self.returned == 0 and self._pool():
            self.client.anomalies.append({
                "what": "no_listing_recognised", "channel_id": self.channel_id,
                "handle": self.handle, "posts_observed": len(self._pool()),
                "refused": dict(self.refused),
                "requests": self.client.observed.queries_by_navigation.get(self.navigation, [])[:40]})
        raise StopIteration

    def close(self) -> None:
        pass


class _ScrollingComments:
    """A post's comments as its page loads them, replies on request."""

    def __init__(self, client: YouTubeBrowserClient, navigation: int, post_id: str):
        self.client = client
        self.navigation = navigation
        self.post_id = post_id
        self.handed: set[str] = set()
        self.stalls = 0

    def __iter__(self):
        return self

    def _pool(self):
        return self.client.observed.comments_in(self.navigation)

    def _pending(self) -> Optional[YouTubeComment]:
        for comment_id, comment in list(self._pool().items()):
            if comment_id in self.handed:
                continue
            self.handed.add(comment_id)
            comment.target_id = self.post_id
            return comment
        return None

    def __next__(self) -> YouTubeComment:
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self._pool())
            self.client._press_replies()
            self.client._scroll()
            self.client._check_page_state()
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self._pool()) == before:
                self.stalls += 1
        raise StopIteration

    def close(self) -> None:
        pass
