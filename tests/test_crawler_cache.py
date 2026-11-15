"""Content cache tests.

Feeds were re-downloaded on every search. Measured on six sources, that was
104.9s of crawl-delay sleeping against 15.9s of real network I/O, so a cache
is the single largest lever on search latency.
"""
import unittest

try:
    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class ContentCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._feed = crawler._fetch_feed_uncached
        self._page = crawler._fetch_and_extract_uncached
        crawler.clear_cache()

    def tearDown(self):
        crawler._fetch_feed_uncached = self._feed
        crawler._fetch_and_extract_uncached = self._page
        crawler.clear_cache()

    def _stub_feed(self, ok=True):
        def stub(url):
            self.calls.append(url)
            if not ok:
                return crawler.FeedResult(ok=False, error="boom")
            return crawler.FeedResult(ok=True, items=[crawler.FeedItem("t", "d", "https://x.test/a")])
        crawler._fetch_feed_uncached = stub

    def test_repeated_fetches_hit_the_network_once(self):
        self._stub_feed()
        for _ in range(3):
            crawler.fetch_feed("https://example.test/feed.xml")
        self.assertEqual(len(self.calls), 1)

    def test_cached_result_matches_the_live_one(self):
        self._stub_feed()
        first = crawler.fetch_feed("https://example.test/feed.xml")
        second = crawler.fetch_feed("https://example.test/feed.xml")
        self.assertEqual(first.items[0].title, second.items[0].title)

    def test_distinct_urls_are_cached_separately(self):
        self._stub_feed()
        crawler.fetch_feed("https://example.test/one.xml")
        crawler.fetch_feed("https://example.test/two.xml")
        crawler.fetch_feed("https://example.test/one.xml")
        self.assertEqual(len(self.calls), 2)

    def test_failures_are_not_cached(self):
        # Caching a failure would make a transient network error stick for the
        # whole TTL, turning a blip into a source that looks permanently dead.
        self._stub_feed(ok=False)
        crawler.fetch_feed("https://example.test/dead.xml")
        crawler.fetch_feed("https://example.test/dead.xml")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(crawler.cache_stats()["entries"], 0)

    def test_expiry_returns_to_the_network(self):
        self._stub_feed()
        original_ttl = crawler.FEED_CACHE_TTL
        try:
            crawler.FEED_CACHE_TTL = -1.0  # already expired when stored
            crawler.fetch_feed("https://example.test/feed.xml")
            crawler.fetch_feed("https://example.test/feed.xml")
        finally:
            crawler.FEED_CACHE_TTL = original_ttl
        self.assertEqual(len(self.calls), 2)

    def test_clear_cache_empties_it(self):
        self._stub_feed()
        crawler.fetch_feed("https://example.test/feed.xml")
        self.assertEqual(crawler.cache_stats()["entries"], 1)
        crawler.clear_cache()
        self.assertEqual(crawler.cache_stats()["entries"], 0)
        crawler.fetch_feed("https://example.test/feed.xml")
        self.assertEqual(len(self.calls), 2)

    def test_cache_is_bounded(self):
        self._stub_feed()
        original = crawler.MAX_CACHE_ENTRIES
        try:
            crawler.MAX_CACHE_ENTRIES = 4
            for i in range(20):
                crawler.fetch_feed(f"https://example.test/{i}.xml")
            self.assertLessEqual(crawler.cache_stats()["entries"], 4)
        finally:
            crawler.MAX_CACHE_ENTRIES = original

    def test_page_key_includes_the_link_scope(self):
        # fetch_and_extract's link list depends on same_domain_only_from, so two
        # calls differing only in that argument must not share a cache entry.
        pages = []

        def stub(url, same_domain_only_from=None):
            pages.append(same_domain_only_from)
            return crawler.FetchResult(url=url, ok=True, text="body text")

        crawler._fetch_and_extract_uncached = stub
        crawler.fetch_and_extract("https://example.test/a")
        crawler.fetch_and_extract("https://example.test/a", same_domain_only_from="https://example.test/search")
        crawler.fetch_and_extract("https://example.test/a")
        self.assertEqual(len(pages), 2)


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class CacheByteBudgetTests(unittest.TestCase):
    """An entry count is not a bound. 512 entries of up to MAX_CONTENT_BYTES
    each can reach 1.4 GB, and 200 entries of 1 MB were measured holding 191 MB
    without eviction firing once."""

    def setUp(self):
        crawler.clear_cache()

    def tearDown(self):
        crawler.clear_cache()

    @staticmethod
    def _page(i, size):
        return crawler.FetchResult(url=f"https://e{i}.test/a", ok=True, text="x" * size)

    def test_the_cache_stays_within_its_byte_budget(self):
        for i in range(200):
            crawler._cache_put(("page", f"u{i}", None), self._page(i, 1_000_000), 300.0)
        stats = crawler.cache_stats()
        self.assertLessEqual(stats["bytes"], crawler.MAX_CACHE_BYTES)
        self.assertLess(stats["entries"], 200, "nothing was evicted")

    def test_eviction_fires_on_bytes_before_the_entry_cap(self):
        # Entry count stays far below MAX_CACHE_ENTRIES, so only a byte budget
        # can trigger eviction here.
        for i in range(120):
            crawler._cache_put(("page", f"u{i}", None), self._page(i, 1_000_000), 300.0)
        self.assertLess(crawler.cache_stats()["entries"], crawler.MAX_CACHE_ENTRIES)
        self.assertLessEqual(crawler.cache_stats()["bytes"], crawler.MAX_CACHE_BYTES)

    def test_small_entries_are_not_evicted_unnecessarily(self):
        for i in range(50):
            crawler._cache_put(("page", f"u{i}", None), self._page(i, 1000), 300.0)
        self.assertEqual(crawler.cache_stats()["entries"], 50)

    def test_an_entry_larger_than_the_whole_budget_is_not_cached(self):
        # It must not evict everything else to make room for one item.
        crawler._cache_put(("page", "small", None), self._page(0, 1000), 300.0)
        crawler._cache_put(("page", "huge", None), self._page(1, crawler.MAX_CACHE_BYTES + 1), 300.0)
        self.assertIsNone(crawler._cache_get(("page", "huge", None)))
        self.assertIsNotNone(crawler._cache_get(("page", "small", None)))

    def test_replacing_a_key_does_not_double_count_its_bytes(self):
        key = ("page", "same", None)
        crawler._cache_put(key, self._page(0, 500_000), 300.0)
        first = crawler.cache_stats()["bytes"]
        crawler._cache_put(key, self._page(0, 500_000), 300.0)
        self.assertEqual(crawler.cache_stats()["bytes"], first)
        self.assertEqual(crawler.cache_stats()["entries"], 1)

    def test_expiry_releases_the_bytes_it_was_holding(self):
        crawler._cache_put(("page", "gone", None), self._page(0, 500_000), -1.0)
        self.assertIsNone(crawler._cache_get(("page", "gone", None)))
        self.assertEqual(crawler.cache_stats()["bytes"], 0)

    def test_clear_cache_resets_the_byte_count(self):
        crawler._cache_put(("page", "a", None), self._page(0, 500_000), 300.0)
        crawler.clear_cache()
        self.assertEqual(crawler.cache_stats()["bytes"], 0)
        self.assertEqual(crawler.cache_stats()["entries"], 0)

    def test_feed_results_are_measured_too(self):
        feed = crawler.FeedResult(ok=True, items=[
            crawler.FeedItem("t" * 100, "d" * 1000, "https://x.test/a") for _ in range(10)
        ])
        crawler._cache_put(("feed", "f", None), feed, 300.0)
        self.assertGreater(crawler.cache_stats()["bytes"], 10_000)

if __name__ == "__main__":
    unittest.main()
