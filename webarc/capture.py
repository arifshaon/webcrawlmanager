"""Capture layer: turn Playwright network events into WARC request/response
records via warcio, with SHA-1 payload-digest dedup emitting revisit records.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from . import __version__
from .config import WarcConfig

log = logging.getLogger(__name__)

# hop-by-hop / synthetic headers that must not be replayed into WARC records
_STRIP_REQ = {"host", "content-length"}
_STRIP_RESP = {"content-encoding", "transfer-encoding", "content-length"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_header_value(value: object) -> str:
    """Return a header value that cannot create a second HTTP header line.

    Chromium/Playwright may represent repeated response headers as one value
    separated by embedded newlines. Passing that value directly to warcio
    creates a malformed HTTP header block. Browsertrix and ArchiveWeb.page use
    the same practical approach: collapse line breaks into a comma-separated
    value before serialisation.
    """
    return (str(value)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", ", "))


def _header_pairs(headers: object) -> list[tuple[str, str]]:
    """Normalise dictionary, tuple-list, or Playwright header-array input.

    Supporting ordered pairs now lets callers move to ``headers_array()``
    without another capture-layer change. Invalid names and HTTP/2 pseudo
    headers are discarded; repeated valid names remain separate entries.
    """
    if headers is None:
        return []

    if isinstance(headers, Mapping):
        source = headers.items()
    else:
        try:
            source = iter(headers)  # type: ignore[arg-type]
        except TypeError:
            return []

    result: list[tuple[str, str]] = []
    for item in source:
        if isinstance(item, Mapping):
            name = item.get("name")
            value = item.get("value", "")
        else:
            try:
                name, value = item
            except (TypeError, ValueError):
                continue

        name = str(name or "").strip()
        if (not name or name.startswith(":")
                or "\r" in name or "\n" in name):
            continue
        result.append((name, _clean_header_value(value)))

    return result


class WarcSession:
    """One (rotating) WARC output per seed."""

    def __init__(self, out_dir: Path, crawl_name: str, seed_url: str,
                 seed_idx: int, operator: str, cfg: WarcConfig,
                 info_extra: dict | None = None):
        self.out_dir = out_dir
        self.crawl_name = crawl_name
        self.seed_url = seed_url
        self.seed_idx = seed_idx
        self.operator = operator
        self.cfg = cfg
        # overrides/additions to the warcinfo record, e.g. a recording
        # session sets robots: none since a human drives the navigation
        self.info_extra = info_extra or {}
        self.serial = 0
        self.bytes_written = 0
        self._fh = None
        self._writer: WARCWriter | None = None
        self._digests: dict[str, tuple[str, str, str]] = {}  # digest -> (uri, date, record_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        self._rotate()

    # -- file management -----------------------------------------------------
    @property
    def path(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return self.out_dir / (
            f"{self.crawl_name}-seed{self.seed_idx:03d}-{ts}-{self.serial:05d}.warc.gz"
        )

    def _rotate(self) -> None:
        if self._fh:
            self._fh.close()
        self.serial += 1
        self._current_path = self.path
        self._fh = open(self._current_path, "wb")
        self._writer = WARCWriter(self._fh, gzip=True)
        info = self._writer.create_warcinfo_record(
            filename=self._current_path.name,
            info={
                "software": f"webarc/{__version__} (warcio)",
                "format": "WARC File Format 1.1",
                "operator": self.operator,
                "isPartOf": self.crawl_name,
                "description": f"Browser-based capture of seed {self.seed_url}",
                "robots": "obey",
                **self.info_extra,
            },
        )
        self._writer.write_record(info)
        log.info("Writing WARC: %s", self._current_path.name)

    def _maybe_rotate(self) -> None:
        size = self._current_path.stat().st_size
        # bytes_written tracks total across rotated files: closed files keep
        # their size, only the active file's size is still growing
        if size > self.cfg.max_size_mb * 1024 * 1024:
            self._closed_bytes = getattr(self, "_closed_bytes", 0) + size
            self._rotate()

    @property
    def total_bytes(self) -> int:
        active = self._current_path.stat().st_size if self._current_path.exists() else 0
        return getattr(self, "_closed_bytes", 0) + active

    # -- record writing ------------------------------------------------------
    def write_exchange(self, *, url: str, method: str, req_headers: object,
                       post_data: bytes | None, status: int, status_text: str,
                       resp_headers: object, body: bytes,
                       http_version: str = "HTTP/1.1") -> None:
        assert self._writer is not None
        date = _utcnow()

        # ---- request record
        req_hlist = [(k, v) for k, v in _header_pairs(req_headers)
                     if k.lower() not in _STRIP_REQ]
        from urllib.parse import urlsplit
        p = urlsplit(url)
        req_hlist.insert(0, ("Host", p.netloc))
        if post_data:
            req_hlist.append(("Content-Length", str(len(post_data))))
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        req_status = StatusAndHeaders(f"{method} {path} {http_version}",
                                      req_hlist, is_http_request=True)
        req_record = self._writer.create_warc_record(
            url, "request",
            payload=BytesIO(post_data or b""),
            http_headers=req_status,
            warc_content_type="application/http; msgtype=request",
        )
        req_record.rec_headers.replace_header("WARC-Date", date)

        # ---- response or revisit record
        digest = "sha1:" + hashlib.sha1(body).hexdigest()
        resp_hlist = [(k, v) for k, v in _header_pairs(resp_headers)
                      if k.lower() not in _STRIP_RESP]
        resp_hlist.append(("Content-Length", str(len(body))))
        resp_status = StatusAndHeaders(f"{status} {status_text}".strip(),
                                       resp_hlist, protocol=http_version)

        if self.cfg.dedup and body and digest in self._digests:
            orig_uri, orig_date, orig_id = self._digests[digest]
            record = self._writer.create_revisit_record(
                url, digest=digest, refers_to_uri=orig_uri,
                refers_to_date=orig_date, http_headers=resp_status,
            )
            record.rec_headers.add_header("WARC-Refers-To", orig_id)
        else:
            record = self._writer.create_warc_record(
                url, "response",
                payload=BytesIO(body),
                http_headers=resp_status,
                warc_content_type="application/http; msgtype=response",
            )
            if self.cfg.dedup and body:
                self._digests[digest] = (
                    url, date, record.rec_headers.get_header("WARC-Record-ID"))

        record.rec_headers.replace_header("WARC-Date", date)
        # link request to response
        req_record.rec_headers.add_header(
            "WARC-Concurrent-To",
            record.rec_headers.get_header("WARC-Record-ID"))

        self._writer.write_record(record)
        self._writer.write_record(req_record)
        self._maybe_rotate()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
