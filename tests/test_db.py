"""Database tests.

db.py imports nothing outside the standard library, so these run on a bare clone.
"""
import tempfile
import unittest
from pathlib import Path

import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self._original = db.DB_PATH
        db.DB_PATH = Path(tempfile.gettempdir()) / "news_synopsis_db_unit.db"
        db.DB_PATH.unlink(missing_ok=True)
        db.init_db()

    def tearDown(self):
        db.DB_PATH.unlink(missing_ok=True)
        db.DB_PATH = self._original

    def test_init_db_is_idempotent(self):
        db.init_db()
        db.init_db()
        self.assertEqual(db.list_sites(), [])

    def test_add_and_get_site(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        self.assertEqual(db.get_site(site["id"])["name"], "Example")
        self.assertEqual(site["enabled"], 1)

    def test_get_site_returns_none_when_missing(self):
        self.assertIsNone(db.get_site(9999))

    def test_feeds_are_attached_to_rss_sites(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        db.add_feed(site["id"], "https://example.test/a.xml")
        db.add_feed(site["id"], "https://example.test/b.xml")
        listed = db.list_sites()[0]
        self.assertEqual(len(listed["feeds"]), 2)

    def test_search_sites_get_an_empty_feed_list(self):
        db.add_site("Searchy", "search", tos_ack=True, search_page_url="https://x.test", param_name="q")
        self.assertEqual(db.list_sites()[0]["feeds"], [])

    def test_feed_exists(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        db.add_feed(site["id"], "https://example.test/a.xml")
        self.assertTrue(db.feed_exists(site["id"], "https://example.test/a.xml"))
        self.assertFalse(db.feed_exists(site["id"], "https://example.test/other.xml"))

    def test_adding_the_same_feed_twice_does_not_duplicate_it(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        first = db.add_feed(site["id"], "https://example.test/a.xml")
        second = db.add_feed(site["id"], "https://example.test/a.xml")
        # The second call returns the existing row rather than a new one. It is
        # selected by (site_id, feed_url), not by lastrowid, which is meaningless
        # when INSERT OR IGNORE skipped the insert.
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(db.list_feeds_for_site(site["id"])), 1)

    def test_add_feed_to_a_missing_site_returns_none(self):
        self.assertIsNone(db.add_feed(9999, "https://example.test/a.xml"))

    def test_deleting_a_site_cascades_to_its_feeds(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        db.add_feed(site["id"], "https://example.test/a.xml")
        self.assertTrue(db.delete_site(site["id"]))
        self.assertEqual(db.list_feeds_for_site(site["id"]), [])

    def test_delete_reports_whether_anything_was_removed(self):
        self.assertFalse(db.delete_site(9999))
        self.assertFalse(db.delete_feed(9999))

    def test_enabled_toggle_filters_the_enabled_list(self):
        site = db.add_site("Example", "rss", tos_ack=True)
        self.assertEqual(len(db.list_enabled_sites()), 1)
        self.assertEqual(db.set_enabled(site["id"], False)["enabled"], 0)
        self.assertEqual(db.list_enabled_sites(), [])
        self.assertEqual(db.set_enabled(site["id"], True)["enabled"], 1)

    def test_set_enabled_on_a_missing_site_returns_none(self):
        self.assertIsNone(db.set_enabled(9999, True))

    def test_connection_is_released_when_a_call_raises(self):
        # Every db function wraps its connection in closing(). Without that, an
        # exception between get_conn() and close() leaks the handle. On Windows
        # an open sqlite handle keeps the file locked, so unlinking successfully
        # afterwards is direct evidence the connection was released.
        original = db._attach_feeds

        def boom(conn, sites):
            raise RuntimeError("query blew up")

        db._attach_feeds = boom
        try:
            with self.assertRaises(RuntimeError):
                db.list_sites()
        finally:
            db._attach_feeds = original

        db.DB_PATH.unlink()
        self.assertFalse(db.DB_PATH.exists())


if __name__ == "__main__":
    unittest.main()
