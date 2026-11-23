import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd

from bot import TradingAssistant, private_owner
from market import MarketError, Quote
from research import candidate_key
from strategies import BASELINE, build_plan, candidate_setups


def setup_candidate(symbol="ETH/USDT"):
    candle = "2026-01-01T00:00:00+00:00"
    return {
        "id": candidate_key("binance", symbol, BASELINE, candle),
        "exchange": "binance", "symbol": symbol, "strategy": BASELINE, "candle": candle,
        "stop": 98.0, "tp1": 102.0, "tp2": 105.0, "reference": 100.0, "atr": 2.0,
        "score": 85, "evidence": ["Тренд"], "btc_bullish": True, "eligible": True,
    }


def quote(symbol="ETH/USDT", bid=99.99, ask=100.0):
    return Quote(symbol=symbol, exchange="binance", bid=bid, ask=ask,
                 bid_size=100, ask_size=100, observed_at=time())


class PaperIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.app = TradingAssistant(1234, self.path, ("binance",))
        self.app.ledger.configure(1000, 500)
        self.app.paused = False
        self.market = SimpleNamespace(quote=AsyncMock(), quotes=AsyncMock())
        self.app.markets = {"binance": self.market}
        self.app.notify = AsyncMock()
        self.app.invalidate = AsyncMock()
        self.candidate = setup_candidate()
        self.token = self.candidate["id"]
        self.app.research.record_candidate(
            self.token, exchange="binance", symbol=self.candidate["symbol"],
            strategy=BASELINE, candle=self.candidate["candle"], observed_at=time(),
            payload=self.candidate, decision="eligible",
        )
        self.app.pending[self.token] = (self.candidate, monotonic() + 90, 1)

    async def test_confirmation_uses_fresh_quote_and_is_idempotent(self):
        self.market.quote.return_value = quote(bid=100.09, ask=100.10)
        self.assertIn("Бумажная покупка", await self.app.paper_confirm(self.token))
        self.assertIn("устарел", await self.app.paper_confirm(self.token))
        positions = self.app.ledger.open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]["entry"], 100.10)
        self.assertEqual(positions[0]["exchange"], "binance")

    async def test_stale_or_missing_price_does_not_invent_fill(self):
        self.market.quote.side_effect = MarketError("unavailable")
        self.assertIn("не открыта", await self.app.paper_confirm(self.token))
        self.assertEqual(self.app.ledger.open_positions(), [])

    async def test_unfavorable_price_cancels_card(self):
        self.market.quotes.return_value = {"ETH/USDT": quote(bid=101.99, ask=102.0)}
        await self.app.monitor_once()
        self.assertNotIn(self.token, self.app.pending)
        self.app.invalidate.assert_awaited_once()
        self.assertEqual(self.app.ledger.open_positions(), [])

    async def test_restore_and_jump_through_both_profit_targets(self):
        self.market.quote.return_value = quote()
        await self.app.paper_confirm(self.token)
        resumed = TradingAssistant(1234, self.path, ("binance",))
        self.market.quotes.return_value = {"ETH/USDT": quote(bid=105.1, ask=105.12)}
        resumed.markets = {"binance": self.market}
        resumed.notify = AsyncMock()
        await resumed.monitor_once()
        self.assertEqual(resumed.ledger.open_positions(), [])
        self.assertEqual(resumed.ledger.report()["closed_count"], 1)
        self.assertEqual(resumed.notify.await_count, 2)

    async def test_paused_search_preserves_paper_monitoring(self):
        self.market.quote.return_value = quote()
        await self.app.paper_confirm(self.token)
        self.app.paused = True
        self.market.quotes.return_value = {"ETH/USDT": quote(bid=97.99, ask=98.0)}
        await self.app.monitor_once()
        self.assertEqual(self.app.ledger.open_positions(), [])
        self.assertLess(self.app.ledger.report()["net_pnl"], 0)

    async def test_scan_delivers_during_shadow_review(self):
        self.app.review_enabled = True
        self.app.pending.clear()
        self.app.candidates = AsyncMock(return_value=[self.candidate])
        self.app.shadow_review = AsyncMock()
        self.market.quote.return_value = quote()
        self.market.quotes.return_value = {"ETH/USDT": quote()}
        self.app.notify.return_value = SimpleNamespace(message_id=12)

        await self.app.scan_once()
        task = self.app.review_task
        self.assertIsNotNone(task)
        await task
        self.app.notify.assert_awaited_once()
        self.app.shadow_review.assert_awaited_once()
        self.assertIn(self.token, self.app.pending)

    async def test_delivery_checks_capacity_and_does_not_open_positions(self):
        for symbol in ("ABC/USDT", "DEF/USDT", "GHI/USDT"):
            self.app.ledger.open(symbol, "binance", symbol, 0.5, 100, 98, 102, 105, 0.002)
        self.app.pending.clear()
        self.market.quotes.return_value = {"ETH/USDT": quote()}
        plan, _ = build_plan(self.candidate, quote(), 500, 0.002)

        await self.app.send_current_signals([(self.candidate, plan)])

        self.app.notify.assert_not_awaited()
        self.assertEqual(len(self.app.ledger.open_positions()), 3)
        self.assertEqual(
            self.app.research.report()["events_by_stage"]["delivery"]["risk_rejected"], 1,
        )

    async def test_delivery_refreshes_one_batch_and_deduplicates(self):
        self.app.pending.clear()
        self.market.quotes.return_value = {"ETH/USDT": quote()}
        self.app.notify.return_value = SimpleNamespace(message_id=15)
        plan, _ = build_plan(self.candidate, quote(), 500, 0.002)

        await self.app.send_current_signals([(self.candidate, plan)])
        self.app.pending.clear()
        await self.app.send_current_signals([(self.candidate, plan)])

        self.app.notify.assert_awaited_once()
        self.market.quotes.assert_awaited_once_with(["ETH/USDT"])
        self.market.quote.assert_not_awaited()

    async def test_owner_and_chat_are_both_checked(self):
        outsider = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(type="private"))
        group = SimpleNamespace(effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type="group"))
        owner = SimpleNamespace(effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type="private"))
        self.assertFalse(private_owner(outsider, self.app))
        self.assertFalse(private_owner(group, self.app))
        self.assertTrue(private_owner(owner, self.app))


class StrategyTests(unittest.TestCase):
    def test_plan_uses_fees_and_price_band(self):
        candidate = setup_candidate()
        plan, _ = build_plan(candidate, quote(), 500, 0.002)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["loss"], plan["qty"] * (100 * 1.002 - 98 * 0.998))
        invalid, _ = build_plan(candidate, quote(bid=102, ask=102.01), 500, 0.002)
        self.assertIsNone(invalid)

    def test_candidate_payload_is_point_in_time_and_finite(self):
        times = pd.date_range("2026-01-01", periods=50, freq="5min", tz="UTC")
        values = [100 + index / 10 for index in range(50)]
        prices = pd.Series(values)
        candles = pd.DataFrame({"ts": times, "open": prices - 0.05, "close": prices,
                                "high": prices + 0.1, "low": prices - 0.1, "volume": 100})
        results = candidate_setups("ETH/USDT", candles, candles, True)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(row["candle"] == times[-1].isoformat() for row in results))
