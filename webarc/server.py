"""FastAPI control server for Simple Webcrawl Manager (SWM).

Endpoints:
  GET  /                      -> dashboard HTML
  GET  /api/capabilities      -> feature availability (interactive recording)
  POST /api/config/parse      -> parse YAML for the guided editor
  POST /api/config/render     -> render guided-editor JSON as YAML
  POST /api/recordings        -> create + launch an interactive recording
  POST /api/facebook          -> create + launch a Facebook Page capture
  GET  /api/facebook/state    -> previous per-Page capture state
  GET  /api/crawls            -> list crawls with live progress + storage
  POST /api/crawls            -> create + launch a crawl (YAML or JSON body)
  GET  /api/crawls/{id}       -> single crawl detail
  POST /api/crawls/{id}/pause
  POST /api/crawls/{id}/resume
  POST /api/crawls/{id}/stop
  DELETE /api/crawls/{id}     -> remove record (and optionally WARCs)
  GET  /api/storage           -> aggregate storage usage
  GET  /api/resources         -> spare CPU, memory and disk; usage per running job
  GET  /api/resources/check   -> whether a new job should be warned before starting
  POST /api/crawls/{id}/start -> launch a job that was told to wait
  GET/PUT /api/settings       -> default storage location, resource warning levels,
                                 the theme judge, the Indexer (Java, jar, config)
  GET  /api/help              -> the help text behind each "?" on the forms
  GET/PUT /api/crawls/{id}/metadata -> a job's descriptive metadata (Dublin Core)
  GET  /api/crawls/{id}/metadata.csv -> the same as a one-row-per-seed sheet
  POST /api/metadata/parse    -> read such a sheet back into the metadata shape
  POST /api/crawls/{id}/index -> index a social capture's records into
                                 warc-indexer's document schema (JSON Lines);
                                 {"relocate": true, "source_root": ...} only
                                 rewrites where the WARCs are said to live
  GET  /api/crawls/{id}/index -> the summary of the last such run
  GET  /api/crawls/{id}/index.jsonl -> download the documents
  POST /api/crawls/{id}/warc-index -> run the warc-indexer jar on a crawl's or
                                 recording's WARCs, writing <warc>.jsonl beside
                                 each; returns at once, the job card follows it
  GET  /api/crawls/{id}/warc-index -> the state of that run
  GET  /api/crawls/{id}/warc-index/log -> the jar's output from it

A crawl runs as an isolated subprocess (webarc.worker). Pause/resume/stop are
delivered through the store's control column, which the worker polls between
pages. Stop also hard-kills the pid as a fallback if the worker is wedged.
"""

from __future__ import annotations

import json
import logging

import os
import shutil
from contextlib import asynccontextmanager
import signal
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import collections as colls
from . import metadata as md
from . import resources
from .procs import pid_alive
from .store import (BLOCKED, CTRL_NONE, FAILED, CTRL_PAUSE, CTRL_RESUME, CTRL_STOP, KIND_FACEBOOK,
                    KIND_INSTAGRAM, KIND_RECORDING, KIND_X, KIND_YOUTUBE, PAUSED, PENDING,
                    RUNNING,
                    STOPPED, STOPPING, WAITING, Store)

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent
DASHBOARD = BASE / "dashboard.html"
DASHBOARD_HARDENING = BASE / "dashboard_hardening.js"

# resolved at startup by create_app
_STORE: Store | None = None
_WARC_ROOT: Path = Path("./warcs")
_SIMULATE = False
_PYWB = None            # lazily-started ReplayServer
_REPLAY_ROOT = Path("./replay")
_BIND_HOST = "127.0.0.1"
_ALLOW_REMOTE_RECORDING = False
_MONITOR: resources.ResourceMonitor | None = None

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _recording_capability() -> dict:
    """Whether interactive recording can work here.

    A recording opens a visible browser ON THE SERVER'S DESKTOP. That is the
    normal case for a loopback-bound dashboard, but must not silently launch
    Chrome on a remote server, and cannot work without a graphical session.
    Simulate mode is always available (no browser is opened)."""
    if _SIMULATE:
        return {"available": True, "reason": None}
    if _BIND_HOST not in _LOOPBACK_HOSTS and not _ALLOW_REMOTE_RECORDING:
        return {"available": False,
                "reason": "Interactive recording is unavailable because the "
                          "dashboard is not bound to this machine's loopback "
                          "interface. The browser would open on the server, "
                          "not in front of you. Start the server with "
                          "--allow-remote-recording to override."}
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {"available": False,
                "reason": "Interactive recording is unavailable because SWM "
                          "is running without a graphical desktop."}
    return {"available": True, "reason": None}


_STORAGE_ROOT_SETTING = "storage_root"


def _storage_is_curator_choosable() -> dict:
    """Whether a curator may name a storage directory from this dashboard.

    Writing wherever the person sitting at the machine points is the
    dashboard's own authority when it is bound to loopback. Reachable over a
    network it is not: naming a path would let anyone who can reach the port
    write to any directory the server can, so the same override that permits
    a remote browser is required for this.
    """
    if _BIND_HOST in _LOOPBACK_HOSTS or _ALLOW_REMOTE_RECORDING:
        return {"available": True, "reason": None}
    return {"available": False,
            "reason": "A storage location cannot be chosen here because the "
                      "dashboard is not bound to this machine's loopback "
                      "interface. Captures go to the server's configured "
                      "storage. Start the server with "
                      "--allow-remote-recording to override."}


def _usable_directory(path: Path, label: str) -> Path:
    """Resolve a curator-named directory, or say plainly why it cannot serve."""
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise HTTPException(400, f"{label} cannot be used: {exc}")
    if resolved.exists() and not resolved.is_dir():
        raise HTTPException(400, f"{label} is a file, not a directory.")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            400, f"{label} is not writable: it cannot be created ({exc}).")
    if not os.access(resolved, os.W_OK):
        raise HTTPException(400, f"{label} is not writable.")
    return resolved


def _default_storage_root() -> Path:
    """Where captures go when a crawl does not name its own location.

    A default that has become unusable -- an unplugged drive, a directory
    since made read-only -- must not fail every new capture, so the server's
    own root is used and the reason is logged.
    """
    stored = (_store().get_setting(_STORAGE_ROOT_SETTING) or "").strip()
    if not stored:
        return _WARC_ROOT
    try:
        return _usable_directory(Path(stored), "The default storage location")
    except HTTPException as exc:
        log.warning("Default storage %s unusable (%s); using %s",
                    stored, exc.detail, _WARC_ROOT)
        return _WARC_ROOT


def _storage_root_for(requested: object) -> Path:
    """The root this crawl's own directory is created under."""
    text = str(requested or "").strip()
    if not text:
        return _default_storage_root()
    choosable = _storage_is_curator_choosable()
    if not choosable["available"]:
        raise HTTPException(403, choosable["reason"])
    return _usable_directory(Path(text), "That storage location")


def _places() -> list[dict]:
    """A few directories worth starting from, without hunting for them."""
    seen: dict[str, dict] = {}
    for label, path in (
        ("Home", Path.home()),
        ("Working directory", Path.cwd()),
        ("SWM storage", _WARC_ROOT),
        ("Current default", _default_storage_root()),
    ):
        try:
            resolved = str(path.expanduser().resolve())
        except OSError:
            continue
        seen.setdefault(resolved, {"label": label, "path": resolved})
    return list(seen.values())


def _crawl_dir(row: dict) -> Path:
    """Where this crawl's files actually are.

    Recomputing the path from the server's root was safe only while every
    crawl lived under it. A crawl given its own storage location has to be
    read back from where it was written, and output_dir is what records that.
    """
    stored = str((row or {}).get("output_dir") or "").strip()
    return Path(stored) if stored else _WARC_ROOT / str((row or {})["id"])


def _instagram_capability() -> dict:
    """Instagram capture drives a browser; it can run without a window, and a
    display is needed only to sign in or clear a checkpoint. The dashboard
    says so rather than refusing outright."""
    visible = _recording_capability()
    note = None if visible["available"] else (
        "No graphical desktop: a capture can run in the background, but "
        "signing in to Instagram or clearing a checkpoint needs a browser "
        "window, which cannot open here.")
    from .instagram_gallery import gallery_dl_version
    return {"available": True, "reason": None, "note": note,
            "gallery_dl": gallery_dl_version()}


def _x_capability() -> dict:
    """X capture drives a signed-in browser with a window, like Instagram."""
    visible = _recording_capability()
    return {"available": visible["available"], "reason": visible["reason"],
            "note": None if visible["available"] else (
                "No graphical desktop: an X capture needs a browser window to "
                "sign in and to run, which cannot open here.")}


def _write_theme_ai_settings(wanted: object) -> None:
    """Store the AI judge's settings. The key is written only when one is
    sent, cleared by an empty string, and never read back to the page."""
    from .theme import PROVIDERS, SETTING_PREFIX
    if not isinstance(wanted, dict):
        raise HTTPException(400, "theme_ai must be an object")
    provider = str(wanted.get("provider") or "none").strip()
    if provider not in PROVIDERS:
        raise HTTPException(400, "theme_ai.provider must be one of " + ", ".join(PROVIDERS))
    endpoint = str(wanted.get("endpoint") or "").strip()
    if provider == "openai_compatible" and not endpoint.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "theme_ai.endpoint must be an http(s) URL")
    if provider == "azure_openai" and not endpoint.lower().startswith("https://"):
        raise HTTPException(400, "theme_ai.endpoint must be the Azure OpenAI resource's https address")
    # Azure calls it a deployment; either name is accepted and stored as the model
    model = str(wanted.get("model") or wanted.get("deployment") or "").strip()[:200]
    try:
        max_calls = int(wanted.get("max_calls") or 2000)
        tokens_per_minute = int(wanted.get("tokens_per_minute") or 0)
        max_prompt_tokens = int(wanted.get("max_prompt_tokens") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "theme_ai.max_calls, tokens_per_minute and max_prompt_tokens "
                                 "must be whole numbers") from exc
    if not 0 <= max_prompt_tokens <= 1_000_000:
        raise HTTPException(400, "theme_ai.max_prompt_tokens must be 0 (sized from the allowance) "
                                 "or a positive number")
    if not 1 <= max_calls <= 1_000_000:
        raise HTTPException(400, "theme_ai.max_calls must be between 1 and 1,000,000")
    if not 0 <= tokens_per_minute <= 100_000_000:
        raise HTTPException(400, "theme_ai.tokens_per_minute must be 0 (no limit) or a positive number")
    api_version = str(wanted.get("api_version") or "").strip()[:40]
    store = _store()
    store.set_setting(SETTING_PREFIX + "provider", provider)
    store.set_setting(SETTING_PREFIX + "endpoint", endpoint[:500])
    store.set_setting(SETTING_PREFIX + "model", model)
    store.set_setting(SETTING_PREFIX + "max_calls", str(max_calls))
    store.set_setting(SETTING_PREFIX + "tokens_per_minute", str(tokens_per_minute))
    store.set_setting(SETTING_PREFIX + "max_prompt_tokens", str(max_prompt_tokens))
    store.set_setting(SETTING_PREFIX + "api_version", api_version)
    if "api_key" in wanted:
        store.set_setting(SETTING_PREFIX + "api_key", str(wanted.get("api_key") or "").strip()[:500])


def _theme_ai_capability() -> dict:
    from .theme import ai_capability
    return ai_capability(_store().get_setting)


# Jobs whose WARCs are being indexed by this server, claimed before the
# runner thread starts: the manifest that says "running" is only written
# once the jar is up, and two clicks in that gap must not start two jars.
_WARC_INDEX_RUNS: set[int] = set()
_WARC_INDEX_LOCK = __import__("threading").Lock()


