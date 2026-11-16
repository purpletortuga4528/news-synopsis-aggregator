import json
import re
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import crawler
import boolean_search
from summarizer import summarize, top_sentences, keywords

app = FastAPI(title="Topic Synopsis Crawler")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# This API has no authentication and is meant to be reached only from a browser
# on the same machine. Without this check, any webpage the user visits could
# submit cross-origin requests to it (add sites, trigger crawls) purely because
# it's listening on localhost: a "local CSRF" pattern used to puppeteer local
# dev tools into acting as an SSRF proxy. Reject anything whose Host or Origin
# doesn't identify this machine.
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _hostname_only(netloc: str) -> str:
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


@app.middleware("http")
async def restrict_to_localhost(request: Request, call_next):
    host_header = request.headers.get("host", "")
    # Host is case-insensitive per RFC 9110, so fold before comparing.
    if _hostname_only(host_header).lower() not in _LOCAL_HOSTNAMES:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        origin_host = urllib.parse.urlparse(origin).hostname
        if origin_host not in _LOCAL_HOSTNAMES:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)

db.init_db()

FOLLOW_LINKS_PER_SITE = 3
FEED_ITEMS_PER_SITE = 5
SITE_SUMMARY_SENTENCES = 3
CLAIM_SENTENCES_PER_DOC = 2
OVERVIEW_SENTENCES = 4
MAX_KEY_POINTS = 8
MAX_ONLY_ONE_SOURCE = 8
# Calibrated over 24 queries against the seeded sources. At 0.35 the run produced
# 3 corroborated points of which 2 were genuine; at 0.25, 8 of which 5 were.
# Precision is flat between the two and recall is 2.5x, so the looser value
# surfaces more real corroboration without being less reliable. It is not
# reliable in absolute terms: roughly a third of key points are a lexical
# coincidence, which is why each one shows every source's own sentence.
CLUSTER_SIMILARITY_THRESHOLD = 0.25
CLUSTER_MIN_KEYWORDS = 3

# A search fans out over sources in parallel, but within a source the fetches
# are serial and each waits out that host's crawl-delay, so a source with many
# feeds can run for a long time. Without a bound the request simply hangs and
# the user cannot tell a slow search from a broken one. Past the budget a
# source stops fetching and reports what it already has.
SEARCH_BUDGET_SECONDS = 60.0
# A fetch already in flight cannot be interrupted, so allow it to land.
SEARCH_GRACE_SECONDS = 15.0


def _past(deadline: float) -> bool:
    return time.monotonic() >= deadline


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Site management
# ---------------------------------------------------------------------------

class SiteIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    url: str = Field(max_length=2048)
    param_name: Optional[str] = Field(default=None, max_length=50)
    tos_ack: bool


def _derive_name(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def build_search_url(search_page_url: str, param_name: str, query: str) -> str:
    """Insert the search phrase into the named query parameter, preserving any
    other query params already present on the search page URL."""
    parsed = urllib.parse.urlparse(search_page_url)
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != param_name]
    params.append((param_name, query))
    new_query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


@app.get("/api/sites")
def get_sites():
    return db.list_sites()


@app.post("/api/sites")
def create_site(site: SiteIn):
    if not site.tos_ack:
        raise HTTPException(400, "You must confirm you've reviewed this site's Terms of Service before adding it.")

    parsed = urllib.parse.urlparse(site.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, f"Invalid URL: {site.url}")

    name = (site.name or "").strip() or _derive_name(site.url)

    # Try treating the URL as an RSS/Atom feed first. Feeds are self-describing,
    # so this can be auto-detected without asking the user anything extra.
    feed = crawler.fetch_feed(site.url)
    if feed.ok and feed.items:
        created = db.add_site(name, "rss", site.tos_ack)
        db.add_feed(created["id"], site.url)
        return {**created, "feeds": db.list_feeds_for_site(created["id"])}

    # Not a feed: treat as a search page, which requires knowing the query parameter name.
    param_name = (site.param_name or "").strip()
    if not param_name:
        raise HTTPException(
            422,
            detail={
                "code": "NEEDS_PARAM_NAME",
                "message": "This doesn't look like an RSS/Atom feed. Enter the search parameter name this site's search URL uses (e.g. 'q').",
            },
        )

    return db.add_site(name, "search", site.tos_ack, search_page_url=site.url, param_name=param_name)


