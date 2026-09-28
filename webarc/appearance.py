"""Dashboard themes: the colours and the job-type icons, as folders.

A theme is a folder holding a ``theme.json`` and an ``icons/`` folder of
SVG files, one per job type (crawl, recording, facebook, instagram, x,
youtube) and one for a collection. The themes SWM ships live in
``ui_themes/`` beside this module; a curator's own live in ``ui-themes/``
next to the dashboard's database, put there by the dashboard's installer
or dropped in by hand. Which theme a browser shows is that browser's
choice, kept with its other appearance settings.

``theme.json``::

    {"schema": "swm-ui-theme-v1", "name": "Harbour", "version": "1.0",
     "author": "...", "description": "...",
     "icon_style": "mono",            # or "color"
     "icon_size": "normal",           # or "large": a tile beside a job's name
     "colors": {"light": {"accent": "#0E7490", ...},
                "dark":  {"accent": "#67E8F9", ...}},
     "icons": {"instagram": "icons/camera.svg"}}   # optional

Everything is optional but the name. Colours override the dashboard's own,
token by token, for light and dark separately; the default theme's
``theme.json`` lists every token. A "mono" icon is drawn in the job type's
colour from the palette, so it follows light, dark and high contrast; a
"color" icon is shown as drawn. An icon a theme does not have comes from
the default theme. High contrast always uses the dashboard's own colours.

Nothing in a theme runs. Colours must be plain colour values, so a theme
cannot inject CSS; icons must be plain drawings -- no scripts, event
handlers, foreign content or references outside the file -- and are only
ever shown as images. A theme installed from a zip is refused whole if any
part fails those checks; one dropped in by hand keeps what passes and says
what was left out.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA = "swm-ui-theme-v1"
PACKAGED_DIR = Path(__file__).resolve().parent / "ui_themes"
INSTALL_DIR_NAME = "ui-themes"
DEFAULT_ID = "default"
MANIFEST = "theme.json"

ICON_ROLES = ("crawl", "recording", "facebook", "instagram", "x", "youtube",
              "collection")
MODES = ("light", "dark")
ICON_STYLES = ("mono", "color")
ICON_SIZES = ("normal", "large")

MAX_ZIP_BYTES = 2 * 1024 * 1024
MAX_UNPACKED_BYTES = 5 * 1024 * 1024
MAX_FILES = 100
MAX_ICON_BYTES = 100 * 1024
MAX_MANIFEST_BYTES = 64 * 1024

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_COLOUR = re.compile(
    r"^(?:#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|(?:rgb|rgba|hsl|hsla)\(\s*[0-9.%\s,/+-]{1,60}\))$")
_ICON_FILE = re.compile(r"^icons/[A-Za-z0-9_-]{1,60}\.svg$")
_EXTRA_FILES = {"README.md", "README.txt", "LICENSE", "LICENSE.md", "LICENSE.txt"}

# text colour on background, for the contrast warnings: the pairs the
# dashboard actually draws
_CONTRAST_PAIRS = (
    ("ink", "bg"), ("ink", "paper"), ("ink", "surface"), ("muted", "paper"),
    ("faint", "paper"), ("accent", "paper"), ("on-accent", "accent"),
    ("run", "run-bg"), ("pause", "pause-bg"), ("stop", "stop-bg"),
    ("block", "block-bg"), ("done", "done-bg"), ("fb", "fb-bg"), ("ig", "ig-bg"),
)
AA_TEXT = 4.5


class ThemeError(ValueError):
    """A theme that cannot be installed, and why."""


class ThemeExists(ThemeError):
    """A theme with that name is installed already."""


@dataclass
class Theme:
    id: str
    name: str
    path: Path
    builtin: bool
    version: str = ""
    author: str = ""
    description: str = ""
    icon_style: str = "mono"
    icon_size: str = "normal"
    colors: dict = field(default_factory=lambda: {m: {} for m in MODES})
    icons: dict = field(default_factory=dict)        # role -> Path
    problems: list = field(default_factory=list)     # what was left out
    warnings: list = field(default_factory=list)     # what may be hard to read

    def describe(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "author": self.author, "description": self.description,
            "builtin": self.builtin, "icon_style": self.icon_style,
            "icon_size": self.icon_size,
            "icons": sorted(self.icons), "modes": [m for m in MODES if self.colors.get(m)],
            "problems": list(self.problems), "warnings": list(self.warnings),
        }


# -- checks --------------------------------------------------------------------
def valid_colour(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 64 and bool(_COLOUR.match(value.strip()))


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


_FORBIDDEN_ELEMENTS = {"script", "foreignobject", "iframe", "object", "embed",
                       "audio", "video", "handler", "listener"}
_RASTER_DATA = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]*$")


def svg_problems(data: bytes) -> list[str]:
    """Why an icon may not be used; empty when it is a plain drawing."""
    if len(data) > MAX_ICON_BYTES:
        return [f"larger than {MAX_ICON_BYTES // 1024} KB"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["not UTF-8 text"]
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        return ["declares a DTD or entities"]
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [f"not well-formed XML ({exc})"]
    if _local(root.tag) != "svg":
        return ["its root element is not <svg>"]
    problems: list[str] = []
    for el in root.iter():
        tag = _local(el.tag) if isinstance(el.tag, str) else ""
        if tag in _FORBIDDEN_ELEMENTS:
            problems.append(f"contains <{tag}>")
        if tag == "style" and _unsafe_css(el.text or ""):
            problems.append("its <style> loads something from outside the file")
        for name, value in el.attrib.items():
            attr = _local(name)
            if attr.startswith("on"):
                problems.append(f"has an event handler ({attr})")
            elif attr == "href":
                if tag == "image" and _RASTER_DATA.match(value.strip()):
                    continue
                if not value.strip().startswith("#"):
                    problems.append("refers to something outside the file")
            elif attr == "style" and _unsafe_css(value):
                problems.append("a style loads something from outside the file")
            if "javascript:" in value.lower().replace(" ", ""):
                problems.append("contains a javascript: address")
    return sorted(set(problems))


def _unsafe_css(css: str) -> bool:
    lowered = css.lower()
    if "@import" in lowered or "expression(" in lowered:
        return True
    return any(not target.strip().strip("'\"").startswith("#")
               for target in re.findall(r"url\(([^)]*)\)", lowered))


def _rgb(value: str) -> Optional[tuple[float, float, float]]:
    value = value.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits[:3])
        return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    match = re.match(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)", value)
    if match:
        return tuple(min(255.0, float(g)) / 255 for g in match.groups())  # type: ignore[return-value]
    return None                     # hsl: not measured


def contrast(foreground: str, background: str) -> Optional[float]:
    """WCAG contrast ratio of two colours; None when one is not measurable."""
    a, b = _rgb(foreground), _rgb(background)
    if a is None or b is None:
        return None

    def luminance(rgb):
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# -- loading -------------------------------------------------------------------
def _text(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def _read_manifest(folder: Path) -> dict:
    path = folder / MANIFEST
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ThemeError(f"{MANIFEST} is larger than {MAX_MANIFEST_BYTES // 1024} KB")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ThemeError(f"{MANIFEST} is not valid JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise ThemeError(f"{MANIFEST} must be an object")
    if doc.get("schema", SCHEMA) != SCHEMA:
        raise ThemeError(f"{MANIFEST} is for another version of SWM ({doc.get('schema')})")
    if not _text(doc.get("name"), 60):
        raise ThemeError(f"{MANIFEST} must give the theme a name")
    return doc


def theme_id_for(doc: dict, fallback: str) -> str:
    wanted = doc.get("id") if isinstance(doc.get("id"), str) else (
        doc.get("name") if isinstance(doc.get("name"), str) else fallback)
    return re.sub(r"[^a-z0-9-]+", "-", wanted.lower()).strip("-")[:40]


def load_theme(folder: Path, builtin: bool = False,
               base: Optional["Theme"] = None) -> Theme:
    """Read one theme folder. What cannot be used is left out and named in
    ``problems``; ThemeError only when there is no usable theme at all."""
    folder = Path(folder)
    doc = _read_manifest(folder)
    theme = Theme(
        id=folder.name,
        name=_text(doc.get("name"), 60), path=folder, builtin=builtin,
        version=_text(doc.get("version"), 20), author=_text(doc.get("author"), 80),
        description=_text(doc.get("description"), 300))
    style = doc.get("icon_style", "mono")
    if style in ICON_STYLES:
        theme.icon_style = style
    else:
        theme.problems.append(f"icon_style {style!r} is not one of {', '.join(ICON_STYLES)}")
    size = doc.get("icon_size", "normal")
    if size in ICON_SIZES:
        theme.icon_size = size
    else:
        theme.problems.append(f"icon_size {size!r} is not one of {', '.join(ICON_SIZES)}")
    allowed = set((base.colors["light"] if base else {}) or {})
    colours = doc.get("colors") or {}
    if not isinstance(colours, dict):
        theme.problems.append("colors must be an object of light and dark palettes")
        colours = {}
    for mode in MODES:
        palette = colours.get(mode) or {}
        if not isinstance(palette, dict):
            theme.problems.append(f"colors.{mode} must be an object")
            continue
        for token, value in palette.items():
            if allowed and token not in allowed:
                theme.problems.append(f"colors.{mode}.{token} is not a colour the dashboard uses")
            elif not valid_colour(value):
                theme.problems.append(f"colors.{mode}.{token} is not a plain colour value")
            else:
                theme.colors[mode][token] = value.strip()
    declared = doc.get("icons") or {}
    if not isinstance(declared, dict):
        theme.problems.append("icons must be an object of job type to file")
        declared = {}
    for role in declared:
        if role not in ICON_ROLES:
            theme.problems.append(f"icons.{role} is not a job type ({', '.join(ICON_ROLES)})")
    for role in ICON_ROLES:
        relative = declared.get(role) or f"icons/{role}.svg"
        if not isinstance(relative, str) or not _ICON_FILE.match(relative):
            theme.problems.append(f"icons.{role} must name a file icons/NAME.svg")
            continue
        path = folder / relative
        if not path.is_file() or path.is_symlink():
            if role in declared:
                theme.problems.append(f"icons.{role}: {relative} is missing")
            continue
        issues = svg_problems(path.read_bytes())
        if issues:
            theme.problems.append(f"{relative} was left out: {'; '.join(issues)}")
            continue
        theme.icons[role] = path
    theme.warnings = contrast_warnings(theme, base)
    return theme


def palette(theme: Theme, base: Optional[Theme], mode: str) -> dict:
    merged = dict(base.colors.get(mode, {})) if base else {}
    merged.update(theme.colors.get(mode, {}))
    return merged


def contrast_warnings(theme: Theme, base: Optional[Theme]) -> list[str]:
    warnings = []
    for mode in MODES:
        if not theme.colors.get(mode):
            continue
        colours = palette(theme, base, mode)
        for fg, bg in _CONTRAST_PAIRS:
            if fg not in colours or bg not in colours:
                continue
            ratio = contrast(colours[fg], colours[bg])
            if ratio is not None and ratio < AA_TEXT:
                warnings.append(f"{mode}: {fg} on {bg} has contrast {ratio:.1f}:1, "
                                f"below the {AA_TEXT}:1 text needs")
    return warnings


def install_dir(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / INSTALL_DIR_NAME


def list_themes(installed: Optional[Path]) -> list[Theme]:
    """The packaged themes, the default first, then the curator's."""
    base = load_theme(PACKAGED_DIR / DEFAULT_ID, builtin=True)
    themes = [base]
    for folder in sorted(PACKAGED_DIR.iterdir()):
        if folder.name != DEFAULT_ID and (folder / MANIFEST).is_file():
            themes.append(load_theme(folder, builtin=True, base=base))
    taken = {t.id for t in themes}
    if installed and installed.is_dir():
        for folder in sorted(installed.iterdir()):
            if folder.name.startswith(".") or not (folder / MANIFEST).is_file():
                continue
            try:
                theme = load_theme(folder, base=base)
            except (ThemeError, OSError) as exc:
                log.warning("Theme in %s left out: %s", folder, exc)
                continue
            if not _ID.match(theme.id) or theme.id in taken:
                log.warning("Theme in %s left out: its name %r is taken or not usable",
                            folder, theme.id)
                continue
            taken.add(theme.id)
            themes.append(theme)
    return themes


