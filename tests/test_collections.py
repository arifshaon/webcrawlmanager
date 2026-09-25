"""Collections: named containers jobs belong to, each with a directory.

A collection is where a curator files the jobs that belong together. Its
directory holds every job run against it, its metadata is inherited by
those jobs, and deleting it -- or one of its jobs -- states the consequences
before anything changes. Everything here runs without a browser.
"""

from __future__ import annotations

import io
import json
import tempfile
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from webarc import collections as colls
from webarc import metadata as md
from webarc import server as srv
from webarc.cli import main as cli_main
from webarc.store import Store


class ModuleTests(unittest.TestCase):
    def test_a_slug_is_stable_and_directory_safe(self):
        self.assertEqual(colls.slugify("Qatar News Sites 2026"), "qatar-news-sites-2026")
        self.assertEqual(colls.slugify("  a/b\\c:d  "), "a-b-c-d")
        self.assertEqual(colls.slugify("مكتبة قطر"), "مكتبة-قطر")
        self.assertEqual(colls.slugify(""), "collection")
        self.assertLessEqual(len(colls.slugify("x" * 500)), colls.MAX_SLUG)

    def test_a_job_inherits_the_collections_fields_and_a_relation(self):
        collection = {"id": 1, "slug": "qnl-2026", "name": "QNL 2026",
                      "metadata": [{"name": "Subject", "value": "Libraries"},
                                   {"name": "Rights", "value": "Public"}]}

        fields = colls.inherited_fields(collection)

        self.assertIn({"name": "Subject", "value": "Libraries"}, fields)
        self.assertIn({"name": "Relation", "value": "isPartOf: QNL 2026"}, fields)
        self.assertIn({"name": "Collection", "value": "qnl-2026"}, fields)

    def test_the_jobs_own_value_replaces_the_inherited_one(self):
        collection = {"id": 1, "slug": "c", "name": "C",
                      "metadata": [{"name": "Subject", "value": "Libraries"},
                                   {"name": "Rights", "value": "Public"}]}

        fields = colls.effective_job_fields(collection, [{"name": "Subject", "value": "Football"}])

        subjects = [f["value"] for f in fields if f["name"] == "Subject"]
        self.assertEqual(subjects, ["Football"])
        self.assertIn({"name": "Rights", "value": "Public"}, fields)

    def test_nothing_is_inherited_without_a_collection(self):
        self.assertEqual(colls.inherited_fields(None), [])
        self.assertIsNone(colls.brief(None))

    def test_metadata_comes_from_json_or_a_file(self):
        self.assertEqual(colls.load_metadata_argument('[{"name": "Subject", "value": "X"}]', None),
                         [{"name": "Subject", "value": "X"}])
        self.assertEqual(colls.load_metadata_argument('{"Subject": "X"}', None),
                         [{"name": "Subject", "value": "X"}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text('{"job": [{"name": "Rights", "value": "Open"}]}', encoding="utf-8")
            self.assertEqual(colls.load_metadata_argument(None, str(path)),
                             [{"name": "Rights", "value": "Open"}])
            sheet = Path(tmp) / "m.csv"
            sheet.write_text(md.csv_text(md.document(
                job_id=1, kind="crawl", name="n", operator="o", seeds=[],
                metadata={"job": [{"name": "Subject", "value": "Sheet"}], "seeds": {}})),
                encoding="utf-8")
            self.assertEqual(colls.load_metadata_argument(None, str(sheet)),
                             [{"name": "Subject", "value": "Sheet"}])
        with self.assertRaises(ValueError):
            colls.load_metadata_argument("not json", None)
        with self.assertRaises(ValueError):
            colls.load_metadata_argument("[]", "also.json")

    def test_the_document_lists_the_jobs_and_what_they_inherit(self):
        collection = {"id": 3, "slug": "c", "name": "C", "description": "d",
                      "root_dir": "/x/collections/c", "metadata": [], "created_at": "t"}
        doc = colls.document(collection, [{"id": 7, "name": "j", "kind": "crawl",
                                           "status": "completed", "output_dir": "/x/c/jobs/7"}])

        self.assertEqual(doc["schema"], colls.SCHEMA)
        self.assertEqual([j["id"] for j in doc["jobs"]], [7])
        self.assertIn({"name": "Relation", "value": "isPartOf: C"}, doc["inherited_by_jobs"])
        with tempfile.TemporaryDirectory() as tmp:
            colls.write_document(tmp, doc)
            self.assertEqual(colls.read_document(tmp)["slug"], "c")

    def test_deleting_a_job_says_nothing_else_refers_into_it_yet(self):
        collection = {"id": 1, "slug": "c", "name": "C", "metadata": []}
        row = {"id": 5, "name": "first", "kind": "crawl", "status": "completed",
               "created_at": "2026-09-01"}
        later = {"id": 6, "name": "second", "created_at": "2026-09-02"}

        impact = colls.job_impact(collection, row, [row, later])

        self.assertEqual(impact["referring_records"], 0)
        self.assertEqual([j["id"] for j in impact["later_jobs_in_collection"]], [6])
        self.assertIn("No other job's records refer into this one", impact["note"])
        self.assertIn("Deleting job #5", colls.describe_impact(impact))

    def test_deleting_a_collection_counts_its_jobs(self):
        collection = {"id": 1, "slug": "c", "name": "C", "root_dir": "/x"}
        impact = colls.collection_impact(
            collection, [{"id": 1, "status": "completed"}, {"id": 2, "status": "failed"}],
            bytes_on_disk=2048, running=[])

        self.assertEqual(impact["job_count"], 2)
        self.assertEqual(impact["by_status"], {"completed": 1, "failed": 1})
        text = colls.describe_impact(impact)
        self.assertIn('Deleting collection "C" removes 2 job(s)', text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "swm.db")

    def test_a_collection_is_created_found_and_listed(self):
        cid = self.store.create_collection("qnl", "QNL", "desc", "/x/qnl",
                                           [{"name": "Subject", "value": "L"}])

        self.assertEqual(self.store.get_collection(cid)["slug"], "qnl")
        self.assertEqual(self.store.find_collection("QNL")["id"], cid)
        self.assertEqual(self.store.find_collection("qnl")["id"], cid)
        self.assertEqual(self.store.find_collection(str(cid))["id"], cid)
        self.assertIsNone(self.store.find_collection("nope"))
        self.assertEqual([c["name"] for c in self.store.list_collections()], ["QNL"])
        self.assertEqual(self.store.get_collection(cid)["metadata"],
                         [{"name": "Subject", "value": "L"}])

    def test_the_identifier_is_unique(self):
        self.store.create_collection("qnl", "QNL", "", "/x/qnl")
        with self.assertRaises(ValueError):
            self.store.create_collection("qnl", "Other", "", "/y/qnl")

    def test_jobs_are_counted_per_collection(self):
        cid = self.store.create_collection("qnl", "QNL", "", "/x/qnl")
        first = self.store.create_crawl("a", {"seeds": []}, "/x/qnl/jobs/1", 0,
                                        collection_id=cid)
        self.store.create_crawl("b", {"seeds": []}, "/x/qnl/jobs/2", 0, collection_id=cid)
        self.store.create_crawl("loose", {"seeds": []}, "/x/3", 0)
        self.store.set_status(first, "completed")

        counts = self.store.collection_counts()[cid]
        self.assertEqual(counts["jobs"], 2)
        self.assertEqual(counts["by_status"], {"completed": 1, "pending": 1})
        self.assertEqual([j["name"] for j in self.store.crawls_in_collection(cid)], ["a", "b"])

    def test_an_older_database_gains_the_column(self):
        """A store made before collections existed still opens, and its
        jobs simply belong to none."""
        path = Path(self._tmp.name) / "old.db"
        import sqlite3
        # Closed explicitly: sqlite3's context manager commits but keeps the
        # connection open, and Windows will not remove the file while it is.
        conn = sqlite3.connect(path)
        try:
            conn.executescript("""
                CREATE TABLE crawls (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'crawl', config_json TEXT NOT NULL,
                    output_dir TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    control TEXT NOT NULL DEFAULT 'none', pid INTEGER,
                    seeds_total INTEGER NOT NULL DEFAULT 0, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO crawls (name, config_json, output_dir, created_at, updated_at)
                    VALUES ('old', '{}', '/x/1', 't', 't');""")
        finally:
            conn.close()
        store = Store(path)

        self.assertIsNone(store.get_crawl(1)["collection_id"])
        self.assertEqual(store.collection_counts(), {})

    def test_deleting_a_collection_removes_its_jobs_rows(self):
        cid = self.store.create_collection("qnl", "QNL", "", "/x/qnl")
        job = self.store.create_crawl("a", {"seeds": []}, "/x/qnl/jobs/1", 0, collection_id=cid)
        loose = self.store.create_crawl("loose", {"seeds": []}, "/x/2", 0)

        removed = self.store.delete_collection(cid)

        self.assertEqual(removed, [job])
        self.assertIsNone(self.store.get_crawl(job))
        self.assertIsNotNone(self.store.get_crawl(loose))
        self.assertIsNone(self.store.get_collection(cid))


class DocumentRefreshTests(unittest.TestCase):
    """collection.json lists each job with the state it is in now, not the
    state it was created in."""

    def test_a_finished_job_is_listed_as_finished(self):
        from webarc import worker

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "swm.db")
            root = Path(tmp) / "collections" / "c"
            cid = store.create_collection("c", "C", "", str(root))
            collection = store.get_collection(cid)
            job = store.create_crawl("j", {"seeds": []}, str(root / "jobs" / "1"), 0,
                                     collection_id=cid)
            colls.refresh_document(store, collection)
            self.assertEqual(colls.read_document(root)["jobs"][0]["status"], "pending")

            store.set_status(job, "completed")
            worker._note_job_end(store, job)

            self.assertEqual(colls.read_document(root)["jobs"][0]["status"], "completed")

    def test_the_command_line_job_reports_its_end_too(self):
        from webarc.cli import _settle_registered_job

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "swm.db")
            root = Path(tmp) / "collections" / "c"
            cid = store.create_collection("c", "C", "", str(root))
            job = store.create_crawl("j", {"seeds": []}, str(root / "jobs" / "1"), 0,
                                     collection_id=cid)

            _settle_registered_job((store, job), "failed")

            self.assertEqual(colls.read_document(root)["jobs"][0]["status"], "failed")

    def test_a_job_metadata_document_carries_its_job_number(self):
        """metadata.json in a job folder names the job it belongs to, whether
        the worker or the command line wrote it."""
        from webarc import worker
        from webarc.crawler import write_crawl_metadata
        from webarc.metadata import read_document

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "swm.db")
            root = Path(tmp) / "collections" / "c"
            cid = store.create_collection("c", "C", "", str(root))
            job_dir = root / "jobs" / "1"
            job = store.create_crawl("j", {"seeds": [{"url": "https://s/"}]},
                                     str(job_dir), 1, collection_id=cid)
            config = worker._config_from_row(store.get_crawl(job), store.get_collection(cid))
            self.assertEqual(config.job_id, job)

            write_crawl_metadata(config)

            self.assertEqual(read_document(job_dir)["job_id"], job)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "warcs"
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.root),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)

    def collection(self, name="QNL 2026", **extra):
        body = {"name": name, "description": "The library's own sites",
                "metadata": [{"name": "Subject", "value": "Libraries"},
                             {"name": "Rights", "value": "Public"}], **extra}
        made = self.client.post("/api/collections", json=body)
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def job(self, **extra):
        body = {"name": "demo", "start": "wait",
                "config": {"operator": "QNL", "seeds": [{"url": "https://a.example/"}]}}
        body.update(extra)
        made = self.client.post("/api/crawls", json=body)
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()


