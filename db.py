import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "sites.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            site_type TEXT NOT NULL DEFAULT 'search',
            search_page_url TEXT,
            param_name TEXT,
            feed_url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            tos_ack INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sites)")}
    if "enabled" not in existing_cols:
        conn.execute("ALTER TABLE sites ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            feed_url TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(site_id, feed_url)
        )
        """
    )

    # One-time migration: move any legacy single feed_url on a site row into the
    # new feeds table, so multi-feed sources and old single-feed sources look the same.
    legacy = conn.execute("SELECT id, feed_url FROM sites WHERE site_type = 'rss' AND feed_url IS NOT NULL").fetchall()
    for row in legacy:
        conn.execute(
            "INSERT OR IGNORE INTO feeds (site_id, feed_url) VALUES (?, ?)",
            (row["id"], row["feed_url"]),
        )
        conn.execute("UPDATE sites SET feed_url = NULL WHERE id = ?", (row["id"],))

    conn.commit()
    conn.close()


def _attach_feeds(conn, sites: list) -> list:
    for site in sites:
        if site["site_type"] == "rss":
            rows = conn.execute("SELECT * FROM feeds WHERE site_id = ? ORDER BY id", (site["id"],)).fetchall()
            site["feeds"] = [dict(r) for r in rows]
        else:
            site["feeds"] = []
    return sites


def list_sites():
    conn = get_conn()
    sites = [dict(r) for r in conn.execute("SELECT * FROM sites ORDER BY id").fetchall()]
    sites = _attach_feeds(conn, sites)
    conn.close()
    return sites


def add_site(
    name: str,
    site_type: str,
    tos_ack: bool,
    search_page_url: Optional[str] = None,
    param_name: Optional[str] = None,
    feed_url: Optional[str] = None,
) -> dict:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO sites (name, site_type, search_page_url, param_name, feed_url, tos_ack)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, site_type, search_page_url, param_name, feed_url, 1 if tos_ack else 0),
    )
    conn.commit()
    site_id = cur.lastrowid
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    conn.close()
    return dict(row)


def delete_site(site_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_sites_by_ids(site_ids: list) -> list:
    if not site_ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in site_ids)
    rows = conn.execute(
        f"SELECT * FROM sites WHERE id IN ({placeholders})", site_ids
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_enabled_sites() -> list:
    conn = get_conn()
    sites = [dict(r) for r in conn.execute("SELECT * FROM sites WHERE enabled = 1 ORDER BY id").fetchall()]
    sites = _attach_feeds(conn, sites)
    conn.close()
    return sites


def set_enabled(site_id: int, enabled: bool) -> Optional[dict]:
    conn = get_conn()
    conn.execute("UPDATE sites SET enabled = ? WHERE id = ?", (1 if enabled else 0, site_id))
    conn.commit()
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_feed(site_id: int, feed_url: str, label: Optional[str] = None) -> Optional[dict]:
    conn = get_conn()
    site = conn.execute("SELECT id FROM sites WHERE id = ?", (site_id,)).fetchone()
    if not site:
        conn.close()
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO feeds (site_id, feed_url, label) VALUES (?, ?, ?)",
        (site_id, feed_url, label),
    )
    conn.commit()
    feed_id = cur.lastrowid
    row = conn.execute("SELECT * FROM feeds WHERE site_id = ? AND feed_url = ?", (site_id, feed_url)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_feed(feed_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def list_feeds_for_site(site_id: int) -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM feeds WHERE site_id = ? ORDER BY id", (site_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
