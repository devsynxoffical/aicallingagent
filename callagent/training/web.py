"""Read a company's website as training material, safely.

The URL comes from a user, and the server fetching it sits inside our network. So
every request, including every redirect hop, is checked: http(s) only, public
addresses only, bounded size, bounded time. Pages are read politely: robots.txt is
honoured and a crawl stays on the site it was given.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .ingest import IngestError, Section, parse_html

USER_AGENT = "DevyTalkTrainingBot/1.0 (+reads pages a customer asked us to learn from)"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
MAX_PAGES_LIMIT = 25
TIMEOUT_SECONDS = 15.0

Resolver = Callable[[str], list[str]]
# (url) -> (status, headers, body). One request, no redirect following.
Fetcher = Callable[[str], tuple[int, dict[str, str], bytes]]


@dataclass
class Page:
    url: str
    title: str
    sections: list[Section]
    links: list[str]


def _system_resolver(host: str) -> list[str]:
    try:
        return list({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror:
        return []


def check_public_url(url: str, resolver: Resolver = _system_resolver) -> str:
    """Return the normalized URL, or raise IngestError when it must not be fetched."""
    url, _fragment = urldefrag(url.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise IngestError("only http and https addresses can be used")
    host = parts.hostname
    if not host or parts.username or parts.password:
        raise IngestError("this is not a valid website address")
    if parts.port not in (None, 80, 443):
        raise IngestError("only standard web ports (80, 443) can be used")
    addresses = resolver(host)
    if not addresses:
        raise IngestError(f"the website '{host}' could not be found")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            # Loopback, private ranges, link-local (cloud metadata), reserved: never from our servers.
            raise IngestError(f"'{host}' is not a public website address")
    return url


def _http_fetcher(url: str) -> tuple[int, dict[str, str], bytes]:
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        with client.stream("GET", url) as response:
            body = b""
            for block in response.iter_bytes():
                body += block
                if len(body) > MAX_PAGE_BYTES:
                    break
            return response.status_code, {k.lower(): v for k, v in response.headers.items()}, body[: MAX_PAGE_BYTES + 1]


def safe_get(url: str, *, fetcher: Fetcher = _http_fetcher, resolver: Resolver = _system_resolver) -> tuple[str, dict[str, str], bytes]:
    """GET a public page, re-checking the address at every redirect. Returns (final_url, headers, body)."""
    for _hop in range(MAX_REDIRECTS + 1):
        url = check_public_url(url, resolver)
        try:
            status, headers, body = fetcher(url)
        except httpx.HTTPError as e:
            raise IngestError(f"the page could not be loaded ({type(e).__name__})") from None
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            url = urljoin(url, headers["location"])
            continue
        if status >= 400:
            raise IngestError(f"the website answered with an error ({status})")
        if len(body) > MAX_PAGE_BYTES:
            raise IngestError("the page is too large to read")
        return url, headers, body
    raise IngestError("the address redirects too many times")


def read_page(url: str, *, fetcher: Fetcher = _http_fetcher, resolver: Resolver = _system_resolver) -> Page:
    final_url, headers, body = safe_get(url, fetcher=fetcher, resolver=resolver)
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type not in ("text/html", "application/xhtml+xml", "text/plain"):
        raise IngestError(f"this address is not a web page ({content_type})")
    text = body.decode(_charset(headers), errors="replace")
    if content_type == "text/plain":
        return Page(final_url, "", [Section("", text.strip())] if text.strip() else [], [])
    title, sections, links = parse_html(text)
    return Page(final_url, title, sections, [urljoin(final_url, link) for link in links])


def _charset(headers: dict[str, str]) -> str:
    for part in headers.get("content-type", "").split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            try:
                "".encode(value.strip("\"' "))
                return value.strip("\"' ")
            except LookupError:
                break
    return "utf-8"


def _robots(root: str, fetcher: Fetcher, resolver: Resolver) -> RobotFileParser:
    parser = RobotFileParser()
    try:
        _url, _headers, body = safe_get(urljoin(root, "/robots.txt"), fetcher=fetcher, resolver=resolver)
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
    except IngestError:
        parser.parse([])  # no robots.txt: nothing is disallowed
    return parser


def crawl_site(
    start_url: str,
    max_pages: int = 8,
    *,
    fetcher: Fetcher = _http_fetcher,
    resolver: Resolver = _system_resolver,
) -> list[Page]:
    """Read the start page and, breadth-first, pages it links to on the same site.

    Raises IngestError only when the start page itself cannot be read; a broken inner
    link is skipped.
    """
    max_pages = max(1, min(max_pages, MAX_PAGES_LIMIT))
    first = read_page(start_url, fetcher=fetcher, resolver=resolver)
    host = urlsplit(first.url).hostname
    robots = _robots(first.url, fetcher, resolver)
    if not robots.can_fetch(USER_AGENT, first.url):
        raise IngestError("this website does not allow automated reading of that page (robots.txt)")
    pages, seen, queue = [first], {_key(first.url), _key(start_url)}, list(first.links)
    while queue and len(pages) < max_pages:
        link = urldefrag(queue.pop(0))[0]
        parts = urlsplit(link)
        if parts.scheme not in ("http", "https") or parts.hostname != host or _key(link) in seen:
            continue
        seen.add(_key(link))
        if parts.path.lower().rsplit(".", 1)[-1] in _NOT_PAGES or not robots.can_fetch(USER_AGENT, link):
            continue
        try:
            page = read_page(link, fetcher=fetcher, resolver=resolver)
        except IngestError:
            continue
        if _key(page.url) != _key(link):
            if urlsplit(page.url).hostname != host or _key(page.url) in {_key(p.url) for p in pages}:
                continue
            seen.add(_key(page.url))
        if page.sections:
            pages.append(page)
            queue.extend(page.links)
    return pages


_NOT_PAGES = {"jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "css", "js", "zip", "mp4", "mp3", "woff", "woff2", "xml", "json"}


def _key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path.rstrip('/')}?{parts.query}".lower()


def site_sections(pages: list[Page]) -> list[Section]:
    """Sections of every page, labelled with the page they came from."""
    out: list[Section] = []
    for page in pages:
        where = page.title or urlsplit(page.url).path or page.url
        for section in page.sections:
            label = f"{where} > {section.label}" if section.label and section.label != where else where
            out.append(Section(f"{label} ({page.url})", section.text))
    return out
