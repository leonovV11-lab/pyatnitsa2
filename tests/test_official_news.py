import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from official_news import NewsSource, fetch_official_news, parse_feed

NVIDIA = NewsSource(
    "nvidia", "NVIDIA Newsroom", "https://nvidianews.nvidia.com/cats/press_release.xml",
    "nvidianews.nvidia.com", "/news/",
)
FED = NewsSource(
    "fed", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
    "www.federalreserve.gov", "/newsevents/pressreleases/",
)
NOW = datetime(2026, 11, 24, tzinfo=UTC).timestamp()


def rss(*items):
    return ("<rss version='2.0'><channel>" + "".join(items) + "</channel></rss>").encode()


def entry(path="/news/source-statement", published="Mon, 23 Nov 2026 13:55:00 GMT", description="First version"):
    return (
        "<item><title> Fictional company announcement </title>"
        f"<link>{path}</link><pubDate>{published}</pubDate>"
        f"<description>{description}</description></item>"
    )


class Response:
    def __init__(self, status, data=b"", headers=None):
        self.status = status
        self.data = data
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, size):
        for i in range(0, len(self.data), size):
            await asyncio.sleep(0)
            yield self.data[i:i + size]


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


class ParseOfficialFeedTests(unittest.TestCase):
    def test_rss_records_source_time_and_revision(self):
        first, = parse_feed(rss(entry()), NVIDIA, fetched_at=NOW)
        revised, = parse_feed(rss(entry(description="A corrected detail")), NVIDIA, fetched_at=NOW + 10)
        self.assertEqual(first.url, "https://nvidianews.nvidia.com/news/source-statement")
        self.assertEqual(first.title, "Fictional company announcement")
        self.assertEqual((first.source, first.publisher), ("nvidia", "NVIDIA Newsroom"))
        self.assertEqual(first.published_at, datetime(2026, 11, 23, 13, 55, tzinfo=UTC).timestamp())
        self.assertEqual(first.fetched_at, NOW)
        self.assertEqual(first.id, revised.id)
        self.assertNotEqual(first.revision, revised.revision)

    def test_untrusted_non_article_and_future_links_are_ignored(self):
        feed = rss(
            entry("https://nvidianews.nvidia.com.invalid/news/claimed"),
            entry("https://nvidianews.nvidia.com@other.invalid/news/claimed"),
            entry("https://nvidianews.nvidia.com:81/news/claimed"),
            entry("https://nvidianews.nvidia.com/news/../rss"),
            entry("https://nvidianews.nvidia.com/cats/corporate.xml"),
            entry(published="Wed, 25 Nov 2026 00:00:01 GMT"),
            entry(published="upcoming Monday"),
            entry(published="Mon, 23 Nov 2026 13:55:00"),
            entry(),
        )
        items = parse_feed(feed, NVIDIA, fetched_at=NOW)
        self.assertEqual([item.url for item in items], ["https://nvidianews.nvidia.com/news/source-statement"])

    def test_atom_ignores_entry_without_publication_timestamp(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Fictional company announcement</title>
          <link rel="alternate" href="/news/source-statement"/>
          <published>2026-11-23T14:55:00+01:00</published></entry>
          <entry><title>Untimed</title><link href="/news/untimed"/>
          <updated>2026-11-23T15:00:00Z</updated></entry>
        </feed>'''
        item, = parse_feed(feed, NVIDIA, fetched_at=NOW)
        self.assertEqual(item.published_at, datetime(2026, 11, 23, 13, 55, tzinfo=UTC).timestamp())

    def test_xml_declarations_and_non_feed_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "text">]><rss>&x;</rss>', NVIDIA, fetched_at=NOW)
        with self.assertRaises(ValueError):
            parse_feed('<!DOCTYPE rss [<!ENTITY x "text">]><rss>&x;</rss>'.encode("utf-16le"), NVIDIA, fetched_at=NOW)
        with self.assertRaises(ValueError):
            parse_feed(b"<html><head></head></html>", NVIDIA, fetched_at=NOW)

    def test_reused_release_link_keeps_separate_event_ids(self):
        source = NewsSource("bls", "BLS", "https://www.bls.gov/feed/cpi.rss", "www.bls.gov", "/news.release/", True)
        newest, oldest = parse_feed(rss(entry("/news.release/cpi.htm"), entry(
            "/news.release/cpi.htm", "Mon, 23 Nov 2026 12:00:00 GMT",
        )), source, fetched_at=NOW)
        self.assertNotEqual(newest.id, oldest.id)


class FetchOfficialFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_failure_isolated_and_redirect_not_followed(self):
        session = Session({
            NVIDIA.feed_url: Response(200, rss(entry())),
            FED.feed_url: Response(302, headers={"Location": "https://other.invalid/feed"}),
        })
        with patch("official_news.SOURCES", (NVIDIA, FED)), patch("official_news.time.time", return_value=NOW):
            items, health = await fetch_official_news(session)
        self.assertEqual([item.source for item in items], ["nvidia"])
        self.assertEqual(health, {"nvidia": "ok", "fed": "http_302"})
        self.assertEqual({url for url, _ in session.calls}, {NVIDIA.feed_url, FED.feed_url})
        self.assertTrue(all(options["allow_redirects"] is False for _, options in session.calls))

    async def test_oversize_body_and_invalid_xml_get_health_errors(self):
        session = Session({NVIDIA.feed_url: Response(200, b"x" * 65), FED.feed_url: Response(200, b"<html/>")})
        with patch("official_news.SOURCES", (NVIDIA, FED)), patch("official_news.MAX_FEED_BYTES", 64):
            items, health = await fetch_official_news(session)
        self.assertEqual(items, [])
        self.assertEqual(health, {"nvidia": "too_large", "fed": "invalid_feed"})