class ToggleIn(BaseModel):
    enabled: bool


@app.patch("/api/sites/{site_id}")
def toggle_site(site_id: int, body: ToggleIn):
    updated = db.set_enabled(site_id, body.enabled)
    if not updated:
        raise HTTPException(404, "Site not found")
    return updated


@app.delete("/api/sites/{site_id}")
def remove_site(site_id: int):
    if not db.delete_site(site_id):
        raise HTTPException(404, "Site not found")
    return {"deleted": True}


class FeedIn(BaseModel):
    feed_url: str = Field(max_length=2048)
    label: Optional[str] = Field(default=None, max_length=100)


@app.post("/api/sites/{site_id}/feeds")
def add_feed(site_id: int, body: FeedIn):
    parsed = urllib.parse.urlparse(body.feed_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, f"Invalid URL: {body.feed_url}")
    # Both cheap checks come before the network fetch. Validating a feed over the
    # network only to answer "no such site" wastes a request and a crawl-delay,
    # and adding a feed that is already present used to return 200 having done
    # nothing, which reads as success.
    if not db.get_site(site_id):
        raise HTTPException(404, "Site not found")
    if db.feed_exists(site_id, body.feed_url):
        raise HTTPException(409, "This source already has that feed.")
    feed = crawler.fetch_feed(body.feed_url)
    if not feed.ok or not feed.items:
        reason = "Blocked by this feed's robots.txt" if feed.blocked_by_robots else (feed.error or "Doesn't look like a valid RSS/Atom feed")
        raise HTTPException(400, f"Couldn't add feed: {reason}")
    created = db.add_feed(site_id, body.feed_url, body.label)
    if not created:
        raise HTTPException(404, "Site not found")
    return created


