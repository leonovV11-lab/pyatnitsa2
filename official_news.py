"""Read time-stamped publication headlines from known first-party feeds."""

import asyncio
import json
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import cache
from hashlib import sha256
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import aiohttp
import certifi

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS_PER_SOURCE = 40
FEED_TIMEOUT = aiohttp.ClientTimeout(total=10)
ATOM = "{http://www.w3.org/2005/Atom}"
XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


@dataclass(frozen=True)
class NewsSource:
    slug: str
    publisher: str
    feed_url: str
    article_host: str
    path_prefix: str
    reuse_article_url: bool = False
    additional_article_prefixes: tuple[str, ...] = ()
    upgrade_legacy_http: bool = False
    article_pattern: re.Pattern[str] | None = None
    feed_encoding: str = "utf-8-sig"


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    publisher: str
    title: str
    url: str
    published_at: float
    fetched_at: float
    revision: str


SOURCES = (
    NewsSource("nvidia", "NVIDIA Newsroom", "https://nvidianews.nvidia.com/cats/press_release.xml",
               "nvidianews.nvidia.com", "/news/"),
    NewsSource("fed", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
               "www.federalreserve.gov", "/newsevents/pressreleases/"),
    NewsSource("ecb", "European Central Bank", "https://www.ecb.europa.eu/rss/press.html",
               "www.ecb.europa.eu", "/press/"),
    NewsSource("bls", "U.S. Bureau of Labor Statistics", "https://www.bls.gov/feed/cpi.rss",
               "www.bls.gov", "/news.release/", reuse_article_url=True),
    NewsSource("sec", "U.S. Securities and Exchange Commission", "https://www.sec.gov/news/pressreleases.rss",
               "www.sec.gov", "/newsroom/press-releases/"),
    NewsSource("boe", "Bank of England", "https://www.bankofengland.co.uk/rss/news",
               "www.bankofengland.co.uk", "/monetary-policy-summary-and-minutes/",
               additional_article_prefixes=("/news/",)),
    # The HTTPS BoJ feed still publishes its own article links with the old scheme.
    NewsSource("boj", "Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml",
               "www.boj.or.jp", "/en/mopo/", upgrade_legacy_http=True),
    NewsSource("eia", "U.S. Energy Information Administration", "https://www.eia.gov/rss/todayinenergy.xml",
               "www.eia.gov", "/todayinenergy/",
               article_pattern=re.compile(r"/todayinenergy/detail\.php\?id=[1-9][0-9]*"),
               feed_encoding="iso-8859-1"),
    NewsSource("unpress", "United Nations Meetings Coverage", "https://press.un.org/en/rss.xml",
               "press.un.org", "/en/", article_pattern=re.compile(r"/en/[0-9]{4}/[a-z0-9_-]+\.doc\.htm")),
    NewsSource("eucommission", "European Commission Press Corner", "https://ec.europa.eu/commission/presscorner/api/rss",
               "ec.europa.eu", "/commission/presscorner/detail/en/"),
    NewsSource("bea", "U.S. Bureau of Economic Analysis", "https://apps.bea.gov/rss/rss.xml",
               "www.bea.gov", "/news/"),
)


def _text(element: ElementTree.Element, tag: str) -> str:
    value = element.find(tag)
    return " ".join("".join(value.itertext()).split()) if value is not None else ""


def _article_url(raw: str, source: NewsSource) -> str:
    if not raw or len(raw) > 2048 or "\\" in raw or any(character.isspace() for character in raw):
        return ""
    try:
        link = urlsplit(urljoin(source.feed_url, raw))
        is_https = link.scheme == "https" and link.port in {None, 443}
        is_legacy_http = source.upgrade_legacy_http and link.scheme == "http" and link.port is None
        if (not (is_https or is_legacy_http) or link.hostname != source.article_host
                or link.username is not None or link.password is not None):
            return ""
    except ValueError:
        return ""
    # ECB emits article URLs with two leading slashes in the HTTPS path.
    raw_path = "/" + link.path.lstrip("/") if source.slug == "ecb" else link.path
    path = unquote(raw_path)
    prefixes = (source.path_prefix, *source.additional_article_prefixes)
    if (not path.startswith(prefixes) or path in prefixes
            or "\\" in path or any(segment in {".", ".."} for segment in path.split("/"))
            or any(ord(character) < 32 for character in path)):
        return ""
    if source.article_pattern is not None and not source.article_pattern.fullmatch(
        urlunsplit(("", "", raw_path, link.query, ""))
    ):
        return ""
    return urlunsplit(("https", source.article_host, raw_path, link.query, ""))


