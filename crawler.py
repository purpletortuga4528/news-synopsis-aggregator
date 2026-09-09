import email.utils
import ipaddress
import socket
import threading
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import defusedxml.ElementTree as ET
import requests
import urllib3.util.connection as _urllib3_connection
from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException

# --- DNS-rebinding-proof SSRF protection --------------------------------
# Resolving a hostname and checking it's public, then letting `requests` do
# its own separate DNS lookup a moment later to actually connect, leaves a
# TOCTOU race: a malicious DNS server can hand back a public IP for the first
# lookup and a private one for the second (a very short TTL makes this
# practical). Closing that requires pinning the exact IP we validated for the
# real connection. This patches urllib3's raw TCP connect step only. SNI,
# the Host header, and certificate hostname verification all still use the
# original hostname untouched, so this cannot weaken TLS verification, it can
# only pin which address the TCP socket actually dials. Thread-local so
# concurrent crawls (this app fans out over a ThreadPoolExecutor) can't step
# on each other's pin.
_pin_state = threading.local()
_unpinned_create_connection = _urllib3_connection.create_connection


def _pinned_create_connection(address, *args, **kwargs):
    host, port = address
    if getattr(_pin_state, "host", None) == host and getattr(_pin_state, "ip", None):
        address = (_pin_state.ip, port)
    return _unpinned_create_connection(address, *args, **kwargs)


_urllib3_connection.create_connection = _pinned_create_connection

_NON_ARTICLE_PATH_HINTS = (
    "/about", "/contact", "/privacy", "/terms", "/cookie", "/login", "/signin", "/signup",
    "/register", "/subscribe", "/account", "/careers", "/jobs", "/advertise", "/help",
    "/faq", "/sitemap", "/rss", "/newsletter", "/settings", "/preferences",
)

USER_AGENT = "SynopsisCrawler/1.0 (+respects robots.txt; educational/personal use)"
REQUEST_TIMEOUT = 10
DEFAULT_MIN_DELAY = 2.0  # seconds between requests to the same domain if robots.txt sets no crawl-delay
MAX_CONTENT_BYTES = 3_000_000
MAX_REDIRECTS = 5

_robots_cache: dict = {}
_robots_lock = threading.Lock()
_domain_last_request: dict = {}
_domain_locks: dict = {}


def _domain_lock(domain: str) -> threading.Lock:
    with _robots_lock:
        if domain not in _domain_locks:
            _domain_locks[domain] = threading.Lock()
        return _domain_locks[domain]


DNS_TIMEOUT = 5.0


def _resolve_ips(hostname: str):
    """socket.getaddrinfo has no built-in timeout. Without bounding it, a
    hostname whose DNS server never responds would hang the calling thread
    forever, and since /api/search waits on every site's crawl to finish, one
    such URL would hang the entire search. Run it in a throwaway thread so a
    stuck lookup can be abandoned instead of blocked on."""
    def _lookup():
        try:
            return {info[4][0] for info in socket.getaddrinfo(hostname, None)}
        except (socket.gaierror, UnicodeError, OSError):
            return None

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_lookup)
    try:
        return future.result(timeout=DNS_TIMEOUT)
    except FutureTimeoutError:
        return None
    finally:
        executor.shutdown(wait=False)


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _resolve_safe_ip(hostname: str) -> Optional[str]:
    """Resolve hostname and return one validated-public IP to pin the actual
    connection to, or None if unresolvable or any resolved address is
    private/loopback/link-local/reserved."""
    ips = _resolve_ips(hostname)
    if not ips:
        return None
    if not all(_is_public_ip(ip) for ip in ips):
        return None
    return sorted(ips)[0]


def _is_ssrf_safe(url: str) -> bool:
    """Cheap boolean check (no pinning) used only for up-front, accurately-worded
    rejection messages before we even attempt a fetch. The actual connection
    path always goes through _safe_get, which pins the specific IP it validates
    to close the DNS-rebinding gap this quick check alone can't."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    return _resolve_safe_ip(hostname) is not None


def _safe_get(url: str, headers: dict, timeout: float, stream: bool = False):
    """requests.get with manually-validated redirect following. Every hop
    (including the initial URL) is resolved and validated before connecting,
    since a redirect can point at an internal address just as easily as the
    original URL can, and the validated IP is pinned for the actual connection
    (see _pinned_create_connection) so a second, differently-answered DNS
    lookup can't undo the check. Returns (response, None) on success or
    (None, error_message)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None, "Blocked: unsupported URL"
        safe_ip = _resolve_safe_ip(parsed.hostname)
        if not safe_ip:
            return None, "Blocked: URL resolves to a non-public address"
        _pin_state.host = parsed.hostname
        _pin_state.ip = safe_ip
        try:
            resp = requests.get(current, headers=headers, timeout=timeout, stream=stream, allow_redirects=False)
        except requests.RequestException as exc:
            return None, str(exc)
        finally:
            _pin_state.host = None
            _pin_state.ip = None
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            next_url = urllib.parse.urljoin(current, resp.headers["Location"])
            resp.close()
            current = next_url
            continue
        return resp, None
    return None, "Too many redirects"