@app.delete("/api/feeds/{feed_id}")
def remove_feed(feed_id: int):
    if not db.delete_feed(feed_id):
        raise HTTPException(404, "Feed not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def _site_result(site: dict, status: str, reason: Optional[str], docs: list) -> dict:
    return {"site_id": site["id"], "name": site["name"], "status": status, "reason": reason, "docs": docs}


def _crawl_search_site(site: dict, query: str, deadline: float) -> dict:
    if not site.get("search_page_url") or not site.get("param_name"):
        return _site_result(site, "skipped", "Site is missing its search configuration", [])
    if _past(deadline):
        return _site_result(site, "timeout", "Search budget ran out before this source started", [])
    search_url = build_search_url(site["search_page_url"], site["param_name"], query)

    entry = crawler.fetch_and_extract(search_url)
    if not entry.ok:
        reason = "Blocked by this site's robots.txt" if entry.blocked_by_robots else (entry.error or "Fetch failed")
        return _site_result(site, "skipped", reason, [])

    # The results page itself is not a document. It lists headlines and
    # navigation, and adding it contributed claims like "Search the BBC" to the
    # report. It is a way of finding articles, not one of them.
    docs = []

    followed = 0
    truncated = False
    for link in entry.links:
        if followed >= FOLLOW_LINKS_PER_SITE:
            break
        if _past(deadline):
            truncated = True
            break
        result = crawler.fetch_and_extract(link, same_domain_only_from=search_url)
        if result.ok and len(result.text) > 200:
            docs.append({"url": link, "title": result.title or link, "published": result.published, "text": result.text})
            followed += 1

    if not docs:
        if truncated:
            return _site_result(site, "timeout", "Search budget ran out before anything was retrieved", [])
        return _site_result(site, "empty", "No extractable text found", [])
    if truncated:
        return _site_result(site, "partial", "Stopped early: the search budget ran out", docs)
    return _site_result(site, "ok", None, docs)


def _crawl_rss_site(site: dict, query_ast, deadline: float) -> dict:
    feed_configs = site.get("feeds") or []
    if not feed_configs:
        return _site_result(site, "skipped", "No feeds configured for this source", [])

    all_items = []
    seen_links = set()
    fetch_errors = []
    truncated = False
    for feed_cfg in feed_configs:
        # Checked per feed rather than per source: a source with fifteen feeds
        # is exactly the case the budget exists for, and stopping between feeds
        # keeps whatever the earlier ones returned.
        if _past(deadline):
            truncated = True
            break
        feed = crawler.fetch_feed(feed_cfg["feed_url"])
        if not feed.ok:
            reason = "Blocked by robots.txt" if feed.blocked_by_robots else (feed.error or "Fetch failed")
            fetch_errors.append(f"{feed_cfg['feed_url']}: {reason}")
            continue
        for item in feed.items:
            key = item.link or f"{feed_cfg['feed_url']}#{item.title}"
            if key in seen_links:
                continue
            seen_links.add(key)
            all_items.append((item, feed_cfg["feed_url"]))

    if not all_items:
        if truncated:
            return _site_result(site, "timeout", "Search budget ran out before any feed was read", [])
        reason = "All feeds failed: " + "; ".join(fetch_errors) if fetch_errors else "No feed items available"
        return _site_result(site, "skipped", reason, [])

    scored = []
    for item, feed_url in all_items:
        matched, score = boolean_search.evaluate(query_ast, item.title, item.description)
        if matched:
            scored.append((score, item, feed_url))

    if not scored:
        return _site_result(site, "empty", "No feed items matched this topic", [])

    scored.sort(key=lambda t: t[0], reverse=True)
    top_items = scored[:FEED_ITEMS_PER_SITE]

    docs = []
    for _, item, feed_url in top_items:
        if _past(deadline):
            truncated = True
            break
        text = ""
        title = item.title
        published = item.published
        # A news feed sometimes points at a podcast or programme page, which
        # carries promotional furniture rather than article text. Skip the scrape
        # and keep the feed's own blurb, so the item still contributes.
        if item.link and crawler.looks_like_article(item.link):
            result = crawler.fetch_and_extract(item.link)
            if result.ok and len(result.text) > 200:
                text = result.text
                title = result.title or item.title
                published = result.published or item.published
        if not text:
            # Newline rather than an invented full stop: a headline that does not
            # end in punctuation is not a sentence we should be fabricating one for.
            text = f"{item.title}\n{item.description}".strip()
        if text:
            docs.append({"url": item.link or feed_url, "title": title or item.title, "published": published, "text": text})

    if not docs:
        if truncated:
            return _site_result(site, "timeout", "Search budget ran out before anything was retrieved", [])
        return _site_result(site, "empty", "No extractable text found", [])
    if truncated:
        return _site_result(site, "partial", "Stopped early: the search budget ran out", docs)
    return _site_result(site, "ok", None, docs)


def _crawl_one_site(site: dict, query: str, query_ast, deadline: float) -> dict:
    if site["site_type"] == "rss":
        return _crawl_rss_site(site, query_ast, deadline)
    # Search-type sites get the raw typed query as-is (including any AND/OR/quotes).
    # It's passed straight to that site's own search engine, which may or may not
    # understand boolean syntax; we don't control or rewrite it.
    return _crawl_search_site(site, query, deadline)


def _crawl_one_site_safe(site: dict, query: str, query_ast, deadline: float) -> dict:
    """An unexpected failure in one site's crawl (a bad HTML page, a network edge
    case, anything not already handled) must not take down the whole search."""
    try:
        return _crawl_one_site(site, query, query_ast, deadline)
    except Exception as exc:
        return _site_result(site, "skipped", f"Internal error: {exc}", [])


# ---------------------------------------------------------------------------
# Cross-source synthesis
# ---------------------------------------------------------------------------

def _cluster_claims(claims: list) -> list:
    """Greedy clustering by keyword-overlap (Jaccard) similarity. Claims whose
    sentence shares enough vocabulary with an existing cluster's first (anchor)
    claim join that cluster; otherwise they start a new one. Comparing only
    against each cluster's anchor (not a growing union) avoids cluster drift."""
    clusters = []
    for claim in claims:
        kw = keywords(claim["sentence"])
        claim["_kw"] = kw
        best_idx, best_score = None, 0.0
        if len(kw) >= CLUSTER_MIN_KEYWORDS:
            for i, cluster in enumerate(clusters):
                anchor_kw = cluster[0]["_kw"]
                if len(anchor_kw) < CLUSTER_MIN_KEYWORDS:
                    continue
                union = kw | anchor_kw
                score = len(kw & anchor_kw) / len(union) if union else 0.0
                if score > best_score:
                    best_score = score
                    best_idx = i
        if best_idx is not None and best_score >= CLUSTER_SIMILARITY_THRESHOLD:
            clusters[best_idx].append(claim)
        else:
            clusters.append([claim])
    return clusters


MAX_OVERVIEW_PER_SITE = 2


def _build_overview(all_claims: list, sites_with_content: int) -> str:
    """A consensus synthesis across the different stories, not a per-site summary.
    Scores sentences by how much of their vocabulary is shared with OTHER sites'
    coverage (not just their own), so the overview reflects what multiple sources
    are actually reporting in common rather than one source's idiosyncratic framing.
    When sources share no real vocabulary, says so plainly instead of stitching
    together unrelated single-source sentences and presenting them as agreement."""
    no_overlap_msg = (
        "The sources that returned results did not share overlapping coverage for "
        "this topic. Each appears to be reporting something different. See Differences in Coverage below."
    )
    if not all_claims or sites_with_content < 2:
        return ""

    site_keyword_union: dict = {}
    for c in all_claims:
        site_keyword_union.setdefault(c["site_name"], set()).update(c["_kw"])

    keyword_site_count = Counter()
    for kws in site_keyword_union.values():
        for kw in kws:
            keyword_site_count[kw] += 1
    consensus_keywords = {kw for kw, n in keyword_site_count.items() if n >= 2}
    if not consensus_keywords:
        return no_overlap_msg

    scored = [(len(c["_kw"] & consensus_keywords), c) for c in all_claims]
    scored = [(score, c) for score, c in scored if score > 0]
    scored.sort(key=lambda t: t[0], reverse=True)

    chosen = []
    per_site_count = Counter()
    for _score, c in scored:
        if per_site_count[c["site_name"]] >= MAX_OVERVIEW_PER_SITE:
            continue
        chosen.append(c["sentence"])
        per_site_count[c["site_name"]] += 1
        if len(chosen) >= OVERVIEW_SENTENCES * 2:
            break

    if not chosen:
        return no_overlap_msg
    return summarize("\n".join(chosen), max_sentences=OVERVIEW_SENTENCES)


# Two sentences can share enough vocabulary to cluster while describing
# different events. The measured example paired "Trump-backed candidate wins
# primary for New Hampshire district" with the same phrasing about Oklahoma
# governor. Set overlap cannot see that: the disagreement lives entirely in the
# words the two sentences do NOT share, which it discards by construction. So
# the disagreement is checked separately.
#
# "Specifics" are capitalised words and multi-digit numbers: names, places,
# dates, quantities. Two accounts of the same event tend to agree on those even
# when they word everything else differently; two accounts of different events
# with the same shape disagree on exactly those and nothing else.
#
# Measured over 16 corroborated pairs from two samples two months apart: the
# current behaviour yields 13 genuine and 3 spurious, and this filter yields 12
# genuine and 0 spurious. It buys precision with one genuine pair, which is the
# right side to err on, because a key point asserts that independent sources
# reported the same thing and a wrong one is a false claim about the world.
#
# Sixteen pairs is a thin sample. Raise this to disable the filter.
MAX_SPECIFIC_CONFLICTS = 2

_SPECIFIC_WORD_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_SPECIFIC_NUMBER_RE = re.compile(r"\b\d{2,}\b")


def _specifics(sentence: str) -> set:
    # The first character is skipped: a capital there is grammar, not a name.
    return {w.lower() for w in _SPECIFIC_WORD_RE.findall(sentence[1:])} | set(
        _SPECIFIC_NUMBER_RE.findall(sentence)
    )


def _accounts_agree(accounts: list) -> bool:
    """False when the accounts name specifics the others do not, beyond a small
    tolerance for one source simply saying more than another."""
    sets = [_specifics(a["sentence"]) for a in accounts]
    shared = set.intersection(*sets)
    union = set().union(*sets)
    return len(union - shared) <= MAX_SPECIFIC_CONFLICTS


def _split_clusters(clusters: list):
    """Partition clusters into corroborated key points and single-source claims.

    A key point carries one account per source rather than a single
    representative sentence standing in for all of them. Clustering is lexical,
    so two sentences can match on shape while disagreeing on the subject: a
    measured example paired "Trump-backed candidate wins primary for New
    Hampshire district" with the same phrasing about Oklahoma governor. Shown one
    sentence tagged with two source names, a reader cannot tell corroboration
    from coincidence. Shown both, the mismatch is obvious and a genuine match
    reads stronger.
    """
    key_points = []
    only_one_source = []
    for cluster in clusters:
        unique_sites = sorted({c["site_name"] for c in cluster})
        representative = max(cluster, key=lambda c: len(c["_kw"]))
        entry = {"sentence": representative["sentence"], "url": representative["url"]}
        if len(unique_sites) >= 2:
            accounts = []
            for name in unique_sites:
                best = max((c for c in cluster if c["site_name"] == name), key=lambda c: len(c["_kw"]))
                accounts.append({"site_name": name, "sentence": best["sentence"], "url": best["url"]})
            if _accounts_agree(accounts):
                key_points.append({**entry, "sources": unique_sites, "accounts": accounts})
            else:
                # Shaped alike but about different events. Reported as a single
                # source's claim rather than as corroboration, which is what it
                # actually is.
                only_one_source.append({**entry, "site_name": representative["site_name"]})
        else:
            only_one_source.append({**entry, "site_name": unique_sites[0]})

    key_points.sort(key=lambda kp: len(kp["sources"]), reverse=True)
    return key_points[:MAX_KEY_POINTS], only_one_source[:MAX_ONLY_ONE_SOURCE]


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)


