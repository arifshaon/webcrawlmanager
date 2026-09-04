"""CLI for Simple Webcrawl Manager (SWM)."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .crawler import run_crawl


APP_NAME = "Simple Webcrawl Manager (SWM)"


def _resolve_cli_replay_url(warc_paths, preferred_url: str | None):
    """Resolve a CLI replay URL against targets that actually exist in WARC.

    ReplayWeb.page looks up archived targets by URL. If the requested URL is
    absent, try only the conservative www/non-www equivalent before falling
    back to the first captured HTML page. Returns ``(url, reason)`` where reason
    is one of exact, host-alias, auto, fallback, or unverified.
    """
    from urllib.parse import urlsplit, urlunsplit

    from warcio.archiveiterator import ArchiveIterator

    def key(value: str):
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = None
        if port and not ((parts.scheme.lower() == "http" and port == 80)
                         or (parts.scheme.lower() == "https" and port == 443)):
            host = f"{host}:{port}"
        return (parts.scheme.lower(), host, parts.path or "/", parts.query)

    captured: dict[tuple, str] = {}
    first_html = None
    for path in warc_paths:
        try:
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type not in ("response", "revisit"):
                        continue
                    uri = record.rec_headers.get_header("WARC-Target-URI")
                    if not uri:
                        continue
                    captured.setdefault(key(uri), uri)
                    if first_html is not None:
                        continue
                    http = record.http_headers
                    if http is None or http.get_statuscode() != "200":
                        continue
                    ctype = (http.get_header("Content-Type") or "").lower()
                    if not ctype.startswith("text/html"):
                        continue
                    if (record.rec_type == "response"
                            and http.get_header("Content-Length") == "0"):
                        continue
                    first_html = uri
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Replay URL scan failed for %s: %s", path, exc)

    if not preferred_url:
        return first_html, "auto" if first_html else "unverified"

    parts = urlsplit(preferred_url)
    fragment = parts.fragment
    preferred_base = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, ""))
    exact = captured.get(key(preferred_base))
    if exact:
        return exact + (f"#{fragment}" if fragment else ""), "exact"

    host = parts.hostname or ""
    if host:
        alternate_host = (host[4:] if host.lower().startswith("www.")
                          else f"www.{host}")
        try:
            port = parts.port
        except ValueError:
            port = None
        alternate_netloc = alternate_host + (f":{port}" if port else "")
        alternate = urlunsplit(
            (parts.scheme, alternate_netloc, parts.path, parts.query, ""))
        matched = captured.get(key(alternate))
        if matched:
            return matched + (f"#{fragment}" if fragment else ""), "host-alias"

    if first_html:
        return first_html, "fallback"
    return preferred_url, "unverified"


def _build_cli_www_alias_warc(warc_paths):
    """Create a temporary replay-only WARC containing conservative host aliases.

    ReplayWeb.page's address box performs its own archive lookup, so resolving
    only the CLI's initial ``--url`` is not enough. When both ``example.org`` and
    ``www.example.org`` already occur in the capture, add a synthetic 302 for
    each missing HTML-page counterpart. The original WARCs are never modified.

    Returns ``(path, count)``. ``path`` is ``None`` when no aliases are needed.
    """
    import tempfile
    from io import BytesIO
    from pathlib import Path
    from urllib.parse import urlsplit, urlunsplit

    from warcio.archiveiterator import ArchiveIterator
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    def key(value: str):
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = None
        if port and not ((parts.scheme.lower() == "http" and port == 80)
                         or (parts.scheme.lower() == "https" and port == 443)):
            host = f"{host}:{port}"
        return (parts.scheme.lower(), host, parts.path or "/", parts.query)

    captured: set[tuple] = set()
    hosts: set[str] = set()
    html_urls: dict[tuple, str] = {}

    for path in warc_paths:
        try:
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type not in ("response", "revisit"):
                        continue
                    uri = record.rec_headers.get_header("WARC-Target-URI")
                    if not uri:
                        continue
                    parts = urlsplit(uri)
                    host = (parts.hostname or "").lower()
                    if parts.scheme.lower() not in ("http", "https") or not host:
                        continue
                    captured.add(key(uri))
                    hosts.add(host)

                    http = record.http_headers
                    if http is None or http.get_statuscode() != "200":
                        continue
                    ctype = (http.get_header("Content-Type") or "").lower()
                    if not ctype.startswith("text/html"):
                        continue
                    if (record.rec_type == "response"
                            and http.get_header("Content-Length") == "0"):
                        continue
                    html_urls.setdefault(key(uri), uri)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Replay alias scan failed for %s: %s", path, exc)

    # Do not assume that www and the bare hostname are equivalent merely from
    # their spelling. Require evidence that both hosts occur in this archive.
    paired_bases: set[str] = set()
    for host in hosts:
        bare = host[4:] if host.startswith("www.") else host
        if bare in hosts and f"www.{bare}" in hosts:
            paired_bases.add(bare)

    aliases: dict[str, str] = {}
    for target in html_urls.values():
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        bare = host[4:] if host.startswith("www.") else host
        if bare not in paired_bases:
            continue
        alternate_host = bare if host.startswith("www.") else f"www.{bare}"
        try:
            port = parts.port
        except ValueError:
            port = None
        alternate_netloc = alternate_host + (f":{port}" if port else "")
        alias = urlunsplit(
            (parts.scheme, alternate_netloc, parts.path, parts.query, ""))
        if key(alias) not in captured:
            aliases.setdefault(alias, target)

    if not aliases:
        return None, 0

    tmp = tempfile.NamedTemporaryFile(
        prefix="swm-replay-alias-", suffix=".warc.gz", delete=False)
    try:
        with tmp:
            writer = WARCWriter(tmp, gzip=True)
            for alias, target in sorted(aliases.items()):
                headers = StatusAndHeaders(
                    "302 Found",
                    [
                        ("Location", target),
                        ("Content-Length", "0"),
                        ("X-SWM-Replay-Alias", "www/non-www"),
                    ],
                    protocol="HTTP/1.1",
                )
                record = writer.create_warc_record(
                    alias,
                    "response",
                    payload=BytesIO(b""),
                    http_headers=headers,
                    warc_content_type="application/http; msgtype=response",
                )
                writer.write_record(record)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    return Path(tmp.name), len(aliases)


def _resource_thresholds_for(db_path: str) -> dict:
    """The warning levels the dashboard holds, or the defaults."""
    from pathlib import Path as _P

    from . import resources

    if db_path and _P(db_path).exists():
        from .store import Store
        return resources.thresholds_from_settings(Store(db_path).get_setting)
    return dict(resources.DEFAULT_THRESHOLDS)


def _running_jobs(db_path: str) -> list[dict]:
    """Jobs the dashboard is running, with what each one is using."""
    from pathlib import Path as _P

    if not db_path or not _P(db_path).exists():
        return []
    from . import resources
    from .server import _ACTIVE_STATES, _pid_alive, _pid_is_worker
    from .store import PENDING, Store

    usage = resources.ProcessUsage()
    rows = [r for r in Store(db_path).list_crawls()
            if r.get("status") in _ACTIVE_STATES + (PENDING,)
            and _pid_alive(r.get("pid")) and _pid_is_worker(r["pid"])]
    for row in rows:                       # first reading of a rate is zero
        usage.usage(row["pid"])
    if rows:
        import time as _t
        _t.sleep(0.5)
    out = []
    for row in rows:
        seen = usage.usage(row["pid"]) or {}
        out.append({"id": row["id"], "name": row["name"],
                    "kind": row.get("kind", "crawl"), "status": row["status"],
                    "pid": row["pid"], **seen})
    return out


def _print_resource_report(snapshot: dict, thresholds: dict, jobs: list[dict],
                           warnings: list[dict]) -> None:
    from .resources import _fmt_bytes

    cpu, mem, disk = snapshot["cpu"], snapshot["memory"], snapshot["disk"]
    pct = lambda v: "not measured" if v is None else f"{v:.0f}% free"
    print("This machine")
    print(f"  CPU     : {pct(cpu.get('free_percent'))}"
          + (f" of {cpu['count']} cores" if cpu.get("count") else ""))
    print(f"  memory  : {pct(mem.get('free_percent'))}"
          + (f" ({_fmt_bytes(mem['available'])} available)" if mem.get("available") is not None else ""))
    print(f"  disk    : {pct(disk.get('free_percent'))} on {disk.get('path')}"
          + (f" ({_fmt_bytes(disk['free'])})" if disk.get("free") else ""))
    print(f"  warn when less than {thresholds['cpu_free_percent']:g}% CPU, "
          f"{thresholds['memory_free_percent']:g}% memory or "
          f"{thresholds['disk_free_percent']:g}% disk is free"
          + ("" if thresholds.get("enabled", True) else " (warnings off)"))
    if jobs:
        print(f"\nRunning jobs ({len(jobs)})")
        print(f"  {'id':>4}  {'CPU':>6}  {'memory':>9}  {'procs':>5}  {'status':9}  name")
        for job in jobs:
            print(f"  {job['id']:>4}  "
                  f"{job.get('cpu_percent_of_machine', 0):>5.0f}%  "
                  f"{_fmt_bytes(job.get('rss_bytes', 0)):>9}  "
                  f"{job.get('processes', 0):>5}  {job['status']:9}  {job['name']}")
    else:
        print("\nNo jobs are running.")
    for warning in warnings:
        print(f"\nWARNING: {warning['message']}")
    if snapshot.get("note"):
        print(f"\nNote: {snapshot['note']}")


def _cmd_metadata(args) -> int:
    from pathlib import Path as _P

    from . import metadata as md

    job_dir = _P(args.job_dir)
    doc = md.read_document(job_dir)
    if not doc:
        print(f"No {md.DOCUMENT_NAME} in {job_dir}: this folder is not a job's, "
              "or the job was made before metadata was recorded.", file=sys.stderr)
        return 1
    text = md.csv_text(doc)
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    target = _P(args.output) if args.output else job_dir / md.CSV_NAME
    target.write_text(text, encoding="utf-8")
    rows = max(0, text.count("\n") - 1)
    print(f"Wrote {target}: {rows} row(s)")
    return 0


def _cmd_resources(args) -> int:
    import json as _json

    from . import resources

    thresholds = _resource_thresholds_for(args.db)
    storage = args.warc_root
    from pathlib import Path as _P
    if args.db and _P(args.db).exists():
        from .store import Store
        stored = (Store(args.db).get_setting("storage_root") or "").strip()
        if stored:
            storage = stored
    snapshot = resources.system_snapshot(storage, cpu_interval=0.5)
    jobs = _running_jobs(args.db)
    warnings = resources.evaluate(snapshot, thresholds)
    if args.json:
        print(_json.dumps({"snapshot": snapshot, "thresholds": thresholds,
                           "jobs": jobs, "warnings": warnings}, indent=2))
    else:
        _print_resource_report(snapshot, thresholds, jobs, warnings)
    return 0


def _resource_gate(output_dir, db_path: str, *, assume_yes: bool = False,
                   wait: bool = False, ask=input, poll_seconds: float = 5.0) -> bool:
    """Before a crawl starts: warn, and let the curator choose.

    Returns True to start, False to cancel. With a shortage and no answer
    given on the command line, an interactive terminal is asked to start,
    wait or cancel; a non-interactive run starts, with the warning printed,
    so an unattended crawl is never left hanging on a prompt.
    """
    import time as _t

    from . import resources

    thresholds = _resource_thresholds_for(db_path)
    if not thresholds.get("enabled", True):
        return True
    snapshot = resources.system_snapshot(output_dir, cpu_interval=0.5)
    warnings = resources.evaluate(snapshot, thresholds)
    if not warnings:
        return True
    for warning in warnings:
        print(f"WARNING: {warning['message']}", file=sys.stderr)
    choice = "start" if assume_yes else "wait" if wait else None
    if choice is None:
        if not sys.stdin.isatty():
            print("Not a terminal: starting anyway (use --wait or "
                  "--no-resource-check to choose ahead of time).", file=sys.stderr)
            return True
        while choice is None:
            answer = ask("Start anyway, wait until it is free, or cancel? [s/w/c] ").strip().lower()
            choice = {"s": "start", "start": "start", "w": "wait", "wait": "wait",
                      "c": "cancel", "cancel": "cancel", "": "cancel"}.get(answer)
    if choice == "cancel":
        print("Cancelled.", file=sys.stderr)
        return False
    if choice == "wait":
        print("Waiting for the machine to have room; press Ctrl-C to give up.",
              file=sys.stderr)
        while warnings:
            _t.sleep(poll_seconds)
            snapshot = resources.system_snapshot(output_dir, cpu_interval=0.5)
            warnings = resources.evaluate(snapshot, thresholds)
        print("Resources are free again; starting.", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swm",
        description=f"{APP_NAME} — browser-based WARC crawler",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Run a crawl from a YAML config")
    p_crawl.add_argument("config", help="Path to config.yaml")
    p_crawl.add_argument("-v", "--verbose", action="store_true")
    p_crawl.add_argument("--db", default="./webarc-state/webarc.db",
                         help="dashboard state file whose resource warning "
                         "levels apply (defaults are used when it does not exist)")
    p_crawl.add_argument("--yes", "-y", action="store_true",
                         help="start even when the machine is short of a "
                         "resource, without asking")
    p_crawl.add_argument("--wait", action="store_true",
                         help="when the machine is short of a resource, wait "
                         "until it is free, then start")
    p_crawl.add_argument("--no-resource-check", action="store_true",
                         help="skip the CPU, memory and disk check")

    p_val = sub.add_parser("validate", help="Parse and print the resolved config")
    p_val.add_argument("config")

    p_srv = sub.add_parser("serve", help="Run the SWM dashboard control server")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8080)
    p_srv.add_argument("--db", default="./webarc-state/webarc.db")
    p_srv.add_argument("--warc-root", default="./warcs")
    p_srv.add_argument(
        "--simulate",
        action="store_true",
        help="run browserless fake crawls (demo/test the UI)",
    )
    p_srv.add_argument(
        "--allow-remote-recording",
        action="store_true",
        help="permit interactive recording even when the dashboard is not "
        "bound to loopback (the browser opens on the SERVER's desktop)",
    )

    p_res = sub.add_parser(
        "resources",
        help="Show spare CPU, memory and disk, and what running jobs use")
    p_res.add_argument("--db", default="./webarc-state/webarc.db",
                       help="dashboard state file listing the jobs")
    p_res.add_argument("--warc-root", default="./warcs",
                       help="disk to report when no default storage is set")
    p_res.add_argument("--json", action="store_true",
                       help="print the reading as JSON")

    p_md = sub.add_parser(
        "metadata", help="Export a capture's descriptive metadata as a sheet")
    md_sub = p_md.add_subparsers(dest="metadata_command", required=True)
    p_md_export = md_sub.add_parser(
        "export", help="Write metadata.csv (one row per seed) from a job folder")
    p_md_export.add_argument("job_dir", help="A job's folder (holds metadata.json)")
    p_md_export.add_argument("--output", "-o",
                             help="Where to write the sheet (default: metadata.csv in the folder; - for stdout)")

    p_rec = sub.add_parser(
        "record",
        help="Interactive recording: you browse, SWM archives what loads",
    )
    p_rec.add_argument("url", help="Starting URL (opened in a visible browser)")
    p_rec.add_argument("--name", help="Session name (default: derived from host)")
    p_rec.add_argument("--output", default="./warcs",
                       help="WARC root; session writes to <output>/<name>/")
    p_rec.add_argument("--browser", choices=["headed", "native"],
                       default="headed",
                       help="headed = Playwright-managed Chrome (recommended); "
                       "native = attach to system Chrome via CDP (advanced)")
    p_rec.add_argument("--operator", default="webarc",
                       help="Operator recorded in the WARC metadata")
    p_rec.add_argument("-v", "--verbose", action="store_true")

    p_ins = sub.add_parser(
        "inspect", help="List response records in captured WARCs")
    p_ins.add_argument("warc_dir", help="Directory containing .warc.gz files")
    p_ins.add_argument("--grep", help="Only show URLs containing this text")
    p_ins.add_argument("--hosts", action="store_true",
                       help="Summarise capture counts per host instead")

    p_ext = sub.add_parser(
        "extract", help="Copy WARCs into one file, dropping large bodies "
        "(for sharing/diagnosis)")
    p_ext.add_argument("warc_dir", help="Directory containing .warc.gz files")
    p_ext.add_argument("output", help="Output .warc.gz path")
    p_ext.add_argument("--max-mb", type=float, default=1.0,
                       help="Drop records with bodies larger than this (MB)")

    p_rep = sub.add_parser("replay", help="Replay captured WARCs with ReplayWeb.page")
    p_rep.add_argument("warc_dir", help="Directory containing .warc.gz files")
    p_rep.add_argument("--url", help="Seed URL to deep-link (optional)")
    p_rep.add_argument(
        "--collection",
        help="Replay collection name (default: derived from folder name)",
    )
    p_rep.add_argument("--replay-root", default="./replay")
    p_rep.add_argument("--host", default="127.0.0.1")
    p_rep.add_argument("--port", type=int, default=8091)
    p_rep.add_argument(
        "--self-host",
        action="store_true",
        help=(
            "reference vendored ui.js/sw.js in <replay-root>/vendor/ "
            "instead of the jsDelivr CDN (for offline machines)"
        ),
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "extract":
        from pathlib import Path as _P

        from warcio.archiveiterator import ArchiveIterator
        from warcio.warcwriter import WARCWriter

        warc_dir = _P(args.warc_dir)
        warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))
        if not warcs:
            print(f"No WARC files found in {warc_dir}", file=sys.stderr)
            return 1
        limit = int(args.max_mb * 1024 * 1024)
        kept = dropped = 0
        with open(args.output, "wb") as out:
            writer = WARCWriter(out, gzip=True)
            for path in warcs:
                with open(path, "rb") as fh:
                    for record in ArchiveIterator(fh):
                        size = int(record.rec_headers.get_header(
                            "Content-Length") or 0)
                        if size > limit:
                            dropped += 1
                            continue
                        writer.write_record(record)
                        kept += 1
        out_size = _P(args.output).stat().st_size
        print(f"Wrote {args.output}: {kept} record(s) kept, "
              f"{dropped} large record(s) dropped, "
              f"{out_size / 1024 / 1024:.1f} MB")
        return 0

    if args.command == "inspect":
        from collections import Counter
        from pathlib import Path as _P
        from urllib.parse import urlsplit as _us

        from warcio.archiveiterator import ArchiveIterator

        warc_dir = _P(args.warc_dir)
        warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))
        if not warcs:
            print(f"No WARC files found in {warc_dir}", file=sys.stderr)
            return 1
        from .detect import WAF_ACTION_HEADER

        hosts: Counter = Counter()
        empty = 0
        shown = total = 0
        waf_challenged: list[str] = []
        empty_api: list[str] = []
        for path in warcs:
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type not in ("response", "revisit"):
                        continue
                    uri = record.rec_headers.get_header("WARC-Target-URI") or ""
                    total += 1
                    hosts[_us(uri).hostname or "?"] += 1
                    http = record.http_headers
                    length = http.get_header("Content-Length") if http else None
                    if length == "0" and record.rec_type == "response":
                        empty += 1
                        ctype = (http.get_header("Content-Type") or "").lower()
                        if ctype.startswith("application/json"):
                            empty_api.append(uri)
                    if http and http.get_header(WAF_ACTION_HEADER):
                        waf_challenged.append(uri)
                    if not args.hosts:
                        if args.grep and args.grep.lower() not in uri.lower():
                            continue
                        status = (record.http_headers.get_statuscode()
                                  if record.http_headers else "?")
                        shown += 1
                        print(f"{record.rec_type:8} {status:>4} "
                              f"{length or '?':>10}  {uri}")
        if args.hosts or not args.grep:
            print(f"\n{total} captured exchange(s), "
                  f"{empty} with empty bodies, by host:")
            for host, n in hosts.most_common(20):
                print(f"  {n:5d}  {host}")
        elif args.grep:
            print(f"\n{shown} of {total} exchange(s) matched {args.grep!r}")

        def _preview(urls: list[str], limit: int = 5) -> None:
            for u in urls[:limit]:
                print(f"    {u}")
            if len(urls) > limit:
                print(f"    … and {len(urls) - limit} more")

        if waf_challenged:
            print(f"\nWARNING: {len(waf_challenged)} response(s) are WAF "
                  "bot-challenge verdicts (x-amzn-waf-action), not real "
                  "content. The crawl was challenged; those URLs will show "
                  "as missing/broken content on replay:")
            _preview(waf_challenged)
        if empty_api:
            print(f"\nWARNING: {len(empty_api)} JSON/API response(s) were "
                  "archived with EMPTY bodies. Pages whose records load "
                  "dynamically from these URLs will replay with missing "
                  "content ('could not load') even though the HTML looks "
                  "fine:")
            _preview(empty_api)
        return 0

    if args.command == "record":
        import re
        from pathlib import Path as _P
        from urllib.parse import urlsplit

        from .capture import WarcSession
        from .config import BrowserConfig, WarcConfig
        from .recorder import RecordingSession

        host = urlsplit(args.url).hostname or "session"
        name = args.name or f"rec-{host}"
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "rec-session"
        out_dir = _P(args.output) / name

        print(f"\n{APP_NAME} — interactive recording")
        print(f"  Session : {name}")
        print(f"  Output  : {out_dir}")
        print(f"  Browser : {args.browser}")
        print("\nA browser window will open at the starting URL. Browse normally —")
        print("every page and resource the browser loads is written to WARC.")
        print("NOTE: this includes cookies, logins, form submissions and any")
        print("private content you access during the session.")
        print("\nUse the 'SWM Recording' widget (bottom-right of every page) to")
        print("pause capture, resume, or capture the current page. Close the")
        print("browser window (or press Ctrl+C here) to finish.\n")

        warc = WarcSession(
            out_dir, name, args.url, 1, args.operator, WarcConfig(),
            info_extra={
                "robots": "none",
                "description": f"Interactive session recording starting "
                               f"at {args.url}",
            })
        last = {"visited": -1}

        def on_progress(state, visited, bytes_written, current_url):
            if visited != last["visited"]:
                last["visited"] = visited
                print(f"  [{state}] {visited} page(s), "
                      f"{bytes_written / 1024:.0f} KB — {current_url}")

        session = RecordingSession(
            args.url, BrowserConfig(mode=args.browser), warc,
            on_progress=on_progress)
        try:
            stats = session.run()
        except KeyboardInterrupt:
            session.apply("stop")
            stats = {"visited": session.visited,
                     "bytes": warc.total_bytes}
            warc.close()
            print("\nInterrupted — finalising WARC.")
        print(f"\nRecording finished: {stats['visited']} page(s), "
              f"{stats['bytes'] / 1024:.0f} KB in {out_dir}")
        print(f"Replay it with:\n  python -m webarc.cli replay {out_dir}")
        return 0

    if args.command == "replay":
        import webbrowser
        from pathlib import Path as _P

        from .replay import ReplayServer, build_replay_site, collection_name

        from .facebook_render import build_site, is_facebook_capture

        warc_dir = _P(args.warc_dir)
        warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))

        # A Facebook capture is read through its rendered pages: the feed
        # cannot be re-driven in a replay browser, and the capture may have
        # been run without a WARC at all.
        facebook_site = None
        if is_facebook_capture(warc_dir):
            try:
                facebook_site = build_site(warc_dir)
                print(f"Facebook capture: built reader pages at "
                      f"{facebook_site}")
            except Exception as exc:
                print(f"Could not build Facebook reader pages: {exc}",
                      file=sys.stderr)

        if not warcs:
            if facebook_site is None:
                print(f"No WARC files found in {warc_dir}", file=sys.stderr)
                return 1
            server = ReplayServer(facebook_site.parent, port=args.port,
                                  host=args.host)
            url = (f"http://{args.host}:{args.port}/"
                   f"{facebook_site.name}/index.html")
            print(f"\nNo WARC in this capture; serving its pages instead.")
            print(f"  Open: {url}")
            print("\nPress Ctrl+C to stop the server.")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.")
            return 0
        coll = args.collection or collection_name(warc_dir.resolve().name)
        replay_root = _P(args.replay_root)
        seed, reason = _resolve_cli_replay_url(warcs, args.url)
        if reason == "host-alias":
            print("Requested replay URL was not captured exactly:")
            print(f"  {args.url}")
            print("Using the captured www/non-www equivalent:")
            print(f"  {seed}")
        elif reason == "fallback":
            print("Requested replay URL was not found in the archive:")
            print(f"  {args.url}")
            print("Using the first captured HTML page instead:")
            print(f"  {seed}")
        elif seed and reason == "auto":
            print(f"Start page (auto-detected, override with --url): {seed}")
        elif reason == "unverified" and args.url:
            print("Warning: requested replay URL was not found while scanning "
                  "the archive; ReplayWeb.page may report it as unavailable.",
                  file=sys.stderr)

        alias_warc, alias_count = _build_cli_www_alias_warc(warcs)
        replay_warcs = warcs + ([alias_warc] if alias_warc else [])
        if alias_count:
            print(f"Added {alias_count} replay-only www/non-www URL alias(es).")
        try:
            build_replay_site(
                replay_warcs,
                replay_root / coll,
                seed_url=seed,
                self_host=args.self_host,
            )
        finally:
            if alias_warc:
                alias_warc.unlink(missing_ok=True)

        server = ReplayServer(replay_root, port=args.port, host=args.host)
        url = server.replay_url(coll)
        print(f"\nReplaying {len(warcs)} WARC(s) as '{coll}' (ReplayWeb.page)")
        print(f"  Open: {url}")
        if facebook_site is not None:
            print("\nThis is a Facebook capture. Replay shows the Page as it "
                  "first loaded; its captured posts, media and comments are "
                  "in the reader pages:")
            print(f"  file:///{facebook_site.as_posix()}/index.html")
        print("\nReplay runs in your browser — no pywb, any Python version.")
        print("Press Ctrl+C to stop the server.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    if args.command == "resources":
        return _cmd_resources(args)

    if args.command == "metadata":
        return _cmd_metadata(args)

    if args.command == "serve":
        try:
            import uvicorn

            from .server import create_app
        except ImportError:
            print(
                "The SWM dashboard needs extra packages that aren't installed.\n"
                "Install them with:\n"
                "    pip install -r requirements-dashboard.txt\n"
                "Or keep using the command line: "
                "python -m webarc.cli crawl config.yaml",
                file=sys.stderr,
            )
            return 1
        app = create_app(args.db, args.warc_root, simulate=args.simulate,
                         bind_host=args.host,
                         allow_remote_recording=args.allow_remote_recording)
        mode = "SIMULATE (no browser)" if args.simulate else "live"
        print(f"{APP_NAME} dashboard → http://{args.host}:{args.port} [{mode}]")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    cfg = load_config(args.config)

    if args.command == "crawl" and not args.no_resource_check:
        if not _resource_gate(cfg.output_dir, args.db, assume_yes=args.yes,
                              wait=args.wait):
            return 2

    if args.command == "validate":
        for i, seed in enumerate(cfg.seeds, 1):
            print(f"seed {i}: {seed.url}")
            print(f"  browser : {seed.browser}")
            print(f"  scope   : {seed.scope}")
            print(f"  behavior: {seed.behavior}")
            print(f"  warc    : {seed.warc}")
        return 0

    run_crawl(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
