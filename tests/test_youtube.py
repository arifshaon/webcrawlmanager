"""YouTube capture: targets, configuration, extraction from YouTube's
shapes, the yt-dlp client against a stand-in downloader, the engine with a
fake client, the reader pages, and the job endpoints."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from webarc.youtube import (BLOCKED, DownloadInterrupted, LoginRequired, RateLimited,
                            TargetUnavailable, YouTubeCaptureConfig, YouTubeCaptureSession,
                            YouTubeChannel, YouTubeComment, YouTubePlaylist, YouTubePost,
                            YouTubeVideo, free_disk_check, parse_youtube_target)
from webarc.youtube_extract import (channel_from, comments_from, continuation_tokens,
                                    describe_youtubei_request, parse_count, posts_from,
                                    read_initial_data, relative_to_iso)
from webarc.youtube_ytdlp import (YtDlpClient, _translate, comments_from_info,
                                  netscape_cookie_lines, video_from_info)

from tests.fixtures.youtube import serve

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class TargetTests(unittest.TestCase):
    def test_a_handle_with_or_without_the_at_sign_is_a_channel(self):
        for raw in ("qnl", "@qnl", "https://www.youtube.com/@qnl", "youtube.com/@QNL/videos",
                    "https://m.youtube.com/@qnl/posts"):
            target = parse_youtube_target(raw)
            self.assertEqual(target.kind, "channel", raw)
            self.assertEqual(target.key, "youtube:@qnl", raw)
            self.assertEqual(target.url, "https://www.youtube.com/@" + target.handle)
        self.assertEqual(parse_youtube_target("@qnl").label, "@qnl")

    def test_channel_ids_and_legacy_paths_are_channels(self):
        by_id = parse_youtube_target("https://www.youtube.com/channel/" + serve.CHANNEL_ID)
        self.assertEqual(by_id.kind, "channel")
        self.assertEqual(by_id.channel_id, serve.CHANNEL_ID)
        self.assertEqual(by_id.key, "youtube:channel/" + serve.CHANNEL_ID)
        self.assertEqual(parse_youtube_target(serve.CHANNEL_ID).channel_id, serve.CHANNEL_ID)
        for raw in ("https://www.youtube.com/c/QatarNationalLibrary",
                    "https://www.youtube.com/user/qnl"):
            self.assertEqual(parse_youtube_target(raw).kind, "channel", raw)

    def test_video_addresses_in_every_form_are_one_video(self):
        for raw in ("https://www.youtube.com/watch?v=wGA27zJEnaU&t=12",
                    "https://youtu.be/wGA27zJEnaU", "https://www.youtube.com/shorts/wGA27zJEnaU",
                    "https://www.youtube.com/live/wGA27zJEnaU", "wGA27zJEnaU"):
            target = parse_youtube_target(raw)
            self.assertEqual(target.kind, "video", raw)
            self.assertEqual(target.video_id, "wGA27zJEnaU", raw)
            self.assertEqual(target.key, "youtube:video/wGA27zJEnaU")

    def test_a_playlist_is_a_playlist(self):
        target = parse_youtube_target("https://www.youtube.com/playlist?list=PLabcdefghijklmnop")
        self.assertEqual(target.kind, "playlist")
        self.assertEqual(target.playlist_id, "PLabcdefghijklmnop")
        self.assertEqual(target.key, "youtube:playlist/PLabcdefghijklmnop")

    def test_the_viewers_own_pages_and_other_sites_are_refused(self):
        for raw in ("https://www.youtube.com/feed/subscriptions",
                    "https://www.youtube.com/results?search_query=library",
                    "https://www.youtube.com/account", "https://vimeo.com/123", "", "  "):
            with self.assertRaises(ValueError, msg=raw):
                parse_youtube_target(raw)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_deduplication(self):
        config = YouTubeCaptureConfig.from_dict({"targets": "qnl, @qnl\nhttps://youtu.be/wGA27zJEnaU"})
        self.assertEqual(config.targets, ["https://www.youtube.com/@qnl",
                                          "https://www.youtube.com/watch?v=wGA27zJEnaU"])
        self.assertEqual(config.mode, "latest_n")
        self.assertEqual(config.latest_n, 50)
        self.assertEqual(config.surfaces, ("videos", "shorts", "streams", "posts"))
        self.assertEqual(config.max_resolution, "1080")
        self.assertIn("height<=1080", config.format_selector)
        self.assertEqual(config.max_comments_per_item, 1000)
        self.assertEqual(config.comment_sort, "new")
        self.assertFalse(config.write_warc)

    def test_no_media_when_the_resolution_is_none(self):
        config = YouTubeCaptureConfig.from_dict({"targets": ["qnl"], "max_resolution": "none"})
        self.assertFalse(config.capture_media)
        best = YouTubeCaptureConfig.from_dict({"targets": ["qnl"], "max_resolution": "best"})
        self.assertEqual(best.format_selector, "bestvideo*+bestaudio/best")

    def test_validation(self):
        bad = [
            {"targets": []},
            {"targets": ["qnl"], "mode": "everything"},
            {"targets": ["qnl"], "mode": "date_range"},
            {"targets": ["qnl"], "mode": "date_range", "from_date": "2026-03-01",
             "to_date": "2026-01-01"},
            {"targets": ["qnl"], "surfaces": ["comments"]},
            {"targets": ["qnl"], "max_resolution": "4k"},
            {"targets": ["qnl"], "comment_sort": "oldest"},
            {"targets": ["qnl"], "latest_n": "many"},
            {"targets": ["qnl"], "mode": "since_last"},
            {"targets": ["https://www.youtube.com/feed/you"]},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                YouTubeCaptureConfig.from_dict(raw)
        prior = {"youtube:@qnl": {"item_id": "wGA27zJEnaU", "date": "2026-01-01T00:00:00Z"}}
        self.assertEqual(YouTubeCaptureConfig.from_dict(
            {"targets": ["qnl"], "mode": "since_last", "prior_newest": prior}).mode, "since_last")


class ExtractionTests(unittest.TestCase):
    """YouTube's own shapes, as the fixture site serves them."""

    def test_initial_data_is_read_from_a_page(self):
        page = (b"<html><script>var ytInitialData = " +
                json.dumps(serve.channel_initial([])).encode() + b";</script></html>")
        documents = read_initial_data(page)
        self.assertEqual(len(documents), 1)
        self.assertEqual(channel_from(documents).channel_id, serve.CHANNEL_ID)
        self.assertEqual(read_initial_data(b"<html>nothing</html>"), [])

    def test_a_youtubei_request_is_described_from_its_body(self):
        seen = describe_youtubei_request(
            "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
            json.dumps({"browseId": serve.CHANNEL_ID, "continuation": "abc"}))
        self.assertEqual(seen["endpoint"], "browse")
        self.assertEqual(seen["browse_id"], serve.CHANNEL_ID)
        self.assertEqual(seen["continuation"], "abc")
        self.assertIsNone(describe_youtubei_request("https://www.youtube.com/watch?v=x", None))

    def test_the_channel_is_read_with_its_counts_avatar_and_banner(self):
        channel = channel_from([serve.channel_initial([])])
        self.assertEqual(channel.handle, "qnl")
        self.assertEqual(channel.name, "Qatar National Library")
        self.assertEqual(channel.subscriber_count, 1200)
        self.assertEqual(channel.video_count, 45)
        self.assertEqual(channel.avatar_url, "http://MEDIAHOST/avatar=s900")
        self.assertEqual(channel.banner_url, "http://MEDIAHOST/banner")
        self.assertEqual(channel.source, "browser")

    def test_posts_of_every_kind_are_read_in_order_with_estimated_dates(self):
        items = [serve.thread_item(p) for p in serve.POSTS] + [serve.continuation("posts-page-2")]
        posts = posts_from([serve.channel_initial(items)], {"response": "raw/responses/1"}, NOW)
        self.assertEqual([p.kind for p in posts], ["image", "poll", "video", "images", "text"])
        image, poll, video, images, text = posts
        self.assertEqual(image.images[0]["url"], "http://MEDIAHOST/img/one=s1600")
        self.assertEqual(image.published_text, "2 days ago")
        self.assertEqual(image.published_time, "2026-09-05T12:00:00Z")
        self.assertEqual(image.like_count, 12)
        self.assertEqual(image.comment_count, 3)
        self.assertEqual(image.channel_id, serve.CHANNEL_ID)
        self.assertEqual(image.channel_handle, "qnl")
        self.assertEqual([o["text"] for o in poll.poll["options"]], ["Books", "Films"])
        self.assertFalse(poll.poll["results_available"])
        self.assertEqual(video.attached_video_id, "wGA27zJEnaU")
        self.assertEqual(len(images.images), 2)
        self.assertEqual(text.text, "Post number 5 from the library.")
        self.assertEqual(image.provenance["response"], "raw/responses/1")
        self.assertEqual(image.provenance["renderer"], "backstagePostRenderer")
        self.assertEqual(continuation_tokens([serve.channel_initial(items)]), ["posts-page-2"])

    def test_a_shared_post_keeps_the_original_inside_not_beside(self):
        share = serve.post(11)
        share["backstageAttachment"] = {"postRenderer": serve.post(12, channel_id=serve.OTHER_CHANNEL_ID)}
        posts = posts_from([serve.channel_initial([serve.thread_item(share)])])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].kind, "shared")
        self.assertEqual(posts[0].shared_post_id, serve.post(12)["postId"])

    def test_comments_in_the_entity_shape_keep_threads_and_authorship(self):
        roots = comments_from([serve.comments_response(serve.ROOT_COMMENTS, "replies-A")],
                              "post-1", "post", now=NOW)
        replies = comments_from([serve.comments_response(serve.REPLIES, None, level=1)],
                                "post-1", "post", now=NOW)
        self.assertEqual([c.comment_id for c in roots], ["UgxrootA", "UgxrootB"])
        self.assertEqual(roots[0].text, "Wonderful post")
        self.assertEqual(roots[0].like_count, 4)
        self.assertEqual(roots[0].published_time, "2026-09-06T12:00:00Z")
        self.assertFalse(roots[0].author_is_uploader)
        self.assertTrue(roots[1].author_is_uploader)
        self.assertEqual(roots[0].thread_root_id, "UgxrootA")
        self.assertEqual(roots[0].reply_depth, 0)
        reply = replies[0]
        self.assertEqual(reply.parent_id, "UgxrootA")
        self.assertEqual(reply.thread_root_id, "UgxrootA")
        self.assertEqual(reply.reply_depth, 1)
        self.assertEqual(reply.target_type, "post")
        self.assertEqual(reply.provenance["renderer"], "commentEntityPayload")

    def test_counts_and_relative_times_are_read_as_youtube_writes_them(self):
        self.assertEqual(parse_count("1.2K subscribers"), 1200)
        self.assertEqual(parse_count("3,456"), 3456)
        self.assertEqual(parse_count({"simpleText": "12M views"}), 12_000_000)
        self.assertIsNone(parse_count("no likes"))
        self.assertEqual(relative_to_iso("3 weeks ago", NOW), "2026-08-17T12:00:00Z")
        self.assertEqual(relative_to_iso("Streamed 1 year ago", NOW), "2025-09-07T12:00:00Z")
        self.assertEqual(relative_to_iso("edited 5 hours ago", NOW), "2026-09-07T07:00:00Z")
        self.assertIsNone(relative_to_iso("Premieres tomorrow", NOW))


