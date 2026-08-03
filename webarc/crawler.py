"""Crawl orchestrator: runs each seed with its own browser, scope, frontier
and WARC session, reporting to and taking direction from a Controller.
"""

from __future__ import annotations

import logging
import time

from .browser import BrowserDriver
from .capture import WarcSession
from .config import CrawlConfig, SeedConfig
from .control import Controller, NullController
from .detect import BlockController, detect_block
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


def _make_response_handler(warc: WarcSession, driver: BrowserDriver):
    """Playwright 'response' event -> WARC request+response records."""

    def on_response(response):
        try:
            request = response.request
            ctype = (response.headers.get("content-type") or "").lower()
            if (ctype.startswith("application/pdf")
                    and request.resource_type == "document"):
                _write_pdf_response(warc, driver, response)
                return

            try:
                body = response.body()
            except Exception:
                body = b""  # redirects / cached / aborted bodies

            post = request.post_data_buffer or None
            warc.write_exchange(
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

    return on_response


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
                       seed_idx, crawl.operator, seed.warc)
    stats = {"visited": 0, "skipped_robots": 0, "failed": 0, "blocked": 0}
    controller.seed_status(seed_idx, RUNNING)
    blocks = BlockController(seed.behavior)
    stopped = False
    blocked_out = False

    try:
        with BrowserDriver(seed.browser, seed.behavior) as driver:
            page = driver.new_page(_make_response_handler(warc, driver))

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


def run_crawl(crawl: CrawlConfig, controller: Controller | None = None) -> None:
    controller = controller or NullController()
    for idx, seed in enumerate(crawl.seeds, start=1):
        if controller.should_stop():
            log.info("Crawl stop requested; skipping remaining seeds")
            break
        try:
            crawl_seed(seed, crawl, idx, controller)
        except Exception:
            log.exception("Seed %d (%s) aborted", idx, seed.url)
            controller.seed_status(idx, "failed")
