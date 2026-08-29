"""Local replay via Webrecorder ReplayWeb.page (wabac.js).

Replay runs entirely in the browser through a service worker — there is no
Python replay server, so this works on any Python version (3.13 / 3.14 included)
with no pywb and no extra dependencies. webarc only serves static files:

  <replay-root>/<coll>/index.html          embeds <replay-web-page>
  <replay-root>/<coll>/archive.warc.gz     the crawl's WARC(s), concatenated
  <replay-root>/<coll>/replay/sw.js         service-worker shim

The ReplayWeb.page UI (ui.js) and backend (sw.js) are vendored into
<replay-root>/vendor/ (downloaded once from the jsDelivr CDN, pinned version)
and sw.js is patched for a wabac.js POST-lookup bug — see _SW_PATCH_OLD below.
Air-gapped machines can pre-place ui.js and sw.js in <replay-root>/vendor/
(self_host=True / --self-host makes their absence an error instead of a
CDN fallback). If vendoring is impossible the site falls back to referencing
the CDN directly, with degraded replay for sites that load content via POST.

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
RWP_VERSION = "2.5.0"
CDN = f"https://cdn.jsdelivr.net/npm/replaywebpage@{RWP_VERSION}"

# wabac.js (through at least 2.5.0) converts a POST body into URL query params
# to build the lookup key for archived POST responses, then decodeURI()s the
# result. A JSON value containing a newline or tab — every GraphQL query body,
# so all Figshare-style portals — embeds raw control characters in the index
# key; they do not survive URL normalisation on the lookup side, so every such
# POST replays as 404 and the site shows "we could not load the content" over
# data that IS in the archive. Patching the vendored worker to strip control
# characters after decoding keeps both sides of the lookup symmetric.
_SW_PATCH_OLD = 'try{a=decodeURI(a)}catch{a=""}'
_SW_PATCH_NEW = 'try{a=decodeURI(a).replace(/[\\r\\n\\t]/g,"")}catch{a=""}'


def _patch_sw_js(sw_path: Path) -> None:
    """Apply the POST-lookup fix to a vendored sw.js in place (idempotent)."""
    try:
        text = sw_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read %s to patch it: %s", sw_path, exc)
        return
    if _SW_PATCH_NEW in text:
        return  # already patched
    if _SW_PATCH_OLD not in text:
        log.warning("sw.js does not contain the expected POST-decode code — "
                    "a new ReplayWeb.page version may have changed it. "
                    "POST-heavy sites (GraphQL APIs) may replay as "
                    "'content not found' if the upstream bug is still there.")
        return
    sw_path.write_text(text.replace(_SW_PATCH_OLD, _SW_PATCH_NEW),
                       encoding="utf-8")
    log.info("Patched vendored sw.js for the wabac.js POST-body lookup bug "
             "(newlines in JSON POST bodies)")


def _ensure_vendor_assets(vendor_dir: Path, *, download: bool = True) -> bool:
    """Make <replay-root>/vendor/{ui.js,sw.js} available and patch sw.js.

    Returns True when the vendored assets are usable. Downloads the pinned
    release on first use; an air-gapped machine can pre-place the two files.
    """
    import urllib.request

    vendor_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ui.js", "sw.js"):
        target = vendor_dir / name
        if target.exists() and target.stat().st_size > 0:
            continue
        if not download:
            return False
        url = f"{CDN}/{name}"
        try:
            log.info("Downloading %s -> %s", url, target)
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        except Exception as exc:
            log.warning("Could not download %s: %s", url, exc)
            return False
    _patch_sw_js(vendor_dir / "sw.js")
    return True

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

  // Archived pages behind AWS WAF reference the WAF's challenge/token SDK
  // (challenge.js from *.awswaf.com). Its URL rotates, so during replay the
  // script is usually not in the archive and never loads — and an application
  // that waits on AwsWafIntegration.getToken() before fetching its data then
  // hangs or errors out ("we could not load the content") even though the
  // data responses ARE archived. Pre-seed an already-resolved integration
  // object in replayed realms so such applications proceed straight to their
  // (archived) API calls. Never touch fetch/XHR here: wrapping fetch inside
  // replay realms breaks wombat's URL rewriting.
  function isReplayedRealm(win) {
    // Archived documents replay under .../w/<id>/mp_/<original-url>. Only
    // those realms get the WAF stub — the ReplayWeb.page app frames must be
    // left alone.
    try { return win.location.href.indexOf("mp_/") !== -1; }
    catch (_) { return false; }
  }

  function neutralizeWafSdk(win) {
    try {
      if (!win || !isReplayedRealm(win)) return;
      if (!win.AwsWafIntegration) {
        win.AwsWafIntegration = {
          getToken: () => Promise.resolve("replay-stub"),
          hasToken: () => true,
          fetch: (...args) => win.fetch(...args),
          saveReferrer: () => {},
        };
      }
    } catch (_) {}
  }

  function patchRealm(win) {
    neutralizeWafSdk(win);
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

      // Setting the IDL property (iframe.credentialless = true) reflects to
      // the attribute without going through setAttribute — override the
      // property setter as well.
      try {
        const desc = Object.getOwnPropertyDescriptor(
          IFrame.prototype, "credentialless");
        if (!desc || !desc.set || !desc.set[PATCH_FLAG]) {
          const setter = function(_value) { /* suppressed during replay */ };
          Object.defineProperty(setter, PATCH_FLAG, { value: true });
          Object.defineProperty(IFrame.prototype, "credentialless", {
            configurable: true,
            get: (desc && desc.get) ? desc.get : function() { return false; },
            set: setter,
          });
        }
      } catch (_) {}

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


# Hosts whose records are kept in the archive but excluded from the REPLAY
# copy. AWS WAF's token service serves the challenge SDK (challenge.js) and
# its verify endpoints; captured faithfully they are part of the archival
# record, but replaying the SDK breaks the archived site: its freshly
# generated token calls can never match archived responses, and applications
# that gate their data fetches on the SDK then hang or error out. With the
# SDK absent, the compat script's pre-seeded AwsWafIntegration stub takes
# over and the application proceeds straight to its archived API calls.
_REPLAY_EXCLUDED_HOSTS = ("awswaf.com",)


def _replay_excluded(uri: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(uri).hostname or "").lower()
    return any(host == h or host.endswith("." + h)
               for h in _REPLAY_EXCLUDED_HOSTS)


def _replay_excluded_record(record) -> bool:
    """True for records that must not enter the replay copy: WAF challenge-SDK
    hosts, and challenge-verdict responses (e.g. the HTTP 202 interstitial
    captured for a page URL before the real page loaded — left in, it can
    shadow the real 200 document under the same URL at replay)."""
    uri = record.rec_headers.get_header("WARC-Target-URI") or ""
    if uri and _replay_excluded(uri):
        return True
    if record.rec_type in ("response", "revisit"):
        from .detect import WAF_ACTION_HEADER
        http = record.http_headers
        if http is not None and http.get_header(WAF_ACTION_HEADER):
            return True
    return False


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

    # Combine the WARCs into one replay archive, dropping records from hosts
    # that must not replay (WAF challenge SDK — see _REPLAY_EXCLUDED_HOSTS).
    # The originals are never modified; this is a per-record rewrite of the
    # replay copy only, which costs a parse pass but keeps archived bot
    # challenges from sabotaging their own replay. The filename carries a
    # content digest: ReplayWeb.page caches loaded archives by source URL, so
    # a stable name could serve a stale index after new captures are added —
    # a changed archive must get a changed URL.
    import hashlib

    from warcio.archiveiterator import ArchiveIterator
    from warcio.warcwriter import WARCWriter

    tmp = site_dir / "archive.tmp"
    excluded = 0
    with open(tmp, "wb") as out:
        writer = WARCWriter(out, gzip=True)
        for p in warc_paths:
            with open(Path(p).resolve(), "rb") as f:
                for record in ArchiveIterator(f):
                    if _replay_excluded_record(record):
                        excluded += 1
                        continue
                    writer.write_record(record)
    if excluded:
        log.info("Excluded %d WAF challenge record(s) from the replay copy "
                 "so the archived challenge cannot break replay (original "
                 "WARCs are untouched)", excluded)

    digest = hashlib.sha1()
    with open(tmp, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    archive_name = f"archive-{digest.hexdigest()[:12]}.warc.gz"
    archive = site_dir / archive_name
    for old in site_dir.glob("archive-*.warc.gz"):
        if old.name != archive_name:
            old.unlink()
    tmp.replace(archive)

    # Prefer vendored (and patched) assets; see _SW_PATCH_OLD above. With
    # self_host the files must already be in <replay-root>/vendor (offline);
    # otherwise they are downloaded once. Only if neither works does the site
    # reference the CDN directly, which leaves the upstream POST-lookup bug
    # in place.
    vendor_dir = site_dir.parent / "vendor"
    if self_host:
        if not _ensure_vendor_assets(vendor_dir, download=False):
            raise FileNotFoundError(
                f"self_host requires ui.js and sw.js in {vendor_dir}")
        vendored = True
    else:
        vendored = _ensure_vendor_assets(vendor_dir, download=True)
        if not vendored:
            log.warning(
                "Falling back to the ReplayWeb.page CDN: sites that load "
                "their content via POST requests (GraphQL APIs) may replay "
                "as 'content not found' until the vendored, patched worker "
                "can be downloaded (see webarc.replay._SW_PATCH_OLD)")
    if vendored:
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
