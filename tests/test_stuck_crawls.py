"""Crawls whose worker is gone must not stay stuck.

A stop marks a crawl "stopping" and waits for the worker to report; a
worker that died, or was lost when the server restarted, never reports,
and the crawl stayed stopping -- unstoppable, undeletable -- with a pid
that may by then belong to any process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from webarc import server as srv
from webarc.store import FAILED, RUNNING, STOPPED, STOPPING


class StuckCrawlTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "swm.db")
        self.app = srv.create_app(self.db, str(self.tmp / "warcs"), simulate=True,
                                  replay_root=str(self.tmp / "replay"))
        self.client = TestClient(self.app)

    def crawl(self) -> int:
        made = self.client.post("/api/crawls", json={
            "name": "example",
            "config": {"crawl_name": "example", "operator": "webarc",
                       "seeds": [{"url": "https://example.org/"}]}})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()["id"]

    def leave(self, crawl_id: int, status: str, pid: int, minutes_ago: float = 5) -> None:
        """Put a crawl in the state a lost worker leaves behind."""
        store = srv._store()
        store.set_status(crawl_id, status)
        store.set_pid(crawl_id, pid)
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(
            timespec="seconds")
        with store._conn() as c:
            c.execute("UPDATE crawls SET updated_at=? WHERE id=?", (stamp, crawl_id))

    @staticmethod
    def dead_pid() -> int:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid


class StuckCrawlTests(StuckCrawlTestCase):
    def test_a_stopping_crawl_with_no_worker_settles_as_stopped(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, STOPPING, self.dead_pid())

        seen = self.client.get(f"/api/crawls/{crawl_id}").json()

        self.assertEqual(seen["status"], STOPPED)
        self.assertIn("not running", seen["error"])
        self.assertEqual(seen["control"], "none")

    def test_a_running_crawl_with_no_worker_settles_as_failed(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, RUNNING, self.dead_pid())

        seen = self.client.get(f"/api/crawls/{crawl_id}").json()

        self.assertEqual(seen["status"], FAILED)

    def test_a_worker_that_has_only_just_gone_quiet_is_given_a_minute(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, RUNNING, self.dead_pid(), minutes_ago=0)

        self.assertEqual(self.client.get(f"/api/crawls/{crawl_id}").json()["status"], RUNNING)

    def test_a_settled_crawl_can_be_deleted(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, STOPPING, self.dead_pid())
        self.client.get(f"/api/crawls/{crawl_id}")

        self.assertEqual(self.client.delete(f"/api/crawls/{crawl_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/crawls/{crawl_id}").status_code, 404)

    def test_stop_on_a_crawl_with_no_worker_settles_it_at_once(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, RUNNING, self.dead_pid(), minutes_ago=0)

        answer = self.client.post(f"/api/crawls/{crawl_id}/stop").json()

        self.assertTrue(answer["settled"])
        self.assertEqual(self.client.get(f"/api/crawls/{crawl_id}").json()["status"], STOPPED)

    def test_a_pid_reused_by_another_process_is_not_taken_for_the_worker(self):
        """After a restart the recorded pid can belong to anything; only a
        process that is one of ours counts as the worker."""
        crawl_id = self.crawl()
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(stranger.kill)
        self.leave(crawl_id, STOPPING, stranger.pid)

        seen = self.client.get(f"/api/crawls/{crawl_id}").json()

        if os.name == "posix" and Path("/proc").exists():
            self.assertEqual(seen["status"], STOPPED)       # not ours: settled
            self.assertEqual(self.client.delete(f"/api/crawls/{crawl_id}").status_code, 200)
        else:                                                 # pragma: no cover
            self.assertIn(seen["status"], (STOPPING, STOPPED))

    def test_a_live_worker_blocks_delete_unless_forced(self):
        crawl_id = self.crawl()
        worker = subprocess.Popen([sys.executable, "-c",
                                   "import webarc, time; time.sleep(30)"],
                                  **({"start_new_session": True} if os.name == "posix" else {}))
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        self.leave(crawl_id, RUNNING, worker.pid, minutes_ago=0)

        refused = self.client.delete(f"/api/crawls/{crawl_id}")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("force", refused.text)

        forced = self.client.delete(f"/api/crawls/{crawl_id}?force=true")
        self.assertEqual(forced.status_code, 200)
        worker.wait(timeout=10)
        self.assertIsNotNone(worker.poll())                   # ended

    def test_the_server_settles_stuck_crawls_when_it_starts(self):
        crawl_id = self.crawl()
        self.leave(crawl_id, STOPPING, self.dead_pid())

        srv.create_app(self.db, str(self.tmp / "warcs"), simulate=True,
                       replay_root=str(self.tmp / "replay"))

        self.assertEqual(srv._store().get_crawl(crawl_id)["status"], STOPPED)


if __name__ == "__main__":
    unittest.main()
