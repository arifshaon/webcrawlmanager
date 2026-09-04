"""A small site in the shape of Instagram's web client, for tests.

A profile page carries its first posts as JSON embedded in the HTML and
fetches the next page over /graphql/query when scrolled to the bottom; a
post page carries the post and its first comments the same way and fetches
more comments on scroll. The shapes follow Instagram's current web payloads
(pk/code/taken_at/image_versions2/carousel_media, comments as pk/text/
created_at/user), and the profile lists a pinned post first. A second
profile exists so a run over two targets can be checked for isolation, and
the first profile's page carries a suggested post by someone else, as
Instagram's pages do, which is not the profile's own.
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


def node(n: int, taken_at: int, kind: str = "image", user: str = "qnl",
         user_pk: str = "100") -> dict:
    code = f"C{n:05d}ab" if user == "qnl" else f"D{n:05d}{user[:2]}"
    # the caption is shaped like a comment (pk, text, created_at) but names
    # no author, as Instagram's is
    base = {"pk": str(1000 + n), "id": f"{1000 + n}_{user_pk}", "code": code,
            "taken_at": taken_at, "media_type": 1,
            "caption": {"pk": f"cap{n}", "text": f"Caption {n}", "created_at": taken_at},
            "like_count": n, "comment_count": 3 if n == 1 else 0,
            "user": {"pk": user_pk, "username": user}}
    if kind == "carousel":
        base["media_type"] = 8
        base["product_type"] = "carousel_container"
        # components carry their own code, id and time, as Instagram's do;
        # their files live on a CDN host that, like Instagram's, lets no
        # page on the site read them cross-origin
        base["carousel_media"] = [
            {"pk": f"{1000 + n}0{i}", "id": f"{1000 + n}0{i}_{user_pk}",
             "code": f"{code}x{i}", "carousel_parent_id": str(1000 + n),
             "taken_at": taken_at, "media_type": 1,
             "user": {"pk": user_pk, "username": user},
             "image_versions2": {"candidates": [
                 {"url": f"http://CDNHOST/pic/{code}-{i}-s.jpg", "width": 320, "height": 320},
                 {"url": f"http://CDNHOST/pic/{code}-{i}-l.jpg", "width": 1080, "height": 1080}]}}
            for i in range(3)]
    elif kind == "reel":
        base["media_type"] = 2
        base["product_type"] = "clips"
        base["play_count"] = 999
        # the reel's files sit on the media host under a path it lets no
        # page read at all
        base["video_versions"] = [{"url": f"http://CDNHOST/sealed/{code}.mp4", "width": 720, "height": 1280}]
        base["image_versions2"] = {"candidates": [{"url": f"http://CDNHOST/sealed/{code}-poster.jpg", "width": 720, "height": 1280}]}
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

# a second profile with its own posts, none shared with the first
PROFILE_B = {"pk": "200", "username": "qbl", "full_name": "Qatar Ballers",
             "biography": "Hoops.", "is_private": False, "is_verified": False,
             "follower_count": 300, "following_count": 9, "media_count": 4,
             "profile_pic_url_hd": "http://HOST/pic/avatar-b.jpg"}
TIMELINE_B = [node(n, 1_690_000_000 - n * 86400, user="qbl", user_pk="200")
              for n in range(1, 5)]
# what Instagram shows on a profile page that is not the profile's own
SUGGESTED = node(77, 1_695_000_000, user="someone_else", user_pk="777")

# a profile whose post nodes name no owner, as some payload shapes do not
PROFILE_C = {"pk": "300", "username": "noname", "full_name": "No Name",
             "is_private": False, "follower_count": 1, "following_count": 1,
             "media_count": 3}
TIMELINE_C = [{k: v for k, v in node(n, 1_680_000_000 - n * 86400,
                                     user="noname", user_pk="300").items()
               if k != "user"} for n in range(21, 24)]

PROFILES = {"qnl": (PROFILE, TIMELINE, "c1"), "qbl": (PROFILE_B, TIMELINE_B, None),
            "noname": (PROFILE_C, TIMELINE_C, None)}

# what a signed-in page preloads besides the profile: the viewer's own feed,
# with posts whose owner is given by id alone
VIEWER_POST = {**node(88, 1_698_000_000, user="viewer", user_pk="424242"),
               "user": {"pk": "424242"}}
STRANGER_POST = {k: v for k, v in node(89, 1_698_100_000, user="stranger",
                                       user_pk="555").items() if k != "user"}
STRANGER_POST["owner"] = {"id": "555"}
# the profile's own post shown in the viewer's feed: the profile's, but not
# the profile's listing -- only the listing decides
OWN_POST_ELSEWHERE = node(99, 1_698_200_000)
VIEWER_FEED = [VIEWER_POST, STRANGER_POST, OWN_POST_ELSEWHERE]


def _prefetch(query: str, data: dict) -> dict:
    """How the page embeds a prefetched query's answer, keyed by its name."""
    return {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
        ["RelayPrefetchedStreamCache", "next", [],
         [f"adp_{query}relayprovider_0", {"__bbox": {"result": {"data": data}}}]]]}}]]]}