def find_theme(theme_id: str, installed: Optional[Path]) -> Optional[Theme]:
    return next((t for t in list_themes(installed) if t.id == theme_id), None)


def icon_path(theme_id: str, role: str, installed: Optional[Path]) -> Optional[Path]:
    """The icon for a job type, from the theme or else the default."""
    if role not in ICON_ROLES:
        return None
    themes = list_themes(installed)
    chosen = next((t for t in themes if t.id == theme_id), themes[0])
    return chosen.icons.get(role) or themes[0].icons.get(role)


def icon_style(theme_id: str, installed: Optional[Path]) -> str:
    theme = find_theme(theme_id, installed)
    return theme.icon_style if theme else "mono"


def stylesheet(theme: Theme) -> str:
    """The theme's colours as CSS over the dashboard's own. High contrast
    keeps the dashboard's colours; values were checked to be plain colours."""
    def rules(selector: str, colours: dict) -> str:
        body = "".join(f"  --{token}: {value};\n" for token, value in sorted(colours.items()))
        return f"{selector} {{\n{body}}}\n"

    css = [f"/* {theme.name} {theme.version} */\n"]
    light, dark = theme.colors.get("light") or {}, theme.colors.get("dark") or {}
    if light:
        css.append(rules(':root:not([data-contrast="high"])', light))
    if dark:
        css.append("@media (prefers-color-scheme: dark) {\n"
                   + rules(':root:not([data-theme="light"]):not([data-contrast="high"])', dark)
                   + "}\n")
        css.append(rules(':root[data-theme="dark"]:not([data-contrast="high"])', dark))
    return "".join(css)