# ---------------------------------------------------------------------------
# yt-dlp client against a stand-in downloader
# ---------------------------------------------------------------------------

FLAT = [{"id": "vid00000001", "title": "First", "url": "https://www.youtube.com/watch?v=vid00000001",
         "playlist_index": 1},
        {"id": "vid00000002", "title": "[Private video]", "url": "https://www.youtube.com/watch?v=vid00000002",
         "playlist_index": 2},
        {"id": "vid00000003", "title": "Third", "url": "https://www.youtube.com/shorts/vid00000003",
         "playlist_index": 3}]


def full_info(video_id: str, **extra) -> dict:
    return {"id": video_id, "title": f"Video {video_id}", "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "channel_id": serve.CHANNEL_ID, "uploader_id": "@qnl", "channel": "Qatar National Library",
            "timestamp": 1756900000, "upload_date": "20260903", "duration": 61.0, "view_count": 100,
            "like_count": 5, "comment_count": 2, "availability": "public",
            "thumbnails": [{"url": "http://thumbs/small", "width": 120}, {"url": "http://thumbs/big", "width": 1280}],
            "formats": [{"format_id": "137"}], "categories": ["Education"], "tags": ["library"],
            "comments": [
                {"id": "c1", "parent": "root", "text": "Lovely", "author": "reader", "author_id": "UCr",
                 "timestamp": 1756910000, "like_count": 3},
                {"id": "c1.r1", "parent": "c1", "text": "Thanks", "author": "Qatar National Library",
                 "author_is_uploader": True, "timestamp": 1756920000}],
            **extra}


class FakeYoutubeDL:
    """What the client needs of ``yt_dlp.YoutubeDL``: a scripted extractor."""

    calls: list[dict] = []
    script: dict = {}

    def __init__(self, options: dict):
        self.options = options
        type(self).calls.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url: str, download: bool = False):
        answer = self.script.get(url)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(self, download)
        return answer

    def sanitize_info(self, info):
        return info

    def urlopen(self, url):
        class _Response:
            headers = {"content-type": "image/jpeg"}

            def read(self_inner):
                return b"\xff\xd8jpeg"
        return _Response()


class YtDlpClientTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.calls = []
        FakeYoutubeDL.script = {}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.evidence: dict[str, object] = {}

    def client(self, **kw) -> YtDlpClient:
        return YtDlpClient(ydl_factory=FakeYoutubeDL, scratch_dir=self.tmp / "scratch",
                           evidence_sink=lambda name, payload: self.evidence.setdefault(
                               name, payload) and f"evidence/yt-dlp/{name}.info.json", **kw)

    def test_options_carry_the_comment_cap_sort_and_replies_choice(self):
        client = self.client(max_comments=250, comment_sort="top", include_replies=False)
        FakeYoutubeDL.script["https://www.youtube.com/@qnl/videos"] = {
            "id": serve.CHANNEL_ID, "channel_id": serve.CHANNEL_ID, "uploader_id": "@qnl",
            "channel": "Qatar National Library", "playlist_count": 45, "channel_follower_count": 1200,
            "thumbnails": [{"id": "avatar_uncropped", "url": "http://a/avatar"},
                           {"id": "banner", "url": "http://a/banner"}], "entries": []}
        channel = client.channel(parse_youtube_target("qnl"))

        self.assertEqual(channel.channel_id, serve.CHANNEL_ID)
        self.assertEqual(channel.handle, "qnl")
        self.assertEqual(channel.subscriber_count, 1200)
        self.assertEqual(channel.avatar_url, "http://a/avatar")
        self.assertEqual(channel.banner_url, "http://a/banner")
        options = FakeYoutubeDL.calls[-1]
        youtube = options["extractor_args"]["youtube"]
        self.assertEqual(youtube["comment_sort"], ["top"])
        self.assertEqual(youtube["max_comments"], ["250", "all", "0", "0"])
        self.assertTrue(options["extract_flat"])
        self.assertNotIn("cookiefile", options)

    def test_a_listing_is_lazy_and_marks_private_entries(self):
        client = self.client()
        FakeYoutubeDL.script["https://www.youtube.com/@qnl/shorts"] = {"entries": iter(FLAT)}
        listing = client.list_items(parse_youtube_target("qnl"), "shorts")
        first = next(listing)
        self.assertEqual(first.video_id, "vid00000001")
        self.assertEqual(first.kind, "short")
        self.assertEqual(first.availability, "unknown")
        self.assertEqual(first.provenance["listing_position"], 1)
        self.assertEqual(next(listing).availability, "private")
        listing.close()
        with self.assertRaises(StopIteration):
            next(listing)

    def test_a_video_is_read_whole_kept_as_evidence_and_its_comments_modelled(self):
        client = self.client()
        FakeYoutubeDL.script["https://www.youtube.com/watch?v=vid00000001"] = full_info("vid00000001")
        video = client.video("vid00000001")

        self.assertEqual(video.title, "Video vid00000001")
        self.assertEqual(video.published_time, "2025-09-03T11:46:40Z")
        self.assertEqual(video.channel_handle, "qnl")
        self.assertEqual(video.thumbnail_url, "http://thumbs/big")
        self.assertEqual(video.duration_seconds, 61)
        self.assertEqual(video.provenance["evidence"], "evidence/yt-dlp/vid00000001.info.json")
        self.assertFalse(video.provenance["verbatim_platform_response"])
        self.assertNotIn("formats", video.raw)
        self.assertIn("formats", self.evidence["vid00000001"])
        comments = list(client.comments(video))
        self.assertEqual([c.comment_id for c in comments], ["c1", "c1.r1"])
        self.assertIsNone(comments[0].parent_id)
        self.assertEqual(comments[1].parent_id, "c1")
        self.assertEqual(comments[1].thread_root_id, "c1")
        self.assertEqual(comments[1].reply_depth, 1)
        self.assertTrue(comments[1].author_is_uploader)
        self.assertEqual(comments[0].published_time, "2025-09-03T14:33:20Z")

    def test_a_download_writes_under_the_video_folder_and_describes_each_file(self):
        client = self.client()
        progress: list[dict] = []

        def download(ydl, _download):
            home = Path(ydl.options["paths"]["home"])
            for hook in ydl.options["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
                      "filename": str(home / "vid00000001.f137.mp4")})
            (home / "vid00000001.mp4").write_bytes(b"video")
            (home / "vid00000001.jpg").write_bytes(b"thumb")
            (home / "vid00000001.en.vtt").write_bytes(b"WEBVTT")
            (home / "vid00000001.live_chat.json").write_bytes(b"{}")
            (home / "vid00000001.mp4.part").write_bytes(b"partial")
            return {**full_info("vid00000001"), "requested_downloads": [
                {"format_id": "137+140", "height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "mp4a"}]}

        FakeYoutubeDL.script["https://www.youtube.com/watch?v=vid00000001"] = download
        video = YouTubeVideo(video_id="vid00000001", url="https://www.youtube.com/watch?v=vid00000001")
        files = client.download(video, self.tmp / "media" / "videos" / "vid00000001", progress.append)

        roles = {Path(f["path"]).name: f["role"] for f in files}
        self.assertEqual(roles, {"vid00000001.mp4": "video", "vid00000001.jpg": "thumbnail",
                                 "vid00000001.en.vtt": "captions",
                                 "vid00000001.live_chat.json": "live_chat"})
        main = next(f for f in files if f["role"] == "video")
        self.assertEqual(main["resolution"], "1920x1080")
        self.assertEqual(main["format_id"], "137+140")
        captions = next(f for f in files if f["role"] == "captions")
        self.assertEqual(captions["language"], "en")
        self.assertEqual(progress[0]["percent"], 50.0)
        options = FakeYoutubeDL.calls[-1]
        self.assertEqual(options["subtitleslangs"], ["all"])
        self.assertTrue(options["continuedl"])
        self.assertTrue(options["writethumbnail"])

    def test_a_stop_during_a_download_is_an_interruption_the_engine_understands(self):
        client = self.client()

        def download(ydl, _download):
            for hook in ydl.options["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
            return {}

        def stop(_info):
            raise DownloadInterrupted("stopped")

        FakeYoutubeDL.script["https://www.youtube.com/watch?v=vid00000001"] = download
        with self.assertRaises(DownloadInterrupted) as caught:
            client.download(YouTubeVideo(video_id="vid00000001"), self.tmp / "v", stop)
        self.assertEqual(str(caught.exception), "stopped")

    def test_the_lent_session_is_a_private_file_deleted_at_the_end(self):
        client = self.client()
        client.use_cookies([{"name": "SAPISID", "value": "secret", "domain": ".youtube.com",
                             "path": "/", "secure": True, "httpOnly": False, "expires": 1800000000},
                            {"name": "SID", "value": "s", "domain": ".google.com", "path": "/",
                             "httpOnly": True, "expires": -1}])
        cookie_file = client._cookie_file
        self.assertTrue(cookie_file.exists())
        self.assertTrue(str(cookie_file).startswith(str(self.tmp / "scratch")))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(cookie_file.stat().st_mode), 0o600)
        text = cookie_file.read_text()
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSAPISID\tsecret", text)
        self.assertIn("#HttpOnly_.google.com\tTRUE\t/\tFALSE\t0\tSID\ts", text)
        FakeYoutubeDL.script["https://www.youtube.com/@qnl/videos"] = {"id": serve.CHANNEL_ID, "entries": []}
        client.channel(parse_youtube_target("qnl"))
        self.assertEqual(FakeYoutubeDL.calls[-1]["cookiefile"], str(cookie_file))
        client.forget_cookies()
        self.assertFalse(cookie_file.exists())
        self.assertIsNone(client._cookie_file)
        self.assertTrue(netscape_cookie_lines([]).startswith("# Netscape HTTP Cookie File"))

    def test_yt_dlps_messages_become_the_engines_conditions(self):
        self.assertIsInstance(_translate(Exception("Sign in to confirm you’re not a bot")), LoginRequired)
        self.assertIsInstance(_translate(Exception("HTTP Error 429: Too Many Requests")), RateLimited)
        private = _translate(Exception("Private video. Sign in if you've been granted access"))
        self.assertIsInstance(private, TargetUnavailable)
        self.assertEqual(private.availability, "private")
        members = _translate(Exception("Join this channel to get access to members-only content"))
        self.assertEqual(members.availability, "subscriber_only")
        gone = _translate(Exception("Video unavailable. This video has been removed by the uploader"))
        self.assertEqual(gone.availability, "unavailable")
        self.assertIn("Comments are disabled", str(_translate(Exception("Comments are turned off"))))
        client = self.client()
        FakeYoutubeDL.script["https://www.youtube.com/watch?v=gone0000000"] = Exception(
            "ERROR: [youtube] gone0000000: Video unavailable")
        with self.assertRaises(TargetUnavailable):
            client.video("gone0000000")
        self.assertEqual(client.fetch("http://thumbs/big"), (b"\xff\xd8jpeg", "image/jpeg"))

    def test_records_are_built_from_flat_and_full_info_alike(self):
        stream = video_from_info({"id": "s", "live_status": "was_live", "upload_date": "20260101"},
                                 surface="streams")
        self.assertEqual(stream.kind, "stream")
        self.assertEqual(stream.published_time, "2026-01-01T00:00:00Z")
        self.assertEqual(stream.url, "https://www.youtube.com/watch?v=s")
        self.assertEqual(comments_from_info({"comments": [{"id": "x", "parent": "root"}, {"nope": 1}]},
                                            "s")[0].thread_root_id, "x")


# ---------------------------------------------------------------------------
# The engine with a fake client
# ---------------------------------------------------------------------------

def vid(n: int, *, when: Optional[str] = None, kind: str = "video", availability: str = "public",
        comment_count: int = 0) -> YouTubeVideo:
    video_id = f"vid{n:08d}"
    return YouTubeVideo(video_id=video_id, title=f"Video {n}", kind=kind, availability=availability,
                        published_time=when, url=f"https://www.youtube.com/watch?v={video_id}",
                        channel_id=serve.CHANNEL_ID, comment_count=comment_count,
                        thumbnail_url=f"http://thumbs/{video_id}")


def fake_post(n: int, *, when: str, images: int = 0,
              comment_count: Optional[int] = None) -> YouTubePost:
    return YouTubePost(post_id=f"post{n:04d}", channel_id=serve.CHANNEL_ID, channel_handle="qnl",
                       text=f"Post {n}", published_text="some time ago", published_time=when,
                       images=[{"url": f"http://media/post{n}-{i}", "width": 1600, "height": 900}
                               for i in range(images)],
                       kind="images" if images else "text", comment_count=comment_count,
                       url=f"https://www.youtube.com/post/post{n:04d}",
                       provenance={"response": "raw/responses/000001.json"})


class FakeClient:
    """A YouTube client scripted per surface; records what was asked."""

    version = "fake"
    versions = {"videos": "fake-yt-dlp", "posts": "fake-browser"}

    def __init__(self, *, videos=(), shorts=(), streams=(), posts=(), playlist_items=(),
                 comments=None, post_comments=None, full=None, download_files=("mp4", "jpg")):
        self.surfaces = {"videos": list(videos), "shorts": list(shorts), "streams": list(streams),
                         "playlist": list(playlist_items)}
        self.post_list = list(posts)
        self.comments_by_id = comments or {}
        self.post_comments_by_id = post_comments or {}
        self.full = full or {}
        self.download_files = download_files
        self.calls: list[tuple] = []
        self.cookies_used: list[list[dict]] = []
        self.forgotten = 0
        self.fail_download: dict[str, list] = {}
        self.list_errors: list[Exception] = []
        self.channel_error: Optional[Exception] = None
        self.anomalies: list[dict] = []
        self.more: Optional[bool] = None

    def channel(self, target):
        self.calls.append(("channel", target.key))
        if self.channel_error is not None:
            error, self.channel_error = self.channel_error, None
            raise error
        return YouTubeChannel(channel_id=serve.CHANNEL_ID, handle="qnl", name="Qatar National Library",
                              url="https://www.youtube.com/@qnl", avatar_url="http://media/avatar",
                              subscriber_count=1200, video_count=45)

    def list_items(self, target, surface):
        self.calls.append(("list", surface))
        items = self.surfaces.get(surface, [])
        if self.list_errors:
            error = self.list_errors.pop(0)
            def failing():
                raise error
                yield  # pragma: no cover
            return failing()
        return iter([YouTubeVideo(**{**v.__dict__}) for v in items])

    def playlist(self, target):
        self.calls.append(("playlist", target.key))
        return YouTubePlaylist(playlist_id=target.playlist_id, title="Talks", channel_id=serve.CHANNEL_ID,
                               item_count=len(self.surfaces["playlist"]))

    def video(self, video_id):
        self.calls.append(("video", video_id))
        found = self.full.get(video_id)
        if isinstance(found, Exception):
            raise found
        if found is None:
            found = next((v for items in self.surfaces.values() for v in items if v.video_id == video_id),
                         None)
            if found is None:
                raise TargetUnavailable("no such video", "unavailable")
        full = YouTubeVideo(**{**found.__dict__})
        full.raw = {"id": video_id, "full": True}
        full.provenance = {"evidence": f"evidence/yt-dlp/{video_id}.info.json"}
        return full

    def download(self, video, dest, on_progress):
        self.calls.append(("download", video.video_id))
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        planned = self.fail_download.get(video.video_id)
        on_progress({"downloaded_bytes": 10, "total_bytes": 100, "percent": 10.0})
        if planned:
            raise planned.pop(0)
        on_progress({"downloaded_bytes": 100, "total_bytes": 100, "percent": 100.0})
        files = []
        for ext in self.download_files:
            path = dest / f"{video.video_id}.{ext}"
            path.write_bytes(b"data-" + ext.encode())
            files.append({"path": str(path), "role": "video" if ext == "mp4" else "thumbnail",
                          "resolution": "1920x1080" if ext == "mp4" else None})
        return files

    def comments(self, video):
        self.calls.append(("comments", video.video_id))
        found = self.comments_by_id.get(video.video_id, [])
        if isinstance(found, Exception):
            raise found
        return iter([YouTubeComment(**{**c.__dict__}) for c in found])

    def comments_more(self):
        return self.more

    def posts(self, channel):
        self.calls.append(("posts", channel.channel_id))
        return iter([YouTubePost(**{**p.__dict__, "images": [dict(i) for i in p.images]})
                     for p in self.post_list])

    def post_comments(self, post):
        self.calls.append(("post_comments", post.post_id))
        return iter([YouTubeComment(**{**c.__dict__})
                     for c in self.post_comments_by_id.get(post.post_id, [])])

    def fetch(self, url):
        self.calls.append(("fetch", url))
        return b"\x89PNG" + url.encode(), "image/png"

    def use_cookies(self, cookies):
        self.cookies_used.append(cookies)

    def forget_cookies(self):
        self.forgotten += 1

    def tool_report(self):
        return {"yt_dlp": "fake"}


def comment(comment_id: str, target_id: str, *, parent: Optional[str] = None,
            target_type: str = "video") -> YouTubeComment:
    return YouTubeComment(comment_id=comment_id, target_type=target_type, target_id=target_id,
                          parent_id=parent, thread_root_id=parent or comment_id,
                          reply_depth=1 if parent else 0, text=f"Comment {comment_id}")


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.persisted: list[dict] = []

    def session(self, client, control=None, disk=None, cookies=None, open_browser=None, **raw):
        config = YouTubeCaptureConfig.from_dict({"targets": ["qnl"], "mode": "latest_n",
                                                 "latest_n": 100, **raw})
        return YouTubeCaptureSession(
            config=config, client=client, output_dir=self.out, crawl_id=7, crawl_name="t",
            control_poll=control, disk_check=disk, session_cookies=cookies,
            open_browser=open_browser, sleep=lambda s: None,
            persist=lambda **kw: self.persisted.append(kw))

    def rows(self, name: str) -> list[dict]:
        path = self.out / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def manifest(self) -> dict:
        return json.loads((self.out / "youtube-manifest.json").read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        return self.rows("youtube-events.jsonl")


class EngineTests(EngineTestCase):
    def test_the_package_is_written_with_fixity_evidence_and_provenance(self):
        client = FakeClient(videos=[vid(1, when="2026-09-01T00:00:00Z", comment_count=2)],
                            posts=[fake_post(1, when="2026-08-30T00:00:00Z", images=1)],
                            comments={"vid00000001": [comment("c1", "vid00000001"),
                                                      comment("c1.r1", "vid00000001", parent="c1")]},
                            post_comments={"post0001": [comment("p1", "post0001", target_type="post")]})
        result = self.session(client).run()

        self.assertEqual(result["stop_reason"], "targets_complete")
        self.assertEqual((result["videos"], result["posts"], result["comments"]), (1, 1, 3))
        videos = self.rows("youtube-videos.jsonl")
        self.assertEqual(videos[0]["video_id"], "vid00000001")
        self.assertEqual(videos[0]["provenance"]["evidence"], "evidence/yt-dlp/vid00000001.info.json")
        self.assertEqual(videos[0]["provenance"]["target_key"], "youtube:@qnl")
        self.assertEqual({f["role"] for f in videos[0]["files"]}, {"video", "thumbnail"})
        self.assertEqual(videos[0]["comment_capture"]["status"], "reported_count_reached")
        post_rows = self.rows("youtube-posts.jsonl")
        self.assertEqual(post_rows[0]["images"][0]["file"], "media/posts/post0001/image-1.png")
        comments = self.rows("youtube-comments.jsonl")
        self.assertEqual({(c["target_type"], c["target_id"]) for c in comments},
                         {("video", "vid00000001"), ("post", "post0001")})
        reply = next(c for c in comments if c["comment_id"] == "c1.r1")
        self.assertEqual((reply["parent_id"], reply["thread_root_id"], reply["reply_depth"]),
                         ("c1", "c1", 1))
        for name in ("youtube-videos.csv", "youtube-posts.csv", "youtube-comments.csv",
                     "youtube-channels.json", "youtube-media.json", "checksums.sha256",
                     "youtube-checkpoint.json"):
            self.assertTrue((self.out / name).exists(), name)
        media = json.loads((self.out / "youtube-media.json").read_text())
        self.assertIn("media/videos/vid00000001/vid00000001.mp4", media)
        self.assertEqual(media["media/videos/vid00000001/vid00000001.mp4"]["fetched_via"], "yt-dlp")
        self.assertIn("media/channels/" + serve.CHANNEL_ID + "/avatar.png", media)
        checksums = (self.out / "checksums.sha256").read_text()
        self.assertIn("media/videos/vid00000001/vid00000001.mp4", checksums)
        channels = json.loads((self.out / "youtube-channels.json").read_text())
        self.assertEqual(channels[serve.CHANNEL_ID]["handle"], "qnl")
        manifest = self.manifest()
        self.assertEqual(manifest["schema"], "swm-youtube-capture-manifest-v1")
        self.assertEqual(manifest["capture"]["clients"], client.versions)
        self.assertEqual(manifest["capture"]["stopping_rule_fired"], "every_target_worked")
        self.assertEqual(manifest["layers"]["evidence"]["evidence_type"], "tool-derived metadata")
        self.assertTrue(manifest["layers"]["raw"]["verbatim_platform_response"])
        self.assertEqual(manifest["replay"]["expected"], "none")
        self.assertEqual(manifest["counts"]["videos_exported"], 1)
        self.assertEqual(manifest["counts"]["comment_statuses"], {"reported_count_reached": 1,
                                                                   "exhausted_unverified": 1})
        self.assertEqual(manifest["tools"]["yt_dlp"], "fake")
        self.assertEqual(client.forgotten, 1)
        self.assertEqual(self.persisted[0]["targets"]["youtube:@qnl"]["item_id"], "vid00000001")
        self.assertEqual({v["video_id"] for v in self.persisted[0]["videos"]}, {"vid00000001"})
        self.assertEqual({p["post_id"] for p in self.persisted[0]["posts"]}, {"post0001"})

    def test_posts_are_read_before_any_video_and_latest_n_counts_across_surfaces(self):
        client = FakeClient(videos=[vid(n, when=f"2026-08-{n:02d}T00:00:00Z") for n in range(1, 7)],
                            shorts=[vid(10 + n, kind="short") for n in range(3)],
                            posts=[fake_post(1, when="2026-08-30T00:00:00Z")])
        session = self.session(client, latest_n=4, include_comments=False)
        session.run()

        names = [c[0] for c in client.calls]
        self.assertLess(names.index("posts"), names.index("list"))
        self.assertEqual(len(self.rows("youtube-posts.jsonl")), 1)
        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 3)
        self.assertEqual(session.target_status["youtube:@qnl"]["items_selected"], 4)
        self.assertNotIn("comments", names)
        self.assertEqual(session.exclusions["older_than_requested"], 1)
        self.assertEqual(self.manifest()["capture"]["targets"][0]["status"], "done")

    def test_a_video_seen_on_two_tabs_is_one_record(self):
        one = vid(1, when="2026-08-01T00:00:00Z")
        client = FakeClient(videos=[one], shorts=[one], posts=[])
        session = self.session(client, surfaces=["videos", "shorts"], include_comments=False)
        session.run()
        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 1)
        self.assertEqual(session.exclusions["duplicate_across_surfaces"], 1)

    def test_a_date_range_reads_undated_entries_first_then_stops_after_older_ones(self):
        listing = [vid(n) for n in range(1, 10)]
        full = {v.video_id: vid(n, when=f"2026-{9 - n:02d}-15T00:00:00Z")
                for n, v in enumerate(listing, start=1)}
        client = FakeClient(videos=listing, full=full)
        session = self.session(client, mode="date_range", from_date="2026-05-01",
                               to_date="2026-07-31", surfaces=["videos"],
                               include_comments=False, consecutive_older=2)
        session.run()

        kept = [v["video_id"] for v in self.rows("youtube-videos.jsonl")]
        self.assertEqual(kept, ["vid00000002", "vid00000003", "vid00000004"])
        self.assertEqual(session.exclusions["newer_than_requested"], 1)
        self.assertEqual(session.exclusions["older_than_requested"], 2)
        read = [c[1] for c in client.calls if c[0] == "video"]
        self.assertEqual(read, [f"vid{n:08d}" for n in range(1, 7)])
        self.assertTrue(any(e["event"] == "lower_boundary_reached" for e in self.events()))
        manifest = self.manifest()
        self.assertEqual(manifest["coverage"]["newest_video"], "2026-07-15T00:00:00Z")
        self.assertEqual(manifest["coverage"]["oldest_video"], "2026-05-15T00:00:00Z")

    def test_since_last_stops_past_the_previous_newest_item(self):
        listing = [vid(n, when=f"2026-08-{20 - n:02d}T00:00:00Z") for n in range(1, 9)]
        client = FakeClient(videos=listing, posts=[fake_post(1, when="2026-08-25T00:00:00Z"),
                                                   fake_post(2, when="2026-08-01T00:00:00Z")])
        session = self.session(client, mode="since_last", include_comments=False,
                               consecutive_older=2, prior_newest={"youtube:@qnl": {
                                   "item_id": "vid00000003", "date": "2026-08-17T00:00:00Z"}})
        session.run()

        self.assertEqual([v["video_id"] for v in self.rows("youtube-videos.jsonl")],
                         ["vid00000001", "vid00000002"])
        self.assertEqual([p["post_id"] for p in self.rows("youtube-posts.jsonl")], ["post0001"])
        self.assertGreaterEqual(session.exclusions["captured_previously"], 2)
        newest = session.newest_by_target["youtube:@qnl"]
        self.assertEqual(newest["item_id"], "post0001")
        self.assertEqual(newest["date"], "2026-08-25T00:00:00Z")

    def test_a_private_video_in_the_listing_is_an_absence_not_a_record(self):
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z"),
                                    vid(2, availability="private"),
                                    vid(3, when="2026-07-01T00:00:00Z")])
        session = self.session(client, surfaces=["videos"], include_comments=False)
        session.run()

        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 2)
        self.assertEqual(session.absences[0]["video_id"], "vid00000002")
        self.assertEqual(session.absences[0]["availability"], "private")
        manifest = self.manifest()
        self.assertEqual(manifest["counts"]["items_unavailable"], 1)
        self.assertEqual(manifest["coverage"]["absences"][0]["video_id"], "vid00000002")
        self.assertIn("private", manifest["layers"]["normalised"]["availability_vocabulary"])

    def test_a_low_disk_holds_before_the_next_file_and_a_critical_level_interrupts_mid_file(self):
        levels = iter([("warning", "8% free"), ("ok", ""), ("ok", ""), ("critical", "1% free"),
                       ("ok", ""), ("ok", "")])
        state = {"level": ("ok", "")}

        def disk():
            try:
                state["level"] = next(levels)
            except StopIteration:
                state["level"] = ("ok", "")
            return state["level"]

        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z"),
                                    vid(2, when="2026-07-01T00:00:00Z")])
        session = self.session(client, disk=disk, surfaces=["videos"], include_comments=False)
        session.run()

        events = [e["event"] for e in self.events()]
        self.assertIn("disk_hold", events)
        self.assertIn("disk_hold_released", events)
        interrupted = next(e for e in self.events() if e["event"] == "download_interrupted")
        self.assertEqual(interrupted["reason"], "disk_critical")
        self.assertTrue(interrupted["partial_kept"])
        self.assertEqual(session.counters["disk_holds"], 2)
        self.assertEqual([c[1] for c in client.calls if c[0] == "download"],
                         ["vid00000001", "vid00000001", "vid00000002"])
        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 2)
        self.assertEqual([len(v["files"]) for v in self.rows("youtube-videos.jsonl")], [2, 2])

    def test_a_stop_during_a_download_keeps_the_partial_file_and_ends_the_run(self):
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z"),
                                    vid(2, when="2026-07-01T00:00:00Z")])
        session = self.session(client, control=lambda: "stop" if session.download_state else None,
                               surfaces=["videos"], include_comments=False)
        result = session.run()

        self.assertEqual(result["stop_reason"], "curator_stop")
        self.assertEqual([c[1] for c in client.calls if c[0] == "download"], ["vid00000001"])
        interrupted = next(e for e in self.events() if e["event"] == "download_interrupted")
        self.assertEqual(interrupted["reason"], "stopped")
        self.assertEqual(self.rows("youtube-videos.jsonl")[0]["files"], [])
        self.assertEqual(self.manifest()["capture"]["stopping_rule_fired"],
                         "curator_selected_stop_and_save")
        self.assertEqual(self.manifest()["capture"]["targets"][0]["status"], "interrupted")

    def test_a_sign_in_demand_holds_for_the_curator_then_lends_the_session(self):
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z")])
        client.full["vid00000001"] = LoginRequired("Sign in to confirm you're not a bot")
        opened: list[str] = []
        closed: list[bool] = []
        session = self.session(
            client, surfaces=["videos"], mode="date_range", from_date="2026-01-01",
            include_comments=False,
            control=lambda: "resume" if session.state == BLOCKED else None,
            cookies=lambda: [{"name": "SAPISID", "value": "s", "domain": ".youtube.com"}],
            open_browser=lambda url: (opened.append(url), lambda: closed.append(True))[1])

        def after_hold(video_id):
            client.full["vid00000001"] = vid(1, when="2026-08-01T00:00:00Z")
        original = client.video

        def video(video_id):
            found = client.full.get(video_id)
            if isinstance(found, Exception):
                client.full.pop(video_id)
                raise found
            return original(video_id)
        client.video = video
        session.run()

        self.assertEqual(client.cookies_used, [[{"name": "SAPISID", "value": "s", "domain": ".youtube.com"}]])
        self.assertTrue(opened[0].startswith("https://accounts.google.com/"))
        self.assertEqual(closed, [True])
        events = [e["event"] for e in self.events()]
        self.assertEqual(events.index("curator_needed") + 1, events.index("curator_resolved"))
        self.assertIn("session_lent_to_downloader", events)
        self.assertEqual(self.manifest()["capture"]["viewer"], "signed_in")
        self.assertIsNone(self.manifest()["completeness"]["signed_out_capture_meaning"])
        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 1)

    def test_a_rate_limit_is_waited_out_and_the_listing_resumed(self):
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z")])
        client.list_errors = [RateLimited(30.0, "429")]
        session = self.session(client, surfaces=["videos"], include_comments=False)
        session.run()

        self.assertEqual(session.counters["rate_limit_waits"], 1)
        self.assertEqual([c for c in client.calls if c[0] == "list"], [("list", "videos"), ("list", "videos")])
        self.assertEqual(len(self.rows("youtube-videos.jsonl")), 1)
        self.assertEqual(next(e for e in self.events() if e["event"] == "rate_limited")["wait_seconds"], 30.0)

    def test_comments_are_graded_capped_disabled_partial_and_absent(self):
        capped = vid(1, when="2026-08-04T00:00:00Z", comment_count=50)
        disabled = vid(2, when="2026-08-03T00:00:00Z", comment_count=5)
        none = vid(3, when="2026-08-02T00:00:00Z", comment_count=0)
        short = vid(4, when="2026-08-01T00:00:00Z", comment_count=9)
        client = FakeClient(
            videos=[capped, disabled, none, short],
            comments={"vid00000001": [comment(f"c{n}", "vid00000001") for n in range(50)],
                      "vid00000002": TargetUnavailable("Comments are disabled on this item.", "unavailable"),
                      "vid00000004": [comment("only", "vid00000004"),
                                      comment("only.r", "vid00000004", parent="only")]})
        session = self.session(client, surfaces=["videos"], max_comments_per_item=3,
                               include_replies=False)
        session.run()

        statuses = {v["video_id"]: v["comment_capture"] for v in self.rows("youtube-videos.jsonl")}
        self.assertEqual(statuses["vid00000001"]["status"], "capped")
        self.assertEqual(statuses["vid00000001"]["observed"], 3)
        self.assertEqual(statuses["vid00000001"]["cap"], 3)
        self.assertEqual(statuses["vid00000002"]["status"], "disabled")
        self.assertEqual(statuses["vid00000003"]["status"], "no_comments_reported")
        self.assertEqual(statuses["vid00000004"]["status"], "partial")
        self.assertEqual(statuses["vid00000004"]["observed"], 1)
        self.assertEqual(session.exclusions["replies_not_requested"], 1)
        self.assertEqual(self.manifest()["counts"]["comment_statuses"],
                         {"capped": 1, "disabled": 1, "no_comments_reported": 1, "partial": 1})

    def test_a_playlist_target_records_the_list_and_its_items_in_order(self):
        items = [vid(1, when="2026-08-01T00:00:00Z"), vid(2, availability="deleted"),
                 vid(3, when="2026-06-01T00:00:00Z")]
        client = FakeClient(playlist_items=items)
        session = self.session(client, targets=["https://www.youtube.com/playlist?list=PLtalkstalkstalks"],
                               include_comments=False)
        result = session.run()

        self.assertEqual(result["videos"], 2)
        playlists = self.rows("youtube-playlists.jsonl")
        self.assertEqual(playlists[0]["playlist_id"], "PLtalkstalkstalks")
        self.assertEqual(playlists[0]["title"], "Talks")
        rows = self.rows("youtube-playlist-items.jsonl")
        self.assertEqual([(r["position"], r["video_id"]) for r in rows],
                         [(1, "vid00000001"), (2, "vid00000002"), (3, "vid00000003")])
        self.assertEqual(rows[1]["availability"], "deleted")
        self.assertEqual(self.manifest()["counts"]["playlists_exported"], 1)
        self.assertIsNone(session.newest_by_target.get("youtube:playlist/PLtalkstalkstalks"))

    def test_a_single_video_target_is_read_whole_and_downloaded(self):
        client = FakeClient(full={"wGA27zJEnaU": YouTubeVideo(
            video_id="wGA27zJEnaU", title="One", published_time="2026-01-01T00:00:00Z",
            url="https://www.youtube.com/watch?v=wGA27zJEnaU", comment_count=0)})
        session = self.session(client, targets=["https://youtu.be/wGA27zJEnaU"], include_comments=False)
        result = session.run()

        self.assertEqual(result["videos"], 1)
        self.assertEqual(self.rows("youtube-videos.jsonl")[0]["surface"], "direct")
        self.assertEqual([c[0] for c in client.calls], ["video", "download"])

    def test_a_channel_youtube_does_not_have_is_reported_not_fatal(self):
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z")])
        client.channel_error = TargetUnavailable("YouTube reports this page does not exist.", "unavailable")
        session = self.session(client, targets=["nobody", "qnl"], surfaces=["videos"],
                               include_comments=False)
        result = session.run()

        self.assertEqual(result["stop_reason"], "targets_complete")
        targets = {t["label"]: t for t in self.manifest()["capture"]["targets"]}
        self.assertEqual(targets["@nobody"]["status"], "unavailable")
        self.assertEqual(targets["@nobody"]["availability"], "unavailable")
        self.assertEqual(targets["@qnl"]["status"], "done")
        self.assertEqual(self.manifest()["counts"]["targets_unavailable"], 1)

    def test_a_channel_without_videos_is_still_a_capture(self):
        client = FakeClient()
        session = self.session(client, surfaces=["videos", "posts"], include_comments=False)
        result = session.run()

        self.assertEqual(result["videos"], 0)
        status = self.manifest()["capture"]["targets"][0]
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["empty_surfaces"], ["posts", "videos"])
        self.assertEqual(len(json.loads((self.out / "youtube-channels.json").read_text())), 1)
        from webarc.youtube_render import build_site
        site = build_site(self.out)
        self.assertIn("Qatar National Library", (site / "index.html").read_text())

    def test_a_foreign_post_in_the_listing_is_not_the_channels(self):
        foreign = fake_post(9, when="2026-08-30T00:00:00Z")
        foreign.channel_id = serve.OTHER_CHANNEL_ID
        client = FakeClient(posts=[fake_post(1, when="2026-08-31T00:00:00Z"), foreign])
        session = self.session(client, surfaces=["posts"], include_comments=False)
        session.run()

        self.assertEqual([p["post_id"] for p in self.rows("youtube-posts.jsonl")], ["post0001"])
        self.assertEqual(session.counters["foreign_posts_skipped"], 1)

    def test_the_progress_report_carries_the_download_and_the_counts(self):
        reports: list[dict] = []
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z")],
                            posts=[fake_post(1, when="2026-08-02T00:00:00Z")])
        config = YouTubeCaptureConfig.from_dict({"targets": ["qnl"], "include_comments": False})
        session = YouTubeCaptureSession(config=config, client=client, output_dir=self.out,
                                        crawl_id=1, crawl_name="t", sleep=lambda s: None,
                                        on_progress=lambda **kw: reports.append(kw))
        session.run()

        self.assertTrue(all("download" in r["details"] for r in reports))
        last = reports[-1]
        self.assertEqual(last["state"], "stopped")
        self.assertEqual(last["details"]["phase"], "finished")
        self.assertEqual(last["details"]["videos_exported"], 1)
        self.assertEqual(last["details"]["channel_posts_exported"], 1)
        self.assertEqual(last["details"]["posts_exported"], 2)
        self.assertEqual(last["details"]["users_exported"], 1)
        self.assertEqual(last["details"]["download"], {})
        self.assertIn("Collected 1 video, 1 post", last["details"]["message"])