class ServerTests(ServerTestCase):
    def test_a_collection_gets_a_directory_and_a_document(self):
        made = self.collection()

        root = Path(made["root_dir"])
        self.assertEqual(root, self.root / "collections" / "qnl-2026")
        self.assertTrue((root / colls.DOCUMENT_NAME).is_file())
        doc = json.loads((root / colls.DOCUMENT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(doc["name"], "QNL 2026")
        self.assertEqual(made["jobs"], 0)
        self.assertEqual(made["metadata_fields"], 2)

    def test_a_second_collection_of_the_same_name_is_refused(self):
        self.collection()
        again = self.client.post("/api/collections", json={"name": "qnl 2026"})

        self.assertEqual(again.status_code, 409)

    def test_a_nameless_collection_is_refused(self):
        self.assertEqual(self.client.post("/api/collections", json={"name": " "}).status_code, 400)

    def test_a_job_is_placed_under_its_collection(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])

        self.assertEqual(Path(job["output_dir"]),
                         Path(made["root_dir"]) / "jobs" / str(job["id"]))
        self.assertEqual(job["collection"]["id"], made["id"])
        self.assertEqual(job["collection"]["slug"], "qnl-2026")
        self.assertEqual(job["collection"]["name"], "QNL 2026")
        self.assertTrue(job["collection"]["dedup_across_jobs"])
        doc = json.loads((Path(made["root_dir"]) / colls.DOCUMENT_NAME).read_text())
        self.assertEqual([j["id"] for j in doc["jobs"]], [job["id"]])
        listed = self.client.get("/api/collections").json()[0]
        self.assertEqual(listed["jobs"], 1)

    def test_a_collection_can_be_named_or_made_from_the_job_form(self):
        made = self.collection()
        by_name = self.job(collection="QNL 2026")
        self.assertEqual(by_name["collection"]["id"], made["id"])

        fresh = self.job(new_collection={"name": "Elections"})
        self.assertEqual(fresh["collection"]["slug"], "elections")
        self.assertEqual(len(self.client.get("/api/collections").json()), 2)

    def test_a_name_that_matches_nothing_is_an_error_not_a_new_collection(self):
        body = {"name": "demo", "start": "wait", "collection": "typo",
                "config": {"operator": "QNL", "seeds": [{"url": "https://a.example/"}]}}

        self.assertEqual(self.client.post("/api/crawls", json=body).status_code, 404)
        self.assertEqual(self.client.get("/api/collections").json(), [])

    def test_every_job_type_can_join_a_collection(self):
        made = self.collection()
        rec = self.client.post("/api/recordings", json={
            "url": "https://a.example/", "start": "wait", "collection_id": made["id"]})
        self.assertEqual(rec.status_code, 201, rec.text)
        self.assertEqual(rec.json()["collection"]["id"], made["id"])
        self.assertTrue(str(rec.json()["output_dir"]).startswith(made["root_dir"]))

    def test_the_job_inherits_the_collections_metadata(self):
        made = self.collection()
        job = self.job(collection_id=made["id"],
                       metadata={"job": {"Subject": "Football"}})

        doc = json.loads((Path(job["output_dir"]) / md.DOCUMENT_NAME).read_text())
        effective = doc["seeds"][0]["effective"]
        self.assertEqual([f["value"] for f in effective if f["name"] == "Subject"], ["Football"])
        self.assertIn({"name": "Rights", "value": "Public"}, effective)
        self.assertIn({"name": "Relation", "value": "isPartOf: QNL 2026"}, effective)
        self.assertIn({"name": "Collection", "value": "qnl-2026"}, effective)
        self.assertEqual(doc["collection"]["slug"], "qnl-2026")
        self.assertEqual(doc["job"], [{"name": "Subject", "value": "Football"}])

    def test_changing_the_collection_reaches_its_jobs(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])

        updated = self.client.put(f"/api/collections/{made['id']}", json={
            "name": "QNL 2027", "metadata": [{"name": "Rights", "value": "Restricted"}]})

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["slug"], "qnl-2026")      # never changes
        self.assertEqual(updated.json()["name"], "QNL 2027")
        doc = json.loads((Path(job["output_dir"]) / md.DOCUMENT_NAME).read_text())
        effective = doc["seeds"][0]["effective"]
        self.assertIn({"name": "Rights", "value": "Restricted"}, effective)
        self.assertIn({"name": "Relation", "value": "isPartOf: QNL 2027"}, effective)
        self.assertNotIn({"name": "Subject", "value": "Libraries"}, effective)

    def test_the_collection_view_lists_its_jobs(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])

        view = self.client.get(f"/api/collections/{made['id']}").json()
        self.assertEqual([j["id"] for j in view["job_list"]], [job["id"]])
        jobs = self.client.get(f"/api/collections/{made['id']}/jobs").json()
        self.assertEqual([j["id"] for j in jobs], [job["id"]])

    def test_deleting_a_job_states_its_impact_first(self):
        made = self.collection()
        first = self.job(collection_id=made["id"])
        second = self.job(collection_id=made["id"])

        impact = self.client.get(f"/api/crawls/{first['id']}/impact").json()

        self.assertEqual(impact["collection"]["id"], made["id"])
        self.assertEqual(impact["referring_records"], 0)
        self.assertEqual([j["id"] for j in impact["later_jobs_in_collection"]], [second["id"]])
        self.assertIn("No other job's records refer into this one", impact["note"])

    def test_deleting_a_job_keeps_the_collections_document_current(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])

        gone = self.client.delete(f"/api/crawls/{job['id']}?purge=true")

        self.assertEqual(gone.status_code, 200, gone.text)
        doc = json.loads((Path(made["root_dir"]) / colls.DOCUMENT_NAME).read_text())
        self.assertEqual(doc["jobs"], [])
        self.assertFalse(Path(job["output_dir"]).exists())
        self.assertTrue(Path(made["root_dir"]).exists())

    def test_deleting_a_collection_states_its_impact_then_does_as_told(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])
        (Path(job["output_dir"]) / "a.warc.gz").write_bytes(b"x" * 100)

        impact = self.client.get(f"/api/collections/{made['id']}/impact").json()
        self.assertEqual(impact["job_count"], 1)
        self.assertGreaterEqual(impact["bytes_on_disk"], 100)
        self.assertEqual(impact["running_jobs"], [])

        kept = self.client.delete(f"/api/collections/{made['id']}")
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(kept.json()["jobs_removed"], [job["id"]])
        self.assertTrue((Path(job["output_dir"]) / "a.warc.gz").exists())   # files kept
        self.assertEqual(self.client.get(f"/api/crawls/{job['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/collections").json(), [])

    def test_a_purged_collection_takes_its_files_with_it(self):
        made = self.collection()
        job = self.job(collection_id=made["id"])

        self.client.delete(f"/api/collections/{made['id']}?purge=true")

        self.assertFalse(Path(made["root_dir"]).exists())
        self.assertFalse(Path(job["output_dir"]).exists())

    def test_nothing_changes_when_the_collection_is_missing(self):
        self.assertEqual(self.client.get("/api/collections/99").status_code, 404)
        self.assertEqual(self.client.delete("/api/collections/99").status_code, 404)
        self.assertEqual(self.client.get("/api/collections/99/impact").status_code, 404)

    def test_a_collection_with_no_warc_cannot_replay_yet(self):
        made = self.collection()
        self.job(collection_id=made["id"])

        self.assertEqual(self.client.post(f"/api/collections/{made['id']}/replay").status_code, 409)

    def test_storage_reports_each_collection(self):
        made = self.collection()
        storage = self.client.get("/api/storage").json()

        self.assertEqual([c["id"] for c in storage["per_collection"]], [made["id"]])

    def test_a_job_that_names_no_collection_goes_to_the_default_one(self):
        self.assertEqual(self.client.get("/api/collections").json(), [])
        job = self.job()

        self.assertEqual(job["collection"]["slug"], "default")
        self.assertEqual(job["collection"]["name"], "Default")
        default_root = self.root / "collections" / "default"
        self.assertEqual(Path(job["output_dir"]), default_root / "jobs" / str(job["id"]))
        doc = json.loads((Path(job["output_dir"]) / md.DOCUMENT_NAME).read_text())
        self.assertEqual(doc["collection"]["slug"], "default")
        listed = self.client.get("/api/collections").json()
        self.assertEqual([c["slug"] for c in listed], ["default"])
        self.assertEqual(listed[0]["jobs"], 1)
        again = self.job()                                   # made once, reused after
        self.assertEqual(again["collection"]["id"], job["collection"]["id"])
        self.assertEqual(self.client.get("/api/collections").json()[0]["jobs"], 2)

    def test_a_deleted_default_collection_is_made_again_when_needed(self):
        first = self.job()
        gone = self.client.delete(f"/api/collections/{first['collection']['id']}?purge=true")
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertEqual(self.client.get("/api/collections").json(), [])
        second = self.job()
        self.assertEqual(second["collection"]["slug"], "default")
        self.assertNotEqual(second["collection"]["id"], first["collection"]["id"])
        self.assertTrue(Path(second["output_dir"]).is_dir())

    def test_a_job_from_before_collections_keeps_its_place(self):
        row = srv._store().create_crawl("old", {"seeds": []}, str(self.root / "7"), 0)
        view = self.client.get(f"/api/crawls/{row}").json()
        self.assertIsNone(view["collection"])
        self.assertEqual(Path(view["output_dir"]), self.root / "7")


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "swm.db")
        self.root = str(self.tmp / "warcs")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_collection_is_created_listed_and_shown(self):
        code, out, _ = self.run_cli(
            "collection", "create", "QNL 2026", "--db", self.db, "--warc-root", self.root,
            "--description", "The library's sites",
            "--metadata-json", '[{"name": "Subject", "value": "Libraries"}]')

        self.assertEqual(code, 0, out)
        self.assertIn("qnl-2026", out)
        root = self.tmp / "warcs" / "collections" / "qnl-2026"
        self.assertTrue((root / colls.DOCUMENT_NAME).is_file())

        code, out, _ = self.run_cli("collection", "list", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("QNL 2026", out)
        self.assertIn("0 job(s)", out)

        code, out, _ = self.run_cli("collection", "show", "qnl-2026", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("Subject: Libraries", out)

        code, out, _ = self.run_cli("collection", "list", "--db", self.db, "--json")
        self.assertEqual(json.loads(out)[0]["slug"], "qnl-2026")

    def test_metadata_can_come_from_a_file(self):
        path = self.tmp / "m.json"
        path.write_text('[{"name": "Rights", "value": "Open"}]', encoding="utf-8")
        code, out, _ = self.run_cli(
            "collection", "create", "Open", "--db", self.db, "--warc-root", self.root,
            "--metadata-file", str(path))

        self.assertEqual(code, 0, out)
        self.assertEqual(Store(self.db).find_collection("open")["metadata"],
                         [{"name": "Rights", "value": "Open"}])

    def test_a_duplicate_name_is_refused(self):
        self.run_cli("collection", "create", "QNL", "--db", self.db, "--warc-root", self.root)
        code, _, err = self.run_cli("collection", "create", "qnl", "--db", self.db,
                                    "--warc-root", self.root)

        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_deleting_says_what_it_means_and_needs_a_yes(self):
        self.run_cli("collection", "create", "QNL", "--db", self.db, "--warc-root", self.root)
        store = Store(self.db)
        cid = store.find_collection("qnl")["id"]
        store.create_crawl("a", {"seeds": []}, str(self.tmp / "warcs/collections/qnl/jobs/1"),
                           0, collection_id=cid)

        # no terminal to ask on, no --yes: nothing happens. (Standard input is
        # replaced so the test never waits on a real keyboard.)
        with mock.patch("sys.stdin", io.StringIO()):
            code, out, err = self.run_cli("collection", "delete", "qnl", "--db", self.db)
        self.assertEqual(code, 2)
        self.assertIn("removes 1 job(s)", out)
        self.assertIn("Nothing is changed until you confirm", out)
        self.assertIn("no terminal to confirm on", err)
        self.assertIsNotNone(store.find_collection("qnl"))

        # a terminal that answers no: nothing happens either
        terminal = mock.Mock(); terminal.isatty.return_value = True
        with mock.patch("sys.stdin", terminal), mock.patch("builtins.input", return_value="n"):
            code, out, _ = self.run_cli("collection", "delete", "qnl", "--db", self.db)
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", out)
        self.assertIsNotNone(store.find_collection("qnl"))

        # a terminal that answers yes deletes, as --yes does
        with mock.patch("sys.stdin", terminal), mock.patch("builtins.input", return_value="y"):
            code, out, _ = self.run_cli("collection", "delete", "qnl", "--db", self.db)
        self.assertEqual(code, 0, out)
        self.assertIsNone(store.find_collection("qnl"))

        # made again, --yes needs no terminal at all
        self.run_cli("collection", "create", "QNL", "--db", self.db, "--warc-root", self.root)
        code, out, _ = self.run_cli("collection", "delete", "qnl", "--db", self.db, "--yes")
        self.assertEqual(code, 0, out)
        self.assertIsNone(store.find_collection("qnl"))
        self.assertIsNone(store.get_crawl(1))
        self.assertTrue((self.tmp / "warcs/collections/qnl").exists())     # files kept

    def test_purge_removes_the_directory(self):
        self.run_cli("collection", "create", "QNL", "--db", self.db, "--warc-root", self.root)
        self.assertTrue((self.tmp / "warcs/collections/qnl").exists())

        code, _, _ = self.run_cli("collection", "delete", "qnl", "--db", self.db,
                                  "--yes", "--purge")

        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "warcs/collections/qnl").exists())

    def test_a_job_names_a_collection_that_must_exist(self):
        from types import SimpleNamespace

        from webarc.cli import _register_job_in_collection

        args = SimpleNamespace(db=self.db, collection="nope", create_collection=False,
                               warc_root=self.root)
        with self.assertRaises(ValueError) as caught:
            _register_job_in_collection(args, "job", "crawl", {"seeds": []}, seeds_total=0)
        self.assertIn("Create it first", str(caught.exception))
        self.assertEqual(Store(self.db).list_collections(), [])

    def test_a_job_naming_no_collection_is_placed_in_the_default_one(self):
        from types import SimpleNamespace
        from webarc.cli import _register_job_in_collection

        args = SimpleNamespace(db=self.db, collection=None, create_collection=False,
                               warc_root=self.root)
        (store, job), collection, job_dir = _register_job_in_collection(
            args, "job", "crawl", {"seeds": [{"url": "https://a.example/"}]}, seeds_total=1)
        self.assertEqual(collection["slug"], "default")
        self.assertEqual(job_dir, Path(collection["root_dir"]) / "jobs" / str(job))
        self.assertTrue(Path(collection["root_dir"]).is_absolute())
        self.assertEqual(Path(collection["root_dir"]).resolve(),
                         (Path(self.root) / "collections" / "default").resolve())
        (_store, second), again, _dir = _register_job_in_collection(
            args, "job2", "crawl", {"seeds": []}, seeds_total=0)
        self.assertEqual(again["id"], collection["id"])

    def test_standalone_keeps_a_job_out_of_every_collection(self):
        cfg = Path(self.tmp) / "cfg.yaml"
        cfg.write_text("crawl_name: x\noutput_dir: %s\nseeds:\n  - url: https://a.example/\n"
                       % (Path(self.tmp) / "out"))
        with mock.patch("webarc.cli.run_crawl") as run:
            code, out, err = self.run_cli("crawl", str(cfg), "--standalone", "--no-resource-check",
                                          "--db", self.db, "--warc-root", self.root)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Path(run.call_args[0][0].output_dir), Path(self.tmp) / "out")
        self.assertFalse(Path(self.db).exists())            # no record kept
        self.assertIsNone(run.call_args[0][0].collection)

        with mock.patch("webarc.cli.run_crawl") as run:
            code, out, err = self.run_cli("crawl", str(cfg), "--no-resource-check",
                                          "--db", self.db, "--warc-root", self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Collection: Default (default)", out)
        placed = run.call_args[0][0]
        self.assertEqual(placed.collection["slug"], "default")
        self.assertEqual(Path(placed.output_dir).parent.parent.name, "default")
        self.assertEqual(Store(self.db).find_collection("default")["name"], "Default")

    def test_a_job_can_make_its_collection_and_is_placed_in_it(self):
        from types import SimpleNamespace

        from webarc.cli import _register_job_in_collection, _settle_registered_job

        args = SimpleNamespace(db=self.db, collection="Fresh", create_collection=True,
                               warc_root=self.root)
        registered, collection, job_dir = _register_job_in_collection(
            args, "job", "crawl", {"seeds": [{"url": "https://a.example/"}]}, seeds_total=1)

        self.assertEqual(collection["slug"], "fresh")
        self.assertEqual(job_dir, self.tmp / "warcs/collections/fresh/jobs" / str(registered[1]))
        self.assertTrue(job_dir.is_dir())
        store = Store(self.db)
        self.assertEqual(store.get_crawl(registered[1])["status"], "running")
        _settle_registered_job(registered, "completed")
        self.assertEqual(store.get_crawl(registered[1])["status"], "completed")
        doc = colls.read_document(collection["root_dir"])
        self.assertEqual([j["id"] for j in doc["jobs"]], [registered[1]])


if __name__ == "__main__":
    unittest.main()


class CreationSafetyTests(unittest.TestCase):
    """A collection exists in the store only once its directory does, its
    root is stored absolute, and its slug is a directory name everywhere."""

    def test_reserved_windows_names_are_not_used_as_directories(self):
        for name in ("con", "NUL", "com1", "Lpt9", "aux.txt"):
            self.assertNotIn(colls.slugify(name).split(".", 1)[0].upper(),
                             colls._WINDOWS_RESERVED, name)
        self.assertEqual(colls.slugify("con"), "c-con")
        self.assertEqual(colls.slugify("Console"), "console")

    def test_a_directory_that_cannot_be_made_leaves_no_row(self):
        from fastapi.testclient import TestClient
        from webarc import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            app = srv.create_app(str(Path(tmp) / "swm.db"), str(Path(tmp) / "warcs"),
                                 simulate=True, replay_root=str(Path(tmp) / "replay"),
                                 monitor_resources=False)
            client = TestClient(app)
            with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
                refused = client.post("/api/collections", json={"name": "QNL"})
            self.assertEqual(refused.status_code, 400, refused.text)
            self.assertEqual(client.get("/api/collections").json(), [])
            made = client.post("/api/collections", json={"name": "QNL"})
            self.assertEqual(made.status_code, 201, made.text)
            self.assertTrue(Path(made.json()["root_dir"]).is_absolute())
            self.assertTrue((Path(made.json()["root_dir"]) / "collection.json").exists())

    def test_the_command_line_stores_an_absolute_root_and_refuses_a_bad_one(self):
        from webarc.cli import _create_collection_row

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "swm.db")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                row = _create_collection_row(store, "QNL", "", [], Path("warcs"))
            finally:
                os.chdir(cwd)
            self.assertTrue(Path(row["root_dir"]).is_absolute())
            self.assertEqual(Path(row["root_dir"]).resolve(),
                             (Path(tmp) / "warcs" / "collections" / "qnl").resolve())
            with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
                with self.assertRaises(ValueError):
                    _create_collection_row(store, "Other", "", [], Path(tmp) / "w")
            self.assertIsNone(store.find_collection("other"))


