"""A small site in the shape of X's web client, for tests.

The HTML pages carry nothing but a script: like X's client, they resolve
the account with ``UserByScreenName``, list its posts with ``UserTweets``
(``UserTweetsAndReplies`` on the Replies tab, ``UserMedia`` on the Media
tab), page by sending the bottom cursor back on scroll, open a post with
``TweetDetail`` and a search with ``SearchTimeline`` -- every call a GET to
``/i/api/graphql/<id>/<Name>`` with ``variables`` as a JSON query parameter.
The responses follow X's instruction/entry shapes: a pinned entry, tweet
entries, a thread module, a who-to-follow module, a promoted item, a
tombstone, cursors; a repost, a quote, a long-form post, a photo, a video,
a GIF. A media host answers ``name=orig`` requests and refuses a bare URL
for one image, as X's does when the format does not match.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USER = {"__typename": "User", "rest_id": "100",
        "legacy": {"screen_name": "qnl", "name": "Qatar National Library",
                   "description": "Books.", "followers_count": 1200,
                   "friends_count": 5, "statuses_count": 12, "protected": False,
                   "created_at": "Mon Jan 01 00:00:00 +0000 2018",
                   "profile_image_url_https": "http://MEDIAHOST/profile_images/100/avatar_normal.jpg",
                   "pinned_tweet_ids_str": ["1000000000000000050"]},
        "is_blue_verified": True}
OTHER = {"__typename": "User", "rest_id": "777",
         "legacy": {"screen_name": "someone_else", "name": "Someone Else",
                    "followers_count": 3, "friends_count": 3, "statuses_count": 3,
                    "protected": False}}
VIEWER = {"__typename": "User", "rest_id": "424242",
          "legacy": {"screen_name": "curator", "name": "Curator",
                     "followers_count": 0, "friends_count": 1, "statuses_count": 1}}


def _id(n: int) -> str:
    # ids are time-ordered, as X's snowflakes are: post n is n days before a
    # fixed day, so a smaller n is a newer post with a larger id
    return str(1000000000000000000 + (1000 - n))


USER["legacy"]["pinned_tweet_ids_str"] = [_id(50)]


def _created(n: int) -> str:
    # newest first: post n is n days before a fixed day
    from datetime import datetime, timedelta, timezone
    when = datetime(2026, 3, 1, tzinfo=timezone.utc) - timedelta(days=n)
    return when.strftime("%a %b %d %H:%M:%S +0000 %Y")


def tweet(n: int, *, user: dict = USER, text: str | None = None,
          media: list | None = None, reply_to: int | None = None,
          conversation: int | None = None, note: str | None = None,
          reply_count: int = 0) -> dict:
    post_id = _id(n)
    legacy = {"id_str": post_id, "full_text": text if text is not None else f"Post {n}",
              "created_at": _created(n), "conversation_id_str": _id(conversation or n),
              "favorite_count": abs(n), "retweet_count": 1, "reply_count": reply_count,
              "quote_count": 0, "bookmark_count": 0, "lang": "en",
              "entities": {"urls": [], "hashtags": [{"text": "books"}] if n % 5 == 0 else [],
                           "user_mentions": []}}
    if reply_to is not None:
        legacy["in_reply_to_status_id_str"] = _id(reply_to)
        legacy["in_reply_to_screen_name"] = "qnl"
    if media:
        legacy["extended_entities"] = {"media": media}
        legacy["entities"]["media"] = media
    result = {"__typename": "Tweet", "rest_id": post_id,
              "core": {"user_results": {"result": user}},
              "views": {"count": str(100 + abs(n))}, "legacy": legacy}
    if note:
        result["note_tweet"] = {"note_tweet_results": {"result": {
            "text": note, "entity_set": {"urls": [{"url": "https://t.co/x",
                                                   "expanded_url": "https://example.org/long",
                                                   "display_url": "example.org/long"}]}}}}
    return result


def photo(name: str) -> dict:
    return {"type": "photo", "media_key": f"3_{name}",
            "media_url_https": f"http://MEDIAHOST/media/{name}.jpg",
            "original_info": {"width": 4000, "height": 3000},
            "ext_alt_text": f"alt {name}"}


def video(name: str) -> dict:
    return {"type": "video", "media_key": f"7_{name}",
            "media_url_https": f"http://MEDIAHOST/ext_tw_video_thumb/{name}.jpg",
            "original_info": {"width": 1280, "height": 720},
            "video_info": {"duration_millis": 5000, "variants": [
                {"content_type": "application/x-mpegURL",
                 "url": f"http://MEDIAHOST/ext_tw_video/{name}/pl/playlist.m3u8"},
                {"bitrate": 288000, "content_type": "video/mp4",
                 "url": f"http://MEDIAHOST/ext_tw_video/{name}/480x270.mp4"},
                {"bitrate": 2176000, "content_type": "video/mp4",
                 "url": f"http://MEDIAHOST/ext_tw_video/{name}/1280x720.mp4"}]}}


def gif(name: str) -> dict:
    return {"type": "animated_gif", "media_key": f"16_{name}",
            "media_url_https": f"http://MEDIAHOST/tweet_video_thumb/{name}.jpg",
            "video_info": {"variants": [
                {"bitrate": 0, "content_type": "video/mp4",
                 "url": f"http://MEDIAHOST/tweet_video/{name}.mp4"}]}}


# the account's timeline, newest first, with the pinned post (50, old) first
PINNED = tweet(50, text="Pinned post")
ORIGINAL_BY_OTHER = tweet(60, user=OTHER, text="Something worth sharing",
                          media=[photo("shared")])
REPOST = tweet(1, text="RT @someone_else: Something worth sharing")
REPOST["legacy"]["retweeted_status_result"] = {"result": ORIGINAL_BY_OTHER}
QUOTED = tweet(61, user=OTHER, text="Quoted words")
QUOTE = tweet(2, text="Look at this")
QUOTE["legacy"]["is_quote_status"] = True
QUOTE["legacy"]["quoted_status_id_str"] = _id(61)
QUOTE["quoted_status_result"] = {"result": QUOTED}
LONG = tweet(3, text="This is the truncated form…",
             note="This is the whole of a long post, kept in full.")
PHOTO = tweet(4, media=[photo("one"), photo("two")], reply_count=3)
VIDEO = tweet(5, media=[video("clip")])
GIF = tweet(6, media=[gif("loop")])
PLAIN = [tweet(n) for n in range(7, 13)]
TIMELINE = [REPOST, QUOTE, LONG, PHOTO, VIDEO, GIF] + PLAIN
PAGE_SIZE = 6
REPLY_BY_ACCOUNT = tweet(-20, text="Replying to a reader", reply_to=70, conversation=70)
READER_POST = tweet(70, user=OTHER, text="A question", reply_count=1)
PROMOTED = tweet(90, user=OTHER, text="Buy things")
# the conversation of post 4: an earlier reply by a reader and one by the
# account, a tombstone, and a later reply behind a ShowMore cursor
REPLIES = [tweet(-80, user=OTHER, text="Nice", reply_to=4, conversation=4),
           tweet(-81, text="Thanks", reply_to=-80, conversation=4)]
MORE_REPLIES = [tweet(-82, user=OTHER, text="Late reply", reply_to=4, conversation=4)]
SEARCH_RESULTS = [tweet(30, user=OTHER, text="#books are great"),
                  tweet(31, text="#books at the library")]


def entry(t: dict, prefix: str = "tweet-") -> dict:
    return {"entryId": f"{prefix}{t['rest_id']}", "sortIndex": t["rest_id"],
            "content": {"entryType": "TimelineTimelineItem",
                        "__typename": "TimelineTimelineItem",
                        "itemContent": {"itemType": "TimelineTweet",
                                        "__typename": "TimelineTweet",
                                        "tweet_results": {"result": t}}}}


def promoted_entry(t: dict) -> dict:
    made = entry(t, "promoted-tweet-")
    made["content"]["itemContent"]["promotedMetadata"] = {"advertiser_results": {}}
    return made


def module(entry_id: str, tweets: list[dict]) -> dict:
    return {"entryId": entry_id, "sortIndex": "0",
            "content": {"entryType": "TimelineTimelineModule",
                        "__typename": "TimelineTimelineModule",
                        "items": [{"entryId": f"{entry_id}-tweet-{t['rest_id']}",
                                   "item": {"itemContent": {
                                       "itemType": "TimelineTweet",
                                       "tweet_results": {"result": t}}}}
                                  for t in tweets]}}


def who_to_follow() -> dict:
    return {"entryId": "who-to-follow-1", "sortIndex": "0",
            "content": {"entryType": "TimelineTimelineModule",
                        "items": [{"entryId": "who-to-follow-1-user-777",
                                   "item": {"itemContent": {"itemType": "TimelineUser",
                                                            "user_results": {"result": OTHER}}}}]}}


def cursor(kind: str, value: str) -> dict:
    return {"entryId": f"cursor-{kind.lower()}-{value}", "sortIndex": "0",
            "content": {"entryType": "TimelineTimelineCursor",
                        "__typename": "TimelineTimelineCursor",
                        "value": value, "cursorType": kind}}


def tombstone(reason: str = "This Post was deleted by the Post author.") -> dict:
    return {"entryId": "tweet-1000000000000000099", "sortIndex": "0",
            "content": {"entryType": "TimelineTimelineItem",
                        "itemContent": {"itemType": "TimelineTweet",
                                        "tweet_results": {"result": {
                                            "__typename": "TweetTombstone",
                                            "tombstone": {"text": {"text": reason}}}}}}}


def timeline_response(instructions: list[dict]) -> dict:
    return {"data": {"user": {"result": {"__typename": "User", "rest_id": "100",
                                         "timeline": {"timeline": {
                                             "instructions": instructions}}}}}}


PAGE_HTML = """<!doctype html><html><head><title>%(title)s</title></head><body>
<h1>%(title)s</h1><div style='height:2400px'>scroll me</div>
<script>
const vars = %(vars)s; const ops = %(ops)s;
let cursor = null; let busy = false; let started = false;
function url(op, v) { return '/i/api/graphql/q_' + op + '/' + op + '?variables=' + encodeURIComponent(JSON.stringify(v)) + '&features=%%7B%%7D'; }
async function call(op, v) { const r = await fetch(url(op, v), {headers: {'x-csrf-token': 'ct0value', 'authorization': 'Bearer AAAA'}}); if (!r.ok) return null; return r.json(); }
function bottom(j) {
  let found = null;
  JSON.stringify(j, (k, v) => { if (v && v.cursorType === 'Bottom' && v.value) found = v.value; if (v && v.cursorType === 'ShowMore' && v.value) found = v.value; return v; });
  return found;
}
(async () => {
  for (const [op, v] of ops) { const j = await call(op, v); if (j) cursor = bottom(j) || cursor; }
  started = true;
})();
window.addEventListener('scroll', async () => {
  if (busy || !started || !cursor) return;
  if (window.innerHeight + window.scrollY < document.body.scrollHeight - 50) return;
  busy = true;
  const j = await call('%(page_op)s', Object.assign({cursor}, vars));
  cursor = j ? bottom(j) : null;
  document.body.insertAdjacentHTML('beforeend', "<div style='height:1200px'>more</div>");
  busy = false;
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    host = "127.0.0.1:0"
    media_host = "127.0.0.1:0"
    rate_limit_once = False          # the next UserTweets page answers 429

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status=200,
              headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _fix(self, obj):
        return json.loads(json.dumps(obj).replace("MEDIAHOST", self.media_host))

    def _page(self, title: str, ops: list, page_op: str, vars_: dict) -> bytes:
        return (PAGE_HTML % {"title": title, "ops": json.dumps(ops),
                             "page_op": page_op, "vars": json.dumps(vars_)}).encode()

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        if segs[:3] == ["i", "api", "graphql"]:
            return self._graphql(segs[4] if len(segs) > 4 else "", parts.query)
        if not segs:
            if "auth_token=" in (self.headers.get("Cookie") or ""):
                return self._send(b"<html><head><title>Home / X</title></head><body>Home</body></html>")
            self.send_response(302)
            self.send_header("Location", "/i/flow/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if segs[:3] == ["i", "flow", "login"]:
            return self._send(b"<html><head><title>Log in to X</title></head><body>Log in</body></html>")
        if segs[:2] == ["i", "status"] and len(segs) == 3:
            return self._send(self._page(
                "Post", [["TweetDetail", {"focalTweetId": segs[2]}]], "TweetDetail",
                {"focalTweetId": segs[2]}))
        if segs[:1] == ["search"]:
            params = parse_qs(parts.query)
            query = (params.get("q") or [""])[0]
            product = "Top" if (params.get("f") or ["live"])[0] == "top" else "Latest"
            return self._send(self._page(
                "Search", [["SearchTimeline", {"rawQuery": query, "product": product}]],
                "SearchTimeline", {"rawQuery": query, "product": product}))
        if segs[0] == "nobody":
            return self._send(b"<html><body>This account doesn\xe2\x80\x99t exist</body></html>")
        if segs[0] in ("qnl", "someone_else"):
            op = {"with_replies": "UserTweetsAndReplies", "media": "UserMedia"}.get(
                segs[1] if len(segs) > 1 else "", "UserTweets")
            user_id = "100" if segs[0] == "qnl" else "777"
            ops = [["UserByScreenName", {"screen_name": segs[0]}],
                   [op, {"userId": user_id, "count": 20}]]
            if segs[0] == "qnl" and op == "UserTweets":
                # the signed-in client also fetches the viewer's own things
                ops.insert(0, ["HomeTimeline", {"count": 20}])
            return self._send(self._page(segs[0], ops, op, {"userId": user_id, "count": 20}))
        return self._send(b"not found", status=404)

    def _graphql(self, name: str, query: str):
        variables = json.loads((parse_qs(query).get("variables") or ["{}"])[0])
        headers = {"x-rate-limit-limit": "50", "x-rate-limit-remaining": "49",
                   "x-rate-limit-reset": str(int(__import__("time").time()) + 30)}
        body: dict
        if name == "UserByScreenName":
            user = {"qnl": USER, "someone_else": OTHER}.get(variables.get("screen_name"))
            body = {"data": {"user": {"result": self._fix(user)}}} if user else {"data": {}}
        elif name == "HomeTimeline":
            body = {"data": {"home": {"home_timeline_urt": {"instructions": [
                {"type": "TimelineAddEntries", "entries": [entry(self._fix(READER_POST)),
                                                          entry(self._fix(PLAIN[0]))]}]}}}}
        elif name == "UserTweets":
            if variables.get("userId") != "100":
                body = timeline_response([{"type": "TimelineAddEntries", "entries": [
                    entry(self._fix(ORIGINAL_BY_OTHER)), cursor("Bottom", "o1")]}])
            elif variables.get("cursor"):
                if Handler.rate_limit_once:
                    Handler.rate_limit_once = False
                    return self._send(json.dumps({"errors": [{"code": 88}]}).encode(),
                                      "application/json", status=429,
                                      headers={"x-rate-limit-reset": str(int(__import__("time").time()) + 1)})
                rest = [entry(self._fix(t)) for t in TIMELINE[PAGE_SIZE:]]
                body = timeline_response([{"type": "TimelineAddEntries",
                                           "entries": rest + [cursor("Bottom", "end")]}])
            else:
                first = [entry(self._fix(t)) for t in TIMELINE[:PAGE_SIZE]]
                body = timeline_response([
                    {"type": "TimelineClearCache"},
                    {"type": "TimelinePinEntry", "entry": entry(self._fix(PINNED))},
                    {"type": "TimelineAddEntries", "entries":
                        first[:2] + [promoted_entry(self._fix(PROMOTED)), who_to_follow()]
                        + first[2:] + [cursor("Top", "t1"), cursor("Bottom", "b1")]}])
        elif name == "UserTweetsAndReplies":
            body = timeline_response([{"type": "TimelineAddEntries", "entries": [
                module("profile-conversation-70", [self._fix(READER_POST), self._fix(REPLY_BY_ACCOUNT)]),
                entry(self._fix(PLAIN[1])), cursor("Bottom", "r-end")]}])
        elif name == "UserMedia":
            body = timeline_response([{"type": "TimelineAddEntries", "entries": [
                entry(self._fix(PHOTO)), entry(self._fix(VIDEO)), cursor("Bottom", "m-end")]}])
        elif name == "TweetDetail":
            focal = variables.get("focalTweetId")
            focal_tweet = next((t for t in TIMELINE + [PINNED] + SEARCH_RESULTS
                                if t["rest_id"] == focal), None)
            if focal_tweet is None:
                body = {"data": {"threaded_conversation_with_injections_v2": {"instructions": []}}}
            elif variables.get("cursor"):
                body = {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
                    {"type": "TimelineAddEntries", "entries": [
                        module("conversationthread-1082", [self._fix(t) for t in MORE_REPLIES])]}]}}}
            else:
                entries = [entry(self._fix(focal_tweet))]
                if focal == PHOTO["rest_id"]:
                    entries += [module("conversationthread-1080", [self._fix(t) for t in REPLIES]),
                                tombstone(),
                                {"entryId": "cursor-showmore-x", "sortIndex": "0",
                                 "content": {"entryType": "TimelineTimelineItem",
                                             "itemContent": {"itemType": "TimelineTimelineCursor",
                                                             "cursorType": "ShowMore",
                                                             "value": "more-1"}}}]
                body = {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
                    {"type": "TimelineAddEntries", "entries": entries},
                    {"type": "TimelineTerminateTimeline", "direction": "Top"}]}}}
        elif name == "SearchTimeline":
            if variables.get("cursor"):
                body = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
                    "instructions": [{"type": "TimelineAddEntries", "entries": [cursor("Bottom", "s-end")]}]}}}}}
            else:
                body = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
                    "instructions": [{"type": "TimelineAddEntries", "entries": [
                        entry(self._fix(t)) for t in SEARCH_RESULTS] + [cursor("Bottom", "s1")]}]}}}}}
        else:
            body = {"data": {}}
        return self._send(json.dumps(body).encode(), "application/json", headers=headers)