def _get_robot_parser(domain: str, scheme: str) -> urllib.robotparser.RobotFileParser:
    with _robots_lock:
        cached = _robots_cache.get(domain)
    if cached is not None and (time.time() - cached[1]) < 3600:
        return cached[0]

    rp = urllib.robotparser.RobotFileParser()
    robots_url = f"{scheme}://{domain}/robots.txt"
    resp, _err = _safe_get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    if resp is None:
        # If robots.txt is unreachable (or blocked as unsafe), fail closed: do not crawl.
        rp.disallow_all = True
    else:
        if resp.status_code in (401, 403):
            rp.disallow_all = True
        elif resp.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(resp.text.splitlines())
        resp.close()

    with _robots_lock:
        _robots_cache[domain] = (rp, time.time())
    return rp


def is_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    rp = _get_robot_parser(parsed.netloc, parsed.scheme or "https")
    return rp.can_fetch(USER_AGENT, url)


def _crawl_delay_for(url: str) -> float:
    parsed = urllib.parse.urlparse(url)
    rp = _get_robot_parser(parsed.netloc, parsed.scheme or "https")
    delay = rp.crawl_delay(USER_AGENT)
    if delay is not None:
        return max(float(delay), 0.5)
    req_rate = rp.request_rate(USER_AGENT)
    if req_rate is not None and req_rate.requests:
        return max(req_rate.seconds / req_rate.requests, 0.5)
    return DEFAULT_MIN_DELAY


def _polite_wait(url: str):
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc
    delay = _crawl_delay_for(url)
    lock = _domain_lock(domain)
    with lock:
        last = _domain_last_request.get(domain, 0.0)
        wait_needed = delay - (time.time() - last)
        if wait_needed > 0:
            time.sleep(wait_needed)
        _domain_last_request[domain] = time.time()


@dataclass
class FetchResult:
    url: str
    ok: bool
    text: str = ""
    title: str = ""
    published: Optional[str] = None
    links: list = field(default_factory=list)
    error: Optional[str] = None
    blocked_by_robots: bool = False


def _parse_date_guess(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(" ", strip=True)
        if text:
            return text
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _extract_published(soup: BeautifulSoup) -> Optional[str]:
    meta_candidates = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "publish-date"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for tag_name, attrs in meta_candidates:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get("content"):
            parsed = _parse_date_guess(tag["content"])
            if parsed:
                return parsed
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        parsed = _parse_date_guess(time_tag["datetime"])
        if parsed:
            return parsed
    return None


@dataclass
class RawFetch:
    ok: bool
    content: bytes = b""
    content_type: str = ""
    error: Optional[str] = None
    blocked_by_robots: bool = False


def _fetch_raw(url: str, accept: str = "text/html") -> RawFetch:
    # Checked directly (not just implicitly through a failed robots.txt fetch) so
    # the reported reason is accurate: this is our own safety policy refusing to
    # connect, not the target site disallowing the crawl.
    if not _is_ssrf_safe(url):
        return RawFetch(ok=False, error="Blocked: URL resolves to a non-public address")
    if not is_allowed(url):
        return RawFetch(ok=False, blocked_by_robots=True, error="Disallowed by robots.txt")

    _polite_wait(url)

    resp, err = _safe_get(url, headers={"User-Agent": USER_AGENT, "Accept": accept}, timeout=REQUEST_TIMEOUT, stream=True)
    if resp is None:
        return RawFetch(ok=False, error=err)
    try:
        content = resp.raw.read(MAX_CONTENT_BYTES, decode_content=True)
        resp.close()
        if resp.status_code >= 400:
            return RawFetch(ok=False, error=f"HTTP {resp.status_code}")
        return RawFetch(ok=True, content=content, content_type=resp.headers.get("Content-Type", ""))
    except requests.RequestException as exc:
        return RawFetch(ok=False, error=str(exc))


def fetch_and_extract(url: str, same_domain_only_from: Optional[str] = None) -> FetchResult:
    raw = _fetch_raw(url, accept="text/html")
    if not raw.ok:
        return FetchResult(url=url, ok=False, blocked_by_robots=raw.blocked_by_robots, error=raw.error)
    if "html" not in raw.content_type and raw.content_type:
        return FetchResult(url=url, ok=False, error=f"Unsupported content-type: {raw.content_type}")
    content = raw.content

    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    title = _extract_title(soup)
    published = _extract_published(soup)

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "form", "iframe"]):
        tag.decompose()

    text_parts = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        chunk = el.get_text(" ", strip=True)
        if chunk:
            text_parts.append(chunk)
    page_text = " ".join(text_parts)

    # Some sites put body text in divs/spans rather than p/li (e.g. quote blocks,
    # card layouts). Fall back to whole-page text if the targeted tags yielded little.
    if len(page_text) < 200:
        whole_text = soup.get_text(" ", strip=True)
        if len(whole_text) > len(page_text):
            page_text = whole_text

    links = []
    base_domain = urllib.parse.urlparse(same_domain_only_from or url).netloc
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(url, href)
        parsed_link = urllib.parse.urlparse(absolute)
        if parsed_link.scheme not in ("http", "https"):
            continue
        if parsed_link.netloc != base_domain:
            continue
        absolute = absolute.split("#")[0]
        if absolute in seen or absolute == url:
            continue
        # Skip obvious non-article boilerplate (nav/footer links) so search-result
        # follow-up fetches spend their budget on actual content, not "About Us" pages.
        path_lower = parsed_link.path.lower()
        if any(seg in path_lower for seg in _NON_ARTICLE_PATH_HINTS):
            continue
        seen.add(absolute)
        links.append(absolute)

    return FetchResult(url=url, ok=True, text=page_text, title=title, published=published, links=links)


