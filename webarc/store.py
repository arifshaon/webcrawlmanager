"""SQLite state store shared between the API server and crawl worker processes.

The store is the single source of truth for crawl definitions, live progress,
and control commands (pause/resume/stop). The API writes control commands and
reads progress; workers read control commands and write progress. SQLite's
WAL mode makes this concurrent access safe across processes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# job kinds
KIND_CRAWL = "crawl"
KIND_RECORDING = "recording"
KIND_FACEBOOK = "facebook"

# crawl lifecycle states
PENDING = "pending"
RUNNING = "running"
PAUSED = "paused"
STOPPING = "stopping"
COMPLETED = "completed"
STOPPED = "stopped"
FAILED = "failed"
BLOCKED = "blocked"

# control commands the API can set; the worker polls and acts on these
CTRL_NONE = "none"
CTRL_PAUSE = "pause"
CTRL_RESUME = "resume"
CTRL_STOP = "stop"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'crawl',
    config_json  TEXT NOT NULL,
    output_dir   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    control      TEXT NOT NULL DEFAULT 'none',
    pid          INTEGER,
    seeds_total  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS progress (
    crawl_id       INTEGER NOT NULL,
    seed_idx       INTEGER NOT NULL,
    seed_url       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    visited        INTEGER NOT NULL DEFAULT 0,
    queued         INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    skipped_robots INTEGER NOT NULL DEFAULT 0,
    bytes          INTEGER NOT NULL DEFAULT 0,
    current_url    TEXT,
    details_json   TEXT NOT NULL DEFAULT '{}',
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (crawl_id, seed_idx)
);

CREATE TABLE IF NOT EXISTS facebook_pages (
    page_key          TEXT PRIMARY KEY,
    page_url          TEXT NOT NULL,
    page_name         TEXT,
    newest_post_id    TEXT,
    newest_post_date  TEXT,
    oldest_post_id    TEXT,
    oldest_post_date  TEXT,
    last_crawl_id     INTEGER,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facebook_posts (
    page_key       TEXT NOT NULL,
    post_id        TEXT NOT NULL,
    post_date      TEXT,
    first_crawl_id INTEGER NOT NULL,
    last_crawl_id  INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (page_key, post_id)
);

CREATE INDEX IF NOT EXISTS idx_facebook_posts_page_date
    ON facebook_posts (page_key, post_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # migrate databases created before the kind column existed;
            # CREATE TABLE IF NOT EXISTS does not update existing tables
            cols = {r["name"] for r in c.execute("PRAGMA table_info(crawls)")}
            if "kind" not in cols:
                c.execute("ALTER TABLE crawls ADD COLUMN kind TEXT NOT NULL "
                          "DEFAULT 'crawl'")
            progress_cols = {
                r["name"] for r in c.execute("PRAGMA table_info(progress)")
            }
            if "details_json" not in progress_cols:
                c.execute("ALTER TABLE progress ADD COLUMN details_json TEXT "
                          "NOT NULL DEFAULT '{}'")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- crawl lifecycle -----------------------------------------------------
    def create_crawl(self, name: str, config: dict, output_dir: str,
                     seeds_total: int, kind: str = KIND_CRAWL) -> int:
        ts = _now()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO crawls (name, kind, config_json, output_dir, "
                "status, control, seeds_total, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, kind, json.dumps(config), output_dir, PENDING,
                 CTRL_NONE, seeds_total, ts, ts),
            )
            crawl_id = cur.lastrowid
            for idx, seed in enumerate(config.get("seeds", []), start=1):
                c.execute(
                    "INSERT INTO progress (crawl_id, seed_idx, seed_url, "
                    "updated_at) VALUES (?,?,?,?)",
                    (crawl_id, idx, seed["url"], ts),
                )
        return crawl_id

    def finalize_config(self, crawl_id: int, config: dict,
                        output_dir: str) -> None:
        """Set the resolved config + per-crawl output dir once the id is known."""
        with self._conn() as c:
            c.execute(
                "UPDATE crawls SET config_json=?, output_dir=?, updated_at=? "
                "WHERE id=?",
                (json.dumps(config), output_dir, _now(), crawl_id))

    def get_crawl(self, crawl_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
            return dict(row) if row else None

    def list_crawls(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM crawls ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def get_progress(self, crawl_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM progress WHERE crawl_id=? ORDER BY seed_idx",
                (crawl_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["details"] = json.loads(
                        item.pop("details_json", "{}") or "{}")
                except (TypeError, json.JSONDecodeError):
                    item["details"] = {}
                    item.pop("details_json", None)
                result.append(item)
            return result

    def set_status(self, crawl_id: int, status: str,
                   error: str | None = None) -> None:
        with self._conn() as c:
            if error is not None:
                c.execute("UPDATE crawls SET status=?, error=?, updated_at=? "
                          "WHERE id=?", (status, error, _now(), crawl_id))
            else:
                c.execute("UPDATE crawls SET status=?, updated_at=? WHERE id=?",
                          (status, _now(), crawl_id))

    def set_pid(self, crawl_id: int, pid: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE crawls SET pid=?, updated_at=? WHERE id=?",
                      (pid, _now(), crawl_id))

    # -- control commands ----------------------------------------------------
    def set_control(self, crawl_id: int, command: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE crawls SET control=?, updated_at=? WHERE id=?",
                      (command, _now(), crawl_id))

    def get_control(self, crawl_id: int) -> str:
        with self._conn() as c:
            row = c.execute("SELECT control FROM crawls WHERE id=?",
                            (crawl_id,)).fetchone()
            return row["control"] if row else CTRL_NONE

    def clear_control(self, crawl_id: int) -> None:
        self.set_control(crawl_id, CTRL_NONE)

    # -- progress reporting --------------------------------------------------
    def update_progress(self, crawl_id: int, seed_idx: int, *,
                        status: str | None = None, visited: int | None = None,
                        queued: int | None = None, failed: int | None = None,
                        skipped_robots: int | None = None,
                        bytes_written: int | None = None,
                        current_url: str | None = None,
                        details: dict | None = None) -> None:
        sets: list[str] = ["updated_at=?"]
        vals: list[Any] = [_now()]
        for col, val in (("status", status), ("visited", visited),
                         ("queued", queued), ("failed", failed),
                         ("skipped_robots", skipped_robots),
                         ("bytes", bytes_written), ("current_url", current_url)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        if details is not None:
            sets.append("details_json=?")
            vals.append(json.dumps(details, ensure_ascii=False))
        vals.extend([crawl_id, seed_idx])
        with self._conn() as c:
            c.execute(f"UPDATE progress SET {', '.join(sets)} "
                      f"WHERE crawl_id=? AND seed_idx=?", vals)

    # -- Facebook Page collection state -----------------------------------
    def get_facebook_page(self, page_key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM facebook_pages WHERE page_key=?", (page_key,)
            ).fetchone()
            return dict(row) if row else None

    def get_facebook_post_ids(self, page_key: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT post_id FROM facebook_posts WHERE page_key=?",
                (page_key,),
            ).fetchall()
            return {str(row["post_id"]) for row in rows}

    def record_facebook_posts(self, page_key: str, page_url: str,
                              page_name: str | None, crawl_id: int,
                              posts: list[dict]) -> None:
        """Persist observed post identities for incremental and resumed runs.

        This index describes what SWM observed in the raw capture, including
        posts outside a requested normalised-export date range. The run
        manifest makes that distinction explicit.
        """
        ts = _now()
        with self._conn() as c:
            for post in posts:
                post_id = str(post.get("post_id") or "").strip()
                if not post_id:
                    continue
                post_date = post.get("created_time") or None
                c.execute(
                    "INSERT INTO facebook_posts "
                    "(page_key, post_id, post_date, first_crawl_id, "
                    "last_crawl_id, first_seen_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(page_key, post_id) DO UPDATE SET "
                    "post_date=COALESCE(excluded.post_date, post_date), "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "last_seen_at=excluded.last_seen_at",
                    (page_key, post_id, post_date, crawl_id, crawl_id, ts, ts),
                )

            newest = c.execute(
                "SELECT post_id, post_date FROM facebook_posts "
                "WHERE page_key=? AND post_date IS NOT NULL "
                "ORDER BY post_date DESC LIMIT 1", (page_key,)
            ).fetchone()
            oldest = c.execute(
                "SELECT post_id, post_date FROM facebook_posts "
                "WHERE page_key=? AND post_date IS NOT NULL "
                "ORDER BY post_date ASC LIMIT 1", (page_key,)
            ).fetchone()
            c.execute(
                "INSERT INTO facebook_pages "
                "(page_key, page_url, page_name, newest_post_id, "
                "newest_post_date, oldest_post_id, oldest_post_date, "
                "last_crawl_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(page_key) DO UPDATE SET "
                "page_url=excluded.page_url, "
                "page_name=COALESCE(excluded.page_name, page_name), "
                "newest_post_id=COALESCE(excluded.newest_post_id, "
                "newest_post_id), "
                "newest_post_date=COALESCE(excluded.newest_post_date, "
                "newest_post_date), "
                "oldest_post_id=COALESCE(excluded.oldest_post_id, "
                "oldest_post_id), "
                "oldest_post_date=COALESCE(excluded.oldest_post_date, "
                "oldest_post_date), "
                "last_crawl_id=excluded.last_crawl_id, "
                "updated_at=excluded.updated_at",
                (
                    page_key, page_url, page_name,
                    newest["post_id"] if newest else None,
                    newest["post_date"] if newest else None,
                    oldest["post_id"] if oldest else None,
                    oldest["post_date"] if oldest else None,
                    crawl_id, ts,
                ),
            )

    def delete_crawl(self, crawl_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM progress WHERE crawl_id=?", (crawl_id,))
            c.execute("DELETE FROM crawls WHERE id=?", (crawl_id,))


def wait_for_db(path: str | Path, tries: int = 50) -> None:
    """Poll until a worker-visible DB file exists (startup race guard)."""
    for _ in range(tries):
        if Path(path).exists():
            return
        time.sleep(0.1)