class MediaHandler(BaseHTTPRequestHandler):
    """X's media host as it answers: any page may read a file without
    credentials; an image is served at the rendition named, the original
    only for a matching format; a bare URL answers the medium rendition."""

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        params = parse_qs(parts.query)
        name = parts.path.rsplit("/", 1)[-1]
        if parts.path.endswith(".mp4"):
            return self._send(b"\x00\x00\x00\x18ftypmp42" + parts.path.encode(), "video/mp4")
        if parts.path.endswith(".m3u8"):
            return self._send(b"#EXTM3U", "application/vnd.apple.mpegurl")
        rendition = (params.get("name") or ["medium"])[0]
        fmt = (params.get("format") or [""])[0]
        if rendition == "orig" and name == "two" and fmt != "png":
            # an upload whose real format is not what the URL says
            return self._send(b"", "text/plain", status=404)
        return self._send(b"\x89PNG\r\n\x1a\n" + f"{parts.path}:{rendition}".encode(),
                          "image/png")


class _Site(ThreadingHTTPServer):
    media: "ThreadingHTTPServer | None" = None

    def shutdown(self):
        super().shutdown()
        if self.media is not None:
            self.media.shutdown()


def start() -> tuple[ThreadingHTTPServer, str]:
    server = _Site(("127.0.0.1", 0), Handler)
    host = f"127.0.0.1:{server.server_address[1]}"
    Handler.host = host
    media = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
    Handler.media_host = f"127.0.0.1:{media.server_address[1]}"
    server.media = media
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=media.serve_forever, daemon=True).start()
    return server, host
