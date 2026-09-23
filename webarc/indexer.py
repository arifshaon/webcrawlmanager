"""Index a social-media capture into warc-indexer's document schema.

A Facebook, Instagram, X or YouTube capture leaves normalised records
(``<platform>-posts.jsonl`` and friends) beside the WARC files it wrote. A
generic WARC indexer sees only the WARC: hundreds of scripts and images and
one page whose text is mostly navigation. The records are the content.

This module turns those records into one JSON Lines document per item --
post, comment, profile, video, channel -- using the field names of the
warc-indexer (webarchive-discovery) Solr schema, so the documents load into
the same index as warc-indexer's own output and appear in SolrWayback or
any other consumer of that schema. Each document points at the WARC record
of the page it came from (``source_file_path`` and ``source_file_offset``)
when that page is in a WARC beside the records, which is what turns a
search hit into an archive citation.

Only field names from the schema are emitted; ``SCHEMA_FIELDS`` is the
contract and ``validate_document`` enforces it. Platform-specific detail
that has no schema field (reaction counts, parent ids, ...) goes into the
multi-valued ``content_metadata_ss`` field as ``key=value`` strings.

Output: ``<capture_dir>/index/<platform>-index.jsonl`` and a summary in
``<capture_dir>/index/index-manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

log = logging.getLogger("webarc.indexer")

INDEX_DIR_NAME = "index"
INDEX_MANIFEST_NAME = "index-manifest.json"
INDEX_SCHEMA = "swm-social-index/1"
# The schema whose field names the documents use.
TARGET_SCHEMA = "webarchive-discovery warc-indexer 3.x (SolrWayback bundle schema)"

PLATFORMS = ("facebook", "instagram", "x", "youtube")

# Every field name defined by warc-indexer's SolrFields plus the bundled
# schema.xml. A document may use any subset; nothing else.
SCHEMA_FIELDS = frozenset({
    "id", "id_long", "url", "source_file", "source_file_offset", "source_file_path",
    "redirect_to_norm", "url_norm", "url_path", "status_code", "resourcename",
    "url_type", "host", "domain", "host_surt", "public_suffix", "hash", "title",
    "wct_subjects", "description", "comments", "author", "keywords", "category",
    "collection", "collections", "links_images", "links", "links_hosts",
    "links_hosts_surts", "links_domains", "links_public_suffixes",
    "content_language", "type", "content_type", "content_encoding",
    "content_type_version", "content_type_full", "content_type_tika",
    "content_type_droid", "content_type_served", "content_type_ext",
    "content_type_norm", "content_ffb", "content_first_bytes", "generator",
    "server", "parse_error", "content_warning", "xml_root_ns", "pdf_pdfa_is_valid",
    "pdf_pdfa_errors", "record_type", "warc_key_id", "warc_ip", "content_length",
    "timestamp", "referrer_url", "content", "text", "content_text_length",
    "content_metadata", "content_metadata_ss", "wayback_date", "crawl_date",
    "crawl_dates", "crawl_year", "crawl_years", "publication_date",
    "publication_year", "last_modified", "last_modified_year", "exif_version",
    "exif_location", "image_pdq_hash", "image_p_hash", "postcode",
    "postcode_district", "locations", "sentiment", "sentiment_score",
    "license_url", "access_terms", "elements_used", "image_width", "image_height",
    "image_size", "image_faces", "image_faces_count", "image_colours",
    "image_dominant_colour", "institution", "collection_id",
})

# Fields the schema declares multi-valued: always emitted as lists.
MULTI_VALUED = frozenset({
    "author", "keywords", "collection", "collections", "links", "links_images",
    "links_hosts", "links_domains", "links_public_suffixes", "links_hosts_surts",
    "access_terms", "license_url", "content_metadata_ss", "wct_subjects",
    "crawl_dates", "crawl_years", "elements_used",
})

# Values of the ``type`` field, in the style of warc-indexer's own
# "Web Page", "Image", "Twitter Tweet".
TYPE_POST = {"facebook": "Facebook Post", "instagram": "Instagram Post",
             "x": "X Post", "youtube": "YouTube Post"}
TYPE_COMMENT = {"facebook": "Facebook Comment", "instagram": "Instagram Comment",
                "youtube": "YouTube Comment"}
TYPE_PROFILE = {"instagram": "Instagram Profile", "x": "X User",
                "youtube": "YouTube Channel"}
TYPE_VIDEO = "YouTube Video"
TYPE_PLAYLIST = "YouTube Playlist"
TYPE_PHOTO = "Facebook Photo"

MANIFESTS = {p: f"{p}-manifest.json" for p in PLATFORMS}


class IndexingError(Exception):
    """A capture directory that cannot be indexed."""


# --- small helpers ---------------------------------------------------------

def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping unreadable line in %s", path.name)
                continue
            if isinstance(row, dict):
                yield row


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _text(value: object) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def _first_line(text: str, limit: int = 120) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def _int(value: object) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_WAYBACK = "%Y%m%d%H%M%S"


def parse_time(value: object) -> Optional[datetime]:
    """An aware UTC datetime from the shapes captures write: ISO 8601 with
    or without a zone, epoch seconds, or a 14-digit Wayback timestamp."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if re.fullmatch(r"\d{14}", text):
        return datetime.strptime(text, _WAYBACK).replace(tzinfo=timezone.utc)
    if re.fullmatch(r"\d{9,11}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    if re.fullmatch(r"\d{8}", text):                     # yt-dlp upload_date
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wayback_date(when: datetime) -> int:
    return int(when.astimezone(timezone.utc).strftime(_WAYBACK))


_WWW = re.compile(r"^(www\d*|ww\d?|ww\.)\.", re.IGNORECASE)


def normalise_url(url: str) -> str:
    """The shape warc-indexer's standard normaliser gives ``url_norm``:
    http, no www, lower-case host, no default port, sorted query, no
    fragment, no trailing slash on a path. Used for the documents' own
    ``url_norm`` and for matching records in the WARC, so both sides agree
    even where this differs from the Java code in a corner."""
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    host = _WWW.sub("", host)
    port = parts.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = ""
    if parts.query:
        pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
        query = "?" + urlencode(pairs) if pairs else ""
    return f"http://{host}{path}{query}"


def _url_parts(url: str) -> dict:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    domain = ".".join(labels[-2:]) if len(labels) >= 2 else host
    return {"host": host, "domain": domain,
            "url_path": parts.path or "/"}


def _sha1_text(text: str) -> str:
    return "sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def _kv(name: str, value: object) -> Optional[str]:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    return f"{name}={value}"


def _strip_none(doc: dict) -> dict:
    out = {}
    for key, value in doc.items():
        if value is None or value == "" or value == []:
            continue
        if key in MULTI_VALUED and not isinstance(value, list):
            value = [value]
        out[key] = value
    return out


# --- WARC lookup -----------------------------------------------------------

@dataclass
class WarcHit:
    path: Path
    offset: int
    url: str
    date: Optional[datetime]
    status: Optional[int]
    content_type: Optional[str]
    digest: Optional[str]


class WarcLocator:
    """Where each captured URL's response record lives, across the WARC
    files of one capture directory. Built once by scanning the files; the
    scan reads only record headers, so it is cheap next to the capture."""

    def __init__(self, warc_paths: Iterable[Path]):
        self.paths = sorted(Path(p) for p in warc_paths)
        self._hits: dict[str, list[WarcHit]] = {}
        self.records_scanned = 0
        self.errors: list[str] = []
        for path in self.paths:
            self._scan(path)

    def _scan(self, path: Path) -> None:
        try:
            from warcio.archiveiterator import ArchiveIterator
        except ImportError as exc:                       # pragma: no cover
            raise IndexingError("warcio is required to locate WARC records") from exc
        try:
            with path.open("rb") as stream:
                iterator = ArchiveIterator(stream)
                for record in iterator:
                    self.records_scanned += 1
                    if record.rec_type != "response":
                        continue
                    uri = record.rec_headers.get_header("WARC-Target-URI")
                    if not uri:
                        continue
                    status = None
                    ctype = None
                    if record.http_headers is not None:
                        status = _int(record.http_headers.get_statuscode())
                        ctype = record.http_headers.get_header("Content-Type")
                    hit = WarcHit(
                        path=path, offset=iterator.get_record_offset(), url=uri,
                        date=parse_time(record.rec_headers.get_header("WARC-Date")),
                        status=status, content_type=ctype,
                        digest=record.rec_headers.get_header("WARC-Payload-Digest"),
                    )
                    self._hits.setdefault(normalise_url(uri), []).append(hit)
        except Exception as exc:                          # noqa: BLE001 - keep indexing
            self.errors.append(f"{path.name}: {exc}")
            log.warning("Could not scan %s: %s", path, exc)

    def find(self, *candidates: Optional[str], near: Optional[datetime] = None) -> Optional[WarcHit]:
        """The best response record for the first candidate URL that has
        one: an HTML 200 before anything else, then the capture nearest in
        time to ``near``."""
        for url in candidates:
            if not url:
                continue
            hits = self._hits.get(normalise_url(url))
            if not hits:
                continue

            def rank(hit: WarcHit) -> tuple:
                html = "html" in (hit.content_type or "").lower()
                ok = hit.status == 200
                distance = abs((hit.date - near).total_seconds()) if (near and hit.date) else 0
                return (not (html and ok), not ok, distance)

            return min(hits, key=rank)
        return None

    def __len__(self) -> int:
        return sum(len(v) for v in self._hits.values())


# --- the capture being indexed ------------------------------------------

@dataclass
class Capture:
    directory: Path
    platform: str
    manifest: dict
    metadata: dict | None
    name: str
    operator: str
    captured_at: datetime
    warc_paths: list[Path]

    @property
    def targets(self) -> list[str]:
        cap = self.manifest.get("capture") or {}
        if self.platform == "facebook":
            return [u for u in [cap.get("page_url")] if u]
        return [t.get("url") for t in cap.get("targets") or [] if t.get("url")]


def detect_platform(directory: Path) -> Optional[str]:
    directory = Path(directory)
    for platform in PLATFORMS:
        if (directory / MANIFESTS[platform]).is_file():
            return platform
    for platform in PLATFORMS:
        if (directory / f"{platform}-posts.jsonl").is_file() or \
                (directory / f"{platform}-videos.jsonl").is_file():
            return platform
    return None


def _capture_time(directory: Path, platform: str, manifest: dict,
                  metadata: dict | None) -> datetime:
    for row in _read_jsonl(directory / f"{platform}-events.jsonl"):
        when = parse_time(row.get("time"))
        if when:
            return when
    for candidate in ((metadata or {}).get("written_at"), manifest.get("updated_at")):
        when = parse_time(candidate)
        if when:
            return when
    return datetime.now(timezone.utc)


def load_capture(directory: Path, platform: Optional[str] = None) -> Capture:
    directory = Path(directory)
    if not directory.is_dir():
        raise IndexingError(f"{directory} is not a directory")
    platform = platform or detect_platform(directory)
    if platform not in PLATFORMS:
        raise IndexingError(
            f"{directory} holds no Facebook, Instagram, X or YouTube capture "
            "(no <platform>-manifest.json or <platform>-posts.jsonl)")
    manifest = _read_json(directory / MANIFESTS[platform]) or {}
    from .metadata import read_document
    metadata = read_document(directory)
    cap = manifest.get("capture") or {}
    name = str(cap.get("name") or (metadata or {}).get("name") or directory.name)
    operator = str(cap.get("operator") or "")
    return Capture(
        directory=directory, platform=platform, manifest=manifest,
        metadata=metadata, name=name, operator=operator,
        captured_at=_capture_time(directory, platform, manifest, metadata),
        warc_paths=sorted(directory.glob("*.warc.gz")) + sorted(directory.glob("*.warc")),
    )


# --- documents -------------------------------------------------------------

class DocumentBuilder:
    """Shared shape of every document; the platform mappers fill it."""

    def __init__(self, capture: Capture, locator: WarcLocator,
                 collection: Optional[str] = None):
        self.capture = capture
        self.locator = locator
        self.collection = collection or capture.name
        self.located = 0
        self.unlocated = 0
        self._describe = self._description_fields()

    def _description_fields(self) -> dict:
        """What metadata.json says about the whole job: rights, subjects."""
        doc = self.capture.metadata or {}
        fields = list(doc.get("job") or [])
        for seed in doc.get("seeds") or []:
            fields.extend(seed.get("effective") or [])
        out: dict[str, list[str]] = {}
        for entry in fields:
            name, value = entry.get("name"), _text(entry.get("value"))
            if name and value and value not in out.setdefault(name, []):
                out[name].append(value)
        return out

    def document(self, *, kind: str, native_id: str, type_name: str,
                 url: Optional[str], evidence_urls: Iterable[Optional[str]] = (),
                 text: str = "", title: Optional[str] = None,
                 authors: Iterable[Optional[str]] = (), published: object = None,
                 description: Optional[str] = None, keywords: Iterable[str] = (),
                 language: Optional[str] = None, links: Iterable[str] = (),
                 images: Iterable[str] = (), extra: dict | None = None,
                 category: Optional[str] = None) -> dict:
        cap = self.capture
        platform = cap.platform
        evidence = list(evidence_urls) or [url]
        hit = self.locator.find(*evidence, near=cap.captured_at)
        if hit:
            self.located += 1
        else:
            self.unlocated += 1
        crawl_when = (hit.date if hit and hit.date else cap.captured_at)
        published_when = parse_time(published)
        raw_text = str(text or "")
        text = _text(raw_text)
        links = [l for l in dict.fromkeys(links) if l]
        images = [i for i in dict.fromkeys(images) if i]
        link_hosts = sorted({_url_parts(l)["host"] for l in links + images if _url_parts(l)["host"]})
        link_domains = sorted({_url_parts(l)["domain"] for l in links + images if _url_parts(l)["domain"]})
        meta = [_kv("platform", platform), _kv("swm_kind", kind), _kv("swm_capture_id",
                (cap.manifest.get("capture") or {}).get("crawl_id"))]
        for key, value in (extra or {}).items():
            meta.append(_kv(key, value))
        doc = {
            "id": f"{platform}:{kind}:{native_id}",
            "type": type_name,
            "category": category,
            "url": url,
            "content": text or None,
            "content_text_length": len(text) if text else None,
            "title": title or (_first_line(raw_text) if text else None),
            "author": [a for a in dict.fromkeys(_text(a) for a in authors) if a],
            "description": _text(description) or None,
            "keywords": [k for k in dict.fromkeys(_text(k) for k in keywords) if k],
            "content_language": language or None,
            "crawl_date": iso_utc(crawl_when),
            "wayback_date": wayback_date(crawl_when),
            "crawl_year": crawl_when.year,
            "publication_date": iso_utc(published_when) if published_when else None,
            "publication_year": str(published_when.year) if published_when else None,
            "content_type": "text/html",
            "content_type_norm": "html",
            "links": links,
            "links_images": images,
            "links_hosts": link_hosts,
            "links_domains": link_domains,
            "collection": [self.collection],
            "collections": [self.collection],
            "institution": cap.operator or None,
            "access_terms": self._describe.get("Rights", []),
            "wct_subjects": self._describe.get("Subject", []),
            "content_metadata_ss": [m for m in meta if m],
            "hash": _sha1_text(text) if text else None,
        }
        if url:
            doc.update({"url_norm": normalise_url(url), **_url_parts(url),
                        "resourcename": native_id})
        if hit:
            doc.update({
                "record_type": "response",
                "status_code": hit.status,
                "source_file": hit.path.name,
                "source_file_path": str(hit.path.resolve()),
                "source_file_offset": hit.offset,
            })
            if hit.digest:
                doc["hash"] = hit.digest
        return _strip_none(doc)


# --- platform mappers ------------------------------------------------------

def _facebook(cap: Capture, build: DocumentBuilder) -> Iterator[dict]:
    d = cap.directory
    posts = {row.get("post_id"): row for row in _read_jsonl(d / "facebook-posts.jsonl")}
    page_url = (cap.targets or [None])[0]
    capture = cap.manifest.get("capture") or {}
    target_post = str(capture.get("target_post_id") or "")

    def evidence_for(post: dict) -> list:
        """Where the post's page may be in the WARC. Facebook hands out a
        permalink in its pfbid form while the page the browser loaded, and
        the WARC recorded, is the slug or numeric form; when the capture's
        own target names this post, that URL is tried too."""
        pid = str(post.get("post_id") or "")
        urls = [post.get("permalink_url"), *(post.get("aliases") or [])]
        if page_url and pid and (pid == target_post or pid in page_url):
            urls.append(page_url)
        return urls

    for post in posts.values():
        permalink = post.get("permalink_url")
        yield build.document(
            kind="post", native_id=str(post.get("post_id")), type_name=TYPE_POST["facebook"],
            url=permalink or page_url,
            evidence_urls=evidence_for(post),
            text=post.get("text") or "", authors=[post.get("author_name")],
            published=post.get("created_time"), images=post.get("media_urls") or [],
            category="facebook/post",
            extra={"author_id": post.get("author_id"), "reactions_count": post.get("reactions_count"),
                   "comments_count": post.get("comments_count"), "shares_count": post.get("shares_count"),
                   "is_pinned": post.get("is_pinned"), "source": post.get("source"),
                   "page_url": page_url})
    for comment in _read_jsonl(d / "facebook-comments.jsonl"):
        parent = posts.get(comment.get("parent_post_id")) or {}
        permalink = parent.get("permalink_url")
        cid = str(comment.get("comment_id"))
        url = f"{permalink}?comment_id={cid}" if permalink else None
        yield build.document(
            kind="comment", native_id=cid, type_name=TYPE_COMMENT["facebook"],
            url=url, evidence_urls=evidence_for(parent) if parent else [],
            text=comment.get("text") or "", authors=[comment.get("author_name")],
            published=comment.get("created_time"), images=comment.get("media_urls") or [],
            category="facebook/comment",
            extra={"parent_post_id": comment.get("parent_post_id"),
                   "parent_comment_id": comment.get("parent_comment_id"),
                   "author_id": comment.get("author_id"), "depth": comment.get("depth")})
    for photo in _read_jsonl(d / "facebook-album-context.jsonl"):
        pid = str(photo.get("photo_id"))
        yield build.document(
            kind="photo", native_id=pid, type_name=TYPE_PHOTO,
            url=photo.get("photo_url"), evidence_urls=[photo.get("photo_url"), photo.get("post_url")],
            text=photo.get("caption") or photo.get("post_text") or "",
            authors=[photo.get("owner_name")], published=photo.get("created_time"),
            images=[photo.get("image_url")] if photo.get("image_url") else [],
            category="facebook/photo",
            extra={"post_id": photo.get("post_id"), "album_id": photo.get("album_id"),
                   "width": photo.get("width"), "height": photo.get("height"),
                   "belongs_to_captured_post": photo.get("belongs_to_captured_post")})


def _instagram(cap: Capture, build: DocumentBuilder) -> Iterator[dict]:
    d = cap.directory
    posts = {row.get("shortcode"): row for row in _read_jsonl(d / "instagram-posts.jsonl")}
    for post in posts.values():
        code = post.get("shortcode")
        permalink = post.get("permalink_url") or (f"https://www.instagram.com/p/{code}/" if code else None)
        prov = post.get("provenance") or {}
        media_urls = post.get("media_urls") or [m.get("url") for m in post.get("media") or [] if m.get("url")]
        yield build.document(
            kind="post", native_id=str(post.get("media_id") or code), type_name=TYPE_POST["instagram"],
            url=permalink, evidence_urls=[permalink, prov.get("url")],
            text=post.get("caption") or "", authors=[post.get("owner_username")],
            published=post.get("created_time"), images=media_urls,
            category=f"instagram/{post.get('kind') or 'post'}",
            extra={"shortcode": code, "owner_id": post.get("owner_id"), "kind": post.get("kind"),
                   "likes_count": post.get("likes_count"), "comments_count": post.get("comments_count"),
                   "video_view_count": post.get("video_view_count"), "is_pinned": post.get("is_pinned"),
                   "surface": post.get("surface"), "source": post.get("source")})
    for comment in _read_jsonl(d / "instagram-comments.jsonl"):
        parent = posts.get(comment.get("post_shortcode")) or {}
        code = comment.get("post_shortcode")
        permalink = parent.get("permalink_url") or (f"https://www.instagram.com/p/{code}/" if code else None)
        cid = str(comment.get("comment_id"))
        yield build.document(
            kind="comment", native_id=cid, type_name=TYPE_COMMENT["instagram"],
            url=f"{permalink}c/{cid}/" if permalink else None,
            evidence_urls=[permalink, (comment.get("provenance") or {}).get("url")],
            text=comment.get("text") or "", authors=[comment.get("author_username")],
            published=comment.get("created_time"), category="instagram/comment",
            extra={"post_shortcode": code, "parent_comment_id": comment.get("parent_comment_id"),
                   "author_id": comment.get("author_id"), "likes_count": comment.get("likes_count"),
                   "depth": comment.get("depth")})
    profiles = _read_json(d / "instagram-profiles.json") or {}
    for username, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        handle = profile.get("username") or username
        url = f"https://www.instagram.com/{handle}/"
        yield build.document(
            kind="profile", native_id=str(profile.get("user_id") or handle),
            type_name=TYPE_PROFILE["instagram"], url=url,
            evidence_urls=[url, (profile.get("provenance") or {}).get("url")],
            text=profile.get("biography") or "", title=profile.get("full_name") or handle,
            authors=[handle], description=profile.get("biography"),
            links=[profile.get("external_url")] if profile.get("external_url") else [],
            images=[profile.get("profile_pic_url")] if profile.get("profile_pic_url") else [],
            category="instagram/profile",
            extra={"username": handle, "followers_count": profile.get("followers_count"),
                   "following_count": profile.get("following_count"), "posts_count": profile.get("posts_count"),
                   "is_private": profile.get("is_private"), "is_verified": profile.get("is_verified")})


def _x(cap: Capture, build: DocumentBuilder) -> Iterator[dict]:
    d = cap.directory
    for post in _read_jsonl(d / "x-posts.jsonl"):
        pid = str(post.get("post_id"))
        permalink = post.get("permalink_url")
        prov = post.get("provenance") or {}
        media_urls = post.get("media_urls") or [m.get("url") for m in post.get("media") or [] if m.get("url")]
        links = [u.get("expanded_url") or u.get("url") for u in post.get("urls") or []
                 if isinstance(u, dict)]
        yield build.document(
            kind="post", native_id=pid, type_name=TYPE_POST["x"],
            url=permalink or f"https://x.com/i/status/{pid}",
            evidence_urls=[permalink, f"https://x.com/i/status/{pid}",
                           f"https://twitter.com/i/status/{pid}", prov.get("url")],
            text=post.get("text") or "",
            authors=[post.get("author_handle") and f"@{post['author_handle']}", post.get("author_name")],
            published=post.get("created_time"), keywords=post.get("hashtags") or [],
            language=post.get("lang"), links=links, images=media_urls,
            category=f"x/{post.get('capture_role') or 'post'}",
            extra={"author_id": post.get("author_id"), "relationship": post.get("relationship"),
                   "capture_role": post.get("capture_role"), "conversation_id": post.get("conversation_id"),
                   "in_reply_to_post_id": post.get("in_reply_to_post_id"),
                   "in_reply_to_handle": post.get("in_reply_to_handle"),
                   "original_post_id": post.get("original_post_id"), "quoted_post_id": post.get("quoted_post_id"),
                   "reply_count": post.get("reply_count"), "repost_count": post.get("repost_count"),
                   "like_count": post.get("like_count"), "quote_count": post.get("quote_count"),
                   "view_count": post.get("view_count"), "is_pinned": post.get("is_pinned"),
                   "mentions": ",".join(post.get("mentions") or []) or None})
    users = _read_json(d / "x-users.json") or {}
    for user_id, user in users.items():
        if not isinstance(user, dict):
            continue
        handle = user.get("handle") or ""
        url = f"https://x.com/{handle}" if handle else None
        yield build.document(
            kind="user", native_id=str(user.get("user_id") or user_id), type_name=TYPE_PROFILE["x"],
            url=url, evidence_urls=[url, f"https://twitter.com/{handle}" if handle else None,
                                    (user.get("provenance") or {}).get("url")],
            text=user.get("description") or "", title=user.get("name") or handle,
            authors=[f"@{handle}" if handle else None, user.get("name")],
            description=user.get("description"), published=user.get("created_time"),
            links=[user.get("url")] if user.get("url") else [],
            images=[user.get("profile_image_url")] if user.get("profile_image_url") else [],
            category="x/user",
            extra={"handle": handle, "location": user.get("location"),
                   "followers_count": user.get("followers_count"), "following_count": user.get("following_count"),
                   "posts_count": user.get("posts_count"), "is_protected": user.get("is_protected"),
                   "is_verified": user.get("is_verified")})


def _youtube(cap: Capture, build: DocumentBuilder) -> Iterator[dict]:
    d = cap.directory
    videos: dict[str, dict] = {}
    for video in _read_jsonl(d / "youtube-videos.jsonl"):
        vid = str(video.get("video_id"))
        videos[vid] = video
        prov = video.get("provenance") or {}
        url = video.get("url") or f"https://www.youtube.com/watch?v={vid}"
        text = " ".join(t for t in (video.get("title"), video.get("description")) if t)
        yield build.document(
            kind="video", native_id=vid, type_name=TYPE_VIDEO, url=url,
            evidence_urls=[prov.get("watch_page_in_warc"), url, f"https://www.youtube.com/watch?v={vid}"],
            text=text, title=video.get("title"), description=video.get("description"),
            authors=[video.get("channel_name"), video.get("channel_handle")],
            published=video.get("published_time"),
            keywords=list(video.get("tags") or []) + list(video.get("categories") or []),
            images=[video.get("thumbnail_url")] if video.get("thumbnail_url") else [],
            category=f"youtube/{video.get('kind') or 'video'}",
            extra={"channel_id": video.get("channel_id"), "duration_seconds": video.get("duration_seconds"),
                   "live_status": video.get("live_status"), "availability": video.get("availability"),
                   "view_count": video.get("view_count"), "like_count": video.get("like_count"),
                   "comment_count": video.get("comment_count"), "media_file": video.get("media_file"),
                   "complete": video.get("complete"), "source": video.get("source")})
    posts: dict[str, dict] = {}
    for post in _read_jsonl(d / "youtube-posts.jsonl"):
        pid = str(post.get("post_id"))
        posts[pid] = post
        url = post.get("url") or f"https://www.youtube.com/post/{pid}"
        yield build.document(
            kind="post", native_id=pid, type_name=TYPE_POST["youtube"], url=url,
            evidence_urls=[url, (post.get("provenance") or {}).get("url")],
            text=post.get("text") or "", authors=[post.get("author_name"), post.get("channel_handle")],
            published=post.get("published_time"),
            images=[i.get("url") for i in post.get("images") or [] if isinstance(i, dict) and i.get("url")],
            category=f"youtube/post-{post.get('kind') or 'text'}",
            extra={"channel_id": post.get("channel_id"), "kind": post.get("kind"),
                   "like_count": post.get("like_count"), "comment_count": post.get("comment_count"),
                   "attached_video_id": post.get("attached_video_id"), "shared_post_id": post.get("shared_post_id"),
                   "published_text": post.get("published_text"), "source": post.get("source")})
    for comment in _read_jsonl(d / "youtube-comments.jsonl"):
        cid = str(comment.get("comment_id"))
        target_type, target_id = comment.get("target_type"), str(comment.get("target_id") or "")
        if target_type == "post":
            target_url = (posts.get(target_id) or {}).get("url") or f"https://www.youtube.com/post/{target_id}"
        else:
            target_url = (videos.get(target_id) or {}).get("url") or f"https://www.youtube.com/watch?v={target_id}"
        joiner = "&" if "?" in target_url else "?"
        yield build.document(
            kind="comment", native_id=cid, type_name=TYPE_COMMENT["youtube"],
            url=f"{target_url}{joiner}lc={cid}",
            evidence_urls=[(comment.get("provenance") or {}).get("url"), target_url,
                           ((videos.get(target_id) or {}).get("provenance") or {}).get("watch_page_in_warc")],
            text=comment.get("text") or "", authors=[comment.get("author_name")],
            published=comment.get("published_time"), category="youtube/comment",
            extra={"target_type": target_type, "target_id": target_id, "parent_id": comment.get("parent_id"),
                   "thread_root_id": comment.get("thread_root_id"), "reply_depth": comment.get("reply_depth"),
                   "author_channel_id": comment.get("author_channel_id"),
                   "author_is_uploader": comment.get("author_is_uploader"), "like_count": comment.get("like_count"),
                   "is_pinned": comment.get("is_pinned"), "published_text": comment.get("published_text")})
    channels = _read_json(d / "youtube-channels.json") or {}
    for channel_id, channel in channels.items():
        if not isinstance(channel, dict):
            continue
        handle = channel.get("handle")
        url = channel.get("url") or (f"https://www.youtube.com/{handle}" if handle else None)
        yield build.document(
            kind="channel", native_id=str(channel.get("channel_id") or channel_id),
            type_name=TYPE_PROFILE["youtube"], url=url, evidence_urls=[url],
            text=channel.get("description") or "", title=channel.get("name") or handle,
            authors=[channel.get("name"), handle], description=channel.get("description"),
            links=[l.get("url") for l in channel.get("external_links") or [] if isinstance(l, dict) and l.get("url")],
            images=[u for u in (channel.get("avatar_url"), channel.get("banner_url")) if u],
            category="youtube/channel",
            extra={"handle": handle, "subscriber_count": channel.get("subscriber_count"),
                   "video_count": channel.get("video_count"), "source": channel.get("source")})
    for playlist in _read_jsonl(d / "youtube-playlists.jsonl"):
        pid = str(playlist.get("playlist_id"))
        url = playlist.get("url") or f"https://www.youtube.com/playlist?list={pid}"
        yield build.document(
            kind="playlist", native_id=pid, type_name=TYPE_PLAYLIST, url=url, evidence_urls=[url],
            text=" ".join(t for t in (playlist.get("title"), playlist.get("description")) if t),
            title=playlist.get("title"), description=playlist.get("description"),
            authors=[playlist.get("channel_name")], category="youtube/playlist",
            extra={"channel_id": playlist.get("channel_id"), "item_count": playlist.get("item_count")})


MAPPERS: dict[str, Callable[[Capture, DocumentBuilder], Iterator[dict]]] = {
    "facebook": _facebook, "instagram": _instagram, "x": _x, "youtube": _youtube,
}


# --- validation ------------------------------------------------------------

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INT_FIELDS = {"content_text_length", "status_code", "crawl_year", "content_length",
               "image_width", "image_height", "image_size", "image_faces_count"}
_LONG_FIELDS = {"wayback_date", "source_file_offset"}
_DATE_FIELDS = {"crawl_date", "publication_date", "last_modified", "timestamp"}


def validate_document(doc: dict) -> list[str]:
    """Problems that would make the document unloadable into the target
    schema: unknown fields, wrong shapes for typed fields, no id."""
    problems = []
    if not doc.get("id"):
        problems.append("missing id")
    for key, value in doc.items():
        if key not in SCHEMA_FIELDS:
            problems.append(f"unknown field {key}")
            continue
        if key in MULTI_VALUED and not isinstance(value, list):
            problems.append(f"{key} must be a list")
        elif key not in MULTI_VALUED and isinstance(value, list):
            problems.append(f"{key} must be a single value")
        if key in _INT_FIELDS | _LONG_FIELDS and not (isinstance(value, int) and not isinstance(value, bool)):
            problems.append(f"{key} must be an integer")
        if key in _DATE_FIELDS and not (isinstance(value, str) and _ISO.match(value)):
            problems.append(f"{key} must be an ISO 8601 UTC timestamp")
    if "wayback_date" in doc and len(str(doc["wayback_date"])) != 14:
        problems.append("wayback_date must have 14 digits")
    return problems


# --- driver ----------------------------------------------------------------

@dataclass
class IndexResult:
    platform: str
    capture_dir: str
    output: str
    manifest: str
    collection: str
    documents: int = 0
    by_type: dict = field(default_factory=dict)
    located: int = 0
    unlocated: int = 0
    warc_files: int = 0
    warc_records: int = 0
    invalid: int = 0
    warnings: list = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": INDEX_SCHEMA, "target_schema": TARGET_SCHEMA,
            "platform": self.platform, "capture_dir": self.capture_dir,
            "output": self.output, "manifest": self.manifest, "collection": self.collection,
            "documents": self.documents, "by_type": dict(sorted(self.by_type.items())),
            "located": self.located, "unlocated": self.unlocated,
            "warc_files": self.warc_files, "warc_records": self.warc_records,
            "invalid": self.invalid, "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }


def index_path_for(directory: Path, platform: str) -> Path:
    return Path(directory) / INDEX_DIR_NAME / f"{platform}-index.jsonl"


def read_index_manifest(directory: Path) -> dict | None:
    return _read_json(Path(directory) / INDEX_DIR_NAME / INDEX_MANIFEST_NAME)


def index_capture(directory: Path, *, output: Optional[Path] = None,
                  collection: Optional[str] = None, platform: Optional[str] = None,
                  progress: Optional[Callable[[str], None]] = None) -> IndexResult:
    """Index one capture directory. Returns the summary that is also
    written to ``index/index-manifest.json``."""
    say = progress or (lambda _msg: None)
    cap = load_capture(directory, platform)
    say(f"Scanning {len(cap.warc_paths)} WARC file(s)")
    locator = WarcLocator(cap.warc_paths)
    build = DocumentBuilder(cap, locator, collection)
    out_path = Path(output) if output else index_path_for(cap.directory, cap.platform)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = IndexResult(
        platform=cap.platform, capture_dir=str(cap.directory.resolve()),
        output=str(out_path.resolve()),
        manifest=str((out_path.parent / INDEX_MANIFEST_NAME).resolve()),
        collection=build.collection, warc_files=len(cap.warc_paths),
        warc_records=locator.records_scanned, warnings=list(locator.errors),
        generated_at=iso_utc(datetime.now(timezone.utc)),
    )
    if not cap.warc_paths:
        result.warnings.append(
            "No WARC file beside the records: documents carry no source_file_path/offset.")
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    say("Writing documents")
    with tmp.open("w", encoding="utf-8") as handle:
        for doc in MAPPERS[cap.platform](cap, build):
            problems = validate_document(doc)
            if problems:
                result.invalid += 1
                result.warnings.append(f"{doc.get('id')}: {'; '.join(problems)}")
                continue
            handle.write(json.dumps(doc, ensure_ascii=False) + "\n")
            result.documents += 1
            result.by_type[doc["type"]] = result.by_type.get(doc["type"], 0) + 1
    os.replace(tmp, out_path)
    result.located, result.unlocated = build.located, build.unlocated
    manifest_path = out_path.parent / INDEX_MANIFEST_NAME
    manifest_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                             encoding="utf-8")
    say(f"Indexed {result.documents} document(s)")
    return result
