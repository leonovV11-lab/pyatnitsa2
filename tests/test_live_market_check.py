import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pandas as pd

from live_market_check import (
    inspect_market,
    observe_reaction,
    prepare_candidate,
    refresh_first_trade_plan,
    refresh_plan,
)
from market import MarketError, Quote, SpotOrderRules

SYMBOL = "ETH/USDT"
NOW = datetime(2026, 1, 1, 12, 1, tzinfo=UTC).timestamp()
BUDGET = 500.0
FEE = 0.001


def candles(interval):
    last_open = NOW // interval * interval - interval
    times = pd.date_range(
        end=pd.Timestamp(last_open, unit="s", tz="UTC"), periods=50, freq=f"{interval}s",
    )
    prices = pd.Series([100 + index * 0.09 + 0.5 * (index % 2) for index in range(50)])
    return pd.DataFrame(
        {"ts": times, "open": prices - 0.05, "close": prices, "high": prices + 0.1,
         "low": prices - 0.1, "volume": [100] * 49 + [300]},
    )


class BookFixture:
    exchange = "okx"

    def __init__(self):
        self.frames = {
            (SYMBOL, "5m"): candles(300),
            (SYMBOL, "15m"): candles(900),
            ("BTC/USDT", "5m"): candles(300),
            ("BTC/USDT", "15m"): candles(900),
        }
        self.book = Quote(SYMBOL, self.exchange, 104.90, 104.91, 20, 20, NOW)
        self.quote_calls = 0
        self.order_rules = SpotOrderRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.1"))

    async def spot_order_rules(self, symbol):
        if symbol != self.book.symbol:
            raise MarketError("Unknown symbol")
        return self.order_rules

    async def candles(self, symbol, timeframe, limit):
        return self.frames[symbol, timeframe].tail(limit)

    async def quote(self, symbol):
        self.quote_calls += 1
        if symbol != self.book.symbol:
            raise MarketError("Unknown symbol")
        return self.book


class LiveMarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = self.enterContext(patch("time.time", return_value=NOW))
        self.market = BookFixture()

    async def inspect(self, known_at=NOW - 500):
        return await inspect_market(self.market, SYMBOL, known_at, BUDGET, FEE)

    async def test_first_trade_plan_sizes_to_60_usdt_budget_and_okx_steps(self):
        setup = {"symbol": SYMBOL, "strategy": "trend_v1", "score": 85, "reference": 100, "atr": 2.0,
                 "stop": 97.99, "tp1": 101.98, "tp2": 106.19,
                 "snapshot_5m_close": NOW // 300 * 300, "event_known_at": NOW - 600}
        self.market.book = Quote(SYMBOL, "okx", 99.45, 99.5, 20, 20, NOW)
        plan, reason = await refresh_first_trade_plan(self.market, setup, 60, 0.002, 0.6)
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertEqual((plan["price"], plan["stop"], plan["tp1"], plan["tp2"]), (99.5, 97.9, 104.0, 104.0))
        self.assertEqual(plan["qty"], 0.120)
        self.assertEqual(plan["exit_qty"], 0.119)
        self.assertLessEqual(plan["qty"] * plan["price"] * 1.002, 12)
        self.assertLessEqual(plan["stressed_loss_usdt"], 0.6)
        self.assertLess(plan["valid_until"], plan["entry_deadline"])

        self.market.order_rules = replace(self.market.order_rules, min_size=Decimal("0.12"))
        plan, reason = await refresh_first_trade_plan(self.market, setup, 60, 0.002, 0.6)
        self.assertIsNone(plan)
        self.assertIn("минимума OKX", reason)

    async def test_snapshot_uses_completed_post_event_bars_and_fee_adjusted_plan(self):
        result, reason = await self.inspect()
        self.assertEqual(reason, "")
        assert result is not None
        evidence, plan = result["market"], result["plan"]
        frame = self.market.frames[SYMBOL, "5m"]
        self.assertEqual(evidence["first_post_event_open"], frame["ts"].iloc[-1].isoformat())
        self.assertAlmostEqual(evidence["move"], frame["close"].iloc[-1] / frame["open"].iloc[-1] - 1)
        self.assertAlmostEqual(plan["loss"], plan["qty"] * (plan["price"] * (1 + FEE) - plan["stop"] * (1 - FEE)))
        self.assertGreaterEqual(plan["ratio"], 1)
        self.assertEqual(plan["valid_until"], NOW + 5)
        json.dumps(result, allow_nan=False)

    async def test_prepare_candidate_returns_closed_candle_evidence_without_requesting_book(self):
        draft, reason = await prepare_candidate(self.market, SYMBOL, NOW - 500)
        self.assertEqual(reason, "")
        assert draft is not None
        self.assertEqual(set(draft), {"setup", "market"})
        self.assertEqual(draft["setup"]["snapshot_5m_close"], draft["market"]["latest_closed_at"])
        self.assertEqual(draft["market"]["direction"], "up")
        self.assertNotIn("book", draft["market"])
        self.assertEqual(self.market.quote_calls, 0)
        json.dumps(draft, allow_nan=False)

        plan, reason = await refresh_plan(self.market, draft["setup"], BUDGET, FEE)
        self.assertEqual(reason, "")
        self.assertIsNotNone(plan)
        self.assertEqual(self.market.quote_calls, 1)

    async def test_prepare_candidate_rejects_decline_without_requesting_book(self):
        frame = self.market.frames[SYMBOL, "5m"]
        frame.loc[frame.index[-1], "open"] = frame["close"].iloc[-1] + 0.05
        draft, reason = await prepare_candidate(self.market, SYMBOL, NOW - 500)
        self.assertIsNone(draft)
        self.assertIn("рост", reason)
        self.assertEqual(self.market.quote_calls, 0)

    async def test_requires_a_full_candle_after_knowledge_time(self):
        result, reason = await self.inspect(NOW - 300)
        self.assertIsNone(result)
        self.assertIn("свеч", reason)
        self.assertEqual(self.market.quote_calls, 0)

    async def test_observes_decline_against_btc_without_a_book(self):
        frame = self.market.frames[SYMBOL, "5m"]
        frame.loc[frame.index[-1], "open"] = frame["close"].iloc[-1] + 0.05
        evidence, reason = await observe_reaction(self.market, SYMBOL, NOW - 500)
        self.assertEqual(reason, "")
        assert evidence is not None
        self.assertEqual(evidence["direction"], "down")
        self.assertLess(evidence["reaction_pct"], 0)
        self.assertGreater(evidence["btc_reaction_pct"], 0)
        self.assertEqual(evidence["latest_closed_at"], NOW - 60)
        self.assertEqual(self.market.quote_calls, 0)
        json.dumps(evidence, allow_nan=False)

    async def test_observes_flat_market(self):
        frame = self.market.frames[SYMBOL, "5m"]
        frame.loc[frame.index[-1], "open"] = frame["close"].iloc[-1]
        evidence, reason = await observe_reaction(self.market, SYMBOL, NOW - 500)
        self.assertEqual(reason, "")
        assert evidence is not None
        self.assertEqual(evidence["direction"], "flat")
        self.assertEqual(evidence["reaction_pct"], 0)
        self.assertEqual(self.market.quote_calls, 0)

    async def test_observation_waits_for_first_full_post_event_candle(self):
        evidence, reason = await observe_reaction(self.market, SYMBOL, NOW - 300)
        self.assertIsNone(evidence)
        self.assertIn("свеч", reason)
        self.assertEqual(self.market.quote_calls, 0)

    async def test_mixed_candle_jump_does_not_count(self):
        frame = self.market.frames[SYMBOL, "5m"]
        frame.loc[frame.index[-1], "open"] = frame["close"].iloc[-1] + 0.05
        self.assertGreater(frame["close"].iloc[-1], frame["close"].iloc[-3])
        result, reason = await self.inspect()
        self.assertIsNone(result)
        self.assertIn("рост", reason)

    async def test_rejects_missing_latest_benchmark_candle(self):
        key = ("BTC/USDT", "15m")
        self.market.frames[key] = self.market.frames[key].iloc[:-1]
        result, reason = await self.inspect()
        self.assertIsNone(result)
        self.assertIn("BTC", reason)

    async def test_market_unavailability_returns_watch_reason(self):
        with patch.object(self.market, "candles", side_effect=MarketError("Data unavailable")):
            result, reason = await self.inspect()
        self.assertIsNone(result)
        self.assertTrue(reason)

    async def test_revalidates_price_and_current_top_of_book_size(self):
        result, _ = await self.inspect()
        assert result is not None
        setup = result["setup"]
        self.market.book = replace(self.market.book, bid=104.91, ask=104.92, observed_at=NOW + 1)
        self.clock.return_value = NOW + 1
        plan, reason = await refresh_plan(self.market, setup, BUDGET, FEE)
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertEqual(plan["price"], 104.92)
        self.assertEqual(self.market.quote_calls, 2)
        self.market.book = replace(self.market.book, bid_size=0.001)
        plan, reason = await refresh_plan(self.market, setup, BUDGET, FEE)
        self.assertIsNone(plan)
        self.assertIn("объём", reason)

    async def test_rejects_setup_superseded_by_another_closed_candle(self):
        result, _ = await self.inspect()
        assert result is not None
        self.clock.return_value = NOW + 240
        self.market.book = replace(self.market.book, observed_at=NOW + 240)
        plan, reason = await refresh_plan(self.market, result["setup"], BUDGET, FEE)
        self.assertIsNone(plan)
        self.assertIn("свеч", reason)
        self.assertEqual(self.market.quote_calls, 1)

    async def test_rechecks_latest_closed_candle_after_requesting_book(self):
        result, _ = await self.inspect()
        assert result is not None

        async def book_at_boundary(*_):
            self.clock.return_value = NOW + 240
            return replace(self.market.book, observed_at=NOW + 240)

        with patch.object(self.market, "quote", side_effect=book_at_boundary):
            plan, reason = await refresh_plan(self.market, result["setup"], BUDGET, FEE)
        self.assertIsNone(plan)
        self.assertIn("свеч", reason)

    async def test_rejects_stale_book(self):
        self.market.book = replace(self.market.book, observed_at=NOW - 6)
        result, reason = await self.inspect()
        self.assertIsNone(result)
        self.assertIn("котировк", reason)
