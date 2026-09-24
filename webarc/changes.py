"""What changed since the collection last saw a page.

Two captures of the same page are seldom the same bytes: a cache stamp in a
comment, a nonce in a script, the menu item marked as current. Byte-level
deduplication (the collection index) rightly stores such a page again. This
module answers the curator's question instead: is the page's *content* new,
changed, or the same as when the collection last held it, and which pages
the collection held are gone.

The fingerprint is a hash of the page's text and its links, with scripts,
styles, comments, tags and attributes stripped and whitespace collapsed. It
changes when the words or the links on the page change, and stays when only
the markup around them does. Records of it live in the collection index
(the pages table), and each job writes a changes.json beside its WARCs.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

REPORT_NAME = "changes.json"
CHANGES = ("new", "changed", "unchanged", "gone")
PAGE_MIMES = ("text/html", "application/xhtml+xml")
GONE_STATUSES = (404, 410)

_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_LINK_ATTRS = {("a", "href"), ("area", "href"), ("iframe", "src"), ("img", "src"),
               ("video", "src"), ("audio", "src"), ("source", "src")}
_WS = re.compile(r"\s+")


class _TextAndLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: set[str] = set()
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        for name, value in attrs:
            if (tag, name) in _LINK_ATTRS and value:
                self.links.add(value.strip())

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)


def page_fingerprint(body: bytes, charset: Optional[str] = None) -> str:
    """The fingerprint of an HTML page: its words and its links."""
    text = body.decode(charset or "utf-8", "replace") if body else ""
    parser = _TextAndLinks()
    try:
        parser.feed(text)
        parser.close()
    except Exception:                                   # noqa: BLE001 - a page that will not parse
        pass
    words = _WS.sub(" ", " ".join(parser.parts)).strip()
    links = "\n".join(sorted(parser.links))
    return "sha1:" + hashlib.sha1((words + "\n--links--\n" + links).encode("utf-8")).hexdigest()


def is_page(status: Optional[int], mime: Optional[str]) -> bool:
    return bool(mime) and mime.split(";", 1)[0].strip().lower() in PAGE_MIMES


def charset_of(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    m = re.search(r"charset=\"?([\w.\-]+)", content_type, re.I)
    return m.group(1) if m else None


def classify(previous: Optional[dict], fingerprint: Optional[str], status: Optional[int]) -> Optional[str]:
    """How this capture of a page relates to the collection's last one.

    A 200 page is new when the collection never held it, unchanged when its
    fingerprint matches the last capture, changed otherwise. A 404 or 410
    for a page the collection held is gone. Anything else is not a page
    event and is not recorded.
    """
    if status in GONE_STATUSES:
        return "gone" if previous and previous.get("status") == 200 else None
    if status != 200 or not fingerprint:
        return None
    if previous is None or previous.get("status") != 200:
        return "new"
    return "unchanged" if previous.get("fingerprint") == fingerprint else "changed"


def write_report(index, crawl_id: int, job_dir: Path | str) -> dict:
    """changes.json for a job, from the index: what the job found new,
    changed, unchanged and gone, and which pages the collection held that
    this job did not visit."""
    report = index.page_changes(crawl_id)
    report["not_visited"] = index.pages_not_visited(crawl_id)
    counts = {key: len(report[key]) for key in (*CHANGES, "not_visited")}
    document = {
        "schema": "swm-page-changes/1",
        "job_id": crawl_id,
        "counts": counts,
        **report,
        "note": ("Pages compared by their words and links with the collection's last "
                 "capture of them, so a new cache stamp or nonce does not count as a "
                 "change. 'gone' is a page the collection held that now answers 404 or "
                 "410; 'not_visited' is one the collection held that this job did not "
                 "reach, which says nothing about whether it still exists."),
    }
    try:
        (Path(job_dir) / REPORT_NAME).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return document


def read_report(job_dir: Path | str) -> Optional[dict]:
    try:
        return json.loads((Path(job_dir) / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
