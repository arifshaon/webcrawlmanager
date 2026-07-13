"""Browser driver: launches or attaches to a browser per seed config and
provides human-like navigation plus network capture hooks.

Modes:
  headless — bundled Playwright Chromium, no window
  headed   — installed Google Chrome (channel='chrome'), visible window
  native   — spawn the system's default Chrome with --remote-debugging-port
             and attach over CDP (connect_over_cdp)
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import BehaviorConfig, BrowserConfig

log = logging.getLogger(__name__)

_CHROME_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium-browser", "chromium",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in _CHROME_CANDIDATES:
        path = shutil.which(cand) or (cand if shutil.os.path.exists(cand) else None)
        if path:
            return path
    raise RuntimeError("Could not locate a Chrome executable; set browser.chrome_path")


class BrowserDriver:
    def __init__(self, cfg: BrowserConfig, behavior: BehaviorConfig):
        self.cfg = cfg
        self.behavior = behavior
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._native_proc: subprocess.Popen | None = None
        self.delay_multiplier = 1.0

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "driver not started (use as context manager)"
        return self._context

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "BrowserDriver":
        self._pw = sync_playwright().start()
        try:
            self._start()
        except Exception:
            # a failed start would otherwise orphan the spawned Chrome and
            # the Playwright driver (the with-statement never enters, so
            # __exit__ would never run)
            self.__exit__()
            raise
        return self

    def _start(self) -> None:
        mode = self.cfg.mode

        if mode == "native":
            self._launch_native_chrome()
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.cfg.cdp_port}")
            self._context = (self._browser.contexts[0]
                             if self._browser.contexts
                             else self._browser.new_context())
        else:
            launch_kwargs: dict = {"headless": mode == "headless"}
            if mode == "headed":
                launch_kwargs["channel"] = "chrome"
            if self.cfg.proxy:
                launch_kwargs["proxy"] = {"server": self.cfg.proxy}
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            ctx_kwargs: dict = {
                "viewport": {"width": self.cfg.viewport[0],
                             "height": self.cfg.viewport[1]},
                "ignore_https_errors": bool(self.cfg.proxy),
            }
            if self.cfg.user_agent:
                ctx_kwargs["user_agent"] = self.cfg.user_agent
            self._context = self._browser.new_context(**ctx_kwargs)

    def _launch_native_chrome(self) -> None:
        chrome = _find_chrome(self.cfg.chrome_path)
        # refuse to attach to a browser we didn't launch: if the port already
        # answers, it belongs to an unrelated Chrome/DevTools session
        if self._cdp_answers(timeout=0.5):
            raise RuntimeError(
                f"Port {self.cfg.cdp_port} already serves a DevTools endpoint "
                "— another Chrome owns it. Close it or set a different "
                "browser.cdp_port so webarc does not attach to an unrelated "
                "browser session.")
        # Chrome 111+ silently ignores --remote-debugging-port on the default
        # profile: without a dedicated --user-data-dir the debug endpoint never
        # opens and the window just sits at about:blank. Always use one.
        user_data_dir = self.cfg.user_data_dir
        if not user_data_dir:
            user_data_dir = str(Path("./chrome-profile-webarc").resolve())
            log.info("native mode: no browser.user_data_dir configured; using "
                     "dedicated profile %s (Chrome requires a non-default "
                     "profile for CDP remote debugging)", user_data_dir)
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={self.cfg.cdp_port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run", "--no-default-browser-check",
        ]
        if self.cfg.proxy:
            args.append(f"--proxy-server={self.cfg.proxy}")
        args.append("about:blank")
        log.info("Launching native Chrome: %s (CDP port %d)", chrome, self.cfg.cdp_port)
        self._native_proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_cdp(timeout=20.0)

    def _cdp_answers(self, timeout: float = 1.0) -> bool:
        url = f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _wait_for_cdp(self, timeout: float) -> None:
        """Poll the DevTools endpoint until it answers (or fail with a clear error)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._native_proc and self._native_proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited immediately (code {self._native_proc.returncode}). "
                    "This usually means another Chrome instance is already running "
                    "on the same profile — close it, or set a different "
                    "browser.user_data_dir / cdp_port.")
            if self._cdp_answers(timeout=1.0):
                return
            time.sleep(0.25)
        raise RuntimeError(
            f"Chrome's CDP endpoint did not come up on port {self.cfg.cdp_port} "
            f"within {timeout:.0f}s. Check that nothing else uses the port and "
            "that browser.user_data_dir points to a profile not already in use.")

    def __exit__(self, *exc) -> None:
        try:
            if self._context and self.cfg.mode != "native":
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._native_proc:
                self._native_proc.terminate()
            if self._pw:
                self._pw.stop()

    # -- page work ---------------------------------------------------------
    def new_page(self, on_response: Callable) -> Page:
        page = None
        if self.cfg.mode == "native":
            # reuse the launch tab (about:blank) rather than opening a second
            # tab and leaving a blank one in the foreground
            for existing in self._context.pages:
                if existing.url in ("about:blank", ""):
                    page = existing
                    break
        if page is None:
            page = self._context.new_page()
        page.on("response", on_response)
        return page

    def visit(self, page: Page, url: str):
        """Navigate like a human. Returns the main-document Response, or None
        if navigation failed outright."""
        b = self.behavior
        try:
            resp = page.goto(url, wait_until=b.wait_until,
                             timeout=int(b.page_timeout * 1000))
        except Exception as exc:
            log.warning("Navigation failed for %s: %s", url, exc)
            return None

        if b.mouse_jitter:
            for _ in range(random.randint(1, 3)):
                page.mouse.move(random.randint(80, 1000),
                                random.randint(80, 700),
                                steps=random.randint(4, 12))

        if b.scroll:
            self._human_scroll(page)

        # allow lazy-loaded requests to finish
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return resp

    @staticmethod
    def page_signature(page: Page) -> tuple[str, str]:
        """Cheap title + small HTML slice for block detection."""
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            html = page.content()[:8000]
        except Exception:
            html = ""
        return title, html

    def _human_scroll(self, page: Page) -> None:
        b = self.behavior
        try:
            total = page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            viewport = page.evaluate("() => window.innerHeight") or 800
        except Exception:
            return
        pos = 0
        while pos < total:
            step = int(viewport * random.uniform(0.6, 0.95))
            pos += step
            try:
                page.evaluate(f"window.scrollTo(0, {pos})")
            except Exception:
                return
            time.sleep(random.uniform(*b.scroll_pause))
            try:  # pages can grow while scrolling (infinite scroll)
                total = min(page.evaluate("() => document.body.scrollHeight"),
                            total + viewport * 10)
            except Exception:
                break
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

    @staticmethod
    def extract_links(page: Page) -> list[str]:
        try:
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href)")
        except Exception:
            return []

    def inter_page_delay(self) -> None:
        lo, hi = self.behavior.delay_range
        mult = getattr(self, "delay_multiplier", 1.0)
        time.sleep(random.uniform(lo, hi) * mult)
