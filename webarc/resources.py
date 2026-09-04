"""What the machine has left, and what each running job is using.

A capture is a browser plus a worker process, and several at once can
leave a machine with no memory or disk for any of them. The dashboard and
the command line both show the system's spare CPU, memory and disk and the
share each running job takes, and warn before a new job starts when any of
the three is below a threshold the curator sets.

Measurement comes from psutil where it is installed. Without it, the
machine's CPU and memory are still read through the operating system
directly (Windows and Linux), disk space is always read, and only the
per-job figures are unavailable; nothing here ever stops a capture.
"""

from __future__ import annotations

import os
import shutil
import sys
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


# --- reading the machine without psutil ------------------------------------
#
# CPU use is the busy share of the time between two readings; the first
# reading alone says nothing, so it is kept here and the next one, two
# seconds later from the sampler, gives the figure. Windows and Linux are
# covered; elsewhere psutil is needed.

_cpu_times_lock = threading.Lock()
_last_cpu_times: tuple[float, float] | None = None       # (busy, total)


def _cpu_times() -> tuple[float, float] | None:
    """(busy, total) CPU time so far, in any consistent unit."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        total = as_int(kernel) + as_int(user)             # kernel includes idle
        return total - as_int(idle), total
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            first = fh.readline().split()
    except OSError:
        return None
    if not first or first[0] != "cpu":
        return None
    fields = [float(x) for x in first[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
    total = sum(fields)
    return total - idle, total


def _cpu_used_percent(interval: float | None) -> float | None:
    global _last_cpu_times
    now = _cpu_times()
    if now is None:
        return None
    if interval:
        time.sleep(interval)
        later = _cpu_times()
        if later is None:
            return None
        before, now = now, later
    else:
        with _cpu_times_lock:
            before, _last_cpu_times = _last_cpu_times, now
        if before is None:
            return None                     # nothing to compare with yet
    busy = now[0] - before[0]
    total = now[1] - before[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, busy / total * 100))


def _memory() -> tuple[int, int] | None:
    """(total, available) bytes."""
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            found = {}
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    found[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" in found and "MemAvailable" in found:
        return found["MemTotal"], found["MemAvailable"]
    return None


def measurement_note() -> str | None:
    """Why part of the reading is missing, when it is."""
    if psutil is not None:
        return None
    if sys.platform == "win32" or sys.platform.startswith("linux"):
        return ("psutil is not installed, so each job's own CPU and memory "
                "cannot be shown: run  pip install -r requirements.txt  "
                "and restart the server.")
    return ("psutil is not installed, so CPU and memory cannot be measured "
            "on this system: run  pip install -r requirements.txt  and "
            "restart the server.")


def system_snapshot(storage_path: Path | str, cpu_interval: float | None = None) -> dict:
    """The machine's spare CPU, memory and disk right now.

    ``cpu_interval`` of None reads the running average since the last
    call (the sampler keeps that warm); a number blocks that long for a
    one-shot reading, which the command line uses.
    """
    snap: dict = {
        "sampled_at": time.time(),
        "measured": False,
        "cpu": {"used_percent": None, "free_percent": None, "count": os.cpu_count()},
        "memory": {"total": None, "available": None, "used_percent": None,
                   "free_percent": None},
        "disk": disk_snapshot(storage_path),
    }
    note = measurement_note()
    if note:
        snap["note"] = note
    try:
        if psutil is not None:
            used = psutil.cpu_percent(interval=cpu_interval)
            count = psutil.cpu_count() or snap["cpu"]["count"]
            mem = psutil.virtual_memory()
            total, available = mem.total, mem.available
        else:
            used = _cpu_used_percent(cpu_interval)
            count = snap["cpu"]["count"]
            memory = _memory()
            total, available = memory if memory else (None, None)
        if used is not None:
            snap["cpu"] = {"used_percent": used, "free_percent": max(0.0, 100.0 - used),
                           "count": count}
        if total:
            snap["memory"] = {"total": total, "available": available,
                              "used_percent": (total - available) / total * 100,
                              "free_percent": available / total * 100}
        snap["measured"] = total is not None or used is not None
    except Exception as exc:                # pragma: no cover - platform oddities
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
