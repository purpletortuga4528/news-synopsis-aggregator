"""Streaming search.

A cold search takes most of a minute, nearly all of it spent waiting out other
people's crawl-delays. The blocking endpoint returns nothing until the whole
thing finishes, which is indistinguishable from a hang, so the same search is
also reported source by source as it happens.
"""
import asyncio
import json
import unittest

try:
    import app
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False

SITES = [
    {"id": 1, "name": "Alpha", "site_type": "rss", "feeds": [{"feed_url": "https://a.test/f"}]},
    {"id": 2, "name": "Beta", "site_type": "rss", "feeds": [{"feed_url": "https://b.test/f"}]},
]
BODY = "The council approved the budget after a long debate about school funding. " * 4


def doc(url="https://a.test/1"):
    return {"url": url, "title": "A headline", "published": None, "text": BODY}


async def _drain(body_iterator):
    return [chunk async for chunk in body_iterator]


def parse_events(chunks):
    """Turn raw SSE bytes back into (event name, payload) pairs."""
    text = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    events = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[6:])))
    return events


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class SseFormatTests(unittest.TestCase):
    def test_frame_shape(self):
        frame = app._sse("source", {"name": "Alpha"})
        self.assertTrue(frame.startswith("event: source\ndata: "))
        self.assertTrue(frame.endswith("\n\n"))

    def test_payload_stays_on_one_line(self):
        # A newline inside the data would terminate the frame early and split
        # one event into two malformed ones.
        frame = app._sse("report", {"overview": "line one\nline two"})
        self.assertEqual(frame.count("\n"), 3)


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class CrawlSitesTests(unittest.TestCase):
    def setUp(self):
        self._safe = app._crawl_one_site_safe

    def tearDown(self):
        app._crawl_one_site_safe = self._safe

    def test_results_are_yielded_not_collected(self):
        app._crawl_one_site_safe = lambda site, q, ast, dl: app._site_result(site, "ok", None, [doc()])
        produced = app._crawl_sites(SITES, "budget", None, app.time.monotonic() + 60)
        # A generator, so a caller can report progress while the crawl is still
        # running rather than only once every source has finished.
        self.assertTrue(hasattr(produced, "__next__"))
        self.assertEqual(len(list(produced)), 2)

    def test_every_site_appears_exactly_once(self):
        app._crawl_one_site_safe = lambda site, q, ast, dl: app._site_result(site, "empty", "nothing", [])
        names = [r["name"] for r in app._crawl_sites(SITES, "budget", None, app.time.monotonic() + 60)]
        self.assertCountEqual(names, ["Alpha", "Beta"])

    def test_an_expired_budget_still_accounts_for_every_source(self):
        app._crawl_one_site_safe = lambda site, q, ast, dl: app._site_result(site, "timeout", "out of time", [])
        results = list(app._crawl_sites(SITES, "budget", None, app.time.monotonic() - 1))
        self.assertEqual(len(results), 2)


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class StreamEndpointTests(unittest.TestCase):
    def setUp(self):
        self._prepare = app._prepare_search
        self._crawl = app._crawl_sites

    def tearDown(self):
        app._prepare_search = self._prepare
        app._crawl_sites = self._crawl

    def _events(self, q="budget"):
        return parse_events(asyncio.run(_drain(app.search_stream(q=q).body_iterator)))

    def test_order_is_start_then_each_source_then_the_report(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([
            app._site_result(SITES[0], "ok", None, [doc("https://a.test/1")]),
            app._site_result(SITES[1], "ok", None, [doc("https://b.test/1")]),
        ])
        names = [name for name, _ in self._events()]
        self.assertEqual(names, ["start", "source", "source", "report"])

    def test_start_names_the_sources_so_the_ui_can_show_what_it_waits_on(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([])
        start = dict(self._events())["start"]
        self.assertEqual([s["name"] for s in start["sources"]], ["Alpha", "Beta"])
        self.assertEqual(start["budget_seconds"], app.SEARCH_BUDGET_SECONDS)

    def test_each_source_event_carries_progress(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([
            app._site_result(SITES[0], "ok", None, [doc()]),
            app._site_result(SITES[1], "empty", "no match", []),
        ])
        sources = [p for n, p in self._events() if n == "source"]
        self.assertEqual([s["done"] for s in sources], [1, 2])
        self.assertEqual([s["total"] for s in sources], [2, 2])
        self.assertEqual(sources[0]["documents"], 1)
        self.assertEqual(sources[1]["status"], "empty")
        self.assertEqual(sources[1]["reason"], "no match")

    def test_the_report_event_carries_the_whole_report(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([
            app._site_result(SITES[0], "ok", None, [doc()]),
        ])
        report = dict(self._events())["report"]
        for key in ("query", "overview", "key_points", "differences",
                    "only_one_source", "citations", "site_statuses", "stats"):
            self.assertIn(key, report)
        self.assertEqual(report["stats"]["articles_found"], 1)

    def test_a_rejected_query_arrives_as_an_event_not_an_http_error(self):
        # EventSource cannot read the body of an HTTP error response, so a
        # raised HTTPException would reach the user as "something went wrong".
        # Sources are stubbed in so validation reaches the query check rather
        # than stopping at "no enabled sites".
        import db
        original = db.list_enabled_sites
        db.list_enabled_sites = lambda: SITES
        try:
            events = self._events(q="AND OR")
        finally:
            db.list_enabled_sites = original
        self.assertEqual([n for n, _ in events], ["error"])
        self.assertIn("search term", events[0][1]["detail"])

    def test_having_no_sources_is_also_reported_as_an_event(self):
        import db
        original = db.list_enabled_sites
        db.list_enabled_sites = list
        try:
            events = self._events(q="budget")
        finally:
            db.list_enabled_sites = original
        self.assertEqual([n for n, _ in events], ["error"])
        self.assertIn("No enabled sites", events[0][1]["detail"])

    def test_the_stream_sets_the_headers_that_stop_buffering(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([])
        resp = app.search_stream(q="budget")
        self.assertEqual(resp.media_type, "text/event-stream")
        self.assertEqual(resp.headers["cache-control"], "no-cache")


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class BlockingEndpointStillWorksTests(unittest.TestCase):
    def setUp(self):
        self._prepare = app._prepare_search
        self._crawl = app._crawl_sites

    def tearDown(self):
        app._prepare_search = self._prepare
        app._crawl_sites = self._crawl

    def test_post_search_returns_the_same_report(self):
        app._prepare_search = lambda q: (SITES, None)
        app._crawl_sites = lambda sites, q, ast, dl: iter([
            app._site_result(SITES[0], "ok", None, [doc()]),
        ])
        report = app.search(app.SearchIn(query="budget"))
        self.assertEqual(report["query"], "budget")
        self.assertEqual(report["stats"]["articles_found"], 1)


if __name__ == "__main__":
    unittest.main()
