"""Find a Chrome binary the browser tests can drive in this environment."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


def find_chrome() -> Optional[str]:
    """The first Chrome that exists: Playwright's, the system's, or a bundle."""
    candidates: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:                                    # pragma: no cover
        return None
    with sync_playwright() as pw:
        candidates.append(pw.chromium.executable_path)
    candidates += [shutil.which(n) or "" for n in
                   ("google-chrome", "google-chrome-stable", "chromium")]
    candidates += sorted(glob.glob(
        "/opt/pw-browsers/chromium-*/chrome-linux*/chrome"), reverse=True)
    return next((c for c in candidates if c and Path(c).exists()), None)


def ensure_display() -> Optional[Callable[[], None]]:
    """A display a visible browser can open on; None when none can be had.

    Returns a closer for a display started here, or a no-op closer when the
    environment already has one.
    """
    if os.environ.get("DISPLAY"):
        return lambda: None
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return None
    number = 90 + os.getpid() % 100
    process = subprocess.Popen(
        [xvfb, f":{number}", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline and not Path(f"/tmp/.X11-unix/X{number}").exists():
        if process.poll() is not None:
            return None
        time.sleep(0.05)
    os.environ["DISPLAY"] = f":{number}"

    def close() -> None:
        os.environ.pop("DISPLAY", None)
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
    return close
