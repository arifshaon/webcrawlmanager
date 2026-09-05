"""Session material stays out of preserved bodies."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from webarc.redaction import REDACTED, redact_body

BOOTSTRAP = (
    b'<script id="__eqmc" type="application/json">'
    b'{"f":"NASECRET:17843683195144578:1788449426","u":"/?__user=42&jazoest=999"}'
    b'</script>'
    b'["DTSGInitialData",[],{"token":"NASECRET:17843683195144578:1788449426"}]'
    b'["LSD",[],{"token":"lsd-secret"}]'
    b'{"IG_USER_EIMU":"12345","ACCOUNT_ID":"67890","sessionID":"33604a746e370d66"}'
    b'{"data":{"user":{"pk":"4267196155","username":"qatarballers"},"caption":{"text":"kept"}}}'
)


class RedactBodyTests(unittest.TestCase):
    def test_bootstrap_tokens_and_the_capturing_accounts_ids_are_removed(self):
        safe, fields = redact_body(BOOTSTRAP, "text/html")
        text = safe.decode("utf-8")

        for secret in ("NASECRET", "lsd-secret", '"12345"', '"67890"',
                       "33604a746e370d66", "__user=42", "jazoest=999"):
            self.assertNotIn(secret, text)
        self.assertIn(REDACTED, text)
        self.assertIn("dtsg", fields)
        self.assertIn("lsd", fields)
        self.assertIn("navigation_session_id", fields)

    def test_the_targets_own_records_are_untouched(self):
        safe, _ = redact_body(BOOTSTRAP, "text/html")

        self.assertIn('"username":"qatarballers"', safe.decode("utf-8"))
        self.assertIn('"pk":"4267196155"', safe.decode("utf-8"))
        self.assertIn('"text":"kept"', safe.decode("utf-8"))

    def test_javascript_code_is_never_rewritten(self):
        """Minified bundles are full of names ending in a secret's key name
        followed by "=". Rewriting them breaks the page's client at replay:
        a capture whose bundles were touched showed nothing but the logo."""
        code = (b'l.getTabId=C,l.extend=b,l.getSessionId=function(){return h()};'
                b'n.getSessionId=function(){return this.$13};'
                b'var fb_dtsg="x",lsd=y;o.access_token=z;'
                b'c("upl_"+Date.now());u="?__a=1&lsd="+t+"&fb_dtsg="+d;')

        safe, fields = redact_body(code, "application/x-javascript; charset=utf-8")

        self.assertEqual(safe, code)
        self.assertEqual(fields, [])

    def test_a_secret_in_a_query_string_or_form_body_is_still_removed(self):
        for body, ctype in (
            (b'{"u":"/ajax/qm/?__a=1&__user=42&__comet_req=7&jazoest=999"}', "text/html"),
            (b'<a href="/x?lsd=abc&amp;fb_dtsg=def">', "text/html"),
            (b'fb_dtsg=abc&jazoest=1&lsd=def&other=kept', "application/x-www-form-urlencoded"),
        ):
            with self.subTest(body=body):
                safe, fields = redact_body(body, ctype)
                text = safe.decode("utf-8")
                for secret in ("=42", "=999", "=abc", "=def"):
                    self.assertNotIn(secret, text)
                self.assertTrue(fields)
        self.assertIn(b"other=kept", redact_body(
            b'fb_dtsg=abc&other=kept', "application/x-www-form-urlencoded")[0])

    def test_a_body_with_nothing_to_redact_is_returned_as_it_is(self):
        body = b'{"data": {"posts": []}}'
        safe, fields = redact_body(body, "application/json")

        self.assertIs(safe, body)
        self.assertEqual(fields, [])

    def test_media_passes_through_untouched(self):
        body = b"\x89PNG\r\n\x1a\n" + b"DTSGInitialData token"
        safe, fields = redact_body(body, "image/png")

        self.assertIs(safe, body)
        self.assertEqual(fields, [])


class PreservedBodiesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_raw_response_store_keeps_the_redacted_body_and_says_so(self):
        from webarc.instagram import InstagramArchive
        archive = InstagramArchive(self.tmp)

        ref = archive.save_response({"url": "https://www.instagram.com/p/x/",
                                     "content_type": "text/html"}, BOOTSTRAP)

        import json, hashlib
        saved = json.loads((self.tmp / ref).read_text(encoding="utf-8"))
        self.assertNotIn("NASECRET", saved["body"])
        self.assertIn("qatarballers", saved["body"])
        self.assertIn("dtsg", saved["redacted_fields"])
        self.assertEqual(saved["body_sha256"],
                         hashlib.sha256(saved["body"].encode("utf-8")).hexdigest())

    def test_the_warc_body_is_redacted_and_the_record_says_so(self):
        from warcio.archiveiterator import ArchiveIterator
        from webarc.config import WarcConfig
        from webarc.facebook import FacebookWarcSession

        warc = FacebookWarcSession(self.tmp, "ig", "https://www.instagram.com/x/", 1,
                                   "webarc", WarcConfig())
        warc.write_exchange(url="https://www.instagram.com/p/x/", method="GET",
                            req_headers={}, post_data=None, status=200,
                            status_text="OK",
                            resp_headers={"content-type": "text/html"},
                            body=BOOTSTRAP)
        warc.close()

        with next(self.tmp.glob("*.warc.gz")).open("rb") as handle:
            for record in ArchiveIterator(handle):
                if record.rec_type != "response":
                    continue
                body = record.content_stream().read().decode("utf-8")
                note = record.http_headers.get_header("X-SWM-Redacted")
                break
        self.assertNotIn("NASECRET", body)
        self.assertIn("qatarballers", body)
        self.assertIn("session material removed", note)


if __name__ == "__main__":
    unittest.main()
