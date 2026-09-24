"""Run the warc-indexer jar on a job's WARC files, from the dashboard or
the command line.

The jar is the SWM fork of netarchivesuite/warc-indexer kept under
``warc-indexer/`` in the repository and built there (see its README-SWM.md).
It is a separate GPL-2 program: SWM launches it as a subprocess and reads
what it writes, the same arrangement SWM has with gallery-dl.

For each WARC the jar writes ``<warc file name>.jsonl`` beside the WARC,
one document per record in warc-indexer's schema. This module records the
run in ``warc-index-manifest.json`` in the same folder (status, progress
while it goes, outputs, document counts, the command, the error and the
tail of the log when it fails) and keeps the jar's output in
``warc-index.log``.

Where things are found, in order, each stopping at the first that exists:

- Java: the ``indexer.java`` setting (a JAVA_HOME folder or the java
  executable), ``SWM_JAVA``, ``JAVA_HOME``, then ``java`` on PATH.
- The jar: the ``indexer.jar`` setting, ``SWM_WARC_INDEXER_JAR``, then the
  newest ``warc-indexer-*-jar-with-dependencies.jar`` under
  ``warc-indexer/target`` next to this package.
- The configuration: the ``indexer.config`` setting, ``SWM_WARC_INDEXER_CONF``,
  then ``config/swm-indexer.conf`` beside the jar's ``target`` folder.
- Java heap: the ``indexer.memory`` setting, else 2g.

The settings live in the dashboard's store (Settings › Indexer).
``SWM_WARC_INDEXER_CMD`` (a JSON list) replaces the ``java -Xmx.. -jar JAR``
prefix altogether, for wrappers and tests.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("webarc.warc_indexer")

JAR_ENV = "SWM_WARC_INDEXER_JAR"
CONF_ENV = "SWM_WARC_INDEXER_CONF"
JAVA_ENV = "SWM_JAVA"
CMD_ENV = "SWM_WARC_INDEXER_CMD"

SETTING_PREFIX = "indexer."
SETTING_KEYS = ("java", "jar", "config", "memory")

MANIFEST_NAME = "warc-index-manifest.json"
LOG_NAME = "warc-index.log"
DEFAULT_MEMORY = "2g"
POLL_SECONDS = 2.0
LOG_TAIL_LINES = 15

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

GetSetting = Optional[Callable[[str], Optional[str]]]

_PARSING = re.compile(r"Parsing Archive File \[(\d+)/(\d+)\]:\s*(.*)")
_MEMORY = re.compile(r"^\d+[mMgG]$")


class WarcIndexerUnavailable(Exception):
    """The jar or Java cannot be found, or there is nothing to index."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _setting(get_setting: GetSetting, key: str) -> str:
    if get_setting is None:
        return ""
    try:
        return str(get_setting(SETTING_PREFIX + key) or "").strip()
    except Exception:                                   # noqa: BLE001 - a store problem is not ours
        return ""


def settings_from(get_setting: GetSetting) -> dict:
    """The stored Indexer settings, raw, empty where not set."""
    return {key: _setting(get_setting, key) for key in SETTING_KEYS}


# --- finding the pieces ------------------------------------------------------

def java_from(candidate: str) -> Optional[str]:
    """A java executable from either its path or a JAVA_HOME folder."""
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.is_file():
        return str(path)
    exe = "java.exe" if os.name == "nt" else "java"
    for inner in (path / "bin" / exe, path / exe):
        if inner.is_file():
            return str(inner)
    return None


def find_java(get_setting: GetSetting = None) -> Optional[str]:
    for candidate in (_setting(get_setting, "java"), os.environ.get(JAVA_ENV, ""),
                      os.environ.get("JAVA_HOME", "")):
        found = java_from(candidate)
        if found:
            return found
    return shutil.which("java")


_JAVA_VERSIONS: dict[tuple[str, Optional[float]], Optional[str]] = {}


