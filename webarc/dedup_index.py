"""The collection index: what a collection already holds, payload by payload.

Within one job the WARC writer already keeps a table of the payloads it has
written and stores a repeat as a WARC *revisit* record pointing at the first
copy. That table lives in memory and dies with the job. This index is the
same table made durable and shared by every job in a collection, kept in an
SQLite file beside the collection's jobs, so an image, a stylesheet or a
script the collection already holds is written once and referred to
thereafter, whichever job meets it again.

Every capture is recorded here, revisit or not, with its URL, date, digest
and the WARC file and record that hold it. That is a CDX-shaped history of
the collection: it answers "have we this payload" today, and "when did we
last see this URL, and had it changed" for the crawl policy to come.

The index is also what a deletion consults. A revisit refers into the job
that holds the original; delete that job and the revisit's page replays
without its content. The index counts those references before a deletion,
and afterwards marks them orphaned so they can be listed and re-crawled.

Concurrency: jobs of one collection may run at once. Every write is its
own short transaction, so the file is never locked between two captures:
a sibling job, or the dashboard reading the collection's counts, waits at
most for one row. A duplicate original written by two jobs in the same
instant is merely stored twice, never lost.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FILE_NAME = "index.sqlite"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id            INTEGER,
    url                 TEXT NOT NULL,
    url_key             TEXT NOT NULL,
    warc_date           TEXT NOT NULL,
    digest              TEXT NOT NULL,
    record_id           TEXT NOT NULL,
    record_type         TEXT NOT NULL,
    status              INTEGER,
    mime                TEXT,
    length              INTEGER NOT NULL DEFAULT 0,
    warc_file           TEXT NOT NULL,
    refers_to_crawl_id  INTEGER,
    refers_to_record_id TEXT,
    refers_to_url       TEXT,
    refers_to_date      TEXT,
    orphaned            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS captures_digest ON captures(digest, record_type);
CREATE INDEX IF NOT EXISTS captures_refers ON captures(refers_to_crawl_id);
CREATE INDEX IF NOT EXISTS captures_crawl ON captures(crawl_id);
CREATE INDEX IF NOT EXISTS captures_url ON captures(url_key, warc_date);
CREATE TABLE IF NOT EXISTS jobs_removed (
    crawl_id         INTEGER PRIMARY KEY,
    removed_at       TEXT NOT NULL,
    orphaned_records INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Added after the first indexes were written: created on open when missing.
_PAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id          INTEGER,
    url               TEXT NOT NULL,
    url_key           TEXT NOT NULL,
    warc_date         TEXT NOT NULL,
    status            INTEGER,
    fingerprint       TEXT,
    change            TEXT,
    previous_crawl_id INTEGER
);
CREATE INDEX IF NOT EXISTS pages_url ON pages(url_key, warc_date);
CREATE INDEX IF NOT EXISTS pages_crawl ON pages(crawl_id, change);
"""

_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_ga")