class CommandLineJobLivenessTests(unittest.TestCase):
    """A job the command line runs in a collection is alive to the dashboard
    while it runs, and ends without a lost-worker note."""

    def test_the_registering_process_is_the_jobs_worker(self):
        from types import SimpleNamespace
        from webarc.cli import _register_job_in_collection, _settle_registered_job
        from webarc import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "swm.db")
            root = Path(tmp) / "collections" / "c"
            cid = store.create_collection("c", "C", "", str(root))
            args = SimpleNamespace(db=str(Path(tmp) / "swm.db"), collection="c",
                                   create_collection=False, warc_root=str(Path(tmp)))
            (store, job), _collection, _dir = _register_job_in_collection(
                args, "j", "crawl", {"seeds": []}, seeds_total=0)
            row = store.get_crawl(job)
            self.assertEqual(row["pid"], os.getpid())
            # The test runner's own command line names neither webarc nor
            # swm; a job run as "swm crawl ..." or "python -m webarc.cli" does.
            with mock.patch.object(srv, "_store", return_value=store), \
                    mock.patch.object(srv, "_pid_is_worker", return_value=True):
                self.assertTrue(srv._worker_alive(row))
                # Long idle, still alive: not settled as lost.
                self.assertEqual(srv._reconcile({**row, "updated_at": "2000-01-01T00:00:00+00:00"})
                                 ["status"], row["status"])
            store.set_status(job, "running", "The worker process is not running")
            _settle_registered_job((store, job), "completed")
            self.assertEqual(store.get_crawl(job)["status"], "completed")
            self.assertFalse(store.get_crawl(job)["error"])

    @unittest.skipIf(os.name == "nt", "the /proc command line is read on POSIX only")
    def test_the_swm_command_counts_as_a_worker(self):
        from webarc import server as srv

        def cmdline(argv):
            return mock.patch.multiple(Path, exists=lambda self: True,
                                       read_bytes=lambda self: b"\0".join(argv))
        with cmdline([b"/venv/bin/python", b"/venv/bin/swm", b"crawl", b"cfg.yaml"]):
            self.assertTrue(srv._pid_is_worker(os.getpid()))
        with cmdline([b"/venv/bin/python", b"-m", b"webarc.worker", b"--crawl-id", b"3"]):
            self.assertTrue(srv._pid_is_worker(os.getpid()))
        with cmdline([b"/usr/bin/python", b"-m", b"http.server"]):
            self.assertFalse(srv._pid_is_worker(os.getpid()))


