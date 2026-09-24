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


def _serve_until_interrupted(server) -> None:
    """Keep a started replay server up until Ctrl-C."""
    import time as _t
    try:
        while True:
            _t.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.stop()


def _open_store(db_path: str):
    from .store import Store
    return Store(db_path)


def _register_job_in_collection(args, name: str, kind: str, config: dict,
                                *, seeds_total: int):
    """Create the job's row in the collection and its directory under it.

    Returns (store, crawl_id), the collection row and the job directory.
    Raises ValueError when the collection does not exist and was not to be
    created, so a typo never files a job in a collection of its own.
    """
    from pathlib import Path as _P

    from . import collections as colls
    from .store import RUNNING

    store = _open_store(args.db)
    collection = store.find_collection(args.collection)
    if collection is None:
        if not getattr(args, "create_collection", False):
            raise ValueError(
                f"No collection called '{args.collection}'. Create it first with "
                f"'swm collection create', or add --create-collection.")
        collection = _create_collection_row(
            store, args.collection, "", [], _P(getattr(args, "warc_root", None)
                                               or "./warcs"))
    crawl_id = store.create_crawl(
        name=name, config=dict(config), output_dir="", seeds_total=seeds_total,
        kind=kind, collection_id=collection["id"])
    job_dir = colls.job_home(collection["root_dir"], crawl_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    config = dict(config)
    config["output_dir"] = str(job_dir)
    store.finalize_config(crawl_id, config, str(job_dir))
    store.set_status(crawl_id, RUNNING)
    _refresh_collection_document(store, collection)
    return (store, crawl_id), collection, job_dir


def _settle_registered_job(registered, outcome: str) -> None:
    if not registered:
        return
    from .store import COMPLETED, FAILED, STOPPED
    store, crawl_id = registered
    status = {"completed": COMPLETED, "failed": FAILED}.get(outcome, STOPPED)
    try:
        store.set_status(crawl_id, status)
        row = store.get_crawl(crawl_id)
        _refresh_collection_document(store, store.get_collection(
            (row or {}).get("collection_id")))
    except Exception as exc:                        # pragma: no cover
        logging.getLogger(__name__).warning("Could not record the job's end: %s", exc)


def _refresh_collection_document(store, collection: dict) -> None:
    from . import collections as colls
    colls.refresh_document(store, collection)


def _create_collection_row(store, name: str, description: str,
                           metadata: list[dict], base: "Path",
                           storage_dir: str | None = None,
                           policy: dict | None = None) -> dict:
    """A collection's row, directory and collection.json."""
    from pathlib import Path as _P

    from . import collections as colls

    name = colls.validate_name(name)
    description = colls.validate_description(description)
    slug = colls.slugify(name)
    if store.find_collection(slug):
        raise ValueError(f"A collection with the identifier '{slug}' already exists.")
    root = colls.collection_root(_P(storage_dir) if storage_dir else _P(base), slug)
    collection_id = store.create_collection(slug, name, description, str(root), metadata,
                                            policy or dict(colls.DEFAULT_POLICY))
    row = store.get_collection(collection_id)
    root.mkdir(parents=True, exist_ok=True)
    colls.write_document(root, colls.document(row, []))
    return row


def _cmd_collection(args) -> int:
    import json as _json
    from pathlib import Path as _P

    from . import collections as colls

    store = _open_store(args.db)
    command = args.collection_command

    if command == "create":
        try:
            metadata = colls.load_metadata_argument(args.metadata_json, args.metadata_file)
            row = _create_collection_row(
                store, args.name, args.description, metadata, _P(args.warc_root),
                args.storage_dir, policy={"dedup_across_jobs": not args.no_cross_job_dedup})
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Created collection '{row['name']}' ({row['slug']}, id {row['id']})")
        print(f"  Directory : {row['root_dir']}")
        print(f"  Metadata  : {len(row['metadata'])} field(s)")
        print("  Dedup     : " + ("each payload stored once across the collection's jobs"
                                 if colls.policy_of(row)["dedup_across_jobs"]
                                 else "every payload stored in full in each job"))
        print(f"Run a job against it with: swm crawl config.yaml --collection {row['slug']}")
        return 0

    if command == "list":
        rows = store.list_collections()
        counts = store.collection_counts()
        if args.json:
            for row in rows:
                row["counts"] = counts.get(row["id"], {"jobs": 0, "by_status": {}})
            print(_json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("No collections yet. Make one with: swm collection create NAME")
            return 0
        width = max(len(r["name"]) for r in rows)
        for row in rows:
            entry = counts.get(row["id"], {"jobs": 0, "by_status": {}})
            status = ", ".join(f"{n} {k}" for k, n in sorted(entry["by_status"].items()))
            print(f"{row['id']:>4}  {row['name']:<{width}}  {entry['jobs']:>3} job(s)"
                  f"{'  (' + status + ')' if status else ''}  {row['root_dir']}")
        return 0

    row = store.find_collection(args.collection)
    if not row:
        print(f"No collection called '{args.collection}'.", file=sys.stderr)
        return 2
    jobs = store.crawls_in_collection(row["id"])

    if command == "show":
        if args.json:
            print(_json.dumps({**row, "jobs": jobs}, ensure_ascii=False, indent=2))
            return 0
        print(f"{row['name']}  (id {row['id']}, identifier {row['slug']})")
        if row.get("description"):
            print(f"  {row['description']}")
        print(f"  Directory : {row['root_dir']}")
        print(f"  Created   : {row['created_at']}")
        print("  Dedup     : " + ("each payload stored once across the collection's jobs"
                                 if colls.policy_of(row)["dedup_across_jobs"]
                                 else "every payload stored in full in each job"))
        index = colls.read_index(row)
        if index is not None:
            try:
                counts = index.counts()
                orphans = index.orphan_urls()
            finally:
                index.close()
            print(f"  Index     : {counts['originals']} original(s), {counts['revisits']} "
                  f"revisit(s), {counts['bytes_saved'] / (1024 * 1024):.1f} MB not stored twice")
            if orphans:
                print(f"  Missing   : {len(orphans)} page(s) whose original was deleted; "
                      "re-crawl them to restore")
        if row["metadata"]:
            print("  Metadata  :")
            for field in row["metadata"]:
                print(f"    {field['name']}: {field['value']}")
        print(f"  Jobs      : {len(jobs)}")
        for job in jobs:
            print(f"    #{job['id']:<4} {job['status']:<10} {job.get('kind', 'crawl'):<10} "
                  f"{job['name']}  {job['output_dir']}")
        return 0

    if command == "delete":
        root = _P(row["root_dir"])
        size = 0
        if root.exists():
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    pass
        impact = colls.collection_impact(row, jobs, bytes_on_disk=size)
        print(colls.describe_impact(impact))
        if args.purge:
            print(f"With --purge, the directory {root} and everything under it is deleted.")
        else:
            print("The files stay on disk; only the records are removed. Add --purge to "
                  "delete the files too.")
        print("Nothing is changed until you confirm.")
        if not args.yes:
            if not sys.stdin.isatty():
                print("Not deleting: no terminal to confirm on. Add --yes to delete "
                      "having read the above.", file=sys.stderr)
                return 2
            answer = input("Delete this collection? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Nothing was changed.")
                return 1
        removed = store.delete_collection(row["id"])
        if args.purge:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        print(f"Deleted collection '{row['name']}' and {len(removed)} job record(s)"
              f"{'; files removed from disk' if args.purge else '; files kept on disk'}.")
        return 0

    return 2


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


class _DroppedConnectionFilter(logging.Filter):
    """Keep a browser hanging up out of the server log.

    On Windows, asyncio's proactor loop logs an ERROR with a traceback
    every time a browser drops a connection the dashboard still held --
    when a tab is closed, or a new one opens for replay. Nothing failed:
    the request was answered and the socket is gone. Those two messages
    are dropped; every other asyncio message still shows.
    """

    NOISE = ("_call_connection_lost", "socket.send() raised exception")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(sign in message for sign in self.NOISE)


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
    p_crawl.add_argument("--collection",
                         help="run this job as part of a collection (by name, "
                         "identifier or id): its files go under the "
                         "collection's directory and it is listed with the "
                         "collection's other jobs")
    p_crawl.add_argument("--create-collection", action="store_true",
                         help="make the collection named by --collection if "
                         "it does not exist yet")

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

    p_coll = sub.add_parser(
        "collection", help="Create, list, describe and delete collections")
    coll_sub = p_coll.add_subparsers(dest="collection_command", required=True)
    for name, text in (("create", "Make a collection with a directory of its own"),
                       ("list", "List the collections and how many jobs each holds"),
                       ("show", "Describe one collection and list its jobs"),
                       ("delete", "Delete a collection and its jobs, after saying what that means")):
        sp = coll_sub.add_parser(name, help=text)
        sp.add_argument("--db", default="./webarc-state/webarc.db",
                        help="dashboard state file the collections live in")
        sp.add_argument("--warc-root", default="./warcs",
                        help="default storage root a new collection is placed under")
        if name == "create":
            sp.add_argument("name", help="the collection's name")
            sp.add_argument("--description", default="",
                            help="what the collection is for")
            sp.add_argument("--storage-dir",
                            help="create the collection's directory under this "
                            "folder instead of the default storage root")
            sp.add_argument("--metadata-json",
                            help="the collection's descriptive metadata as a "
                            "JSON array of {name, value} fields")
            sp.add_argument("--metadata-file",
                            help="the same, read from a .json file or a "
                            "metadata sheet (.csv) as the dashboard exports one")
            sp.add_argument("--no-cross-job-dedup", action="store_true",
                            help="store every payload in full in each job, rather "
                            "than once across the collection's jobs")
        elif name == "list":
            sp.add_argument("--json", action="store_true",
                            help="print the collections as JSON")
        else:
            sp.add_argument("collection", help="the collection's name, identifier or id")
            if name == "show":
                sp.add_argument("--json", action="store_true",
                                help="print the collection as JSON")
            else:
                sp.add_argument("--purge", action="store_true",
                                help="also delete the collection's files from disk")
                sp.add_argument("--yes", "-y", action="store_true",
                                help="delete without asking, having been told")

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
    p_rec.add_argument("--db", default="./webarc-state/webarc.db",
                       help="dashboard state file (used with --collection)")
    p_rec.add_argument("--collection",
                       help="record as part of a collection (by name, "
                       "identifier or id)")
    p_rec.add_argument("--create-collection", action="store_true",
                       help="make the collection named by --collection if "
                       "it does not exist yet")

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
        registered = None
        collection = None
        if args.collection:
            try:
                registered, collection, out_dir = _register_job_in_collection(
                    args, name, "recording",
                    {"recording": {"start_url": args.url, "operator": args.operator,
                                   "browser": {"mode": args.browser}},
                     "seeds": [{"url": args.url}]},
                    seeds_total=1)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

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

        from .collections import inherited_fields, open_index
        from .metadata import defaults_for, with_defaults
        warc = WarcSession(
            out_dir, name, args.url, 1, args.operator, WarcConfig(),
            info_extra={
                "robots": "none",
                "description": f"Interactive session recording starting "
                               f"at {args.url}",
            },
            metadata_fields=with_defaults(
                inherited_fields(collection),
                defaults_for("recording", name, args.operator, args.url))
            if collection else None,
            collection_index=open_index(collection),
            crawl_id=registered[1] if registered else None)
        last = {"visited": -1}

        def on_progress(state, visited, bytes_written, current_url):
            if visited != last["visited"]:
                last["visited"] = visited
                print(f"  [{state}] {visited} page(s), "
                      f"{bytes_written / 1024:.0f} KB — {current_url}")

        session = RecordingSession(
            args.url, BrowserConfig(mode=args.browser), warc,
            on_progress=on_progress)
        outcome = "completed"
        try:
            stats = session.run()
        except KeyboardInterrupt:
            session.apply("stop")
            stats = {"visited": session.visited,
                     "bytes": warc.total_bytes}
            warc.close()
            outcome = "stopped"
            print("\nInterrupted — finalising WARC.")
        except Exception:
            _settle_registered_job(registered, "failed")
            raise
        _settle_registered_job(registered, outcome)
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
            server.start_background()          # binds now; the port may step up
            url = server.replay_url(facebook_site.name)
            print(f"\nNo WARC in this capture; serving its pages instead.")
            print(f"  Open: {url}")
            print("\nPress Ctrl+C to stop the server.")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            _serve_until_interrupted(server)
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
        server.start_background()              # binds now; the port may step up
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
        _serve_until_interrupted(server)
        return 0

    if args.command == "resources":
        return _cmd_resources(args)

    if args.command == "metadata":
        return _cmd_metadata(args)

    if args.command == "collection":
        return _cmd_collection(args)

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
        logging.getLogger("asyncio").addFilter(_DroppedConnectionFilter())
        mode = "SIMULATE (no browser)" if args.simulate else "live"
        print(f"{APP_NAME} dashboard → http://{args.host}:{args.port} [{mode}]")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    cfg = load_config(args.config)

    registered = None
    if args.command == "crawl" and getattr(args, "collection", None):
        import yaml as _yaml
        from pathlib import Path as _P

        from .collections import brief, inherited_fields
        raw = _yaml.safe_load(_P(args.config).read_text(encoding="utf-8")) or {}
        try:
            registered, collection, job_dir = _register_job_in_collection(
                args, cfg.crawl_name, "crawl", raw, seeds_total=len(cfg.seeds))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cfg.output_dir = job_dir
        cfg.collection = brief(collection)
        cfg.inherited_metadata = inherited_fields(collection)
        cfg.job_id = registered[1]
        print(f"Collection: {collection['name']} ({collection['slug']})\n"
              f"Job #{registered[1]} writes to {job_dir}")

    if args.command == "crawl" and not args.no_resource_check:
        if not _resource_gate(cfg.output_dir, args.db, assume_yes=args.yes,
                              wait=args.wait):
            _settle_registered_job(registered, "stopped")
            return 2

    if args.command == "validate":
        for i, seed in enumerate(cfg.seeds, 1):
            print(f"seed {i}: {seed.url}")
            print(f"  browser : {seed.browser}")
            print(f"  scope   : {seed.scope}")
            print(f"  behavior: {seed.behavior}")
            print(f"  warc    : {seed.warc}")
        return 0

    try:
        run_crawl(cfg)
    except Exception:
        _settle_registered_job(registered, "failed")
        raise
    _settle_registered_job(registered, "completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
