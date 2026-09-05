"""Descriptive metadata for a capture: what was collected, by whom, and why.

The model follows Archive-It's seed-level metadata: the fifteen Dublin
Core 1.1 elements plus Collector, every element repeatable, custom fields
allowed, and two levels -- the job as a whole, and each seed or target,
whose own values replace the job's for that element and add to the rest.

A capture's metadata is written three ways: metadata.json in the job's
folder (the record of truth, editable later), a metadata record beside the
warcinfo in each WARC file (so a file that leaves the folder still says
what it is), and a section in the Facebook or Instagram manifest. It can
be exported and imported as a spreadsheet in Archive-It's one-row-per-seed
shape.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ELEMENTS = ["Title", "Creator", "Subject", "Description", "Publisher",
            "Contributor", "Date", "Type", "Format", "Identifier", "Source",
            "Language", "Relation", "Coverage", "Rights", "Collector"]
DUBLIN_CORE = frozenset(ELEMENTS[:15])
_CANONICAL = {name.lower(): name for name in ELEMENTS}

MAX_NAME = 100
MAX_VALUE = 5000
MAX_FIELDS = 500
DOCUMENT_NAME = "metadata.json"
CSV_NAME = "metadata.csv"
SCHEMA = "swm-capture-metadata/1"
JOB_ROW = "*"          # the seed_url a spreadsheet row uses for job-level values


# --- fields --------------------------------------------------------------

def canonical_name(name: str) -> str:
    """A Dublin Core element written in any case is that element."""
    text = " ".join(str(name).split())
    return _CANONICAL.get(text.lower(), text)


def normalise_fields(raw) -> list[dict]:
    """A list of {"name", "value"} pairs from any reasonable shape.

    Accepts a list of pairs (dicts or two-item lists), or a mapping of name
    to a value or list of values. Blank values are dropped; names are
    canonicalised; order is kept. Raises ValueError for anything else.
    """
    if raw is None or raw == "":
        return []
    items: list[tuple[object, object]] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            items.extend((name, v) for v in values)
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, dict):
                items.append((entry.get("name", entry.get("element")), entry.get("value")))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                items.append((entry[0], entry[1]))
            else:
                raise ValueError("each metadata field needs a name and a value")
    else:
        raise ValueError("metadata fields must be a list of name/value pairs")
    out: list[dict] = []
    for name, value in items:
        if name is None or value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            raise ValueError("a metadata value must be text")
        name_text = canonical_name(name)
        value_text = " ".join(str(value).split())
        if not name_text or not value_text:
            continue
        if len(name_text) > MAX_NAME:
            raise ValueError(f"metadata field name is too long: {name_text[:40]}…")
        if len(value_text) > MAX_VALUE:
            raise ValueError(f"the value for {name_text} is too long ({MAX_VALUE} characters at most)")
        out.append({"name": name_text, "value": value_text})
    if len(out) > MAX_FIELDS:
        raise ValueError(f"too many metadata fields ({MAX_FIELDS} at most)")
    return out


def normalise(raw, seeds: list[str] | None = None) -> dict:
    """{"job": [...], "seeds": {url: [...]}} from a request or a config.

    ``raw`` may be that shape already, or a bare field list meaning job
    level. Seed keys are kept as given; with ``seeds`` named, keys that are
    not one of them are dropped, since nothing would ever be written for
    them.
    """
    if raw is None or raw == "":
        return {"job": [], "seeds": {}}
    if isinstance(raw, dict) and ("job" in raw or "seeds" in raw):
        job = normalise_fields(raw.get("job"))
        seeds_raw = raw.get("seeds") or {}
        if not isinstance(seeds_raw, dict):
            raise ValueError("metadata.seeds must map each seed to its fields")
        per_seed = {}
        for url, fields in seeds_raw.items():
            key = str(url).strip()
            if seeds is not None and key not in seeds:
                continue
            normalised = normalise_fields(fields)
            if normalised:
                per_seed[key] = normalised
        return {"job": job, "seeds": per_seed}
    return {"job": normalise_fields(raw), "seeds": {}}


def from_config(raw: dict) -> dict:
    """The metadata a stored or YAML config carries.

    Job-level fields live under ``metadata`` (either shape); a seed entry
    may carry its own ``metadata`` block, which counts for that seed.
    """
    seeds = [str(s.get("url")) for s in raw.get("seeds", []) if isinstance(s, dict)]
    meta = normalise(raw.get("metadata"), seeds=None)
    for seed in raw.get("seeds", []):
        if isinstance(seed, dict) and seed.get("metadata") is not None:
            fields = normalise_fields(seed["metadata"])
            if fields:
                meta["seeds"][str(seed.get("url"))] = fields
    meta["seeds"] = {k: v for k, v in meta["seeds"].items() if k in seeds}
    return meta


def merge(job: list[dict], seed: list[dict] | None) -> list[dict]:
    """The seed's values replace the job's for an element it names; the
    rest come from the job. Elements first in standard order, then custom
    names in the order they appear."""
    seed = seed or []
    named = {f["name"] for f in seed}
    combined = list(seed) + [f for f in job if f["name"] not in named]
    order = {name: i for i, name in enumerate(ELEMENTS)}
    first_seen: dict[str, int] = {}
    for i, f in enumerate(combined):
        first_seen.setdefault(f["name"], i)
    return sorted(combined, key=lambda f: (order.get(f["name"], len(ELEMENTS)),
                                           first_seen[f["name"]]))


def with_defaults(fields: list[dict], defaults: list[dict]) -> list[dict]:
    """Fill elements the curator left empty from what the capture knows."""
    present = {f["name"] for f in fields}
    return merge(list(fields), [d for d in defaults if d["name"] not in present
                                and d.get("value")])


TYPE_BY_KIND = {
    "crawl": "Website",
    "recording": "Website",
    "facebook": "Social media account",
    "instagram": "Social media account",
    "x": "Social media account",
}


def defaults_for(kind: str, name: str, operator: str, seed_url: str | None,
                 when: datetime | None = None) -> list[dict]:
    """Sensible values for Title, Identifier, Date, Type and Collector.

    Never invented: each is something the capture already knows.
    """
    when = when or datetime.now(timezone.utc)
    out = [{"name": "Title", "value": str(name or "").strip()},
           {"name": "Date", "value": when.strftime("%Y-%m-%d")},
           {"name": "Type", "value": TYPE_BY_KIND.get(kind, "Website")},
           {"name": "Collector", "value": str(operator or "").strip()}]
    if seed_url:
        out.insert(1, {"name": "Identifier", "value": str(seed_url)})
    return [f for f in out if f["value"]]


# --- WARC ----------------------------------------------------------------

def warc_field_name(name: str) -> str:
    """The name a field takes in a WARC metadata record."""
    if name in DUBLIN_CORE:
        return f"dc.{name.lower()}"
    if name == "Collector":
        return "collector"
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.lower()).strip("-")
    return f"custom.{slug or 'field'}"


def warc_fields_text(fields: list[dict]) -> bytes:
    """An application/warc-fields body describing a seed."""
    lines = [f"{warc_field_name(f['name'])}: {f['value']}" for f in fields]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# --- the document on disk -------------------------------------------------

def document(*, job_id, kind: str, name: str, operator: str,
             seeds: list[dict], metadata: dict,
             when: datetime | None = None, existing: dict | None = None) -> dict:
    """The metadata.json for one job.

    ``seeds`` is a list of {"url", "label"?}. Each seed's ``effective``
    fields are what the outputs carry: its own values over the job's, with
    defaults for anything still empty. ``existing`` keeps the first
    written_at and the WARC-time record across later edits.
    """
    when = when or datetime.now(timezone.utc)
    stamp = when.isoformat(timespec="seconds")
    job_fields = list(metadata.get("job", []))
    seed_docs = []
    for seed in seeds:
        url = str(seed.get("url") or "")
        own = list(metadata.get("seeds", {}).get(url, []))
        effective = with_defaults(merge(job_fields, own),
                                  defaults_for(kind, name, operator, url, when))
        entry = {"url": url, "fields": own, "effective": effective}
        if seed.get("label"):
            entry["label"] = seed["label"]
        seed_docs.append(entry)
    written = (existing or {}).get("written_at") or stamp
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "kind": kind,
        "name": name,
        "written_at": written,
        "updated_at": stamp,
        "elements": list(ELEMENTS),
        "job": job_fields,
        "seeds": seed_docs,
        "note": ("Each seed's 'effective' fields are what the capture's outputs "
                 "carry: the seed's own values over the job's, with Title, "
                 "Identifier, Date, Type and Collector filled from the capture "
                 "where left empty. A WARC written at capture time keeps the "
                 "values of that moment; this file is current."),
    }


def write_document(directory: Path, doc: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DOCUMENT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_document(directory: Path) -> dict | None:
    path = Path(directory) / DOCUMENT_NAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


MANIFEST_NAMES = ("facebook-manifest.json", "instagram-manifest.json",
                  "x-manifest.json")


def manifest_section(directory: Path, doc: dict | None = None) -> dict | None:
    """The part of metadata.json a capture manifest repeats."""
    doc = doc or read_document(directory)
    if not doc:
        return None
    return {
        "schema": doc.get("schema"),
        "updated_at": doc.get("updated_at"),
        "job": doc.get("job", []),
        "seeds": [{"url": s.get("url"), "label": s.get("label"),
                   "effective": s.get("effective", [])} for s in doc.get("seeds", [])],
    }


def describe_rows(section: dict | None, seed_url: str | None = None) -> list[tuple[str, str]]:
    """(label, value) pairs for a reader page: one seed's effective fields,
    or the first seed's when none is named. Repeated elements join with
    a semicolon."""
    if not section:
        return []
    seeds = section.get("seeds") or []
    chosen = next((s for s in seeds if seed_url and s.get("url") == seed_url), None) \
        or (seeds[0] if seeds else None)
    fields = (chosen or {}).get("effective") or section.get("job") or []
    grouped: dict[str, list[str]] = {}
    for f in fields:
        grouped.setdefault(f["name"], []).append(f["value"])
    return [(name, "; ".join(values)) for name, values in grouped.items()]


def update_manifest(directory: Path, doc: dict) -> bool:
    """Put the current metadata into a social capture's manifest, if any."""
    for manifest_name in MANIFEST_NAMES:
        path = Path(directory) / manifest_name
        if not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(manifest, dict):
            return False
        manifest["metadata"] = manifest_section(directory, doc)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    return False


