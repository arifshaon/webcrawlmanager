"""A small site in the shape of Instagram's web client, for tests.

A profile page carries its first posts as JSON embedded in the HTML and
fetches the next page over /graphql/query when scrolled to the bottom; a
post page carries the post and its first comments the same way and fetches
more comments on scroll. The shapes follow Instagram's current web payloads
(pk/code/taken_at/image_versions2/carousel_media, comments as pk/text/
created_at/user), and the profile lists a pinned post first.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROFILE = {"pk": "100", "username": "qnl", "full_name": "Qatar National Library",
           "biography": "Books.", "is_private": False, "is_verified": True,
           "follower_count": 1200, "following_count": 5, "media_count": 12,
           "profile_pic_url_hd": "http://HOST/pic/avatar.jpg"}


def node(n: int, taken_at: int, kind: str = "image", user: str = "qnl") -> dict:
    code = f"C{n:05d}ab"
    base = {"pk": str(1000 + n), "id": f"{1000 + n}_100", "code": code,
            "taken_at": taken_at, "media_type": 1,
            "caption": {"text": f"Caption {n}"},
            "like_count": n, "comment_count": 3 if n == 1 else 0,
            "user": {"pk": "100", "username": user}}
    if kind == "carousel":
        base["media_type"] = 8
        base["carousel_media"] = [
            {"pk": f"{1000 + n}_{i}", "media_type": 1,
             "image_versions2": {"candidates": [
                 {"url": f"http://HOST/pic/{code}-{i}-s.jpg", "width": 320, "height": 320},
                 {"url": f"http://HOST/pic/{code}-{i}-l.jpg", "width": 1080, "height": 1080}]}}
            for i in range(3)]
    elif kind == "reel":
        base["media_type"] = 2
        base["product_type"] = "clips"
        base["play_count"] = 999
        base["video_versions"] = [{"url": f"http://HOST/pic/{code}.mp4", "width": 720, "height": 1280}]
        base["image_versions2"] = {"candidates": [{"url": f"http://HOST/pic/{code}-poster.jpg", "width": 720, "height": 1280}]}
    else:
        base["image_versions2"] = {"candidates": [
            {"url": f"http://HOST/pic/{code}-s.jpg", "width": 320, "height": 320},
            {"url": f"http://HOST/pic/{code}-l.jpg", "width": 1080, "height": 1080}]}
    return base


# pinned old post first, then newest-first; 12 posts over two pages
PINNED = node(50, 1_600_000_000)
TIMELINE = [PINNED] + [node(n, 1_700_000_000 - n * 86400,
                            "carousel" if n == 1 else "reel" if n == 2 else "image")
                       for n in range(1, 12)]
PAGE_SIZE = 6

COMMENTS = [
    {"pk": "9001", "text": "Lovely", "created_at": 1_700_000_100,
     "user": {"pk": "7", "username": "reader"}, "comment_like_count": 2,
     "child_comment_count": 1,
     "preview_child_comments": [
         {"pk": "9001_1", "text": "Thank you", "created_at": 1_700_000_200,
          "user": {"pk": "100", "username": "qnl"}}]},
    {"pk": "9002", "text": "Beautiful", "created_at": 1_700_000_300,
     "user": {"pk": "8", "username": "other"}},
]
MORE_COMMENTS = [
    {"pk": "9003", "text": "Third comment", "created_at": 1_700_000_400,
     "user": {"pk": "9", "username": "late"}},
]


def _page(title: str, payload: dict, script: str) -> bytes:
    return (f"<!doctype html><html><head><title>{title}</title></head><body>"
            f"<h1>{title}</h1><div style='height:2400px'>scroll me</div>"
            f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'
            f"<script>{script}</script></body></html>").encode()


SCROLL_JS = """
let cursor = %(cursor)s; let busy = false;
window.addEventListener('scroll', async () => {
  if (busy || cursor === null) return;
  if (window.innerHeight + window.scrollY < document.body.scrollHeight - 50) return;
  busy = true;
  const body = new URLSearchParams({fb_api_req_friendly_name: '%(name)s',
    variables: JSON.stringify({after: cursor})}).toString();
  const r = await fetch('/graphql/query', {method: 'POST', body,
    headers: {'content-type': 'application/x-www-form-urlencoded'}});
  const j = await r.json();
  cursor = j.next || null;
  document.body.insertAdjacentHTML('beforeend', "<div style='height:1200px'>more</div>");
  busy = false;
});
"""


class Handler(BaseHTTPRequestHandler):
    host = "127.0.0.1:0"

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fix(self, obj):
        return json.loads(json.dumps(obj).replace("HOST", self.host))

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        if segs[:1] == ["qnl"] and len(segs) == 1:
            first = self._fix(TIMELINE[:PAGE_SIZE])
            payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
                ["RelayPrefetchedStreamCache", "next", [], ["q", {"__bbox": {"result": {"data": {
                    "user": self._fix(PROFILE),
                    "xdt_api__v1__feed__user_timeline_graphql_connection": {
                        "edges": [{"node": n} for n in first],
                        "page_info": {"has_next_page": True, "end_cursor": "c1"}}}}}}]]]}}]]]}
            return self._send(_page("qnl", payload, SCROLL_JS % {
                "cursor": json.dumps("c1"), "name": "PolarisProfilePostsTabContentQuery_connection"}))
        if segs[:1] == ["qnl"] and segs[1:2] == ["reels"]:
            payload = {"data": {"xdt_api__v1__clips__user__connection_v2": {
                "edges": [{"node": {"media": self._fix(TIMELINE[2])}}],
                "page_info": {"has_next_page": False}}}}
            return self._send(_page("reels", payload, SCROLL_JS % {"cursor": "null", "name": "x"}))
        if segs[:1] == ["p"] and len(segs) >= 2:
            code = segs[1]
            match = next((n for n in TIMELINE if n["code"] == code), None)
            if match is None:
                return self._send(b"<html><body>Sorry, this page isn't available.</body></html>", status=404)
            payload = {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [self._fix(match)]},
                       "xdt_api__v1__media__media_id__comments__connection": {
                           "edges": [{"node": c} for c in self._fix(COMMENTS)],
                           "page_info": {"has_next_page": True, "end_cursor": "k1"}}}}
            return self._send(_page(code, payload, SCROLL_JS % {
                "cursor": json.dumps("k1"), "name": "PolarisPostCommentsPaginationQuery"}))
        if segs[:1] == ["accounts"]:
            return self._send(b"<html><head><title>Login</title></head><body>Log in</body></html>")
        if segs[:1] == ["pic"]:
            name = segs[-1]
            if name.endswith(".mp4"):
                return self._send(b"\x00\x00\x00\x18ftypmp42" + name.encode(), "video/mp4")
            return self._send(b"\x89PNG\r\n\x1a\n" + name.encode(), "image/png")
        return self._send(b"not found", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        name = (form.get("fb_api_req_friendly_name") or [""])[0]
        variables = json.loads((form.get("variables") or ["{}"])[0])
        after = variables.get("after")
        if "Comments" in name:
            body = {"data": {"xdt_api__v1__media__media_id__comments__connection": {
                "edges": [{"node": c} for c in self._fix(MORE_COMMENTS)],
                "page_info": {"has_next_page": False}}}, "next": None}
        else:
            rest = self._fix(TIMELINE[PAGE_SIZE:])
            body = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n} for n in rest],
                "page_info": {"has_next_page": False}}}, "next": None}
        return self._send(json.dumps(body).encode(), "application/json")


def start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host = f"127.0.0.1:{server.server_address[1]}"
    Handler.host = host
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, host