def java_version(java: Optional[str] = None) -> Optional[str]:
    """The version line ``java -version`` prints, or None when it cannot run.

    Remembered per executable (and its modification time, so an upgrade or
    a swapped path is noticed): the dashboard asks for the settings every
    two seconds, and a JVM must not be started on each of them."""
    if not java:
        return None
    try:
        stamp: Optional[float] = Path(java).stat().st_mtime
    except OSError:
        stamp = None
    key = (str(java), stamp)
    if key in _JAVA_VERSIONS:
        return _JAVA_VERSIONS[key]
    try:
        done = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None                       # asked again next time
    lines = [l.strip() for l in (done.stderr or done.stdout or "").splitlines() if l.strip()]
    # the version line proper; a JVM may print JAVA_TOOL_OPTIONS first
    version = next((l for l in lines if "version" in l.lower()), lines[0] if lines else None)
    if version:                       # a failure is asked again next time
        _JAVA_VERSIONS[key] = version
    return version


def configured_path(kind: str, get_setting: GetSetting = None) -> Optional[Path]:
    """The jar or config path the curator set (Settings, else the
    environment), whether or not it exists; None when nothing is set."""
    env = {"jar": JAR_ENV, "config": CONF_ENV}[kind]
    for candidate in (_setting(get_setting, kind), os.environ.get(env, "")):
        if candidate:
            return Path(candidate).expanduser()
    return None


