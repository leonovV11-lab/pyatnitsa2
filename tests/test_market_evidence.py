import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from market import MarketError, Quote
from market_evidence import MarketSampler, QuoteEvidenceJournal
from news_journal import HeadlineJournal

BASE = 1_700_000_000.0
EVENT = {"id": "event-1", "revision": "unique", "known_at": BASE - 600}


def quote(at, *, bid=99.0, ask=100.0):
    return Quote("BTC/USDT", "okx", bid, ask, 10.0, 9.0, at)


class QuoteEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        connect = HeadlineJournal(Path(directory) / "evidence.sqlite3").connect
        self.journal = QuoteEvidenceJournal(connect)

    def test_records_real_quotes_once_and_does_not_replace_them_with_better_prices(self):
        self.assertTrue(self.journal.record(EVENT, "macro", quote(BASE), as_of=BASE))
        self.assertFalse(self.journal.record(EVENT, "macro", quote(BASE + 1, ask=101), as_of=BASE + 1))
        self.assertEqual(self.journal.due_symbols("okx", as_of=BASE + 299), ())
        self.assertEqual(self.journal.due_symbols("okx", as_of=BASE + 300), ("BTC/USDT",))
        self.assertEqual(self.journal.observe(quote(BASE + 299, bid=104, ask=105), as_of=BASE + 300), 0)
        self.assertEqual(self.journal.observe(quote(BASE + 300, bid=103, ask=104), as_of=BASE + 301), 1)
        self.assertEqual(self.journal.observe(quote(BASE + 301, bid=104, ask=105), as_of=BASE + 301), 0)
        value = self.journal.report()["horizons"]["300"]
        self.assertEqual(value["observed"], 1)
        self.assertAlmostEqual(value["average_move_pct"], 3.0)
        self.assertEqual(value["positive_moves"], 1)

    def test_missing_windows_and_stale_baselines_are_never_backfilled(self):
        self.assertFalse(self.journal.record(EVENT, "macro", quote(BASE - 6), as_of=BASE))
        self.assertFalse(self.journal.record(EVENT, "macro", quote(BASE + 1), as_of=BASE))
        self.assertTrue(self.journal.record(EVENT, "macro", quote(BASE), as_of=BASE))
        self.assertEqual(self.journal.due_symbols("okx", as_of=BASE + 376), ())
        self.assertEqual(self.journal.observe(quote(BASE + 376), as_of=BASE + 376), 0)
        status = self.journal.report()
        self.assertEqual(status["provenance"], "public_market_quotes")
        self.assertEqual(status["horizons"]["300"]["missed"], 1)
        self.assertEqual(status["horizons"]["900"]["pending"], 1)


class MarketSamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_initial_and_due_quotes_with_single_quote_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        directory = self.enterContext(TemporaryDirectory())
        journal = QuoteEvidenceJournal(HeadlineJournal(Path(directory) / "marks.sqlite3").connect)
        market = SimpleNamespace(exchange="okx", quotes=AsyncMock(side_effect=MarketError("unavailable")),
                                 quote=AsyncMock(return_value=quote(BASE)))
        sampler = MarketSampler(market, journal)
        with patch("market_evidence.time.time", return_value=BASE):
            self.assertEqual(await sampler.collect(EVENT, "macro", ["BTC/USDT", "BTC/USDT"]), 1)
            self.assertEqual(await sampler.collect(EVENT, "macro", ["BTC/USDT"]), 0)
        market.quote.return_value = quote(BASE + 900, bid=95, ask=97)
        with patch("market_evidence.time.time", return_value=BASE + 900):
            self.assertEqual(await sampler.sample_due(), 1)
        self.assertEqual(journal.report()["horizons"]["900"]["negative_moves"], 1)
        self.assertEqual(market.quote.await_count, 2)
