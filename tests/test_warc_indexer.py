"""Running the warc-indexer jar over a job's WARC files.

The jar itself is a separate Java program that is not built in the test
environment; a small Python stand-in takes its place through the command
override, so what is tested is everything around it: finding it, the
command line, the manifest, the endpoints and the button.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from webarc import cli, warc_indexer
from webarc import server as srv

FAKE = textwrap.dedent('''
    import sys, os
    # A stand-in for the jar: writes <warc>.jsonl beside each WARC named on
    # the command line, two documents each, and echoes its arguments.
    args = sys.argv[1:]
    out = args[args.index("-o") + 1]
    coll = args[args.index("--collection") + 1] if "--collection" in args else None
    warcs = [a for a in args if a.endswith(".warc.gz") or a.endswith(".warc")]
    if os.environ.get("FAKE_INDEXER_FAIL"):
        print("boom", file=sys.stderr); sys.exit(3)
    for w in warcs:
        name = os.path.basename(w)
        with open(os.path.join(out, name + ".jsonl"), "w", encoding="utf-8") as f:
            for i in range(2):
                f.write('{"id": "%s/%d", "source_file": "%s", "collection": %s}\\n'
                        % (name, i, name, ('"%s"' % coll) if coll else "null"))
    print("fake indexer done:", " ".join(args))
''')


class FakeJarTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = self.tmp / "fake_indexer.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {
            warc_indexer.CMD_ENV: json.dumps([sys.executable, str(self.fake)]),
            warc_indexer.CONF_ENV: "",
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def job_dir(self, name="job", warcs=("a-00001.warc.gz", "a-00002.warc.gz")) -> Path:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        for w in warcs:
            (d / w).write_bytes(b"\x1f\x8b" + b"x" * 10)
        return d


class FindingTests(unittest.TestCase):
    def test_without_java_or_jar_the_capability_says_what_is_missing(self):
        with mock.patch.dict(os.environ, {warc_indexer.CMD_ENV: "", warc_indexer.JAR_ENV: "/nowhere/x.jar",
                                          warc_indexer.JAVA_ENV: "", "JAVA_HOME": ""}), \
                mock.patch("webarc.warc_indexer.shutil.which", return_value=None):
            cap = warc_indexer.capability()
        self.assertFalse(cap["available"])
        self.assertIn("Java was not found", cap["reason"])
        self.assertIn("warc-indexer jar was not found", cap["reason"])
        with self.assertRaises(warc_indexer.WarcIndexerUnavailable):
            with mock.patch.dict(os.environ, {warc_indexer.CMD_ENV: "", warc_indexer.JAR_ENV: "/nowhere/x.jar",
                                              warc_indexer.JAVA_ENV: "", "JAVA_HOME": ""}), \
                    mock.patch("webarc.warc_indexer.shutil.which", return_value=None):
                warc_indexer.build_command(Path("."), [Path("x.warc.gz")])

    def test_the_command_names_the_jar_config_output_and_warcs(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "target" / "warc-indexer-3.5.1-jar-with-dependencies.jar"
            jar.parent.mkdir()
            jar.write_bytes(b"PK")
            conf = Path(tmp) / "config" / "swm-indexer.conf"
            conf.parent.mkdir()
            conf.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {warc_indexer.CMD_ENV: "", warc_indexer.JAR_ENV: str(jar),
                                              warc_indexer.CONF_ENV: ""}):
                self.assertEqual(warc_indexer.find_config(jar), conf)
                cmd = warc_indexer.build_command(Path("/out"), [Path("/w/a.warc.gz")], collection="C",
                                                 java="/usr/bin/java", jar=jar, memory="3g")
        self.assertEqual(cmd, ["/usr/bin/java", "-Xmx3g", "-jar", str(jar), "-c", str(conf),
                               "-o", "/out", "-F", "jsonl", "--collection", "C", "/w/a.warc.gz"])


class RunTests(FakeJarTestCase):
    def test_each_warc_gets_its_index_beside_it_and_the_run_is_recorded(self):
        d = self.job_dir()

        manifest = warc_indexer.index_warcs(d, collection="Demo")

        self.assertEqual(manifest["status"], "done")
        self.assertEqual(manifest["documents"], 4)
        self.assertEqual([o["warc"] for o in manifest["outputs"]], ["a-00001.warc.gz", "a-00002.warc.gz"])
        for w in ("a-00001.warc.gz", "a-00002.warc.gz"):
            index = d / (w + ".jsonl")
            self.assertTrue(index.is_file())
            self.assertEqual(json.loads(index.read_text().splitlines()[0])["collection"], "Demo")
        self.assertTrue((d / warc_indexer.LOG_NAME).is_file())
        self.assertIn("fake indexer done", (d / warc_indexer.LOG_NAME).read_text())
        stored = warc_indexer.read_manifest(d)
        self.assertEqual(stored["status"], "done")
        self.assertNotIn("pid", stored)
        self.assertEqual(warc_indexer.summary(d)["documents"], 4)

    def test_one_named_warc_can_be_indexed_alone(self):
        d = self.job_dir()
        manifest = warc_indexer.index_warcs(d, warcs=["a-00002.warc.gz"])
        self.assertEqual(manifest["warcs"], ["a-00002.warc.gz"])
        self.assertTrue((d / "a-00002.warc.gz.jsonl").is_file())
        self.assertFalse((d / "a-00001.warc.gz.jsonl").exists())
        with self.assertRaises(warc_indexer.WarcIndexerUnavailable):
            warc_indexer.index_warcs(d, warcs=["missing.warc.gz"])

    def test_a_failing_run_is_recorded_as_failed_with_the_log(self):
        d = self.job_dir()
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_FAIL": "1"}):
            manifest = warc_indexer.index_warcs(d)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("exited with code 3", manifest["error"])
        self.assertIn("boom", (d / warc_indexer.LOG_NAME).read_text())
        self.assertEqual(warc_indexer.summary(d)["status"], "failed")

    def test_a_folder_without_warcs_is_refused(self):
        d = self.job_dir(warcs=())
        with self.assertRaises(warc_indexer.WarcIndexerUnavailable):
            warc_indexer.index_warcs(d)


class CliTests(FakeJarTestCase):
    def test_index_warc_prints_the_outputs(self):
        d = self.job_dir()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["index-warc", str(d), "--collection", "Demo"])
        self.assertEqual(code, 0)
        self.assertIn("Indexed 4 document(s) from 2 WARC file(s)", out.getvalue())
        self.assertIn("a-00001.warc.gz.jsonl", out.getvalue())

    def test_index_warc_json_and_failures(self):
        d = self.job_dir()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["index-warc", str(d), "--warc", "a-00001.warc.gz", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["warcs"], ["a-00001.warc.gz"])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(cli.main(["index-warc", str(self.tmp / "nope")]), 1)
        self.assertIn("Cannot index", err.getvalue())


class ServerTests(FakeJarTestCase):
    def setUp(self):
        super().setUp()
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)

    def crawl(self) -> dict:
        made = self.client.post("/api/crawls", json={
            "name": "demo", "start": "wait",
            "config": {"seeds": [{"url": "https://a.example/"}]}})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def wait_done(self, crawl_id: int) -> dict:
        for _ in range(100):
            view = self.client.get(f"/api/crawls/{crawl_id}").json()
            if view["warc_index"] and view["warc_index"]["status"] != "running":
                return view
            time.sleep(0.05)
        self.fail("the indexing run did not finish")

    def test_the_capability_is_reported(self):
        caps = self.client.get("/api/capabilities").json()
        self.assertTrue(caps["warc_indexer"]["available"])

    def test_indexing_a_crawls_warcs_from_the_dashboard(self):
        made = self.crawl()
        out_dir = Path(made["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "demo-seed001-20260901120000-00001.warc.gz").write_bytes(b"\x1f\x8bxx")
        self.assertIsNone(made["warc_index"])

        started = self.client.post(f"/api/crawls/{made['id']}/warc-index")

        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["status"], "running")
        self.assertEqual(started.json()["collection"], "demo")
        view = self.wait_done(made["id"])
        self.assertEqual(view["warc_index"]["status"], "done")
        self.assertEqual(view["warc_index"]["documents"], 2)
        self.assertTrue((out_dir / "demo-seed001-20260901120000-00001.warc.gz.jsonl").is_file())
        status = self.client.get(f"/api/crawls/{made['id']}/warc-index")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["summary"]["documents"], 2)
        self.assertNotIn("pid", status.json())

    def test_what_cannot_be_indexed_this_way(self):
        made = self.crawl()                             # no WARC yet
        self.assertEqual(self.client.post(f"/api/crawls/{made['id']}/warc-index").status_code, 409)
        self.assertEqual(self.client.get(f"/api/crawls/{made['id']}/warc-index").status_code, 404)
        out_dir = Path(made["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "x.warc.gz").write_bytes(b"\x1f\x8bxx")
        wrong = self.client.post(f"/api/crawls/{made['id']}/warc-index", json={"warc": "other.warc.gz"})
        self.assertEqual(wrong.status_code, 404)

        social = self.client.post("/api/facebook", json={
            "page_url": "https://www.facebook.com/Some.Page", "mode": "latest_n", "latest_n": 5,
            "start": "wait"})
        self.assertEqual(social.status_code, 201, social.text)
        refused = self.client.post(f"/api/crawls/{social.json()['id']}/warc-index")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("social captures are indexed from their records", refused.json()["detail"])

        with mock.patch.dict(os.environ, {warc_indexer.CMD_ENV: "", warc_indexer.JAR_ENV: "/nowhere.jar",
                                          warc_indexer.JAVA_ENV: "", "JAVA_HOME": ""}), \
                mock.patch("webarc.warc_indexer.shutil.which", return_value=None):
            missing = self.client.post(f"/api/crawls/{made['id']}/warc-index")
            self.assertEqual(missing.status_code, 409)
            self.assertIn("jar was not found", missing.json()["detail"])
            self.assertFalse(self.client.get("/api/capabilities").json()["warc_indexer"]["available"])


if __name__ == "__main__":
    unittest.main()
