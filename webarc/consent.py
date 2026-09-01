"""Dismissing the consent overlay a site puts between a crawler and its pages.

A cookie banner is not a page: it covers one. Scrolling and lazy-loading
behind a modal is wasted work, and the archive ends up describing the banner
rather than the page it hides.

Three things decide whether this works at all, and each is a way for it to do
nothing while appearing to succeed:

* Most consent platforms render inside an iframe or a shadow root, so a plain
  document query finds nothing at all. Every frame is searched, and every open
  shadow root within it.
* Wording is not English. A collection that spans Arabic, French and English
  sites needs the phrases for each; an English-only list quietly skips the
  rest.
* A button whose label says "Accept" is not necessarily a consent control. The
  candidate has to sit in something that behaves like an overlay, or a form
  deep in an ordinary page will eventually be clicked.

What is clicked is a curatorial act, not a technicality. Accepting everything
usually fires the advertising and analytics the banner was gating, and those
requests land in the WARC as part of the record; so where a banner offers a
way to decline, that is taken first, and every dismissal is recorded.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Consent platforms with stable controls. Matching these is far more reliable
# than reading labels, so they are tried first; the phrases below are the
# fallback for everything else.
KNOWN_DECLINE = [
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
    'button[mode="secondary"][aria-label*="Reject" i]',
    ".qc-cmp2-summary-buttons button[mode='secondary']",
    "#didomi-notice-disagree-button",
    'button[data-testid="uc-deny-all-button"]',
    "#cmpwelcomebtnno",
    ".fc-cta-do-not-consent",
]
KNOWN_ACCEPT = [
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#didomi-notice-agree-button",
    'button[data-testid="uc-accept-all-button"]',
    "#cmpwelcomebtnyes",
    ".fc-cta-consent",
    "#hs-eu-confirmation-button",
]

# Lower-cased substrings. Arabic and French sit alongside English because the
# collections do; a list that covers only English skips whole sites in
# silence.
DECLINE_PHRASES = [
    "reject all", "reject non-essential", "decline all", "decline optional",
    "only necessary", "necessary only", "essential only",
    "only essential cookies", "strictly necessary", "continue without",
    "refuse all", "deny all", "do not consent", "manage rejection",
    "tout refuser", "refuser tout", "continuer sans accepter",
    "alle ablehnen", "ablehnen", "rechazar todo", "rifiuta tutto",
    "رفض الكل",       # reject all
    "رفض",                                 # reject
    "الضرورية فقط",  # necessary only
]
ACCEPT_PHRASES = [
    "accept all", "accept cookies", "accept all cookies", "allow all",
    "i accept", "i agree", "agree and continue", "got it", "understood",
    "ok, got it", "that's ok", "yes, i agree", "allow cookies",
    "consent", "accept & close", "accept and close", "accept",
    "tout accepter", "accepter", "j'accepte", "alle akzeptieren",
    "akzeptieren", "aceptar todo", "aceptar", "accetta tutto",
    "موافق",                     # agree
    "قبول الكل",  # accept all
    "قبول",                           # accept
    "أوافق",                     # I agree
]

PREFER_DECLINE = "decline"
PREFER_ACCEPT = "accept"

# Bounds the shadow-root walk. A very large page is not worth an unbounded
# traversal on every navigation.
_ELEMENT_BUDGET = 6000

_SCRIPT = r"""
(options) => {
  const {knownDecline, knownAccept, declinePhrases, acceptPhrases,
         preference, budget, clickIt} = options;

  // Consent platforms render inside shadow roots as often as not, and a query
  // on the document alone never reaches them.
  const roots = [document];
  let examined = 0;
  const collect = (root) => {
    let nodes;
    try { nodes = root.querySelectorAll("*"); } catch (_) { return; }
    for (const el of nodes) {
      if (++examined > budget) return;
      if (el.shadowRoot) { roots.push(el.shadowRoot); collect(el.shadowRoot); }
    }
  };
  collect(document);

  const query = (selector) => {
    const found = [];
    for (const root of roots) {
      try { found.push(...root.querySelectorAll(selector)); } catch (_) {}
    }
    return found;
  };

  const visible = (el) => {
    try {
      const box = el.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) return false;
      const style = getComputedStyle(el);
      return style.visibility !== "hidden" && style.display !== "none"
        && Number(style.opacity) !== 0;
    } catch (_) { return false; }
  };

  // A label saying "Accept" is not enough: an ordinary page has such buttons.
  // The candidate has to sit in something that behaves like an overlay.
  const scrollLocked = (() => {
    try {
      const body = getComputedStyle(document.body).overflow;
      const html = getComputedStyle(document.documentElement).overflow;
      return body === "hidden" || html === "hidden";
    } catch (_) { return false; }
  })();
  const inOverlay = (el) => {
    if (scrollLocked) return true;
    let node = el;
    for (let depth = 0; node && depth < 12; depth++) {
      try {
        if (node.tagName === "DIALOG" && node.hasAttribute("open")) return true;
        const style = getComputedStyle(node);
        if (style.position === "fixed" || style.position === "sticky") return true;
        if (Number(style.zIndex) >= 100) return true;
      } catch (_) {}
      node = node.parentElement || (node.getRootNode() || {}).host || null;
    }
    return false;
  };

  const labelOf = (el) => {
    const text = ((el.innerText || el.textContent || "") + " " +
      (el.getAttribute("aria-label") || "") + " " +
      (el.value || "")).replace(/\s+/g, " ").trim();
    return text.slice(0, 80);
  };

  const seen = [];
  const controls = query('button, [role="button"], a[href], input[type="submit"], input[type="button"]');
  for (const el of controls) {
    if (!visible(el)) continue;
    const label = labelOf(el);
    if (label && seen.length < 40 && seen.indexOf(label) < 0) seen.push(label);
  }

  const byPhrase = (phrases) => {
    for (const el of controls) {
      if (!visible(el) || !inOverlay(el)) continue;
      const label = labelOf(el).toLowerCase();
      if (!label || label.length > 60) continue;
      if (phrases.some(phrase => label.includes(phrase))) return el;
    }
    return null;
  };
  const bySelector = (selectors) => {
    for (const selector of selectors) {
      for (const el of query(selector)) {
        if (visible(el)) return el;
      }
    }
    return null;
  };

  // Known platforms first: their controls are stable, labels are not.
  const order = preference === "accept"
    ? [["known_accept", () => bySelector(knownAccept)],
       ["accept", () => byPhrase(acceptPhrases)],
       ["known_decline", () => bySelector(knownDecline)],
       ["decline", () => byPhrase(declinePhrases)]]
    : [["known_decline", () => bySelector(knownDecline)],
       ["decline", () => byPhrase(declinePhrases)],
       ["known_accept", () => bySelector(knownAccept)],
       ["accept", () => byPhrase(acceptPhrases)]];

  for (const [kind, find] of order) {
    const el = find();
    if (!el) continue;
    const label = labelOf(el);
    if (clickIt) { try { el.click(); } catch (_) { return {kind, label, clicked: false, labels: seen}; } }
    return {kind, label, clicked: Boolean(clickIt), labels: seen,
            scroll_locked: scrollLocked};
  }
  return {kind: null, label: null, clicked: false, labels: seen,
          scroll_locked: scrollLocked};
}
"""


def _options(preference: str, click: bool) -> dict:
    return {
        "knownDecline": KNOWN_DECLINE,
        "knownAccept": KNOWN_ACCEPT,
        "declinePhrases": DECLINE_PHRASES,
        "acceptPhrases": ACCEPT_PHRASES,
        "preference": preference,
        "budget": _ELEMENT_BUDGET,
        "clickIt": click,
    }


def find_consent_control(frame, preference: str = PREFER_DECLINE) -> dict:
    """What this frame offers, without clicking anything."""
    try:
        return frame.evaluate(_SCRIPT, _options(preference, False)) or {}
    except Exception as exc:
        return {"error": str(exc)}


def dismiss_consent(page, preference: str = PREFER_DECLINE,
                    settle_ms: int = 600) -> dict:
    """Dismiss a consent overlay on this page, and report what happened.

    Returns a record rather than a bare boolean, and never raises: a page
    whose consent handling failed is still a page worth archiving. The record
    says which control was used, whether the overlay actually went away, and
    -- when nothing matched -- the labels that were on offer, so a crawl that
    dismissed nothing can say what it saw instead of leaving it to be guessed.
    """
    record: dict = {"clicked": False, "kind": None, "label": None,
                    "dismissed": False, "frames": 0, "labels": [],
                    "error": None}
    try:
        frames = list(page.frames)
    except Exception as exc:
        record["error"] = str(exc)
        return record
    record["frames"] = len(frames)

    target = None
    for frame in frames:
        try:
            result = frame.evaluate(_SCRIPT, _options(preference, True)) or {}
        except Exception as exc:
            # A frame that navigated or was detached mid-search is normal.
            log.debug("Consent search failed in a frame: %s", exc)
            continue
        if not record["labels"]:
            record["labels"] = list(result.get("labels") or [])[:40]
        if result.get("clicked"):
            record.update(clicked=True, kind=result.get("kind"),
                          label=result.get("label"))
            target = frame
            break

    if not record["clicked"]:
        return record

    # Clicking is not the same as dismissing. Confirm the overlay went, so a
    # banner that reappears or ignores the click is reported rather than
    # assumed away.
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass
    still_there = find_consent_control(target, preference)
    record["dismissed"] = not still_there.get("kind")
    return record
