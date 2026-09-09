"""Feed parsing tests: HTML in descriptions, and link safety.

Needs the third-party dependencies (BeautifulSoup, defusedxml), so it skips
cleanly on a bare clone like the other crawler tests.
"""
import unittest

try:
    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False

RSS_WITH_MARKUP = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Budget approved after debate</title>
    <description>&lt;p&gt;The council &lt;a href="http://x.test"&gt;approved&lt;/a&gt; it.&lt;/p&gt;</description>
    <link>https://example.test/budget</link>
  </item>
  <item>
    <title>Plain item</title>
    <description>No markup at all here.</description>
    <link>javascript:alert(1)</link>
  </item>
</channel></rss>"""


@unittest.skipUnless(DEPS_AVAILABLE, "requires beautifulsoup4 and defusedxml")
class StripHtmlTests(unittest.TestCase):
    def test_tags_and_links_are_removed(self):
        cleaned = crawler._strip_html("<p>The council <a href='http://x.test'>approved</a> it.</p>")
        self.assertEqual(cleaned, "The council approved it.")

    def test_plain_text_is_returned_unchanged(self):
        self.assertEqual(crawler._strip_html("No markup here at all"), "No markup here at all")

    def test_empty_input(self):
        self.assertEqual(crawler._strip_html(""), "")

    def test_a_less_than_sign_is_not_mistaken_for_a_tag(self):
        text = "Revenue fell when 3 < 5 became true"
        self.assertEqual(crawler._strip_html(text), text)


@unittest.skipUnless(DEPS_AVAILABLE, "requires beautifulsoup4 and defusedxml")
class FeedParsingTests(unittest.TestCase):
    def setUp(self):
        self._orig = crawler._fetch_raw
        crawler._fetch_raw = lambda url, accept="text/html": crawler.RawFetch(
            ok=True, content=RSS_WITH_MARKUP, content_type="application/rss+xml"
        )

    def tearDown(self):
        crawler._fetch_raw = self._orig

    def test_description_markup_does_not_reach_the_summarizer(self):
        # A source whose article pages are disallowed by robots.txt falls back to
        # the feed description, so markup left here shows up verbatim in a synopsis.
        result = crawler.fetch_feed("https://example.test/feed.xml")
        self.assertTrue(result.ok)
        first = result.items[0]
        self.assertEqual(first.description, "The council approved it.")
        self.assertNotIn("<", first.description)

    def test_non_http_links_are_rejected(self):
        result = crawler.fetch_feed("https://example.test/feed.xml")
        self.assertEqual(result.items[0].link, "https://example.test/budget")
        # A javascript: URI must never survive to become a clickable citation.
        self.assertEqual(result.items[1].link, "")


if __name__ == "__main__":
    unittest.main()
