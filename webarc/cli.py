"""CLI: python -m webarc.cli crawl config.yaml [--verbose]"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .crawler import run_crawl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webarc",
                                     description="Browser-based WARC crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Run a crawl from a YAML config")
    p_crawl.add_argument("config", help="Path to config.yaml")
    p_crawl.add_argument("-v", "--verbose", action="store_true")

    p_val = sub.add_parser("validate", help="Parse and print the resolved config")
    p_val.add_argument("config")

    p_srv = sub.add_parser("serve", help="Run the dashboard control server")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8080)
    p_srv.add_argument("--db", default="./webarc-state/webarc.db")
    p_srv.add_argument("--warc-root", default="./warcs")
    p_srv.add_argument("--simulate", action="store_true",
                       help="run browserless fake crawls (demo/test the UI)")

    p_rep = sub.add_parser("replay", help="Replay captured WARCs with pywb")
    p_rep.add_argument("warc_dir", help="Directory containing .warc.gz files")
    p_rep.add_argument("--url", help="Seed URL to deep-link (optional)")
    p_rep.add_argument("--collection", help="pywb collection name "
                       "(default: derived from folder name)")
    p_rep.add_argument("--replay-root", default="./replay")
    p_rep.add_argument("--host", default="127.0.0.1")
    p_rep.add_argument("--port", type=int, default=8091)
    p_rep.add_argument("--self-host", action="store_true",
                       help="reference vendored ui.js/sw.js in <replay-root>/vendor/ "
                       "instead of the jsDelivr CDN (for offline machines)")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "replay":
        import webbrowser
        from pathlib import Path as _P
        from .replay import (ReplayServer, build_replay_site, collection_name)
        warc_dir = _P(args.warc_dir)
        warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))
        if not warcs:
            print(f"No WARC files found in {warc_dir}", file=sys.stderr)
            return 1
        coll = args.collection or collection_name(warc_dir.resolve().name)
        replay_root = _P(args.replay_root)
        build_replay_site(warcs, replay_root / coll, seed_url=args.url,
                          self_host=args.self_host)
        server = ReplayServer(replay_root, port=args.port, host=args.host)
        url = server.replay_url(coll)
        print(f"\nReplaying {len(warcs)} WARC(s) as '{coll}'  (ReplayWeb.page)")
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
            print("The dashboard needs extra packages that aren't installed.\n"
                  "Install them with:\n"
                  "    pip install -r requirements-dashboard.txt\n"
                  "Or keep using the command line: "
                  "python -m webarc.cli crawl config.yaml",
                  file=sys.stderr)
            return 1
        app = create_app(args.db, args.warc_root, simulate=args.simulate)
        mode = "SIMULATE (no browser)" if args.simulate else "live"
        print(f"webarc dashboard → http://{args.host}:{args.port}  [{mode}]")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    cfg = load_config(args.config)

    if args.command == "validate":
        for i, s in enumerate(cfg.seeds, 1):
            print(f"seed {i}: {s.url}")
            print(f"  browser : {s.browser}")
            print(f"  scope   : {s.scope}")
            print(f"  behavior: {s.behavior}")
            print(f"  warc    : {s.warc}")
        return 0

    run_crawl(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
