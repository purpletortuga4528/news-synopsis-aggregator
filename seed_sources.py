"""Populate sites.db with a starter set of news sources.

Run this after a fresh clone if you'd rather not add sources by hand through
the UI: `python seed_sources.py`

The sources listed in seed_sources.json are all official RSS/Atom feeds
published directly by the outlet for automated consumption. That's different
from search-page scraping, but it's still worth pausing on: by running this
script you are making the same attestation the app's UI checkbox asks for
when adding a source by hand, that you've reviewed each outlet's Terms of
Service and are satisfied automated access for personal search/summarization
is permitted. This script only lists sources; it doesn't research their ToS
for you.

Safe to re-run: sources that already exist (matched by name) are left alone,
and each candidate feed is verified live before being added, so a source
whose feed has moved or gone offline since this list was written is skipped
with a clear reason instead of silently added as dead weight.
"""
import json
import sys
from pathlib import Path

import db
import crawler

SEED_FILE = Path(__file__).parent / "seed_sources.json"


def main():
    if "--yes" not in sys.argv:
        print(__doc__)
        answer = input("Add these starter sources now? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return

    with open(SEED_FILE, encoding="utf-8") as f:
        seed_sources = json.load(f)

    db.init_db()
    existing_names = {s["name"] for s in db.list_sites()}

    for source in seed_sources:
        name = source["name"]
        if name in existing_names:
            print(f"[skip] {name}: a source with this name already exists")
            continue

        working_feeds = []
        for feed_url in source["feeds"]:
            result = crawler.fetch_feed(feed_url)
            if result.ok and result.items:
                working_feeds.append(feed_url)
            else:
                reason = "blocked by robots.txt" if result.blocked_by_robots else (result.error or "no items")
                print(f"  [feed skipped] {feed_url}: {reason}")

        if not working_feeds:
            print(f"[skip] {name}: none of its feeds are currently reachable")
            continue

        site = db.add_site(name, "rss", tos_ack=True)
        for feed_url in working_feeds:
            db.add_feed(site["id"], feed_url)
        print(f"[added] {name}: {len(working_feeds)}/{len(source['feeds'])} feeds")

    print("Done.")


if __name__ == "__main__":
    main()
