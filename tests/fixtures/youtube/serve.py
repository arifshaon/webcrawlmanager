"""A small site in the shape of YouTube's web client, for the Posts collector.

A channel page and its Posts tab embed ``ytInitialData`` in the HTML, as
YouTube's do, with the channel's ``channelMetadataRenderer`` and the first
posts as ``backstagePostRenderer`` items followed by a
``continuationItemRenderer``. Scrolling makes the page POST
``/youtubei/v1/browse`` with the continuation token and the answer carries
the rest under ``appendContinuationItemsAction``. A post page embeds the
post and fetches its comments through ``/youtubei/v1/next`` in the entity
shape YouTube serves now (``commentViewModel`` plus
``commentEntityPayload`` mutations), with a "View replies" control whose
click fetches the replies. A media host serves the post images.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CHANNEL_ID = "UCqnlqnlqnlqnlqnlqnlqnlq"
OTHER_CHANNEL_ID = "UCotherotherotherotherot"


def runs(text: str) -> dict:
    return {"runs": [{"text": text}]}


def post(n: int, *, kind: str = "text", channel_id: str = CHANNEL_ID, handle: str = "qnl",
         ago: str = "2 days ago", likes: str = "12", replies: str = "3") -> dict:
    post_id = f"Ugkx{n:04d}postpostpostpostpost"
    renderer = {
        "postId": post_id,
        "authorText": {"simpleText": "Qatar National Library" if channel_id == CHANNEL_ID else "Someone"},
        "authorEndpoint": {"browseEndpoint": {"browseId": channel_id, "canonicalBaseUrl": f"/@{handle}"}},
        "contentText": runs(f"Post number {n} from the library."),
        "publishedTimeText": runs(ago),
        "voteCount": {"simpleText": likes},
        "actionButtons": {"commentActionButtonsRenderer": {
            "replyButton": {"buttonRenderer": {"text": {"simpleText": replies}}}}},
    }
    if kind == "image":
        renderer["backstageAttachment"] = {"backstageImageRenderer": {"image": {"thumbnails": [
            {"url": "http://MEDIAHOST/img/one=s400", "width": 400, "height": 300},
            {"url": "http://MEDIAHOST/img/one=s1600", "width": 1600, "height": 1200}]}}}
    elif kind == "images":
        renderer["backstageAttachment"] = {"postMultiImageRenderer": {"images": [
            {"backstageImageRenderer": {"image": {"thumbnails": [
                {"url": f"http://MEDIAHOST/img/multi{i}=s1200", "width": 1200, "height": 900}]}}}
            for i in range(2)]}}
    elif kind == "poll":
        renderer["backstageAttachment"] = {"pollRenderer": {
            "choices": [{"text": runs("Books")}, {"text": runs("Films")}],
            "totalVotes": {"simpleText": "128 votes"}}}
    elif kind == "video":
        renderer["backstageAttachment"] = {"videoRenderer": {"videoId": "wGA27zJEnaU",
                                                             "title": runs("A video")}}
    return renderer


POSTS = [post(1, kind="image"), post(2, kind="poll"), post(3, kind="video", ago="1 week ago"),
         post(4, kind="images", ago="3 weeks ago"), post(5, ago="2 months ago")]
MORE_POSTS = [post(6, ago="3 months ago"), post(7, ago="1 year ago")]
FOREIGN = post(9, channel_id=OTHER_CHANNEL_ID, handle="someone")
PAGE_SIZE = 5


def thread_item(renderer: dict) -> dict:
    return {"backstagePostThreadRenderer": {"post": {"backstagePostRenderer": renderer}}}


def continuation(token: str) -> dict:
    return {"continuationItemRenderer": {"continuationEndpoint": {
        "continuationCommand": {"token": token, "request": "CONTINUATION_REQUEST_TYPE_BROWSE"}}}}


def channel_initial(tab_items: list[dict]) -> dict:
    return {
        "metadata": {"channelMetadataRenderer": {
            "title": "Qatar National Library", "description": "Books, films, and more.",
            "externalId": CHANNEL_ID, "vanityChannelUrl": "http://www.youtube.com/@qnl",
            "channelUrl": f"http://www.youtube.com/channel/{CHANNEL_ID}",
            "avatar": {"thumbnails": [{"url": "http://MEDIAHOST/avatar=s88", "width": 88, "height": 88},
                                      {"url": "http://MEDIAHOST/avatar=s900", "width": 900, "height": 900}]}}},
        "header": {"pageHeaderRenderer": {"content": {"pageHeaderViewModel": {"metadata": {
            "contentMetadataViewModel": {"metadataRows": [{"metadataParts": [
                {"text": {"content": "@qnl"}}, {"text": {"content": "1.2K subscribers"}},
                {"text": {"content": "45 videos"}}]}]}},
            "banner": {"imageBannerViewModel": {"image": {"sources": [
                {"url": "http://MEDIAHOST/banner", "width": 2120, "height": 351}]}}}}}}},
        "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
            "title": "Posts", "selected": True,
            "content": {"sectionListRenderer": {"contents": [{"itemSectionRenderer": {
                "contents": tab_items}}]}}}}]}},
    }


def comment_entity(comment_id: str, text: str, author: str, channel_id: str, *,
                   likes: str = "4", replies: str = "0", level: int = 0, ago: str = "1 day ago") -> dict:
    return {"entityBatchUpdate": {"mutations": [{
        "entityKey": f"key-{comment_id}", "type": "ENTITY_UPDATE_TYPE_REPLACE",
        "payload": {"commentEntityPayload": {
            "key": f"key-{comment_id}",
            "properties": {"commentId": comment_id, "content": {"content": text},
                           "publishedTime": ago, "replyLevel": level,
                           "toolbarStateKey": f"toolbar-{comment_id}"},
            "author": {"channelId": channel_id, "displayName": author,
                       "isCreator": channel_id == CHANNEL_ID, "isVerified": False},
            "toolbar": {"likeCountNotliked": likes, "replyCount": replies}}}}]}}


ROOT_COMMENTS = [
    ("UgxrootA", "Wonderful post", "reader", "UCreaderreaderreaderreader", "2"),
    ("UgxrootB", "Thank you all", "Qatar National Library", CHANNEL_ID, "0"),
]
REPLIES = [("UgxrootA.reply1", "Agreed", "other", "UCotherotherotherotherot"),
           ("UgxrootA.reply2", "Glad you liked it", "Qatar National Library", CHANNEL_ID)]


def comments_response(items: list[tuple], replies_token: str | None, level: int = 0) -> dict:
    """A youtubei/v1/next answer in the entity shape."""
    view_models = []
    mutations = []
    for comment_id, text, author, channel_id, reply_count in [(*i, "0")[:5] if len(i) == 4 else i for i in items]:
        thread = {"commentThreadRenderer": {
            "commentViewModel": {"commentViewModel": {"commentId": comment_id,
                                                      "commentKey": f"key-{comment_id}"}}}}
        if replies_token and reply_count not in ("0", None):
            thread["commentThreadRenderer"]["replies"] = {"commentRepliesRenderer": {
                "contents": [{"continuationItemRenderer": {"continuationEndpoint": {
                    "continuationCommand": {"token": replies_token}}}}],
                "viewReplies": {"buttonRenderer": {"text": runs(f"{reply_count} replies")}}}}
        view_models.append(thread)
        mutations.extend(comment_entity(comment_id, text, author, channel_id,
                                        replies=reply_count, level=level)["entityBatchUpdate"]["mutations"])
    return {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": view_models}}],
            "frameworkUpdates": {"entityBatchUpdate": {"mutations": mutations}}}


PAGE = """<!doctype html><html><head><title>%(title)s</title></head><body>
<h1>%(title)s</h1><div style='height:2400px'>scroll me</div>%(extra)s
<script>var ytInitialData = %(initial)s;</script>
<script>
let token = %(token)s; let busy = false;
async function call(endpoint, body) {
  const r = await fetch('/youtubei/v1/' + endpoint + '?prettyPrint=false', {method: 'POST',
    headers: {'content-type': 'application/json', 'authorization': 'SAPISIDHASH 123_abc'},
    body: JSON.stringify(Object.assign({context: {client: {clientName: 'WEB'}}}, body))});
  return r.ok ? r.json() : null;
}
function nextToken(j) {
  let found = null;
  JSON.stringify(j, (k, v) => { if (k === 'continuationCommand' && v && v.token && !found) found = v.token; return v; });
  return found;
}
window.addEventListener('scroll', async () => {
  if (busy || !token) return;
  if (window.innerHeight + window.scrollY < document.body.scrollHeight - 50) return;
  busy = true;
  const j = await call('%(endpoint)s', {continuation: token});
  token = j ? nextToken(j) : null;
  document.body.insertAdjacentHTML('beforeend', "<div style='height:1200px'>more</div>");
  busy = false;
});
%(more)s
</script></body></html>"""

POST_PAGE_JS = """
(async () => {
  const j = await call('next', {continuation: 'comments-1'});
  if (j) { window.__replyToken = nextToken(j); }
  const b = document.getElementById('more-replies');
  b.addEventListener('click', async () => { if (window.__replyToken) { await call('next', {continuation: window.__replyToken}); window.__replyToken = null; } });
})();
"""


class Handler(BaseHTTPRequestHandler):
    host = "127.0.0.1:0"
    media_host = "127.0.0.1:0"

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fix(self, obj):
        return json.loads(json.dumps(obj).replace("MEDIAHOST", self.media_host))

    def _page(self, title, initial, token, endpoint="browse", more="", extra=""):
        return (PAGE % {"title": title, "initial": json.dumps(self._fix(initial)),
                        "token": json.dumps(token), "endpoint": endpoint, "more": more,
                        "extra": extra}).encode()

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        if segs[:1] == ["@qnl"] or segs[:2] == ["channel", CHANNEL_ID]:
            tab = segs[-1] if len(segs) > 1 and segs[-1] in ("posts", "community", "videos") else None
            if tab in ("posts", "community"):
                items = [thread_item(p) for p in POSTS[:PAGE_SIZE]] + [thread_item(FOREIGN)] + \
                        [continuation("posts-page-2")]
                return self._send(self._page("Posts", channel_initial(items), "posts-page-2"))
            return self._send(self._page("Channel", channel_initial([]), None))
        if parts.path == "/watch":
            video_id = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p).get("v", "")
            extra = (f"<img src='http://MEDIAHOST/thumb/{video_id}'>"
                     f"<video src='http://MEDIAHOST/videoplayback?id={video_id}' autoplay></video>")
            return self._send(self._page(f"Watch {video_id}", channel_initial([]), None,
                                         extra=extra.replace("MEDIAHOST", self.media_host)))
        if segs[:1] == ["@nobody"]:
            return self._send(b"<html><body>This channel doesn\xe2\x80\x99t exist.</body></html>")
        if segs[:1] == ["post"] and len(segs) == 2:
            renderer = next((p for p in POSTS + MORE_POSTS if p["postId"] == segs[1]), None)
            if renderer is None:
                return self._send(b"<html><body>This page isn't available.</body></html>", status=404)
            initial = channel_initial([thread_item(renderer)])
            initial["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["content"][
                "sectionListRenderer"]["contents"].append({"itemSectionRenderer": {
                    "sectionIdentifier": "comment-item-section",
                    "contents": [continuation("comments-1")]}})
            return self._send(self._page("Post", initial, None, "next", POST_PAGE_JS,
                                         "<button id='more-replies'>2 replies</button>"))
        return self._send(b"not found", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        endpoint = urlsplit(self.path).path.rsplit("/", 1)[-1]
        token = body.get("continuation")
        if endpoint == "browse" and token == "posts-page-2":
            items = [thread_item(p) for p in MORE_POSTS]
            answer = {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": items}}]}
        elif endpoint == "next" and token == "comments-1":
            answer = comments_response(ROOT_COMMENTS, "replies-A")
        elif endpoint == "next" and token == "replies-A":
            answer = comments_response(REPLIES, None, level=1)
        else:
            answer = {"onResponseReceivedEndpoints": []}
        return self._send(json.dumps(self._fix(answer)).encode(), "application/json")


class MediaHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = b"\x89PNG\r\n\x1a\n" + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


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