# what the route carries beside the post: its parameters name the shortcode
# and a caption parameter, and are not a post
ROUTE_PARAMS = {"shortcode": "PLACEHOLDER", "caption": "", "username": "qnl",
                "img_index": "1", "igsh": "abc"}

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
    # a reply loaded on its own names its parent by field, not by nesting
    {"pk": "9003_1", "text": "Reply by field", "created_at": 1_700_000_500,
     "parent_comment_id": "9003", "user": {"pk": "9", "username": "late"}},
]


def _page(title: str, payload: dict, script: str, extra_html: str = "") -> bytes:
    return (f"<!doctype html><html><head><title>{title}</title></head><body>"
            f"<h1>{title}</h1><div style='height:2400px'>scroll me</div>{extra_html}"
            f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'
            f"<script>{script}</script></body></html>").encode()


# a page that also prefetches another profile's listing, as the real one
# does for a profile the viewer hovers or is suggested
PREFETCH_JS = """
fetch('/graphql/query', {method: 'POST', headers: {'content-type': 'application/x-www-form-urlencoded'},
  body: new URLSearchParams({fb_api_req_friendly_name: 'PolarisProfilePostsQuery',
    variables: JSON.stringify({username: 'qbl', data: {count: 12}})}).toString()});
"""

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
    cdn_host = "127.0.0.1:0"
    cors_media = True          # the site lets its own pages read /pic

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status=200,
              cors: "bool | str" = False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cors == "echo":
            # as Instagram's image host answers a page: its origin named,
            # credentials never allowed
            origin = self.headers.get("Origin")
            self.send_header("Access-Control-Allow-Origin", origin or "*")
            self.send_header("Vary", "Origin")
        elif cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _fix(self, obj):
        return json.loads(json.dumps(obj).replace("CDNHOST", self.cdn_host)
                          .replace("HOST", self.host))

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            # the home page: a signed-in browser sees it, a signed-out one
            # is sent to sign in, as Instagram does
            if "sessionid=" in (self.headers.get("Cookie") or ""):
                return self._send(b"<html><head><title>Instagram</title></head>"
                                  b"<body>Home</body></html>")
            self.send_response(302)
            self.send_header("Location", "/accounts/login/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if len(segs) == 1 and segs[0] in PROFILES:
            profile, timeline, cursor = PROFILES[segs[0]]
            first = self._fix(timeline[:PAGE_SIZE])
            data = {"user": self._fix(profile),
                    "xdt_api__v1__feed__user_timeline_graphql_connection": {
                        "edges": [{"node": n} for n in first],
                        "page_info": {"has_next_page": cursor is not None,
                                      "end_cursor": cursor}}}
            if segs[0] == "qnl":
                data["suggested_posts"] = [{"node": self._fix(SUGGESTED)}]
                data["xdt_api__v1__feed__timeline"] = {
                    "edges": [{"node": self._fix(n)} for n in VIEWER_FEED]}
            payload = _prefetch("PolarisProfilePostsTabContentQuery_connection", data)
            script = SCROLL_JS % {
                "cursor": json.dumps(cursor), "name": "PolarisProfilePostsTabContentQuery_connection"}
            if segs[0] == "qnl":
                script = PREFETCH_JS + script
            return self._send(_page(segs[0], payload, script))
        if segs[:1] == ["qnl"] and segs[1:2] == ["reels"]:
            payload = _prefetch("PolarisProfileReelsTabContentQuery_connection", {
                "xdt_api__v1__clips__user__connection_v2": {
                    "edges": [{"node": {"media": self._fix(TIMELINE[2])}}],
                    "page_info": {"has_next_page": False}}})
            return self._send(_page("reels", payload, SCROLL_JS % {"cursor": "null", "name": "x"}))
        if segs[:1] == ["p"] and len(segs) >= 2:
            code = segs[1]
            match = next((n for n in TIMELINE + TIMELINE_B + TIMELINE_C
                          if n["code"] == code), None)
            if match is None:
                return self._send(b"<html><body>Sorry, this page isn't available.</body></html>", status=404)
            # the post, its first comments, and -- as Instagram's post pages
            # do -- more posts from the same account, with their thumbnails
            others = [n for n in TIMELINE if n["code"] != code][:3]
            payload = {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [self._fix(match)]},
                       "xdt_api__v1__media__media_id__comments__connection": {
                           "edges": [{"node": c} for c in self._fix(COMMENTS)],
                           "page_info": {"has_next_page": True, "end_cursor": "k1"}},
                       "xdt_api__v1__profile_timeline": {
                           "profile_grid_items": [{"media": n} for n in self._fix(others)]},
                       "initialRouteInfo": {"route": {"params": {**ROUTE_PARAMS, "shortcode": code}}}}}
            thumbnails = "".join(
                f"<img src='http://{self.host}/pic/{n['code']}-s.jpg' alt=''>"
                for n in others)
            return self._send(_page(code, payload, SCROLL_JS % {
                "cursor": json.dumps("k1"), "name": "PolarisPostCommentsPaginationQuery"},
                thumbnails))
        if segs[:1] == ["accounts"]:
            return self._send(b"<html><head><title>Login</title></head><body>Log in</body></html>")
        if segs[:1] == ["pic"]:
            name = segs[-1]
            if name.endswith(".mp4"):
                return self._send(b"\x00\x00\x00\x18ftypmp42" + name.encode(), "video/mp4",
                                  cors=self.cors_media)
            return self._send(b"\x89PNG\r\n\x1a\n" + name.encode(), "image/png",
                              cors=self.cors_media)
        return self._send(b"not found", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        name = (form.get("fb_api_req_friendly_name") or [""])[0]
        variables = json.loads((form.get("variables") or ["{}"])[0])
        after = variables.get("after")
        if variables.get("username") and variables["username"] != "qnl":
            # another profile's listing, prefetched: its nodes name no owner
            _, timeline, _ = PROFILES[variables["username"]]
            stripped = [{k: v for k, v in n.items() if k != "user"} for n in timeline]
            body = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n} for n in self._fix(stripped)],
                "page_info": {"has_next_page": False}}}}
        elif "Comments" in name:
            body = {"data": {"xdt_api__v1__media__media_id__comments__connection": {
                "edges": [{"node": c} for c in self._fix(MORE_COMMENTS)],
                "page_info": {"has_next_page": False}}}, "next": None}
        else:
            rest = self._fix(TIMELINE[PAGE_SIZE:])
            body = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n} for n in rest],
                "page_info": {"has_next_page": False}}}, "next": None}
        return self._send(json.dumps(body).encode(), "application/json")


