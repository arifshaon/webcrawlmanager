"""Run the warc-indexer jar on a job's WARC files, from the dashboard or
the command line.

The jar is the SWM fork of netarchivesuite/warc-indexer kept under
``warc-indexer/`` in the repository and built there (see its README-SWM.md).
It is a separate GPL-2 program: SWM launches it as a subprocess and reads
what it writes, the same arrangement SWM has with gallery-dl.

For each WARC the jar writes ``<warc file name>.jsonl`` beside the WARC,
one document per record in warc-indexer's schema. This module records the
run in ``warc-index-manifest.json`` in the same folder (status, outputs,
document counts, the command, where the log is) and keeps the jar's
output in ``warc-index.log``.

Where things are found, in order:

- Java: ``SWM_JAVA``, then ``JAVA_HOME/bin/java``, then ``java`` on PATH.
- The jar: ``SWM_WARC_INDEXER_JAR``, then the newest
  ``warc-indexer-*-jar-with-dependencies.jar`` under ``warc-indexer/target``
  next to this package.
- The configuration: ``SWM_WARC_INDEXER_CONF``, then
  ``config/swm-indexer.conf`` beside the jar's ``target`` folder.

``SWM_WARC_INDEXER_CMD`` (a JSON list) replaces the ``java -Xmx.. -jar JAR``
prefix altogether, for wrappers and tests.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("webarc.warc_indexer")

JAR_ENV = "SWM_WARC_INDEXER_JAR"
CONF_ENV = "SWM_WARC_INDEXER_CONF"
JAVA_ENV = "SWM_JAVA"
CMD_ENV = "SWM_WARC_INDEXER_CMD"

MANIFEST_NAME = "warc-index-manifest.json"
LOG_NAME = "warc-index.log"
DEFAULT_MEMORY = "2g"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class WarcIndexerUnavailable(Exception):
    """The jar or Java cannot be found, or there is nothing to index."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- finding the pieces ------------------------------------------------------

def find_java() -> Optional[str]:
    explicit = os.environ.get(JAVA_ENV)
    if explicit and Path(explicit).is_file():
        return explicit
    home = os.environ.get("JAVA_HOME")
    if home:
        candidate = Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("java")


def java_version(java: Optional[str] = None) -> Optional[str]:
    """The first line ``java -version`` prints, or None when it cannot run."""
    java = java or find_java()
    if not java:
        return None
    try:
        done = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [l.strip() for l in (done.stderr or done.stdout or "").splitlines() if l.strip()]
    # the version line proper; a JVM may print JAVA_TOOL_OPTIONS first
    return next((l for l in lines if "version" in l.lower()), lines[0] if lines else None)


