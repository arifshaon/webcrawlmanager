"""Local replay via Webrecorder ReplayWeb.page (wabac.js).

Replay runs entirely in the browser through a service worker — there is no
Python replay server, so this works on any Python version (3.13 / 3.14 included)
with no pywb and no extra dependencies. webarc only serves static files:

  <replay-root>/<coll>/index.html          embeds <replay-web-page>
  <replay-root>/<coll>/archive.warc.gz     the crawl's WARC(s), concatenated
  <replay-root>/<coll>/replay/sw.js         service-worker shim

The ReplayWeb.page UI (ui.js) and backend (sw.js) load from the jsDelivr CDN by
default (pinned version). For offline/air-gapped machines, drop ui.js and sw.js
into <replay-root>/vendor/ and pass self_host=True (or --self-host) to reference
those instead of the CDN.

Serving index.html, the WARC, and sw.js all from the same local origin means no
CORS configuration is needed, and 127.0.0.1 counts as a secure context so the
service worker is allowed to register.
"""

from __future__ import annotations

import functools
import http.server
import logging
import shutil
import socketserver
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Pinned ReplayWeb.page release (see https://replayweb.page/docs/embedding/).
RWP_VERSION = "2.4.6"
CDN = f"https://cdn.jsdelivr.net/npm/replaywebpage@{RWP_VERSION}"

_INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>webarc replay \u2014 {coll}</title>
  <style>html, body {{ width: 100%; height: 100%; margin: 0; }}</style>
  <script src="{ui_src}"></script>
</head>
<body>
  <replay-web-page source="archive.warc.gz"{url_attr}
    embed="default" replayBase="./replay/"></replay-web-page>
</body>
</html>
"""


def collection_name(crawl_id: int | str) -> str:
    return f"crawl-{crawl_id}"


def build_replay_site(warc_paths: list[Path], site_dir: Path,
                      seed_url: str | None = None,
                      self_host: bool = False) -> Path:
    """Assemble a self-contained ReplayWeb.page site for a set of WARCs.

    Returns the site directory. Combining the WARCs is idempotent-friendly: the
    archive is rebuilt from the current file list each call.
    """
    site_dir = Path(site_dir).resolve()
    (site_dir / "replay").mkdir(parents=True, exist_ok=True)

    # Concatenate WARCs into one archive (gzip members concatenate into a single
    # valid WARC that wabac.js indexes in-browser).
    archive = site_dir / "archive.warc.gz"
    with open(archive, "wb") as out:
        for p in warc_paths:
            with open(Path(p).resolve(), "rb") as f:
                shutil.copyfileobj(f, out)

    if self_host:
        ui_src = "../vendor/ui.js"
        sw_import = "../../vendor/sw.js"   # relative to <coll>/replay/sw.js
    else:
        ui_src = f"{CDN}/ui.js"
        sw_import = f"{CDN}/sw.js"

    (site_dir / "replay" / "sw.js").write_text(
        f'importScripts("{sw_import}");\n', encoding="utf-8")

    url_attr = f'\n    url="{seed_url}"' if seed_url else ""
    (site_dir / "index.html").write_text(
        _INDEX_HTML.format(coll=site_dir.name, ui_src=ui_src, url_attr=url_attr),
        encoding="utf-8")
    log.info("Built replay site for %d WARC(s) at %s",
             len(warc_paths), site_dir)
    return site_dir


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".warc": "application/octet-stream",
        ".gz": "application/octet-stream",
        ".wacz": "application/octet-stream",
    }

    def log_message(self, *args):  # keep the console quiet
        pass

    def end_headers(self):
        # Service Worker allowed scope + no-cache so rebuilt archives are seen
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class ReplayServer:
    """A plain static file server (stdlib) rooted at the replay root.

    No Python-version constraints, no framework. Serves every crawl's replay
    site under <replay_root>/<coll>/.
    """

    def __init__(self, replay_root: str | Path, port: int = 8091,
                 host: str = "127.0.0.1"):
        self.replay_root = Path(replay_root).resolve()
        self.replay_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.host = host
        self._httpd: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    def start_background(self) -> None:
        if self._httpd is not None:
            return
        handler = functools.partial(_QuietHandler, directory=str(self.replay_root))
        socketserver.TCPServer.allow_reuse_address = True
        self._httpd = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("Replay server on http://%s:%d (root=%s)",
                 self.host, self.port, self.replay_root)

    def serve_forever(self) -> None:
        handler = functools.partial(_QuietHandler, directory=str(self.replay_root))
        socketserver.TCPServer.allow_reuse_address = True
        self._httpd = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        self._httpd.serve_forever()

    def is_running(self) -> bool:
        return self._httpd is not None

    def replay_url(self, coll: str) -> str:
        return f"http://{self.host}:{self.port}/{coll}/index.html"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
