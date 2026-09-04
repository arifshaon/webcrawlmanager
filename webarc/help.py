"""The help text behind each "?" on the dashboard.

The wording lives in help_text.yaml beside this module, keyed by field, so
it can be edited without touching the page. An installation can keep its
own copy next to the dashboard's database and change only the entries it
wants: those override the packaged ones, entry by entry, and both files
are read each time the dashboard asks, so a change shows on the next load.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PACKAGED = Path(__file__).resolve().parent / "help_text.yaml"
OVERRIDE_NAME = "help_text.yaml"


def _read(path: Path) -> dict[str, str | None]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Help text in %s could not be read: %s", path, exc)
        return {}
    if not isinstance(loaded, dict):
        log.warning("Help text in %s is not a mapping of field to text", path)
        return {}
    out: dict[str, str | None] = {}
    for key, value in loaded.items():
        # an entry left blank means "no help here": it hides that "?"
        out[str(key)] = None if value is None else " ".join(str(value).split())
    return out


def load_help(override_dir: Path | str | None = None) -> dict[str, str]:
    """Field key -> help text, packaged wording under any local override."""
    texts = _read(PACKAGED)
    if override_dir:
        override = Path(override_dir) / OVERRIDE_NAME
        if override.is_file():
            texts.update(_read(override))
    return {key: text for key, text in texts.items() if text}


def override_path(db_path: str | Path) -> Path:
    """Where an installation's own wording is looked for: beside the database."""
    return Path(db_path).resolve().parent / OVERRIDE_NAME
