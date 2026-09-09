"""Endpoint-level tests for the feed management routes.

The database is redirected to a scratch file by tests/__init__.py, which runs
before any test module imports app.
"""
import unittest

try:
    import app
    import crawler
    import db
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class AddFeedTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH.unlink(missing_ok=True)
        db.init_db()
        self.fetched = []
        self._fetch_feed = crawler.fetch_feed

        def recording_fetch(url):
            self.fetched.append(url)
            return crawler.FeedResult(ok=True, items=[crawler.FeedItem("t", "d", "https://x.test/a")])

        crawler.fetch_feed = recording_fetch
        self.site = db.add_site("Example", "rss", tos_ack=True)

    def tearDown(self):
        crawler.fetch_feed = self._fetch_feed
        db.DB_PATH.unlink(missing_ok=True)

    def _body(self, url="https://example.test/a.xml"):
        return app.FeedIn(feed_url=url)

    def test_a_valid_feed_is_added(self):
        created = app.add_feed(self.site["id"], self._body())
        self.assertEqual(created["feed_url"], "https://example.test/a.xml")
        self.assertEqual(len(self.fetched), 1)

    def test_a_bad_url_is_rejected_without_a_fetch(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.add_feed(self.site["id"], self._body("ftp://example.test/a.xml"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.fetched, [])

    def test_an_unknown_site_is_rejected_before_the_network_fetch(self):
        # Validating a feed over the network only to answer "no such site" wastes
        # a request and a crawl-delay against someone else's server.
        with self.assertRaises(app.HTTPException) as ctx:
            app.add_feed(9999, self._body())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.fetched, [], "no request should be made for an unknown site")

    def test_a_duplicate_feed_is_reported_rather_than_silently_ignored(self):
        app.add_feed(self.site["id"], self._body())
        with self.assertRaises(app.HTTPException) as ctx:
            app.add_feed(self.site["id"], self._body())
        self.assertEqual(ctx.exception.status_code, 409)
        # And it costs no second fetch.
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(len(db.list_feeds_for_site(self.site["id"])), 1)

    def test_an_unusable_feed_is_rejected(self):
        crawler.fetch_feed = lambda url: crawler.FeedResult(ok=False, error="404")
        with self.assertRaises(app.HTTPException) as ctx:
            app.add_feed(self.site["id"], self._body())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_robots_refusal_is_named_in_the_message(self):
        crawler.fetch_feed = lambda url: crawler.FeedResult(ok=False, blocked_by_robots=True, error="x")
        with self.assertRaises(app.HTTPException) as ctx:
            app.add_feed(self.site["id"], self._body())
        self.assertIn("robots.txt", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
