"""Runtime hardening for interactive recording.

Playwright dispatches network and download events while the synchronous API is
inside another Playwright call. Waiting for a PDF refetch or a completed download
inside those callbacks can block the page that is trying to display the viewer.
This module installs a RecordingSession subclass that queues that work and
processes it from the recorder's normal pump loop instead.

Downloads are copied to <session-output>/downloads/ and left there for the
operator. The same bytes are also written to WARC. No second GET is used as the
primary download path, so POST-generated, signed, blob, and one-use downloads are
not silently replaced with a different response.
"""

from __future__ import annotations

import logging
import shutil
from collections import deque
from pathlib import Path

from .capture import safe_filename_component
from . import recorder as _recorder

log = logging.getLogger(__name__)


def _looks_like_pdf(body: bytes) -> bool:
    """Accept leading whitespace/BOM but reject Chromium viewer HTML."""
    return body.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"%PDF-")


class RecordingSession(_recorder.RecordingSession):
    """Recorder that keeps browser callbacks non-blocking."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending_pdf: dict[object, object] = {}
        self._pdf_refetch_queue: deque[object] = deque()
        self._download_queue: deque[tuple[object, bool]] = deque()

    # -- event callbacks: enqueue only -------------------------------------
    def _on_response(self, response) -> None:
        request = response.request
        if request not in self._eligible:
            return
        if 300 <= response.status < 400:
            self._write_exchange(response, b"")
            return

        ctype = ""
        try:
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:
            pass
        if (ctype.startswith("application/pdf")
                and request.resource_type == "document"):
            # Do not call response.body() or issue another request from inside
            # the response callback. Either action can stall an embedded viewer.
            self._pending_pdf[request] = response
            return

        super()._on_response(response)

    def _on_request_finished(self, request) -> None:
        response = self._pending_pdf.pop(request, None)
        if response is None:
            super()._on_request_finished(request)
            return

        if request in self._eligible:
            try:
                body = response.body()
            except Exception:
                body = b""
            if body and _looks_like_pdf(body):
                self._write_exchange(response, body)
                self.capture_stats["pdf-original-body"] += 1
            else:
                # Refetch later, from the main recorder loop. This preserves the
                # live viewer's event flow and rejects viewer HTML under a PDF URL.
                self._pdf_refetch_queue.append(response)
        self._eligible.discard(request)

    def _on_request_failed(self, request) -> None:
        self._pending_pdf.pop(request, None)
        super()._on_request_failed(request)

    def _on_download(self, download) -> None:
        # Capture eligibility at event time, but do not wait for the download in
        # the callback. Waiting here can freeze the initiating page or viewer.
        self._download_queue.append(
            (download, self.state == _recorder.RECORDING)
        )

    # -- main-loop processing ----------------------------------------------
    def _downloads_dir(self) -> Path:
        root = Path(getattr(self.warc, "out_dir", Path("./warcs")))
        path = root / "downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _unique_download_path(self, suggested_name: object) -> Path:
        name = safe_filename_component(suggested_name, "download.bin")
        target = self._downloads_dir() / name
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        counter = 2
        while True:
            candidate = target.with_name(f"{stem}-{counter}{suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _save_actual_download(self, download, target: Path) -> None:
        """Persist the bytes Chromium downloaded, without deleting them."""
        try:
            download.save_as(str(target))
            return
        except Exception as save_exc:
            # Some CDP-attached contexts do not support save_as reliably. A
            # local path still represents the completed browser download.
            try:
                source = download.path()
                if source is None:
                    raise RuntimeError("browser did not expose a download path")
                shutil.copy2(source, target)
                return
            except Exception as path_exc:
                raise RuntimeError(
                    f"save_as failed: {save_exc}; path fallback failed: {path_exc}"
                ) from path_exc

    def _process_download_queue(self) -> None:
        while self._download_queue:
            download, eligible = self._download_queue.popleft()
            if not eligible:
                continue
            url = getattr(download, "url", "") or "about:download"
            filename = getattr(download, "suggested_filename", None) or "download.bin"
            target = self._unique_download_path(filename)
            try:
                self._save_actual_download(download, target)
                body = target.read_bytes()
                self._write_download_bytes(url, target.name, body)
                self.capture_stats["downloads-retained"] += 1
                log.info("Saved user download to %s", target)
            except Exception as exc:
                self.capture_stats["downloads-failed"] += 1
                log.warning("Could not preserve download %s: %s", url, exc)

    def _process_pdf_queue(self) -> None:
        while self._pdf_refetch_queue:
            response = self._pdf_refetch_queue.popleft()
            # The inherited method preserves safe original request headers,
            # validates failure by writing an empty body, and disposes the API
            # response buffer. Crucially, it now runs outside the event callback.
            self._capture_pdf_response(response)

    def _process_queued_capture(self) -> None:
        self._process_download_queue()
        self._process_pdf_queue()

    def _pump(self, context) -> None:
        self._process_queued_capture()
        super()._pump(context)

    def _log_capture_summary(self) -> None:
        # Flush anything queued by the final Playwright dispatch before the
        # context is closed by BrowserDriver.__exit__().
        self._process_queued_capture()
        super()._log_capture_summary()


def install() -> None:
    """Expose the hardened class through webarc.recorder for all callers."""
    _recorder.RecordingSession = RecordingSession
