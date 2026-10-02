"""Whether a process is still there: one answer for the dashboard's workers
and the indexer's jar, so the two never disagree about the same pid."""

from __future__ import annotations

import os
from typing import Optional


def pid_alive(pid: Optional[int]) -> bool:
    """True while the process exists and has not exited; a zombie (exited,
    not yet reaped) counts as gone."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            return exit_code.value == 259                   # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                         # exists, not ours
    except OSError:
        return False
    try:
        import psutil
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:                                       # noqa: BLE001 - psutil is optional here
        return True