def _warc_indexing(crawl_id: int, crawl_dir: Path) -> bool:
    """Whether a warc-indexer run is under way for this job: claimed here,
    or recorded as running by a manifest a runner is still updating."""
    from . import warc_indexer
    with _WARC_INDEX_LOCK:
        if crawl_id in _WARC_INDEX_RUNS:
            return True
    return warc_indexer.is_running(crawl_dir)


def _warc_indexer_capability() -> dict:
    """Whether the warc-indexer jar can be run here: Java and a built jar,
    found through the Indexer settings, the environment, or the repository."""
    from .warc_indexer import capability
    return capability(_store().get_setting)


def _youtube_capability() -> dict:
    """YouTube capture needs yt-dlp for videos and a browser window for the
    Posts tab and for the sign-in YouTube demands; either half can be
    missing, and the dashboard says which."""
    from .youtube_ytdlp import ffmpeg_path, js_runtime, po_token_provider_available, ytdlp_version

    visible = _recording_capability()
    version = ytdlp_version()
    runtime = js_runtime()
    notes = []
    if not version:
        notes.append("yt-dlp is not installed, so videos cannot be listed or downloaded; "
                     "install it with: pip install yt-dlp. The Posts tab can still be captured.")
    if version and not ffmpeg_path():
        notes.append("ffmpeg was not found, so separate video and audio streams cannot be "
                     "joined; downloads fall back to single-file renditions, usually 720p or less.")
    if version and not runtime:
        notes.append("No JavaScript runtime (deno or node) was found; yt-dlp needs one for "
                     "some of YouTube's players and may miss formats.")
    if not visible["available"]:
        notes.append("No graphical desktop: the Posts tab cannot be read here, and a sign-in "
                     "YouTube demands of yt-dlp cannot be done here.")
    return {"available": bool(version) or visible["available"],
            "reason": None if (version or visible["available"]) else
            "Neither yt-dlp nor a browser window is available on this server.",
            "note": " ".join(notes) or None,
            "yt_dlp": version, "ffmpeg": ffmpeg_path(),
            "js_runtime": runtime[0] if runtime else None,
            "po_token_provider": po_token_provider_available()}


def _youtube_refusal(config, targets) -> str | None:
    """Why this YouTube run cannot start here, or None.

    A run that needs yt-dlp (videos, Shorts, live streams, a video or a
    playlist target) is refused up front when yt-dlp is not importable,
    rather than started and left to write an empty package."""
    from .youtube import NO_YTDLP
    from .youtube_ytdlp import ytdlp_version

    needs_videos = (any(t.kind in ("video", "playlist") for t in targets)
                    or any(s in config.surfaces for s in ("videos", "shorts", "streams")))
    if needs_videos and not ytdlp_version():
        return NO_YTDLP + " To capture only the Posts tab of a channel meanwhile, untick " \
               "Videos, Shorts and Live streams."
    if "posts" in config.surfaces and not _recording_capability()["available"] \
            and not needs_videos:
        return ("No browser window is available on this server, so the Posts tab cannot "
                "be read here.")
    return None


def _youtube_replay_media(crawl_dir: Path, crawl_id: int, base_url: str) -> dict:
    """The downloaded video files of a YouTube capture, by video id, as the
    URLs this server serves them at, for the replayed watch page to play."""
    import json as _json
    try:
        index = _json.loads((crawl_dir / "youtube-media.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: dict = {}
    for rel, entry in (index.items() if isinstance(index, dict) else []):
        if not isinstance(entry, dict) or entry.get("role") != "video":
            continue
        video_id = entry.get("video_id")
        if not video_id or not str(rel).startswith("media/") or video_id in found:
            continue
        if not str(rel).lower().endswith((".mp4", ".webm", ".m4a", ".mp3", ".opus")):
            continue
        found[str(video_id)] = {
            "url": base_url.rstrip("/") + f"/captures/{crawl_id}/media/" + str(rel)[len("media/"):],
            "file": str(rel), "resolution": entry.get("resolution")}
    return found


def _youtube_name_part(target) -> str:
    if target.kind == "channel":
        return str(target.handle or target.channel_id)
    if target.kind == "video":
        return f"video-{target.video_id}"
    return f"playlist-{target.playlist_id}"


def _x_name_part(target) -> str:
    import re as _re
    if target.kind == "profile":
        return str(target.handle)
    if target.kind == "post":
        return f"post-{target.post_id}"
    words = _re.sub(r"[^A-Za-z0-9]+", "-", str(target.query or "")).strip("-")[:40]
    return f"search-{words or 'query'}"


def _store() -> Store:
    assert _STORE is not None
    return _STORE


def _validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise HTTPException(400, "config must be a YAML/JSON object")
    seeds = config.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise HTTPException(400, "config must define at least one seed")
    for index, seed in enumerate(seeds, 1):
        if not isinstance(seed, dict) or not seed.get("url"):
            raise HTTPException(400, f"seed {index} must define a URL")
    if config.get("theme") is not None:
        from .theme import ThemeConfig
        try:
            ThemeConfig.from_dict(config["theme"])
        except ValueError as exc:
            raise HTTPException(400, f"theme: {exc}") from exc
    return config



def _parse_yaml(source: object) -> dict:
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(400, "config_yaml must contain YAML text")
    try:
        config = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"invalid YAML: {exc}") from exc
    return _validate_config(config)


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _pid_alive(pid: int | None) -> bool:
    return pid_alive(pid)


def _pid_is_worker(pid: int) -> bool:
    """Whether ``pid`` is an SWM worker, not another process that inherited
    the number after a restart. Best effort: where the platform will not say,
    a live pid is taken to be the worker."""
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
            if not h:
                return False
            try:
                size = ctypes.c_ulong(1024)
                buffer = ctypes.create_unicode_buffer(size.value)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                        h, 0, buffer, ctypes.byref(size)):
                    return "python" in buffer.value.lower()
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
            return True
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists():
            args = cmdline.read_bytes().split(b"\0")
            # a worker process, or the swm command running a job itself
            return any(b"webarc" in a or Path(a.decode("utf-8", "replace")).name == "swm"
                       for a in args)
    except Exception:
        pass
    return True


def _worker_alive(row: dict) -> bool:
    pid = row.get("pid")
    return bool(pid) and _pid_alive(pid) and _pid_is_worker(pid)


_ACTIVE_STATES = (RUNNING, PAUSED, BLOCKED, STOPPING)
_ORPHAN_GRACE_SECONDS = 60
_LOST_WORKER = ("The worker process is not running: it ended without reporting "
                "a result, or the server was restarted while it ran.")


def _reconcile(row: dict) -> dict:
    """Bring a crawl's stored state in line with whether its worker exists.

    A worker reports its own final state; one that died, or was lost when
    the server restarted, never will, and the crawl would stay running,
    paused, blocked or stopping forever -- unstoppable and undeletable. A
    crawl in such a state with no worker behind it, and no report for a
    minute, is settled here: stopping becomes stopped, the others failed,
    and the reason is recorded.
    """
    status = row.get("status")
    if status not in _ACTIVE_STATES or _worker_alive(row):
        return row
    try:
        from datetime import datetime, timezone
        seen = datetime.fromisoformat(str(row.get("updated_at")))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        idle = (datetime.now(timezone.utc) - seen).total_seconds()
    except (TypeError, ValueError):
        idle = _ORPHAN_GRACE_SECONDS + 1
    if idle < _ORPHAN_GRACE_SECONDS:
        return row
    settled = STOPPED if status == STOPPING else FAILED
    _store().set_status(row["id"], settled, _LOST_WORKER)
    _store().clear_control(row["id"])
    log.warning("Crawl %s was %s with no worker behind it; marked %s",
                row["id"], status, settled)
    return _store().get_crawl(row["id"]) or row


