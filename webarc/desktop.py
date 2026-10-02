"""Opening a capture's folder in the machine's own file manager.

The dashboard runs on the curator's machine, so the server can ask the
operating system to show a folder: Explorer on Windows, Finder on macOS, the
desktop's file manager (through xdg-open) on Linux. That is only meaningful
where the dashboard and the person are on the same machine and there is a
desktop to show it on; elsewhere the dashboard offers the path to copy.

Nothing here takes a path from a browser: the server resolves the folder of
a job or collection it already knows, and only that is opened.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


def _linux_desktop() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def availability(platform: Optional[str] = None,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 has_desktop: Callable[[], bool] = _linux_desktop) -> dict:
    """Whether this machine can show a folder in a file manager, and why not."""
    platform = platform or sys.platform
    if platform.startswith("win") or platform == "darwin":
        return {"available": True, "reason": None}
    if not which("xdg-open"):
        return {"available": False,
                "reason": "No file manager can be opened from here (xdg-open is not installed)."}
    if not has_desktop():
        return {"available": False,
                "reason": "This machine has no desktop session to open a folder in."}
    return {"available": True, "reason": None}


def command_for(path: Path, platform: Optional[str] = None) -> Optional[list[str]]:
    """The command that shows ``path`` in the file manager; None on Windows,
    where the shell's own call (os.startfile) is used instead."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return None
    if platform == "darwin":
        return ["open", str(path)]
    return ["xdg-open", str(path)]


def open_folder(path: Path, platform: Optional[str] = None) -> None:
    """Show the folder; returns once the file manager has been asked."""
    platform = platform or sys.platform
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not there (yet)")
    command = command_for(path, platform)
    if command is None:
        os.startfile(str(path))                         # type: ignore[attr-defined]  # Explorer
        return
    # detached: the file manager outlives the request, and its output is not ours
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
