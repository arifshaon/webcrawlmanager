"""Keeping the capturing session out of what is preserved.

Instagram's and Facebook's pages embed the browser session's own material in
otherwise useful bootstrap data: request-signing tokens (DTSG, LSD), nonces,
the per-navigation logging session, and the capturing account's ids. None of
it is part of the target; all of it identifies or authenticates the curator.
Header and form redaction handles the request side; this handles the bodies
that are written to the WARC and kept under raw/responses/.

Patterns keep the surrounding JSON and HTML valid, and the target's own
records -- posts, comments, profiles -- are left untouched. Only textual
bodies are considered; media passes through as it is.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED BY SWM]"

# (name, pattern): group 1 is kept, group 2 is the secret, group 3 the suffix
_BODY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("dtsg", re.compile(r'("DTSGInitialData".{0,500}?"token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("dtsg_init", re.compile(r'("DTSGInitData".{0,700}?"token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("dtsg_async", re.compile(r'("DTSGInitData".{0,900}?"async_get_token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("lsd", re.compile(r'(\["LSD"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("server_nonce", re.compile(r'("ServerNonce"\s*:\s*")([^"]+)(")', re.I)),
    ("navigation_session_id", re.compile(r'("sessionID"\s*:\s*")([^"]+)(")', re.I)),
    ("capturing_user_eimu", re.compile(r'("IG_USER_EIMU"\s*:\s*")([^"]+)(")', re.I)),
    ("capturing_user_id", re.compile(r'("USER_ID"\s*:\s*")([^"]+)(")', re.I)),
    ("capturing_account_id", re.compile(r'("ACCOUNT_ID"\s*:\s*")([^"]+)(")', re.I)),
    ("mrequest_dtsg", re.compile(r'("MRequestConfig".{0,1400}?"dtsg"\s*:\s*\{\s*"token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("mrequest_dtsg_ag", re.compile(r'("MRequestConfig".{0,1800}?"dtsg_ag"\s*:\s*\{\s*"token"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("eqmc_dtsg", re.compile(r'(<script[^>]+id="__eqmc"[^>]*>.*?"f"\s*:\s*")([^"]+)(")', re.I | re.S)),
    ("meta_auth_token", re.compile(r'()((?:NA|Ad)[A-Za-z0-9_-]{20,}:\d{10,}:\d{10,})(?=["&<\s])')),
)
# A secret in a query string or a form body: the key sits at the start of
# the body or right after ? or & (or its HTML form). Nothing else counts --
# minified JavaScript is full of names ending in these words followed by
# "=" (getSessionId=function..., fb_dtsg="..."), and rewriting code breaks
# the page's client at replay. Keys are lower case in the real URLs.
_QUERY_SECRET = re.compile(
    r"(?:^|(?<=[?&])|(?<=&amp;))"
    r"(?P<key>fb_dtsg(?:_ag)?|lsd|jazoest|__user|__s|__hsi|access_token"
    r"|auth_token|csrftoken|sessionid)=(?P<value>[^&\"'\\\s<>]+)")
_TEXTUAL = ("text/", "json", "javascript", "xml", "x-www-form-urlencoded")


def is_textual(content_type: str, body: bytes) -> bool:
    lowered = (content_type or "").lower()
    if any(marker in lowered for marker in _TEXTUAL):
        return True
    if lowered:
        return False
    return body[:1] in (b"<", b"{", b"[", b"f")     # unknown type: sniff


def redact_body(body: bytes, content_type: str = "") -> tuple[bytes, list[str]]:
    """The body with session material removed, and what was removed.

    A body that is not text, or that carries none of the known material,
    comes back unchanged with an empty list.
    """
    if not body or not is_textual(content_type, body):
        return body, []
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body, []
    changed: list[str] = []
    for name, pattern in _BODY_PATTERNS:
        def repl(match: re.Match) -> str:
            suffix = match.group(3) if match.lastindex and match.lastindex >= 3 else ""
            return match.group(1) + REDACTED + suffix
        text, count = pattern.subn(repl, text)
        if count:
            changed.append(name)

    def replace_query(match: re.Match) -> str:
        changed.append(match.group("key"))
        return match.group("key") + "=" + REDACTED
    text = _QUERY_SECRET.sub(replace_query, text)
    if not changed:
        return body, []
    return text.encode("utf-8"), sorted(set(changed))