def _prepare_search(query: str):
    """Validation shared by both search entry points."""
    sites = db.list_enabled_sites()
    if not sites:
        raise HTTPException(400, "No enabled sites to search. Toggle at least one site on.")
    query_ast = boolean_search.parse_query(query)
    if not boolean_search.has_real_term(query_ast):
        raise HTTPException(400, "Enter at least one search term (operators like AND/OR/NOT aren't a query).")
    return sites, query_ast


def _crawl_sites(sites: list, query: str, query_ast, deadline: float):
    """Yield each source's result as it lands rather than collecting them all
    first, so a caller can report progress while the crawl is still running.
    Sources that have not reported by the grace deadline are yielded last, as
    timeouts."""
    reported = set()
    executor = ThreadPoolExecutor(max_workers=min(8, len(sites)))
    try:
        futures = {
            executor.submit(_crawl_one_site_safe, site, query, query_ast, deadline): site
            for site in sites
        }
        try:
            # Workers check the deadline between fetches, but one already in
            # flight can overrun it, so wait a grace period past the budget
            # rather than indefinitely.
            for future in as_completed(futures, timeout=SEARCH_BUDGET_SECONDS + SEARCH_GRACE_SECONDS):
                result = future.result()
                reported.add(result["site_id"])
                yield result
        except FuturesTimeoutError:
            pass
    finally:
        # Do not block on stragglers. Their own deadline checks wind them down
        # within one in-flight fetch.
        executor.shutdown(wait=False, cancel_futures=True)

    for site in sites:
        if site["id"] not in reported:
            yield _site_result(site, "timeout", "Search budget ran out before this source finished", [])