class EditTests(unittest.TestCase):
    def test_name_description_and_policy_change_in_one_put(self):
        from fastapi.testclient import TestClient
        from webarc import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            app = srv.create_app(str(Path(tmp) / "swm.db"), str(Path(tmp) / "warcs"),
                                 simulate=True, replay_root=str(Path(tmp) / "replay"),
                                 monitor_resources=False)
            client = TestClient(app)
            made = client.post("/api/collections", json={"name": "QNL"}).json()
            changed = client.put(f"/api/collections/{made['id']}", json={
                "name": "QNL 2026", "description": "News sites", "dedup_across_jobs": False})
            self.assertEqual(changed.status_code, 200, changed.text)
            view = client.get(f"/api/collections/{made['id']}").json()
            self.assertEqual((view["name"], view["description"], view["slug"]),
                             ("QNL 2026", "News sites", "qnl"))
            self.assertFalse(view["policy"]["dedup_across_jobs"])
            self.assertEqual(colls.read_document(Path(made["root_dir"]))["name"], "QNL 2026")


class RebuildLockTests(unittest.TestCase):
    def test_no_job_of_the_collection_starts_while_its_index_is_rebuilt(self):
        from fastapi import HTTPException
        from webarc import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            app = srv.create_app(str(Path(tmp) / "swm.db"), str(Path(tmp) / "warcs"),
                                 simulate=True, replay_root=str(Path(tmp) / "replay"),
                                 monitor_resources=False)
            from fastapi.testclient import TestClient
            client = TestClient(app)
            coll = client.post("/api/collections", json={"name": "QNL"}).json()
            job = client.post("/api/crawls", json={
                "name": "j", "start": "wait", "collection_id": coll["id"],
                "config": {"seeds": [{"url": "https://s/"}]}}).json()
            srv._store().set_status(job["id"], "waiting")
            with srv._REBUILD_LOCK:
                srv._REBUILDING.add(coll["id"])
            try:
                with self.assertRaises(HTTPException) as refused:
                    srv._launch(job["id"])
                self.assertEqual(refused.exception.status_code, 409)
                with mock.patch.object(srv, "_launch") as launch:
                    srv._launch_waiting({"cpu": 0, "memory": 0, "disk": {}})
                    launch.assert_not_called()
                again = client.post(f"/api/collections/{coll['id']}/rebuild-index")
                self.assertEqual(again.status_code, 409)
            finally:
                with srv._REBUILD_LOCK:
                    srv._REBUILDING.discard(coll["id"])
            self.assertEqual(srv._store().get_crawl(job["id"])["status"], "waiting")
            srv._store().set_status(job["id"], "pending")               # launched, not yet running
            pending = client.post(f"/api/collections/{coll['id']}/rebuild-index")
            self.assertEqual(pending.status_code, 409, pending.text)


