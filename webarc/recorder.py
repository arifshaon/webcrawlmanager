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
from collections import Counter, deque
from typing import Callable, Optional
from urllib.parse import urlsplit

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

# In-page control widget. Injected as a context init script so it reappears
# on every navigation and in every new tab. Rendered inside a Shadow DOM so
# page styles cannot break it. It lives only in the live DOM — capture is of
# network responses, so the widget never appears in the archived pages.
# Buttons call back into Python via the swmControl binding; the recorder
# pushes state changes to every page via window.__swmSetState.
_WIDGET_JS = """
(() => {
  if (window.__swmWidgetInstalled) return;
  window.__swmWidgetInstalled = true;
  let state = "recording";
  let root = null;
  const LABELS = {
    recording: "\\u25CF Recording",
    paused: "\\u23F8 Capture paused",
    stopped: "Stopped"
  };
  function buttons() {
    if (state === "recording")
      return [["pause", "Pause capture"], ["stop", "Stop"]];
    if (state === "paused")
      return [["resume", "Resume capture"],
              ["capture_page", "Capture this page"],
              ["stop", "Stop"]];
    return [];
  }
  function render() {
    if (!root) return;
    const st = root.querySelector(".swm-state");
    st.textContent = LABELS[state] || state;
    st.className = "swm-state " + state;
    const bar = root.querySelector(".swm-buttons");
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    for (const [cmd, label] of buttons()) {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", () => {
        if (window.swmControl)
          window.swmControl(cmd).then(s => { state = s; render(); });
      });
      bar.appendChild(b);
    }
  }
  window.__swmSetState = (s) => { state = s; if (!root) install(); render(); };
  function install() {
    if (!document.documentElement || root) return;
    try {
      const host = document.createElement("div");
      const shadow = host.attachShadow({ mode: "open" });
      // Built with createElement/textContent, never innerHTML: sites that
      // enforce Trusted Types (require-trusted-types-for 'script') make
      // innerHTML assignments throw, which silently killed the widget.
      const style = document.createElement("style");
      style.textContent = [
        ".swm-box { position: fixed; right: 16px; bottom: 16px;",
        "  z-index: 2147483647; font: 12px/1.4 system-ui, sans-serif;",
        "  background: #1b1e23; color: #fff; border-radius: 8px;",
        "  padding: 10px 12px; box-shadow: 0 4px 16px rgba(0,0,0,.35);",
        "  min-width: 180px; }",
        ".swm-title { font-weight: 600; opacity: .7; font-size: 10px;",
        "  text-transform: uppercase; letter-spacing: .08em;",
        "  margin-bottom: 4px; }",
        ".swm-state { margin-bottom: 8px; }",
        ".swm-state.recording { color: #ff5f56; }",
        ".swm-state.paused { color: #ffbd2e; }",
        ".swm-buttons { display: flex; gap: 6px; flex-wrap: wrap; }",
        "button { font: 11px system-ui, sans-serif;",
        "  border: 1px solid rgba(255,255,255,.25);",
        "  background: rgba(255,255,255,.08); color: #fff;",
        "  border-radius: 5px; padding: 4px 8px; cursor: pointer; }",
        "button:hover { background: rgba(255,255,255,.18); }",
      ].join(" ");
      const box = document.createElement("div");
      box.className = "swm-box";
      const title = document.createElement("div");
      title.className = "swm-title";
      title.textContent = "SWM Recording";
      const stateEl = document.createElement("div");
      stateEl.className = "swm-state";
      const buttons = document.createElement("div");
      buttons.className = "swm-buttons";
      box.appendChild(title); box.appendChild(stateEl);
      box.appendChild(buttons);
      shadow.appendChild(style); shadow.appendChild(box);
      root = shadow;
      document.documentElement.appendChild(host);
      render();
      if (window.swmControl)
        window.swmControl("state").then(s => { state = s; render(); });
    } catch (_) {
      root = null;  // leave installable for a later attempt
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, { once: true });
  else install();
})();
"""


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
        self._commands: deque[str] = deque()  # queued by the in-page widget
        self._state_dirty = False         # widgets need a state push
        # responses whose body was not yet readable at the response event
        # (common for streaming media); retried at requestfinished
        self._pending_body: dict = {}
        self.capture_stats: Counter = Counter()
        # gap detection: main-frame URLs the user saw vs document exchanges
        # actually captured — a page can render with no network fetch at all
        # (back/forward cache, prerender), and would then be missing on replay
        self._captured_docs: set[str] = set()
        self._nav_watch: deque = deque()  # (url, deadline)
        self.doc_grace = 10.0             # seconds to wait for the document

    # -- state transitions -------------------------------------------------
    def apply(self, command: str) -> None:
        """Single state-transition authority. Unknown or invalid-for-state
        commands are ignored (callers may deliver duplicates)."""
        if command == CMD_PAUSE and self.state == RECORDING:
            self.state = PAUSED
            self._state_dirty = True
            log.info("Capture paused — browsing continues unrecorded")
        elif command == CMD_RESUME and self.state == PAUSED:
            self.state = RECORDING
            self._state_dirty = True
            log.info("Capture resumed (future traffic only)")
        elif command == CMD_CAPTURE_PAGE and self.state == PAUSED:
            self.state = RECORDING
            self._state_dirty = True
            log.info("Capture resumed — reloading current page to record it")
            self._reload_current_page()
        elif command == CMD_STOP and self.state != STOPPED:
            self.state = STOPPED
            self._state_dirty = True
            log.info("Stopping recording")

    def _on_widget_command(self, _source, command: str = "state") -> str:
        """Playwright binding target. Never touches Playwright objects —
        binding handlers run re-entrantly, so commands are queued and the
        main loop applies them. Returns the current state so the widget
        can render optimistically."""
        if command in (CMD_PAUSE, CMD_RESUME, CMD_CAPTURE_PAGE, CMD_STOP):
            self._commands.append(command)
        return self.state

    def _ensure_widgets(self) -> None:
        """Re-inject the widget into pages where the init script did not run
        or its install failed (e.g. attached contexts, strict-CSP sites).
        Idempotent: the script bails out if already wired."""
        if not self._context:
            return
        for page in list(self._context.pages):
            try:
                page.evaluate(_WIDGET_JS)
                page.evaluate(
                    "s => window.__swmSetState && window.__swmSetState(s)",
                    self.state)
            except Exception:
                pass

    def _sync_widgets(self) -> None:
        """Push the authoritative state to every open page's widget."""
        if not self._context:
            return
        for page in list(self._context.pages):
            try:
                page.evaluate(
                    "s => window.__swmSetState && window.__swmSetState(s)",
                    self.state)
            except Exception:
                pass  # page mid-navigation or closed; init script will ask
        self._state_dirty = False

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

    def _on_request_finished(self, request) -> None:
        # streaming/media bodies are often not readable at the response
        # event but are complete by requestfinished — retry the write here
        response = self._pending_body.pop(request, None)
        if response is not None and request in self._eligible:
            try:
                body = response.body()
                self._write_exchange(response, body)
                self.capture_stats["body-retry-ok"] += 1
            except Exception as exc:
                self.capture_stats["body-unavailable"] += 1
                host = urlsplit(response.url).hostname or "?"
                self.capture_stats[f"body-unavailable:{host}"] += 1
                log.warning("Body unavailable for %s (%s) — recorded "
                            "headers with empty body", response.url, exc)
                self._write_exchange(response, b"")
        self._eligible.discard(request)

    def _on_request_failed(self, request) -> None:
        self._pending_body.pop(request, None)
        self._eligible.discard(request)

    def _on_response(self, response) -> None:
        request = response.request
        if request not in self._eligible:
            return
        # redirects never have a readable body; write them immediately
        if 300 <= response.status < 400:
            self._write_exchange(response, b"")
            return
        try:
            body = response.body()
        except Exception:
            self._pending_body[request] = response
            self.capture_stats["body-deferred"] += 1
            return
        self._write_exchange(response, body)

    def _write_exchange(self, response, body: bytes) -> None:
        request = response.request
        try:
            self.warc.write_exchange(
                url=response.url,
                method=request.method,
                req_headers=request.headers,
                post_data=request.post_data_buffer or None,
                status=response.status,
                status_text=response.status_text or "",
                resp_headers=response.headers,
                body=body,
            )
            self.capture_stats["captured"] += 1
            host = urlsplit(response.url).hostname or "?"
            self.capture_stats[f"host:{host}"] += 1
            try:
                if request.resource_type == "document":
                    self._captured_docs.add(response.url.split("#")[0])
            except Exception:
                pass
        except Exception as exc:
            self.capture_stats["write-failed"] += 1
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
            self._nav_watch.append(
                (url.split("#")[0], time.monotonic() + self.doc_grace))

    def _check_nav_watch(self) -> None:
        now = time.monotonic()
        while self._nav_watch and self._nav_watch[0][1] <= now:
            url, _ = self._nav_watch.popleft()
            if url not in self._captured_docs:
                self.capture_stats["page-doc-missing"] += 1
                log.warning(
                    "Page %s was displayed but its document was never "
                    "captured — likely served from the browser's back/"
                    "forward cache or a prerender, with no network fetch. "
                    "It will be MISSING on replay. To record it: pause, "
                    "then use 'Capture this page' while viewing it.", url)

    # -- main loop -----------------------------------------------------------
    def run(self) -> dict:
        """Own the browser lifecycle: launch, record, finalise the WARC."""
        mode = self.browser_cfg.mode
        if mode not in ("headed", "native"):
            raise ValueError(
                "Interactive recording needs a visible browser: set browser "
                "mode to 'headed' (recommended) or 'native', not "
                f"{mode!r}")
        if mode == "native":
            log.warning(
                "native mode attaches to an existing browser context, so "
                "service workers cannot be blocked there — sites that route "
                "media through a service worker may not capture fully. "
                "Use 'headed' mode for maximum capture fidelity.")
        # recordings must never emulate a viewport: the page has to track the
        # real window so the widget (fixed, bottom-right) is always on screen
        self.browser_cfg.viewport = None
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
        context.on("requestfinished", self._on_request_finished)
        context.on("requestfailed", self._on_request_failed)
        context.on("page", self._on_page)
        context.on("close", self._mark_closed)
        try:
            context.expose_binding("swmControl", self._on_widget_command)
            context.add_init_script(_WIDGET_JS)
        except Exception as exc:
            log.warning("In-page control widget unavailable: %s "
                        "(recording continues; use Ctrl+C / dashboard to "
                        "control it)", exc)
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
                while self._commands:          # widget-queued commands
                    self.apply(self._commands.popleft())
                if self._state_dirty:
                    self._sync_widgets()
                self._check_nav_watch()
                now = time.monotonic()
                if now - last_report >= 1.0:
                    self._report()
                    self._ensure_widgets()
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
        self._log_capture_summary()
        return {"visited": self.visited, "bytes": self.warc.total_bytes,
                "current_url": self.current_url}

    def _log_capture_summary(self) -> None:
        s = self.capture_stats
        hosts = sorted(((n, k[5:]) for k, n in s.items()
                        if k.startswith("host:")), reverse=True)
        log.info("Capture summary: %d exchange(s) from %d host(s)"
                 "%s%s",
                 s.get("captured", 0), len(hosts),
                 f", {s['body-retry-ok']} recovered on retry"
                 if s.get("body-retry-ok") else "",
                 f", {s['body-unavailable']} bodies UNAVAILABLE "
                 "(recorded empty)" if s.get("body-unavailable") else "")
        if s.get("page-doc-missing"):
            log.warning("  %d page(s) displayed without a captured document "
                        "(cache/prerender) — they will be missing on replay",
                        s["page-doc-missing"])
        for n, host in hosts[:10]:
            log.info("  %5d  %s", n, host)
        for key, n in s.items():
            if key.startswith("body-unavailable:"):
                log.warning("  body unavailable x%d from %s", n, key[17:])

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