def url_key(url: str) -> str:
    """One key for the URLs that are the same page: lower-cased scheme and
    host, no fragment, no tracking parameters, query sorted."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return str(url or "").strip()
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAMS))
    host = (parts.hostname or "").lower()
    if port and not ((parts.scheme == "http" and port == 80)
                     or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CollectionIndex:
    """The durable payload table of one collection."""

    # Each write is one transaction (autocommit mode, BEGIN IMMEDIATE around
    # the statements of a write). With WAL and synchronous=NORMAL that is
    # cheap, and nothing holds the file's write lock between captures.
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30,
                                     check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        if not self._has_table("meta"):
            # Only a new file is written to on open: opening an index a
            # sibling job is writing to must never need its lock.
            self._conn.execute("PRAGMA journal_mode=WAL")
            with self._transaction() as c:
                for statement in (_SCHEMA + _PAGES_SCHEMA).split(";"):
                    if statement.strip():
                        c.execute(statement)
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                          (str(SCHEMA_VERSION),))
        elif not self._has_table("pages"):            # an index from before page changes
            with self._transaction() as c:
                for statement in _PAGES_SCHEMA.split(";"):
                    if statement.strip():
                        c.execute(statement)
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None

    @classmethod
    def for_collection(cls, root_dir: Path | str) -> "CollectionIndex":
        return cls(Path(root_dir) / FILE_NAME)

    @staticmethod
    def path_for(root_dir: Path | str) -> Path:
        return Path(root_dir) / FILE_NAME

    # -- lifecycle -----------------------------------------------------------
    def commit(self) -> None:
        """Every write has already been committed; kept for callers that
        flush before reading."""
        if self._conn.in_transaction:            # pragma: no cover - defensive
            self._conn.commit()

    def close(self) -> None:
        try:
            self.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> "CollectionIndex":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- what is held ---------------------------------------------------------
    def lookup(self, digest: str) -> Optional[dict]:
        """The earliest original holding this payload, if any job of the
        collection still holds it."""
        row = self._conn.execute(
            "SELECT crawl_id, url, warc_date, record_id, warc_file FROM captures "
            "WHERE digest=? AND record_type='response' AND orphaned=0 "
            "ORDER BY id LIMIT 1", (digest,)).fetchone()
        return dict(row) if row else None

    def record_response(self, *, crawl_id: Optional[int], url: str, warc_date: str,
                        digest: str, record_id: str, warc_file: str,
                        status: Optional[int] = None, mime: Optional[str] = None,
                        length: int = 0) -> None:
        key = url_key(url)
        with self._transaction() as c:
            c.execute(
                "INSERT INTO captures (crawl_id, url, url_key, warc_date, digest, record_id, "
                "record_type, status, mime, length, warc_file) "
                "VALUES (?,?,?,?,?,?,'response',?,?,?,?)",
                (crawl_id, url, key, warc_date, digest, record_id,
                 status, mime, int(length), warc_file))
            # A page stored again in full is no longer missing its original.
            c.execute("UPDATE captures SET orphaned=0 WHERE orphaned=1 AND url_key=?", (key,))

    def record_revisit(self, *, crawl_id: Optional[int], url: str, warc_date: str,
                       digest: str, record_id: str, warc_file: str,
                       refers_to: dict, status: Optional[int] = None,
                       mime: Optional[str] = None, length: int = 0) -> None:
        # A revisit into a job that has since been deleted is orphaned from
        # the start: its page has no original to replay from.
        with self._transaction() as c:
            c.execute(
                "INSERT INTO captures (crawl_id, url, url_key, warc_date, digest, record_id, "
                "record_type, status, mime, length, warc_file, refers_to_crawl_id, "
                "refers_to_record_id, refers_to_url, refers_to_date, orphaned) "
                "VALUES (?,?,?,?,?,?,'revisit',?,?,?,?,?,?,?,?,"
                "EXISTS(SELECT 1 FROM jobs_removed WHERE crawl_id=?))",
                (crawl_id, url, url_key(url), warc_date, digest, record_id,
                 status, mime, int(length), warc_file,
                 refers_to.get("crawl_id"), refers_to.get("record_id"),
                 refers_to.get("url"), refers_to.get("warc_date"),
                 refers_to.get("crawl_id")))

    def find_record(self, record_id: str) -> Optional[dict]:
        """The capture a WARC-Refers-To names, if the index holds it."""
        row = self._conn.execute(
            "SELECT crawl_id, url, warc_date, record_type, length FROM captures "
            "WHERE record_id=? LIMIT 1", (record_id,)).fetchone()
        return dict(row) if row else None

    def find_page_fingerprint(self, record_id: str) -> Optional[str]:
        """The fingerprint recorded for the page an original record holds."""
        row = self._conn.execute(
            "SELECT p.fingerprint FROM captures c JOIN pages p ON p.url_key=c.url_key "
            "AND p.crawl_id=c.crawl_id AND p.warc_date=c.warc_date "
            "WHERE c.record_id=? AND p.fingerprint IS NOT NULL LIMIT 1", (record_id,)).fetchone()
        return row["fingerprint"] if row else None

    def mark_orphaned(self, record_id: str) -> None:
        with self._transaction() as c:
            c.execute("UPDATE captures SET orphaned=1 WHERE record_id=?", (record_id,))

    def removed_jobs(self) -> set[int]:
        """Jobs whose originals were deleted; nothing may refer into them."""
        rows = self._conn.execute("SELECT crawl_id FROM jobs_removed").fetchall()
        return {int(r["crawl_id"]) for r in rows}

    # -- history -------------------------------------------------------------
    def last_seen(self, url: str) -> Optional[dict]:
        """The most recent capture of a URL, revisit or not."""
        row = self._conn.execute(
            "SELECT crawl_id, url, warc_date, digest, record_type, status, mime, length, "
            "warc_file, orphaned FROM captures WHERE url_key=? "
            "ORDER BY warc_date DESC, id DESC LIMIT 1", (url_key(url),)).fetchone()
        return dict(row) if row else None

    def summary(self, crawl_id: int) -> dict:
        """What one job wrote: originals, revisits within and across jobs,
        bytes it did not have to store, and which jobs it refers into."""
        self.commit()
        rows = self._conn.execute(
            "SELECT record_type, refers_to_crawl_id, COUNT(*) AS n, "
            "COALESCE(SUM(length), 0) AS bytes FROM captures WHERE crawl_id=? "
            "GROUP BY record_type, refers_to_crawl_id", (crawl_id,)).fetchall()
        out = {"responses": 0, "revisits_within_job": 0, "revisits_across_jobs": 0,
               "bytes_saved": 0, "bytes_saved_across_jobs": 0, "refers_to_jobs": {}}
        for row in rows:
            if row["record_type"] == "response":
                out["responses"] += int(row["n"])
                continue
            out["bytes_saved"] += int(row["bytes"])
            if row["refers_to_crawl_id"] in (None, crawl_id):
                out["revisits_within_job"] += int(row["n"])
            else:
                out["revisits_across_jobs"] += int(row["n"])
                out["bytes_saved_across_jobs"] += int(row["bytes"])
                key = str(row["refers_to_crawl_id"])
                out["refers_to_jobs"][key] = out["refers_to_jobs"].get(key, 0) + int(row["n"])
        return out

    def referenced_jobs(self, crawl_id: int) -> list[int]:
        """The jobs whose originals this job's revisits point at."""
        self.commit()
        rows = self._conn.execute(
            "SELECT DISTINCT refers_to_crawl_id FROM captures WHERE crawl_id=? "
            "AND record_type='revisit' AND refers_to_crawl_id IS NOT NULL "
            "AND refers_to_crawl_id<>?", (crawl_id, crawl_id)).fetchall()
        return sorted(int(r["refers_to_crawl_id"]) for r in rows)

    # -- pages: what changed since the collection last saw them -----------------
    def last_page(self, url: str, before_crawl_id: Optional[int] = None) -> Optional[dict]:
        """The collection's latest capture of a page by an earlier job."""
        query = ("SELECT crawl_id, warc_date, status, fingerprint FROM pages WHERE url_key=? "
                 "AND status IN (200, 404, 410)")
        args: list = [url_key(url)]
        if before_crawl_id is not None:
            query += " AND crawl_id<>?"
            args.append(before_crawl_id)
        row = self._conn.execute(query + " ORDER BY warc_date DESC, id DESC LIMIT 1",
                                 args).fetchone()
        return dict(row) if row else None

    def record_page(self, *, crawl_id: Optional[int], url: str, warc_date: str,
                    status: Optional[int], fingerprint: Optional[str], change: Optional[str],
                    previous_crawl_id: Optional[int] = None) -> None:
        with self._transaction() as c:
            c.execute(
                "INSERT INTO pages (crawl_id, url, url_key, warc_date, status, fingerprint, "
                "change, previous_crawl_id) VALUES (?,?,?,?,?,?,?,?)",
                (crawl_id, url, url_key(url), warc_date, status, fingerprint, change,
                 previous_crawl_id))

    def page_changes(self, crawl_id: int) -> dict[str, list[dict]]:
        """A job's pages by what happened to them, one entry per page (its
        last capture in the job)."""
        out: dict[str, list[dict]] = {"new": [], "changed": [], "unchanged": [], "gone": []}
        seen: set[str] = set()
        rows = self._conn.execute(
            "SELECT url, url_key, warc_date, status, change, previous_crawl_id FROM pages "
            "WHERE crawl_id=? AND change IS NOT NULL ORDER BY warc_date DESC, id DESC",
            (crawl_id,)).fetchall()
        for row in rows:
            if row["url_key"] in seen:
                continue
            seen.add(row["url_key"])
            out[row["change"]].append({
                "url": row["url"], "date": row["warc_date"], "status": row["status"],
                "previous_job": row["previous_crawl_id"]})
        for entries in out.values():
            entries.sort(key=lambda e: e["url"])
        return out

    def pages_not_visited(self, crawl_id: int) -> list[dict]:
        """Pages the collection's earlier jobs held (their latest capture a
        200, not since found gone) that this job did not reach at all."""
        visited = {r["url_key"] for r in self._conn.execute(
            "SELECT DISTINCT url_key FROM pages WHERE crawl_id=?", (crawl_id,))}
        rows = self._conn.execute(
            "SELECT url, url_key, warc_date, status, crawl_id FROM pages WHERE crawl_id<>? "
            "ORDER BY url_key, warc_date DESC, id DESC", (crawl_id,)).fetchall()
        out, seen = [], set()
        for row in rows:                       # the first row per page is its latest capture
            if row["url_key"] in seen:
                continue
            seen.add(row["url_key"])
            if row["status"] == 200 and row["url_key"] not in visited:
                out.append({"url": row["url"], "last_seen": row["warc_date"],
                            "last_job": row["crawl_id"]})
        out.sort(key=lambda e: e["url"])
        return out

    def page_counts(self, crawl_id: int) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT change, COUNT(DISTINCT url_key) AS n FROM pages WHERE crawl_id=? "
            "AND change IS NOT NULL GROUP BY change", (crawl_id,)).fetchall()
        counts = {"new": 0, "changed": 0, "unchanged": 0, "gone": 0}
        for row in rows:
            counts[row["change"]] = int(row["n"])
        return counts

    # -- deletion ---------------------------------------------------------------
    def referring_into(self, crawl_id: int) -> dict:
        """Which other jobs hold revisits that point into this job's
        originals, and how many records that is."""
        self.commit()
        rows = self._conn.execute(
            "SELECT crawl_id, COUNT(*) AS n FROM captures WHERE refers_to_crawl_id=? "
            "AND record_type='revisit' AND orphaned=0 AND crawl_id<>? "
            "GROUP BY crawl_id ORDER BY crawl_id", (crawl_id, crawl_id)).fetchall()
        jobs = {int(r["crawl_id"]): int(r["n"]) for r in rows if r["crawl_id"] is not None}
        return {"jobs": jobs, "records": sum(jobs.values())}

    def forget_job(self, crawl_id: int) -> int:
        """The job is gone: drop what it held, and mark what pointed into it
        as orphaned so those pages can be listed and re-crawled. Returns
        the number of records orphaned."""
        with self._transaction() as c:
            c.execute("DELETE FROM captures WHERE crawl_id=?", (crawl_id,))
            c.execute("DELETE FROM pages WHERE crawl_id=?", (crawl_id,))
            cur = c.execute(
                "UPDATE captures SET orphaned=1 WHERE refers_to_crawl_id=? "
                "AND record_type='revisit' AND orphaned=0", (crawl_id,))
            orphaned = cur.rowcount if cur.rowcount is not None else 0
            c.execute(
                "INSERT OR REPLACE INTO jobs_removed (crawl_id, removed_at, orphaned_records) "
                "VALUES (?,?,?)", (crawl_id, _now(), orphaned))
        return orphaned

    def orphans(self, crawl_id: Optional[int] = None) -> list[dict]:
        """Pages whose original was deleted: URL, the job holding the
        revisit, and the job that held the original."""
        self.commit()
        query = ("SELECT crawl_id, url, warc_date, refers_to_crawl_id, refers_to_url "
                 "FROM captures WHERE orphaned=1")
        args: tuple = ()
        if crawl_id is not None:
            query += " AND crawl_id=?"
            args = (crawl_id,)
        rows = self._conn.execute(query + " ORDER BY crawl_id, url_key, warc_date", args)
        return [dict(r) for r in rows]

    def orphan_urls(self, crawl_id: Optional[int] = None) -> list[str]:
        """The distinct URLs to re-crawl for the orphans."""
        seen: dict[str, str] = {}
        for row in self.orphans(crawl_id):
            seen.setdefault(url_key(row["url"]), row["url"])
        return list(seen.values())

    def restored(self, urls: Iterator[str] | list[str]) -> int:
        """A re-crawl has stored these pages again: their orphan marks go."""
        keys = [url_key(u) for u in urls]
        if not keys:
            return 0
        with self._transaction() as c:
            total = 0
            for key in keys:
                cur = c.execute("UPDATE captures SET orphaned=0 WHERE orphaned=1 AND url_key=?",
                                (key,))
                total += cur.rowcount or 0
        return total

    def counts(self) -> dict:
        self.commit()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN record_type='response' THEN 1 ELSE 0 END) AS originals, "
            "SUM(CASE WHEN record_type='revisit' THEN 1 ELSE 0 END) AS revisits, "
            "SUM(CASE WHEN orphaned=1 THEN 1 ELSE 0 END) AS orphaned, "
            "COALESCE(SUM(CASE WHEN record_type='revisit' THEN length ELSE 0 END), 0) AS saved "
            "FROM captures").fetchone()
        return {"records": int(row["n"] or 0), "originals": int(row["originals"] or 0),
                "revisits": int(row["revisits"] or 0), "orphaned": int(row["orphaned"] or 0),
                "bytes_saved": int(row["saved"] or 0)}

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write: takes the lock, applies, releases. BEGIN IMMEDIATE
        waits for a sibling's write (busy_timeout) rather than failing."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise


