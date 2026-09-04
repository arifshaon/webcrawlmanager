"""Crawl orchestrator: runs each seed with its own browser, scope, frontier
and WARC session, reporting to and taking direction from a Controller.
"""

from __future__ import annotations

import logging
import time
from collections import Counter

from .browser import BrowserDriver
from .capture import WarcSession
from .config import CrawlConfig, SeedConfig
from .control import Controller, NullController
from .detect import BlockController, detect_block, is_waf_challenge
from .frontier import Frontier
from .frontier import RobotsCache
from .scope import ScopeMatcher, canonicalize
from .store import COMPLETED, RUNNING, STOPPED

log = logging.getLogger(__name__)

_REFETCH_STRIP = {
    "host", "content-length", "connection", "transfer-encoding",
    "accept-encoding",
}


def _refetch_headers(headers: object) -> dict[str, str]:
    """Keep meaningful browser-request headers for a browser-context refetch.

    The request context shares the browser's cookies. Transport-specific headers
    are recalculated, while Range, Referer, Origin, Accept, Authorization and
    similar representation-affecting fields are preserved.
    """
    if not isinstance(headers, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _REFETCH_STRIP
    }


def _dispose_response(response) -> None:
    try:
        response.dispose()
    except Exception:
        pass


def _write_pdf_response(warc: WarcSession, driver: BrowserDriver,
                        response) -> None:
    """Capture a PDF document through the browser request context.

    Chromium's PDF viewer can expose its HTML shell through response.body(). Once
    a response is identified as a PDF document, that browser body is never used.
    A failed refetch is represented as an empty response and logged clearly rather
    than silently storing viewer HTML under the PDF URL.
    """
    request = response.request
    headers = _refetch_headers(request.headers)
    direct = None
    try:
        direct = driver.context.request.get(
            response.url, headers=headers, timeout=45_000)
        if direct.ok:
            body = direct.body()
            warc.write_exchange(
                url=response.url,
                method="GET",
                req_headers=headers,
                post_data=None,
                status=direct.status,
                status_text=direct.status_text or "",
                resp_headers=direct.headers,
                body=body,
            )
            log.info("Captured PDF %s via browser-context refetch", response.url)
            return
        log.warning("PDF refetch returned HTTP %s for %s; recording an empty "
                    "response instead of Chromium viewer HTML",
                    direct.status, response.url)
    except Exception as exc:
        log.warning("PDF refetch failed for %s: %s; recording an empty response "
                    "instead of Chromium viewer HTML", response.url, exc)
    finally:
        if direct is not None:
            _dispose_response(direct)

    warc.write_exchange(
        url=response.url,
        method=request.method,
        req_headers=request.headers,
        post_data=request.post_data_buffer or None,
        status=response.status,
        status_text=response.status_text or "",
        resp_headers=response.headers,
        body=b"",
    )


class PageCapture:
    """Playwright network events -> WARC records, with body-loss accounting.

    Sites that load their records dynamically (search portals, infinite-scroll
    repositories) live or die by their XHR/fetch responses being archived
    intact. Two failure modes used to be silent here:

    - a body not yet readable at the 'response' event (streaming responses, or
      scroll-triggered requests still in flight) was archived as an EMPTY 200,
      which replays as 'we could not load the content'. Bodies are now retried
      at 'requestfinished', mirroring the interactive recorder.
    - WAF bot-challenge verdicts on API calls (e.g. AWS WAF's HTTP 202 with
      x-amzn-waf-action) were archived as if they were the content.

    Both are now counted per page so the crawl loop can warn that a page's
    dynamic content is incomplete in the archive.
    """

    def __init__(self, warc: WarcSession, driver: BrowserDriver):
        self.warc = warc
        self.driver = driver
        self._pending: dict = {}          # request -> response awaiting body
        self.page_counts: Counter = Counter()

    # -- event handlers ------------------------------------------------------
    def on_response(self, response):
        try:
            request = response.request
            if 300 <= response.status < 400:
                # redirects never expose a readable body
                self._write(response, b"")
                return
            ctype = (response.headers.get("content-type") or "").lower()
            if (ctype.startswith("application/pdf")
                    and request.resource_type == "document"):
                _write_pdf_response(self.warc, self.driver, response)
                return
            self._note_suspect(response, request)
            try:
                body = response.body()
            except Exception:
                # retry when the transfer completes (requestfinished)
                self._pending[request] = response
                return
            self._write(response, body)
        except Exception as exc:
            log.debug("Capture skipped for %s: %s", response.url, exc)

    def on_request_finished(self, request):
        response = self._pending.pop(request, None)
        if response is None:
            return
        try:
            body = response.body()
        except Exception as exc:
            self.page_counts["body-unavailable"] += 1
            log.warning("Body unavailable for %s (%s) — archived with an "
                        "empty body; this resource will be missing/broken "
                        "on replay", response.url, exc)
            self._write(response, b"")
            return
        self._write(response, body)

    def on_request_failed(self, request):
        response = self._pending.pop(request, None)
        if response is not None:
            # a response arrived but its transfer never completed (typically
            # an XHR cancelled by navigating away mid-flight)
            self.page_counts["lost-inflight"] += 1
            log.warning("In-flight response for %s was cancelled before its "
                        "body arrived — not archived", response.url)

    # -- helpers -------------------------------------------------------------
    def _note_suspect(self, response, request) -> None:
        """Count subresource responses that are WAF verdicts or errors: the
        archived page will replay without the content they should carry."""
        try:
            if is_waf_challenge(response.headers):
                self.page_counts["waf-challenged"] += 1
                log.warning("WAF challenge verdict archived for %s (the real "
                            "content of this request is NOT in the archive)",
                            response.url)
            elif (response.status >= 400
                    and request.resource_type in ("xhr", "fetch")):
                self.page_counts["subresource-error"] += 1
        except Exception:
            pass

    def _write(self, response, body: bytes) -> None:
        try:
            request = response.request
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
        except Exception as exc:
            log.debug("Capture skipped for %s: %s", response.url, exc)

    def take_page_report(self) -> dict:
        """Return and reset this page's dynamic-content problem counters."""
        report = {k: v for k, v in self.page_counts.items() if v}
        self.page_counts.clear()
        return report


