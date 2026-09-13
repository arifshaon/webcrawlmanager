"""A small news site for the theme tests: hubs, articles in two languages,
a borderline page, a log-in page and a PDF, every page wearing the same
navigation so that menu text can be told from article text."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAV = ("<nav><a href='/'>Home</a> <a href='/culture/'>Culture</a> <a href='/sport/'>Sport</a> "
       "<a href='/about'>About</a> <a href='/login'>Library card log-in</a></nav>"
       "<header><h2>The Daily Fixture — Library card holders read free</h2></header>")
FOOTER = "<footer>© The Daily Fixture · <a href='/about'>About</a></footer>"


def page(title: str, body: str, *, lang: str = "en", section: str = "", published: str = "",
         tags: tuple[str, ...] = ()) -> bytes:
    meta = ""
    if section:
        meta += f"<meta property='article:section' content='{section}'>"
    if published:
        meta += f"<meta property='article:published_time' content='{published}'>"
    for tag in tags:
        meta += f"<meta property='article:tag' content='{tag}'>"
    return (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'><title>{title}</title>{meta}"
            f"</head><body>{NAV}{body}{FOOTER}</body></html>").encode("utf-8")


def article(h1: str, paragraphs: list[str], **kw) -> bytes:
    body = "<article><h1>" + h1 + "</h1>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</article>"
    return page(h1, body, **kw)


ARTICLES = {
    "/news/1-library-opens": article(
        "New public library opens in Doha",
        ["The city opened a new public library on Monday, with a reading room for children.",
         "The library will lend books in Arabic and English and host talks every week."],
        section="Culture", published="2026-09-01T09:00:00+03:00", tags=("libraries",)),
    "/news/2-football-final": article(
        "Football cup final ends in penalties",
        ["The final went to penalties after a goalless draw.",
         "Supporters filled the stadium two hours before kick-off."],
        section="Sport", published="2026-09-02T21:00:00+03:00"),
    "/news/3-maktaba": article(
        "افتتاح مكتبة عامة جديدة في الدوحة",
        ["افتتحت المدينة المكتبة العامة الجديدة يوم الاثنين، وتضم قاعة قراءة للأطفال.",
         "وستعير المكتبات الفرعية الكتب بالعربية والإنجليزية."],
        lang="ar", section="ثقافة", published="2026-09-03T09:00:00+03:00"),
    "/news/4-city-budget": article(
        "Council approves next year's budget",
        ["The council approved the budget after a long session.",
         "Spending on roads rises; the library budget is unchanged."],
        section="Politics", published="2026-09-04T09:00:00+03:00"),
    "/news/5-old-library-story": article(
        "Library extension planned",
        ["An extension to the central library was announced years ago."],
        section="Culture", published="2019-05-01T09:00:00+03:00"),
}

HUBS = {
    "/": page("The Daily Fixture", "<main><h1>Latest</h1><ul>"
              "<li><a href='/news/1-library-opens'>New public library opens in Doha</a> — reading room for children</li>"
              "<li><a href='/news/2-football-final'>Football cup final ends in penalties</a> — sport report</li>"
              "<li><a href='/news/3-maktaba'>افتتاح مكتبة عامة جديدة</a></li>"
              "<li><a href='/news/4-city-budget'>Council approves budget</a> — roads and services</li>"
              "<li><a href='/news/5-old-library-story'>Library extension planned</a> — from the archive</li>"
              "<li><a href='/files/report.pdf'>Annual report (PDF)</a></li></ul></main>"),
    "/culture/": page("Culture", "<main><h1>Culture</h1><ul>"
                      "<li><a href='/news/1-library-opens'>New public library opens</a></li>"
                      "<li><a href='/news/3-maktaba'>افتتاح مكتبة</a></li>"
                      "<li><a href='/news/5-old-library-story'>Library extension planned</a></li></ul></main>"),
    "/sport/": page("Sport", "<main><h1>Sport</h1><ul>"
                    "<li><a href='/news/2-football-final'>Cup final</a></li></ul></main>"),
    "/about": page("About", "<main><h1>About us</h1><p>The Daily Fixture is a test newspaper.</p></main>"),
    "/login": page("Log in", "<main><h1>Log in</h1><form><input name='user'></form></main>"),
}


class Handler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        type(self).requests.append(path)
        if path == "/files/report.pdf":
            body = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
            ctype = "application/pdf"
        elif path in ARTICLES:
            body, ctype = ARTICLES[path], "text/html; charset=utf-8"
        elif path in HUBS:
            body, ctype = HUBS[path], "text/html; charset=utf-8"
        elif path == "/robots.txt":
            body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
        else:
            body, ctype = b"not found", "text/plain"
            self.send_response(404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