class CdnHandler(Handler):
    """Instagram's media host, as it really answers: a page's read of a
    file is allowed for its origin but never with credentials; a sealed
    path allows no page to read at all; its root answers 204 No Content,
    which commits no document; any other path a small 403 page."""
    cors_media = "echo"

    def do_GET(self):
        segs = [s for s in self.path.split("/") if s]
        if segs[:1] == ["pic"]:
            return super().do_GET()
        if segs[:1] == ["sealed"] and len(segs) == 2:
            name = segs[-1]
            if name.endswith(".mp4"):
                return self._send(b"\x00\x00\x00\x18ftypmp42" + name.encode(), "video/mp4")
            return self._send(b"\x89PNG\r\n\x1a\n" + name.encode(), "image/png")
        if not segs:
            self.send_response(204)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            return None
        return self._send(b"Bad URL hash", "text/plain", status=403)


class _Site(ThreadingHTTPServer):
    cdn: "ThreadingHTTPServer | None" = None

    def shutdown(self):
        super().shutdown()
        if self.cdn is not None:
            self.cdn.shutdown()


def start() -> tuple[ThreadingHTTPServer, str]:
    server = _Site(("127.0.0.1", 0), Handler)
    host = f"127.0.0.1:{server.server_address[1]}"
    Handler.host = host
    cdn = ThreadingHTTPServer(("127.0.0.1", 0), CdnHandler)
    Handler.cdn_host = f"127.0.0.1:{cdn.server_address[1]}"
    server.cdn = cdn
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=cdn.serve_forever, daemon=True).start()
    return server, host