def _build_report(query: str, sites: list, results: list, started: float) -> dict:
    order = {s["id"]: i for i, s in enumerate(sites)}
    results = sorted(results, key=lambda r: order.get(r["site_id"], 0))

    differences = []
    all_claims = []
    citations = []
    seen_urls = set()

    for r in results:
        # Keyed on whether anything was retrieved, not on the status label, so a
        # source cut short by the deadline still contributes what it did gather.
        if not r["docs"]:
            continue
        joined_text = "\n".join(d["text"] for d in r["docs"])
        differences.append({"site_id": r["site_id"], "name": r["name"], "summary": summarize(joined_text, max_sentences=SITE_SUMMARY_SENTENCES)})

        for doc in r["docs"]:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                citations.append({"site_name": r["name"], "url": doc["url"], "title": doc["title"], "published": doc["published"]})
            all_claims.extend(
                {"sentence": sentence, "site_name": r["name"], "site_id": r["site_id"], "url": doc["url"]}
                for sentence in top_sentences(doc["text"], max_sentences=CLAIM_SENTENCES_PER_DOC)
            )

    key_points, only_one_source = _split_clusters(_cluster_claims(all_claims))
    sites_with_content = sum(1 for r in results if r["docs"])
    overview = _build_overview(all_claims, sites_with_content)
    citations.sort(key=lambda c: c["published"] or "", reverse=True)

    return {
        "query": query,
        "overview": overview,
        "key_points": key_points,
        "differences": differences,
        "only_one_source": only_one_source,
        "citations": citations,
        "site_statuses": [{"site_id": r["site_id"], "name": r["name"], "status": r["status"], "reason": r["reason"]} for r in results],
        "stats": {
            "sites_searched": len(sites),
            "sites_with_results": sites_with_content,
            "articles_found": len(citations),
            "key_points": len(key_points),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "budget_seconds": SEARCH_BUDGET_SECONDS,
            "cut_short": sum(1 for r in results if r["status"] in ("timeout", "partial")),
        },
    }