def _capture_nonrenderable_url(warc: WarcSession, driver: BrowserDriver,
                               url: str) -> bool:
    """Try a failed navigation as a PDF or attachment download.

    Do not convert every navigation error into a successful direct GET: timeouts,
    TLS failures and ordinary HTML failures must remain failures. Only responses
    explicitly identified as PDF or attachment content are accepted here.
    """
    direct = driver.fetch_direct(url)
    if direct is None:
        return False
    try:
        ctype = (direct.headers.get("content-type") or "").lower()
        disposition = (direct.headers.get("content-disposition") or "").lower()
        if not (ctype.startswith("application/pdf")
                or "attachment" in disposition):
            return False
        if not direct.ok:
            log.warning("Direct download fetch returned HTTP %s for %s",
                        direct.status, url)
            return False
        body = direct.body()
        warc.write_exchange(
            url=url,
            method="GET",
            req_headers={},
            post_data=None,
            status=direct.status,
            status_text=direct.status_text or "",
            resp_headers=direct.headers,
            body=body,
        )
        log.info("Captured %s via direct fetch (PDF/attachment)", url)
        return True
    finally:
        _dispose_response(direct)


def crawl_seed(seed: SeedConfig, crawl: CrawlConfig, seed_idx: int,
               controller: Controller) -> dict:
    log.info("=== Seed %d: %s (mode=%s, scope=%s, depth<=%d, pages<=%d)",
             seed_idx, seed.url, seed.browser.mode, seed.scope.strategy,
             seed.scope.max_depth, seed.scope.max_pages)

    scope = ScopeMatcher(seed.url, seed.scope)
    frontier = Frontier(seed.scope.max_depth, seed.scope.max_pages)
    frontier.add(scope.seed, 0)
    robots = RobotsCache("webarc") if seed.behavior.obey_robots else None

    warc = WarcSession(crawl.output_dir, crawl.crawl_name, seed.url,
                       seed_idx, crawl.operator, seed.warc,
                       metadata_fields=seed_metadata(crawl, seed.url))
    stats = {"visited": 0, "skipped_robots": 0, "failed": 0, "blocked": 0,
             "dynamic_incomplete": 0, "consent_dismissed": 0,
             "consent_unresolved": 0}
    consent_labels_logged = False
    controller.seed_status(seed_idx, RUNNING)
    blocks = BlockController(seed.behavior)
    stopped = False
    blocked_out = False

    try:
        with BrowserDriver(seed.browser, seed.behavior) as driver:
            capture = PageCapture(warc, driver)
            page = driver.new_page(capture.on_response)
            page.on("requestfinished", capture.on_request_finished)
            page.on("requestfailed", capture.on_request_failed)

            while (item := frontier.next()) is not None:
                # honour pause (blocks) and stop (breaks) between pages
                controller.wait_if_paused()
                if controller.should_stop():
                    log.info("Seed %d received stop", seed_idx)
                    stopped = True
                    break

                url, depth = item

                if robots and not robots.allowed(url):
                    log.info("robots.txt disallows %s", url)
                    stats["skipped_robots"] += 1
                    controller.report(seed_idx,
                                      skipped_robots=stats["skipped_robots"])
                    continue

                log.info("[depth %d | %d queued] %s", depth, len(frontier), url)
                controller.report(seed_idx, current_url=url, queued=len(frontier))
                resp = driver.visit(page, url)
                frontier.mark_done()
                consent = getattr(driver, "last_consent", None)
                if consent and consent.get("clicked"):
                    if consent.get("dismissed"):
                        stats["consent_dismissed"] += 1
                        log.info("Dismissed a consent overlay on %s "
                                 "(%s: %r)", url, consent.get("kind"),
                                 consent.get("label"))
                    else:
                        stats["consent_unresolved"] += 1
                        log.warning(
                            "Clicked %r on %s but the consent overlay is "
                            "still there; the page may be archived behind it",
                            consent.get("label"), url)
                elif consent and consent.get("labels") \
                        and not consent_labels_logged:
                    # Recorded once per seed. A crawl that dismissed nothing
                    # should say what was on offer rather than leave it to be
                    # guessed at from the archive afterwards.
                    consent_labels_logged = True
                    log.info("No consent control matched on %s; controls "
                             "seen: %s", url,
                             ", ".join(repr(l) for l in consent["labels"][:12]))
                # dynamic-content health for the page just visited (counters
                # reset here so problems attribute to the right page)
                dyn = capture.take_page_report()
                if dyn:
                    stats["dynamic_incomplete"] += 1
                    log.warning(
                        "Dynamic content of %s is likely incomplete in the "
                        "archive (%s) — on replay this page may show missing "
                        "records or 'could not load content' errors", url,
                        ", ".join(f"{k}={v}" for k, v in sorted(dyn.items())))
                if resp is None:
                    if _capture_nonrenderable_url(warc, driver, url):
                        stats["visited"] += 1
                        controller.report(seed_idx, visited=stats["visited"],
                                          queued=len(frontier),
                                          bytes_written=warc.total_bytes)
                        driver.inter_page_delay()
                        continue
                    stats["failed"] += 1
                    controller.report(seed_idx, failed=stats["failed"])
                    continue

                # -- WAF / bot-block handling ---------------------------------
                if seed.behavior.detect_blocks:
                    title, html = driver.page_signature(page)
                    blocked, reason = detect_block(resp.status, title, html)
                    decision = blocks.record(blocked)
                    if blocked:
                        stats["blocked"] += 1
                        log.warning("Block detected at %s (%s) [%d in a row]",
                                    url, reason, decision.consecutive)
                        controller.report(seed_idx, status="blocked",
                                          current_url=url)
                        if decision.action == "stop":
                            log.error("Seed %d: persistent blocking — stopping; "
                                      "the rest of the site is likely gated too",
                                      seed_idx)
                            blocked_out = True
                            break
                        # back off: slow the crawl and cool down before next page
                        driver.delay_multiplier = decision.delay_multiplier
                        log.info("Backing off %.0fs, slowing to %.1fx",
                                 decision.cooldown, decision.delay_multiplier)
                        time.sleep(decision.cooldown)
                        # don't harvest links from a block page
                        driver.inter_page_delay()
                        continue
                    elif decision.recovered:
                        log.info("Seed %d: block cleared, resuming normal pace",
                                 seed_idx)
                        driver.delay_multiplier = 1.0
                        controller.seed_status(seed_idx, RUNNING)

                stats["visited"] += 1

                if depth < seed.scope.max_depth:
                    for href in driver.extract_links(page):
                        canon = canonicalize(href, base=url)
                        if canon and scope.in_scope(canon):
                            frontier.add(canon, depth + 1)

                controller.report(seed_idx,
                                  visited=stats["visited"],
                                  queued=len(frontier),
                                  bytes_written=warc.total_bytes)
                driver.inter_page_delay()
    finally:
        warc.close()

    if blocked_out:
        final = "blocked"
    elif stopped:
        final = STOPPED
    else:
        final = COMPLETED
    controller.report(seed_idx, status=final, visited=stats["visited"],
                      queued=len(frontier), bytes_written=warc.total_bytes)
    log.info("Seed %d %s: %s", seed_idx, final, stats)
    return stats


