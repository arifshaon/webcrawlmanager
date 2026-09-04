"""Configuration models: global defaults deep-merged with per-seed overrides."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class BrowserConfig:
    mode: str = "headless"              # headless | headed | native
    chrome_path: Optional[str] = None
    cdp_port: int = 9222
    user_data_dir: Optional[str] = None
    proxy: Optional[str] = None
    viewport: tuple[int, int] = (1366, 900)
    user_agent: Optional[str] = None


@dataclass
class ScopeConfig:
    strategy: str = "same-host"         # same-host | same-domain | path-prefix | any
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_depth: int = 2
    max_pages: int = 200


@dataclass
class BehaviorConfig:
    obey_robots: bool = True
    delay_range: tuple[float, float] = (1.5, 4.0)
    page_timeout: float = 45.0
    wait_until: str = "networkidle"
    scroll: bool = True
    scroll_pause: tuple[float, float] = (0.4, 1.2)
    # Infinite-scroll feeds grow while being scrolled; without a budget a page
    # with tens of thousands of records never finishes. 0 disables the cap.
    scroll_max_screens: int = 40
    # Seconds to let a WAF JS challenge interstitial (e.g. AWS WAF's HTTP 202
    # "challenge" action) solve itself and reload the real page before the
    # crawler proceeds. 0 disables the wait.
    challenge_grace: float = 20.0
    mouse_jitter: bool = True
    # Consent overlays. What is clicked is a curatorial act: accepting
    # everything fires the advertising and analytics the banner was gating,
    # and those requests are archived as part of the record. "decline" takes
    # the banner's own refusal where it offers one, so the page is reachable
    # without pulling third-party tracking into the WARC.
    dismiss_consent: bool = True
    consent_preference: str = "decline"     # "decline" or "accept"
    # WAF / bot-block handling
    detect_blocks: bool = True
    block_backoff_factor: float = 3.0   # multiply inter-page delay per block
    block_cooldown: float = 30.0        # base seconds to wait after a block
    block_max_consecutive: int = 3      # stop the seed after this many in a row


@dataclass
class WarcConfig:
    max_size_mb: int = 900
    dedup: bool = True


@dataclass
class SeedConfig:
    url: str
    browser: BrowserConfig
    scope: ScopeConfig
    behavior: BehaviorConfig
    warc: WarcConfig


@dataclass
class CrawlConfig:
    crawl_name: str
    output_dir: Path
    operator: str
    seeds: list[SeedConfig]
    # descriptive metadata: {"job": [fields], "seeds": {url: [fields]}}
    metadata: dict = field(default_factory=lambda: {"job": [], "seeds": {}})


def _build_section(cls, data: dict):
    """Instantiate a dataclass from a dict, tolerating unknown keys."""
    fields = {f for f in cls.__dataclass_fields__}
    kwargs: dict[str, Any] = {}
    for k, v in (data or {}).items():
        if k not in fields:
            continue
        if isinstance(v, list) and k in ("viewport", "delay_range", "scroll_pause"):
            v = tuple(v)
        kwargs[k] = v
    return cls(**kwargs)


def load_config(path: str | Path) -> CrawlConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})

    seeds: list[SeedConfig] = []
    for seed_raw in raw.get("seeds", []):
        merged = _deep_merge(defaults, {k: v for k, v in seed_raw.items() if k != "url"})
        seeds.append(
            SeedConfig(
                url=seed_raw["url"],
                browser=_build_section(BrowserConfig, merged.get("browser", {})),
                scope=_build_section(ScopeConfig, merged.get("scope", {})),
                behavior=_build_section(BehaviorConfig, merged.get("behavior", {})),
                warc=_build_section(WarcConfig, merged.get("warc", {})),
            )
        )

    if not seeds:
        raise ValueError("No seeds defined in configuration")

    from .metadata import from_config

    return CrawlConfig(
        crawl_name=raw.get("crawl_name", "webarc-crawl"),
        output_dir=Path(raw.get("output_dir", "./warcs")),
        operator=raw.get("operator", "webarc"),
        seeds=seeds,
        metadata=from_config(raw),
    )