class CollectionReplayTests(ServerTestCase):
    def test_the_collection_replay_opens_at_the_start_pages(self):
        from unittest import mock
        coll = self.client.post("/api/collections", json={"name": "QNL"}).json()
        for name, url in (("first", "https://a.example/"), ("second", "https://b.example/x")):
            job = self.client.post("/api/crawls", json={
                "name": name, "start": "wait", "collection_id": coll["id"],
                "config": {"operator": "o", "seeds": [{"url": url}]}}).json()
            (Path(job["output_dir"]) / f"{name}.warc.gz").write_bytes(b"\x1f\x8bxx")
        with mock.patch("webarc.replay.build_replay_site", return_value=self.tmp / "site") as build, \
                mock.patch("webarc.replay.build_start_page") as start, \
                mock.patch("webarc.replay.ReplayServer") as server_cls:
            server_cls.return_value.is_running.return_value = True
            server_cls.return_value.replay_url.side_effect = (
                lambda coll, page="index.html": f"http://replay/{coll}/{page}")
            srv._PYWB = server_cls.return_value
            try:
                response = self.client.post(f"/api/collections/{coll['id']}/replay")
            finally:
                srv._PYWB = None
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["replay_url"], "http://replay/collection-qnl/seeds.html")
        self.assertEqual(response.json()["start_pages"], 2)
        self.assertEqual(len(build.call_args[0][0]), 2)
        entries = start.call_args[0][2]
        self.assertEqual([e["url"] for e in entries], ["https://b.example/x", "https://a.example/"])
        self.assertEqual(start.call_args[0][1], "QNL")
