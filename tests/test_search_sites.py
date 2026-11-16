"""Search-page sources.

This path had only ever run against unit tests; all the seeded sources are RSS.
Trying it against six real news search pages turned up both a publisher-side
reality (most disallow their search path in robots.txt) and a bug of our own:
the results page itself was being treated as a document, so a BBC search
contributed "Search the BBC" and 15,000 characters of topic-index link text to
the report as though they were reporting.
"""
import unittest

try:
    import app
    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False

SITE = {
    "id": 7,
    "name": "Searchy",
    "site_type": "search",
    "search_page_url": "https://example.test/search",
    "param_name": "q",
}
ARTICLE = "The council approved the budget after a long debate about school funding. " * 6


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class SearchResultsPageTests(unittest.TestCase):
    def setUp(self):
        self._fetch = crawler.fetch_and_extract

    def tearDown(self):
        crawler.fetch_and_extract = self._fetch

    def _install(self, links, article_text=ARTICLE):
        search_url = app.build_search_url(SITE["search_page_url"], SITE["param_name"], "budget")

        def fetch(url, same_domain_only_from=None):
            # Compared exactly rather than by substring: an earlier version of
            # this stub matched "/search" anywhere, and the test host name
            # contained it, so every article took the results-page branch.
            if url == search_url:
                return crawler.FetchResult(
                    url=url, ok=True, title="Search results",
                    text="Search results Skip to content Sign in Results for budget",
                    links=list(links),
                )
            return crawler.FetchResult(url=url, ok=True, title="A real article", text=article_text)
        crawler.fetch_and_extract = fetch

    def test_the_results_page_is_not_itself_a_document(self):
        # It lists headlines and navigation. Treating it as reporting put
        # "Search the BBC" into a synopsis as a claim.
        self._install(["https://example.test/news/2026/a-real-story"])
        result = app._crawl_search_site(SITE, "budget", app.time.monotonic() + 60)
        urls = [d["url"] for d in result["docs"]]
        self.assertNotIn("https://example.test/search?q=budget", urls)
        self.assertEqual(urls, ["https://example.test/news/2026/a-real-story"])

    def test_articles_behind_the_results_page_are_still_collected(self):
        self._install([
            "https://example.test/news/2026/one",
            "https://example.test/news/2026/two",
        ])
        result = app._crawl_search_site(SITE, "budget", app.time.monotonic() + 60)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["docs"]), 2)

    def test_a_results_page_with_no_usable_links_yields_nothing(self):
        # Previously this still produced one document: the results page itself.
        self._install([])
        result = app._crawl_search_site(SITE, "budget", app.time.monotonic() + 60)
        self.assertEqual(result["docs"], [])
        self.assertEqual(result["status"], "empty")

    def test_the_follow_limit_is_respected(self):
        self._install([f"https://example.test/news/2026/{i}" for i in range(10)])
        result = app._crawl_search_site(SITE, "budget", app.time.monotonic() + 60)
        self.assertEqual(len(result["docs"]), app.FOLLOW_LINKS_PER_SITE)


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class IndexPageTests(unittest.TestCase):
    """An index page lists headlines rather than reporting anything, so its text
    is link text. A BBC topic index was measured contributing 15,000 characters
    of it as though it were an article."""

    def test_index_pages_are_rejected(self):
        for url in (
            "https://example.test/search?q=climate",
            "https://example.test/news/topics/cmj34zmwm1zt",
            "https://example.test/topic/energy",
            "https://example.test/tag/policy",
            "https://example.test/category/world",
            "https://example.test/archive/2026",
        ):
            with self.subTest(url=url):
                self.assertFalse(crawler.looks_like_article(url))

    def test_real_articles_are_still_accepted(self):
        for url in (
            "https://www.bbc.co.uk/news/articles/ckx28ly7y387o",
            "https://www.theguardian.com/politics/2026/sep/09/met-police-inquiry",
            "https://example.test/news/2026/11/16/council-approves-budget",
        ):
            with self.subTest(url=url):
                self.assertTrue(crawler.looks_like_article(url))

    def test_a_segment_is_matched_whole_not_as_a_substring(self):
        # "searchlight" and "indexation" are not index pages.
        self.assertTrue(crawler.looks_like_article("https://example.test/news/searchlight-report"))
        self.assertTrue(crawler.looks_like_article("https://example.test/news/indexation-of-pensions"))


if __name__ == "__main__":
    unittest.main()