class ComposedClientTests(EngineTestCase):
    def test_both_halves_answer_and_their_anomalies_reach_the_events(self):
        from webarc.youtube import ComposedClient
        videos = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z", comment_count=1)],
                            comments={"vid00000001": [comment("c1", "vid00000001")]})
        videos.anomalies.append({"what": "no_formats", "video_id": "vid00000001"})
        posts = FakeClient(posts=[fake_post(1, when="2026-08-02T00:00:00Z")])
        posts.anomalies.append({"what": "no_posts_observed"})
        posts.more = True
        client = ComposedClient(videos=videos, posts=posts)
        session = self.session(client, surfaces=["videos", "posts"])
        result = session.run()

        self.assertEqual((result["videos"], result["posts"]), (1, 1))
        self.assertEqual([c[0] for c in posts.calls if c[0] in ("posts", "post_comments")],
                         ["posts", "post_comments"])
        self.assertNotIn("posts", [c[0] for c in videos.calls])
        anomalies = [e for e in self.events() if e["event"] == "client_anomaly"]
        self.assertEqual({a["what"] for a in anomalies}, {"no_formats", "no_posts_observed"})
        statuses = {v["video_id"]: v["comment_capture"]["status"] for v in self.rows("youtube-videos.jsonl")}
        self.assertEqual(statuses["vid00000001"], "reported_count_reached")
        self.assertEqual(self.rows("youtube-posts.jsonl")[0]["comment_capture"]["status"], "partial")
        self.assertEqual(self.manifest()["capture"]["clients"], {"videos": "fake", "posts": "fake"})
        self.assertEqual(videos.forgotten, 1)

    def test_a_missing_half_is_reported_not_fatal(self):
        from webarc.youtube import ComposedClient
        posts = FakeClient(posts=[fake_post(1, when="2026-08-02T00:00:00Z")])
        client = ComposedClient(videos=None, posts=posts)
        session = self.session(client, surfaces=["videos", "posts"], include_comments=False)
        result = session.run()

        self.assertEqual(result["posts"], 1)
        target = self.manifest()["capture"]["targets"][0]
        self.assertEqual(target["status"], "unavailable")
        self.assertIn("yt-dlp", target["reason"])


