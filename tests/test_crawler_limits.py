"""Response size limiting.

resp.raw.read(amt, decode_content=True) bounds the COMPRESSED bytes read, not
the decoded output. Measured: a 51 KB gzip payload decoded to 52 MB straight
through a 3 MB cap, so a malicious or compromised source could force unbounded
allocation. The fetch now counts decoded bytes as they arrive.
"""
import unittest

try:
    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False


class _FakeResponse:
    """Stands in for a requests.Response. iter_content yields already-decoded
    bytes, which is what the real one does for a gzip-encoded body."""

    def __init__(self, chunks, status=200, content_type="text/html"):
        self._chunks = chunks
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class ResponseSizeLimitTests(unittest.TestCase):
    def setUp(self):
        self._saved = (crawler._is_ssrf_safe, crawler.is_allowed,
                       crawler._polite_wait, crawler._safe_get)
        crawler._is_ssrf_safe = lambda url: True
        crawler.is_allowed = lambda url: True
        crawler._polite_wait = lambda url: None

    def tearDown(self):
        (crawler._is_ssrf_safe, crawler.is_allowed,
         crawler._polite_wait, crawler._safe_get) = self._saved

    def _serve(self, chunks, **kw):
        self.response = _FakeResponse(chunks, **kw)
        crawler._safe_get = lambda *a, **k: (self.response, None)
        return crawler._fetch_raw("https://example.test/page")

    def test_a_body_within_the_cap_is_accepted(self):
        result = self._serve([b"x" * 1000, b"y" * 1000])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.content), 2000)
        self.assertTrue(self.response.closed)

    def test_decoded_bytes_over_the_cap_are_refused(self):
        # A decompression bomb arrives as many ordinary-looking decoded chunks.
        oversize = [b"a" * 1_000_000] * 60      # 60 MB decoded
        result = self._serve(oversize)
        self.assertFalse(result.ok)
        self.assertIn("limit", result.error.lower())

    def test_an_oversize_body_is_abandoned_not_buffered_whole(self):
        # The generator must stop being consumed once the cap is passed, rather
        # than the whole bomb being read and only then rejected.
        consumed = {"chunks": 0}

        def counting_chunks():
            for _ in range(10_000):
                consumed["chunks"] += 1
                yield b"a" * 1_000_000        # 10 GB if fully consumed

        self.response = _FakeResponse(None)
        self.response.iter_content = lambda chunk_size=65536: counting_chunks()
        crawler._safe_get = lambda *a, **k: (self.response, None)
        result = crawler._fetch_raw("https://example.test/page")

        self.assertFalse(result.ok)
        self.assertLessEqual(consumed["chunks"], (crawler.MAX_CONTENT_BYTES // 1_000_000) + 2)
        self.assertTrue(self.response.closed)

    def test_the_connection_is_closed_when_the_body_errors(self):
        import requests

        def exploding():
            yield b"partial"
            raise requests.ConnectionError("stream died")

        self.response = _FakeResponse(None)
        self.response.iter_content = lambda chunk_size=65536: exploding()
        crawler._safe_get = lambda *a, **k: (self.response, None)
        result = crawler._fetch_raw("https://example.test/page")

        self.assertFalse(result.ok)
        self.assertTrue(self.response.closed, "a failed read must not leak the connection")

    def test_an_http_error_is_reported_after_the_body_is_drained(self):
        result = self._serve([b"not found"], status=404)
        self.assertFalse(result.ok)
        self.assertIn("404", result.error)


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class RobotsHostTests(unittest.TestCase):
    def test_userinfo_is_stripped_from_the_robots_host(self):
        import urllib.parse
        # robots.txt must not be fetched with credentials in the URL, and the
        # robots and crawl-delay caches must not treat these as two sites.
        with_creds = urllib.parse.urlparse("https://user:pass@example.test/a")
        plain = urllib.parse.urlparse("https://example.test/a")
        self.assertEqual(crawler._robots_host(with_creds), "example.test")
        self.assertEqual(crawler._robots_host(with_creds), crawler._robots_host(plain))

    def test_port_is_kept(self):
        import urllib.parse
        self.assertEqual(
            crawler._robots_host(urllib.parse.urlparse("https://example.test:8443/a")),
            "example.test:8443",
        )

    def test_ipv6_literal_keeps_its_brackets(self):
        import urllib.parse
        self.assertEqual(
            crawler._robots_host(urllib.parse.urlparse("https://[2001:db8::1]:8443/a")),
            "[2001:db8::1]:8443",
        )


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class RobotsFetchLimitTests(unittest.TestCase):
    """robots.txt is the first request made to any new host, so it must be
    bounded before anything else trusts that host. It previously used a
    non-streaming fetch and resp.text, which materialised the whole body: a
    38 MB robots.txt was accepted and parsed."""

    def setUp(self):
        self._safe_get = crawler._safe_get
        crawler._robots_cache.clear()

    def tearDown(self):
        crawler._safe_get = self._safe_get
        crawler._robots_cache.clear()

    def _serve(self, chunks, status=200):
        self.response = _FakeResponse(chunks, status=status)
        crawler._safe_get = lambda *a, **k: (self.response, None)
        return crawler._get_robot_parser("limits.test", "https")

    def test_a_normal_robots_file_is_parsed(self):
        rp = self._serve([b"User-agent: *\nDisallow: /private\n"])
        self.assertTrue(rp.can_fetch(crawler.USER_AGENT, "https://limits.test/news/a"))
        self.assertFalse(rp.can_fetch(crawler.USER_AGENT, "https://limits.test/private/x"))

    def test_an_oversized_robots_file_fails_closed(self):
        # Not a licence to crawl: an unreadable robots.txt must deny, like an
        # unreachable one, rather than falling through to allow.
        oversize = [b"#" + b"a" * 100_000] * 20          # ~2 MB
        rp = self._serve(oversize)
        self.assertFalse(rp.can_fetch(crawler.USER_AGENT, "https://limits.test/anything"))
        self.assertTrue(self.response.closed)

    def test_the_body_is_abandoned_rather_than_fully_read(self):
        consumed = {"n": 0}

        def endless():
            for _ in range(10_000):
                consumed["n"] += 1
                yield b"#" + b"a" * 100_000

        self.response = _FakeResponse(None)
        self.response.iter_content = lambda chunk_size=65536: endless()
        crawler._safe_get = lambda *a, **k: (self.response, None)
        crawler._get_robot_parser("abandon.test", "https")
        self.assertLess(consumed["n"], 20, "the whole body was read before rejecting it")

    def test_a_stream_error_fails_closed(self):
        import requests

        def exploding():
            yield b"User-agent: *\n"
            raise requests.ConnectionError("died")

        self.response = _FakeResponse(None)
        self.response.iter_content = lambda chunk_size=65536: exploding()
        crawler._safe_get = lambda *a, **k: (self.response, None)
        rp = crawler._get_robot_parser("broken.test", "https")
        self.assertFalse(rp.can_fetch(crawler.USER_AGENT, "https://broken.test/x"))
        self.assertTrue(self.response.closed)

    def test_unreachable_robots_still_fails_closed(self):
        crawler._safe_get = lambda *a, **k: (None, "boom")
        rp = crawler._get_robot_parser("gone.test", "https")
        self.assertFalse(rp.can_fetch(crawler.USER_AGENT, "https://gone.test/x"))

    def test_404_robots_allows_crawling(self):
        # A site with no robots.txt has not disallowed anything.
        rp = self._serve([b"not found"], status=404)
        self.assertTrue(rp.can_fetch(crawler.USER_AGENT, "https://limits.test/x"))

    def test_401_and_403_fail_closed(self):
        for status in (401, 403):
            with self.subTest(status=status):
                crawler._robots_cache.clear()
                rp = self._serve([b"denied"], status=status)
                self.assertFalse(rp.can_fetch(crawler.USER_AGENT, "https://limits.test/x"))


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class BodyReadTimeoutTests(unittest.TestCase):
    def test_a_slow_trickle_is_cut_off(self):
        # requests' timeout is per socket operation, so a server sending a byte
        # at a time never trips it. The body gets its own wall-clock bound.
        import time

        def trickle():
            for _ in range(1000):
                time.sleep(0.01)
                yield b"a"

        resp = _FakeResponse(None)
        resp.iter_content = lambda chunk_size=65536: trickle()
        content, err = crawler._read_capped(resp, crawler.MAX_CONTENT_BYTES, max_seconds=0.05)
        self.assertIsNone(content)
        self.assertIn("longer than", err)

    def test_a_prompt_body_is_returned_whole(self):
        resp = _FakeResponse([b"abc", b"def"])
        content, err = crawler._read_capped(resp, crawler.MAX_CONTENT_BYTES)
        self.assertEqual(content, b"abcdef")
        self.assertIsNone(err)

if __name__ == "__main__":
    unittest.main()
