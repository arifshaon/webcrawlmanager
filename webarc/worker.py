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
from .store import (BLOCKED, COMPLETED, CTRL_PAUSE, CTRL_RESUME, CTRL_STOP,
                    FAILED, KIND_FACEBOOK, KIND_INSTAGRAM, KIND_RECORDING,
                    PAUSED, RUNNING,
                    STOPPED, Store)

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
    from .metadata import from_config
    return CrawlConfig(
        crawl_name=raw.get("crawl_name", row["name"]),
        output_dir=Path(row["output_dir"]),
        operator=raw.get("operator", "webarc"),
        seeds=seeds,
        metadata=from_config(raw),
    )


def _job_metadata(row: dict, kind: str, seed_urls: list[str],
                  operator: str) -> dict[str, list[dict]]:
    """Each seed's effective metadata for a social or recorded job, and the
    metadata.json beside its outputs, from what the server stored."""
    import json

    from .metadata import (defaults_for, document, from_config, merge,
                           read_document, with_defaults, write_document)

    raw = json.loads(row["config_json"])
    meta = from_config(raw)
    out_dir = Path(row["output_dir"])
    try:
        write_document(out_dir, document(
            job_id=row.get("id"), kind=kind, name=row["name"], operator=operator,
            seeds=[{"url": u} for u in seed_urls], metadata=meta,
            existing=read_document(out_dir)))
    except OSError as exc:                       # pragma: no cover
        log.warning("Could not write metadata.json: %s", exc)
    return {url: with_defaults(merge(meta["job"], meta["seeds"].get(url)),
                               defaults_for(kind, row["name"], operator, url))
            for url in seed_urls}


def _describe_manifest(row: dict) -> None:
    """After a social capture: the manifest carries the metadata too."""
    from .metadata import read_document, update_manifest
    doc = read_document(Path(row["output_dir"]))
    if doc:
        update_manifest(Path(row["output_dir"]), doc)


def _simulate(crawl: CrawlConfig, controller: StoreController) -> None:
    """Browserless fake crawl for testing the control plane."""
    from .capture import WarcSession
    body = b"<html><body>simulated capture</body></html>"
    for idx, seed in enumerate(crawl.seeds, start=1):
        controller.seed_status(idx, RUNNING)
        from .crawler import seed_metadata, write_crawl_metadata
        if idx == 1:
            write_crawl_metadata(crawl)
        warc = WarcSession(crawl.output_dir, crawl.crawl_name, seed.url,
                           idx, crawl.operator, seed.warc,
                           metadata_fields=seed_metadata(crawl, seed.url))
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


def _run_recording(store: Store, crawl_id: int, row: dict) -> None:
    """Run an interactive recording session under dashboard control.

    Unlike crawls, pause must NOT block the worker: the browser stays usable
    while capture is paused, so the store's control column is polled
    non-blockingly and mapped onto the recorder's state machine. Widget and
    dashboard commands both land in the same RecordingSession.apply()."""
    import json

    from .capture import WarcSession
    from .recorder import (CMD_PAUSE, CMD_RESUME, CMD_STOP, PAUSED as R_PAUSED,
                           RECORDING as R_RECORDING, RecordingSession)

    raw = json.loads(row["config_json"])
    rec = raw.get("recording", {})
    start_url = rec.get("start_url") or raw["seeds"][0]["url"]
    browser = _build_section(BrowserConfig, rec.get("browser", {}))
    if browser.mode not in ("headed", "native"):
        browser.mode = "headed"

    described = _job_metadata(row, KIND_RECORDING, [start_url],
                              rec.get("operator", "webarc"))
    warc = WarcSession(
        Path(row["output_dir"]), row["name"], start_url, 1,
        rec.get("operator", "webarc"), WarcConfig(),
        info_extra={
            "robots": "none",
            "description": f"Interactive session recording starting "
                           f"at {start_url}",
        },
        metadata_fields=described.get(start_url))

    def control_poll():
        command = store.get_control(crawl_id)
        if command == CTRL_STOP:
            return CMD_STOP          # left set; main() reads it for final status
        if command == CTRL_PAUSE:
            store.clear_control(crawl_id)
            return CMD_PAUSE
        if command == CTRL_RESUME:
            store.clear_control(crawl_id)
            return CMD_RESUME
        return None

    last_state = {"state": None}

    def on_progress(state, visited, bytes_written, current_url):
        store.update_progress(crawl_id, 1, status=state, visited=visited,
                              bytes_written=bytes_written,
                              current_url=current_url)
        if state != last_state["state"]:
            last_state["state"] = state
            if state == R_PAUSED:
                store.set_status(crawl_id, PAUSED)
            elif state == R_RECORDING:
                store.set_status(crawl_id, RUNNING)

    session = RecordingSession(start_url, browser, warc,
                               control_poll=control_poll,
                               on_progress=on_progress)
    session.run()


