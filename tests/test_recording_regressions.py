from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webarc.capture import WarcSession, safe_filename_component
from webarc.config import BrowserConfig, WarcConfig
from webarc.recorder import (CMD_CAPTURE_PAGE, PAUSED, RecordingSession)


class DummyWarc:
    def __init__(self):
        self.writes: list[dict] = []
        self.total_bytes = 0

    def write_exchange(self, **kwargs):
        self.writes.append(kwargs)
        self.total_bytes += len(kwargs.get("body") or b"")

    def close(self):
        pass


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.context = None
        self.reloads = 0

    def reload(self, **_kwargs):
        self.reloads += 1


class FakeContext:
    def __init__(self, pages=None, request=None):
        self.pages = list(pages or [])
        self.request = request
        for page in self.pages:
            page.context = self

    def new_cdp_session(self, _page):
        raise RuntimeError("CDP intentionally unavailable in unit test")


class FakeDownload:
    def __init__(self, path: Path, url: str, filename: str):
        self._path = path
        self.url = url
        self.suggested_filename = filename
        self.deleted = False

    def path(self):
        return self._path

    def delete(self):
        self.deleted = True


class FakeDirectResponse:
    def __init__(self, body: bytes):
        self.ok = True
        self.status = 200
        self.status_text = "OK"
        self.headers = {"content-type": "application/pdf"}
        self._body = body
        self.disposed = False

    def body(self):
        return self._body

    def dispose(self):
        self.disposed = True


class FakeRequestContext:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.headers = None

    def get(self, _url, *, headers=None, timeout=None):
        self.headers = headers
        if self.error:
            raise self.error
        return self.response


class FakeRequest:
    """Identity-hashable stand-in for Playwright's Request object."""

    def __init__(self, *, method="GET", headers=None, post_data_buffer=None,
                 resource_type="document"):
        self.method = method
        self.headers = headers or {}
        self.post_data_buffer = post_data_buffer
        self.resource_type = resource_type


class RecordingRegressionTests(unittest.TestCase):
    def make_session(self, warc=None):
        return RecordingSession(
            "https://example.org/",
            BrowserConfig(mode="headed"),
            warc or DummyWarc(),
        )

    def test_warc_filename_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            session = WarcSession(
                out,
                "../../outside\\nested:report",
                "https://example.org/",
                1,
                "tester",
                WarcConfig(),
            )
            path = session._current_path
            session.close()

            self.assertEqual(path.parent.resolve(), out.resolve())
            self.assertNotIn("/", path.name)
            self.assertNotIn("\\", path.name)
            self.assertNotIn(":", path.name)
            self.assertTrue(path.name.endswith(".warc.gz"))

    def test_windows_device_names_are_normalised(self):
        self.assertEqual(safe_filename_component("CON"), "_CON")
        self.assertEqual(safe_filename_component("  ../  "), "capture")

    def test_capture_page_targets_the_widget_tab(self):
        first = FakePage("https://example.org/first")
        second = FakePage("https://example.org/second")
        context = FakeContext([first, second])
        session = self.make_session()
        session._context = context
        session.state = PAUSED
        session.current_url = first.url

        session._on_widget_command({"page": second}, CMD_CAPTURE_PAGE)
        command, page = session._commands.popleft()
        session.apply(command, page)

        self.assertEqual(first.reloads, 0)
        self.assertEqual(second.reloads, 1)

    def test_download_archives_actual_browser_file_bytes(self):
        warc = DummyWarc()
        session = self.make_session(warc)
        payload = b"%PDF-1.7\nactual browser download\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.pdf"
            path.write_bytes(payload)
            download = FakeDownload(
                path,
                "blob:https://example.org/1234",
                "report.pdf",
            )

            session._on_download(download)

        self.assertTrue(download.deleted)
        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["body"], payload)
        self.assertEqual(
            warc.writes[0]["resp_headers"]["content-type"],
            "application/pdf",
        )
        self.assertEqual(session.capture_stats["downloads-captured"], 1)
        self.assertEqual(session.capture_stats["downloads-refetched"], 0)

    def test_failed_pdf_refetch_never_archives_viewer_html(self):
        warc = DummyWarc()
        session = self.make_session(warc)
        session._context = FakeContext(
            request=FakeRequestContext(error=RuntimeError("network failure"))
        )
        request = FakeRequest(headers={"accept": "application/pdf"})
        response = SimpleNamespace(
            request=request,
            url="https://example.org/report.pdf",
            status=200,
            status_text="OK",
            headers={"content-type": "application/pdf"},
            body=lambda: b"<html>Chromium PDF viewer shell</html>",
        )
        session._eligible.add(request)

        session._on_response(response)

        self.assertEqual(len(warc.writes), 1)
        self.assertEqual(warc.writes[0]["body"], b"")
        self.assertEqual(session.capture_stats["body-unavailable"], 1)

    def test_pdf_refetch_preserves_range_and_disposes_response(self):
        warc = DummyWarc()
        direct = FakeDirectResponse(b"%PDF-1.7\nrefetched\n")
        request_context = FakeRequestContext(response=direct)
        session = self.make_session(warc)
        session._context = FakeContext(request=request_context)
        request = FakeRequest(
            headers={
                "host": "example.org",
                "content-length": "0",
                "range": "bytes=0-1023",
                "referer": "https://example.org/index.html",
            }
        )
        response = SimpleNamespace(
            request=request,
            url="https://example.org/report.pdf",
            status=200,
            status_text="OK",
            headers={"content-type": "application/pdf"},
        )

        session._capture_pdf_response(response)

        self.assertTrue(direct.disposed)
        self.assertEqual(request_context.headers["range"], "bytes=0-1023")
        self.assertNotIn("host", request_context.headers)
        self.assertNotIn("content-length", request_context.headers)
        self.assertEqual(warc.writes[0]["body"], b"%PDF-1.7\nrefetched\n")

    def test_dashboard_hardening_is_loaded_and_packaged(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "webarc" / "server.py").read_text(encoding="utf-8")
        script = (root / "webarc" / "dashboard_hardening.js").read_text(
            encoding="utf-8"
        )
        package = (root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("DASHBOARD_HARDENING", server)
        self.assertIn("escapeHtml", script)
        self.assertIn('$$("#new-form .mode-tab")', script)
        self.assertIn('"dashboard_hardening.js"', package)


if __name__ == "__main__":
    unittest.main()
