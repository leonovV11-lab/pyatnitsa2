import asyncio
import ssl
import unittest
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

from aiohttp import ClientSession

from official_news import SOURCES, NewsSource, fetch_official_news, parse_feed

NVIDIA = NewsSource(
    "nvidia", "NVIDIA Newsroom", "https://nvidianews.nvidia.com/cats/press_release.xml",
    "nvidianews.nvidia.com", "/news/",
)
FED = NewsSource(
    "fed", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
    "www.federalreserve.gov", "/newsevents/pressreleases/",
)
NOW = datetime(2026, 11, 24, tzinfo=UTC).timestamp()
REGISTERED_SOURCES = {source.slug: source for source in SOURCES}


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

    def test_sec_releases_require_official_host_and_release_paths(self):
        source = REGISTERED_SOURCES["sec"]
        self.assertEqual(source.feed_url, "https://www.sec.gov/news/pressreleases.rss")
        release = "https://www.sec.gov/newsroom/press-releases/2026-119-example"
        item, = parse_feed(rss(
            entry(release, "Mon, 23 Nov 2026 09:55:00 -0500"),
            entry("http://www.sec.gov/newsroom/press-releases/2026-120-claimed"),
            entry(release.replace("www.sec.gov", "www.sec.gov.invalid")),
            entry("/about/claimed-release"),
        ), source, fetched_at=NOW)
        self.assertEqual((item.source, item.url), ("sec", release))
        self.assertEqual(item.published_at, datetime(2026, 11, 23, 14, 55, tzinfo=UTC).timestamp())

    def test_boe_accepts_rate_decision_and_news_paths_with_zulu_time(self):
        source = REGISTERED_SOURCES["boe"]
        self.assertEqual(source.feed_url, "https://www.bankofengland.co.uk/rss/news")
        rate_decision = "/monetary-policy-summary-and-minutes/2026/november-2026"
        press_release = "/news/2026/november/example"
        items = parse_feed(rss(
            entry(rate_decision, "Mon, 23 Nov 2026 12:00:00 Z"),
            entry(press_release),
            entry("/monetary-policy-summary-and-minutes-adjacent/2026/example"),
            entry("/minutes/2026/november/example"),
        ), source, fetched_at=NOW)
        self.assertEqual({item.url for item in items}, {
            f"https://www.bankofengland.co.uk{rate_decision}", f"https://www.bankofengland.co.uk{press_release}",
        })
        self.assertEqual(items[-1].published_at, datetime(2026, 11, 23, 12, tzinfo=UTC).timestamp())

    def test_boj_canonicalizes_its_legacy_links_and_filters_other_paths(self):
        source = REGISTERED_SOURCES["boj"]
        self.assertEqual(source.feed_url, "https://www.boj.or.jp/en/rss/whatsnew.xml")
        release = "http://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/example.pdf"
        item, = parse_feed(rss(
            entry(release, "Tue, 24 Nov 2026 08:50:00 +0900"),
            entry("http://www.boj.or.jp:80/en/mopo/mpmdeci/mpr_2026/claimed.pdf"),
            entry(release.replace("www.boj.or.jp", "www.boj.or.jp@other.invalid")),
            entry(release.replace("www.boj.or.jp", "www.boj.or.jp.invalid")),
            entry("/en/statistics/bis/example.htm"),
        ), source, fetched_at=NOW)
        self.assertEqual((item.source, item.url), ("boj", release.replace("http:", "https:")))
        self.assertEqual(item.published_at, datetime(2026, 11, 23, 23, 50, tzinfo=UTC).timestamp())

    def test_ecb_canonicalizes_its_duplicate_leading_slashes(self):
        source = REGISTERED_SOURCES["ecb"]
        path = "/press/pr/date/2026/html/example.en.html"
        base = "https://www.ecb.europa.eu"
        item, = parse_feed(rss(
            entry(f"{base}/{path}"),
            entry(f"{base}.invalid/{path}"),
            entry(f"{base}//press/%2e%2e/claimed.en.html"),
        ), source, fetched_at=NOW)
        canonical, = parse_feed(rss(entry(f"{base}{path}")), source, fetched_at=NOW)
        self.assertEqual((item.source, item.url), ("ecb", f"{base}{path}"))
        self.assertEqual(item.id, canonical.id)

    def test_eia_requires_a_unique_article_id(self):
        source = REGISTERED_SOURCES["eia"]
        base = "/todayinenergy/detail.php"
        item, = parse_feed(rss(
            entry(f"{base}?id=68504", "Mon, 23 Nov 2026 09:00:00 EST"),
            entry(f"{base}?id="),
            entry(f"{base}?id=0"),
            entry(f"{base}?id=68504&amp;id=68505"),
            entry("/todayinenergy/about.php?id=68504"),
        ), source, fetched_at=NOW)
        self.assertEqual(item.url, f"https://www.eia.gov{base}?id=68504")
        self.assertEqual(item.published_at, datetime(2026, 11, 23, 14, tzinfo=UTC).timestamp())

    def test_eia_decodes_the_publisher_encoding_without_allowing_xml_entities(self):
        source = REGISTERED_SOURCES["eia"]
        payload = rss(entry("/todayinenergy/detail.php?id=68504")).decode()
        payload = payload.replace("Fictional company announcement", "El Niño and energy demand")
        header = '<?xml version="1.0" encoding="ISO-8859-1"?>'
        item, = parse_feed((header + payload).encode("iso-8859-1"), source, fetched_at=NOW)
        self.assertEqual(item.title, "El Niño and energy demand")
        with self.assertRaisesRegex(ValueError, "unsupported XML declaration"):
            parse_feed((header + '<!DOCTYPE rss [<!ENTITY title "test">]>' + payload).encode("iso-8859-1"),
                       source, fetched_at=NOW)

    def test_world_news_requires_first_party_release_url_and_publication_time(self):
        releases = (
            ("unpress", "https://press.un.org/en/2026/sc16518.doc.htm", "/en/blog/sc16518"),
            ("eucommission", "https://ec.europa.eu/commission/presscorner/detail/en/ip_26_2564", "/commission/faq"),
            ("bea", "https://www.bea.gov/news/2026/activities-us-multinational-enterprises-2024", "/data/example"),
        )
        for slug, canonical, invalid in releases:
            with self.subTest(slug=slug):
                item, = parse_feed(rss(
                    entry(canonical),
                    entry(invalid),
                    entry(canonical + "-future", "Tue, 24 Nov 2026 12:00:00 GMT"),
                    entry("https://unexpected.example/news/announcement"),
                ), REGISTERED_SOURCES[slug], fetched_at=NOW)
                self.assertEqual((item.source, item.url), (slug, canonical))


class FetchOfficialFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_failure_isolated_and_redirect_not_followed(self):
        session = Session({
            NVIDIA.feed_url: Response(200, rss(entry())),
            FED.feed_url: Response(302, headers={"Location": "https://other.invalid/feed"}),
        })
        with patch("official_news.time.time", return_value=NOW):
            items, health = await fetch_official_news(cast(ClientSession, session), sources=(NVIDIA, FED))
        self.assertEqual([item.source for item in items], ["nvidia"])
        self.assertEqual(health, {"nvidia": "ok", "fed": "http_302"})
        self.assertEqual({url for url, _ in session.calls}, {NVIDIA.feed_url, FED.feed_url})
        self.assertTrue(all(options["allow_redirects"] is False for _, options in session.calls))

    async def test_oversize_body_and_invalid_xml_get_health_errors(self):
        session = Session({NVIDIA.feed_url: Response(200, b"x" * 65), FED.feed_url: Response(200, b"<html/>")})
        with patch("official_news.MAX_FEED_BYTES", 64):
            items, health = await fetch_official_news(cast(ClientSession, session), sources=(NVIDIA, FED))
        self.assertEqual(items, [])
        self.assertEqual(health, {"nvidia": "too_large", "fed": "invalid_feed"})

    async def test_missing_provenance_and_unavailable_sources_keep_other_sources_working(self):
        sec = REGISTERED_SOURCES["sec"]
        boe = REGISTERED_SOURCES["boe"]
        boj = REGISTERED_SOURCES["boj"]
        session = Session({
            sec.feed_url: Response(200, rss(entry("/newsroom/press-releases/2026-example"))),
            boe.feed_url: Response(503),
            boj.feed_url: Response(200, rss(entry("/en/mopo/mpmdeci/example.pdf", published=""))),
        })
        with patch("official_news.time.time", return_value=NOW):
            items, health = await fetch_official_news(cast(ClientSession, session), sources=(sec, boe, boj))
        self.assertEqual([item.source for item in items], ["sec"])
        self.assertEqual(health, {"sec": "ok", "boe": "http_503", "boj": "no_verified_items"})

    async def test_european_publishers_use_a_verifying_tls_context(self):
        ecb = REGISTERED_SOURCES["ecb"]
        commission = REGISTERED_SOURCES["eucommission"]
        session = Session({
            ecb.feed_url: Response(200, rss(entry("/press/pr/example.en.html"))),
            commission.feed_url: Response(200, rss(entry("/commission/presscorner/detail/en/ip_26_2564"))),
        })
        with patch("official_news.time.time", return_value=NOW):
            items, health = await fetch_official_news(cast(ClientSession, session), sources=(ecb, commission))
        self.assertEqual({item.source for item in items}, {"ecb", "eucommission"})
        self.assertEqual(health, {"ecb": "ok", "eucommission": "ok"})
        for _, options in session.calls:
            self.assertEqual(options["ssl"].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(options["ssl"].check_hostname)
            self.assertFalse(options["allow_redirects"])