def find_jar(get_setting: GetSetting = None) -> Optional[Path]:
    """A set path wins, even when it dangles: capability() then says so
    rather than silently running whatever jar the repository holds."""
    configured = configured_path("jar", get_setting)
    if configured is not None:
        return configured if configured.is_file() else None
    target = _repo_root() / "warc-indexer" / "target"
    jars = sorted(target.glob("warc-indexer-*-jar-with-dependencies.jar"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return jars[0] if jars else None


def find_config(jar: Optional[Path] = None, get_setting: GetSetting = None) -> Optional[Path]:
    configured = configured_path("config", get_setting)
    if configured is not None:
        return configured if configured.is_file() else None
    candidates = []
    if jar:
        candidates.append(jar.resolve().parent.parent / "config" / "swm-indexer.conf")
    candidates.append(_repo_root() / "warc-indexer" / "config" / "swm-indexer.conf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def memory_from(get_setting: GetSetting = None) -> str:
    stored = _setting(get_setting, "memory")
    return stored if _MEMORY.match(stored) else DEFAULT_MEMORY


def command_override() -> Optional[list[str]]:
    raw = os.environ.get(CMD_ENV)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return [str(p) for p in parsed] if isinstance(parsed, list) and parsed else None


def validate_settings(payload: object) -> dict:
    """The Indexer settings a curator typed, checked: each path must exist
    as what it claims to be, or be empty to fall back to finding it. Raises
    ValueError with a sentence to show."""
    if not isinstance(payload, dict):
        raise ValueError("indexer settings must be an object")
    accepted = {}
    for key in SETTING_KEYS:
        if key not in payload:
            continue
        value = str(payload.get(key) or "").strip()
        if value:
            if key == "java" and not java_from(value):
                raise ValueError(
                    f"No java was found at {value}: give the JAVA_HOME folder "
                    "(the one holding bin/java) or the java executable itself.")
            if key == "jar" and not (Path(value).expanduser().is_file() and value.lower().endswith(".jar")):
                raise ValueError(f"The jar {value} does not exist. Build it in the warc-indexer "
                                 "folder (mvnw -DskipTests package) or point at a built jar.")
            if key == "config" and not Path(value).expanduser().is_file():
                raise ValueError(f"The configuration file {value} does not exist.")
            if key == "memory" and not _MEMORY.match(value):
                raise ValueError("Memory must be a Java heap size such as 2g or 1500m.")
        accepted[key] = value
    if not accepted:
        raise ValueError("provide at least one of java, jar, config, memory")
    return accepted


def capability(get_setting: GetSetting = None) -> dict:
    """Whether the jar can be run here, with what, and what to do if not."""
    override = command_override()
    if override:
        return {"available": True, "reason": None, "note": None, "jar": None, "java": None,
                "java_version": None, "config": str(find_config(None, get_setting) or "") or None,
                "memory": memory_from(get_setting), "command": override,
                "settings": settings_from(get_setting)}
    java = find_java(get_setting)
    jar = find_jar(get_setting)
    config = find_config(jar, get_setting)
    problems = []
    if not java:
        problems.append("Java was not found. Install Java 11 or newer, or set the Java path "
                        "(JAVA_HOME or the java executable) under Settings › Indexer.")
    if not jar:
        dangling = configured_path("jar", get_setting)
        if dangling is not None:
            problems.append(f"The warc-indexer jar set under Settings › Indexer (or "
                            f"{JAR_ENV}) is not there: {dangling}. Correct the path, or "
                            "clear it to use the repository's own build.")
        else:
            problems.append("The warc-indexer jar was not found. Build it with "
                            "`mvnw -DskipTests package` in the repository's warc-indexer "
                            "folder, or set its path under Settings › Indexer.")
    version = java_version(java) if java else None
    if java and not version:
        problems.append(f"Java at {java} could not be run.")
    note = None
    if jar and not config:
        dangling = configured_path("config", get_setting)
        note = ((f"The configuration set under Settings › Indexer (or {CONF_ENV}) is not "
                 f"there: {dangling}. ") if dangling is not None else
                "config/swm-indexer.conf was not found beside the jar. ") + (
                "The jar's built-in defaults would be used, which index request records "
                "too. Set the configuration path under Settings › Indexer.")
    return {"available": bool(java and jar and version), "reason": " ".join(problems) or None,
            "note": note, "jar": str(jar) if jar else None, "java": java,
            "java_version": version, "config": str(config) if config else None,
            "memory": memory_from(get_setting), "settings": settings_from(get_setting)}


# --- the run ---------------------------------------------------------------------

def warc_files(crawl_dir: Path) -> list[Path]:
    crawl_dir = Path(crawl_dir)
    return sorted(crawl_dir.glob("*.warc.gz")) + sorted(crawl_dir.glob("*.warc"))


def index_path_for(warc: Path) -> Path:
    """Where the jar writes a WARC's documents: beside it, ``<name>.jsonl``."""
    return warc.parent / (warc.name + ".jsonl")


def build_command(out_dir: Path, warcs: Iterable[Path], *, collection: Optional[str] = None,
                  java: Optional[str] = None, jar: Optional[Path] = None,
                  config: Optional[Path] = None, memory: Optional[str] = None,
                  get_setting: GetSetting = None) -> list[str]:
    prefix = command_override()
    if not prefix:
        java = java or find_java(get_setting)
        jar = jar or find_jar(get_setting)
        if not java or not jar:
            raise WarcIndexerUnavailable(capability(get_setting)["reason"] or "warc-indexer is unavailable")
        prefix = [java, f"-Xmx{memory or memory_from(get_setting)}", "-jar", str(jar)]
    config = config or find_config(jar, get_setting)
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


from .procs import pid_alive as _pid_alive  # noqa: E402 - one liveness answer


def is_running(crawl_dir: Path) -> bool:
    manifest = read_manifest(crawl_dir)
    return bool(manifest and manifest.get("status") == STATUS_RUNNING
                and not _run_died(manifest, crawl_dir))


def _count_lines(path: Path) -> int:
    count = 0
    try:
        with path.open("rb") as handle:
            for line in handle:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def log_tail(crawl_dir: Path, lines: int = LOG_TAIL_LINES) -> str:
    """The last lines of the jar's output, for a failure message."""
    path = Path(crawl_dir) / LOG_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    kept = [l.rstrip() for l in text.splitlines() if l.strip()]
    return "\n".join(kept[-lines:])


def _explain(exit_code: Optional[int], tail: str) -> str:
    """A sentence about a failed run, from what the log says."""
    low = tail.lower()
    if "unsupportedclassversionerror" in low or "class file version" in low:
        return "This Java is too old for the jar: Java 11 or newer is needed."
    if "outofmemoryerror" in low or "java heap space" in low:
        return "The jar ran out of memory: raise the memory under Settings › Indexer (for example 4g)."
    if "unable to access jarfile" in low or "error: invalid or corrupt jarfile" in low:
        return "The jar could not be opened: check its path under Settings › Indexer and rebuild it if needed."
    if "no such file or directory" in low and ".conf" in low or "config" in low and "not found" in low:
        return "The configuration file could not be read: check its path under Settings › Indexer."
    if "filenotfoundexception" in low and (".warc" in low or ".arc" in low):
        return "The indexer could not open a WARC file at the path it was given."
    if exit_code is not None:
        return f"The indexer exited with code {exit_code}."
    return "The indexer failed."


class _Progress:
    """Progress of one run, read incrementally: each poll reads only what
    the jar appended to its log and its outputs since the last one, so a
    long run with large outputs costs the same per poll at its end as at
    its start."""

    def __init__(self, crawl_dir: Path, chosen: list[Path], started: float):
        self.crawl_dir, self.chosen, self.started = crawl_dir, chosen, started
        self.file_index, self.files_total, self.current = 0, len(chosen), None
        self._log_offset = 0
        self._log_rest = b""
        self._counted: dict[Path, tuple[int, int]] = {}      # output -> (offset, lines)

    def _read_log(self) -> None:
        path = self.crawl_dir / LOG_NAME
        try:
            with path.open("rb") as handle:
                handle.seek(self._log_offset)
                chunk = handle.read()
        except OSError:
            return
        self._log_offset += len(chunk)
        data = self._log_rest + chunk
        lines = data.split(b"\n")
        self._log_rest = lines.pop()                       # a line still being written
        for raw in lines:
            m = _PARSING.search(raw.decode("utf-8", "replace"))
            if m:
                self.file_index, self.files_total = int(m.group(1)), int(m.group(2))
                self.current = Path(m.group(3).strip()).name

    def _documents(self) -> int:
        total = 0
        for warc in self.chosen:
            out = index_path_for(warc)
            offset, lines = self._counted.get(out, (0, 0))
            try:
                with out.open("rb") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if line.endswith(b"\n"):
                            offset += len(line)
                            if line.strip():
                                lines += 1
            except OSError:
                pass
            self._counted[out] = (offset, lines)
            total += lines
        return total

    def read(self) -> dict:
        self._read_log()
        return {"file_index": self.file_index, "files_total": self.files_total,
                "current_warc": self.current, "documents": self._documents(),
                "elapsed_seconds": round(time.monotonic() - self.started)}


def _progress(crawl_dir: Path, chosen: list[Path], started: float) -> dict:
    """One full read, for the final tally."""
    return _Progress(crawl_dir, chosen, started).read()


def index_warcs(crawl_dir: Path, *, warcs: Optional[Iterable[str | Path]] = None,
                collection: Optional[str] = None, memory: Optional[str] = None,
                timeout: Optional[float] = None, get_setting: GetSetting = None,
                on_progress: Optional[Callable[[dict], None]] = None,
                poll: Optional[float] = None) -> dict:
    """Run the jar on a job's WARC files, or on the named ones, writing each
    ``<warc>.jsonl`` beside its WARC. Blocks until the jar exits, recording
    progress in the manifest every ``poll`` seconds (and to ``on_progress``);
    the dashboard calls this on a thread. Returns the manifest it wrote."""
    # The jar runs with the job's folder as its working directory, so every
    # path handed to it must be absolute: a job folder recorded relative to
    # the dashboard's own directory ("warcs/108") would otherwise be
    # resolved twice, and the jar would look for warcs/108/warcs/108/...
    crawl_dir = Path(crawl_dir).resolve()
    poll = poll or POLL_SECONDS
    if warcs:
        chosen = []
        for item in warcs:
            path = Path(item)
            if not path.is_absolute():
                path = crawl_dir / path
            path = path.resolve()
            # only the job's own files: the outputs go beside the WARC, and a
            # run must never write beside, or delete beside, someone else's
            if path.parent != crawl_dir or not path.is_file():
                raise WarcIndexerUnavailable(f"{path.name} is not a WARC file in this job's folder")
            chosen.append(path)
    else:
        chosen = [w.resolve() for w in warc_files(crawl_dir)]
    if not chosen:
        raise WarcIndexerUnavailable("this job has no WARC files to index")
    command = build_command(crawl_dir, chosen, collection=collection, memory=memory,
                            get_setting=get_setting)
    # a previous run's outputs would be counted as progress: clear them
    for warc in chosen:
        try:
            index_path_for(warc).unlink()
        except FileNotFoundError:
            pass
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
        "progress": {"file_index": 0, "files_total": len(chosen), "current_warc": None,
                     "documents": 0, "elapsed_seconds": 0},
    }
    log_path = crawl_dir / LOG_NAME
    started = time.monotonic()
    progress = _Progress(crawl_dir, chosen, started)
    with log_path.open("wb") as log_file:
        try:
            proc = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT,
                                    cwd=str(crawl_dir))
        except OSError as exc:
            manifest.update({"status": STATUS_FAILED, "finished_at": _iso_now(),
                             "error": f"The indexer could not be started: {exc}",
                             "error_detail": " ".join(command[:4])})
            _write_manifest(crawl_dir, manifest)
            return manifest
        manifest["pid"] = proc.pid
        _write_manifest(crawl_dir, manifest)
        code: Optional[int] = None
        while True:
            try:
                code = proc.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                if timeout is not None and time.monotonic() - started > timeout:
                    proc.kill()
                    proc.wait()
                    manifest.update({"status": STATUS_FAILED, "finished_at": _iso_now(),
                                     "error": f"The indexer ran longer than {timeout:.0f} seconds and was stopped.",
                                     "error_detail": log_tail(crawl_dir)})
                    manifest.pop("pid", None)
                    _write_manifest(crawl_dir, manifest)
                    return manifest
                manifest["progress"] = progress.read()
                _write_manifest(crawl_dir, manifest)
                if on_progress:
                    on_progress(manifest["progress"])
    outputs = []
    for warc in chosen:
        out = index_path_for(warc)
        outputs.append({"warc": warc.name, "index": str(out),
                        "documents": _count_lines(out) if out.is_file() else None})
    manifest["outputs"] = outputs
    manifest["documents"] = sum(o["documents"] or 0 for o in outputs)
    manifest["exit_code"] = code
    manifest["finished_at"] = _iso_now()
    manifest["progress"] = _progress(crawl_dir, chosen, started)
    missing = [o["warc"] for o in outputs if o["documents"] is None]
    tail = log_tail(crawl_dir)
    if code != 0:
        manifest["status"] = STATUS_FAILED
        manifest["error"] = _explain(code, tail)
        manifest["error_detail"] = tail
    elif missing:
        manifest["status"] = STATUS_FAILED
        manifest["error"] = "The indexer finished but wrote no index for: " + ", ".join(missing)
        manifest["error_detail"] = tail
    else:
        manifest["status"] = STATUS_DONE
    manifest.pop("pid", None)
    _write_manifest(crawl_dir, manifest)
    return manifest


STALE_SECONDS = 3 * POLL_SECONDS
# With the pid apparently alive the manifest may still go stale: after a
# server restart the runner thread that wrote it is gone, and the number may
# by now belong to another process. A live run rewrites the manifest every
# poll; one untouched this long has no runner behind it.
STALE_WITH_PID_SECONDS = 60.0


def _stale(crawl_dir: Path, limit: float = STALE_SECONDS) -> bool:
    """Whether the manifest has not been touched for longer than the
    runner's own polling would allow while a run is alive."""
    try:
        age = time.time() - (Path(crawl_dir) / MANIFEST_NAME).stat().st_mtime
    except OSError:
        return True
    return age > limit


def _run_died(manifest: dict, crawl_dir: Path) -> bool:
    """A manifest that says running, with no runner behind it any more."""
    if manifest.get("status") != STATUS_RUNNING:
        return False
    if not _pid_alive(manifest.get("pid")):
        return _stale(crawl_dir)
    return _stale(crawl_dir, STALE_WITH_PID_SECONDS)


def summary(crawl_dir: Path) -> Optional[dict]:
    """What the dashboard shows about the last run."""
    manifest = read_manifest(crawl_dir)
    if not manifest:
        return None
    status = manifest.get("status")
    error = manifest.get("error")
    if _run_died(manifest, crawl_dir):
        # the process went away without the run being finished off: the
        # server restarted or the runner died. A manifest written moments
        # ago is just the gap between the jar exiting and its outcome being
        # recorded, so only an old one counts.
        status = STATUS_FAILED
        error = error or "The indexer stopped without finishing (the server may have restarted)."
    detail = manifest.get("error_detail") or ""
    return {"status": status, "documents": manifest.get("documents"),
            "warcs": len(manifest.get("warcs") or []),
            "outputs": [o.get("index") for o in manifest.get("outputs") or []],
            "started_at": manifest.get("started_at"), "finished_at": manifest.get("finished_at"),
            "error": error, "error_detail": detail[-600:] if detail else None,
            "progress": manifest.get("progress"), "collection": manifest.get("collection")}


if __name__ == "__main__":                              # pragma: no cover
    print(json.dumps(capability(), indent=2))
    sys.exit(0)


# --- a whole collection ---------------------------------------------------------

COLLECTION_MANIFEST_NAME = "warc-index-manifest.json"     # in the collection's root


def index_collection(root_dir: Path, jobs: list[tuple[int, Path]], *, collection: str,
                     memory: Optional[str] = None, get_setting: GetSetting = None,
                     on_progress: Optional[Callable[[dict], None]] = None,
                     poll: Optional[float] = None) -> dict:
    """Run the jar over every job of a collection in turn, each job's
    outputs beside its own WARCs and every document carrying the
    collection's name. A job without WARC files is skipped. The record of
    the run is a manifest in the collection's root; each job keeps its own
    as well. Blocks until the last job is done; the dashboard calls this
    on a thread."""
    root = Path(root_dir).resolve()
    manifest = {
        "schema": "swm-warc-index-collection-run/1",
        "status": STATUS_RUNNING,
        "collection": collection,
        "started_at": _iso_now(),
        "jobs": [{"id": int(crawl_id), "status": "pending", "documents": 0}
                 for crawl_id, _dir in jobs],
        "current_job": None,
        "documents": 0,
        "pid": os.getpid(),
    }
    _write_collection_manifest(root, manifest)

    def note(entry: dict, **fields) -> None:
        entry.update(fields)
        manifest["documents"] = sum(int(j.get("documents") or 0) for j in manifest["jobs"])
        _write_collection_manifest(root, manifest)
        if on_progress:
            on_progress(dict(manifest))

    for entry, (crawl_id, crawl_dir) in zip(manifest["jobs"], jobs):
        crawl_dir = Path(crawl_dir)
        if not warc_files(crawl_dir):
            note(entry, status="skipped", note="no WARC files")
            continue
        manifest["current_job"] = int(crawl_id)
        note(entry, status=STATUS_RUNNING)
        try:
            result = index_warcs(crawl_dir, collection=collection, memory=memory,
                                 get_setting=get_setting, poll=poll,
                                 on_progress=lambda p, e=entry: note(e, progress=p))
        except Exception as exc:                          # noqa: BLE001 - recorded, run goes on
            note(entry, status=STATUS_FAILED, error=str(exc))
            continue
        note(entry, status=result["status"], documents=result.get("documents") or 0,
             error=result.get("error"), warcs=len(result.get("warcs") or []))
    manifest["current_job"] = None
    failed = [j["id"] for j in manifest["jobs"] if j["status"] == STATUS_FAILED]
    manifest["status"] = STATUS_FAILED if failed else STATUS_DONE
    if failed:
        manifest["error"] = f"indexing failed for job(s) {', '.join(map(str, failed))}"
    manifest["finished_at"] = _iso_now()
    manifest.pop("pid", None)
    _write_collection_manifest(root, manifest)
    return manifest


def _write_collection_manifest(root: Path, manifest: dict) -> None:
    path = Path(root) / COLLECTION_MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_collection_manifest(root: Path) -> Optional[dict]:
    path = Path(root) / COLLECTION_MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collection_summary(root: Path) -> Optional[dict]:
    """What the collection's row shows about its last run."""
    manifest = read_collection_manifest(root)
    if not manifest:
        return None
    status = manifest.get("status")
    error = manifest.get("error")
    if status == STATUS_RUNNING:
        try:
            age = time.time() - (Path(root) / COLLECTION_MANIFEST_NAME).stat().st_mtime
        except OSError:
            age = STALE_WITH_PID_SECONDS + 1
        alive = _pid_alive(manifest.get("pid"))
        if (not alive and age > STALE_SECONDS) or age > STALE_WITH_PID_SECONDS:
            status = STATUS_FAILED
            error = error or "The indexer stopped without finishing (the server may have restarted)."
    jobs = manifest.get("jobs") or []
    return {"status": status, "documents": manifest.get("documents"),
            "jobs": len(jobs), "jobs_done": sum(1 for j in jobs if j.get("status") == STATUS_DONE),
            "jobs_failed": sum(1 for j in jobs if j.get("status") == STATUS_FAILED),
            "current_job": manifest.get("current_job"),
            "started_at": manifest.get("started_at"), "finished_at": manifest.get("finished_at"),
            "error": error, "collection": manifest.get("collection")}


def collection_is_running(root: Path) -> bool:
    summary = collection_summary(root)
    return bool(summary and summary["status"] == STATUS_RUNNING)