# --- rebuilding from the WARCs themselves -----------------------------------

def rebuild(root_dir: Path | str, jobs: list[tuple[int, Path | str]]) -> dict:
    """Make the collection's index anew from its jobs' WARC files.

    For a collection whose jobs predate the index, or whose index was lost:
    every response and revisit record of every job is read back, in job
    order, so a revisit's WARC-Refers-To resolves to the original that was
    stored before it. A revisit whose original is in no job any more is
    orphaned. The new index replaces the old only once it is complete, and
    each job gets a fresh dedup-summary.json.

    Returns per-job counts and the totals.
    """
    import os

    from . import changes

    root = Path(root_dir)
    final = CollectionIndex.path_for(root)
    fresh = final.with_name(final.name + ".rebuild")
    for stale in (fresh, fresh.with_name(fresh.name + "-wal"), fresh.with_name(fresh.name + "-shm")):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    index = CollectionIndex(fresh)
    index.unresolved = 0                                  # revisits whose original is in no job
    per_job: dict[int, dict] = {}
    unresolved = 0
    unreadable: list[str] = []
    try:
        for crawl_id, job_dir in sorted(jobs, key=lambda item: int(item[0])):
            job_dir = Path(job_dir)
            warcs = sorted(job_dir.glob("*.warc.gz")) + sorted(job_dir.glob("*.warc"))
            for warc in warcs:
                try:
                    _read_warc_into(index, int(crawl_id), warc)
                except Exception as exc:              # noqa: BLE001 - one bad file, not the run
                    unreadable.append(f"{warc.name}: {exc}")
            summary = index.summary(int(crawl_id))
            summary["pages"] = index.page_counts(int(crawl_id))
            per_job[int(crawl_id)] = summary
            _write_job_summary(job_dir, summary)
            changes.write_report(index, int(crawl_id), job_dir)
        totals = index.counts()
    finally:
        index.close()
    # the finished index takes the old one's place; its journal goes with it
    for leftover in (final.with_name(final.name + "-wal"), final.with_name(final.name + "-shm")):
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
    os.replace(fresh, final)
    for leftover in (fresh.with_name(fresh.name + "-wal"), fresh.with_name(fresh.name + "-shm")):
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
    return {"jobs": per_job, "unresolved_revisits": index.unresolved, "unreadable": unreadable,
            **totals}


