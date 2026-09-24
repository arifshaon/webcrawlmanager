"""Collections: named containers that jobs belong to.

A collection is the unit a curator thinks in -- "the 2026 election sites",
"the library's own channels" -- and the unit a later capture is compared
against. Each has a directory of its own under the storage root, with a
``collection.json`` describing it and a ``jobs/`` folder that every job run
against it is placed in, so where a job's files are is never in doubt.

A collection carries descriptive metadata of the same shape as a job's (the
Dublin Core elements plus Collector, repeatable, with custom fields). A job
in a collection inherits those values for every element it does not set
itself, the way a seed inherits its job's, and every job in a collection
carries a ``Relation`` naming it, so a WARC that leaves the folder still
says which collection it came from.

Deleting a collection or one of its jobs is allowed, with the consequences
stated first. Until cross-job deduplication exists nothing refers into a
job from outside it, and the impact report says so; once it does, the same
report names the jobs and records that would lose their originals.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import metadata as md

SCHEMA = "swm-collection/1"
COLLECTIONS_DIR = "collections"
JOBS_DIR = "jobs"
DOCUMENT_NAME = "collection.json"
MAX_NAME = 200
MAX_DESCRIPTION = 5000
MAX_SLUG = 80

# The custom field every job in a collection carries, beside Relation.
COLLECTION_FIELD = "Collection"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- names ----------------------------------------------------------------

def slugify(name: str) -> str:
    """A directory-safe, stable identifier for a collection name.

    Letters and digits in any script are kept; everything else becomes a
    hyphen. The slug is fixed at creation and never changes with a rename,
    because it is written into WARC records and directory paths.
    """
    text = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    text = text[:MAX_SLUG].rstrip("-_")
    return text or "collection"


def validate_name(name: object) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("a collection needs a name")
    if len(text) > MAX_NAME:
        raise ValueError(f"a collection name must be {MAX_NAME} characters or fewer")
    return text


def validate_description(description: object) -> str:
    text = str(description or "").strip()
    if len(text) > MAX_DESCRIPTION:
        raise ValueError(
            f"a collection description must be {MAX_DESCRIPTION} characters or fewer")
    return text


# --- places ---------------------------------------------------------------

def collection_root(base: Path, slug: str) -> Path:
    """Where a collection lives under a storage root."""
    return Path(base) / COLLECTIONS_DIR / slug


def job_home(root_dir: Path | str, crawl_id: int | str) -> Path:
    """Where a job of this collection is placed."""
    return Path(root_dir) / JOBS_DIR / str(crawl_id)


# --- metadata -------------------------------------------------------------

DEFAULT_POLICY = {"dedup_across_jobs": True}


def policy_of(collection: Optional[dict]) -> dict:
    """The collection's policy with defaults filled in."""
    raw = (collection or {}).get("policy") or {}
    return {**DEFAULT_POLICY, **{k: v for k, v in raw.items() if k in DEFAULT_POLICY}}


def brief(collection: Optional[dict]) -> Optional[dict]:
    """The few facts about a collection that travel with a job."""
    if not collection:
        return None
    return {"id": collection.get("id"), "slug": collection.get("slug"),
            "name": collection.get("name"),
            "root_dir": str(collection.get("root_dir") or ""),
            "dedup_across_jobs": policy_of(collection)["dedup_across_jobs"]}


def open_index(collection: Optional[dict]):
    """The collection's payload index, when the collection deduplicates
    across jobs; None otherwise. Takes a full row or a brief."""
    if not collection or not collection.get("root_dir"):
        return None
    dedup = collection.get("dedup_across_jobs")
    if dedup is None:
        dedup = policy_of(collection)["dedup_across_jobs"]
    if not dedup:
        return None
    from .dedup_index import CollectionIndex
    try:
        return CollectionIndex.for_collection(collection["root_dir"])
    except Exception:
        return None


def read_index(collection: Optional[dict]):
    """The index if the collection has one on disk, for reading; None
    otherwise. Never creates the file."""
    if not collection or not collection.get("root_dir"):
        return None
    from .dedup_index import CollectionIndex
    path = CollectionIndex.path_for(collection["root_dir"])
    if not path.exists():
        return None
    try:
        return CollectionIndex(path)
    except Exception:
        return None