class DiskCheckTests(unittest.TestCase):
    def test_levels_follow_the_free_percentage(self):
        import shutil
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        original = shutil.disk_usage
        try:
            check = free_disk_check(Path("."), 10.0, 3.0)
            shutil.disk_usage = lambda _p: usage(100_000_000_000, 50_000_000_000, 50_000_000_000)
            self.assertEqual(check()[0], "ok")
            shutil.disk_usage = lambda _p: usage(100_000_000_000, 92_000_000_000, 8_000_000_000)
            level, message = check()
            self.assertEqual(level, "warning")
            self.assertIn("8.0%", message)
            shutil.disk_usage = lambda _p: usage(100_000_000_000, 98_000_000_000, 2_000_000_000)
            self.assertEqual(check()[0], "critical")
        finally:
            shutil.disk_usage = original


class RenderTests(EngineTestCase):
    def test_pages_show_the_video_the_post_and_their_comments(self):
        from webarc.youtube_render import build_site, is_youtube_capture
        client = FakeClient(videos=[vid(1, when="2026-08-01T00:00:00Z", comment_count=1)],
                            posts=[fake_post(1, when="2026-08-02T00:00:00Z", images=1)],
                            comments={"vid00000001": [comment("c1", "vid00000001")]},
                            post_comments={"post0001": [comment("p1", "post0001", target_type="post")]})
        self.session(client).run()

        self.assertTrue(is_youtube_capture(self.out))
        site = build_site(self.out)
        index = (site / "index.html").read_text()
        self.assertIn("Video 1", index)
        self.assertIn("Post 1", index)
        video_page = (site / "videos" / "vid00000001.html").read_text()
        self.assertIn("media/videos/vid00000001/vid00000001.mp4", video_page)
        self.assertIn("Comment c1", video_page)
        post_page = (site / "posts" / "post0001.html").read_text()
        self.assertIn("media/posts/post0001/image-1.png", post_page)
        self.assertIn("Comment p1", post_page)