def _read_warc_into(index: "CollectionIndex", crawl_id: int, warc: Path) -> None:
    """Every response and revisit record of one WARC file, into the index."""
    import hashlib

    from warcio.archiveiterator import ArchiveIterator

    with warc.open("rb") as handle:
        for record in ArchiveIterator(handle):
            if record.rec_type not in ("response", "revisit"):
                continue
            h = record.rec_headers
            url = h.get_header("WARC-Target-URI") or ""
            date = h.get_header("WARC-Date") or ""
            record_id = h.get_header("WARC-Record-ID") or ""
            status = None
            mime = None
            if record.http_headers is not None:
                try:
                    status = int(record.http_headers.get_statuscode())
                except (TypeError, ValueError):
                    status = None
                ctype = record.http_headers.get_header("Content-Type") or ""
                mime = ctype.split(";", 1)[0].strip().lower() or None
            if record.rec_type == "response":
                body = record.content_stream().read()
                index.record_response(
                    crawl_id=crawl_id, url=url, warc_date=date,
                    digest="sha1:" + hashlib.sha1(body).hexdigest(),
                    record_id=record_id, warc_file=warc.name,
                    status=status, mime=mime, length=len(body))
                note_page(index, crawl_id, url, date, status, mime, body,
                          record.http_headers.get_header("Content-Type") if record.http_headers else None)
                continue
            if record.rec_type == "revisit" and status == 200 and mime in ("text/html", "application/xhtml+xml"):
                # the same bytes as the original: the same page content
                original_page = index.find_page_fingerprint(h.get_header("WARC-Refers-To") or "")
                if original_page:
                    note_page(index, crawl_id, url, date, status, mime, None, None,
                              fingerprint=original_page)
            refers_id = h.get_header("WARC-Refers-To") or ""
            original = index.find_record(refers_id) if refers_id else None
            index.record_revisit(
                crawl_id=crawl_id, url=url, warc_date=date,
                digest=h.get_header("WARC-Payload-Digest") or "",
                record_id=record_id, warc_file=warc.name, status=status,
                mime=mime, length=int((original or {}).get("length") or 0),
                refers_to={
                    "crawl_id": (original or {}).get("crawl_id"),
                    "record_id": refers_id or None,
                    "url": h.get_header("WARC-Refers-To-Target-URI"),
                    "warc_date": h.get_header("WARC-Refers-To-Date")})
            if original is None:
                index.mark_orphaned(record_id)
                index.unresolved += 1


