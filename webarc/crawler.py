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
from .frontier import Frontier, RobotsCache
from .scope import ScopeMatcher, canonicalize
from .store import COMPLETED, RUNNING, STOPPED

log = logging.getLogger(__name__)


def _make_response_handler(warc: WarcSession, driver: "BrowserDriver"):
    """Playwright 'response' event -> WARC request+response records."""

    def on_response(response):
        try:
            request = response.request
            # PDF documents are taken over by Chromium's viewer and body()
            # returns the viewer shell, not the PDF; refetch them directly
            ctype = (response.headers.get("content-type") or "").lower()
            if (ctype.startswith("application/pdf")
                    and request.resource_type == "document"):
                direct = driver.fetch_direct(response.url)
                if direct is not None and direct.ok:
                    warc.write_exchange(
                        url=response.url, method="GET",
                        req_headers=request.headers, post_data=None,
                        status=direct.status,
                        status_text=direct.status_text or "",
                        resp_headers=direct.headers, body=direct.body())
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
                    # navigations to PDFs/attachments become downloads and
                    # "fail"; capture such resources with a direct request
                    # through the same browser context instead
                    direct = driver.fetch_direct(url)
                    if direct is not None and direct.ok:
                        warc.write_exchange(
                            url=url, method="GET", req_headers={},
                            post_data=None, status=direct.status,
                            status_text=direct.status_text or "",
                            resp_headers=direct.headers, body=direct.body())
                        stats["visited"] += 1
                        log.info("Captured %s via direct fetch (download/"
                                 "non-renderable resource)", url)
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
