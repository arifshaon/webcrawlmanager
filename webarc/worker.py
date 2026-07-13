"""Worker subprocess: runs one crawl to completion under a StoreController.

Launched by the server as:
    python -m webarc.worker <crawl_id> --db <path>

--simulate runs a browserless fake crawl (timed page visits writing tiny WARC
records) so the dashboard, control plane, and storage accounting can be
exercised end-to-end without Playwright installed. Real crawls omit the flag.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .config import CrawlConfig, SeedConfig, _build_section
from .config import (BehaviorConfig, BrowserConfig, ScopeConfig, WarcConfig)
from .control import StoreController
from .store import (COMPLETED, FAILED, RUNNING, STOPPED, Store)

log = logging.getLogger("webarc.worker")


def _config_from_row(row: dict) -> CrawlConfig:
    import json
    raw = json.loads(row["config_json"])
    defaults = raw.get("defaults", {})
    import copy

    def merge(base, over):
        out = copy.deepcopy(base)
        for k, v in (over or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(
                out.get(k), dict) else copy.deepcopy(v)
        return out

    seeds = []
    for seed_raw in raw.get("seeds", []):
        m = merge(defaults, {k: v for k, v in seed_raw.items() if k != "url"})
        seeds.append(SeedConfig(
            url=seed_raw["url"],
            browser=_build_section(BrowserConfig, m.get("browser", {})),
            scope=_build_section(ScopeConfig, m.get("scope", {})),
            behavior=_build_section(BehaviorConfig, m.get("behavior", {})),
            warc=_build_section(WarcConfig, m.get("warc", {})),
        ))
    return CrawlConfig(
        crawl_name=raw.get("crawl_name", row["name"]),
        output_dir=Path(row["output_dir"]),
        operator=raw.get("operator", "webarc"),
        seeds=seeds,
    )


def _simulate(crawl: CrawlConfig, controller: StoreController) -> None:
    """Browserless fake crawl for testing the control plane."""
    from .capture import WarcSession
    body = b"<html><body>simulated capture</body></html>"
    for idx, seed in enumerate(crawl.seeds, start=1):
        controller.seed_status(idx, RUNNING)
        warc = WarcSession(crawl.output_dir, crawl.crawl_name, seed.url,
                           idx, crawl.operator, seed.warc)
        visited = queued = 0
        stopped = False
        total = min(seed.scope.max_pages, 12)  # keep the demo short
        try:
            for n in range(total):
                controller.wait_if_paused()
                if controller.should_stop():
                    stopped = True
                    break
                url = f"{seed.url}#page{n+1}"
                queued = total - n - 1
                controller.report(idx, current_url=url, queued=queued)
                warc.write_exchange(
                    url=url, method="GET",
                    req_headers={"user-agent": "webarc-sim"},
                    post_data=None, status=200, status_text="OK",
                    resp_headers={"content-type": "text/html"},
                    body=body + str(n).encode())
                visited += 1
                controller.report(idx, visited=visited, queued=queued,
                                  bytes_written=warc.total_bytes)
                time.sleep(1.0)
        finally:
            warc.close()
        controller.report(idx, status=STOPPED if stopped else COMPLETED,
                          visited=visited, bytes_written=warc.total_bytes)
        if stopped:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webarc.worker")
    parser.add_argument("crawl_id", type=int)
    parser.add_argument("--db", required=True)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    store = Store(args.db)
    controller = StoreController(store, args.crawl_id)
    row = store.get_crawl(args.crawl_id)
    if not row:
        log.error("Crawl %d not found", args.crawl_id)
        return 1

    store.set_pid(args.crawl_id, os.getpid())
    store.set_status(args.crawl_id, RUNNING)
    store.clear_control(args.crawl_id)

    try:
        crawl = _config_from_row(row)
        if args.simulate:
            _simulate(crawl, controller)
        else:
            from .crawler import run_crawl
            run_crawl(crawl, controller)
    except Exception as exc:
        log.exception("Crawl %d failed", args.crawl_id)
        store.set_status(args.crawl_id, FAILED, error=str(exc))
        return 1

    # decide final crawl-level status from control state
    if controller.should_stop():
        store.set_status(args.crawl_id, STOPPED)
    else:
        store.set_status(args.crawl_id, COMPLETED)
    store.clear_control(args.crawl_id)
    log.info("Crawl %d finished", args.crawl_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
