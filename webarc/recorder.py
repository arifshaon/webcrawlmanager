"""Interactive recording engine: the user drives a visible browser and SWM
captures the network traffic their browsing generates into WARC.

Unlike the crawler, the recorder performs no automatic scrolling, clicking,
link discovery, or queue processing. After opening the starting URL, all
navigation belongs to the user. What is archived is the network traffic
caused by their actions — not the actions themselves: scrolling that
triggers lazy loads is captured because those loads cross the network;
a DOM-only interaction that makes no request leaves no trace in the WARC.

State machine (the single authority for every control surface — CLI,
dashboard, and in-page widget all funnel through apply()):

  recording — exchanges whose request began now are written to WARC
  paused    — browser stays fully usable; exchanges are ignored;
              current_url keeps updating
  stopped   — loop exits, WARC finalises, browser closes

Pause boundary: an exchange is captured only when its *request began* while
recording was active. Eligibility is tracked per Playwright request object
(each hop of a redirect chain is a distinct request object and is judged
independently), so a response arriving after resume for a request that
started during pause is correctly dropped, and vice versa.

Resume semantics are explicit, never implicit:
  resume        — capture future traffic only; the page the user is looking
                  at may be incomplete in the WARC (its resources loaded
                  while paused and cannot be recovered retrospectively)
  capture_page  — resume AND reload the current page so its complete
                  network traffic is recorded

The engine is deliberately unaware of SQLite, FastAPI, or argparse: callers
supply `control_poll` (returns a pending command or None) and `on_progress`
(receives state/visited/bytes/current_url). The CLI, the dashboard worker,
and tests are all thin shells around this class.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .browser import BrowserDriver
from .capture import WarcSession
from .config import BehaviorConfig, BrowserConfig

log = logging.getLogger(__name__)

# session states
RECORDING = "recording"
PAUSED = "paused"
STOPPED = "stopped"

# control commands accepted by apply()
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_CAPTURE_PAGE = "capture_page"
CMD_STOP = "stop"

_SKIP_URLS = ("about:blank", "about:srcdoc", "")


class RecordingSession:
    """One interactive recording session: visible browser, context-wide
    capture, pause/resume with a precise request-eligibility boundary."""

    def __init__(self, start_url: str, browser_cfg: BrowserConfig,
                 warc: WarcSession, *,
                 control_poll: Optional[Callable[[], Optional[str]]] = None,
                 on_progress: Optional[Callable[..., None]] = None,
                 tick_seconds: float = 0.5,
                 page_timeout: float = 45.0):
        self.start_url = start_url
        self.browser_cfg = browser_cfg
        self.warc = warc
        self.control_poll = control_poll or (lambda: None)
        self.on_progress = on_progress or (lambda **kw: None)
        self.tick = tick_seconds
        self.page_timeout = page_timeout

        self.state = RECORDING
        self.visited = 0                  # top-level navigations while recording
        self.current_url = start_url
        self._eligible: set = set()       # requests that began while recording
        self._closed = False              # browser/context gone
        self._context = None

    # -- state transitions -------------------------------------------------
    def apply(self, command: str) -> None:
        """Single state-transition authority. Unknown or invalid-for-state
        commands are ignored (callers may deliver duplicates)."""
        if command == CMD_PAUSE and self.state == RECORDING:
            self.state = PAUSED
            log.info("Capture paused — browsing continues unrecorded")
        elif command == CMD_RESUME and self.state == PAUSED:
            self.state = RECORDING
            log.info("Capture resumed (future traffic only)")
        elif command == CMD_CAPTURE_PAGE and self.state == PAUSED:
            self.state = RECORDING
            log.info("Capture resumed — reloading current page to record it")
            self._reload_current_page()
        elif command == CMD_STOP and self.state != STOPPED:
            self.state = STOPPED
            log.info("Stopping recording")

    def _reload_current_page(self) -> None:
        if not self._context:
            return
        page = None
        for p in self._context.pages:
            if p.url == self.current_url:
                page = p
                break
        if page is None and self._context.pages:
            page = self._context.pages[-1]
        if page is None:
            return
        # bypass the browser cache for this reload: a cached revalidation
        # (304, empty body) would defeat the point of capturing the page
        cdp = None
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception:
            cdp = None
        try:
            page.reload(wait_until="load",
                        timeout=int(self.page_timeout * 1000))
        except Exception as exc:
            log.warning("Reload of %s failed: %s", page.url, exc)
        finally:
            if cdp:
                try:
                    cdp.send("Network.setCacheDisabled",
                             {"cacheDisabled": False})
                    cdp.detach()
                except Exception:
                    pass

    # -- network event handlers (context-wide) -----------------------------
    def _on_request(self, request) -> None:
        if self.state == RECORDING:
            self._eligible.add(request)

    def _on_request_done(self, request) -> None:
        self._eligible.discard(request)

    def _on_response(self, response) -> None:
        request = response.request
        if request not in self._eligible:
            return
        try:
            try:
                body = response.body()
            except Exception:
                body = b""  # redirects / cached / aborted bodies
            post = request.post_data_buffer or None
            self.warc.write_exchange(
                url=response.url,
                method=request.method,
                req_headers=request.headers,
                post_data=post,
                status=response.status,
                status_text=response.status_text or "",
                resp_headers=response.headers,
                body=body,
            )
        except Exception as exc:
            log.debug("Capture skipped for %s: %s", response.url, exc)

    # -- page lifecycle -----------------------------------------------------
    def _on_page(self, page) -> None:
        # covers user-opened tabs, popups, and target=_blank links
        page.on("framenavigated", self._on_frame_navigated)

    def _on_frame_navigated(self, frame) -> None:
        if frame.parent_frame is not None:
            return
        url = frame.url
        if url in _SKIP_URLS:
            return
        self.current_url = url  # updates while paused too, by design
        if self.state == RECORDING:
            self.visited += 1

    # -- main loop -----------------------------------------------------------
    def run(self) -> dict:
        """Own the browser lifecycle: launch, record, finalise the WARC."""
        mode = self.browser_cfg.mode
        if mode not in ("headed", "native"):
            raise ValueError(
                "Interactive recording needs a visible browser: set browser "
                "mode to 'headed' (recommended) or 'native', not "
                f"{mode!r}")
        try:
            with BrowserDriver(self.browser_cfg, BehaviorConfig()) as driver:
                return self.run_with_context(driver.context)
        finally:
            self.warc.close()

    def run_with_context(self, context, navigate: bool = True) -> dict:
        """Record against an existing BrowserContext (used by run(), embedders
        and tests). Does not close the WARC — the caller owns it."""
        self._context = context
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_request_done)
        context.on("requestfailed", self._on_request_done)
        context.on("page", self._on_page)
        context.on("close", self._mark_closed)
        for page in context.pages:
            self._on_page(page)

        page = context.pages[0] if context.pages else context.new_page()
        if navigate:
            try:
                page.goto(self.start_url, wait_until="load",
                          timeout=int(self.page_timeout * 1000))
            except Exception as exc:
                # keep the session alive: the user can retry in the window
                log.warning("Initial navigation to %s failed: %s "
                            "(browser stays open — navigate manually)",
                            self.start_url, exc)

        log.info("Recording session started at %s — browse in the window; "
                 "close it (or send stop) to finish", self.start_url)

        last_report = 0.0
        while self.state != STOPPED and not self._closed:
            try:
                command = self.control_poll()
                if command:
                    self.apply(command)
                now = time.monotonic()
                if now - last_report >= 1.0:
                    self._report()
                    last_report = now
                if not context.pages:
                    log.info("All browser tabs closed — ending recording")
                    break
                self._pump(context)
            except Exception as exc:
                if self._closed or _is_closed_error(exc):
                    log.info("Browser closed — ending recording")
                    break
                raise

        self.state = STOPPED
        self._report()
        return {"visited": self.visited, "bytes": self.warc.total_bytes,
                "current_url": self.current_url}

    def _mark_closed(self, *_args) -> None:
        self._closed = True

    def _pump(self, context) -> None:
        """Let Playwright dispatch network events. The sync API only delivers
        events while we're inside a Playwright call, so the idle wait must go
        through a page — a plain time.sleep() would starve capture."""
        for page in list(context.pages):
            try:
                page.wait_for_timeout(self.tick * 1000)
                return
            except Exception:
                continue  # that page closed mid-wait; try another
        # no usable page this tick; loop re-checks context.pages next pass

    def _report(self) -> None:
        try:
            bytes_written = self.warc.total_bytes
        except Exception:
            bytes_written = 0
        try:
            self.on_progress(state=self.state, visited=self.visited,
                             bytes_written=bytes_written,
                             current_url=self.current_url)
        except Exception as exc:
            log.debug("Progress report failed: %s", exc)


def _is_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("closed" in text or "disconnected" in text
            or "browser has been closed" in text)
