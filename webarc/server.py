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

A crawl runs as an isolated subprocess (webarc.worker). Pause/resume/stop are
delivered through the store's control column, which the worker polls between
pages. Stop also hard-kills the pid as a fallback if the worker is wedged.
"""

from __future__ import annotations

import logging

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .store import (BLOCKED, CTRL_PAUSE, CTRL_RESUME, CTRL_STOP, KIND_FACEBOOK,
                    KIND_INSTAGRAM, KIND_RECORDING, PAUSED, PENDING, RUNNING,
                    STOPPED, STOPPING, Store)

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
        raise HTTPException(400, f"{label} cannot be created: {exc}")
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
    """Instagram capture runs in the background; a display is needed only to
    sign in, and the dashboard says so rather than refusing outright."""
    try:
        import instaloader  # noqa: F401
    except ImportError:
        return {"available": False,
                "reason": "Instagram capture needs the instaloader package. "
                          "Install it with: pip install instaloader"}
    visible = _recording_capability()
    note = None if visible["available"] else (
        "No graphical desktop: the capture can run, but signing in to "
        "Instagram or clearing a checkpoint needs a browser window, which "
        "cannot open here.")
    return {"available": True, "reason": None, "note": note}


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
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if h:
            # distinguish a still-running process from a not-yet-reaped zombie
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(h)
            return exit_code.value == 259  # STILL_ACTIVE
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


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
        os.killpg(os.getpgid(pid), signal.SIGTERM)
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


def _crawl_view(row: dict) -> dict:
    progress = _store().get_progress(row["id"])
    crawl_dir = _crawl_dir(row)
    disk_bytes = _dir_size(crawl_dir)
    reported = sum(p["bytes"] for p in progress)
    visited = sum(p["visited"] for p in progress)
    queued = sum(p["queued"] for p in progress)
    failed = sum(p["failed"] for p in progress)
    # reconcile "running" flag with actual process liveness
    status = row["status"]
    if status in (RUNNING,) and not _pid_alive(row["pid"]):
        status = row["status"]  # leave as-is; worker updates final state itself
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
        "totals": {"visited": visited, "queued": queued, "failed": failed,
                   "bytes": max(disk_bytes, reported)},
        "seeds": progress,
    }


def create_app(db_path: str, warc_root: str, simulate: bool = False,
               replay_root: str = "./replay", bind_host: str = "127.0.0.1",
               allow_remote_recording: bool = False) -> FastAPI:
    global _STORE, _WARC_ROOT, _SIMULATE, _REPLAY_ROOT, _BIND_HOST, \
        _ALLOW_REMOTE_RECORDING
    _STORE = Store(db_path)
    _WARC_ROOT = Path(warc_root)
    _WARC_ROOT.mkdir(parents=True, exist_ok=True)
    _SIMULATE = simulate
    _REPLAY_ROOT = Path(replay_root)
    _BIND_HOST = bind_host
    _ALLOW_REMOTE_RECORDING = allow_remote_recording

    app = FastAPI(title="Simple Webcrawl Manager (SWM) control server",
                  version="0.2.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        html = DASHBOARD.read_text(encoding="utf-8")
        hardening = DASHBOARD_HARDENING.read_text(encoding="utf-8")
        injected = f"<script>\n{hardening}\n</script>\n</body>"
        return html.replace("</body>", injected, 1)

    @app.get("/api/capabilities")
    def capabilities():
        visible = _recording_capability()
        return {
            "recording": visible,
            "facebook": dict(visible),
            "simulate": _SIMULATE,
            "storage": _storage_is_curator_choosable(),
            "instagram": _instagram_capability(),
        }

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
        }
        # Resolved before the row exists: a location that cannot serve
        # should fail the request, not leave a crawl pointing nowhere.
        storage_root = _storage_root_for(payload.get("storage_dir"))
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="", seeds_total=1,
            kind=KIND_RECORDING)
        crawl_dir = storage_root / str(crawl_id)
        config["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))

        pid = _launch_worker(crawl_id)
        _store().set_pid(crawl_id, pid)
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
                             storage_root: Path) -> JSONResponse:
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
        }
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="", seeds_total=1,
            kind=KIND_FACEBOOK,
        )
        crawl_dir = storage_root / str(crawl_id)
        config["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))
        pid = _launch_worker(crawl_id)
        _store().set_pid(crawl_id, pid)
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
            name, facebook, _storage_root_for(payload.get("storage_dir")))

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
        # create once to obtain the id, then point the config at its own dir
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="",
            seeds_total=len(config["seeds"]))
        crawl_dir = storage_root / str(crawl_id)
        config["output_dir"] = str(crawl_dir)
        config.setdefault("crawl_name", name)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config, str(crawl_dir))

        pid = _launch_worker(crawl_id)
        _store().set_pid(crawl_id, pid)
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

    @app.post("/api/crawls/{crawl_id}/stop")
    def stop(crawl_id: int):
        _require(crawl_id)
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
                                    _crawl_dir(row).parent)

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
        profile_dir = Path(_store().db_path).resolve().parent / \
            "browser-profiles" / "instagram"
        instagram = {
            "browser": {"mode": browser_mode, "user_data_dir": str(profile_dir)},
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
                       "seeds": [{"url": u} for u in config.targets]}
        crawl_id = _store().create_crawl(
            name=name, config=config_json, output_dir="",
            seeds_total=len(config.targets), kind=KIND_INSTAGRAM)
        crawl_dir = storage_root / str(crawl_id)
        config_json["output_dir"] = str(crawl_dir)
        crawl_dir.mkdir(parents=True, exist_ok=True)
        _store().finalize_config(crawl_id, config_json, str(crawl_dir))
        pid = _launch_worker(crawl_id)
        _store().set_pid(crawl_id, pid)
        return JSONResponse(status_code=201,
                            content=_crawl_view(_store().get_crawl(crawl_id)))

    @app.post("/api/crawls/{crawl_id}/kill")
    def kill(crawl_id: int):
        """Hard-kill fallback if a worker won't stop gracefully."""
        row = _require(crawl_id)
        pid = row["pid"]
        if _pid_alive(pid):
            _terminate(pid)
        _store().set_status(crawl_id, STOPPED)
        return {"ok": True}

    @app.delete("/api/crawls/{crawl_id}")
    def delete(crawl_id: int, purge: bool = False):
        row = _require(crawl_id)
        if _pid_alive(row["pid"]):
            raise HTTPException(409, "crawl is still running; stop it first")
        if purge:
            shutil.rmtree(_crawl_dir(row), ignore_errors=True)
        _store().delete_crawl(crawl_id)
        return {"ok": True, "purged": purge}

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
    def replay(crawl_id: int):
        """Build a ReplayWeb.page site for this crawl and return the replay URL."""
        global _PYWB
        row = _require(crawl_id)
        from .replay import (ReplayServer, build_replay_site, collection_name)
        crawl_dir = _crawl_dir(row)
        warcs = sorted(crawl_dir.glob("*.warc.gz")) + sorted(crawl_dir.glob("*.warc"))

        # A Facebook capture is read through the pages built from its records.
        # They are built inside the capture directory, beside the media they
        # reference, and served from there so those references resolve. When
        # the capture also has a WARC, both ways in are offered: replay shows
        # the Page as it first loaded, the pages show what was collected.
        from .facebook_render import build_site, is_facebook_capture
        from .instagram_render import build_site as build_instagram_site
        from .instagram_render import is_instagram_capture
        pages_url = None
        if is_instagram_capture(crawl_dir):
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
        try:
            build_replay_site(warcs, _REPLAY_ROOT / coll,
                              seed_url=row_seed_url(crawl_id))
            if _PYWB is None:
                _PYWB = ReplayServer(_REPLAY_ROOT, port=8091)
                _PYWB.start_background()
        except Exception as exc:
            raise HTTPException(500, f"replay setup failed: {exc}") from exc

        return {"collection": coll, "replay_url": _PYWB.replay_url(coll),
                "pages_url": pages_url}

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
        return {
            "storage_root": configured,
            "effective_storage_root": str(_default_storage_root()),
            "server_storage_root": str(_WARC_ROOT),
            "storage": _storage_is_curator_choosable(),
        }

    @app.put("/api/settings")
    def write_settings(payload: dict = Body(...)):
        """Change where captures go by default.

        Existing crawls are not moved or re-pointed: each records the
        directory it was written to, so a changed default applies to captures
        made after it.
        """
        if "storage_root" not in payload:
            raise HTTPException(400, "provide storage_root")
        requested = str(payload.get("storage_root") or "").strip()
        if requested:
            choosable = _storage_is_curator_choosable()
            if not choosable["available"]:
                raise HTTPException(403, choosable["reason"])
            _usable_directory(Path(requested),
                              "That default storage location")
        _store().set_setting(_STORAGE_ROOT_SETTING, requested)
        return read_settings()

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
        return {
            "warc_root": str(_WARC_ROOT),
            "total_bytes": total,
            "per_crawl": per_crawl,
            "disk": {"total": usage.total, "used": usage.used,
                     "free": usage.free},
        }

    return app