def _write_job_summary(job_dir: Path, summary: dict) -> None:
    import json
    path = Path(job_dir) / "dedup-summary.json"
    document = {**summary, "dedup_across_jobs": True, "rebuilt": True,
                "note": ("Rebuilt from the WARC files. A revisit record stands for a payload "
                         "already held: within this job, or by another job of the collection "
                         "(refers_to_jobs).")}
    try:
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def note_page(index: "CollectionIndex", crawl_id: Optional[int], url: str, date: str,
              status: Optional[int], mime: Optional[str], body: Optional[bytes],
              content_type: Optional[str], fingerprint: Optional[str] = None) -> Optional[str]:
    """Classify one captured page against the collection's last capture of
    it and record the outcome. Returns the change, or None when the
    response is not a page event (not HTML, a redirect, a server error)."""
    from . import changes

    # a 404/410 counts whatever it is served as; anything else must be HTML
    if status not in changes.GONE_STATUSES and not changes.is_page(status, mime):
        return None
    if fingerprint is None and status == 200:
        fingerprint = changes.page_fingerprint(body or b"", changes.charset_of(content_type))
    previous = index.last_page(url, before_crawl_id=crawl_id)
    change = changes.classify(previous, fingerprint, status)
    if change is None:                       # a 404 for a page never held: no event
        return None
    index.record_page(crawl_id=crawl_id, url=url, warc_date=date, status=status,
                      fingerprint=fingerprint, change=change,
                      previous_crawl_id=(previous or {}).get("crawl_id"))
    return change