def inherited_fields(collection: Optional[dict]) -> list[dict]:
    """The metadata a job takes from its collection.

    The collection's own fields, plus a Relation naming the collection and
    a Collection field carrying its slug. A job's own value for any element
    replaces the inherited one, as a seed's replaces its job's.
    """
    if not collection:
        return []
    fields = md.normalise_fields(collection.get("metadata") or [])
    name = str(collection.get("name") or "").strip()
    slug = str(collection.get("slug") or "").strip()
    if name:
        fields.append({"name": "Relation", "value": f"isPartOf: {name}"})
    if slug:
        fields.append({"name": COLLECTION_FIELD, "value": slug})
    return fields


def effective_job_fields(collection: Optional[dict],
                         job_fields: list[dict]) -> list[dict]:
    """A job's fields with the collection's underneath them."""
    return md.merge(inherited_fields(collection), list(job_fields or []))


def load_metadata_argument(json_text: Optional[str],
                           file_path: Optional[str]) -> list[dict]:
    """Collection metadata from a command line: a JSON array, or a file.

    The file may be JSON (an array of fields, or an object with a ``job``
    block) or a metadata sheet as the dashboard exports one, whose job-level
    row is taken.
    """
    if json_text and file_path:
        raise ValueError("give the metadata as JSON or as a file, not both")
    if json_text:
        try:
            raw = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"the metadata is not valid JSON: {exc}") from exc
        return _fields_from_loaded(raw)
    if file_path:
        path = Path(file_path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"could not read {path}: {exc}") from exc
        if path.suffix.lower() == ".csv":
            parsed = md.parse_csv(text)
            return md.normalise_fields(parsed.get("job") or [])
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        return _fields_from_loaded(raw)
    return []


def _fields_from_loaded(raw) -> list[dict]:
    if isinstance(raw, dict) and ("job" in raw or "seeds" in raw):
        return md.normalise_fields(raw.get("job") or [])
    return md.normalise_fields(raw)


# --- the document ---------------------------------------------------------

def document(collection: dict, jobs: Iterable[dict] = (),
             existing: Optional[dict] = None) -> dict:
    """The collection.json for a collection: what it is, and what is in it."""
    stamp = _now()
    written = (existing or {}).get("written_at") or collection.get("created_at") or stamp
    return {
        "schema": SCHEMA,
        "id": collection.get("id"),
        "slug": collection.get("slug"),
        "name": collection.get("name"),
        "description": collection.get("description") or "",
        "root_dir": str(collection.get("root_dir") or ""),
        "written_at": written,
        "updated_at": stamp,
        "elements": list(md.ELEMENTS),
        "metadata": md.normalise_fields(collection.get("metadata") or []),
        "policy": policy_of(collection),
        "inherited_by_jobs": inherited_fields(collection),
        "jobs": [
            {"id": job.get("id"), "name": job.get("name"),
             "kind": job.get("kind", "crawl"), "status": job.get("status"),
             "created_at": job.get("created_at"),
             "output_dir": str(job.get("output_dir") or "")}
            for job in jobs
        ],
        "note": (
            "Jobs run against this collection are placed under jobs/ here. "
            "Each inherits the collection's metadata for every element it "
            "does not set itself, and carries a Relation naming the "
            "collection. This file is current; a WARC written at capture "
            "time keeps the values of that moment."
        ),
    }


def write_document(root_dir: Path | str, doc: dict) -> Path:
    root = Path(root_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / DOCUMENT_NAME
    temporary = target.with_name(DOCUMENT_NAME + ".tmp")
    temporary.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, target)
    return target


def refresh_document(store, collection: Optional[dict]) -> None:
    """Rewrite collection.json with the collection's jobs as they now are.

    Called wherever a job's place or state changes: when it is created,
    when it ends, when it is deleted. A document that only knew jobs at
    their creation listed a finished job as pending for ever.
    """
    if not collection:
        return
    row = store.get_collection(collection["id"]) or collection
    root = Path(row["root_dir"])
    try:
        write_document(root, document(
            row, store.crawls_in_collection(row["id"]),
            existing=read_document(root)))
    except OSError as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Could not write collection.json for %s: %s", row.get("slug"), exc)


def read_document(root_dir: Path | str) -> Optional[dict]:
    path = Path(root_dir) / DOCUMENT_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --- what a deletion would do --------------------------------------------