def seed_metadata(crawl: CrawlConfig, seed_url: str) -> list[dict]:
    """What this seed's outputs say about it: its own fields over the
    job's, with the capture's own facts filling anything left empty."""
    from .metadata import defaults_for, merge, with_defaults
    meta = getattr(crawl, "metadata", None) or {"job": [], "seeds": {}}
    return with_defaults(
        merge(meta.get("job", []), meta.get("seeds", {}).get(seed_url)),
        defaults_for("crawl", crawl.crawl_name, crawl.operator, seed_url))


def write_crawl_metadata(crawl: CrawlConfig, job_id=None) -> None:
    """metadata.json in the output folder, before the first seed runs."""
    from .metadata import document, read_document, write_document
    meta = getattr(crawl, "metadata", None) or {"job": [], "seeds": {}}
    write_document(crawl.output_dir, document(
        job_id=job_id, kind="crawl", name=crawl.crawl_name,
        operator=crawl.operator, seeds=[{"url": s.url} for s in crawl.seeds],
        metadata=meta, existing=read_document(crawl.output_dir)))


def run_crawl(crawl: CrawlConfig, controller: Controller | None = None) -> None:
    controller = controller or NullController()
    try:
        write_crawl_metadata(crawl)
    except OSError as exc:                 # pragma: no cover - a full disk
        log.warning("Could not write metadata.json: %s", exc)
    for idx, seed in enumerate(crawl.seeds, start=1):
        if controller.should_stop():
            log.info("Crawl stop requested; skipping remaining seeds")
            break
        try:
            crawl_seed(seed, crawl, idx, controller)
        except Exception:
            log.exception("Seed %d (%s) aborted", idx, seed.url)
            controller.seed_status(idx, "failed")