# --- spreadsheet ---------------------------------------------------------

def csv_text(doc: dict) -> str:
    """One row per seed, Archive-It style: a column per value, repeated
    columns for repeated elements, and a first row (seed_url "*") for the
    job-level values so the sheet round-trips."""
    rows: list[tuple[str, list[dict]]] = []
    if doc.get("job"):
        rows.append((JOB_ROW, doc["job"]))
    for seed in doc.get("seeds", []):
        rows.append((seed.get("url", ""), seed.get("fields") or []))
    counts: dict[str, int] = {}
    for _, fields in rows:
        seen: dict[str, int] = {}
        for f in fields:
            seen[f["name"]] = seen.get(f["name"], 0) + 1
        for name, n in seen.items():
            counts[name] = max(counts.get(name, 0), n)
    order = {name: i for i, name in enumerate(ELEMENTS)}
    names = sorted(counts, key=lambda n: (order.get(n, len(ELEMENTS)), n))
    header = ["seed_url"] + [name for name in names for _ in range(counts[name])]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for url, fields in rows:
        cells: dict[str, list[str]] = {}
        for f in fields:
            cells.setdefault(f["name"], []).append(f["value"])
        row = [url]
        for name in names:
            values = cells.get(name, [])
            row.extend(values + [""] * (counts[name] - len(values)))
        writer.writerow(row)
    return out.getvalue()


def parse_csv(text: str) -> dict:
    """The {"job", "seeds"} shape from a sheet in csv_text's layout.

    A row whose seed_url is "*" or empty is the job level. Column names are
    field names; a repeated column repeats the element.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {"job": [], "seeds": {}}
    header = [h.strip().lstrip("﻿") for h in header]
    if not header or header[0].lower() not in ("seed_url", "seed", "url"):
        raise ValueError("the first column must be seed_url")
    job: list[dict] = []
    seeds: dict[str, list[dict]] = {}
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        url = (row[0] if row else "").strip()
        fields = [{"name": header[i], "value": row[i]}
                  for i in range(1, min(len(header), len(row))) if header[i].strip()]
        fields = normalise_fields(fields)
        if url in ("", JOB_ROW):
            job.extend(fields)
        elif fields:
            seeds.setdefault(url, []).extend(fields)
    return {"job": job, "seeds": seeds}