@app.post("/api/search")
def search(payload: SearchIn):
    sites, query_ast = _prepare_search(payload.query)
    started = time.monotonic()
    results = list(_crawl_sites(sites, payload.query, query_ast, started + SEARCH_BUDGET_SECONDS))
    return _build_report(payload.query, sites, results, started)


def _sse(event: str, data) -> str:
    return "event: " + event + "\ndata: " + json.dumps(data) + "\n\n"


@app.get("/api/search/stream")
def search_stream(q: str = Query(min_length=1, max_length=200)):
    """The same search, reported source by source as it happens.

    A cold search takes most of a minute, nearly all of it spent waiting out
    other people's crawl-delays, and the blocking endpoint gives no sign of life
    until the whole thing finishes. That is indistinguishable from a hang.
    """
    def events():
        # A validation failure is sent as an event rather than raised, because
        # EventSource cannot read the body of an HTTP error: the browser would
        # only ever see "something went wrong".
        try:
            sites, query_ast = _prepare_search(q)
        except HTTPException as exc:
            yield _sse("error", {"detail": exc.detail})
            return

        started = time.monotonic()
        yield _sse("start", {
            "sources": [{"site_id": s["id"], "name": s["name"]} for s in sites],
            "budget_seconds": SEARCH_BUDGET_SECONDS,
        })

        results = []
        for result in _crawl_sites(sites, q, query_ast, started + SEARCH_BUDGET_SECONDS):
            results.append(result)
            yield _sse("source", {
                "site_id": result["site_id"],
                "name": result["name"],
                "status": result["status"],
                "reason": result["reason"],
                "documents": len(result["docs"]),
                "done": len(results),
                "total": len(sites),
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })

        yield _sse("report", _build_report(q, sites, results, started))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # No buffering: anything holding these back defeats the point.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
