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


@unittest.skipUnless(DEPS_AVAILABLE, "requires beautifulsoup4 and defusedxml")
class ArticlePathTests(unittest.TestCase):
    def test_site_furniture_is_rejected(self):
        for url in (
            "https://example.test/about",
            "https://example.test/contact/form",
            "https://example.test/news/rss.xml",
            "https://example.test/subscribe",
        ):
            with self.subTest(url=url):
                self.assertFalse(crawler.looks_like_article(url))

    def test_media_platform_pages_are_rejected(self):
        # A news feed can point at these. They carry promos, not article text,
        # which then surfaced in synopses as "Find out how to listen to other
        # BBC stations".
        for url in (
            "https://www.bbc.co.uk/sounds/play/m002abc",
            "https://www.bbc.co.uk/programmes/b006qshd",
            "https://example.test/podcasts/episode-12",
            "https://example.test/iplayer/episode/x",
        ):
            with self.subTest(url=url):
                self.assertFalse(crawler.looks_like_article(url))

    def test_real_articles_are_accepted(self):
        for url in (
            "https://www.bbc.co.uk/news/articles/cp3k72k1y3vo",
            "https://www.theguardian.com/politics/2026/sep/09/met-police-inquiry",
        ):
            with self.subTest(url=url):
                self.assertTrue(crawler.looks_like_article(url))

    def test_a_hint_inside_a_longer_word_does_not_reject(self):
        # Regression: the old substring test threw away an article at
        # /news/subscribers-hit-record because "/subscribe" is inside it.
        self.assertTrue(crawler.looks_like_article("https://example.test/news/subscribers-hit-record"))
        self.assertTrue(crawler.looks_like_article("https://example.test/news/helping-hands-charity"))


@unittest.skipUnless(DEPS_AVAILABLE, "requires beautifulsoup4 and defusedxml")
class SpacingTests(unittest.TestCase):
    def test_space_before_punctuation_is_removed(self):
        # get_text(" ") separates inline nodes, so "<a>police</a>." arrives as
        # "police ." and reads as a typo in the finished synopsis.
        self.assertEqual(
            crawler._tidy_spacing("So that does increase the demand for police ."),
            "So that does increase the demand for police.",
        )

    def test_brackets_and_percent(self):
        self.assertEqual(
            crawler._tidy_spacing("The report ( published today ) says 40 % ."),
            "The report (published today) says 40%.",
        )

    def test_block_boundaries_are_never_crossed(self):
        # A newline is a block boundary. Pulling punctuation across one would
        # silently glue two separate blocks into a single sentence.
        text = "Block one\n, block two"
        self.assertEqual(crawler._tidy_spacing(text), text)

    def test_ordinary_text_is_untouched(self):
        text = "The council approved the budget after a long debate."
        self.assertEqual(crawler._tidy_spacing(text), text)

if __name__ == "__main__":
    unittest.main()
