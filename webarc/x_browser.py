"""Collecting X through the browser X is served to.

X's web client fetches everything it shows through GraphQL operations
whose names are in their URLs; the page loads nothing worth reading in its
HTML. A real Chrome, signed in once by the curator in a dedicated profile,
opens a profile, a post or a search the way a person does, and this module
reads what the page's own requests bring back:

* a profile page resolves the account (``UserByScreenName``) and lists its
  posts (``UserTweets``, ``UserTweetsAndReplies``, ``UserMedia``) as it is
  scrolled; a post is the account's only when one of its own listing
  requests -- the one naming the account's numeric id -- returned it;
* a post's page carries the conversation (``TweetDetail``): the post, what
  it replies to, and the replies under it, more on scroll and on "Show
  more replies";
* a search page lists what X served this account for the query
  (``SearchTimeline``);
* media is requested from inside the page so it passes through the same
  response hook as everything else; images are asked for at ``name=orig``,
  videos as the best progressive MP4; the driver's own HTTP client is a
  logged last resort;
* every exchange the browser makes can be written to a WARC as it happens,
  credentials and the session's own material redacted.

What the browser observes is scoped to the navigation it was observed
under, as in the Instagram collector: each page opened is a new generation
and a listing hands over only records from its own generation that its
target's own operation returned. Other operations the signed-in client
makes (the home feed, notifications, recommendations) are in the WARC and
counted, and never produce records.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import unquote, urlsplit

from .config import BrowserConfig
from .browser import operator_launch_kwargs
from .instagram_browser import _PAGE_FETCH_JS
from .x import (CheckpointRequired, LoginRequired, RateLimited, TargetUnavailable,
                XError, search_url)
from .x_extract import (OPERATIONS_OF_SURFACE, SURFACE_OF_OPERATION, XAbsence,
                        XCursor, XPost, XUser, describe_graphql_request,
                        rate_limit_reset, read_timeline)

log = logging.getLogger(__name__)

_X_HOSTS = ("x.com", "twitter.com")
_MEDIA_HOST_MARKERS = ("twimg.com",)
_SESSION_COOKIES = ("auth_token", "ct0", "twid", "kdt", "att")

_PATH_OF_SURFACE = {"posts": "", "replies": "/with_replies", "media": "/media"}


class _Observed:
    """What the browser has loaded so far, by the navigation it was under."""

    def __init__(self) -> None:
        self._posts_by_navigation: dict[int, OrderedDict[str, XPost]] = {}
        self._absences_by_navigation: dict[int, list[XAbsence]] = {}
        self._cursors_by_navigation: dict[int, list[XCursor]] = {}
        self.users: dict[str, XUser] = {}
        self.queries_by_navigation: dict[int, list[str]] = {}
        self.operations: Counter = Counter()
        self.responses = 0
        self.api_responses = 0
        self.promoted_skipped = 0
        self.injected_skipped = 0

    def take(self, read, navigation: int) -> None:
        pool = self._posts_by_navigation.setdefault(navigation, OrderedDict())
        for post in read.posts:
            current = pool.get(post.post_id)
            if current is None:
                pool[post.post_id] = post
            else:
                if post.is_pinned:
                    current.is_pinned = True
                if len(post.media) > len(current.media) or (
                        post.text and not current.text):
                    post.is_pinned = post.is_pinned or current.is_pinned
                    pool[post.post_id] = post
        for user in read.users:
            current = self.users.get(user.user_id)
            if current is None or len(user.raw) > len(current.raw):
                self.users[user.user_id] = user
        self._absences_by_navigation.setdefault(navigation, []).extend(read.absences)
        # the cursors of the latest page decide whether more is offered; a
        # page without any says the offer is spent
        self._cursors_by_navigation[navigation] = list(read.cursors)
        self.promoted_skipped += read.promoted_skipped
        self.injected_skipped += read.injected_skipped

    def posts_in(self, navigation: int) -> "OrderedDict[str, XPost]":
        return self._posts_by_navigation.get(navigation) or OrderedDict()

    def absences_in(self, navigation: int) -> list[XAbsence]:
        return self._absences_by_navigation.get(navigation) or []

    def cursors_in(self, navigation: int) -> list[XCursor]:
        return self._cursors_by_navigation.get(navigation) or []

    def user_by_handle(self, handle: str) -> Optional[XUser]:
        wanted = handle.lower()
        return next((u for u in self.users.values() if u.handle.lower() == wanted), None)


class XBrowserClient:
    """X through a signed-in browser, answering the client protocol.

    ``warc`` is a WarcSession (the redacting FacebookWarcSession in practice)
    or None. ``sleep`` is injectable so tests do not wait for real seconds.
    """

    version = "browser"

    def __init__(self, browser: BrowserConfig, warc=None,
                 sleep: Callable[[float], None] = time.sleep,
                 stall_rounds: int = 3, page_timeout: float = 60.0,
                 base_url: str = "https://x.com", headless: bool = False,
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
        self._rate_limited: Optional[tuple[float, str]] = None
        self._last_fetch_error: Optional[str] = None

    # -- what the engine may ask for beside the protocol -----------------------
    @property
    def operations_observed(self) -> dict:
        return dict(self.observed.operations)

    def record_responses_to(self, sink: Callable[[dict, bytes], str]) -> None:
        self._response_sink = sink

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "XBrowserClient":
        try:
            return self._start()
        except Exception:
            self.close()
            self._pw = self._context = self._native = self._page = None
            raise

    def _start(self) -> "XBrowserClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile = self.browser.user_data_dir or str(Path("./x-profile-swm").resolve())
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
                log.warning("Native Chrome exited at once (%s); retrying without "
                            "its sandbox.", exc)
                self._native = _launch_native_chrome(
                    profile, self.headless, self.browser.chrome_path, no_sandbox=True)
                process, port = self._native
                _wait_for_cdp(port, process)
            attached = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self._context = (attached.contexts[0] if attached.contexts
                             else attached.new_context())
        else:
            launch_kwargs: dict = {
                "headless": self.headless, **operator_launch_kwargs(),
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
        cookies: dict[str, str] = {}
        try:
            for cookie in self._context.cookies(self.base_url + "/"):
                if cookie.get("name") in _SESSION_COOKIES:
                    cookies[cookie["name"]] = cookie["value"]
        except Exception:
            pass
        self.signed_in = bool(cookies.get("auth_token"))
        self._session = ({"cookies": cookies, "user_agent": self.user_agent}
                         if self.signed_in else None)

    @property
    def session(self) -> Optional[dict]:
        return self._session

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
        return netloc == self.host or any(
            host == h or host.endswith("." + h) for h in _X_HOSTS)

    def _handle_response(self, response) -> None:
        try:
            url = response.url
            netloc = (urlsplit(url).netloc or "").lower()
            host = urlsplit(url).hostname or ""
            ours = self._is_ours(netloc, host)
            media_host = netloc in self._media_hosts or any(
                marker in host for marker in _MEDIA_HOST_MARKERS)
            if not ours and not media_host:
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
            if not ours:
                return
            asked = describe_graphql_request(url)
            if asked is None:
                return
            self.observed.api_responses += 1
            self.observed.operations[asked["operation"]] += 1
            navigation = self.navigation
            self.observed.queries_by_navigation.setdefault(navigation, []).append(
                asked["operation"] + (" [listing]" if asked["listing"] else ""))
            if response.status == 429:
                reset = rate_limit_reset(response.headers)
                self._rate_limited = (reset or 0.0, asked["operation"])
                return
            if not asked["target_operation"] or not body or response.status >= 400:
                return
            try:
                document = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                return
            origin = {"url": url, "operation": asked["operation"],
                      "query_id": asked["query_id"], "navigation": navigation,
                      "response": None, "listing_request": asked["listing"],
                      "listing_user": asked["user_id"], "raw_query": asked["raw_query"],
                      "product": asked["product"], "focal_id": asked["focal_id"],
                      "cursor": asked["cursor"]}
            if self._response_sink is not None:
                try:
                    origin["response"] = self._response_sink({
                        "url": url, "method": request.method,
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "resource_type": request.resource_type,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "navigation": navigation, "operation": asked["operation"],
                        "page_url": self.current_url,
                    }, body)
                except Exception as exc:
                    log.warning("Could not keep the response for %s: %s", url, exc)
            surface = SURFACE_OF_OPERATION.get(asked["operation"], "posts")
            self.observed.take(read_timeline([document], origin, surface), navigation)
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
            raise XError(f"Could not open {url}: {exc}") from exc
        self._settle()
        self._check_page_state()

    def _check_page_state(self) -> None:
        """X's answers that are not content, raised as the engine's conditions."""
        if self._rate_limited is not None:
            reset, operation = self._rate_limited
            self._rate_limited = None
            wait = max(20.0, reset - time.time()) if reset else 60.0
            raise RateLimited(min(wait, 960.0),
                              f"X answered {operation} with 429; it reopens in {int(wait)}s.")
        try:
            url = self._page.url
        except Exception:
            return
        lowered = url.lower()
        if "/i/flow/login" in lowered or lowered.rstrip("/").endswith("/login"):
            raise LoginRequired("X is showing its sign-in page.")
        if "/account/access" in lowered or "/i/flow/consent" in lowered:
            raise CheckpointRequired("X is asking the account to verify itself.")
        try:
            text = self._page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 6000)")
        except Exception:
            text = ""
        lowered_text = str(text).lower().replace("’", "'")
        if "this account doesn't exist" in lowered_text:
            raise TargetUnavailable("X reports this account does not exist.")
        if "account suspended" in lowered_text:
            raise TargetUnavailable("X reports this account is suspended.")
        if "hmm...this page doesn't exist" in lowered_text or \
                "hmm... this page doesn't exist" in lowered_text:
            raise TargetUnavailable("X reports this page does not exist.")
        if "these posts are protected" in lowered_text:
            raise TargetUnavailable("This account's posts are protected and the "
                                    "capture account does not follow it.")
        if "rate limit exceeded" in lowered_text:
            raise RateLimited(120.0, "X reports its rate limit exceeded.")

    def show(self, url: str) -> Callable[[], None]:
        """Bring the curator to a page in a window they can see."""
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
        observed = self.observed
        self.close()
        self._pw = self._context = self._native = self._page = None
        self.start()
        self.observed = observed

    def _reload(self) -> int:
        """Open the current page again, after X refused to page it.

        A page that met a 429 stops asking; X's own client shows "Something
        went wrong. Try reloading." Reloading is what a person does, and
        the listing carries on from what it had already handed over.
        """
        self._goto(self.current_url or self.base_url)
        return self.navigation

    def _scroll(self) -> None:
        try:
            self._page.evaluate("""
              () => { window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: 'auto'});
                      window.dispatchEvent(new Event('scroll')); }""")
        except Exception:
            pass
        self._settle()

    def _press_show_more(self) -> None:
        """Click X's "Show more replies" / "Show replies" offers, if shown."""
        try:
            self._page.evaluate("""
              () => {
                const controls = Array.from(document.querySelectorAll('button, [role="button"], a'));
                for (const c of controls) {
                  const label = (c.innerText || c.getAttribute('aria-label') || '').trim().toLowerCase();
                  if (/^show (more )?replies$|^show more$|^show additional replies$|^show probable spam$/.test(label)) { c.click(); }
                }
              }""")
        except Exception:
            pass

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
        twid = unquote(str((self._session or {}).get("cookies", {}).get("twid") or ""))
        if twid.startswith("u="):
            return twid[2:] or "signed-in"
        return "signed-in"

    def user(self, handle: str) -> XUser:
        self._goto(f"{self.base_url}/{handle}")
        for _ in range(8):
            found = self.observed.user_by_handle(handle)
            if found is not None:
                return found
            self._settle(0.4)
            self._check_page_state()
        # The page opened and said nothing that reads as the account: say
        # so rather than invent one, but keep the target usable by handle.
        self.anomalies.append({"what": "no_user_lookup_observed", "handle": handle,
                               "requests": self.observed.queries_by_navigation.get(
                                   self.navigation, [])[:40]})
        return XUser(user_id="", handle=handle, raw={})

    def timeline(self, user: XUser, surface: str) -> Iterator[XPost]:
        url = f"{self.base_url}/{user.handle}{_PATH_OF_SURFACE.get(surface, '')}"
        if self.current_url != url:
            self._goto(url)
        return _ScrollingTimeline(self, self.navigation,
                                  operations=set(OPERATIONS_OF_SURFACE.get(
                                      surface, OPERATIONS_OF_SURFACE["posts"])),
                                  user_id=user.user_id or None, handle=user.handle)

    def post(self, post_id: str) -> XPost:
        self._goto(f"{self.base_url}/i/status/{post_id}")
        for _ in range(10):
            found = self.observed.posts_in(self.navigation).get(post_id)
            if found is not None:
                return found
            self._settle(0.4)
            self._check_page_state()
        raise TargetUnavailable(f"X opened the page of {post_id} but served no post record.")

    def conversation(self, post_id: str) -> Iterator[XPost]:
        url = f"{self.base_url}/i/status/{post_id}"
        pool = self.observed.posts_in(self.navigation)
        on_page = post_id in pool and any(
            (p.provenance or {}).get("focal_id") == post_id for p in pool.values())
        if not on_page:
            self._goto(url)
        return _ScrollingConversation(self, self.navigation, post_id)

    def search(self, query: str, product: str) -> Iterator[XPost]:
        url = search_url(query, product).replace("https://x.com", self.base_url, 1)
        self._goto(url)
        return _ScrollingTimeline(self, self.navigation,
                                  operations=set(OPERATIONS_OF_SURFACE["search"]),
                                  raw_query=query, product=product)

    def conversation_context(self, post_id: str) -> list[XPost]:
        """Posts the listing showed in the same module as this one."""
        pool = self.observed.posts_in(self.navigation)
        post = pool.get(post_id)
        if post is None:
            return []
        module = (post.provenance or {}).get("module")
        if not module:
            return []
        return [p for p in pool.values()
                if p.post_id != post_id and (p.provenance or {}).get("module") == module]

    def absences(self, post_id: str) -> list[XAbsence]:
        return list(self.observed.absences_in(self.navigation))

    def conversation_more(self) -> Optional[bool]:
        """Whether the last conversation page seen offered more replies."""
        cursors = self.observed.cursors_in(self.navigation)
        if not cursors:
            return None
        return any(c.cursor_type in ("ShowMore", "ShowMoreThreads", "Bottom")
                   for c in cursors)

    def fetch(self, url: str) -> tuple[bytes, str]:
        """Media, requested from inside the browser.

        X's media hosts answer a page's cross-origin read when no
        credentials are sent, so the open page asks without them, with its
        own network identity; the exchange reaches the WARC through the
        response hook. Only if the page cannot is the driver's HTTP client
        used, and that fallback is counted and logged.
        """
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
        log.warning("The browser could not fetch %s (%s); using the driver's HTTP "
                    "client instead.", url, self._last_fetch_error)
        self.fallback_fetches += 1
        self.last_fetch_via = "playwright-api-request"
        try:
            response = self._context.request.get(url, timeout=60_000)
        except Exception as exc:
            raise XError(f"Media request failed: {exc}") from exc
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

    def _fetch_in(self, page, url: str, credentials: str) -> Optional[tuple[bytes, str]]:
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
        self._await_hook()
        status = int(answer.get("status") or 0)
        if status == 429:
            raise RateLimited(120.0, "429 on media")
        if status >= 400:
            raise TargetUnavailable(f"HTTP {status}")
        return (base64.b64decode(answer.get("body") or ""),
                str(answer.get("content_type") or ""))

    def _await_hook(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        try:
            while not self._awaited_seen and time.monotonic() < deadline:
                self._page.wait_for_timeout(25)
        except Exception:
            pass
        finally:
            self._awaited_url = None


class _ScrollingTimeline:
    """Posts as the browser loads them, scrolling for more on demand.

    Only posts a target operation of this navigation returned are handed
    over: for a profile, a listing request naming the account's numeric id
    (or, before the id is known, its handle); for a search, the request
    carrying the query. After ``stall_rounds`` scrolls that load nothing
    new the listing ends; X keeps offering a cursor on an exhausted
    timeline, so a missing cursor is never waited for.
    """

    def __init__(self, client: XBrowserClient, navigation: int, operations: set[str],
                 user_id: Optional[str] = None, handle: Optional[str] = None,
                 raw_query: Optional[str] = None, product: Optional[str] = None):
        self.client = client
        self.navigation = navigation
        self.operations = operations
        self.user_id = user_id
        self.handle = (handle or "").lower() or None
        self.raw_query = raw_query
        self.product = product
        self.handed: set[str] = set()
        self.returned = 0
        self.stalls = 0
        self.refused: dict[str, int] = {}
        self._reload_pending = False

    def __iter__(self):
        return self

    def _refusal(self, post: XPost) -> Optional[str]:
        origin = post.provenance or {}
        if origin.get("operation") not in self.operations:
            return "not_from_the_listing_operation"
        if self.raw_query is not None:
            asked = str(origin.get("raw_query") or "")
            if asked.strip().lower() != self.raw_query.strip().lower():
                return "listing_of_another_query"
            return None
        named = str(origin.get("listing_user") or "")
        if self.user_id:
            if named and named != self.user_id:
                return "listing_of_another_user"
            if post.author_id and post.author_id != self.user_id:
                return "another_author"
            return None
        if self.handle and post.author_handle and post.author_handle.lower() != self.handle:
            return "another_author"
        return None

    def _pool(self) -> "OrderedDict[str, XPost]":
        return self.client.observed.posts_in(self.navigation)

    def _pending(self) -> Optional[XPost]:
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

    def _finished(self) -> None:
        if self.returned == 0 and self._pool():
            self.client.anomalies.append({
                "what": "no_listing_recognised", "user_id": self.user_id,
                "handle": self.handle, "query": self.raw_query,
                "posts_observed": len(self._pool()), "refused": dict(self.refused),
                "requests": self.client.observed.queries_by_navigation.get(
                    self.navigation, [])[:40]})
            log.warning("No listing for %s recognised among %d observed posts (%s); "
                        "the page asked for: %s",
                        self.handle or self.raw_query, len(self._pool()),
                        ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in self.refused.items())
                        or "no reason recorded",
                        self.client.observed.queries_by_navigation.get(self.navigation, []))

    def __next__(self) -> XPost:
        if self._reload_pending:
            self._reload_pending = False
            self.navigation = self.client._reload()
            self.stalls = 0
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self._pool())
            self.client._scroll()
            try:
                self.client._check_page_state()
            except RateLimited:
                self._reload_pending = True
                raise
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self._pool()) == before:
                self.stalls += 1
        self._finished()
        raise StopIteration


