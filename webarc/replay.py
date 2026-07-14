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
import io
import logging
import os
import re
import shutil
import socketserver
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Pinned ReplayWeb.page release (see https://replayweb.page/docs/embedding/).
RWP_VERSION = "2.4.6"
CDN = f"https://cdn.jsdelivr.net/npm/replaywebpage@{RWP_VERSION}"

_REPLAY_COMPAT_JS = r"""
(() => {
  // Some media viewers (including Fancybox 6) add the experimental
  // `credentialless` attribute to dynamically created video iframes. Chromium
  // loads those frames in a separate ephemeral network context, outside the
  // ReplayWeb.page service worker that serves archived responses. Suppress the
  // attribute only inside this local replay site so the archived iframe remains
  // under replay control.
  const PATCH_FLAG = "__swmNoCredentialless";

  function wrapMethod(proto, name, wrapperFactory) {
    if (!proto || typeof proto[name] !== "function") return;
    const current = proto[name];
    if (current && current[PATCH_FLAG]) return;
    const wrapped = wrapperFactory(current);
    Object.defineProperty(wrapped, PATCH_FLAG, { value: true });
    proto[name] = wrapped;
  }

  function patchRealm(win) {
    try {
      const ElementProto = win.Element && win.Element.prototype;
      const IFrame = win.HTMLIFrameElement;
      if (!ElementProto || !IFrame) return;

      wrapMethod(ElementProto, "setAttribute", (original) => function(name, value) {
        if (this instanceof IFrame &&
            String(name).toLowerCase() === "credentialless") {
          return;
        }
        return original.call(this, name, value);
      });

      wrapMethod(ElementProto, "setAttributeNS", (original) =>
        function(namespace, name, value) {
          if (this instanceof IFrame &&
              String(name).toLowerCase() === "credentialless") {
            return;
          }
          return original.call(this, namespace, name, value);
        });

      wrapMethod(ElementProto, "toggleAttribute", (original) =>
        function(name, force) {
          if (this instanceof IFrame &&
              String(name).toLowerCase() === "credentialless") {
            try { this.removeAttribute("credentialless"); } catch (_) {}
            return false;
          }
          return original.call(this, name, force);
        });

      for (const iframe of win.document.querySelectorAll("iframe[credentialless]")) {
        iframe.removeAttribute("credentialless");
      }
    } catch (_) {
      // A frame may be between documents while ReplayWeb.page is navigating it.
    }
  }

  function collectFrames(root, frames) {
    try {
      for (const iframe of root.querySelectorAll("iframe")) frames.push(iframe);
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) collectFrames(element.shadowRoot, frames);
      }
    } catch (_) {}
  }

  function scanWindow(win, seen) {
    if (!win || seen.has(win)) return;
    seen.add(win);
    patchRealm(win);

    let frames = [];
    try { collectFrames(win.document, frames); } catch (_) { return; }
    for (const frame of frames) {
      try {
        frame.removeAttribute("credentialless");
        scanWindow(frame.contentWindow, seen);
      } catch (_) {}
    }
  }

  const scan = () => scanWindow(window, new WeakSet());
  scan();
  window.addEventListener("load", scan);
  document.addEventListener("readystatechange", scan);
  setInterval(scan, 250);
})();
"""

_INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>webarc replay — {coll}</title>
  <style>html, body {{ width: 100%; height: 100%; margin: 0; }}</style>
  <script src="{ui_src}"></script>
  <script>{compat_js}</script>
</head>
<body>
  <replay-web-page source="{archive}"{url_attr}
    embed="default" replayBase="./replay/" loading="eager"></replay-web-page>
</body>
</html>
"""


def collection_name(crawl_id: int | str) -> str:
    return f"crawl-{crawl_id}"


def detect_start_url(warc_paths: list[Path]) -> str | None:
    """Pick a sensible replay entry page: the first successful, non-empty
    HTML response in the archive. Without a start URL, ReplayWeb.page shows
    the raw resource list instead of opening a page."""
    from warcio.archiveiterator import ArchiveIterator
    for path in warc_paths:
        try:
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response":
                        continue
                    http = record.http_headers
                    if http is None or http.get_statuscode() != "200":
                        continue
                    ctype = (http.get_header("Content-Type") or "").lower()
                    if not ctype.startswith("text/html"):
                        continue
                    if (http.get_header("Content-Length") or "0") == "0":
                        continue
                    return record.rec_headers.get_header("WARC-Target-URI")
        except Exception as exc:
            log.debug("Start-URL scan failed for %s: %s", path, exc)
    return None


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
    # valid WARC that wabac.js indexes in-browser). The filename carries a
    # content digest: ReplayWeb.page caches loaded archives by source URL, so
    # a stable name could serve a stale index after new captures are added —
    # a changed archive must get a changed URL.
    import hashlib
    digest = hashlib.sha1()
    tmp = site_dir / "archive.tmp"
    with open(tmp, "wb") as out:
        for p in warc_paths:
            with open(Path(p).resolve(), "rb") as f:
                while chunk := f.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
    archive_name = f"archive-{digest.hexdigest()[:12]}.warc.gz"
    archive = site_dir / archive_name
    for old in site_dir.glob("archive-*.warc.gz"):
        if old.name != archive_name:
            old.unlink()
    tmp.replace(archive)

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
        _INDEX_HTML.format(coll=site_dir.name, ui_src=ui_src,
                           url_attr=url_attr, archive=archive_name,
                           compat_js=_REPLAY_COMPAT_JS),
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

    # Requests land here when the replay service worker finds no match in
    # the archive and lets them fall through. Make that self-explanatory
    # instead of serving Python's default error page.
    error_message_format = """<!DOCTYPE html>
<html><head><title>Not in this archive</title>
<style>body{font-family:system-ui,sans-serif;max-width:40em;margin:15vh auto;
color:#333}h1{font-size:1.2em}code{background:#f0f0f0;padding:1px 4px}</style>
</head><body>
<h1>Not captured in this archive (HTTP %(code)d)</h1>
<p>The replayed page requested a resource that is not in the archive.
Only resources that were actually loaded during the crawl or recording
session are captured.</p>
<p>Common causes: this link or player was not opened while recording;
or the page generates its URLs dynamically (timestamps, tokens, signed
video URLs), so the URL requested now differs from the one captured.</p>
<p><b>Requested:</b> <code>%(explain)s</code></p>
</body></html>"""

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            from urllib.parse import unquote
            explain = unquote(self.path)
        super().send_error(code, message, explain)

    def log_message(self, *args):  # keep the console quiet
        pass

    def end_headers(self):
        # Service Worker allowed scope + no-cache so rebuilt archives are seen
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    # HTTP Range support. wabac.js indexes the archive by streaming it, then
    # loads individual records on demand with Range requests; a server that
    # ignores Range (like the stdlib default) makes every record load fail
    # and each page replays as "Archived Page Not Found".
    _RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = self._RANGE_RE.match(rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()  # malformed/multi-range: serve full
        size = os.path.getsize(path)
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:                            # suffix form: bytes=-N
            start = max(0, size - int(m.group(2)))
            end = size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        return io.BytesIO(data)


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
