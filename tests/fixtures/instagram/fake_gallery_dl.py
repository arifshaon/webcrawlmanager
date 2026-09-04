"""A stand-in for the gallery-dl program, for the streaming tests.

Run as ``python -m tests.fixtures.instagram.fake_gallery_dl <gallery-dl args>``.
It writes the recorded gallery-dl JSON Lines fixture to stdout the way the
real program does with output.jsonl on -- one message per line, flushed --
and logs a cursor to stderr after each post as the real Instagram extractor
does at debug level. Environment variables shape the run:

FAKE_GALLERY_DL_DELAY      seconds to sleep before each post (streaming proof)
FAKE_GALLERY_DL_FAIL_AFTER fail with a 429 after this many posts, once
FAKE_GALLERY_DL_FAIL_FLAG  a file whose absence means "fail this run"; it is
                           created after failing so the resumed run succeeds
FAKE_GALLERY_DL_REJECT_SESSION
                           a sessionid value the lent cookie file must not
                           carry: with it, the run answers 401 like Instagram
FAKE_GALLERY_DL_REQUIRE_COOKIE
                           a cookie name the lent file must carry: without
                           it, the run is answered as signed out the way
                           Instagram really answers -- a redirect to the
                           home page, no output, exit 0
"""
import json
import os
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).with_name("gallery-dl-posts.jsonl")


def main(argv):
    options = {}
    for index, arg in enumerate(argv):
        if arg == "-o" and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition("=")
            options[key] = value
    assert "--config-ignore" in argv, "the real program would read the user's config"
    assert options.get("output.jsonl") == "true", "only JSON Lines streams"
    cursor = int(options.get("cursor") or 0)
    max_posts = int(options.get("extractor.instagram.max-posts") or 0)
    delay = float(os.environ.get("FAKE_GALLERY_DL_DELAY") or 0)
    fail_after = int(os.environ.get("FAKE_GALLERY_DL_FAIL_AFTER") or 0)
    flag = os.environ.get("FAKE_GALLERY_DL_FAIL_FLAG")
    should_fail = bool(fail_after) and (not flag or not Path(flag).exists())

    required = os.environ.get("FAKE_GALLERY_DL_REQUIRE_COOKIE")
    if required and "-C" in argv:
        cookie_file = Path(argv[argv.index("-C") + 1])
        names = {line.split("\t")[5] for line in cookie_file.read_text(encoding="utf-8").splitlines()
                 if len(line.split("\t")) == 7}
        if required not in names:
            sys.stderr.write("[urllib3.connectionpool][debug] https://www.instagram.com:443 "
                             "\"GET /api/v1/feed/user/1/?count=30 HTTP/1.1\" 302 0\n")
            sys.stderr.write("[urllib3.connectionpool][debug] https://www.instagram.com:443 "
                             "\"GET / HTTP/1.1\" 200 None\n")
            sys.stderr.flush()
            return 0
    rejected = os.environ.get("FAKE_GALLERY_DL_REJECT_SESSION")
    if rejected and "-C" in argv:
        cookie_file = Path(argv[argv.index("-C") + 1])
        for line in cookie_file.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 7 and fields[5] == "sessionid" and fields[6] == rejected:
                sys.stderr.write("[instagram][error] HttpError: '401 Unauthorized' "
                                 "for 'https://www.instagram.com/api/v1/feed/user/1/' "
                                 "(login required)\n")
                sys.stderr.flush()
                return 1

    posts_seen = 0
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        message = json.loads(line)
        if message[0] == 2:
            posts_seen += 1
            if posts_seen <= cursor:
                continue
            if max_posts and posts_seen - cursor > max_posts:
                break
            if should_fail and posts_seen - cursor > fail_after:
                if flag:
                    Path(flag).write_text("failed once")
                sys.stderr.write("[instagram][error] HttpError: '429 Too Many Requests' for "
                                 "'https://www.instagram.com/api/v1/feed/user/1/'\n")
                sys.stderr.write(f"[instagram][info] Use '-o cursor={posts_seen - 1}' to continue "
                                 "downloading from the current position\n")
                sys.stderr.flush()
                return 1
            if delay:
                time.sleep(delay)
            sys.stderr.write(f"[instagram][debug] Cursor: {posts_seen}\n")
            sys.stderr.flush()
        elif posts_seen <= cursor:
            continue
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
