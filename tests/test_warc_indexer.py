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
        print("Exception in thread main java.lang.RuntimeException: boom", file=sys.stderr); sys.exit(3)
    import time
    for n, w in enumerate(warcs, 1):
        name = os.path.basename(w)
        print("Parsing Archive File [%d/%d]:%s" % (n, len(warcs), w), flush=True)
        with open(os.path.join(out, name + ".jsonl"), "w", encoding="utf-8") as f:
            for i in range(2):
                f.write('{"id": "%s/%d", "source_file": "%s", "collection": %s}\\n'
                        % (name, i, name, ('"%s"' % coll) if coll else "null"))
                f.flush()
        time.sleep(float(os.environ.get("FAKE_INDEXER_SLOW", "0")))
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
        d.mkdir(parents=True, exist_ok=True)
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

    def test_the_indexer_settings_come_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "jdk"
            (home / "bin").mkdir(parents=True)
            java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
            java.write_text("", encoding="utf-8")
            jar = Path(tmp) / "custom.jar"
            jar.write_bytes(b"PK")
            conf = Path(tmp) / "my.conf"
            conf.write_text("{}", encoding="utf-8")
            stored = {"indexer.java": str(home), "indexer.jar": str(jar),
                      "indexer.config": str(conf), "indexer.memory": "3g"}
            with mock.patch.dict(os.environ, {warc_indexer.CMD_ENV: "", warc_indexer.JAR_ENV: "/elsewhere.jar",
                                              warc_indexer.JAVA_ENV: "/elsewhere/java", "JAVA_HOME": ""}):
                self.assertEqual(warc_indexer.find_java(stored.get), str(java))     # a JAVA_HOME folder
                self.assertEqual(warc_indexer.find_jar(stored.get), jar)
                self.assertEqual(warc_indexer.find_config(jar, stored.get), conf)
                self.assertEqual(warc_indexer.memory_from(stored.get), "3g")
                cmd = warc_indexer.build_command(Path("/out"), [Path("/w/a.warc.gz")], get_setting=stored.get)
            self.assertEqual(cmd[:6], [str(java), "-Xmx3g", "-jar", str(jar), "-c", str(conf)])
            self.assertEqual(warc_indexer.settings_from(stored.get),
                             {"java": str(home), "jar": str(jar), "config": str(conf), "memory": "3g"})

    def test_settings_are_checked_before_they_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "x.jar"
            jar.write_bytes(b"PK")
            self.assertEqual(warc_indexer.validate_settings({"jar": str(jar), "memory": "4g", "java": ""}),
                             {"jar": str(jar), "memory": "4g", "java": ""})
            for bad, wording in (({"java": str(Path(tmp) / "nope")}, "No java was found"),
                                 ({"jar": str(Path(tmp) / "missing.jar")}, "does not exist"),
                                 ({"config": str(Path(tmp) / "no.conf")}, "does not exist"),
                                 ({"memory": "lots"}, "heap size"),
                                 ({}, "at least one"),
                                 ("text", "must be an object")):
                with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                    warc_indexer.validate_settings(bad)
                self.assertIn(wording, str(caught.exception))

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

    def test_a_job_folder_given_relative_to_the_working_directory_still_works(self):
        # The dashboard records job folders like "warcs/108"; the jar runs
        # inside that folder, so the paths it gets must be absolute.
        d = self.job_dir("warcs/108".replace("/", os.sep))
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            manifest = warc_indexer.index_warcs(Path("warcs") / "108")
        finally:
            os.chdir(cwd)
        self.assertEqual(manifest["status"], "done", manifest.get("error"))
        for arg in manifest["command"]:
            if arg.endswith(".warc.gz"):
                self.assertTrue(Path(arg).is_absolute(), arg)
                self.assertEqual(Path(arg).parent, d.resolve())
        out_flag = manifest["command"].index("-o")
        self.assertEqual(Path(manifest["command"][out_flag + 1]), d.resolve())
        self.assertTrue((d / "a-00001.warc.gz.jsonl").is_file())

    def test_one_named_warc_can_be_indexed_alone(self):
        d = self.job_dir()
        manifest = warc_indexer.index_warcs(d, warcs=["a-00002.warc.gz"])
        self.assertEqual(manifest["warcs"], ["a-00002.warc.gz"])
        self.assertTrue((d / "a-00002.warc.gz.jsonl").is_file())
        self.assertFalse((d / "a-00001.warc.gz.jsonl").exists())
        with self.assertRaises(warc_indexer.WarcIndexerUnavailable):
            warc_indexer.index_warcs(d, warcs=["missing.warc.gz"])

    def test_a_failing_run_says_why_with_the_indexers_own_words(self):
        d = self.job_dir()
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_FAIL": "1"}):
            manifest = warc_indexer.index_warcs(d)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error"], "The indexer exited with code 3.")
        self.assertIn("RuntimeException: boom", manifest["error_detail"])
        self.assertIn("boom", (d / warc_indexer.LOG_NAME).read_text())
        shown = warc_indexer.summary(d)
        self.assertEqual(shown["status"], "failed")
        self.assertIn("boom", shown["error_detail"])
        self.assertEqual(warc_indexer._explain(1, "java.lang.UnsupportedClassVersionError: 55.0"),
                         "This Java is too old for the jar: Java 11 or newer is needed.")
        self.assertIn("memory", warc_indexer._explain(1, "java.lang.OutOfMemoryError: Java heap space"))

    def test_progress_is_recorded_while_the_run_goes(self):
        d = self.job_dir()
        seen = []
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_SLOW": "0.3"}):
            manifest = warc_indexer.index_warcs(d, poll=0.05, on_progress=seen.append)
        self.assertEqual(manifest["status"], "done")
        self.assertTrue(seen, "progress should have been reported during the run")
        self.assertEqual(seen[-1]["files_total"], 2)
        self.assertTrue(any(p["documents"] > 0 for p in seen))
        self.assertTrue(any(p["current_warc"] for p in seen))
        self.assertEqual(manifest["progress"]["documents"], 4)
        self.assertEqual(warc_indexer.summary(d)["progress"]["files_total"], 2)

    def test_a_run_that_takes_too_long_is_stopped_and_says_so(self):
        d = self.job_dir()
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_SLOW": "5"}):
            manifest = warc_indexer.index_warcs(d, poll=0.05, timeout=0.3)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("ran longer than", manifest["error"])

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

    def test_index_warc_shows_the_failure_and_the_indexers_words(self):
        d = self.job_dir()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_FAIL": "1"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["index-warc", str(d)])
        self.assertEqual(code, 1)
        self.assertIn("Indexing failed: The indexer exited with code 3.", err.getvalue())
        self.assertIn("RuntimeException: boom", err.getvalue())
        self.assertIn("Full output:", err.getvalue())

    def test_index_warc_shows_progress(self):
        d = self.job_dir()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_SLOW": "0.3"}), \
                mock.patch.object(warc_indexer, "POLL_SECONDS", 0.05), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["index-warc", str(d)])
        self.assertEqual(code, 0)
        self.assertIn("documents so far", err.getvalue())


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

    def test_the_indexer_settings_round_trip_and_are_checked(self):
        jar = self.tmp / "custom.jar"
        jar.write_bytes(b"PK")
        before = self.client.get("/api/settings").json()
        self.assertEqual(before["indexer"]["settings"], {"java": "", "jar": "", "config": "", "memory": ""})

        saved = self.client.put("/api/settings", json={"indexer": {"jar": str(jar), "memory": "4g"}})

        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["indexer"]["settings"]["jar"], str(jar))
        self.assertEqual(saved.json()["indexer"]["settings"]["memory"], "4g")
        self.assertEqual(saved.json()["indexer"]["memory"], "4g")
        again = self.client.get("/api/settings").json()
        self.assertEqual(again["indexer"]["settings"]["jar"], str(jar))

        refused = self.client.put("/api/settings", json={"indexer": {"java": str(self.tmp / "nope")}})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("No java was found", refused.json()["detail"])
        self.assertEqual(self.client.put("/api/settings", json={"indexer": {"memory": "lots"}}).status_code, 400)
        cleared = self.client.put("/api/settings", json={"indexer": {"jar": "", "memory": ""}})
        self.assertEqual(cleared.json()["indexer"]["settings"]["jar"], "")

    def test_a_failed_run_is_shown_on_the_card_with_the_log(self):
        made = self.crawl()
        out_dir = Path(made["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "x.warc.gz").write_bytes(b"\x1f\x8bxx")
        with mock.patch.dict(os.environ, {"FAKE_INDEXER_FAIL": "1"}):
            self.assertEqual(self.client.post(f"/api/crawls/{made['id']}/warc-index").status_code, 202)
            view = self.wait_done(made["id"])
        self.assertEqual(view["warc_index"]["status"], "failed")
        self.assertEqual(view["warc_index"]["error"], "The indexer exited with code 3.")
        self.assertIn("boom", view["warc_index"]["error_detail"])
        log = self.client.get(f"/api/crawls/{made['id']}/warc-index/log")
        self.assertEqual(log.status_code, 200)
        self.assertIn("boom", log.text)
        self.assertEqual(self.client.get("/api/crawls/9999/warc-index/log").status_code, 404)

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
