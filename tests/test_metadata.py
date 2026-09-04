"""Every capture carries a description of what it is: Dublin Core, the
Archive-It way -- repeatable elements, custom fields, a job level and a
seed level, written into metadata.json, the WARC and the manifest."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fastapi.testclient import TestClient

from webarc import cli
from webarc import metadata as md
from webarc import server as srv


def names(fields):
    return [(f["name"], f["value"]) for f in fields]


class ModelTests(unittest.TestCase):
    def test_fields_come_in_from_any_reasonable_shape(self):
        seen = md.normalise_fields({"subject": ["Football", " Qatar "], "TITLE": "Ballers", "Rights": ""})

        self.assertEqual(names(seen), [("Subject", "Football"), ("Subject", "Qatar"), ("Title", "Ballers")])

    def test_pairs_and_dicts_are_accepted_too(self):
        seen = md.normalise_fields([["Creator", "A"], {"name": "custom note", "value": "kept"}])

        self.assertEqual(names(seen), [("Creator", "A"), ("custom note", "kept")])

    def test_a_value_that_is_not_text_is_refused(self):
        with self.assertRaises(ValueError):
            md.normalise_fields([{"name": "Title", "value": {"nested": 1}}])
        with self.assertRaises(ValueError):
            md.normalise_fields([{"name": "Title", "value": "x" * (md.MAX_VALUE + 1)}])

    def test_seed_values_replace_the_jobs_for_that_element_only(self):
        job = md.normalise_fields({"Title": "Job", "Subject": ["A", "B"]})
        seed = md.normalise_fields({"Subject": "C"})

        self.assertEqual(names(md.merge(job, seed)), [("Title", "Job"), ("Subject", "C")])

    def test_defaults_fill_only_what_is_empty(self):
        fields = md.normalise_fields({"Title": "Mine"})

        filled = md.with_defaults(fields, md.defaults_for("crawl", "job-name", "QNL", "https://a/"))

        self.assertEqual(dict(names(filled))["Title"], "Mine")
        self.assertEqual(dict(names(filled))["Identifier"], "https://a/")
        self.assertEqual(dict(names(filled))["Collector"], "QNL")
        self.assertEqual(dict(names(filled))["Type"], "Website")

    def test_a_config_carries_job_and_seed_blocks(self):
        raw = {"metadata": {"Title": "Job"},
               "seeds": [{"url": "https://a/", "metadata": {"Title": "A"}}, {"url": "https://b/"}]}

        seen = md.from_config(raw)

        self.assertEqual(names(seen["job"]), [("Title", "Job")])
        self.assertEqual(list(seen["seeds"]), ["https://a/"])

    def test_the_warc_record_names_dublin_core_elements(self):
        text = md.warc_fields_text(md.normalise_fields({"Title": "T", "Collector": "C", "My note": "n"})).decode()

        self.assertIn("dc.title: T\r\n", text)
        self.assertIn("collector: C\r\n", text)
        self.assertIn("custom.my-note: n\r\n", text)

    def test_the_sheet_round_trips_with_repeated_columns(self):
        meta = md.normalise({"job": {"Subject": ["A", "B"]}, "seeds": {"https://a/": {"Title": "A site"}}})
        doc = md.document(job_id=1, kind="crawl", name="j", operator="o",
                          seeds=[{"url": "https://a/"}, {"url": "https://b/"}], metadata=meta)

        text = md.csv_text(doc)
        back = md.parse_csv(text)

        self.assertEqual(text.splitlines()[0], "seed_url,Title,Subject,Subject")
        self.assertEqual(names(back["job"]), [("Subject", "A"), ("Subject", "B")])
        self.assertEqual(names(back["seeds"]["https://a/"]), [("Title", "A site")])
        self.assertNotIn("https://b/", back["seeds"])

    def test_a_sheet_without_a_seed_column_is_refused(self):
        with self.assertRaises(ValueError):
            md.parse_csv("Title,Subject\nA,B\n")

    def test_an_edit_keeps_the_first_written_time(self):
        first = md.document(job_id=1, kind="crawl", name="j", operator="o", seeds=[], metadata={"job": [], "seeds": {}})
        with tempfile.TemporaryDirectory() as tmp:
            md.write_document(Path(tmp), first)
            time.sleep(0.01)
            again = md.document(job_id=1, kind="crawl", name="j", operator="o", seeds=[],
                                metadata={"job": [], "seeds": {}}, existing=md.read_document(Path(tmp)))

        self.assertEqual(again["written_at"], first["written_at"])

    def test_reader_page_rows_join_repeated_elements(self):
        section = {"seeds": [{"url": "https://a/", "effective": md.normalise_fields({"Subject": ["A", "B"], "Title": "T"})}]}

        self.assertEqual(md.describe_rows(section, "https://a/"), [("Subject", "A; B"), ("Title", "T")])


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)

    def create(self, **extra) -> dict:
        body = {"name": "demo", "start": "wait",
                "config": {"operator": "QNL", "seeds": [{"url": "https://a.example/"}, {"url": "https://b.example/"}]}}
        body.update(extra)
        made = self.client.post("/api/crawls", json=body)
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def doc(self, made: dict) -> dict:
        return json.loads((Path(made["output_dir"]) / md.DOCUMENT_NAME).read_text(encoding="utf-8"))


class ServerTests(ServerTestCase):
    METADATA = {"job": {"Subject": "Football"}, "seeds": {"https://b.example/": {"Title": "B site"}}}

    def test_a_job_is_described_the_moment_it_is_created(self):
        made = self.create(metadata=self.METADATA)

        doc = self.doc(made)
        self.assertEqual(doc["schema"], md.SCHEMA)
        self.assertEqual(names(doc["job"]), [("Subject", "Football")])
        b = next(s for s in doc["seeds"] if s["url"] == "https://b.example/")
        self.assertEqual(dict(names(b["effective"]))["Title"], "B site")
        self.assertEqual(dict(names(b["effective"]))["Collector"], "QNL")
        self.assertEqual(made["metadata_fields"], 2)

    def test_every_job_type_accepts_metadata(self):
        for path, body in (
            ("/api/recordings", {"url": "https://example.org/"}),
            ("/api/facebook", {"page_url": "https://www.facebook.com/qatarnationallibrary", "mode": "latest_n", "latest_n": 5}),
            ("/api/instagram", {"targets": ["qatarnationallibrary"], "mode": "latest_n", "latest_n": 5}),
        ):
            with self.subTest(path=path):
                body.update({"start": "wait", "metadata": {"job": {"Rights": "CC BY"}}})
                made = self.client.post(path, json=body)
                self.assertEqual(made.status_code, 201, made.text)
                self.assertEqual(names(self.doc(made.json())["job"]), [("Rights", "CC BY")])

    def test_bad_metadata_is_refused_before_anything_is_made(self):
        refused = self.client.post("/api/crawls", json={
            "config": {"seeds": [{"url": "https://a.example/"}]},
            "metadata": {"job": [{"name": "Title", "value": ["not", "text"]}]}})

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.client.get("/api/crawls").json(), [])

    def test_metadata_in_the_yaml_counts_too(self):
        made = self.client.post("/api/crawls", json={"start": "wait", "config_yaml":
            "seeds:\n  - url: https://a.example/\n    metadata:\n      Title: From YAML\nmetadata:\n  Subject: Sport\n"})

        self.assertEqual(made.status_code, 201, made.text)
        doc = self.doc(made.json())
        self.assertEqual(names(doc["job"]), [("Subject", "Sport")])
        self.assertEqual(names(doc["seeds"][0]["fields"]), [("Title", "From YAML")])

    def test_it_can_be_read_edited_and_exported_after_the_fact(self):
        made = self.create(metadata=self.METADATA)

        edited = self.client.put(f"/api/crawls/{made['id']}/metadata", json={
            "metadata": {"job": {"Subject": ["Football", "Sport"]}, "seeds": {}}})

        self.assertEqual(edited.status_code, 200, edited.text)
        seen = self.client.get(f"/api/crawls/{made['id']}/metadata").json()
        self.assertEqual(names(seen["job"]), [("Subject", "Football"), ("Subject", "Sport")])
        self.assertEqual(seen["seeds"][1]["fields"], [])
        self.assertEqual(names(self.doc(made)["job"]), names(seen["job"]))
        sheet = self.client.get(f"/api/crawls/{made['id']}/metadata.csv")
        self.assertEqual(sheet.status_code, 200)
        self.assertTrue(sheet.text.startswith("seed_url,Subject,Subject"))
        self.assertIn("attachment", sheet.headers["content-disposition"])

    def test_a_sheet_can_be_read_back(self):
        parsed = self.client.post("/api/metadata/parse", json={
            "csv": "seed_url,Title,Subject\n*,,Sport\nhttps://a.example/,A site,\n"}).json()

        self.assertEqual(names(parsed["job"]), [("Subject", "Sport")])
        self.assertEqual(names(parsed["seeds"]["https://a.example/"]), [("Title", "A site")])

    def test_a_social_manifest_takes_the_edited_metadata(self):
        made = self.client.post("/api/instagram", json={
            "targets": ["qatarnationallibrary"], "mode": "latest_n", "latest_n": 5, "start": "wait"}).json()
        manifest = Path(made["output_dir"]) / "instagram-manifest.json"
        manifest.write_text(json.dumps({"schema": "swm-instagram-capture-manifest-v1"}), encoding="utf-8")

        self.client.put(f"/api/crawls/{made['id']}/metadata", json={"metadata": {"job": {"Title": "QNL on Instagram"}}})

        seen = json.loads(manifest.read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(dict(names(seen["seeds"][0]["effective"]))["Title"], "QNL on Instagram")

    def test_the_warc_carries_a_metadata_record_for_its_seed(self):
        from warcio.archiveiterator import ArchiveIterator
        made = self.create(start="now", metadata=self.METADATA)
        job_dir = Path(made["output_dir"])
        for _ in range(100):
            if self.client.get(f"/api/crawls/{made['id']}").json()["status"] in ("completed", "failed", "stopped"):
                break
            time.sleep(0.2)
        records = {}
        for path in sorted(job_dir.glob("*.warc.gz")):
            with open(path, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type == "metadata":
                        records[record.rec_headers.get_header("WARC-Target-URI")] = record.content_stream().read().decode()

        self.assertIn("dc.subject: Football", records["https://a.example/"])
        self.assertIn("dc.title: B site", records["https://b.example/"])
        self.assertIn("dc.identifier: https://b.example/", records["https://b.example/"])
        self.assertIn("collector: QNL", records["https://a.example/"])


class CommandLineTests(unittest.TestCase):
    def test_export_writes_a_sheet_from_a_job_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = md.normalise({"job": {"Title": "T"}, "seeds": {}})
            md.write_document(Path(tmp), md.document(job_id=1, kind="crawl", name="j", operator="o",
                                                     seeds=[{"url": "https://a/"}], metadata=meta))
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["metadata", "export", tmp, "-o", "-"])

            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue().splitlines()[0], "seed_url,Title")
            with redirect_stdout(io.StringIO()):
                cli.main(["metadata", "export", tmp])
            self.assertTrue((Path(tmp) / md.CSV_NAME).is_file())

    def test_a_folder_without_metadata_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["metadata", "export", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
