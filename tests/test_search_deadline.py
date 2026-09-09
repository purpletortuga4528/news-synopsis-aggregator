"""Search budget tests.

Sources are crawled in parallel but each source's fetches are serial and every
one waits out that host's crawl-delay, so a source with many feeds can run for a
long time. Without a bound the HTTP request simply hangs and a slow search is
indistinguishable from a broken one. Past the budget a source stops fetching and
reports whatever it already gathered.
"""
import pathlib
import tempfile
import unittest

try:
    import db
    # Point the database at a scratch file before importing app, which calls
    # init_db() at import time. Tests must not touch the real sites.db.
    db.DB_PATH = pathlib.Path(tempfile.gettempdir()) / "news_synopsis_deadline_test.db"
    import app
    import boolean_search
    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False

RSS_SITE = {
    "id": 1,
    "name": "Test Source",
    "site_type": "rss",
    "feeds": [{"feed_url": "https://example.test/feed.xml"}],
}
BODY = "The council approved the budget after a long debate about school funding. " * 5


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"t": 1000.0}
        self._monotonic = app.time.monotonic
        self._feed = crawler.fetch_feed
        self._page = crawler.fetch_and_extract
        app.time.monotonic = lambda: self.clock["t"]
        self.ast = boolean_search.parse_query("budget")

    def tearDown(self):
        app.time.monotonic = self._monotonic
        crawler.fetch_feed = self._feed
        crawler.fetch_and_extract = self._page

    def _install(self, items, feed_cost=0.0, page_cost=0.0):
        def fetch_feed(url):
            self.clock["t"] += feed_cost
            return crawler.FeedResult(ok=True, items=items)

        def fetch_and_extract(url, same_domain_only_from=None):
            self.clock["t"] += page_cost
            return crawler.FetchResult(url=url, ok=True, text=BODY, title="Budget approved")

        crawler.fetch_feed = fetch_feed
        crawler.fetch_and_extract = fetch_and_extract

    @staticmethod
    def _items(n):
        return [
            crawler.FeedItem(f"Budget item {i}", "council budget debate", f"https://example.test/news/{i}")
            for i in range(n)
        ]

    def test_expired_budget_reports_timeout_and_fetches_nothing(self):
        fetched = []
        crawler.fetch_feed = lambda url: fetched.append(url) or crawler.FeedResult(ok=True, items=[])
        result = app._crawl_rss_site(RSS_SITE, self.ast, deadline=self.clock["t"] - 1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["docs"], [])
        self.assertEqual(fetched, [], "no request should be made once the budget is gone")

    def test_partial_keeps_what_was_gathered_before_the_budget_ran_out(self):
        # One feed costing 6s, then articles costing 6s each, against a 10s
        # budget: the feed and the first article land, the second does not.
        self._install(self._items(3), feed_cost=6.0, page_cost=6.0)
        result = app._crawl_rss_site(RSS_SITE, self.ast, deadline=self.clock["t"] + 10.0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["docs"]), 1)
        self.assertIn("budget", result["reason"].lower())

    def test_a_search_within_budget_is_reported_as_ok(self):
        self._install(self._items(2), feed_cost=0.1, page_cost=0.1)
        result = app._crawl_rss_site(RSS_SITE, self.ast, deadline=self.clock["t"] + 60.0)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["reason"])
        self.assertEqual(len(result["docs"]), 2)

    def test_partial_results_still_reach_the_report(self):
        # A "partial" source must not be discarded by the synthesis step: the
        # report keys on whether docs were retrieved, not on the status label.
        partial = app._site_result(RSS_SITE, "partial", "Stopped early", [
            {"url": "https://example.test/news/0", "title": "t", "published": None, "text": BODY}
        ])
        self.assertTrue(partial["docs"])
        self.assertNotEqual(partial["status"], "ok")

    def test_search_site_respects_an_expired_budget(self):
        site = {"id": 2, "name": "Searchy", "site_type": "search",
                "search_page_url": "https://example.test/search", "param_name": "q"}
        called = []
        crawler.fetch_and_extract = lambda url, same_domain_only_from=None: called.append(url)
        result = app._crawl_search_site(site, "budget", deadline=self.clock["t"] - 1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(called, [])

    def test_budget_constants_are_sane(self):
        self.assertGreater(app.SEARCH_BUDGET_SECONDS, 0)
        self.assertGreater(app.SEARCH_GRACE_SECONDS, 0)
        self.assertFalse(app._past(self.clock["t"] + 1))
        self.assertTrue(app._past(self.clock["t"] - 1))


if __name__ == "__main__":
    unittest.main()