@dataclass
class FeedItem:
    title: str
    description: str
    link: str
    published: Optional[str] = None


@dataclass
class FeedResult:
    ok: bool
    items: list = field(default_factory=list)
    error: Optional[str] = None
    blocked_by_robots: bool = False


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _safe_link(link: str) -> str:
    """A feed's <link> is publisher-controlled but not trustworthy. Reject
    anything that isn't a plain http(s) URL so a malicious or compromised feed
    can't smuggle a javascript: URI (or similar) into a citation link that later
    gets rendered as a clickable href in the browser."""
    link = link.strip()
    if not link:
        return ""
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return link


def fetch_feed(url: str) -> FeedResult:
    """Fetch and parse an RSS 2.0 or Atom feed. No query parameter involved:
    feeds are fixed, publisher-controlled lists of items."""
    raw = _fetch_raw(url, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
    if not raw.ok:
        return FeedResult(ok=False, blocked_by_robots=raw.blocked_by_robots, error=raw.error)

    try:
        root = ET.fromstring(raw.content)
    except ET.ParseError as exc:
        return FeedResult(ok=False, error=f"Could not parse feed XML: {exc}")
    except DefusedXmlException as exc:
        return FeedResult(ok=False, error=f"Rejected unsafe feed XML: {exc}")

    items = []
    root_tag = _strip_ns(root.tag)

    if root_tag == "rss":
        for entry in root.iter():
            if _strip_ns(entry.tag) != "item":
                continue
            title = description = link = ""
            published = None
            for child in entry:
                name = _strip_ns(child.tag)
                if name == "title":
                    title = (child.text or "").strip()
                elif name in ("description", "encoded"):
                    description = (child.text or "").strip()
                elif name == "link":
                    link = (child.text or "").strip()
                elif name in ("pubDate", "date"):
                    published = _parse_date_guess(child.text or "")
            if title or description:
                items.append(FeedItem(title=title, description=description, link=_safe_link(link), published=published))
    elif root_tag == "feed":  # Atom
        for entry in root:
            if _strip_ns(entry.tag) != "entry":
                continue
            title = description = link = ""
            published = None
            for child in entry:
                name = _strip_ns(child.tag)
                if name == "title":
                    title = (child.text or "").strip()
                elif name in ("summary", "content"):
                    description = (child.text or "").strip()
                elif name == "link":
                    link = child.attrib.get("href", "").strip()
                elif name in ("published", "updated"):
                    published = _parse_date_guess(child.text or "")
            if title or description:
                items.append(FeedItem(title=title, description=description, link=_safe_link(link), published=published))
    else:
        return FeedResult(ok=False, error=f"Unrecognized feed format (root tag: {root_tag})")

    return FeedResult(ok=True, items=items)
