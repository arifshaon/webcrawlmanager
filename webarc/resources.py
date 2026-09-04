"""What the machine has left, and what each running job is using.

A capture is a browser plus a worker process, and several at once can
leave a machine with no memory or disk for any of them. The dashboard and
the command line both show the system's spare CPU, memory and disk and the
share each running job takes, and warn before a new job starts when any of
the three is below a threshold the curator sets.

Measurement comes from psutil where it is installed. Without it, disk
space is still measured (the standard library can), CPU and memory are
reported as unmeasured, and no warning is raised for them: a missing
library must not stop captures.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:                       # pragma: no cover - exercised in CI without psutil only by hand
    psutil = None


# Thresholds are "warn when less than this share is free". One rule for all
# three keeps the setting readable: below X % free, say so before starting.
DEFAULT_THRESHOLDS = {
    "enabled": True,
    "cpu_free_percent": 15,
    "memory_free_percent": 15,
    "disk_free_percent": 10,
}

SETTING_PREFIX = "resource_warn_"
THRESHOLD_KEYS = ("cpu_free_percent", "memory_free_percent", "disk_free_percent")

_LABELS = {
    "cpu": "CPU",
    "memory": "memory",
    "disk": "disk space",
}


def thresholds_from_settings(get_setting) -> dict:
    """Read the thresholds a store holds, falling back to the defaults.

    ``get_setting`` is ``Store.get_setting`` or anything with its shape. A
    value that does not parse is treated as unset rather than failing the
    check: a bad setting must not block starting a job.
    """
    out = dict(DEFAULT_THRESHOLDS)
    raw = get_setting(SETTING_PREFIX + "enabled")
    if raw is not None and str(raw).strip() != "":
        out["enabled"] = str(raw).strip().lower() not in ("0", "false", "no", "off")
    for key in THRESHOLD_KEYS:
        raw = get_setting(SETTING_PREFIX + key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            out[key] = value
    return out


def validate_thresholds(payload: dict) -> dict:
    """Check a settings payload; return the normalised subset it carries.

    Raises ValueError with a sentence the dashboard can show.
    """
    out: dict = {}
    if "enabled" in payload:
        out["enabled"] = bool(payload["enabled"])
    for key in THRESHOLD_KEYS:
        if key not in payload:
            continue
        raw = payload[key]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number between 0 and 100.")
        if not 0 <= value <= 100:
            raise ValueError(f"{key} must be between 0 and 100.")
        out[key] = value
    return out


def disk_snapshot(path: Path | str) -> dict:
    """Free space where captures go, or where they will go."""
    target = Path(path)
    try:
        target = target.expanduser().resolve()
    except OSError:
        pass
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return {"path": str(target), "total": 0, "free": 0, "used": 0,
                "free_percent": None, "error": str(exc)}
    free_percent = usage.free / usage.total * 100 if usage.total else None
    return {"path": str(target), "total": usage.total, "free": usage.free,
            "used": usage.used, "free_percent": free_percent}


def system_snapshot(storage_path: Path | str, cpu_interval: float | None = None) -> dict:
    """The machine's spare CPU, memory and disk right now.

    ``cpu_interval`` of None reads psutil's running average since the last
    call (the sampler keeps that warm); a number blocks that long for a
    one-shot reading, which the command line uses.
    """
    snap: dict = {
        "sampled_at": time.time(),
        "measured": psutil is not None,
        "cpu": {"used_percent": None, "free_percent": None, "count": None},
        "memory": {"total": None, "available": None, "used_percent": None,
                   "free_percent": None},
        "disk": disk_snapshot(storage_path),
    }
    if psutil is None:
        return snap
    try:
        used = psutil.cpu_percent(interval=cpu_interval)
        snap["cpu"] = {"used_percent": used, "free_percent": max(0.0, 100.0 - used),
                       "count": psutil.cpu_count() or None}
        mem = psutil.virtual_memory()
        snap["memory"] = {"total": mem.total, "available": mem.available,
                          "used_percent": mem.percent,
                          "free_percent": mem.available / mem.total * 100 if mem.total else None}
    except Exception as exc:                # pragma: no cover - platform oddities
        snap["measured"] = False
        snap["error"] = str(exc)
    return snap


def evaluate(snapshot: dict, thresholds: dict) -> list[dict]:
    """Which resources are below their threshold, as sentences."""
    if not thresholds.get("enabled", True):
        return []
    warnings: list[dict] = []
    checks = (
        ("cpu", snapshot.get("cpu", {}).get("free_percent"), thresholds.get("cpu_free_percent")),
        ("memory", snapshot.get("memory", {}).get("free_percent"), thresholds.get("memory_free_percent")),
        ("disk", snapshot.get("disk", {}).get("free_percent"), thresholds.get("disk_free_percent")),
    )
    for resource, free, limit in checks:
        if free is None or limit is None:
            continue                          # unmeasured: no warning
        if free < limit:
            detail = f"{free:.0f}% of {_LABELS[resource]} is free; the warning level is {limit:g}%."
            if resource == "disk":
                disk = snapshot.get("disk", {})
                detail = (f"{_fmt_bytes(disk.get('free'))} free on {disk.get('path')} "
                          f"({free:.0f}%); the warning level is {limit:g}%.")
            warnings.append({"resource": resource, "free_percent": free,
                             "threshold": limit, "message": detail})
    return warnings


def _fmt_bytes(value) -> str:
    if not value:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}" if index else f"{int(size)} {units[index]}"


class ProcessUsage:
    """CPU and memory of one worker and everything it started.

    A job is a worker plus a browser plus, at times, gallery-dl: its use is
    the sum over that tree. CPU percent is a rate, so the first reading of
    a process is zero and the truth arrives on the next sample; the sampler
    keeps process handles between samples for that reason.
    """

    def __init__(self) -> None:
        self._procs: dict[int, object] = {}
        self._lock = threading.Lock()

    def usage(self, pid: int | None) -> dict | None:
        if not pid or psutil is None:
            return None
        with self._lock:
            try:
                root = psutil.Process(pid)
                members = [root] + root.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(pid, None)
                return None
            cpu = 0.0
            rss = 0
            count = 0
            live: dict[int, object] = {}
            for proc in members:
                handle = self._procs.get(proc.pid)
                if handle is None or handle != proc:   # same pid, another process
                    handle = proc
                try:
                    cpu += handle.cpu_percent(interval=None)
                    rss += handle.memory_info().rss
                    count += 1
                    live[proc.pid] = handle
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            self._procs.update(live)
            for stale in [p for p, h in self._procs.items()
                          if p not in live and not h.is_running()]:
                self._procs.pop(stale, None)
            cores = psutil.cpu_count() or 1
            return {"cpu_percent": cpu, "cpu_percent_of_machine": cpu / cores,
                    "rss_bytes": rss, "processes": count}

    def forget(self, pid: int) -> None:
        with self._lock:
            self._procs.pop(pid, None)


class ResourceMonitor:
    """Keeps a warm reading of the machine, and of each running job.

    ``tick()`` takes one sample and runs ``on_tick`` (the server uses it to
    launch jobs that were told to wait); ``start()`` does that on a thread
    every ``interval`` seconds. Tests call ``tick()`` directly.
    """

    def __init__(self, storage_path, interval: float = 2.0, on_tick=None,
                 snapshot_fn=system_snapshot) -> None:
        self.storage_path = storage_path
        self.interval = interval
        self.on_tick = on_tick
        self._snapshot_fn = snapshot_fn
        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.processes = ProcessUsage()

    def snapshot(self, storage_path=None) -> dict:
        """The latest reading; a fresh one for a different disk."""
        if storage_path is not None and Path(storage_path) != Path(self.storage_path):
            snap = dict(self._latest or self._snapshot_fn(self.storage_path))
            snap["disk"] = disk_snapshot(storage_path)
            return snap
        with self._lock:
            if self._latest is None:
                self._latest = self._snapshot_fn(self.storage_path)
            return dict(self._latest)

    def tick(self) -> dict:
        snap = self._snapshot_fn(self.storage_path)
        with self._lock:
            self._latest = snap
        if self.on_tick is not None:
            try:
                self.on_tick(snap)
            except Exception:                 # pragma: no cover - logged by caller
                pass
        return snap

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="swm-resources",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:                 # pragma: no cover
                pass
            self._stop.wait(self.interval)
