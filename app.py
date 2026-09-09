import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
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
    if _hostname_only(host_header) not in _LOCAL_HOSTNAMES:
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
CLUSTER_SIMILARITY_THRESHOLD = 0.35
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
    url: str
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
    feed_url: str
    label: Optional[str] = None


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

    docs = []
    if entry.text.strip():
        docs.append({"url": search_url, "title": entry.title or site["name"], "published": entry.published, "text": entry.text})

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
    for score, c in scored:
        if per_site_count[c["site_name"]] >= MAX_OVERVIEW_PER_SITE:
            continue
        chosen.append(c["sentence"])
        per_site_count[c["site_name"]] += 1
        if len(chosen) >= OVERVIEW_SENTENCES * 2:
            break

    if not chosen:
        return no_overlap_msg
    return summarize("\n".join(chosen), max_sentences=OVERVIEW_SENTENCES)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)


@app.post("/api/search")
def search(payload: SearchIn):
    sites = db.list_enabled_sites()
    if not sites:
        raise HTTPException(400, "No enabled sites to search. Toggle at least one site on.")

    query_ast = boolean_search.parse_query(payload.query)
    if not boolean_search.has_real_term(query_ast):
        raise HTTPException(400, "Enter at least one search term (operators like AND/OR/NOT alone aren't a query).")

    started = time.monotonic()
    deadline = started + SEARCH_BUDGET_SECONDS

    results = []
    reported = set()
    executor = ThreadPoolExecutor(max_workers=min(8, len(sites)))
    try:
        futures = {
            executor.submit(_crawl_one_site_safe, site, payload.query, query_ast, deadline): site
            for site in sites
        }
        try:
            # Workers check the deadline between fetches, but one already in
            # flight can overrun it, so wait a grace period past the budget
            # rather than indefinitely.
            for future in as_completed(futures, timeout=SEARCH_BUDGET_SECONDS + SEARCH_GRACE_SECONDS):
                result = future.result()
                results.append(result)
                reported.add(result["site_id"])
        except FuturesTimeoutError:
            pass
    finally:
        # Do not block on stragglers. Anything that has not reported is reported
        # as timed out, and its thread is left to wind down on its own.
        executor.shutdown(wait=False, cancel_futures=True)

    for site in sites:
        if site["id"] not in reported:
            results.append(_site_result(site, "timeout", "Search budget ran out before this source finished", []))

    order = {s["id"]: i for i, s in enumerate(sites)}
    results.sort(key=lambda r: order.get(r["site_id"], 0))

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
            for sentence in top_sentences(doc["text"], max_sentences=CLAIM_SENTENCES_PER_DOC):
                all_claims.append({"sentence": sentence, "site_name": r["name"], "site_id": r["site_id"], "url": doc["url"]})

    clusters = _cluster_claims(all_claims)

    key_points = []
    only_one_source = []
    for cluster in clusters:
        unique_sites = sorted({c["site_name"] for c in cluster})
        representative = max(cluster, key=lambda c: len(c["_kw"]))
        entry = {"sentence": representative["sentence"], "url": representative["url"]}
        if len(unique_sites) >= 2:
            key_points.append({**entry, "sources": unique_sites})
        else:
            only_one_source.append({**entry, "site_name": unique_sites[0]})

    key_points.sort(key=lambda kp: len(kp["sources"]), reverse=True)
    key_points = key_points[:MAX_KEY_POINTS]
    only_one_source = only_one_source[:MAX_ONLY_ONE_SOURCE]

    sites_with_content = sum(1 for r in results if r["docs"])
    overview = _build_overview(all_claims, sites_with_content)

    citations.sort(key=lambda c: c["published"] or "", reverse=True)

    site_statuses = [{"site_id": r["site_id"], "name": r["name"], "status": r["status"], "reason": r["reason"]} for r in results]
    cut_short = [r for r in results if r["status"] in ("timeout", "partial")]

    return {
        "query": payload.query,
        "overview": overview,
        "key_points": key_points,
        "differences": differences,
        "only_one_source": only_one_source,
        "citations": citations,
        "site_statuses": site_statuses,
        "stats": {
            "sites_searched": len(sites),
            "sites_with_results": sites_with_content,
            "articles_found": len(citations),
            "key_points": len(key_points),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "budget_seconds": SEARCH_BUDGET_SECONDS,
            "cut_short": len(cut_short),
        },
    }
