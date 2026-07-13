"""CLI for Simple Webcrawl Manager (SWM)."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .crawler import run_crawl


APP_NAME = "Simple Webcrawl Manager (SWM)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swm",
        description=f"{APP_NAME} — browser-based WARC crawler",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Run a crawl from a YAML config")
    p_crawl.add_argument("config", help="Path to config.yaml")
    p_crawl.add_argument("-v", "--verbose", action="store_true")

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

        warc_dir = _P(args.warc_dir)
        warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))
        if not warcs:
            print(f"No WARC files found in {warc_dir}", file=sys.stderr)
            return 1
        coll = args.collection or collection_name(warc_dir.resolve().name)
        replay_root = _P(args.replay_root)
        build_replay_site(
            warcs,
            replay_root / coll,
            seed_url=args.url,
            self_host=args.self_host,
        )
        server = ReplayServer(replay_root, port=args.port, host=args.host)
        url = server.replay_url(coll)
        print(f"\nReplaying {len(warcs)} WARC(s) as '{coll}' (ReplayWeb.page)")
        print(f"  Open: {url}")
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
        app = create_app(args.db, args.warc_root, simulate=args.simulate)
        mode = "SIMULATE (no browser)" if args.simulate else "live"
        print(f"{APP_NAME} dashboard → http://{args.host}:{args.port} [{mode}]")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    cfg = load_config(args.config)

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
