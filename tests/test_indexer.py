"""Indexing a social capture into warc-indexer's document schema.

Each platform's records become one document per item, using only field
names the target schema defines, pointing at the WARC record of the page
the item came from. Covers the module, the ``index`` command and the
dashboard endpoints.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from webarc import cli, indexer
from webarc import server as srv


def write_warc(path: Path, pages: list[tuple[str, bytes]], *, date="2026-09-01T12:00:05Z",
               content_type="text/html; charset=utf-8", status="200 OK") -> None:
    with path.open("wb") as handle:
        writer = WARCWriter(handle, gzip=True)
        writer.write_record(writer.create_warcinfo_record(filename=path.name, info={"software": "test"}))
        for url, body in pages:
            headers = StatusAndHeaders(status, [("Content-Type", content_type)], protocol="HTTP/1.1")
            record = writer.create_warc_record(url, "response", payload=io.BytesIO(body), http_headers=headers)
            record.rec_headers.replace_header("WARC-Date", date)
            writer.write_record(record)


def jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_docs(result: indexer.IndexResult) -> list[dict]:
    return [json.loads(line) for line in Path(result.output).read_text(encoding="utf-8").splitlines()]


PERMALINK = "https://www.facebook.com/Some.Page/posts/hello-world/1593564465471991"


def facebook_capture(directory: Path, *, warc: bool = True) -> None:
    (directory / "facebook-manifest.json").write_text(json.dumps({
        "schema": "swm-facebook-capture-manifest-v1",
        "capture": {"crawl_id": 7, "name": "fb demo", "operator": "QNL",
                    "page_url": "https://www.facebook.com/Some.Page"},
        "updated_at": "2026-09-01T12:30:00Z"}), encoding="utf-8")
    jsonl(directory / "facebook-events.jsonl", [{"time": "2026-09-01T12:00:00Z", "event": "capture_created"}])
    jsonl(directory / "facebook-posts.jsonl", [{
        "post_id": "1593564465471991", "created_time": "2026-08-31T10:00:00Z", "permalink_url": PERMALINK,
        "author_id": "1", "author_name": "Some Page", "text": "قطر\nSecond line of the post",
        "is_pinned": False, "timeline_item": True, "reactions_count": 120, "comments_count": 1,
        "shares_count": 2, "media_urls": ["https://scontent.example/a.jpg"], "source": "graphql",
        "source_path": None, "synthetic_id": False, "aliases": []}])
    jsonl(directory / "facebook-comments.jsonl", [{
        "comment_id": "c1", "created_time": "2026-08-31T11:00:00Z", "parent_post_id": "1593564465471991",
        "parent_comment_id": None, "author_id": "2", "author_name": "A Reader", "text": "Mabrook",
        "depth": 0, "source_path": None, "media_urls": []}])
    if warc:
        write_warc(directory / "fb-demo-seed001-20260901120000-00001.warc.gz",
                   [("https://www.facebook.com/Some.Page", b"<html>page</html>"),
                    (PERMALINK, b"<html>post page</html>")])


class UrlAndTimeTests(unittest.TestCase):
    def test_normalisation_matches_warc_indexer_conventions(self):
        cases = {
            "https://www.Example.org/A/b/?z=1&a=2#frag": "http://example.org/A/b?a=2&z=1",
            "http://example.org:80/": "http://example.org/",
            "https://example.org:8443/x/": "http://example.org:8443/x",
            "https://www2.instagram.com/p/ABC/": "http://instagram.com/p/ABC",
            "http://example.org": "http://example.org/",
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(indexer.normalise_url(raw), expected)

    def test_times_in_every_shape_captures_write(self):
        utc = timezone.utc
        self.assertEqual(indexer.parse_time("2026-09-01T12:00:05Z"), datetime(2026, 9, 1, 12, 0, 5, tzinfo=utc))
        self.assertEqual(indexer.parse_time("2026-09-01T14:00:05+02:00"), datetime(2026, 9, 1, 12, 0, 5, tzinfo=utc))
        self.assertEqual(indexer.parse_time("20260901120005"), datetime(2026, 9, 1, 12, 0, 5, tzinfo=utc))
        self.assertEqual(indexer.parse_time("20260901"), datetime(2026, 9, 1, tzinfo=utc))
        self.assertEqual(indexer.parse_time(1756728005), datetime(2025, 9, 1, 12, 0, 5, tzinfo=utc))
        self.assertIsNone(indexer.parse_time("yesterday"))
        self.assertIsNone(indexer.parse_time(None))
        # a date that is no date is undated, not a failed run
        self.assertIsNone(indexer.parse_time("20261399"))
        self.assertIsNone(indexer.parse_time("20260931123456"))
        self.assertEqual(indexer.wayback_date(datetime(2026, 9, 1, 12, 0, 5, tzinfo=utc)), 20260901120005)

    def test_a_url_with_no_shape_to_normalise_is_kept_as_it_is(self):
        self.assertEqual(indexer.normalise_url("http://host:80abc/x"), "http://host:80abc/x")
        self.assertEqual(indexer.normalise_url("http://host:99999/"), "http://host:99999/")


class SchemaTests(unittest.TestCase):
    def test_unknown_fields_and_wrong_shapes_are_refused(self):
        good = {"id": "x:post:1", "author": ["a"], "crawl_date": "2026-09-01T12:00:05Z",
                "wayback_date": 20260901120005, "status_code": 200}
        self.assertEqual(indexer.validate_document(good), [])
        bad = dict(good, reactions=3, author="a", crawl_date="1 Sept", status_code="200")
        problems = indexer.validate_document(bad)
        self.assertIn("unknown field reactions", problems)
        self.assertIn("author must be a list", problems)
        self.assertIn("crawl_date must be an ISO 8601 UTC timestamp", problems)
        self.assertIn("status_code must be an integer", problems)
        self.assertIn("missing id", indexer.validate_document({"url": "x"}))

    def test_the_contract_lists_the_fields_documents_rely_on(self):
        for name in ("id", "url", "url_norm", "crawl_date", "wayback_date", "content", "type",
                     "source_file_path", "source_file_offset", "collection", "content_metadata_ss"):
            self.assertIn(name, indexer.SCHEMA_FIELDS)


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class FacebookTests(CaptureTestCase):
    def test_posts_and_comments_become_documents_that_point_at_the_page_record(self):
        facebook_capture(self.dir)

        result = indexer.index_capture(self.dir)

        self.assertEqual(result.platform, "facebook")
        self.assertEqual(result.documents, 2)
        self.assertEqual(result.by_type, {"Facebook Post": 1, "Facebook Comment": 1})
        self.assertEqual((result.located, result.unlocated, result.invalid), (2, 0, 0))
        self.assertEqual(Path(result.output), self.dir / "index" / "facebook-index.jsonl")
        self.assertTrue((self.dir / "index" / indexer.INDEX_MANIFEST_NAME).is_file())

        post, comment = read_docs(result)
        self.assertEqual(post["id"], "facebook:post:1593564465471991")
        self.assertEqual(post["type"], "Facebook Post")
        self.assertEqual(post["url"], PERMALINK)
        self.assertEqual(post["url_norm"], "http://facebook.com/Some.Page/posts/hello-world/1593564465471991")
        self.assertEqual(post["host"], "www.facebook.com")
        self.assertEqual(post["domain"], "facebook.com")
        self.assertEqual(post["content"], "قطر Second line of the post")
        self.assertEqual(post["title"], "قطر")
        self.assertEqual(post["author"], ["Some Page"])
        self.assertEqual(post["publication_date"], "2026-08-31T10:00:00Z")
        self.assertEqual(post["publication_year"], "2026")
        # the WARC record's own date, not the capture's start, as warc-indexer does
        self.assertEqual(post["crawl_date"], "2026-09-01T12:00:05Z")
        self.assertEqual(post["wayback_date"], 20260901120005)
        self.assertEqual(post["crawl_year"], 2026)
        self.assertEqual(post["record_type"], "response")
        self.assertEqual(post["status_code"], 200)
        self.assertEqual(post["source_file"], "fb-demo-seed001-20260901120000-00001.warc.gz")
        # no root given: the full path of where the file is now
        self.assertEqual(Path(post["source_file_path"]), (self.dir / post["source_file"]).resolve())
        self.assertIsInstance(post["source_file_offset"], int)
        self.assertTrue(post["hash"].startswith("sha1:"))
        self.assertEqual(post["links_images"], ["https://scontent.example/a.jpg"])
        self.assertEqual(post["links_domains"], ["scontent.example"])
        self.assertEqual(post["collection"], ["fb demo"])
        self.assertEqual(post["institution"], "QNL")
        self.assertIn("reactions_count=120", post["content_metadata_ss"])
        self.assertIn("platform=facebook", post["content_metadata_ss"])
        self.assertEqual(indexer.validate_document(post), [])

        self.assertEqual(comment["id"], "facebook:comment:c1")
        self.assertEqual(comment["url"], PERMALINK + "?comment_id=c1")
        # a comment's evidence is the post page it was read from
        self.assertEqual(comment["source_file_offset"], post["source_file_offset"])
        self.assertIn("parent_post_id=1593564465471991", comment["content_metadata_ss"])

    def test_the_post_record_is_the_one_at_its_own_offset(self):
        facebook_capture(self.dir)
        from warcio.archiveiterator import ArchiveIterator

        post = read_docs(indexer.index_capture(self.dir))[0]
        with open(self.dir / post["source_file"], "rb") as handle:
            handle.seek(post["source_file_offset"])
            record = next(iter(ArchiveIterator(handle)))
        self.assertEqual(record.rec_headers.get_header("WARC-Target-URI"), PERMALINK)
        self.assertEqual(record.content_stream().read(), b"<html>post page</html>")

    def test_without_a_warc_the_documents_carry_no_pointer_and_say_so(self):
        facebook_capture(self.dir, warc=False)

        result = indexer.index_capture(self.dir)

        self.assertEqual((result.documents, result.located, result.unlocated), (2, 0, 2))
        self.assertTrue(any("No WARC file" in w for w in result.warnings))
        post = read_docs(result)[0]
        for absent in ("source_file_path", "source_file_offset", "record_type", "status_code"):
            self.assertNotIn(absent, post)
        self.assertEqual(post["crawl_date"], "2026-09-01T12:00:00Z")   # the capture's start
        self.assertEqual(post["hash"], indexer._sha1_text(post["content"]))

    def test_collection_can_be_named_and_metadata_rights_travel_with_every_document(self):
        facebook_capture(self.dir)
        from webarc import metadata as md
        md.write_document(self.dir, md.document(
            job_id=7, kind="facebook", name="fb demo", operator="QNL",
            seeds=[{"url": "https://www.facebook.com/Some.Page"}],
            metadata={"job": md.normalise_fields({"Rights": "CC BY 4.0", "Subject": ["Heritage", "Doha"]}),
                      "seeds": {}}))

        docs = read_docs(indexer.index_capture(self.dir, collection="QNL social 2026"))

        for doc in docs:
            self.assertEqual(doc["collection"], ["QNL social 2026"])
            self.assertEqual(doc["access_terms"], ["CC BY 4.0"])
            self.assertEqual(doc["wct_subjects"], ["Heritage", "Doha"])

    def test_a_pfbid_permalink_still_finds_the_page_the_capture_targeted(self):
        # Facebook reports the post's permalink in its pfbid form; the page
        # the browser loaded (the capture's target) is the slug form.
        facebook_capture(self.dir, warc=False)
        pfbid = "https://www.facebook.com/Some.Page/posts/pfbid0abcDEF"
        (self.dir / "facebook-manifest.json").write_text(json.dumps({
            "capture": {"crawl_id": 7, "name": "fb demo", "operator": "QNL", "page_url": PERMALINK,
                        "target_type": "post", "target_post_id": "1593564465471991"}}), encoding="utf-8")
        post_row = json.loads((self.dir / "facebook-posts.jsonl").read_text(encoding="utf-8"))
        post_row["permalink_url"] = pfbid
        jsonl(self.dir / "facebook-posts.jsonl", [post_row])
        write_warc(self.dir / "fb-00001.warc.gz", [(PERMALINK, b"<html>the post page</html>")])

        result = indexer.index_capture(self.dir)
        post, comment = read_docs(result)

        self.assertEqual((result.located, result.unlocated), (2, 0))
        self.assertEqual(post["url"], pfbid)                    # the permalink is still the url
        self.assertIn("source_file_offset", post)              # but the evidence is the page loaded
        self.assertEqual(post["crawl_date"], "2026-09-01T12:00:05Z")
        self.assertEqual(comment["source_file_offset"], post["source_file_offset"])

    def test_the_pointer_survives_the_files_moving(self):
        # Whoever ingests the WARCs decides where they live: the index
        # carries that root, the file name and offset, and the record's id.
        facebook_capture(self.dir)

        moved = indexer.index_capture(self.dir, source_root="https://repo.example/warcstore/")
        post = read_docs(moved)[0]

        self.assertEqual(post["source_file_path"],
                         "https://repo.example/warcstore/fb-demo-seed001-20260901120000-00001.warc.gz")
        self.assertEqual(post["source_file"], "fb-demo-seed001-20260901120000-00001.warc.gz")
        self.assertTrue(post["warc_key_id"].startswith("<urn:uuid:"))
        self.assertEqual(moved.source_root, "https://repo.example/warcstore")
        self.assertEqual(indexer.read_index_manifest(self.dir)["source_root"],
                         "https://repo.example/warcstore")
        self.assertEqual(indexer.validate_document(post), [])

        here = read_docs(indexer.index_capture(self.dir))[0]
        self.assertEqual(Path(here["source_file_path"]).parent, self.dir.resolve())
        self.assertEqual(here["warc_key_id"], post["warc_key_id"])

    def test_an_existing_index_can_be_relocated_without_reindexing(self):
        facebook_capture(self.dir)
        first = indexer.index_capture(self.dir)
        before = read_docs(first)
        # the records and WARC are gone: only the index remains
        for path in list(self.dir.glob("*.jsonl")) + list(self.dir.glob("*.warc.gz")):
            path.unlink()

        moved = indexer.relocate_index(self.dir, "/mnt/repository/warcs/")
        after = read_docs(first)

        self.assertEqual((moved["documents"], moved["rewritten"]), (2, 2))
        self.assertEqual(moved["source_root"], "/mnt/repository/warcs")
        for old, new in zip(before, after):
            self.assertEqual(new["source_file_path"], "/mnt/repository/warcs/" + old["source_file"])
            self.assertEqual({k: v for k, v in new.items() if k != "source_file_path"},
                             {k: v for k, v in old.items() if k != "source_file_path"})
        manifest = indexer.read_index_manifest(self.dir)
        self.assertEqual(manifest["source_root"], "/mnt/repository/warcs")
        self.assertIn("relocated_at", manifest)

        # the index file itself is accepted too, and the root is used as given
        back = indexer.relocate_index(Path(first.output), "s3://archive/warcs/")
        self.assertEqual(back["rewritten"], 2)
        self.assertEqual(read_docs(first)[0]["source_file_path"],
                         "s3://archive/warcs/" + before[0]["source_file"])
        self.assertEqual(indexer.read_index_manifest(self.dir)["source_root"], "s3://archive/warcs")

    def test_relocating_needs_an_index_and_a_root(self):
        facebook_capture(self.dir)
        with self.assertRaises(indexer.IndexingError):
            indexer.relocate_index(self.dir, "/x")          # not indexed yet
        indexer.index_capture(self.dir)
        with self.assertRaises(indexer.IndexingError):
            indexer.relocate_index(self.dir, None)          # nowhere to point

    def test_html_200_is_preferred_over_other_records_of_the_same_url(self):
        facebook_capture(self.dir, warc=False)
        write_warc(self.dir / "a-00001.warc.gz", [(PERMALINK, b"{}")],
                   content_type="application/json", date="2026-09-01T12:00:01Z")
        write_warc(self.dir / "b-00002.warc.gz", [(PERMALINK, b"<html>the page</html>")],
                   date="2026-09-01T12:00:09Z")

        post = read_docs(indexer.index_capture(self.dir))[0]

        self.assertEqual(post["source_file"], "b-00002.warc.gz")
        self.assertEqual(post["crawl_date"], "2026-09-01T12:00:09Z")


class InstagramTests(CaptureTestCase):
    def test_posts_comments_and_profiles(self):
        (self.dir / "instagram-manifest.json").write_text(json.dumps({
            "capture": {"crawl_id": 3, "name": "ig demo", "operator": "QNL",
                        "targets": [{"url": "https://www.instagram.com/someone/", "kind": "profile"}]},
            "updated_at": "2026-09-01T12:30:00Z"}), encoding="utf-8")
        jsonl(self.dir / "instagram-posts.jsonl", [{
            "media_id": "111", "shortcode": "ABC123", "owner_username": "someone", "owner_id": "9",
            "kind": "video", "created_time": "2026-08-30T08:00:00Z", "caption": "Souq Waqif at dusk",
            "permalink_url": "https://www.instagram.com/p/ABC123/", "likes_count": 5, "comments_count": 1,
            "media_urls": ["https://cdn.example/v.mp4"], "media_files": ["media/v.mp4"], "is_pinned": False,
            "surface": "posts", "source": "browser", "comment_capture": {},
            "provenance": {"url": "https://www.instagram.com/api/v1/media/111/info/"}}])
        jsonl(self.dir / "instagram-comments.jsonl", [{
            "comment_id": "c9", "post_shortcode": "ABC123", "parent_comment_id": None, "author_id": "3",
            "author_username": "fan", "text": "Beautiful", "created_time": "2026-08-30T09:00:00Z",
            "likes_count": 0, "depth": 0, "provenance": {}}])
        (self.dir / "instagram-profiles.json").write_text(json.dumps({"someone": {
            "user_id": "9", "username": "someone", "full_name": "Some One", "biography": "Photos of Doha",
            "is_private": False, "is_verified": True, "followers_count": 10, "following_count": 2,
            "posts_count": 1, "profile_pic_url": "https://cdn.example/p.jpg",
            "external_url": "https://example.org", "provenance": {}}}), encoding="utf-8")
        write_warc(self.dir / "ig-00001.warc.gz",
                   [("https://www.instagram.com/p/ABC123/", b"<html>post</html>"),
                    ("https://www.instagram.com/someone/", b"<html>profile</html>")])

        result = indexer.index_capture(self.dir)
        post, comment, profile = read_docs(result)

        self.assertEqual(result.by_type, {"Instagram Post": 1, "Instagram Comment": 1, "Instagram Profile": 1})
        self.assertEqual(result.located, 3)
        self.assertEqual(post["id"], "instagram:post:111")
        self.assertEqual(post["category"], "instagram/video")
        self.assertEqual(comment["url"], "https://www.instagram.com/p/ABC123/c/c9/")
        self.assertEqual(comment["source_file_offset"], post["source_file_offset"])
        self.assertEqual(profile["id"], "instagram:profile:9")
        self.assertEqual(profile["type"], "Instagram Profile")
        self.assertEqual(profile["title"], "Some One")
        self.assertEqual(profile["links"], ["https://example.org"])
        self.assertNotEqual(profile["source_file_offset"], post["source_file_offset"])
        for doc in (post, comment, profile):
            self.assertEqual(indexer.validate_document(doc), [])


class XTests(CaptureTestCase):
    def test_posts_find_the_status_page_the_browser_loaded_and_users_their_profile(self):
        (self.dir / "x-manifest.json").write_text(json.dumps({
            "capture": {"crawl_id": 5, "name": "x demo", "operator": "QNL",
                        "targets": [{"url": "https://x.com/someone", "kind": "profile"}]}}), encoding="utf-8")
        jsonl(self.dir / "x-posts.jsonl", [{
            "post_id": "42", "author_id": "9", "author_handle": "someone", "author_name": "Some One",
            "relationship": "original", "capture_role": "target", "text": "Hello #doha https://t.co/x",
            "created_time": "2026-08-30T08:00:00Z", "conversation_id": "42", "in_reply_to_post_id": None,
            "in_reply_to_handle": None, "permalink_url": "https://x.com/someone/status/42", "lang": "en",
            "reply_count": 1, "repost_count": 2, "like_count": 3, "quote_count": 0, "bookmark_count": 0,
            "view_count": 99, "urls": [{"url": "https://t.co/x", "expanded_url": "https://example.org/a"}],
            "hashtags": ["doha"], "mentions": ["other"], "media_urls": ["https://pbs.example/m.jpg"],
            "media_files": ["media/m.jpg"], "is_pinned": False, "surface": "posts", "source": "browser",
            "reply_capture": {}, "provenance": {"url": "https://x.com/i/api/graphql/abc/TweetDetail?variables=1"}}])
        (self.dir / "x-users.json").write_text(json.dumps({"9": {
            "user_id": "9", "handle": "someone", "name": "Some One", "description": "Bio", "location": "Doha",
            "url": "https://example.org", "created_time": "2020-01-01T00:00:00Z", "followers_count": 1,
            "following_count": 1, "posts_count": 1, "is_protected": False, "is_verified": False,
            "profile_image_url": "https://pbs.example/p.jpg", "pinned_post_ids": [], "provenance": {}}}),
            encoding="utf-8")
        # the browser opens /i/status/<id>, not the handle permalink
        write_warc(self.dir / "x-00001.warc.gz",
                   [("https://x.com/i/status/42", b"<html>status</html>"),
                    ("https://x.com/someone", b"<html>profile</html>")])

        result = indexer.index_capture(self.dir)
        post, user = read_docs(result)

        self.assertEqual(result.by_type, {"X Post": 1, "X User": 1})
        self.assertEqual(result.located, 2)
        self.assertEqual(post["id"], "x:post:42")
        self.assertEqual(post["url"], "https://x.com/someone/status/42")
        self.assertEqual(post["author"], ["@someone", "Some One"])
        self.assertEqual(post["keywords"], ["doha"])
        self.assertEqual(post["content_language"], "en")
        self.assertEqual(post["links"], ["https://example.org/a"])
        self.assertEqual(post["links_images"], ["https://pbs.example/m.jpg"])
        self.assertEqual(sorted(post["links_hosts"]), ["example.org", "pbs.example"])
        self.assertIn("mentions=other", post["content_metadata_ss"])
        self.assertEqual(user["id"], "x:user:9")
        self.assertEqual(user["url"], "https://x.com/someone")
        self.assertEqual(user["publication_date"], "2020-01-01T00:00:00Z")
        for doc in (post, user):
            self.assertEqual(indexer.validate_document(doc), [])


class YouTubeTests(CaptureTestCase):
    def test_videos_posts_comments_channels_and_playlists(self):
        (self.dir / "youtube-manifest.json").write_text(json.dumps({
            "capture": {"crawl_id": 8, "name": "yt demo", "operator": "QNL",
                        "targets": [{"url": "https://www.youtube.com/@someone", "kind": "channel"}]}}),
            encoding="utf-8")
        jsonl(self.dir / "youtube-videos.jsonl", [{
            "video_id": "v1", "channel_id": "UC1", "channel_handle": "@someone", "channel_name": "Some One",
            "title": "A talk", "description": "About heritage", "published_time": "20260830",
            "duration_seconds": 60, "kind": "video", "live_status": None, "availability": "public",
            "view_count": 10, "like_count": 1, "comment_count": 1, "url": "https://www.youtube.com/watch?v=v1",
            "thumbnail_url": "https://i.ytimg.example/v1.jpg", "categories": ["Education"], "tags": ["heritage"],
            "chapters": [], "files": [], "surface": "videos", "source": "yt-dlp", "complete": True,
            "unavailable_reason": None, "comment_capture": {}, "media_file": "media/videos/v1/v1.mp4",
            "provenance": {"source": "yt-dlp", "watch_page_in_warc": "https://www.youtube.com/watch?v=v1"}}])
        jsonl(self.dir / "youtube-posts.jsonl", [{
            "post_id": "p1", "channel_id": "UC1", "channel_handle": "@someone", "author_name": "Some One",
            "kind": "image", "text": "Community post", "published_text": "1 day ago",
            "published_time": "2026-08-31T00:00:00Z", "like_count": 2, "comment_count": 0,
            "url": "https://www.youtube.com/post/p1", "images": [{"url": "https://yt3.example/i.jpg"}],
            "poll": None, "attached_video_id": None, "shared_post_id": None, "source": "browser",
            "comment_capture": {}, "provenance": {"url": "https://www.youtube.com/youtubei/v1/browse"}}])
        jsonl(self.dir / "youtube-comments.jsonl", [{
            "comment_id": "k1", "target_type": "video", "target_id": "v1", "parent_id": None,
            "thread_root_id": None, "reply_depth": 0, "author_name": "Viewer", "author_channel_id": "UC2",
            "author_is_uploader": False, "text": "Great talk", "published_time": "2026-08-31T01:00:00Z",
            "published_text": "1 day ago", "like_count": 0, "is_pinned": False, "is_favorited": False,
            "source": "yt-dlp", "provenance": {}}])
        (self.dir / "youtube-channels.json").write_text(json.dumps({"UC1": {
            "channel_id": "UC1", "handle": "@someone", "name": "Some One", "url": "https://www.youtube.com/@someone",
            "description": "Talks", "subscriber_count": 5, "video_count": 1, "avatar_url": "https://yt3.example/a.jpg",
            "banner_url": None, "external_links": [{"url": "https://example.org"}], "source": "yt-dlp",
            "provenance": {}}}), encoding="utf-8")
        jsonl(self.dir / "youtube-playlists.jsonl", [{
            "playlist_id": "PL1", "title": "Talks", "channel_id": "UC1", "channel_name": "Some One",
            "description": None, "item_count": 1, "url": "https://www.youtube.com/playlist?list=PL1",
            "source": "yt-dlp", "provenance": {}}])
        write_warc(self.dir / "yt-00001.warc.gz",
                   [("https://www.youtube.com/watch?v=v1", b"<html>watch</html>"),
                    ("https://www.youtube.com/post/p1", b"<html>post</html>")])

        result = indexer.index_capture(self.dir)
        docs = {d["id"]: d for d in read_docs(result)}

        self.assertEqual(result.by_type, {"YouTube Video": 1, "YouTube Post": 1, "YouTube Comment": 1,
                                          "YouTube Channel": 1, "YouTube Playlist": 1})
        self.assertEqual((result.located, result.unlocated), (3, 2))
        video = docs["youtube:video:v1"]
        self.assertEqual(video["title"], "A talk")
        self.assertEqual(video["content"], "A talk About heritage")
        self.assertEqual(video["publication_date"], "2026-08-30T00:00:00Z")
        self.assertEqual(sorted(video["keywords"]), ["Education", "heritage"])
        self.assertIn("source_file_offset", video)
        comment = docs["youtube:comment:k1"]
        self.assertEqual(comment["url"], "https://www.youtube.com/watch?v=v1&lc=k1")
        self.assertEqual(comment["source_file_offset"], video["source_file_offset"])
        self.assertIn("source_file_offset", docs["youtube:post:p1"])
        self.assertNotIn("source_file_offset", docs["youtube:channel:UC1"])
        self.assertEqual(docs["youtube:playlist:PL1"]["type"], "YouTube Playlist")
        for doc in docs.values():
            self.assertEqual(indexer.validate_document(doc), [])


class DetectionTests(CaptureTestCase):
    def test_a_folder_without_a_capture_is_refused(self):
        with self.assertRaises(indexer.IndexingError):
            indexer.index_capture(self.dir)
        with self.assertRaises(indexer.IndexingError):
            indexer.index_capture(self.dir / "missing")

    def test_the_platform_is_read_from_the_manifest_or_the_record_file(self):
        self.assertIsNone(indexer.detect_platform(self.dir))
        jsonl(self.dir / "x-posts.jsonl", [])
        self.assertEqual(indexer.detect_platform(self.dir), "x")
        (self.dir / "youtube-manifest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(indexer.detect_platform(self.dir), "youtube")


class CliTests(CaptureTestCase):
    def test_index_prints_a_summary_and_writes_the_files(self):
        facebook_capture(self.dir)
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["index", str(self.dir), "--collection", "QNL"])

        self.assertEqual(code, 0)
        self.assertIn("Indexed 2 document(s) from the facebook capture 'QNL'", out.getvalue())
        self.assertIn("1  Facebook Post", out.getvalue())
        self.assertIn("WARC records found for 2 document(s)", out.getvalue())
        self.assertTrue((self.dir / "index" / "facebook-index.jsonl").is_file())

    def test_index_json_and_output_path(self):
        facebook_capture(self.dir)
        target = self.dir / "elsewhere" / "docs.jsonl"
        out = io.StringIO()

        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["index", str(self.dir), "--json", "-o", str(target)])

        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["documents"], 2)
        self.assertEqual(Path(summary["output"]), target.resolve())
        self.assertTrue(target.is_file())
        self.assertTrue((target.parent / indexer.INDEX_MANIFEST_NAME).is_file())

    def test_index_relocate_rewrites_the_pointers_only(self):
        facebook_capture(self.dir)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["index", str(self.dir)]), 0)
        out = io.StringIO()

        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["index", str(self.dir), "--relocate",
                             "--source-root", "https://repo.example/warcs"])

        self.assertEqual(code, 0)
        self.assertIn("Rewrote source_file_path in 2 of 2 document(s) to https://repo.example/warcs",
                      out.getvalue())
        doc = json.loads((self.dir / "index" / "facebook-index.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(doc["source_file_path"].startswith("https://repo.example/warcs/"))

        for argv in (["index", str(self.dir / "nothing-here"), "--relocate", "--source-root", "/x"],
                     ["index", str(self.dir), "--relocate"]):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(argv)
            self.assertEqual(code, 1)
            self.assertIn("Cannot relocate", err.getvalue())

    def test_index_refuses_a_folder_that_is_not_a_capture(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["index", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("Cannot index", err.getvalue())


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"),
                                  monitor_resources=False)
        self.client = TestClient(self.app)

    def facebook_job(self) -> dict:
        made = self.client.post("/api/facebook", json={
            "page_url": "https://www.facebook.com/Some.Page", "mode": "latest_n", "latest_n": 5,
            "start": "wait"})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def test_indexing_a_social_job_from_the_dashboard(self):
        made = self.facebook_job()
        facebook_capture(Path(made["output_dir"]))
        self.assertIsNone(made["index"])

        indexed = self.client.post(f"/api/crawls/{made['id']}/index",
                                   json={"collection": "QNL social", "source_root": "/mnt/repo/warcs"})

        self.assertEqual(indexed.status_code, 200, indexed.text)
        body = indexed.json()
        self.assertEqual(body["documents"], 2)
        self.assertEqual(body["collection"], "QNL social")
        self.assertEqual(body["source_root"], "/mnt/repo/warcs")
        self.assertEqual(body["download_url"], f"/api/crawls/{made['id']}/index.jsonl")

        # the job card learns about it, and the files can be fetched back
        view = self.client.get(f"/api/crawls/{made['id']}").json()
        self.assertEqual(view["index"]["documents"], 2)
        self.assertEqual(view["index"]["by_type"], {"Facebook Post": 1, "Facebook Comment": 1})
        status = self.client.get(f"/api/crawls/{made['id']}/index")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["platform"], "facebook")
        download = self.client.get(f"/api/crawls/{made['id']}/index.jsonl")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(len(download.text.strip().splitlines()), 2)
        first = json.loads(download.text.strip().splitlines()[0])
        self.assertTrue(first["source_file_path"].startswith("/mnt/repo/warcs/"))

        # later, only where the WARCs live changes
        moved = self.client.post(f"/api/crawls/{made['id']}/index",
                                 json={"relocate": True, "source_root": "s3://archive/warcs"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["rewritten"], 2)
        again = json.loads(self.client.get(f"/api/crawls/{made['id']}/index.jsonl").text.splitlines()[0])
        self.assertEqual(again["source_file_path"], "s3://archive/warcs/" + again["source_file"])
        self.assertIn("attachment", download.headers.get("content-disposition", ""))

    def test_only_finished_social_jobs_can_be_indexed(self):
        crawl = self.client.post("/api/crawls", json={
            "name": "demo", "start": "wait",
            "config": {"seeds": [{"url": "https://a.example/"}]}})
        self.assertEqual(crawl.status_code, 201, crawl.text)
        refused = self.client.post(f"/api/crawls/{crawl.json()['id']}/index")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("Facebook, Instagram, X and YouTube", refused.json()["detail"])

        made = self.facebook_job()          # no records written yet
        empty = self.client.post(f"/api/crawls/{made['id']}/index")
        self.assertEqual(empty.status_code, 409)
        self.assertEqual(self.client.get(f"/api/crawls/{made['id']}/index").status_code, 404)
        self.assertEqual(self.client.get(f"/api/crawls/{made['id']}/index.jsonl").status_code, 404)
        self.assertEqual(self.client.post("/api/crawls/9999/index").status_code, 404)


if __name__ == "__main__":
    unittest.main()