def find_jar() -> Optional[Path]:
    explicit = os.environ.get(JAR_ENV)
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    target = _repo_root() / "warc-indexer" / "target"
    jars = sorted(target.glob("warc-indexer-*-jar-with-dependencies.jar"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return jars[0] if jars else None


def find_config(jar: Optional[Path] = None) -> Optional[Path]:
    explicit = os.environ.get(CONF_ENV)
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    candidates = []
    if jar:
        candidates.append(jar.resolve().parent.parent / "config" / "swm-indexer.conf")
    candidates.append(_repo_root() / "warc-indexer" / "config" / "swm-indexer.conf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def command_override() -> Optional[list[str]]:
    raw = os.environ.get(CMD_ENV)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return [str(p) for p in parsed] if isinstance(parsed, list) and parsed else None


def capability() -> dict:
    """Whether the jar can be run here, and with what."""
    override = command_override()
    if override:
        return {"available": True, "reason": None, "jar": None, "java": None,
                "java_version": None, "config": str(find_config() or "") or None,
                "command": override}
    java = find_java()
    jar = find_jar()
    config = find_config(jar)
    notes = []
    if not java:
        notes.append("Java was not found. Install Java 11 or newer, or point SWM_JAVA at it.")
    if not jar:
        notes.append("The warc-indexer jar was not found. Build it with "
                     "`mvnw -DskipTests package` in the repository's warc-indexer folder, "
                     "or point SWM_WARC_INDEXER_JAR at a built jar.")
    if jar and not config:
        notes.append("config/swm-indexer.conf was not found beside the jar; the jar's "
                     "built-in defaults would be used, which index request records too.")
    return {"available": bool(java and jar),
            "reason": " ".join(n for n in notes if "config" not in n) or None,
            "note": next((n for n in notes if "config" in n), None),
            "jar": str(jar) if jar else None,
            "java": java, "java_version": java_version(java) if java and jar else None,
            "config": str(config) if config else None}


# --- the run ---------------------------------------------------------------------

def warc_files(crawl_dir: Path) -> list[Path]:
    crawl_dir = Path(crawl_dir)
    return sorted(crawl_dir.glob("*.warc.gz")) + sorted(crawl_dir.glob("*.warc"))


def index_path_for(warc: Path) -> Path:
    """Where the jar writes a WARC's documents: beside it, ``<name>.jsonl``."""
    return warc.parent / (warc.name + ".jsonl")


def build_command(out_dir: Path, warcs: Iterable[Path], *, collection: Optional[str] = None,
                  java: Optional[str] = None, jar: Optional[Path] = None,
                  config: Optional[Path] = None, memory: str = DEFAULT_MEMORY) -> list[str]:
    prefix = command_override()
    if not prefix:
        java = java or find_java()
        jar = jar or find_jar()
        if not java or not jar:
            raise WarcIndexerUnavailable(capability()["reason"] or "warc-indexer is unavailable")
        prefix = [java, f"-Xmx{memory}", "-jar", str(jar)]
    config = config or find_config(jar)
    cmd = list(prefix)
    if config:
        cmd += ["-c", str(config)]
    cmd += ["-o", str(out_dir), "-F", "jsonl"]
    if collection:
        cmd += ["--collection", collection]
    cmd += [str(w) for w in warcs]
    return cmd


def read_manifest(crawl_dir: Path) -> Optional[dict]:
    path = Path(crawl_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_manifest(crawl_dir: Path, manifest: dict) -> None:
    path = Path(crawl_dir) / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:                                   # noqa: BLE001
        return False


def is_running(crawl_dir: Path) -> bool:
    manifest = read_manifest(crawl_dir)
    return bool(manifest and manifest.get("status") == STATUS_RUNNING
                and _pid_alive(manifest.get("pid")))


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def index_warcs(crawl_dir: Path, *, warcs: Optional[Iterable[str | Path]] = None,
                collection: Optional[str] = None, memory: str = DEFAULT_MEMORY,
                timeout: Optional[float] = None) -> dict:
    """Run the jar on a job's WARC files, or on the named ones, writing each
    ``<warc>.jsonl`` beside its WARC. Blocks until the jar exits; the
    dashboard calls this on a thread. Returns the manifest it wrote."""
    crawl_dir = Path(crawl_dir)
    if warcs:
        chosen = []
        for item in warcs:
            path = Path(item)
            if not path.is_absolute():
                path = crawl_dir / path
            if not path.is_file():
                raise WarcIndexerUnavailable(f"{path.name} is not a WARC file in this job's folder")
            chosen.append(path)
    else:
        chosen = warc_files(crawl_dir)
    if not chosen:
        raise WarcIndexerUnavailable("this job has no WARC files to index")
    command = build_command(crawl_dir, chosen, collection=collection, memory=memory)
    manifest = {
        "schema": "swm-warc-index-run/1",
        "status": STATUS_RUNNING,
        "started_at": _iso_now(),
        "collection": collection,
        "command": command,
        "log": str(crawl_dir / LOG_NAME),
        "warcs": [w.name for w in chosen],
        "outputs": [],
        "documents": 0,
    }
    log_path = crawl_dir / LOG_NAME
    with log_path.open("wb") as log_file:
        try:
            proc = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT,
                                    cwd=str(crawl_dir))
        except OSError as exc:
            manifest.update({"status": STATUS_FAILED, "error": f"could not start: {exc}",
                             "finished_at": _iso_now()})
            _write_manifest(crawl_dir, manifest)
            return manifest
        manifest["pid"] = proc.pid
        _write_manifest(crawl_dir, manifest)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            manifest.update({"status": STATUS_FAILED, "finished_at": _iso_now(),
                             "error": f"the indexer ran longer than {timeout} seconds and was stopped"})
            _write_manifest(crawl_dir, manifest)
            return manifest
    outputs = []
    for warc in chosen:
        out = index_path_for(warc)
        outputs.append({"warc": warc.name, "index": str(out),
                        "documents": _count_lines(out) if out.is_file() else None})
    manifest["outputs"] = outputs
    manifest["documents"] = sum(o["documents"] or 0 for o in outputs)
    manifest["exit_code"] = code
    manifest["finished_at"] = _iso_now()
    missing = [o["warc"] for o in outputs if o["documents"] is None]
    if code != 0:
        manifest["status"] = STATUS_FAILED
        manifest["error"] = f"the indexer exited with code {code}; see {LOG_NAME}"
    elif missing:
        manifest["status"] = STATUS_FAILED
        manifest["error"] = "no index file was written for: " + ", ".join(missing)
    else:
        manifest["status"] = STATUS_DONE
    manifest.pop("pid", None)
    _write_manifest(crawl_dir, manifest)
    return manifest


def summary(crawl_dir: Path) -> Optional[dict]:
    """What the dashboard shows about the last run."""
    manifest = read_manifest(crawl_dir)
    if not manifest:
        return None
    status = manifest.get("status")
    if status == STATUS_RUNNING and not _pid_alive(manifest.get("pid")):
        status = STATUS_FAILED               # the process went away without finishing
    return {"status": status, "documents": manifest.get("documents"),
            "warcs": len(manifest.get("warcs") or []),
            "outputs": [o.get("index") for o in manifest.get("outputs") or []],
            "started_at": manifest.get("started_at"), "finished_at": manifest.get("finished_at"),
            "error": manifest.get("error"), "collection": manifest.get("collection")}


if __name__ == "__main__":                              # pragma: no cover
    print(json.dumps(capability(), indent=2))
    sys.exit(0)