class ApiTests(unittest.TestCase):
    """The YouTube job endpoints, on the server with simulated workers."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from webarc import server as srv
        self.srv = srv
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = srv.create_app(str(self.tmp / "swm.db"), str(self.tmp / "warcs"),
                                  simulate=True, replay_root=str(self.tmp / "replay"))
        self.client = TestClient(self.app)

    def create(self, **payload):
        body = {"targets": ["qnl", "https://youtu.be/wGA27zJEnaU"], "mode": "latest_n",
                "latest_n": 5, **payload}
        return self.client.post("/api/youtube", json=body)

    def row(self, crawl_id):
        return self.client.get(f"/api/crawls/{crawl_id}").json()

    def test_a_job_is_created_with_one_seed_per_target(self):
        response = self.create(max_resolution="720", surfaces=["videos", "posts"],
                               max_comments_per_item=200)

        self.assertEqual(response.status_code, 201, response.text)
        made = response.json()
        self.assertEqual(made["kind"], "youtube")
        self.assertEqual(made["seeds_total"], 2)
        self.assertEqual(made["name"], "yt-qnl-video-wGA27zJEnaU")
        stored = json.loads(self.srv._store().get_crawl(made["id"])["config_json"])
        self.assertTrue(stored["youtube"]["browser_profile_dir"].endswith(
            str(Path("browser-profiles") / "youtube")))
        self.assertEqual(stored["youtube"]["max_resolution"], "720")
        self.assertEqual(stored["youtube"]["surfaces"], ["videos", "posts"])
        self.assertEqual(stored["youtube"]["max_comments_per_item"], 200)
        self.assertEqual(stored["seeds"][0]["url"], "https://www.youtube.com/@qnl")

    def test_a_bad_target_is_refused_with_the_reason(self):
        response = self.create(targets=["https://www.youtube.com/feed/subscriptions"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("feed", response.text.lower())
        self.assertEqual(self.create(max_resolution="8k").status_code, 400)

    def test_since_last_without_prior_state_is_refused_and_the_state_endpoint_reports(self):
        self.assertEqual(self.create(mode="since_last", targets=["qnl"]).status_code, 400)
        self.assertFalse(self.client.get("/api/youtube/state", params={"target": "qnl"}).json()["available"])

        self.srv._store().record_youtube_capture(3, {"youtube:@qnl": {
            "item_id": "wGA27zJEnaU", "date": "2026-03-01T00:00:00Z", "handle": "qnl",
            "channel_id": serve.CHANNEL_ID, "url": "https://www.youtube.com/@qnl"}}, [
            {"video_id": "wGA27zJEnaU", "target_key": "youtube:@qnl",
             "published_time": "2026-03-01T00:00:00Z"},
            {"post_id": "Ugkxpost", "target_key": "youtube:@qnl", "published_time": "2026-02-01T00:00:00Z"}])

        state = self.client.get("/api/youtube/state", params={"target": "@QNL"}).json()
        self.assertTrue(state["available"])
        self.assertEqual(state["state"]["newest_item_id"], "wGA27zJEnaU")
        self.assertEqual(self.srv._store().get_youtube_item_ids("youtube:@qnl"), {"wGA27zJEnaU", "Ugkxpost"})
        self.assertEqual(self.create(mode="since_last", targets=["qnl"]).status_code, 201)

    def test_the_capability_is_reported_and_metadata_is_described(self):
        capabilities = self.client.get("/api/capabilities").json()
        self.assertIn("available", capabilities["youtube"])
        self.assertIn("yt_dlp", capabilities["youtube"])
        made = self.create(metadata={"job": {"Title": "QNL on YouTube"}}).json()
        seen = self.client.get(f"/api/crawls/{made['id']}/metadata").json()
        effective = {f["name"]: f["value"] for f in seen["seeds"][0]["effective"]}
        self.assertEqual(effective["Title"], "QNL on YouTube")
        self.assertEqual(effective["Type"], "Social media account")

    def test_replay_offers_the_pages_of_a_package(self):
        made = self.create().json()
        crawl_dir = Path(self.row(made["id"])["output_dir"])
        self.srv._store().set_pid(made["id"], None)
        (crawl_dir / "youtube-videos.jsonl").write_text(json.dumps({
            "video_id": "wGA27zJEnaU", "title": "A talk at the library", "kind": "video",
            "published_time": "2026-03-01T00:00:00Z", "files": [], "comment_capture": {}}) + "\n")
        (crawl_dir / "youtube-manifest.json").write_text(json.dumps({
            "capture": {"targets": [{"url": "https://www.youtube.com/@qnl", "label": "@qnl"}],
                        "mode": "latest_n"}}))

        response = self.client.post(f"/api/crawls/{made['id']}/replay")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "capture_pages")
        self.assertIn("A talk at the library", self.client.get(
            f"/captures/{made['id']}/pages/videos/wGA27zJEnaU.html").text)


if __name__ == "__main__":
    unittest.main()