class _ScrollingConversation:
    """A post's conversation as its page loads it: what it replies to and
    the replies under it, more on scroll and on "Show more replies"."""

    def __init__(self, client: XBrowserClient, navigation: int, focal_id: str):
        self.client = client
        self.navigation = navigation
        self.focal_id = focal_id
        self.handed: set[str] = set()
        self.stalls = 0
        self._reload_pending = False

    def __iter__(self):
        return self

    def _pool(self) -> "OrderedDict[str, XPost]":
        return self.client.observed.posts_in(self.navigation)

    def _pending(self) -> Optional[XPost]:
        for post_id, post in list(self._pool().items()):
            if post_id in self.handed or post_id == self.focal_id:
                continue
            self.handed.add(post_id)
            origin = post.provenance or {}
            if origin.get("operation") not in OPERATIONS_OF_SURFACE["conversation"]:
                continue
            if str(origin.get("focal_id") or "") not in ("", self.focal_id):
                continue
            return post

    def __next__(self) -> XPost:
        if self._reload_pending:
            self._reload_pending = False
            self.navigation = self.client._reload()
            self.stalls = 0
        found = self._pending()
        if found is not None:
            return found
        while self.stalls < self.client.stall_rounds:
            before = len(self._pool())
            self.client._press_show_more()
            self.client._scroll()
            try:
                self.client._check_page_state()
            except RateLimited:
                self._reload_pending = True
                raise
            found = self._pending()
            if found is not None:
                self.stalls = 0
                return found
            if len(self._pool()) == before:
                self.stalls += 1
        raise StopIteration
