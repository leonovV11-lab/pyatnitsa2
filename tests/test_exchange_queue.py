import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from live_portfolio import PortfolioSnapshot
from live_telegram import DeliveryResult
from news_live import LiveJournal, LiveMonitor
from official_news import NewsItem
from strategies import MAX_POSITION_FRACTION


class DynamicCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_through_exchange_catalog_including_other_quote_currencies(self):
        symbols = tuple(f"A{index}/{('BTC' if index % 2 else 'USDT')}" for index in range(70))
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"])
        monitor = LiveMonitor(None, None, None, options, all_pairs=True)
        with (
            patch.object(monitor.market, "spot_symbols", new=AsyncMock(return_value=symbols)),
            patch.object(monitor.market, "universe", new=AsyncMock(return_value=[])),
        ):
            pages = [await monitor.scan_symbols() for _ in range(2)]
            self.assertEqual(tuple(pair for page in pages for pair in page), symbols)
            self.assertTrue(any(pair.endswith("/BTC") for pair in pages[0]))
            self.assertEqual(await monitor.scan_symbols(), pages[0])

    async def test_non_usdt_funding_uses_fresh_conversion_in_both_directions(self):
        from market import MarketError, Quote
        from news_live import funding_rate

        direct = SimpleNamespace(quote=AsyncMock(return_value=Quote(
            "BTC/USDT", "okx", 100_000.0, 100_100.0, 10.0, 10.0, time.time(),
        )))
        mark = await funding_rate(direct, "BTC")
        self.assertEqual((mark.buy_rate, mark.sell_rate), (100_100.0, 100_000.0))
        inverse_watch = Quote("USDT/EUR", "okx", 0.9, 1.0, 1000.0, 1000.0, time.time())
        inverse = SimpleNamespace(quote=AsyncMock(side_effect=[MarketError("missing"), inverse_watch]))
        mark = await funding_rate(inverse, "EUR")
        self.assertAlmostEqual(mark.buy_rate, 1 / 0.9)
        self.assertAlmostEqual(mark.sell_rate, 1)

    async def test_plan_keeps_native_prices_and_converts_risk_into_usdt(self):
        from market import Quote

        now = time.time()
        portfolio = PortfolioSnapshot(now, 0.0, 0.0, frozenset(), {"BTC": 0.001})
        options = SimpleNamespace(venue="okx", symbols=["ETH/BTC"], budget=1000.0, allocation_slots=16,
                                  fee_rate=0.001, risk_cap=10.0)
        journal = SimpleNamespace(check_capacity=Mock(return_value=""))
        monitor = LiveMonitor(None, None, journal, options, all_pairs=True)
        monitor.market = SimpleNamespace(quote=AsyncMock(return_value=Quote(
            "BTC/USDT", "okx", 100_000.0, 100_100.0, 10.0, 10.0, now,
        )))
        native = {"qty": 0.01, "price": 0.03, "stop": 0.0299, "loss": 0.000005, "reward": 0.00001,
                  "tp1": 0.032, "tp2": 0.034, "entry_max": 0.031, "ratio": 2.0, "valid_until": now + 60}
        with (
            patch.object(monitor, "portfolio", return_value=(portfolio, "")),
            patch("news_live.refresh_plan", new=AsyncMock(return_value=(native, ""))) as refresh,
        ):
            plan, used_portfolio, reason = await monitor.current_plan({"setup": {"symbol": "ETH/BTC"}}, "ETH/BTC")
        self.assertEqual(reason, "")
        self.assertIs(used_portfolio, portfolio)
        assert plan is not None
        self.assertEqual(plan["quote_asset"], "BTC")
        self.assertEqual(plan["price"], 0.03)
        self.assertEqual(plan["entry_max"], 0.03)
        self.assertAlmostEqual(plan["loss"], 0.5005)
        self.assertAlmostEqual(plan["reward"], 1.0)
        self.assertAlmostEqual(plan["notional_usdt"], 30.03)
        self.assertAlmostEqual(plan["stressed_reward_usdt"], 0.937)
        self.assertAlmostEqual(plan["stressed_ratio"], 0.937 / plan["stressed_loss_usdt"])
        self.assertLessEqual(plan["valid_until"], now + 5)
        max_notional_btc = refresh.await_args.args[2] * MAX_POSITION_FRACTION
        self.assertAlmostEqual(max_notional_btc * 100_100.0, 62.5)

    async def test_rejects_plan_when_cost_stress_erases_required_payoff(self):
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], budget=1000.0, allocation_slots=1,
                                  fee_rate=0.001, risk_cap=10.0, portfolio_file=None)
        journal = SimpleNamespace(check_capacity=Mock(return_value=""))
        monitor = LiveMonitor(None, None, journal, options)
        plan = {"qty": 1.0, "price": 100.0, "stop": 99.2, "tp1": 101.15, "tp2": 101.5,
                "loss": 0.9992, "reward": 1.123675, "ratio": 1.123675 / 0.9992, "entry_max": 100.05}
        with patch("news_live.refresh_plan", new=AsyncMock(return_value=(plan, ""))):
            self.assertEqual(
                await monitor.current_plan({"setup": {"symbol": "BTC/USDT"}}, "BTC/USDT"),
                (None, None, "payoff_below_cost_stress"),
            )
        journal.check_capacity.assert_not_called()


class ManualIssueTests(unittest.TestCase):
    def test_open_risk_survives_quote_expiry_and_releases_on_reported_close(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "signals.sqlite3")
            news_item = NewsItem("example", "fed", "Federal Reserve", "FOMC",
                                 "https://www.federalreserve.gov/news/example", now - 1200, now - 900, "v1")
            event = journal.observe(news_item)
            self.assertIsNotNone(event)
            kwargs = {"cap": 10.0, "telegram_route": "test", "reply_required": True}
            alert = journal.issue(event, "okx", "BTC/USDT", {"loss": 7.0}, as_of=now, **kwargs)
            self.assertTrue(alert["reply_required"])
            self.assertIsNone(journal.issue(event, "okx", "ETH/USDT", {"loss": 1.0}, as_of=now, **kwargs))
            self.assertEqual(journal.telegram.claim_send("test", now)["id"], alert["id"])
            journal.telegram.finish_send(alert["id"], DeliveryResult("sent", message_id=17), now + 1)
            self.assertEqual(journal.slots.respond(alert["id"], "enter", route="test", message_id=17), "entered")
            self.assertIsNone(journal.issue(event, "okx", "ETH/USDT", {"loss": 4.0}, as_of=now + 301, **kwargs))
            self.assertEqual(journal.slots.respond(alert["id"], "close", route="test", message_id=17), "closed")
            self.assertIsNotNone(journal.issue(event, "okx", "ETH/USDT", {"loss": 4.0}, as_of=now + 302, **kwargs))
