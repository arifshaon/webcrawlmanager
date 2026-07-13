"""URL canonicalisation and per-seed scope decisions (Heritrix-style)."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .config import ScopeConfig

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "about:", "blob:")


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Resolve relative URLs, strip fragments, normalise host/port."""
    if not url:
        return None
    url = url.strip()
    if url.lower().startswith(_SKIP_SCHEMES):
        return None
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    port = parts.port
    netloc = host
    if port and not (
        (parts.scheme == "http" and port == 80)
        or (parts.scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def registrable_domain(host: str) -> str:
    """Crude eTLD+1 approximation (swap in tldextract for full PSL accuracy)."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # handle common two-part public suffixes, e.g. .com.qa, .co.uk, .org.qa
    if len(parts[-1]) == 2 and parts[-2] in ("com", "co", "org", "net", "gov", "edu", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class ScopeMatcher:
    def __init__(self, seed_url: str, cfg: ScopeConfig):
        self.cfg = cfg
        seed = canonicalize(seed_url)
        if not seed:
            raise ValueError(f"Invalid seed URL: {seed_url}")
        self.seed = seed
        p = urlsplit(seed)
        self.seed_host = p.hostname or ""
        self.seed_domain = registrable_domain(self.seed_host)
        # path-prefix scope: treat the seed as a directory unless its last
        # segment looks like a file (contains a dot), in which case use its parent
        if seed.endswith("/"):
            self.seed_prefix = seed
        else:
            last = seed.rsplit("/", 1)[-1]
            self.seed_prefix = (seed.rsplit("/", 1)[0] + "/") if "." in last else seed + "/"
        self._include = [re.compile(r) for r in cfg.include]
        self._exclude = [re.compile(r) for r in cfg.exclude]

    def in_scope(self, url: str) -> bool:
        p = urlsplit(url)
        host = p.hostname or ""

        strategy = self.cfg.strategy
        if strategy == "same-host":
            ok = host == self.seed_host
        elif strategy == "same-domain":
            ok = registrable_domain(host) == self.seed_domain
        elif strategy == "path-prefix":
            ok = url.startswith(self.seed_prefix) or url == self.seed
        elif strategy == "any":
            ok = True
        else:
            raise ValueError(f"Unknown scope strategy: {strategy}")
        if not ok:
            return False

        if self._include and not any(r.search(url) for r in self._include):
            return False
        if any(r.search(url) for r in self._exclude):
            return False
        return True