class _NullWarcSession:
    """Stands in for the WARC writer when a capture opts out of writing one.

    Accepts and discards exchanges, so every capture path stays identical
    whether or not a WARC is being produced.
    """

    def __init__(self, out_dir, *_args, **_kwargs):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.total_bytes = 0

    def write_exchange(self, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def _run_facebook(store: Store, crawl_id: int, row: dict) -> dict:
    """Run a visible, curator-controlled Facebook Page capture."""
    import json

    from .facebook import (BLOCKED as FB_BLOCKED, FacebookCaptureConfig,
                           FacebookCaptureSession, FacebookWarcSession)

    raw = json.loads(row["config_json"])
    fb_raw = raw.get("facebook", {})
    fb_config = FacebookCaptureConfig.from_dict(fb_raw)
    browser = _build_section(BrowserConfig, fb_raw.get("browser", {}))
    if browser.mode not in ("headed", "native"):
        browser.mode = "headed"
    operator = str(fb_raw.get("operator") or "webarc")
    output_dir = Path(row["output_dir"])

    # A Facebook capture can be run without a WARC: its records, media and
    # rendered pages stand on their own, and replay of a Facebook feed is
    # limited to the page as first loaded in any case.
    described = _job_metadata(row, KIND_FACEBOOK, [fb_config.page_url], operator)
    warc_cls = FacebookWarcSession if fb_config.write_warc else _NullWarcSession
    warc = warc_cls(
        output_dir, row["name"], fb_config.page_url, 1, operator,
        WarcConfig(),
        info_extra={
            "robots": "none",
            "description": (
                "Curator-controlled Facebook Page capture. Automatic scrolling "
                "may be paused while WARC capture remains active."
            ),
            "facebook-capture-mode": fb_config.mode,
            "facebook-page-key": fb_config.page_key,
        },
        metadata_fields=described.get(fb_config.page_url),
    )

    def control_poll():
        command = store.get_control(crawl_id)
        if command == CTRL_STOP:
            return "stop"
        if command == CTRL_PAUSE:
            store.clear_control(crawl_id)
            return "pause"
        if command == CTRL_RESUME:
            store.clear_control(crawl_id)
            return "resume"
        return None

    last_state = {"state": None}

    def on_progress(state, visited, bytes_written, current_url,
                    queued=0, failed=0, details=None):
        store.update_progress(
            crawl_id, 1, status=state, visited=visited, queued=queued,
            failed=failed, bytes_written=bytes_written,
            current_url=current_url, details=details or {},
        )
        if state != last_state["state"]:
            last_state["state"] = state
            if state == PAUSED:
                store.set_status(crawl_id, PAUSED)
            elif state == FB_BLOCKED:
                store.set_status(crawl_id, BLOCKED)
            elif state == "recording":
                store.set_status(crawl_id, RUNNING)

    def persist_posts(posts: list[dict], page_name: str | None) -> None:
        store.record_facebook_posts(
            fb_config.page_key, fb_config.page_url, page_name, crawl_id, posts)

    session = FacebookCaptureSession(
        config=fb_config,
        browser_cfg=browser,
        warc=warc,
        output_dir=output_dir,
        crawl_id=crawl_id,
        crawl_name=row["name"],
        operator=operator,
        known_post_ids=store.get_facebook_post_ids(fb_config.page_key),
        control_poll=control_poll,
        on_progress=on_progress,
        persist_posts=persist_posts,
    )
    return session.run()


def _run_instagram(store: Store, crawl_id: int, row: dict) -> dict:
    """Run an Instagram capture through the signed-in browser.

    The browser is Chrome on the dedicated Instagram profile, headed or
    native as the job chose, and with a window or without one. Instagram is
    served to it as its own client. A sign-in or a checkpoint is shown in
    that browser -- a window is opened for it when the run had none -- and
    the session is read from it again once the curator continues. With the
    WARC option on, every exchange the browser makes is written as it
    happens, credentials redacted.
    """
    import json

    from .facebook import FacebookWarcSession
    from .instagram import (BLOCKED as IG_BLOCKED, InstagramCaptureConfig,
                            InstagramCaptureSession, parse_instagram_target)
    from .instagram_browser import InstagramBrowserClient

    raw = json.loads(row["config_json"])
    config = InstagramCaptureConfig.from_dict(raw.get("instagram", {}))
    output_dir = Path(row["output_dir"])

    described = _job_metadata(row, KIND_INSTAGRAM, list(config.targets),
                              config.operator)
    warc = None
    if config.write_warc:
        # one WARC for the whole capture: its record describes the first
        # target; every target's description is in metadata.json
        warc = FacebookWarcSession(
            output_dir, row["name"], config.targets[0], 1, config.operator,
            WarcConfig(),
            info_extra={
                "robots": "none",
                "description": ("Instagram capture through the signed-in "
                                "browser: how Instagram presented what was "
                                "collected. The media files, raw payloads and "
                                "normalised records beside it are the primary "
                                "record."),
            },
            metadata_fields=described.get(config.targets[0]))
    client = InstagramBrowserClient(
        BrowserConfig(mode=config.browser_mode,
                      user_data_dir=config.browser_profile_dir,
                      chrome_path=config.chrome_path),
        warc=warc, headless=config.headless)
    client.start()
    if config.listing == "gallery-dl":
        # the listing from gallery-dl on the browser's session; media,
        # comments and evidence still through the browser
        from .instagram_gallery import GalleryListingClient, discovery_limit
        client = GalleryListingClient(
            client, limit=discovery_limit(config.mode, config.latest_n),
            scratch_dir=Path(store.db_path).resolve().parent / "tmp")

    def control_poll():
        command = store.get_control(crawl_id)
        if command == CTRL_STOP:
            return "stop"
        if command == CTRL_PAUSE:
            store.clear_control(crawl_id)
            return "pause"
        if command == CTRL_RESUME:
            store.clear_control(crawl_id)
            return "resume"
        return None

    last_state = {"state": None}

    def on_progress(state, visited, current_url, failed=0, details=None,
                    **_ignored):
        store.update_progress(
            crawl_id, 1, status=state, visited=visited, failed=failed,
            current_url=current_url, details=details or {})
        if state != last_state["state"]:
            last_state["state"] = state
            if state == PAUSED:
                store.set_status(crawl_id, PAUSED)
            elif state == IG_BLOCKED:
                store.set_status(crawl_id, BLOCKED)
            elif state == "recording":
                store.set_status(crawl_id, RUNNING)

    def persist(targets, posts, profiles):
        for key, newest in targets.items():
            newest.setdefault("url", next(
                (u for u in config.targets if key.endswith("@" + str(
                    newest.get("username") or ""))), ""))
        store.record_instagram_capture(crawl_id, targets, posts, {})

    known = {}
    for url in config.targets:
        target = parse_instagram_target(url)
        known[target.key] = store.get_instagram_media_ids(target.key)

    capture = InstagramCaptureSession(
        config=config, client=client, output_dir=output_dir,
        crawl_id=crawl_id, crawl_name=row["name"], known_ids=known,
        control_poll=control_poll, on_progress=on_progress, persist=persist,
        open_browser=client.show)
    try:
        result = capture.run()
    finally:
        if warc is not None:
            try:
                warc.close()
            except Exception:
                pass
        client.close()
    if warc is not None:
        result["warc_files"] = len(list(output_dir.glob("*.warc.gz")))
    return result


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

    kind = row.get("kind", "crawl")
    facebook_result: dict | None = None
    try:
        if args.simulate:
            # recordings simulate fine too: config_json carries a one-seed
            # seeds list, so the browserless fake crawl exercises the same
            # control plane and storage accounting
            _simulate(_config_from_row(row), controller)
        elif kind == KIND_RECORDING:
            _run_recording(store, args.crawl_id, row)
        elif kind == KIND_FACEBOOK:
            facebook_result = _run_facebook(store, args.crawl_id, row)
        elif kind == KIND_INSTAGRAM:
            facebook_result = _run_instagram(store, args.crawl_id, row)
        else:
            from .crawler import run_crawl
            run_crawl(_config_from_row(row), controller)
    except Exception as exc:
        log.exception("Crawl %d failed", args.crawl_id)
        store.set_status(args.crawl_id, FAILED, error=str(exc))
        return 1

    # decide final crawl-level status from control state
    facebook_stop = (facebook_result or {}).get("stop_reason") \
        if kind in (KIND_FACEBOOK, KIND_INSTAGRAM) else None
    if facebook_stop == "unsupported_personal_profile":
        # Not a crash, but not a completed capture either: record why, so the
        # dashboard shows the reason rather than an empty successful run.
        store.set_status(
            args.crawl_id, FAILED,
            error=(facebook_result or {}).get("detail")
            or "The requested URL is a personal Facebook profile, not a Page.")
    elif facebook_stop in ("browser_closed", "curator_stop"):
        store.set_status(args.crawl_id, STOPPED)
    elif controller.should_stop():
        store.set_status(args.crawl_id, STOPPED)
    else:
        store.set_status(args.crawl_id, COMPLETED)
    store.clear_control(args.crawl_id)
    log.info("Crawl %d finished", args.crawl_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
