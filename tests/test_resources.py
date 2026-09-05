"""A machine short of CPU, memory or disk is warned about before a job starts.

The reading is what the dashboard and the command line both show; the
warning levels are settings; a job told to wait is created but not launched
until the machine has room, or the curator says now.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from webarc import cli, resources
from webarc import server as srv
from webarc.store import PENDING, RUNNING, STOPPED, WAITING


def reading(cpu_free=80.0, memory_free=70.0, disk_free=50.0, path="/data") -> dict:
    return {
        "sampled_at": 0, "measured": True,
        "cpu": {"used_percent": 100 - cpu_free, "free_percent": cpu_free, "count": 4},
        "memory": {"total": 8 * 2**30, "available": int(8 * 2**30 * memory_free / 100),
                   "used_percent": 100 - memory_free, "free_percent": memory_free},
        "disk": {"path": path, "total": 100 * 2**30, "free": int(100 * 2**30 * disk_free / 100),
                 "used": 0, "free_percent": disk_free},
    }


class ThresholdTests(unittest.TestCase):
    def test_nothing_is_reported_when_everything_is_above_its_level(self):
        self.assertEqual(resources.evaluate(reading(), resources.DEFAULT_THRESHOLDS), [])

    def test_each_resource_below_its_level_is_named(self):
        low = reading(cpu_free=5, memory_free=10, disk_free=2)

        found = resources.evaluate(low, resources.DEFAULT_THRESHOLDS)

        self.assertEqual([w["resource"] for w in found], ["cpu", "memory", "disk"])
        self.assertIn("5% of CPU is free", found[0]["message"])
        self.assertIn("/data", found[2]["message"])

    def test_an_unmeasured_resource_raises_no_warning(self):
        unmeasured = reading()
        unmeasured["cpu"]["free_percent"] = None
        unmeasured["memory"]["free_percent"] = None

        self.assertEqual(resources.evaluate(unmeasured, resources.DEFAULT_THRESHOLDS), [])

    def test_warnings_can_be_switched_off(self):
        off = dict(resources.DEFAULT_THRESHOLDS, enabled=False)

        self.assertEqual(resources.evaluate(reading(cpu_free=1), off), [])

    def test_settings_that_do_not_parse_fall_back_to_the_defaults(self):
        held = {"resource_warn_cpu_free_percent": "lots",
                "resource_warn_disk_free_percent": "250",
                "resource_warn_memory_free_percent": "40",
                "resource_warn_enabled": "false"}

        seen = resources.thresholds_from_settings(held.get)

        self.assertEqual(seen["cpu_free_percent"], 15)
        self.assertEqual(seen["disk_free_percent"], 10)
        self.assertEqual(seen["memory_free_percent"], 40)
        self.assertFalse(seen["enabled"])

    def test_a_process_tree_is_measured(self):
        seen = resources.ProcessUsage().usage(os.getpid())

        self.assertIsNotNone(seen)
        self.assertGreater(seen["rss_bytes"], 0)
        self.assertGreaterEqual(seen["processes"], 1)

    def test_a_process_that_is_gone_reads_as_nothing(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()

        self.assertIsNone(resources.ProcessUsage().usage(child.pid))


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)
        self.machine = reading()
        srv._monitor()._snapshot_fn = lambda path: dict(self.machine)
        # launching is the server's call; the worker itself is not under test
        self.launched: list[int] = []
        patcher = mock.patch.object(srv, "_launch_worker",
                                    side_effect=lambda crawl_id: self.launched.append(crawl_id) or 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, **extra) -> dict:
        body = {"name": "example",
                "config": {"crawl_name": "example", "seeds": [{"url": "https://example.org/"}]}}
        body.update(extra)
        made = self.client.post("/api/crawls", json=body)
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()


class SettingsTests(ServerTestCase):
    def test_the_defaults_are_reported(self):
        seen = self.client.get("/api/settings").json()

        self.assertEqual(seen["resources"], resources.DEFAULT_THRESHOLDS)
        self.assertTrue(seen["resources_measured"])

    def test_levels_are_kept(self):
        saved = self.client.put("/api/settings", json={
            "resources": {"cpu_free_percent": 30, "disk_free_percent": 5, "enabled": False}})

        self.assertEqual(saved.status_code, 200, saved.text)
        seen = self.client.get("/api/settings").json()["resources"]
        self.assertEqual(seen["cpu_free_percent"], 30)
        self.assertEqual(seen["disk_free_percent"], 5)
        self.assertEqual(seen["memory_free_percent"], 15)
        self.assertFalse(seen["enabled"])

    def test_a_level_outside_the_percent_range_is_refused(self):
        refused = self.client.put("/api/settings", json={"resources": {"cpu_free_percent": 140}})

        self.assertEqual(refused.status_code, 400)
        self.assertIn("between 0 and 100", refused.json()["detail"])

    def test_changing_levels_leaves_the_storage_default_alone(self):
        self.client.put("/api/settings", json={"storage_root": str(self.tmp / "kept")})

        self.client.put("/api/settings", json={"resources": {"cpu_free_percent": 30}})

        self.assertEqual(Path(self.client.get("/api/settings").json()["storage_root"]),
                         (self.tmp / "kept").resolve())


class CheckTests(ServerTestCase):
    def test_a_machine_with_room_passes(self):
        seen = self.client.get("/api/resources/check").json()

        self.assertTrue(seen["ok"])
        self.assertEqual(seen["warnings"], [])

    def test_a_shortage_is_named_not_refused(self):
        self.machine = reading(memory_free=8)

        seen = self.client.get("/api/resources/check").json()

        self.assertFalse(seen["ok"])
        self.assertEqual([w["resource"] for w in seen["warnings"]], ["memory"])
        self.assertEqual(seen["thresholds"]["memory_free_percent"], 15)

    def test_the_check_looks_at_the_disk_the_job_would_write_to(self):
        elsewhere = self.tmp / "elsewhere"
        seen = self.client.get("/api/resources/check",
                               params={"storage_dir": str(elsewhere)}).json()

        self.assertEqual(Path(seen["snapshot"]["disk"]["path"]), elsewhere.resolve())

    def test_the_reading_lists_running_jobs(self):
        made = self.create()
        srv._store().set_status(made["id"], RUNNING)
        srv._store().set_pid(made["id"], os.getpid())
        with mock.patch.object(srv, "_pid_is_worker", return_value=True):
            seen = self.client.get("/api/resources").json()
            row = self.client.get(f"/api/crawls/{made['id']}").json()

        self.assertEqual([j["id"] for j in seen["jobs"]], [made["id"]])
        self.assertGreater(seen["jobs_total"]["rss_bytes"], 0)
        self.assertGreater(row["resources"]["rss_bytes"], 0)

    def test_a_job_without_a_worker_reports_no_usage(self):
        made = self.create()

        self.assertIsNone(self.client.get(f"/api/crawls/{made['id']}").json()["resources"])


class WaitingJobTests(ServerTestCase):
    def test_a_job_told_to_wait_is_created_but_not_launched(self):
        made = self.create(start="wait")

        self.assertEqual(made["status"], WAITING)
        self.assertIsNone(made["pid"])
        self.assertEqual(self.launched, [])
        self.assertTrue(Path(made["output_dir"]).is_dir())

    def test_it_stays_waiting_while_the_machine_is_short(self):
        made = self.create(start="wait")
        self.machine = reading(cpu_free=3)      # disk is read afresh for the job's own directory

        srv._monitor().tick()

        self.assertEqual(self.launched, [])
        self.assertEqual(self.client.get(f"/api/crawls/{made['id']}").json()["status"], WAITING)

    def test_it_is_launched_once_the_machine_has_room(self):
        made = self.create(start="wait")

        srv._monitor().tick()

        self.assertEqual(self.launched, [made["id"]])
        self.assertEqual(self.client.get(f"/api/crawls/{made['id']}").json()["status"], PENDING)

    def test_one_waiting_job_starts_per_check_oldest_first(self):
        first = self.create(start="wait")
        second = self.create(start="wait")

        srv._monitor().tick()

        self.assertEqual(self.launched, [first["id"]])
        srv._monitor().tick()
        self.assertEqual(self.launched, [first["id"], second["id"]])

    def test_a_launched_job_is_not_launched_again(self):
        self.create(start="wait")
        srv._monitor().tick()

        srv._monitor().tick()

        self.assertEqual(len(self.launched), 1)

    def test_the_curator_can_start_it_now(self):
        made = self.create(start="wait")
        self.machine = reading(cpu_free=2)

        started = self.client.post(f"/api/crawls/{made['id']}/start")

        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(self.launched, [made["id"]])

    def test_only_a_waiting_job_can_be_started_that_way(self):
        made = self.create()

        self.assertEqual(self.client.post(f"/api/crawls/{made['id']}/start").status_code, 409)

    def test_stopping_a_waiting_job_cancels_it(self):
        made = self.create(start="wait")

        self.client.post(f"/api/crawls/{made['id']}/stop")

        row = self.client.get(f"/api/crawls/{made['id']}").json()
        self.assertEqual(row["status"], STOPPED)
        self.assertIn("Cancelled", row["error"])
        self.assertEqual(self.client.delete(f"/api/crawls/{made['id']}").status_code, 200)

    def test_the_other_job_kinds_can_wait_too(self):
        made = self.client.post("/api/recordings", json={
            "url": "https://example.org/", "start": "wait"})

        self.assertEqual(made.status_code, 201, made.text)
        self.assertEqual(made.json()["status"], WAITING)
        self.assertEqual(self.launched, [])

    def test_an_unknown_start_choice_is_refused(self):
        refused = self.client.post("/api/crawls", json={
            "start": "later", "config": {"seeds": [{"url": "https://example.org/"}]}})

        self.assertEqual(refused.status_code, 400)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.machine = reading(path=str(self.tmp))
        patcher = mock.patch.object(resources, "system_snapshot",
                                    side_effect=lambda path, cpu_interval=None: dict(self.machine))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_resources_command_reports_the_machine(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["resources", "--db", str(self.tmp / "none.db"),
                             "--warc-root", str(self.tmp)])

        self.assertEqual(code, 0)
        self.assertIn("CPU     : 80% free of 4 cores", out.getvalue())
        self.assertIn("No jobs are running.", out.getvalue())

    def test_the_resources_command_uses_the_dashboards_levels(self):
        from webarc.store import Store
        db = self.tmp / "swm.db"
        Store(db).set_setting("resource_warn_cpu_free_percent", "90")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["resources", "--db", str(db), "--warc-root", str(self.tmp)])

        self.assertIn("warn when less than 90% CPU", out.getvalue())
        self.assertIn("WARNING: 80% of CPU is free", out.getvalue())

    def test_a_crawl_starts_without_a_word_when_there_is_room(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertTrue(cli._resource_gate(self.tmp, str(self.tmp / "none.db")))

        self.assertEqual(err.getvalue(), "")

    def test_the_curator_is_asked_and_may_cancel(self):
        self.machine = reading(disk_free=1)
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(cli.sys.stdin, "isatty", return_value=True):
            go = cli._resource_gate(self.tmp, str(self.tmp / "none.db"), ask=lambda prompt: "c")

        self.assertFalse(go)
        self.assertIn("WARNING:", err.getvalue())
        self.assertIn("Cancelled.", err.getvalue())

    def test_the_curator_may_start_anyway(self):
        self.machine = reading(disk_free=1)
        with redirect_stderr(io.StringIO()), mock.patch.object(cli.sys.stdin, "isatty", return_value=True):
            go = cli._resource_gate(self.tmp, str(self.tmp / "none.db"), ask=lambda prompt: "s")

        self.assertTrue(go)

    def test_the_curator_may_wait_for_room(self):
        self.machine = reading(memory_free=1)
        readings = iter([reading(memory_free=1), reading()])
        with mock.patch.object(resources, "system_snapshot",
                               side_effect=lambda path, cpu_interval=None: next(readings)), \
                redirect_stderr(io.StringIO()):
            go = cli._resource_gate(self.tmp, str(self.tmp / "none.db"), wait=True,
                                    poll_seconds=0)

        self.assertTrue(go)

    def test_yes_starts_without_asking(self):
        self.machine = reading(cpu_free=1)
        with redirect_stderr(io.StringIO()):
            go = cli._resource_gate(self.tmp, str(self.tmp / "none.db"), assume_yes=True,
                                    ask=lambda prompt: self.fail("asked"))

        self.assertTrue(go)

    def test_an_unattended_crawl_starts_with_the_warning_printed(self):
        self.machine = reading(cpu_free=1)
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            go = cli._resource_gate(self.tmp, str(self.tmp / "none.db"),
                                    ask=lambda prompt: self.fail("asked"))

        self.assertTrue(go)
        self.assertIn("Not a terminal", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class WithoutPsutilTests(unittest.TestCase):
    """A machine without psutil still reads its own CPU and memory."""

    def setUp(self):
        patcher = mock.patch.object(resources, "psutil", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        resources._last_cpu_times = None

    @unittest.skipUnless(cli.sys.platform.startswith("linux"), "reads /proc")
    def test_linux_reads_proc(self):
        first = resources.system_snapshot(".")
        second = resources.system_snapshot(".", cpu_interval=0.2)

        self.assertTrue(first["measured"])
        self.assertIsNone(first["cpu"]["free_percent"])     # nothing to compare with yet
        self.assertIsNotNone(second["cpu"]["free_percent"])
        self.assertGreater(second["memory"]["free_percent"], 0)
        self.assertIn("psutil is not installed", second["note"])

    def test_windows_reads_kernel32(self):
        import ctypes

        class FakeKernel32:
            calls = 0

            def GetSystemTimes(self, idle, kernel, user):
                FakeKernel32.calls += 1
                tick = FakeKernel32.calls * 1000
                idle._obj.dwLowDateTime = tick // 2          # half idle
                kernel._obj.dwLowDateTime = tick               # kernel includes idle
                user._obj.dwLowDateTime = tick
                return 1

            def GlobalMemoryStatusEx(self, status):
                status._obj.ullTotalPhys = 8 * 2**30
                status._obj.ullAvailPhys = 2 * 2**30
                return 1

        windll = mock.Mock(kernel32=FakeKernel32())
        with mock.patch.object(resources.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", windll, create=True):
            first = resources.system_snapshot(".")
            second = resources.system_snapshot(".")

        self.assertIsNone(first["cpu"]["free_percent"])
        self.assertAlmostEqual(second["cpu"]["used_percent"], 75.0)   # (2t - t/2) / 2t
        self.assertAlmostEqual(second["memory"]["free_percent"], 25.0)
        self.assertTrue(second["measured"])

    def test_a_job_reports_no_usage_and_the_reading_says_why(self):
        self.assertIsNone(resources.ProcessUsage().usage(os.getpid()))
        self.assertIn("pip install -r requirements.txt", resources.measurement_note())


class HelpEndpointTests(ServerTestCase):
    def test_the_help_text_is_served_with_a_local_override_on_top(self):
        from webarc.help import OVERRIDE_NAME
        (self.tmp / OVERRIDE_NAME).write_text("f-max-depth: Ours.\n", encoding="utf-8")

        seen = self.client.get("/api/help").json()

        self.assertEqual(seen["f-max-depth"], "Ours.")
        self.assertIn("f-operator", seen)


class ServerLogNoiseTests(unittest.TestCase):
    """A browser hanging up is not an error the dashboard's log should shout."""

    def test_a_dropped_connection_is_not_logged_as_an_error(self):
        import logging
        from webarc.cli import _DroppedConnectionFilter
        keep = _DroppedConnectionFilter()

        def record(msg):
            return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, msg, (), None)

        self.assertFalse(keep.filter(record(
            "Exception in callback _ProactorBasePipeTransport._call_connection_lost()")))
        self.assertFalse(keep.filter(record("socket.send() raised exception.")))
        self.assertTrue(keep.filter(record("Task was destroyed but it is pending!")))