def _terminate(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        PROCESS_TERMINATE = 0x0001
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 1)
            ctypes.windll.kernel32.CloseHandle(h)
        return
    try:
        group = os.getpgid(pid)
        if group == os.getpgid(0):
            # a worker that shares our group (a test's stand-in, a worker
            # started without its own session) is ended alone, never with us
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(group, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _launch_worker(crawl_id: int) -> int:
    cmd = [sys.executable, "-m", "webarc.worker", str(crawl_id),
           "--db", _store().db_path]
    if _SIMULATE:
        cmd.append("--simulate")
    # detach so the worker outlives a server reload; new process group
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


def _metadata_from(payload: dict, seeds: list[str]) -> dict:
    """The metadata a create request carries, validated, or a 400."""
    try:
        return md.normalise(payload.get("metadata"), seeds=seeds)
    except ValueError as exc:
        raise HTTPException(400, f"metadata: {exc}") from exc


def _job_operator(config: dict) -> str:
    for section in ("recording", "facebook", "instagram", "x", "youtube"):
        if isinstance(config.get(section), dict) and config[section].get("operator"):
            return str(config[section]["operator"])
    return str(config.get("operator") or "webarc")


def _metadata_document(row: dict) -> dict:
    """metadata.json's content for a job, from what the store holds."""
    import json

    config = json.loads(row["config_json"])
    seeds = [{"url": str(s.get("url"))} for s in config.get("seeds", [])
             if isinstance(s, dict) and s.get("url")]
    crawl_dir = _crawl_dir(row)
    collection = _collection_of(row)
    return md.document(
        job_id=row["id"], kind=row.get("kind", "crawl"), name=row["name"],
        operator=_job_operator(config), seeds=seeds,
        metadata=md.from_config(config), existing=md.read_document(crawl_dir),
        inherited=colls.inherited_fields(collection),
        collection=colls.brief(collection))


def _write_metadata(row: dict) -> dict:
    """Write metadata.json (and the manifest's copy) for a job."""
    doc = _metadata_document(row)
    crawl_dir = _crawl_dir(row)
    try:
        md.write_document(crawl_dir, doc)
        md.update_manifest(crawl_dir, doc)
    except OSError as exc:
        log.warning("Could not write metadata for crawl %s: %s", row["id"], exc)
    return doc


# ---- collections -----------------------------------------------------------

def _collection_of(row: dict | None) -> dict | None:
    return _store().get_collection((row or {}).get("collection_id"))


def _collection_view(row: dict, counts: dict | None = None,
                     with_bytes: bool = True) -> dict:
    counts = counts if counts is not None else _store().collection_counts()
    entry = counts.get(int(row["id"]), {"jobs": 0, "by_status": {}, "last_activity": None})
    root = Path(row["root_dir"])
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row.get("description") or "",
        "root_dir": str(root),
        "metadata": list(row.get("metadata") or []),
        "metadata_fields": len(row.get("metadata") or []),
        "policy": colls.policy_of(row),
        "index": _index_counts(row) if with_bytes else None,
        "warc_index": _collection_index_summary(root) if with_bytes else None,
        "inherited_by_jobs": colls.inherited_fields(row),
        "jobs": entry["jobs"],
        "by_status": entry["by_status"],
        "active_jobs": sum(n for status, n in entry["by_status"].items()
                           if status in ("running", "paused", "blocked", "stopping")),
        "last_activity": entry["last_activity"] or row.get("updated_at"),
        "bytes": _dir_size(root) if with_bytes and root.exists() else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _collection_index_summary(root: Path) -> dict | None:
    from .warc_indexer import collection_summary
    try:
        return collection_summary(root)
    except OSError:
        return None


def _collection_metadata_from(payload: dict) -> list[dict] | None:
    if "metadata" not in payload:
        return None
    raw = payload.get("metadata")
    try:
        if isinstance(raw, dict) and ("job" in raw or "seeds" in raw):
            raw = raw.get("job")
        return md.normalise_fields(raw)
    except ValueError as exc:
        raise HTTPException(400, f"metadata: {exc}") from exc


def _collection_policy_from(payload: dict) -> dict | None:
    """The policy a request carries, or None when it says nothing about it."""
    if "dedup_across_jobs" not in payload:
        return None
    return {"dedup_across_jobs": bool(payload.get("dedup_across_jobs"))}


def _referenced_warcs(row: dict) -> list[Path]:
    collection = _collection_of(row)
    index = colls.read_index(collection)
    if index is None:
        return []
    try:
        referenced = index.referenced_jobs(int(row["id"]))
    finally:
        index.close()
    found: list[Path] = []
    for job_id in referenced:
        other = _store().get_crawl(job_id)
        if not other:
            continue
        other_dir = _crawl_dir(other)
        found += sorted(other_dir.glob("*.warc.gz")) + sorted(other_dir.glob("*.warc"))
    return found


def _page_changes(crawl_dir: Path) -> dict | None:
    """The counts from a job's changes.json, for its row."""
    from .changes import read_report
    report = read_report(crawl_dir)
    return dict(report.get("counts") or {}) if report else None


def _dedup_summary(crawl_dir: Path) -> dict | None:
    """dedup-summary.json, written by the WARC writer, if the job has one."""
    import json
    path = crawl_dir / "dedup-summary.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _index_counts(collection: dict) -> dict | None:
    """What the collection's index holds, or None when it has none yet."""
    index = colls.read_index(collection)
    if index is None:
        return None
    try:
        return index.counts()
    finally:
        index.close()


def _create_collection(payload: dict) -> dict:
    """Make a collection: its directory, its collection.json and its row."""
    try:
        name = colls.validate_name(payload.get("name"))
        description = colls.validate_description(payload.get("description"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    metadata = _collection_metadata_from(payload) or []
    policy = _collection_policy_from(payload) or dict(colls.DEFAULT_POLICY)
    try:
        return colls.create(_store(), name, description, metadata,
                            _storage_root_for(payload.get("storage_dir")), policy=policy)
    except ValueError as exc:                        # taken, or an earlier index in the way
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"could not create the collection's directory: {exc}") from exc


def _default_collection() -> dict:
    """Where a job goes when it names no collection."""
    try:
        return colls.ensure_default(_store(), _default_storage_root())
    except (ValueError, OSError) as exc:
        raise HTTPException(500, f"the default collection could not be made: {exc}") from exc


def _resolve_collection(payload: dict) -> dict | None:
    """The collection a create request names, made if it asks for a new one;
    the default collection when it names none.

    ``collection_id`` names one by id; ``collection`` by id, identifier or
    name; ``new_collection`` is a {name, description?, metadata?} to make
    first. A name that matches nothing is an error, not a new collection:
    a typo must not file a job in a collection of its own.
    """
    if payload.get("new_collection"):
        spec = payload["new_collection"]
        if not isinstance(spec, dict):
            raise HTTPException(400, "new_collection must be an object with a name")
        return _create_collection(spec)
    reference = payload.get("collection_id")
    if reference in (None, ""):
        reference = payload.get("collection")
    if reference in (None, ""):
        return _default_collection()               # every job belongs to a collection
    row = _store().find_collection(reference)
    if not row:
        raise HTTPException(404, f"collection not found: {reference}")
    return row


def _names_location(payload: dict) -> bool:
    """Whether the request chose a storage location of its own."""
    return bool(str(payload.get("storage_dir") or "").strip())


def _job_home(collection: dict | None, storage_root: Path, crawl_id: int,
              own: bool = False) -> Path:
    """A job's directory: the location it named for itself, else under its
    collection. A job from before collections, with none, keeps its place
    under the storage root."""
    if own or not collection:
        return storage_root / str(crawl_id)
    return colls.job_home(collection["root_dir"], crawl_id)


def _refresh_collection_document(collection: dict | None) -> None:
    """Keep collection.json's list of jobs current."""
    colls.refresh_document(_store(), collection)


def _require_collection(collection_id: int) -> dict:
    row = _store().get_collection(collection_id)
    if not row:
        raise HTTPException(404, "collection not found")
    return row


def _monitor() -> resources.ResourceMonitor:
    assert _MONITOR is not None
    return _MONITOR


def _resource_thresholds() -> dict:
    return resources.thresholds_from_settings(_store().get_setting)


def _resource_check(storage_root: Path | None = None) -> dict:
    """Whether a job should be warned before it starts, and why."""
    snap = _monitor().snapshot(storage_root)
    thresholds = _resource_thresholds()
    warnings = resources.evaluate(snap, thresholds)
    return {"ok": not warnings, "warnings": warnings, "snapshot": snap,
            "thresholds": thresholds}


def _job_usage(row: dict) -> dict | None:
    """CPU and memory of a job's worker tree, when it has one."""
    if _MONITOR is None or not _worker_alive(row):
        return None
    return _MONITOR.processes.usage(row.get("pid"))


# Collections whose index is being rebuilt: no job of theirs may start
# meanwhile, or its captures would go to the index file being replaced.
_REBUILDING: set[int] = set()
_REBUILD_LOCK = __import__("threading").Lock()


def _collection_rebuilding(row: dict) -> bool:
    with _REBUILD_LOCK:
        return bool(row.get("collection_id")) and int(row["collection_id"]) in _REBUILDING


def _launch(crawl_id: int) -> None:
    row = _store().get_crawl(crawl_id)
    if row and _collection_rebuilding(row):
        raise HTTPException(409, "this collection's index is being rebuilt; start the job "
                                 "once that is done")
    # pending until the worker reports running: a waiting job must leave
    # the waiting state the moment it is launched, or the next tick would
    # launch it again
    _store().set_status(crawl_id, PENDING)
    pid = _launch_worker(crawl_id)
    _store().set_pid(crawl_id, pid)


def _start_or_wait(crawl_id: int, payload: dict) -> None:
    """Launch a job now, or hold it until the machine has room.

    ``start`` in the request is "now" (the default) or "wait". A waiting
    job is created in full -- its directory, its config -- and launched by
    the monitor's next tick that finds every resource above its warning
    level, or by hand from the dashboard.
    """
    choice = str(payload.get("start") or "now").strip().lower()
    if choice not in ("now", "wait"):
        raise HTTPException(400, "start must be 'now' or 'wait'")
    if choice == "wait":
        _store().set_status(crawl_id, WAITING)
        return
    _launch(crawl_id)


def _sample_jobs() -> None:
    """Keep each running job's reading warm.

    CPU use is a rate between two readings, so a job read only when the
    dashboard asks would show zero on every first look; the monitor reads
    every worker on each tick instead.
    """
    for row in _store().list_crawls():
        if row.get("status") in _ACTIVE_STATES + (PENDING,):
            _job_usage(row)


def _on_tick(snapshot: dict) -> None:
    _sample_jobs()
    _launch_waiting(snapshot)


def _launch_waiting(snapshot: dict) -> None:
    """Start the oldest waiting job if the machine has room for it.

    One per tick: the job just started needs a sample or two before its
    own use shows in the reading, and launching every waiting job at once
    would recreate the shortage the curator chose to wait out.
    """
    waiting = [r for r in _store().list_crawls() if r.get("status") == WAITING
               and not _collection_rebuilding(r)]
    if not waiting:
        return
    job = min(waiting, key=lambda r: r["id"])
    check_snapshot = dict(snapshot)
    check_snapshot["disk"] = resources.disk_snapshot(_crawl_dir(job))  # its own disk
    if resources.evaluate(check_snapshot, _resource_thresholds()):
        return
    log.info("Resources are free again; starting waiting crawl %s", job["id"])
    _launch(job["id"])


def _theme_name_of(row: dict) -> str | None:
    """The name of a crawl's theme, or None; a row whose configuration
    cannot be read is still listed."""
    try:
        config = json.loads(row.get("config_json") or "{}")
        theme = config.get("theme") if isinstance(config, dict) else None
        return (theme.get("name") or "theme") if isinstance(theme, dict) and theme.get("enabled", True) else None
    except (TypeError, ValueError):
        return None


def _crawl_view(row: dict) -> dict:
    row = _reconcile(row)
    progress = _store().get_progress(row["id"])
    crawl_dir = _crawl_dir(row)
    disk_bytes = _dir_size(crawl_dir)
    reported = sum(p["bytes"] for p in progress)
    visited = sum(p["visited"] for p in progress)
    queued = sum(p["queued"] for p in progress)
    failed = sum(p["failed"] for p in progress)
    status = row["status"]
    return {
        "id": row["id"],
        "kind": row.get("kind", "crawl"),
        "name": row["name"],
        "status": status,
        "control": row["control"],
        "pid": row["pid"],
        "seeds_total": row["seeds_total"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        # Where this crawl's files are, so the dashboard can show a capture
        # kept somewhere other than the default without guessing.
        "output_dir": str(crawl_dir),
        "has_selection": (crawl_dir / "pages" / "selection.html").is_file(),
        "theme": _theme_name_of(row),
        "collection": colls.brief(_collection_of(row)),
        "dedup": _dedup_summary(crawl_dir),
        "changes": _page_changes(crawl_dir),
        "totals": {"visited": visited, "queued": queued, "failed": failed,
                   "bytes": max(disk_bytes, reported)},
        "seeds": progress,
        # what this job's worker, browser and helpers are using right now
        "resources": _job_usage(row),
        "metadata_fields": _metadata_count(row),
        # replay needs an archive, not just a described folder: metadata.json
        # alone gives a job a size but nothing to replay
        "warc_files": _warc_count(crawl_dir),
        # the last indexing run of a social capture, if any
        "index": _index_summary(crawl_dir),
        # the last warc-indexer run over a crawl's or recording's WARCs, if any
        "warc_index": _warc_index_summary(crawl_dir),
    }


_WARC_INDEXABLE_KINDS = ("crawl", KIND_RECORDING)


def _warc_index_summary(crawl_dir: Path) -> dict | None:
    from .warc_indexer import summary
    try:
        return summary(crawl_dir)
    except OSError:
        return None


_SOCIAL_KINDS = (KIND_FACEBOOK, KIND_INSTAGRAM, KIND_X, KIND_YOUTUBE)


def _index_summary(crawl_dir: Path) -> dict | None:
    """What the dashboard shows about a capture's index: counts and when."""
    from .indexer import read_index_manifest
    manifest = read_index_manifest(crawl_dir)
    if not manifest:
        return None
    return {key: manifest.get(key) for key in
            ("platform", "documents", "by_type", "located", "unlocated",
             "warc_files", "invalid", "generated_at", "collection")}


def _warc_count(crawl_dir: Path) -> int:
    try:
        return sum(1 for _ in crawl_dir.glob("*.warc.gz")) + sum(1 for _ in crawl_dir.glob("*.warc"))
    except OSError:
        return 0


def _metadata_count(row: dict) -> int:
    """How many fields the curator gave this job, all levels together."""
    import json
    try:
        meta = md.from_config(json.loads(row["config_json"]))
    except (ValueError, TypeError, KeyError):
        return 0
    return len(meta["job"]) + sum(len(v) for v in meta["seeds"].values())


def create_app(db_path: str, warc_root: str, simulate: bool = False,
               replay_root: str = "./replay", bind_host: str = "127.0.0.1",
               allow_remote_recording: bool = False,
               monitor_resources: bool = True) -> FastAPI:
    """Build the control server.

    ``monitor_resources`` starts the sampling thread that keeps the machine
    reading warm and launches jobs told to wait; tests pass False and drive
    the monitor's ``tick()`` themselves.
    """
    global _STORE, _WARC_ROOT, _SIMULATE, _REPLAY_ROOT, _BIND_HOST, \
        _ALLOW_REMOTE_RECORDING, _MONITOR
    _STORE = Store(db_path)
    _WARC_ROOT = Path(warc_root)
    _WARC_ROOT.mkdir(parents=True, exist_ok=True)
    _SIMULATE = simulate
    _REPLAY_ROOT = Path(replay_root)
    _BIND_HOST = bind_host
    _ALLOW_REMOTE_RECORDING = allow_remote_recording
    if _MONITOR is not None:
        _MONITOR.stop()
    _MONITOR = resources.ResourceMonitor(_WARC_ROOT, on_tick=_on_tick)
    if monitor_resources:
        _MONITOR.start()
    if resources.measurement_note():
        log.warning("%s", resources.measurement_note())

    def _leave_cleanly() -> None:
        """Ctrl+C must end the process: stop what the dashboard started."""
        global _PYWB
        if _PYWB is not None:
            try:
                _PYWB.stop()
            except Exception as exc:               # pragma: no cover
                log.debug("Replay server did not stop cleanly: %s", exc)
            _PYWB = None
        if _MONITOR is not None:
            _MONITOR.stop()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # The app's lifespan replaces the on_event hook, which newer FastAPI
        # versions warn about on every start.
        yield
        _leave_cleanly()

    app = FastAPI(title="Simple Webcrawl Manager (SWM) control server",
                  version="0.3.0", lifespan=_lifespan)

    # crawls left mid-flight by a previous server are settled at once
    for stale in _STORE.list_crawls():
        try:
            _reconcile(stale)
        except Exception as exc:               # pragma: no cover
            log.warning("Could not reconcile crawl %s: %s", stale.get("id"), exc)

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        html = DASHBOARD.read_text(encoding="utf-8")
        hardening = DASHBOARD_HARDENING.read_text(encoding="utf-8")
        injected = f"<script>\n{hardening}\n</script>\n</body>"
        return html.replace("</body>", injected, 1)

    @app.get("/api/help")
    def help_text():
        """The wording behind each "?"; an installation's own copy wins."""
        from . import help as help_module
        return help_module.load_help(Path(_store().db_path).resolve().parent)

    @app.get("/api/capabilities")
    def capabilities():
        visible = _recording_capability()
        return {
            "recording": visible,
            "facebook": dict(visible),
            "simulate": _SIMULATE,
            "storage": _storage_is_curator_choosable(),
            "instagram": _instagram_capability(),
            "x": _x_capability(),
            "youtube": _youtube_capability(),
            "theme_ai": _theme_ai_capability(),
            "warc_indexer": _warc_indexer_capability(),
        }

    @app.post("/api/theme/check")
    def theme_check(payload: dict = Body(...)):
        """Judge one page's text against a theme, for calibrating a theme
        before a run. {"theme": {...}, "page": {"url", "title", "html" or
        "text"}, "use_ai": bool}"""
        from .theme import PageText, ThemeConfig, ThemeJudge, extract_page_text, make_ai_judge
        try:
            theme = ThemeConfig.from_dict(payload.get("theme") or {})
        except ValueError as exc:
            raise HTTPException(400, f"theme: {exc}") from exc
        page_raw = payload.get("page") or {}
        if not isinstance(page_raw, dict):
            raise HTTPException(400, "page must be an object")
        url = str(page_raw.get("url") or "https://example.org/")
        if page_raw.get("html"):
            page = extract_page_text(str(page_raw["html"]), url)
        else:
            page = PageText(url=url, title=str(page_raw.get("title") or ""),
                            headline=str(page_raw.get("headline") or page_raw.get("title") or ""),
                            body=str(page_raw.get("text") or ""), main_found=True)
        ai = make_ai_judge(_store().get_setting) if payload.get("use_ai") else None
        judge = ThemeJudge(theme, ai, None)
        decision = judge.judge_page(page, hub=judge.rules.is_hub(url))
        return {"page": page.to_dict(), **decision.to_dict(), "ai_configured": ai is not None}

    @app.post("/api/theme/ai/test")
    def theme_ai_test(payload: dict | None = Body(None)):
        """One small question to the configured AI judge, to prove the
        settings work before a run depends on them."""
        from .theme import AIJudgeError, PageText, ThemeConfig, make_ai_judge
        ai = make_ai_judge(_store().get_setting)
        if ai is None:
            raise HTTPException(409, _theme_ai_capability().get("reason") or "No AI judge is configured.")
        theme = ThemeConfig.from_dict({"name": "libraries", "terms": ["library"],
                                       "brief": "News about public libraries.",
                                       "ai_input": str(payload.get("ai_input") or "compact")
                                       if isinstance(payload, dict) else "compact"})
        page = PageText(url="https://example.org/news/library-opens", title="New public library opens",
                        headline="New public library opens", body="The city opened a new public "
                        "library on Monday with a reading room and a children's section.",
                        main_found=True)
        try:
            verdict = ai.judge_page(theme, page)
        except AIJudgeError as exc:
            raise HTTPException(502, f"The AI judge did not answer: {exc}") from exc
        return {"ok": True, "judge": ai.describe(), "verdict": verdict}

    @app.post("/api/recordings")
    def create_recording(payload: dict = Body(...)):
        """Create + launch an interactive recording session.

        Accepts {"url": ..., "name"?, "browser"? (headed|native),
        "operator"?}. The worker opens a visible browser on this machine."""
        from urllib.parse import urlsplit

        cap = _recording_capability()
        if not cap["available"]:
            raise HTTPException(409, cap["reason"])

        url = str(payload.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HTTPException(400, "url must be an http(s) URL")
        browser_mode = payload.get("browser") or "headed"
        if browser_mode not in ("headed", "native"):
            raise HTTPException(400, "browser must be 'headed' or 'native'")

        name = str(payload.get("name") or f"rec-{parts.hostname}").strip()
        if not name:
            name = f"rec-{parts.hostname}"
        if len(name) > 200:
            raise HTTPException(400, "recording name must be 200 characters or fewer")
        operator = str(payload.get("operator") or "webarc").strip() or "webarc"
        if len(operator) > 200:
            raise HTTPException(400, "operator must be 200 characters or fewer")

        config = {
            "recording": {
                "start_url": url,
                "operator": operator,
                "browser": {"mode": browser_mode},
            },
            "seeds": [{"url": url}],
            "metadata": _metadata_from(payload, [url]),
        }
        # Resolved before the row exists: a location that cannot serve
        # should fail the request, not leave a crawl pointing nowhere.
        storage_root = _storage_root_for(payload.get("storage_dir"))
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="", seeds_total=1,
            kind=KIND_RECORDING,
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)

        _start_or_wait(crawl_id, payload)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    def _active_facebook_job() -> dict | None:
        for job in _store().list_crawls():
            if (job.get("kind") == KIND_FACEBOOK
                    and job.get("status") in (
                        PENDING, RUNNING, PAUSED, BLOCKED, STOPPING)
                    and _pid_alive(job.get("pid"))):
                return job
        return None

    def _launch_facebook_job(name: str, facebook: dict,
                             storage_root: Path, payload: dict) -> JSONResponse:
        """Validate, persist and launch one Facebook job configuration."""
        from .facebook import FacebookCaptureConfig

        try:
            FacebookCaptureConfig.from_dict(facebook)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        active = _active_facebook_job()
        if active and not _SIMULATE:
            raise HTTPException(
                409,
                f"Facebook capture #{active['id']} is already using the "
                "persistent browser profile. Stop it before starting another.",
            )
        config = {
            "facebook": facebook,
            "seeds": [{"url": facebook["page_url"]}],
            "metadata": _metadata_from(payload, [facebook["page_url"]]),
        }
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="", seeds_total=1,
            kind=KIND_FACEBOOK,
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)
        _start_or_wait(crawl_id, payload)
        return JSONResponse(
            status_code=201,
            content=_crawl_view(_store().get_crawl(crawl_id)),
        )

    @app.get("/api/facebook/state")
    def facebook_state(url: str):
        from .facebook import canonical_facebook_page_url, facebook_page_key

        try:
            page_url = canonical_facebook_page_url(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        state = _store().get_facebook_page(facebook_page_key(page_url))
        return {"available": bool(state), "state": state}

    @app.post("/api/facebook")
    def create_facebook_capture(payload: dict = Body(...)):
        """Open a visible browser and capture one Facebook Page timeline."""
        from .facebook import canonical_facebook_page_url, facebook_page_key

        cap = _recording_capability()
        if not cap["available"]:
            raise HTTPException(409, cap["reason"])
        try:
            page_url = canonical_facebook_page_url(payload.get("page_url"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        page_key = facebook_page_key(page_url)
        mode = str(payload.get("mode") or "date_range")
        browser_mode = str(payload.get("browser") or "headed")
        if browser_mode not in ("headed", "native"):
            raise HTTPException(400, "browser must be 'headed' or 'native'")

        operator = str(payload.get("operator") or "webarc").strip() or "webarc"
        if len(operator) > 200:
            raise HTTPException(400, "operator must be 200 characters or fewer")
        default_name = "fb-" + page_key.split(":", 1)[-1].strip("/").replace(
            "/", "-")
        name = str(payload.get("name") or default_name or "facebook-page").strip()
        if not name:
            name = "facebook-page"
        if len(name) > 200:
            raise HTTPException(400, "capture name must be 200 characters or fewer")

        profile_dir = Path(_store().db_path).resolve().parent / \
            "browser-profiles" / "facebook"
        facebook = {
            "page_url": page_url,
            "page_key": page_key,
            "mode": mode,
            "from_date": payload.get("from_date"),
            "to_date": payload.get("to_date"),
            "latest_n": payload.get("latest_n"),
            "consecutive_older": 5,
            "capture_media": bool(payload.get("capture_media", True)),
            "write_warc": bool(payload.get("write_warc", True)),
            "auto_start": bool(payload.get("auto_start", True)),
            "include_comments": bool(payload.get("include_comments", False)),
            "max_comments_per_post": payload.get("max_comments_per_post", 25),
            "include_replies": bool(payload.get("include_replies", False)),
            "operator": operator,
            "browser": {
                "mode": browser_mode,
                "user_data_dir": str(profile_dir),
            },
        }
        if mode == "since_last":
            previous = _store().get_facebook_page(page_key)
            if not previous or not previous.get("newest_post_date"):
                raise HTTPException(
                    409,
                    "No previous capture state exists for this Facebook Page. "
                    "Run another capture mode first.",
                )
            facebook["prior_newest_post_id"] = previous.get("newest_post_id")
            facebook["prior_newest_post_date"] = previous.get("newest_post_date")
        return _launch_facebook_job(
            name, facebook, _storage_root_for(payload.get("storage_dir")),
            payload)

    @app.post("/api/config/parse")
    def parse_config(payload: dict = Body(...)):
        """Parse raw YAML so the dashboard can populate the guided editor."""
        return {"config": _parse_yaml(payload.get("config_yaml"))}

    @app.post("/api/config/render")
    def render_config(payload: dict = Body(...)):
        """Render guided-editor JSON as readable YAML for the raw editor."""
        config = _validate_config(payload.get("config"))
        rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        return {"config_yaml": rendered}

    @app.get("/api/crawls")
    def list_crawls():
        return [_crawl_view(r) for r in _store().list_crawls()]

    @app.get("/api/crawls/{crawl_id}")
    def get_crawl(crawl_id: int):
        row = _store().get_crawl(crawl_id)
        if not row:
            raise HTTPException(404, "crawl not found")
        return _crawl_view(row)

    @app.post("/api/crawls")
    def create_crawl(payload: dict = Body(...)):
        """Accepts {"config_yaml": "..."} or {"config": {...}} plus optional name."""
        if "config_yaml" in payload:
            config = _parse_yaml(payload["config_yaml"])
        elif "config" in payload:
            config = _validate_config(payload["config"])
        else:
            raise HTTPException(400, "provide config_yaml or config")

        name = payload.get("name") or config.get("crawl_name", "webarc-crawl")
        storage_root = _storage_root_for(payload.get("storage_dir"))
        seed_urls = [str(seed["url"]) for seed in config["seeds"]]
        if "metadata" in payload:
            # the request's metadata wins over any in the YAML; a seed's own
            # block inside the YAML still counts
            config["metadata"] = _metadata_from(payload, seed_urls)
        else:
            try:
                config["metadata"] = md.normalise(config.get("metadata"), seeds=seed_urls)
            except ValueError as exc:
                raise HTTPException(400, f"metadata: {exc}") from exc
        # create once to obtain the id, then point the config at its own dir
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="",
            seeds_total=len(config["seeds"]),
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config["output_dir"] = str(crawl_dir)
        config.setdefault("crawl_name", name)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)

        _start_or_wait(crawl_id, payload)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    def _require(crawl_id: int) -> dict:
        row = _store().get_crawl(crawl_id)
        if not row:
            raise HTTPException(404, "crawl not found")
        return row

    @app.post("/api/crawls/{crawl_id}/pause")
    def pause(crawl_id: int):
        _require(crawl_id)
        _store().set_control(crawl_id, CTRL_PAUSE)
        return {"ok": True, "control": CTRL_PAUSE}

    @app.post("/api/crawls/{crawl_id}/resume")
    def resume(crawl_id: int):
        _require(crawl_id)
        _store().set_control(crawl_id, CTRL_RESUME)
        return {"ok": True, "control": CTRL_RESUME}

    @app.post("/api/crawls/{crawl_id}/start")
    def start_now(crawl_id: int):
        """Launch a job that was told to wait, without waiting any longer."""
        row = _require(crawl_id)
        if row["status"] != WAITING:
            raise HTTPException(409, "Only a job that is waiting to start can be started.")
        _launch(crawl_id)
        return {"ok": True, "status": RUNNING}

    @app.post("/api/crawls/{crawl_id}/stop")
    def stop(crawl_id: int):
        row = _require(crawl_id)
        if row["status"] == WAITING:
            # never launched: there is nothing to stop, only the wait to end
            _store().set_status(crawl_id, STOPPED,
                                "Cancelled before it started.")
            return {"ok": True, "control": CTRL_NONE, "settled": True}
        if not _worker_alive(row):
            # nothing to ask: settle it now rather than wait for a report
            # that will never come
            if row["status"] in _ACTIVE_STATES:
                _store().set_status(crawl_id, STOPPED, _LOST_WORKER)
            _store().clear_control(crawl_id)
            return {"ok": True, "control": CTRL_NONE, "settled": True}
        _store().set_control(crawl_id, CTRL_STOP)
        _store().set_status(crawl_id, STOPPING)
        return {"ok": True, "control": CTRL_STOP}

    @app.post("/api/crawls/{crawl_id}/continue")
    def continue_capture(crawl_id: int):
        """Create a provenance-linked continuation of a Facebook capture.

        A continuation deliberately starts a new WARC set and manifest. It
        re-scrolls through already observed post IDs without re-exporting them,
        then continues into records not yet present in the durable Page index.
        """
        import json

        row = _require(crawl_id)
        if row.get("kind") != KIND_FACEBOOK:
            raise HTTPException(409, "Only Facebook captures can be continued.")
        if _pid_alive(row.get("pid")):
            raise HTTPException(409, "Stop the current capture before continuing it.")
        try:
            source = json.loads(row["config_json"])
            facebook = dict(source["facebook"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(500, "The stored Facebook configuration is invalid.") from exc
        facebook["continuation_of"] = crawl_id
        facebook["root_capture_id"] = (
            facebook.get("root_capture_id") or crawl_id
        )
        name = f"{row['name']} — continuation"
        # A continuation belongs beside the capture it continues, whatever
        # the current default is and whether or not this dashboard would let
        # a curator name that location today.
        return _launch_facebook_job(name[:200], facebook,
                                    _crawl_dir(row).parent, {})

    @app.get("/api/instagram/state")
    def instagram_state(target: str):
        from .instagram import parse_instagram_target

        try:
            parsed = parse_instagram_target(target)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        state = _store().get_instagram_target(parsed.key)
        return {"available": bool(state and state.get("newest_media_id")),
                "state": state, "key": parsed.key, "label": parsed.label}

    @app.post("/api/instagram")
    def create_instagram_capture(payload: dict = Body(...)):
        """Start a background Instagram capture over one or more targets.

        No browser opens unless a person is needed. The session comes from the
        dedicated Instagram browser profile, which the curator signs into once.
        """
        from .instagram import InstagramCaptureConfig, parse_instagram_target

        operator = str(payload.get("operator") or "webarc").strip() or "webarc"
        if len(operator) > 200:
            raise HTTPException(400, "operator must be 200 characters or fewer")
        browser_mode = str(payload.get("browser") or "headed")
        if browser_mode not in ("headed", "native"):
            raise HTTPException(400, "browser must be 'headed' or 'native'")
        listing = str(payload.get("listing") or "browser")
        if listing not in ("browser", "gallery-dl"):
            raise HTTPException(400, "listing must be 'browser' or 'gallery-dl'")
        if listing == "gallery-dl":
            from .instagram_gallery import gallery_dl_version
            if not gallery_dl_version():
                raise HTTPException(
                    400, "gallery-dl is not installed. Install it with: "
                         "pip install gallery-dl")
        profile_dir = Path(_store().db_path).resolve().parent / \
            "browser-profiles" / "instagram"
        instagram = {
            "browser": {"mode": browser_mode, "user_data_dir": str(profile_dir)},
            "listing": listing,
            "targets": payload.get("targets"),
            "mode": str(payload.get("mode") or "latest_n"),
            "from_date": payload.get("from_date"),
            "to_date": payload.get("to_date"),
            "latest_n": payload.get("latest_n"),
            "surfaces": payload.get("surfaces") or ["posts", "reels"],
            "capture_media": bool(payload.get("capture_media", True)),
            "include_comments": bool(payload.get("include_comments", False)),
            "max_comments_per_post": payload.get("max_comments_per_post", 25),
            "include_replies": bool(payload.get("include_replies", False)),
            "max_replies_per_comment": payload.get("max_replies_per_comment", 10),
            "write_warc": bool(payload.get("write_warc", False)),
            "operator": operator,
            "browser_profile_dir": str(profile_dir),
        }
        # "since last" needs each profile's previous newest post; look them up
        # here so a target without one is refused before anything starts.
        if instagram["mode"] == "since_last":
            prior = {}
            for item in (payload.get("targets") or []):
                try:
                    target = parse_instagram_target(item)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                state = _store().get_instagram_target(target.key)
                if state and state.get("newest_media_id"):
                    prior[target.key] = {"media_id": state["newest_media_id"],
                                         "date": state.get("newest_post_date")}
            instagram["prior_newest"] = prior
        try:
            config = InstagramCaptureConfig.from_dict(instagram)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        labels = [parse_instagram_target(u).label for u in config.targets]
        default_name = "ig-" + "-".join(l.lstrip("@").replace(" ", "-")
                                        for l in labels[:3])
        if len(labels) > 3:
            default_name += f"-and-{len(labels) - 3}-more"
        name = str(payload.get("name") or default_name).strip()[:200] or "instagram"
        storage_root = _storage_root_for(payload.get("storage_dir"))
        config_json = {"instagram": instagram,
                       "seeds": [{"url": u} for u in config.targets],
                       "metadata": _metadata_from(payload, list(config.targets))}
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config_json, output_dir="",
            seeds_total=len(config.targets), kind=KIND_INSTAGRAM,
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config_json["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config_json, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)
        _start_or_wait(crawl_id, payload)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    @app.get("/api/youtube/state")
    def youtube_state(target: str):
        from .youtube import parse_youtube_target

        try:
            parsed = parse_youtube_target(target)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        state = _store().get_youtube_target(parsed.key)
        return {"available": bool(state and state.get("newest_item_id")),
                "state": state, "key": parsed.key, "label": parsed.label}

    @app.post("/api/youtube")
    def create_youtube_capture(payload: dict = Body(...)):
        """Start a YouTube capture over channels, videos and playlists."""
        from .youtube import YouTubeCaptureConfig, parse_youtube_target

        operator = str(payload.get("operator") or "webarc").strip() or "webarc"
        if len(operator) > 200:
            raise HTTPException(400, "operator must be 200 characters or fewer")
        browser_mode = str(payload.get("browser") or "headed")
        if browser_mode not in ("headed", "native"):
            raise HTTPException(400, "browser must be 'headed' or 'native'")
        profile_dir = Path(_store().db_path).resolve().parent / "browser-profiles" / "youtube"
        youtube = {
            "browser": {"mode": browser_mode, "user_data_dir": str(profile_dir)},
            "targets": payload.get("targets"),
            "mode": str(payload.get("mode") or "latest_n"),
            "from_date": payload.get("from_date"),
            "to_date": payload.get("to_date"),
            "latest_n": payload.get("latest_n"),
            "surfaces": payload.get("surfaces") or ["videos", "shorts", "streams", "posts"],
            "capture_media": bool(payload.get("capture_media", True)),
            "max_resolution": str(payload.get("max_resolution") or "1080"),
            "thumbnails": bool(payload.get("thumbnails", True)),
            "captions": bool(payload.get("captions", True)),
            "auto_captions": bool(payload.get("auto_captions", True)),
            "live_chat": bool(payload.get("live_chat", True)),
            "post_media": bool(payload.get("post_media", True)),
            "include_comments": bool(payload.get("include_comments", True)),
            "max_comments_per_item": payload.get("max_comments_per_item", 1000),
            "include_replies": bool(payload.get("include_replies", True)),
            "comment_sort": str(payload.get("comment_sort") or "new"),
            "write_warc": bool(payload.get("write_warc", False)),
            "operator": operator,
            "browser_profile_dir": str(profile_dir),
        }
        if youtube["mode"] == "since_last":
            prior = {}
            for item in (payload.get("targets") or []):
                try:
                    target = parse_youtube_target(item)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                state = _store().get_youtube_target(target.key)
                if state and state.get("newest_item_id"):
                    prior[target.key] = {"item_id": state["newest_item_id"],
                                         "date": state.get("newest_item_date")}
            youtube["prior_newest"] = prior
        try:
            config = YouTubeCaptureConfig.from_dict(youtube)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        parsed = [parse_youtube_target(u) for u in config.targets]
        refused = _youtube_refusal(config, parsed)
        if refused:
            raise HTTPException(400, refused)
        default_name = "yt-" + "-".join(_youtube_name_part(t) for t in parsed[:3])
        if len(parsed) > 3:
            default_name += f"-and-{len(parsed) - 3}-more"
        name = str(payload.get("name") or default_name).strip()[:200] or "youtube"
        storage_root = _storage_root_for(payload.get("storage_dir"))
        config_json = {"youtube": youtube,
                       "seeds": [{"url": u} for u in config.targets],
                       "metadata": _metadata_from(payload, list(config.targets))}
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config_json, output_dir="",
            seeds_total=len(config.targets), kind=KIND_YOUTUBE,
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config_json["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config_json, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)
        _start_or_wait(crawl_id, payload)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    @app.get("/api/x/state")
    def x_state(target: str):
        from .x import parse_x_target

        try:
            parsed = parse_x_target(target)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        state = _store().get_x_target(parsed.key)
        return {"available": bool(state and state.get("newest_post_id")),
                "state": state, "key": parsed.key, "label": parsed.label}

    @app.post("/api/x")
    def create_x_capture(payload: dict = Body(...)):
        """Start an X capture over one or more targets: handles, profile
        addresses, post addresses, hashtags and searches.

        A Chrome window opens on the dedicated X browser profile, which the
        curator signs into once; X's own client makes every request.
        """
        from .x import XCaptureConfig, parse_x_target

        operator = str(payload.get("operator") or "webarc").strip() or "webarc"
        if len(operator) > 200:
            raise HTTPException(400, "operator must be 200 characters or fewer")
        browser_mode = str(payload.get("browser") or "headed")
        if browser_mode not in ("headed", "native"):
            raise HTTPException(400, "browser must be 'headed' or 'native'")
        profile_dir = Path(_store().db_path).resolve().parent / "browser-profiles" / "x"
        x = {
            "browser": {"mode": browser_mode, "user_data_dir": str(profile_dir)},
            "targets": payload.get("targets"),
            "mode": str(payload.get("mode") or "latest_n"),
            "from_date": payload.get("from_date"),
            "to_date": payload.get("to_date"),
            "latest_n": payload.get("latest_n"),
            "surfaces": payload.get("surfaces") or ["posts"],
            "search_product": str(payload.get("search_product") or "Latest"),
            "capture_media": bool(payload.get("capture_media", True)),
            "keep_reposts": bool(payload.get("keep_reposts", True)),
            "include_conversation": bool(payload.get("include_conversation", False)),
            "max_replies_per_post": payload.get("max_replies_per_post", 50),
            "write_warc": bool(payload.get("write_warc", False)),
            "operator": operator,
            "browser_profile_dir": str(profile_dir),
        }
        if x["mode"] == "since_last":
            prior = {}
            for item in (payload.get("targets") or []):
                try:
                    target = parse_x_target(item, x["search_product"])
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                state = _store().get_x_target(target.key)
                if state and state.get("newest_post_id"):
                    prior[target.key] = {"post_id": state["newest_post_id"],
                                         "date": state.get("newest_post_date")}
            x["prior_newest"] = prior
        try:
            config = XCaptureConfig.from_dict(x)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        labels = [parse_x_target(u, config.search_product) for u in config.targets]
        default_name = "x-" + "-".join(_x_name_part(t) for t in labels[:3])
        if len(labels) > 3:
            default_name += f"-and-{len(labels) - 3}-more"
        name = str(payload.get("name") or default_name).strip()[:200] or "x"
        storage_root = _storage_root_for(payload.get("storage_dir"))
        config_json = {"x": x,
                       "seeds": [{"url": u} for u in config.targets],
                       "metadata": _metadata_from(payload, list(config.targets))}
        collection = _resolve_collection(payload)
        crawl_id = _store().create_crawl(
            name=name, config=config_json, output_dir="",
            seeds_total=len(config.targets), kind=KIND_X,
            collection_id=(collection or {}).get("id"))
        crawl_dir = _job_home(collection, storage_root, crawl_id,
                              own=_names_location(payload))
        config_json["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config_json, str(crawl_dir))
        _write_metadata(_store().get_crawl(crawl_id))
        _refresh_collection_document(collection)
        _start_or_wait(crawl_id, payload)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    @app.get("/api/crawls/{crawl_id}/metadata")
    def read_metadata(crawl_id: int):
        """A job's descriptive metadata: its own fields, each seed's, and
        what the outputs carry once defaults are filled in."""
        return _metadata_document(_require(crawl_id))

    @app.put("/api/crawls/{crawl_id}/metadata")
    def write_metadata(crawl_id: int, payload: dict = Body(...)):
        """Change a job's metadata after the fact.

        metadata.json and a social capture's manifest are rewritten; a WARC
        already written keeps the values of its moment, which the document
        notes rather than rewriting archive files.
        """
        import json

        row = _require(crawl_id)
        config = json.loads(row["config_json"])
        seeds = [str(s.get("url")) for s in config.get("seeds", []) if isinstance(s, dict)]
        config["metadata"] = _metadata_from(payload, seeds)
        for seed in config.get("seeds", []):
            if isinstance(seed, dict):
                seed.pop("metadata", None)        # the job's block now says it all
        _store().finalize_config(crawl_id, config, row["output_dir"])
        return _write_metadata(_store().get_crawl(crawl_id))

    @app.get("/api/crawls/{crawl_id}/metadata.csv")
    def export_metadata(crawl_id: int):
        from fastapi.responses import PlainTextResponse

        row = _require(crawl_id)
        text = md.csv_text(_metadata_document(row))
        name = f"metadata-{crawl_id}.csv"
        return PlainTextResponse(text, media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/metadata/parse")
    def parse_metadata(payload: dict = Body(...)):
        """Read a metadata sheet (as exported, or Archive-It's shape) back
        into the job/seeds structure the forms use."""
        text = payload.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "provide csv text")
        try:
            return md.parse_csv(text)
        except ValueError as exc:
            raise HTTPException(400, f"The sheet could not be read: {exc}") from exc

    @app.post("/api/crawls/{crawl_id}/kill")
    def kill(crawl_id: int):
        """Force stop: end the worker if it is one of ours, settle the state."""
        row = _require(crawl_id)
        if _worker_alive(row):
            _terminate(row["pid"])
        _store().set_status(crawl_id, STOPPED, "Stopped by force from the dashboard.")
        _store().clear_control(crawl_id)
        return {"ok": True}

    @app.delete("/api/crawls/{crawl_id}")
    def delete(crawl_id: int, purge: bool = False, force: bool = False):
        row = _require(crawl_id)
        if _worker_alive(row):
            if not force:
                raise HTTPException(
                    409, "crawl is still running; stop it first, or delete "
                         "with force to end its worker")
            _terminate(row["pid"])
        if _warc_indexing(crawl_id, _crawl_dir(row)):
            raise HTTPException(
                409, "this job's WARCs are being indexed; wait for the run to finish")
        collection = _collection_of(row)
        # The collection's index forgets the job before anything is removed:
        # were the index left holding this job's originals, later jobs would
        # refer to WARCs that no longer exist. If it cannot be updated,
        # nothing is deleted.
        orphaned = 0
        index = colls.read_index(collection)
        if index is not None:
            try:
                orphaned = index.forget_job(crawl_id)
            except Exception as exc:
                raise HTTPException(
                    503, f"the collection's index could not be updated ({exc}); "
                         "nothing was deleted, try again shortly") from exc
            finally:
                index.close()
        elif collection is not None and colls.index_leftover(collection["root_dir"]):
            raise HTTPException(
                503, "the collection's index could not be opened; nothing was deleted, "
                     "try again shortly")
        if purge:
            shutil.rmtree(_crawl_dir(row), ignore_errors=True)
        _store().delete_crawl(crawl_id)
        _refresh_collection_document(collection)
        return {"ok": True, "purged": purge, "forced": force,
                "orphaned_records": orphaned}

    @app.get("/captures/{crawl_id}/{kind}/{path:path}")
    def capture_file(crawl_id: int, kind: str, path: str):
        """Serve a Facebook capture's rendered pages and their media.

        Pages and media are siblings inside the capture directory, so serving
        them under a shared prefix keeps the relative references in the pages
        working without copying media into a second location.
        """
        from fastapi.responses import FileResponse

        if kind not in ("pages", "media"):
            raise HTTPException(404, "not found")
        base = (_crawl_dir(_require(crawl_id)) / kind).resolve()
        try:
            target = (base / path).resolve()
            target.relative_to(base)      # refuse anything outside the capture
        except (ValueError, OSError):
            raise HTTPException(404, "not found") from None
        if not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    @app.post("/api/crawls/{crawl_id}/replay")
    def replay(crawl_id: int, request: Request):
        """Build a ReplayWeb.page site for this crawl and return the replay URL."""
        global _PYWB
        row = _require(crawl_id)
        from .replay import (ReplayServer, build_replay_site, collection_name)
        crawl_dir = _crawl_dir(row)
        warcs = sorted(crawl_dir.glob("*.warc.gz")) + sorted(crawl_dir.glob("*.warc"))
        # A job in a collection may hold revisit records whose originals
        # live in earlier jobs; those WARCs come along, or the pages replay
        # without their content.
        warcs += _referenced_warcs(row)

        # A Facebook capture is read through the pages built from its records.
        # They are built inside the capture directory, beside the media they
        # reference, and served from there so those references resolve. When
        # the capture also has a WARC, both ways in are offered: replay shows
        # the Page as it first loaded, the pages show what was collected.
        from .facebook_render import build_site, is_facebook_capture
        from .instagram_render import build_site as build_instagram_site
        from .instagram_render import is_instagram_capture
        from .x_render import build_site as build_x_site
        from .x_render import is_x_capture
        from .youtube_render import build_site as build_youtube_site
        from .youtube_render import is_youtube_capture
        pages_url = None
        if is_youtube_capture(crawl_dir):
            try:
                build_youtube_site(crawl_dir)
                pages_url = f"/captures/{crawl_id}/pages/index.html"
            except Exception as exc:
                if not warcs:
                    raise HTTPException(
                        500, f"could not build capture pages: {exc}") from exc
                log.warning("Could not build capture pages for %d: %s", crawl_id, exc)
        elif is_x_capture(crawl_dir):
            try:
                build_x_site(crawl_dir)
                pages_url = f"/captures/{crawl_id}/pages/index.html"
            except Exception as exc:
                if not warcs:
                    raise HTTPException(
                        500, f"could not build capture pages: {exc}") from exc
                log.warning("Could not build capture pages for %d: %s",
                            crawl_id, exc)
        elif is_instagram_capture(crawl_dir):
            try:
                build_instagram_site(crawl_dir)
                pages_url = f"/captures/{crawl_id}/pages/index.html"
            except Exception as exc:
                if not warcs:
                    raise HTTPException(
                        500, f"could not build capture pages: {exc}") from exc
                log.warning("Could not build capture pages for %d: %s",
                            crawl_id, exc)
        elif is_facebook_capture(crawl_dir):
            try:
                build_site(crawl_dir)
                pages_url = f"/captures/{crawl_id}/pages/index.html"
            except Exception as exc:
                if not warcs:
                    raise HTTPException(
                        500, f"could not build capture pages: {exc}") from exc
                logging.getLogger(__name__).warning(
                    "Could not build capture pages for %d: %s", crawl_id, exc)

        if not warcs:
            if pages_url:
                return {"pages_url": pages_url, "kind": "capture_pages"}
            raise HTTPException(409, "no WARC files captured yet for this crawl")

        coll = collection_name(crawl_id)
        youtube_media = None
        if is_youtube_capture(crawl_dir):
            youtube_media = _youtube_replay_media(crawl_dir, crawl_id, str(request.base_url))
        try:
            build_replay_site(warcs, _REPLAY_ROOT / coll,
                              seed_url=row_seed_url(crawl_id), youtube_media=youtube_media)
        except Exception as exc:
            raise HTTPException(500, f"replay setup failed: {exc}") from exc
        if _PYWB is None or not _PYWB.is_running():
            server = ReplayServer(_REPLAY_ROOT, port=8091)
            try:
                server.start_background()
            except OSError as exc:
                # nothing kept: the next click tries again
                raise HTTPException(
                    500, f"the replay server could not start: {exc}") from exc
            _PYWB = server

        return {"collection": coll, "replay_url": _PYWB.replay_url(coll),
                "pages_url": pages_url}

    @app.post("/api/crawls/{crawl_id}/index")
    def index_capture(crawl_id: int, payload: dict | None = Body(default=None)):
        """Index a finished social capture's records into warc-indexer's
        document schema, beside the capture in index/. Runs in the request,
        like replay: the records are small and the WARC scan reads headers
        only."""
        from . import indexer
        row = _require(crawl_id)
        if row.get("kind") not in _SOCIAL_KINDS:
            raise HTTPException(
                409, "Only Facebook, Instagram, X and YouTube captures can be indexed.")
        if _pid_alive(row.get("pid")):
            raise HTTPException(409, "Stop the capture before indexing it.")
        collection = source_root = None
        relocate = False
        if isinstance(payload, dict):
            if str(payload.get("collection") or "").strip():
                collection = str(payload["collection"]).strip()
            if str(payload.get("source_root") or "").strip():
                source_root = str(payload["source_root"]).strip()
            relocate = bool(payload.get("relocate"))
        try:
            if relocate:
                # only the pointers change: no re-reading of records or WARCs
                moved = indexer.relocate_index(_crawl_dir(row), source_root)
                moved["download_url"] = f"/api/crawls/{crawl_id}/index.jsonl"
                return moved
            result = indexer.index_capture(_crawl_dir(row), collection=collection,
                                           source_root=source_root)
        except indexer.IndexingError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"indexing failed: {exc}") from exc
        body = result.to_dict()
        body["download_url"] = f"/api/crawls/{crawl_id}/index.jsonl"
        return body

    @app.post("/api/crawls/{crawl_id}/warc-index", status_code=202)
    def warc_index(crawl_id: int, payload: dict | None = Body(default=None)):
        """Run the warc-indexer jar over a crawl's or recording's WARC files.
        The jar runs on its own thread and writes <warc>.jsonl beside each
        WARC; the job card shows the run's state as it goes."""
        import threading

        from . import warc_indexer
        row = _require(crawl_id)
        if row.get("kind") not in _WARC_INDEXABLE_KINDS:
            raise HTTPException(
                409, "Only automated crawls and recordings are indexed from their WARCs; "
                     "social captures are indexed from their records with Index.")
        if _pid_alive(row.get("pid")):
            raise HTTPException(409, "Stop the job before indexing its WARCs.")
        crawl_dir = _crawl_dir(row)
        if _warc_indexing(crawl_id, crawl_dir):
            raise HTTPException(409, "This job's WARCs are being indexed already.")
        get_setting = _store().get_setting
        cap = warc_indexer.capability(get_setting)
        if not cap["available"]:
            raise HTTPException(409, cap["reason"] or "warc-indexer is unavailable")
        collection = None
        chosen = None
        if isinstance(payload, dict):
            if str(payload.get("collection") or "").strip():
                collection = str(payload["collection"]).strip()
            if str(payload.get("warc") or "").strip():
                chosen = [Path(str(payload["warc"])).name]     # one file, by name only
        if not collection:
            member_of = _collection_of(row)
            collection = member_of["name"] if member_of else row["name"]
        warcs = warc_indexer.warc_files(crawl_dir)
        if not warcs:
            raise HTTPException(409, "no WARC files captured yet for this job")
        if chosen and not any(w.name == chosen[0] for w in warcs):
            raise HTTPException(404, f"{chosen[0]} is not one of this job's WARC files")

        with _WARC_INDEX_LOCK:
            if crawl_id in _WARC_INDEX_RUNS:
                raise HTTPException(409, "This job's WARCs are being indexed already.")
            _WARC_INDEX_RUNS.add(crawl_id)

        def run():
            try:
                warc_indexer.index_warcs(crawl_dir, warcs=chosen, collection=collection,
                                         get_setting=get_setting)
            except Exception as exc:                    # noqa: BLE001 - recorded for the card
                log.warning("warc-indexer run for %d failed: %s", crawl_id, exc)
                try:
                    warc_indexer._write_manifest(crawl_dir, {
                        "schema": "swm-warc-index-run/1", "status": warc_indexer.STATUS_FAILED,
                        "error": str(exc), "finished_at": warc_indexer._iso_now(),
                        "warcs": chosen or [w.name for w in warcs], "outputs": [],
                        "documents": 0})
                except OSError:                         # the folder itself is gone
                    log.warning("warc-indexer run for %d: no folder to record the failure in",
                                crawl_id)
            finally:
                with _WARC_INDEX_LOCK:
                    _WARC_INDEX_RUNS.discard(crawl_id)

        threading.Thread(target=run, name=f"warc-index-{crawl_id}", daemon=True).start()
        return {"status": warc_indexer.STATUS_RUNNING, "warcs": chosen or [w.name for w in warcs],
                "collection": collection, "jar": cap.get("jar"),
                "status_url": f"/api/crawls/{crawl_id}/warc-index"}

    @app.get("/api/crawls/{crawl_id}/warc-index")
    def warc_index_status(crawl_id: int):
        from .warc_indexer import read_manifest, summary
        crawl_dir = _crawl_dir(_require(crawl_id))
        manifest = read_manifest(crawl_dir)
        if not manifest:
            raise HTTPException(404, "this job's WARCs have not been indexed yet")
        manifest["summary"] = summary(crawl_dir)
        manifest.pop("pid", None)
        return manifest

    @app.get("/api/crawls/{crawl_id}/warc-index/log")
    def warc_index_log(crawl_id: int):
        """The jar's own output from the last run, as written."""
        from fastapi.responses import PlainTextResponse

        from .warc_indexer import LOG_NAME
        path = _crawl_dir(_require(crawl_id)) / LOG_NAME
        if not path.is_file():
            raise HTTPException(404, "this job's WARCs have not been indexed yet")
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))

    @app.get("/api/crawls/{crawl_id}/index")
    def index_status(crawl_id: int):
        from .indexer import read_index_manifest
        manifest = read_index_manifest(_crawl_dir(_require(crawl_id)))
        if not manifest:
            raise HTTPException(404, "this capture has not been indexed yet")
        manifest["download_url"] = f"/api/crawls/{crawl_id}/index.jsonl"
        return manifest

    @app.get("/api/crawls/{crawl_id}/index.jsonl")
    def index_download(crawl_id: int):
        from fastapi.responses import FileResponse

        from .indexer import read_index_manifest
        row = _require(crawl_id)
        manifest = read_index_manifest(_crawl_dir(row))
        target = Path(manifest["output"]) if manifest and manifest.get("output") else None
        if not target or not target.is_file():
            raise HTTPException(404, "this capture has not been indexed yet")
        return FileResponse(target, media_type="application/x-ndjson",
                            filename=target.name)

    def row_seed_url(crawl_id: int) -> str | None:
        prog = _store().get_progress(crawl_id)
        return prog[0]["seed_url"] if prog else None

    @app.get("/api/browse")
    def browse(path: str | None = None, show_hidden: bool = False):
        """List the directories inside one directory, for choosing a location.

        A browser cannot hand back a filesystem path -- a native picker gives
        a handle or a relative name, neither of which the worker could write
        to -- and captures are written by the server anyway, so the
        directories that matter are the server's own. Only directory names are
        returned: never a file listing, never a file's contents. The same rule
        that governs naming a path governs seeing one.
        """
        choosable = _storage_is_curator_choosable()
        if not choosable["available"]:
            raise HTTPException(403, choosable["reason"])
        raw = str(path or "").strip()
        try:
            target = (Path(raw).expanduser() if raw else Path.home()).resolve()
        except OSError as exc:
            raise HTTPException(400, f"That location cannot be opened: {exc}")
        if not target.is_dir():
            raise HTTPException(400, f"{target} is not a directory.")
        entries: list[dict] = []
        unreadable = None
        try:
            for child in sorted(target.iterdir(),
                                key=lambda c: c.name.lower()):
                if not show_hidden and child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue      # a broken link, or a mount that will not stat
        except (PermissionError, OSError) as exc:
            # Say so in the listing rather than failing the request: the
            # curator can still go back up or type a path.
            unreadable = f"{target} cannot be read: {exc.strerror or exc}"
        return {
            "path": str(target),
            "parent": None if target.parent == target else str(target.parent),
            "entries": entries,
            "writable": os.access(target, os.W_OK),
            "unreadable": unreadable,
            "places": _places(),
        }

    @app.get("/api/settings")
    def read_settings():
        configured = (_store().get_setting(_STORAGE_ROOT_SETTING) or "").strip()
        from .theme import ai_settings
        return {
            "storage_root": configured,
            "effective_storage_root": str(_default_storage_root()),
            "server_storage_root": str(_WARC_ROOT),
            "storage": _storage_is_curator_choosable(),
            "resources": _resource_thresholds(),
            "resources_measured": _monitor().snapshot().get("measured", False),
            "resources_note": resources.measurement_note(),
            "theme_ai": {**ai_settings(_store().get_setting), "capability": _theme_ai_capability()},
            "indexer": _warc_indexer_capability(),
        }

    @app.get("/api/resources")
    def resource_usage():
        """The machine's spare CPU, memory and disk, and each running job's share."""
        check = _resource_check(_default_storage_root())
        jobs = []
        for row in _store().list_crawls():
            if row.get("status") not in _ACTIVE_STATES + (PENDING,):
                continue
            usage = _job_usage(row)
            if usage is None:
                continue
            jobs.append({"id": row["id"], "name": row["name"],
                         "kind": row.get("kind", "crawl"),
                         "status": row["status"], **usage})
        waiting = [{"id": r["id"], "name": r["name"]}
                   for r in _store().list_crawls() if r.get("status") == WAITING]
        return {
            "measured": check["snapshot"].get("measured", False),
            "jobs_measured": resources.psutil is not None,
            "note": resources.measurement_note(),
            "snapshot": check["snapshot"],
            "thresholds": check["thresholds"],
            "warnings": check["warnings"],
            "jobs": jobs,
            "jobs_total": {
                "cpu_percent": sum(j["cpu_percent"] for j in jobs),
                "cpu_percent_of_machine": sum(j["cpu_percent_of_machine"] for j in jobs),
                "rss_bytes": sum(j["rss_bytes"] for j in jobs),
                "processes": sum(j["processes"] for j in jobs),
            },
            "waiting": waiting,
        }

    @app.get("/api/resources/check")
    def resource_check(storage_dir: str | None = None):
        """Whether a job about to start should be warned, and why.

        The dashboard asks before every start; a warning is the curator's
        cue to start anyway, wait, or cancel. Never a refusal.
        """
        root = _storage_root_for(storage_dir) if storage_dir else _default_storage_root()
        return _resource_check(root)

    @app.put("/api/settings")
    def write_settings(payload: dict = Body(...)):
        """Change where captures go by default.

        Existing crawls are not moved or re-pointed: each records the
        directory it was written to, so a changed default applies to captures
        made after it.
        """
        if not any(k in payload for k in ("storage_root", "resources", "theme_ai", "indexer")):
            raise HTTPException(400, "provide storage_root, resources, theme_ai or indexer")
        if "theme_ai" in payload:
            _write_theme_ai_settings(payload.get("theme_ai"))
        if "indexer" in payload:
            from . import warc_indexer
            try:
                accepted = warc_indexer.validate_settings(payload.get("indexer"))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            for key, value in accepted.items():
                _store().set_setting(warc_indexer.SETTING_PREFIX + key, value)
        if "storage_root" in payload:
            requested = str(payload.get("storage_root") or "").strip()
            if requested:
                choosable = _storage_is_curator_choosable()
                if not choosable["available"]:
                    raise HTTPException(403, choosable["reason"])
                _usable_directory(Path(requested),
                                  "That default storage location")
            _store().set_setting(_STORAGE_ROOT_SETTING, requested)
        if "resources" in payload:
            wanted = payload.get("resources")
            if not isinstance(wanted, dict):
                raise HTTPException(400, "resources must be an object")
            try:
                accepted = resources.validate_thresholds(wanted)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            for key, value in accepted.items():
                text = ("true" if value else "false") if key == "enabled" else f"{value:g}"
                _store().set_setting(resources.SETTING_PREFIX + key, text)
        return read_settings()

    # -- collections -------------------------------------------------------
    @app.get("/api/collections")
    def list_collections():
        counts = _store().collection_counts()
        return [_collection_view(r, counts) for r in _store().list_collections()]

    @app.post("/api/collections")
    def create_collection(payload: dict = Body(...)):
        row = _create_collection(payload)
        return JSONResponse(status_code=201, content=_collection_view(row))

    @app.get("/api/collections/{collection_id}")
    def get_collection(collection_id: int):
        row = _require_collection(collection_id)
        view = _collection_view(row)
        view["job_list"] = [_crawl_view(r) for r in
                            _store().crawls_in_collection(collection_id)]
        return view

    @app.put("/api/collections/{collection_id}")
    def update_collection(collection_id: int, payload: dict = Body(...)):
        """Rename or redescribe a collection. The identifier and directory
        never change; every job's metadata.json is rewritten with what it
        now inherits."""
        row = _require_collection(collection_id)
        try:
            name = colls.validate_name(payload["name"]) if "name" in payload else None
            description = (colls.validate_description(payload.get("description"))
                           if "description" in payload else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        metadata = _collection_metadata_from(payload)
        policy = _collection_policy_from(payload)
        if policy is not None:
            policy = {**colls.policy_of(row), **policy}
        _store().update_collection(collection_id, name=name,
                                   description=description, metadata=metadata,
                                   policy=policy)
        row = _store().get_collection(collection_id)
        _refresh_collection_document(row)
        for job in _store().crawls_in_collection(collection_id):
            _write_metadata(job)
        return _collection_view(row)

    @app.get("/api/collections/{collection_id}/jobs")
    def collection_jobs(collection_id: int):
        _require_collection(collection_id)
        return [_crawl_view(r) for r in _store().crawls_in_collection(collection_id)]

    @app.get("/api/collections/{collection_id}/impact")
    def collection_impact(collection_id: int):
        """What deleting this collection would do, before it is done."""
        row = _require_collection(collection_id)
        jobs = [_reconcile(j) for j in _store().crawls_in_collection(collection_id)]
        root = Path(row["root_dir"])
        return colls.collection_impact(
            row, jobs, bytes_on_disk=_dir_size(root) if root.exists() else 0,
            running=[j["id"] for j in jobs if _worker_alive(j)])

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: int, purge: bool = False,
                          force: bool = False):
        """Delete a collection and its jobs, as the impact report said.

        A running job stops the deletion unless forced; purge removes the
        collection's directory and everything under it from disk.
        """
        row = _require_collection(collection_id)
        jobs = [_reconcile(j) for j in _store().crawls_in_collection(collection_id)]
        alive = [j for j in jobs if _worker_alive(j)]
        if alive and not force:
            raise HTTPException(
                409, f"{len(alive)} job(s) in this collection are still running; "
                     "stop them first, or delete with force to end their workers")
        for job in alive:
            _terminate(job["pid"])
        removed = _store().delete_collection(collection_id)
        if purge:
            shutil.rmtree(Path(row["root_dir"]), ignore_errors=True)
        # The WARCs may stay on disk; the index is bookkeeping about jobs
        # that no longer exist and must not be inherited by a namesake.
        colls.remove_index(row["root_dir"])
        return {"ok": True, "purged": purge, "forced": force,
                "jobs_removed": removed}

    @app.get("/api/collections/{collection_id}/orphans")
    def collection_orphans(collection_id: int):
        """Pages whose original was deleted: what a re-crawl should fetch."""
        row = _require_collection(collection_id)
        index = colls.read_index(row)
        if index is None:
            return {"urls": [], "records": []}
        try:
            return {"urls": index.orphan_urls(), "records": index.orphans()}
        finally:
            index.close()

    @app.post("/api/collections/{collection_id}/warc-index", status_code=202)
    def warc_index_collection(collection_id: int, payload: dict | None = Body(default=None)):
        """Run the warc-indexer jar over every crawl's and recording's WARCs
        in the collection, one job after another, each document carrying
        the collection's name. Revisit records that point into another
        job resolve to that job's document when the outputs are loaded
        together."""
        import threading

        from . import warc_indexer
        row = _require_collection(collection_id)
        root = Path(row["root_dir"])
        jobs = [_reconcile(j) for j in _store().crawls_in_collection(collection_id)
                if j.get("kind", "crawl") in _WARC_INDEXABLE_KINDS]
        alive = [j["id"] for j in jobs if _worker_alive(j)]
        if alive:
            raise HTTPException(409, f"job(s) {', '.join(map(str, alive))} are still running; "
                                     "index the collection once it is quiet")
        if warc_indexer.collection_is_running(root):
            raise HTTPException(409, "this collection's WARCs are being indexed already")
        busy = [j["id"] for j in jobs if _warc_indexing(j["id"], _crawl_dir(j))]
        if busy:
            raise HTTPException(409, f"job(s) {', '.join(map(str, busy))} are being indexed "
                                     "on their own; wait for those runs")
        cap = warc_indexer.capability(_store().get_setting)
        if not cap["available"]:
            raise HTTPException(409, cap["reason"] or "warc-indexer is unavailable")
        with_warcs = [(j["id"], _crawl_dir(j)) for j in jobs
                      if warc_indexer.warc_files(_crawl_dir(j))]
        if not with_warcs:
            raise HTTPException(409, "no WARC files in this collection yet")
        collection = row["name"]
        if isinstance(payload, dict) and str(payload.get("collection") or "").strip():
            collection = str(payload["collection"]).strip()
        get_setting = _store().get_setting
        with _WARC_INDEX_LOCK:
            for crawl_id, _dir in with_warcs:
                _WARC_INDEX_RUNS.add(crawl_id)

        def run():
            try:
                warc_indexer.index_collection(root, with_warcs, collection=collection,
                                              get_setting=get_setting)
            except Exception as exc:                    # noqa: BLE001 - recorded for the row
                log.warning("collection-wide warc-indexer run for %d failed: %s",
                            collection_id, exc)
            finally:
                with _WARC_INDEX_LOCK:
                    for crawl_id, _dir in with_warcs:
                        _WARC_INDEX_RUNS.discard(crawl_id)

        threading.Thread(target=run, name=f"warc-index-collection-{collection_id}",
                         daemon=True).start()
        return {"status": warc_indexer.STATUS_RUNNING, "collection": collection,
                "jobs": [crawl_id for crawl_id, _dir in with_warcs],
                "status_url": f"/api/collections/{collection_id}/warc-index"}

    @app.get("/api/collections/{collection_id}/warc-index")
    def warc_index_collection_status(collection_id: int):
        from .warc_indexer import collection_summary, read_collection_manifest
        row = _require_collection(collection_id)
        manifest = read_collection_manifest(Path(row["root_dir"]))
        if not manifest:
            raise HTTPException(404, "this collection's WARCs have not been indexed together yet")
        manifest["summary"] = collection_summary(Path(row["root_dir"]))
        manifest.pop("pid", None)
        return manifest

    @app.post("/api/collections/{collection_id}/rebuild-index")
    def rebuild_collection_index(collection_id: int):
        """Make the collection's index anew from its jobs' WARC files: for
        jobs made before the index existed, or an index that was lost."""
        row = _require_collection(collection_id)
        jobs = [_reconcile(j) for j in _store().crawls_in_collection(collection_id)]
        alive = [j["id"] for j in jobs if _worker_alive(j) or j.get("status") == PENDING]
        if alive:
            raise HTTPException(
                409, f"job(s) {', '.join(map(str, alive))} are still running; the index is "
                     "rebuilt once the collection is quiet")
        with _REBUILD_LOCK:
            if collection_id in _REBUILDING:
                raise HTTPException(409, "this collection's index is being rebuilt already")
            _REBUILDING.add(collection_id)
        try:
            result = colls.rebuild_index(_store(), row)
        except OSError as exc:
            raise HTTPException(500, f"the index could not be rebuilt: {exc}") from exc
        finally:
            with _REBUILD_LOCK:
                _REBUILDING.discard(collection_id)
        return {"ok": True, **result}

    @app.post("/api/collections/{collection_id}/replay")
    def replay_collection(collection_id: int, request: Request):
        """Replay every WARC of every job in the collection as one archive,
        opened at a page listing the jobs' start pages by website."""
        global _PYWB
        from .replay import ReplayServer, build_replay_site, build_start_page
        row = _require_collection(collection_id)
        warcs: list[Path] = []
        entries: list[dict] = []
        jobs = _store().crawls_in_collection(collection_id)
        for job in sorted(jobs, key=lambda j: j["id"], reverse=True):
            job_dir = _crawl_dir(job)
            warcs += sorted(job_dir.glob("*.warc.gz")) + sorted(job_dir.glob("*.warc"))
            kind = job.get("kind", "crawl")
            # a social capture is read through its own pages, served by the
            # dashboard, when they have been built
            pages = job_dir / "pages" / "index.html"
            href = (f"{str(request.base_url).rstrip('/')}/captures/{job['id']}/pages/index.html"
                    if kind not in _WARC_INDEXABLE_KINDS and pages.is_file() else None)
            for seed in _store().get_progress(job["id"]):
                entries.append({"url": seed["seed_url"], "job": job["name"], "kind": kind,
                                "date": job.get("created_at"), "href": href})
        if not warcs:
            raise HTTPException(409, "no WARC files in this collection yet")
        coll = f"collection-{row['slug']}"
        try:
            site = build_replay_site(warcs, _REPLAY_ROOT / coll)
            build_start_page(site, row["name"], entries)
        except Exception as exc:
            raise HTTPException(500, f"replay setup failed: {exc}") from exc
        if _PYWB is None or not _PYWB.is_running():
            server = ReplayServer(_REPLAY_ROOT, port=8091)
            try:
                server.start_background()
            except OSError as exc:
                raise HTTPException(
                    500, f"the replay server could not start: {exc}") from exc
            _PYWB = server
        return {"collection": coll, "replay_url": _PYWB.replay_url(coll, "seeds.html"),
                "start_pages": len(entries), "warc_files": len(warcs)}

    @app.get("/api/crawls/{crawl_id}/changes")
    def crawl_changes(crawl_id: int):
        """What this job found new, changed, unchanged and gone against the
        collection's earlier captures, page by page."""
        from .changes import read_report
        row = _require(crawl_id)
        report = read_report(_crawl_dir(row))
        if report is None:
            raise HTTPException(404, "no change report for this job: it is not in a "
                                     "collection with an index, or it has not ended yet")
        return report

    @app.get("/api/crawls/{crawl_id}/impact")
    def crawl_impact(crawl_id: int):
        """What deleting this job would do, before it is done."""
        row = _reconcile(_require(crawl_id))
        collection = _collection_of(row)
        siblings = _store().crawls_in_collection(collection["id"]) if collection else []
        referring = None
        index = colls.read_index(collection)
        if index is not None:
            try:
                referring = index.referring_into(crawl_id)
            finally:
                index.close()
        return colls.job_impact(collection, row, siblings, referring)

    @app.get("/api/storage")
    def storage():
        crawls = _store().list_crawls()
        per_crawl = []
        total = 0
        for r in crawls:
            b = _dir_size(_crawl_dir(r))
            total += b
            per_crawl.append({"id": r["id"], "name": r["name"], "bytes": b})
        usage = shutil.disk_usage(_WARC_ROOT)
        per_collection = [
            {"id": c["id"], "name": c["name"], "slug": c["slug"],
             "bytes": _dir_size(Path(c["root_dir"])) if Path(c["root_dir"]).exists() else 0}
            for c in _store().list_collections()]
        return {
            "warc_root": str(_WARC_ROOT),
            "total_bytes": total,
            "per_crawl": per_crawl,
            "per_collection": per_collection,
            "disk": {"total": usage.total, "used": usage.used,
                     "free": usage.free},
            "default_disk": resources.disk_snapshot(_default_storage_root()),
        }

    return app
