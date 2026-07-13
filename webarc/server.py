"""FastAPI control server for webarc.

Endpoints:
  GET  /                      -> dashboard HTML
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

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .store import (CTRL_PAUSE, CTRL_RESUME, CTRL_STOP, PENDING, RUNNING,
                    STOPPED, STOPPING, Store)

BASE = Path(__file__).resolve().parent
DASHBOARD = BASE / "dashboard.html"

# resolved at startup by create_app
_STORE: Store | None = None
_WARC_ROOT: Path = Path("./warcs")
_SIMULATE = False
_PYWB = None            # lazily-started PywbServer
_REPLAY_ROOT = Path("./replay")


def _store() -> Store:
    assert _STORE is not None
    return _STORE


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
    crawl_dir = _WARC_ROOT / str(row["id"])
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
        "name": row["name"],
        "status": status,
        "control": row["control"],
        "pid": row["pid"],
        "seeds_total": row["seeds_total"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "totals": {"visited": visited, "queued": queued, "failed": failed,
                   "bytes": max(disk_bytes, reported)},
        "seeds": progress,
    }


def create_app(db_path: str, warc_root: str, simulate: bool = False,
               replay_root: str = "./replay") -> FastAPI:
    global _STORE, _WARC_ROOT, _SIMULATE, _REPLAY_ROOT
    _STORE = Store(db_path)
    _WARC_ROOT = Path(warc_root)
    _WARC_ROOT.mkdir(parents=True, exist_ok=True)
    _SIMULATE = simulate
    _REPLAY_ROOT = Path(replay_root)

    app = FastAPI(title="webarc control server", version="0.2.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD.read_text(encoding="utf-8")

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
            try:
                config = yaml.safe_load(payload["config_yaml"])
            except yaml.YAMLError as exc:
                raise HTTPException(400, f"invalid YAML: {exc}")
        elif "config" in payload:
            config = payload["config"]
        else:
            raise HTTPException(400, "provide config_yaml or config")

        if not isinstance(config, dict) or not config.get("seeds"):
            raise HTTPException(400, "config must define at least one seed")

        name = payload.get("name") or config.get("crawl_name", "webarc-crawl")
        # create once to obtain the id, then point the config at its own dir
        crawl_id = _store().create_crawl(
            name=name, config=config, output_dir="",
            seeds_total=len(config["seeds"]))
        crawl_dir = _WARC_ROOT / str(crawl_id)
        config["output_dir"] = str(crawl_dir)
        config.setdefault("crawl_name", name)
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
        row = _require(crawl_id)
        _store().set_control(crawl_id, CTRL_STOP)
        _store().set_status(crawl_id, STOPPING)
        return {"ok": True, "control": CTRL_STOP}

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
            shutil.rmtree(_WARC_ROOT / str(crawl_id), ignore_errors=True)
        _store().delete_crawl(crawl_id)
        return {"ok": True, "purged": purge}

    @app.post("/api/crawls/{crawl_id}/replay")
    def replay(crawl_id: int):
        """Build a ReplayWeb.page site for this crawl and return the replay URL."""
        global _PYWB
        row = _require(crawl_id)
        from .replay import (ReplayServer, build_replay_site, collection_name)
        crawl_dir = _WARC_ROOT / str(crawl_id)
        warcs = sorted(crawl_dir.glob("*.warc.gz")) + sorted(crawl_dir.glob("*.warc"))
        if not warcs:
            raise HTTPException(409, "no WARC files captured yet for this crawl")

        coll = collection_name(crawl_id)
        try:
            build_replay_site(warcs, _REPLAY_ROOT / coll,
                              seed_url=row_seed_url(crawl_id))
            if _PYWB is None:
                _PYWB = ReplayServer(_REPLAY_ROOT, port=8091)
                _PYWB.start_background()
        except Exception as exc:
            raise HTTPException(500, f"replay setup failed: {exc}")

        return {"collection": coll, "replay_url": _PYWB.replay_url(coll)}

    def row_seed_url(crawl_id: int) -> str | None:
        prog = _store().get_progress(crawl_id)
        return prog[0]["seed_url"] if prog else None

    @app.get("/api/storage")
    def storage():
        crawls = _store().list_crawls()
        per_crawl = []
        total = 0
        for r in crawls:
            b = _dir_size(_WARC_ROOT / str(r["id"]))
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