def _published_at(raw: str) -> float | None:
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _digest(*values: str | float) -> str:
    return sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _item_from_entry(
    entry: ElementTree.Element, source: NewsSource, atom: str, fetched_at: float,
) -> NewsItem | None:
    title = _text(entry, f"{atom}title")
    if not title:
        return None
    if atom:
        link = next((value.get("href", "") for value in entry.findall(f"{atom}link")
                     if value.get("rel", "alternate") == "alternate"), "")
        published = _text(entry, f"{atom}published")
        details = _text(entry, f"{atom}summary") or _text(entry, f"{atom}content")
        updated = _text(entry, f"{atom}updated")
    else:
        link = _text(entry, "link")
        published = _text(entry, "pubDate")
        details = _text(entry, "description")
        updated = ""
    url = _article_url(link, source)
    stamp = _published_at(published) if published else None
    if not url or stamp is None or not 0 < stamp <= fetched_at:
        return None
    # BLS republishes some recurring releases at a fixed latest-release URL.
    identity = _digest(source.slug, url, stamp) if source.reuse_article_url else _digest(source.slug, url)
    return NewsItem(id=identity, source=source.slug, publisher=source.publisher, title=title[:500], url=url,
                    published_at=stamp, fetched_at=fetched_at, revision=_digest(title, stamp, details, updated))


def parse_feed(payload: bytes, source: NewsSource, *, fetched_at: float) -> list[NewsItem]:
    """Parse a bounded RSS/Atom feed using its configured encoding."""
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds size limit")
    text = payload.decode(source.feed_encoding)
    if "\0" in text or XML_DECLARATION.search(text):
        raise ValueError("Feed contains an unsupported XML declaration")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid XML feed") from exc
    if root.tag == "rss" and root.find("channel") is not None:
        entries, atom = root.findall("./channel/item"), ""
    elif root.tag == f"{ATOM}feed":
        entries, atom = root.findall(f"{ATOM}entry"), ATOM
    else:
        raise ValueError("Expected an RSS or Atom feed")
    by_id: dict[str, NewsItem] = {}
    for entry in entries:
        item = _item_from_entry(entry, source, atom, fetched_at)
        if item and (item.id not in by_id or by_id[item.id].published_at < item.published_at):
            by_id[item.id] = item
    return sorted(by_id.values(), key=lambda item: (item.published_at, item.id), reverse=True)[:MAX_ITEMS_PER_SOURCE]


@cache
def _europa_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


async def _fetch_feed(session: aiohttp.ClientSession, source: NewsSource) -> tuple[list[NewsItem], str]:
    payload = bytearray()
    try:
        async with session.get(
            source.feed_url, timeout=FEED_TIMEOUT, allow_redirects=False, auto_decompress=False,
            ssl=_europa_tls_context() if source.slug in {"ecb", "eucommission"} else True,
            headers={"Accept-Encoding": "identity", "User-Agent": "Pyatnitsa2-public-news/1.0"},
        ) as response:
            if response.status != 200:
                return [], f"http_{response.status}"
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return [], "unsupported_encoding"
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(payload) + len(chunk) > MAX_FEED_BYTES:
                    return [], "too_large"
                payload.extend(chunk)
    except TimeoutError:
        return [], "timeout"
    except (aiohttp.ClientError, OSError):
        return [], "unavailable"
    try:
        items = parse_feed(bytes(payload), source, fetched_at=time.time())
    except ValueError:
        return [], "invalid_feed"
    return items, "ok" if items else "no_verified_items"


async def fetch_official_news(
    session: aiohttp.ClientSession, *, sources: tuple[NewsSource, ...] = SOURCES,
) -> tuple[list[NewsItem], dict[str, str]]:
    batches = await asyncio.gather(*(_fetch_feed(session, source) for source in sources))
    items: list[NewsItem] = []
    health: dict[str, str] = {}
    for source, (batch, status) in zip(sources, batches, strict=True):
        items.extend(batch)
        health[source.slug] = status
    items.sort(key=lambda item: (item.published_at, item.id), reverse=True)
    return items, health
