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
KIND_INSTAGRAM = "instagram"
KIND_X = "x"
KIND_YOUTUBE = "youtube"

# crawl lifecycle states
PENDING = "pending"
RUNNING = "running"
PAUSED = "paused"
STOPPING = "stopping"
COMPLETED = "completed"
STOPPED = "stopped"
FAILED = "failed"
BLOCKED = "blocked"
# created but not launched: the curator chose to wait for the machine to
# have room; the server starts it once the resource check passes
WAITING = "waiting"

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

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS instagram_targets (
    target_key        TEXT PRIMARY KEY,
    target_url        TEXT NOT NULL,
    username          TEXT,
    newest_media_id   TEXT,
    newest_post_date  TEXT,
    last_crawl_id     INTEGER,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instagram_posts (
    target_key     TEXT NOT NULL,
    media_id       TEXT NOT NULL,
    shortcode      TEXT,
    post_date      TEXT,
    first_crawl_id INTEGER NOT NULL,
    last_crawl_id  INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (target_key, media_id)
);

CREATE TABLE IF NOT EXISTS x_targets (
    target_key        TEXT PRIMARY KEY,
    target_url        TEXT NOT NULL,
    handle            TEXT,
    user_id           TEXT,
    newest_post_id    TEXT,
    newest_post_date  TEXT,
    last_crawl_id     INTEGER,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_targets (
    target_key        TEXT PRIMARY KEY,
    target_url        TEXT NOT NULL,
    handle            TEXT,
    channel_id        TEXT,
    newest_item_id    TEXT,
    newest_item_date  TEXT,
    last_crawl_id     INTEGER,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_items (
    target_key     TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    item_kind      TEXT,
    item_date      TEXT,
    first_crawl_id INTEGER NOT NULL,
    last_crawl_id  INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (target_key, item_id)
);

CREATE TABLE IF NOT EXISTS x_posts (
    target_key     TEXT NOT NULL,
    post_id        TEXT NOT NULL,
    post_date      TEXT,
    first_crawl_id INTEGER NOT NULL,
    last_crawl_id  INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (target_key, post_id)
);
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

    # -- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?",
                            (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, _now()))

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

    # -- instagram ----------------------------------------------------------
    def get_instagram_target(self, target_key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM instagram_targets WHERE target_key=?",
                            (target_key,)).fetchone()
            return dict(row) if row else None

    def get_instagram_media_ids(self, target_key: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT media_id FROM instagram_posts WHERE target_key=?",
                (target_key,)).fetchall()
            return {str(r["media_id"]) for r in rows}

    def record_instagram_capture(self, crawl_id: int, targets: dict,
                                 posts: list[dict],
                                 target_of_post: dict | None = None) -> None:
        """Remember what a capture reached, for "since last" next time.

        ``targets`` maps target key -> {media_id, date, username, url}; the
        newest kept is the newest non-pinned post the run selected.
        """
        ts = _now()
        with self._conn() as c:
            for key, newest in targets.items():
                c.execute(
                    "INSERT INTO instagram_targets (target_key, target_url, "
                    "username, newest_media_id, newest_post_date, "
                    "last_crawl_id, updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key) DO UPDATE SET "
                    "target_url=excluded.target_url, "
                    "username=COALESCE(excluded.username, username), "
                    "newest_media_id=CASE WHEN excluded.newest_post_date >= "
                    "COALESCE(newest_post_date, '') THEN excluded.newest_media_id "
                    "ELSE newest_media_id END, "
                    "newest_post_date=CASE WHEN excluded.newest_post_date >= "
                    "COALESCE(newest_post_date, '') THEN excluded.newest_post_date "
                    "ELSE newest_post_date END, "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "updated_at=excluded.updated_at",
                    (key, newest.get("url") or "", newest.get("username"),
                     newest.get("media_id"), newest.get("date"), crawl_id, ts))
            for post in posts:
                media_id = str(post.get("media_id") or "").strip()
                owner = str(post.get("owner_username") or "").strip()
                key = (target_of_post or {}).get(post.get("shortcode")) or (
                    f"instagram:@{owner}" if owner else None)
                if not media_id or not key:
                    continue
                c.execute(
                    "INSERT INTO instagram_posts (target_key, media_id, "
                    "shortcode, post_date, first_crawl_id, last_crawl_id, "
                    "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key, media_id) DO UPDATE SET "
                    "post_date=COALESCE(excluded.post_date, post_date), "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "last_seen_at=excluded.last_seen_at",
                    (key, media_id, post.get("shortcode"),
                     post.get("created_time"), crawl_id, crawl_id, ts, ts))

    # -- x -------------------------------------------------------------------
    def get_x_target(self, target_key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM x_targets WHERE target_key=?",
                            (target_key,)).fetchone()
            return dict(row) if row else None

    def get_x_post_ids(self, target_key: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT post_id FROM x_posts WHERE target_key=?",
                             (target_key,)).fetchall()
            return {str(r["post_id"]) for r in rows}

    def record_x_capture(self, crawl_id: int, targets: dict, posts: list[dict]) -> None:
        """Remember what an X capture reached, for "since last" next time.

        ``targets`` maps target key -> {post_id, date, handle, user_id, url};
        the newest kept is the newest non-pinned post the run selected, and
        post ids are time-ordered, so the larger id is the newer post.
        """
        ts = _now()
        with self._conn() as c:
            for key, newest in targets.items():
                c.execute(
                    "INSERT INTO x_targets (target_key, target_url, handle, user_id, "
                    "newest_post_id, newest_post_date, last_crawl_id, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key) DO UPDATE SET "
                    "target_url=excluded.target_url, "
                    "handle=COALESCE(excluded.handle, handle), "
                    "user_id=COALESCE(excluded.user_id, user_id), "
                    "newest_post_id=CASE WHEN CAST(excluded.newest_post_id AS INTEGER) >= "
                    "CAST(COALESCE(newest_post_id, '0') AS INTEGER) THEN excluded.newest_post_id "
                    "ELSE newest_post_id END, "
                    "newest_post_date=CASE WHEN CAST(excluded.newest_post_id AS INTEGER) >= "
                    "CAST(COALESCE(newest_post_id, '0') AS INTEGER) THEN excluded.newest_post_date "
                    "ELSE newest_post_date END, "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "updated_at=excluded.updated_at",
                    (key, newest.get("url") or "", newest.get("handle"),
                     newest.get("user_id"), newest.get("post_id"), newest.get("date"),
                     crawl_id, ts))
            for post in posts:
                post_id = str(post.get("post_id") or "").strip()
                key = post.get("target_key")
                if not post_id or not key or post.get("capture_role") != "target":
                    continue
                c.execute(
                    "INSERT INTO x_posts (target_key, post_id, post_date, "
                    "first_crawl_id, last_crawl_id, first_seen_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key, post_id) DO UPDATE SET "
                    "post_date=COALESCE(excluded.post_date, post_date), "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "last_seen_at=excluded.last_seen_at",
                    (key, post_id, post.get("created_time"), crawl_id, crawl_id, ts, ts))

    # -- youtube -------------------------------------------------------------
    def get_youtube_target(self, target_key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM youtube_targets WHERE target_key=?",
                            (target_key,)).fetchone()
            return dict(row) if row else None

    def get_youtube_item_ids(self, target_key: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT item_id FROM youtube_items WHERE target_key=?",
                             (target_key,)).fetchall()
            return {str(r["item_id"]) for r in rows}

    def record_youtube_capture(self, crawl_id: int, targets: dict, items: list[dict]) -> None:
        """Remember what a YouTube capture reached, for "since last" next time.

        ``targets`` maps target key -> {item_id, date, handle, channel_id, url};
        ``items`` are video and post rows carrying target_key.
        """
        ts = _now()
        with self._conn() as c:
            for key, newest in targets.items():
                c.execute(
                    "INSERT INTO youtube_targets (target_key, target_url, handle, channel_id, "
                    "newest_item_id, newest_item_date, last_crawl_id, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key) DO UPDATE SET "
                    "target_url=excluded.target_url, "
                    "handle=COALESCE(excluded.handle, handle), "
                    "channel_id=COALESCE(excluded.channel_id, channel_id), "
                    "newest_item_id=CASE WHEN COALESCE(excluded.newest_item_date, '') >= "
                    "COALESCE(newest_item_date, '') THEN excluded.newest_item_id "
                    "ELSE newest_item_id END, "
                    "newest_item_date=CASE WHEN COALESCE(excluded.newest_item_date, '') >= "
                    "COALESCE(newest_item_date, '') THEN excluded.newest_item_date "
                    "ELSE newest_item_date END, "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "updated_at=excluded.updated_at",
                    (key, newest.get("url") or "", newest.get("handle"), newest.get("channel_id"),
                     newest.get("item_id"), newest.get("date"), crawl_id, ts))
            for item in items:
                item_id = str(item.get("video_id") or item.get("post_id") or "").strip()
                key = item.get("target_key")
                if not item_id or not key:
                    continue
                c.execute(
                    "INSERT INTO youtube_items (target_key, item_id, item_kind, item_date, "
                    "first_crawl_id, last_crawl_id, first_seen_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key, item_id) DO UPDATE SET "
                    "item_date=COALESCE(excluded.item_date, item_date), "
                    "last_crawl_id=excluded.last_crawl_id, "
                    "last_seen_at=excluded.last_seen_at",
                    (key, item_id, "video" if item.get("video_id") else "post",
                     item.get("published_time"), crawl_id, crawl_id, ts, ts))

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