_NO_DEDUP_NOTE = (
    "This collection does not deduplicate across jobs, so no other job's "
    "records refer into this one and nothing else will lose content."
)


def job_impact(collection: Optional[dict], row: dict,
               siblings: Iterable[dict] = (),
               referring: Optional[dict] = None) -> dict:
    """What deleting one job means for the rest of its collection.

    ``referring`` is the index's answer -- which jobs hold revisit records
    pointing at this job's originals, and how many -- looked up by the
    caller. Those pages will replay without their content once this job
    is gone, until they are crawled again.
    """
    by_id = {job.get("id"): job for job in siblings}
    later = [
        {"id": job.get("id"), "name": job.get("name"), "kind": job.get("kind", "crawl")}
        for job in siblings
        if job.get("id") != row.get("id")
        and str(job.get("created_at") or "") >= str(row.get("created_at") or "")
    ]
    jobs = (referring or {}).get("jobs") or {}
    referring_jobs = [
        {"id": job_id, "name": (by_id.get(job_id) or {}).get("name"),
         "records": count}
        for job_id, count in sorted(jobs.items())]
    records = int((referring or {}).get("records") or sum(jobs.values()))
    if not collection:
        note = "This job is not in a collection; nothing else refers to it."
    elif not policy_of(collection)["dedup_across_jobs"]:
        note = _NO_DEDUP_NOTE
    elif records:
        note = (f"{len(referring_jobs)} later job(s) hold {records} record(s) that "
                "refer into this job for their content. After deletion those pages "
                "replay without it until they are crawled again; the collection "
                "lists them as missing their originals.")
    else:
        note = ("No other job's records refer into this one; nothing else in the "
                "collection loses content.")
    return {
        "job": {"id": row.get("id"), "name": row.get("name"),
                "kind": row.get("kind", "crawl"), "status": row.get("status")},
        "collection": brief(collection),
        "later_jobs_in_collection": later,
        "referring_jobs": referring_jobs,
        "referring_records": records,
        "breaks_replay_elsewhere": records > 0,
        "note": note,
    }


def collection_impact(collection: dict, jobs: Iterable[dict],
                      bytes_on_disk: int = 0,
                      running: Iterable[int] = ()) -> dict:
    """What deleting a whole collection means."""
    jobs = list(jobs)
    running = list(running)
    by_status: dict[str, int] = {}
    for job in jobs:
        key = str(job.get("status") or "pending")
        by_status[key] = by_status.get(key, 0) + 1
    return {
        "collection": brief(collection),
        "root_dir": str(collection.get("root_dir") or ""),
        "jobs": [{"id": j.get("id"), "name": j.get("name"),
                  "kind": j.get("kind", "crawl"), "status": j.get("status")}
                 for j in jobs],
        "job_count": len(jobs),
        "by_status": by_status,
        "running_jobs": running,
        "bytes_on_disk": int(bytes_on_disk),
        "note": (
            "Deleting the collection removes every job listed here from the "
            "dashboard. With purge, their files under the collection's "
            "directory are deleted from disk as well; without it, the files "
            "stay where they are but nothing lists them any more."
        ),
    }


def describe_impact(impact: dict) -> str:
    """The impact as a paragraph, for a confirmation prompt."""
    if "job_count" in impact:
        name = (impact.get("collection") or {}).get("name") or "this collection"
        lines = [f"Deleting collection \"{name}\" removes {impact['job_count']} job(s)"]
        if impact.get("by_status"):
            lines.append("(" + ", ".join(f"{n} {s}" for s, n in
                                         sorted(impact["by_status"].items())) + ")")
        if impact.get("running_jobs"):
            lines.append(f"of which {len(impact['running_jobs'])} still running.")
        if impact.get("bytes_on_disk"):
            lines.append(f"Files on disk: {impact['bytes_on_disk'] / (1024 * 1024):.1f} MB.")
        return " ".join(lines)
    job = impact.get("job") or {}
    parts = [f"Deleting job #{job.get('id')} \"{job.get('name')}\"."]
    if impact.get("referring_records"):
        parts.append(
            f"{len(impact['referring_jobs'])} later job(s) hold "
            f"{impact['referring_records']} record(s) that refer into it; "
            "those pages will replay without their content until re-crawled.")
    else:
        parts.append(impact.get("note") or "")
    return " ".join(p for p in parts if p)