# -- installing ----------------------------------------------------------------
def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    parts = PurePosixPath(name).parts
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts:
        raise ThemeError(f"{info.filename!r} points outside the theme")
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise ThemeError(f"{info.filename!r} is a link")
    return "/".join(parts)


def install_zip(data: bytes, installed: Path, replace: bool = False) -> Theme:
    """Install a theme from a zip; refused whole if any part fails the
    checks. A zip of the folder itself (one top-level folder) is fine."""
    if len(data) > MAX_ZIP_BYTES:
        raise ThemeError(f"the zip is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ThemeError("not a zip file") from None
    files = [i for i in archive.infolist() if not i.is_dir()]
    if not files:
        raise ThemeError("the zip is empty")
    if len(files) > MAX_FILES:
        raise ThemeError(f"more than {MAX_FILES} files")
    if sum(i.file_size for i in files) > MAX_UNPACKED_BYTES:
        raise ThemeError(f"unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
    names = {_member_name(i): i for i in files}
    if MANIFEST not in names:
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and f"{next(iter(tops))}/{MANIFEST}" in names:
            prefix = next(iter(tops)) + "/"
            names = {n[len(prefix):]: i for n, i in names.items()}
        else:
            raise ThemeError(f"no {MANIFEST} at the top of the zip")
    unexpected = sorted(n for n in names if n != MANIFEST and n not in _EXTRA_FILES
                        and not _ICON_FILE.match(n))
    if unexpected:
        raise ThemeError("files a theme does not use: " + ", ".join(unexpected[:5]))

    installed.mkdir(parents=True, exist_ok=True)
    staging = installed / f".installing-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, info in names.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source:
                content = source.read(MAX_UNPACKED_BYTES + 1)
            if len(content) > max(MAX_ICON_BYTES, MAX_MANIFEST_BYTES) and name != MANIFEST \
                    and not name.startswith(("README", "LICENSE")):
                raise ThemeError(f"{name} is larger than {MAX_ICON_BYTES // 1024} KB")
            target.write_bytes(content)
        doc = _read_manifest(staging)
        theme_id = theme_id_for(doc, "")
        if not _ID.match(theme_id):
            raise ThemeError(f"{MANIFEST} must give an id, or a name SWM can make one "
                             "from: letters, digits and hyphens")
        if (PACKAGED_DIR / theme_id).exists():
            raise ThemeError(f"{theme_id!r} is the name of a theme SWM ships")
        base = load_theme(PACKAGED_DIR / DEFAULT_ID, builtin=True)
        theme = load_theme(staging, base=base)
        theme.id = theme_id
        if theme.problems:
            raise ThemeError("; ".join(theme.problems))
        final = installed / theme_id
        if final.exists():
            if not replace:
                raise ThemeExists(f"a theme named {theme_id!r} is installed already")
            retired = installed / f".removing-{uuid.uuid4().hex}"
            final.rename(retired)
            shutil.rmtree(retired, ignore_errors=True)
        staging.rename(final)
        staging = None
        return load_theme(final, base=base)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def remove_theme(theme_id: str, installed: Path) -> None:
    if not _ID.match(theme_id or ""):
        raise ThemeError("no such theme")
    if (PACKAGED_DIR / theme_id).exists():
        raise ThemeError("themes SWM ships cannot be removed")
    target = installed / theme_id
    if not target.is_dir():
        raise ThemeError("no such theme")
    retired = installed / f".removing-{uuid.uuid4().hex}"
    target.rename(retired)
    shutil.rmtree(retired, ignore_errors=True)


def zip_theme(theme: Theme) -> bytes:
    """The theme as a zip a curator can edit and install under a new name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(theme.path.rglob("*")):
            relative = path.relative_to(theme.path).as_posix()
            if path.is_file() and not path.is_symlink() and (
                    relative == MANIFEST or relative in _EXTRA_FILES
                    or _ICON_FILE.match(relative)):
                archive.write(path, f"{theme.id}/{relative}")
    return buffer.getvalue()
