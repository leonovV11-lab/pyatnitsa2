import asyncio
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd

from live_delivery import TelegramPublisher
from live_telegram import DeliveryResult
from market import MarketError
from news_agent_team import ROLE_GROUPS
from news_live import (
    LiveJournal,
    LiveMonitor,
    ScanMarket,
    classify_live_event,
    review_verdict,
    run_monitor,
)
from official_news import NewsItem


def release(now, revision="r1", source="fed"):
    return NewsItem(
        id="latest-release", source=source, publisher="Federal Reserve", title="Federal Reserve issues FOMC statement",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=now - 3600, fetched_at=now - 1800, revision=revision,
    )


class LiveSignalTests(unittest.TestCase):
    def test_gate_distinguishes_direct_events_from_cross_market_research(self):
        self.assertEqual(classify_live_event("fed", "Federal Reserve issues FOMC statement"),
                         ("us_monetary_policy", True))
        self.assertEqual(classify_live_event("nvidia", "NVIDIA board reinstates Jensen Huang as CEO"),
                         ("nvidia_leadership", False))

    def test_global_market_topics_are_recognized_across_publishers(self):
        for source, title, family in (
            ("unpress", "Security Council announces ceasefire agreement", "geopolitics"),
            ("eia", "Inventory of crude oil rises", "energy_supply"),
            ("trade", "Commission publishes tariff changes", "international_trade"),
            ("treasury", "Treasury announces new sanctions", "sanctions"),
            ("issuer", "Quarterly earnings released after merger", "corporate_events"),
            ("exchange", "Clearing outage resolved", "market_infrastructure"),
        ):
            with self.subTest(source=source):
                self.assertEqual(classify_live_event(source, title), (family, False))
        self.assertEqual(classify_live_event("boe", "Bank Rate maintained at 3.75%"), ("uk_monetary_policy", True))
        self.assertEqual(classify_live_event("bea", "Gross Domestic Product, Third Quarter"), ("us_gdp_release", False))
        self.assertEqual(classify_live_event("unpress", "Council adopts meeting agenda"), ("uncatalogued", False))

    def test_durable_once_per_event_and_risk_restricted(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            self.assertIsNotNone(event)
            first = db.issue(event, "okx", "BTC/USDT", {"loss": 7.0}, cap=10, as_of=now)
            self.assertEqual(first["status"], "active")
            self.assertIsNone(db.issue(event, "okx", "ETH/USDT", {"loss": 4.0}, cap=10, as_of=now))
            db = LiveJournal(Path(root) / "live.sqlite3")
            self.assertIsNone(db.issue(event, "okx", "BTC/USDT", {"loss": 5.0}, cap=10, as_of=now + 40))

    def test_correction_invalidates_one_alert_and_keeps_history(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            self.assertIsNotNone(event)
            db.issue(event, "okx", "BTC/USDT", {"loss": 2.0}, cap=3, as_of=now)
            revised = release(now + 1801, "r2")
            db.observe(revised)
            expired = db.invalidate({"fed": "ok"}, now + 2)
            self.assertEqual([row["reason"] for row in expired], ["source_revision_changed"])
            self.assertEqual(db.report()["signals"]["cancelled"], 1)
            self.assertEqual(db.invalidate({"fed": "ok"}, now + 3), [])

    def test_stale_quote_or_plan_cannot_create_an_alert(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            for plan in ({"loss": 1, "quote_observed_at": now - 6}, {"loss": 1, "valid_until": now - 1}):
                self.assertIsNone(db.issue(event, "okx", "BTC/USDT", plan, cap=10, as_of=now))

    def test_risk_remains_reserved_after_telegram_card_expires(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            self.assertIsNotNone(db.issue(event, "okx", "BTC/USDT", {"loss": 7.0}, cap=10, as_of=now))
            self.assertIsNone(db.issue(event, "okx", "ETH/USDT", {"loss": 4.0}, cap=10, as_of=now + 10))
            self.assertIsNotNone(db.issue(event, "okx", "ETH/USDT", {"loss": 4.0}, cap=10, as_of=now + 301))

    def test_daily_risk_charges_confirmed_entry_day_and_survives_closure(self):
        midnight = datetime(2026, 11, 25, 21, tzinfo=UTC).timestamp()
        options = {"cap": 1.0, "daily_risk_cap": 0.25, "budget": 100,
                   "telegram_route": "owner", "reply_required": True}
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(midnight - 2))
            plan = {"loss": 0.20, "qty": 0.1, "price": 30}
            card = db.issue(event, "okx", "BTC/USDT", plan, as_of=midnight - 2, **options)
            self.assertIsNotNone(card)
            self.assertIsNotNone(db.telegram.claim_send("owner", midnight - 2))
            db.telegram.finish_send(card["id"], DeliveryResult("sent", message_id=50), midnight - 2)

            db = LiveJournal(Path(root) / "live.sqlite3")
            with patch("live_slots.time.time", return_value=midnight):
                self.assertEqual(db.slots.respond(card["id"], "enter", route="owner", message_id=50), "entered")
            with patch("live_slots.time.time", return_value=midnight + 86_400):
                self.assertEqual(db.slots.respond(card["id"], "enter", route="owner", message_id=50), "unchanged")
            with patch("live_slots.time.time", return_value=midnight + 1):
                self.assertEqual(db.slots.respond(card["id"], "close", route="owner", message_id=50), "closed")

            candidate = {**plan, "loss": 0.10}
            self.assertIsNone(db.issue(event, "okx", "ETH/USDT", candidate, as_of=midnight + 1, **options))
            reason = db.check_capacity("ETH/USDT", candidate, cap=1.0, budget=100, as_of=midnight + 1,
                                       portfolio=None, daily_risk_cap=0.25, telegram_route="owner")
            self.assertEqual(reason, "daily_planned_risk_limit")
            second_day = midnight + 86_400
            later = db.observe(replace(release(second_day), id="later-event"))
            self.assertIsNotNone(db.issue(later, "okx", "ETH/USDT", candidate, as_of=second_day, **options))

    def test_daily_risk_keeps_carried_position_booked_after_closing_today(self):
        midnight = datetime(2026, 11, 25, 21, tzinfo=UTC).timestamp()
        options = {"cap": 1.0, "daily_risk_cap": 0.25, "budget": 100,
                   "telegram_route": "owner", "reply_required": True}
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(midnight - 3))
            plan = {"loss": 0.20, "qty": 0.1, "price": 30}
            card = db.issue(event, "okx", "BTC/USDT", plan, as_of=midnight - 3, **options)
            db.telegram.claim_send("owner", midnight - 3)
            db.telegram.finish_send(card["id"], DeliveryResult("sent", message_id=50), midnight - 3)
            with patch("live_slots.time.time", return_value=midnight - 2):
                self.assertEqual(db.slots.respond(card["id"], "enter", route="owner", message_id=50), "entered")
            with patch("live_slots.time.time", return_value=midnight + 5):
                self.assertEqual(db.slots.respond(card["id"], "close", route="owner", message_id=50), "closed")
            self.assertIsNone(db.issue(event, "okx", "ETH/USDT", plan, as_of=midnight + 6, **options))

    def test_daily_risk_releases_a_skipped_card_without_charging_it(self):
        now = time.time()
        options = {"cap": 1.0, "daily_risk_cap": 0.25, "budget": 100,
                   "telegram_route": "owner", "reply_required": True}
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            plan = {"loss": 0.20, "qty": 0.1, "price": 30}
            card = db.issue(event, "okx", "BTC/USDT", plan, as_of=now, **options)
            self.assertIsNotNone(card)
            db.telegram.claim_send("owner", now)
            db.telegram.finish_send(card["id"], DeliveryResult("sent", message_id=50), now)
            self.assertEqual(db.slots.respond(card["id"], "skip", route="owner", message_id=50), "skipped")
            self.assertIsNotNone(db.issue(event, "okx", "ETH/USDT", plan, as_of=now + 1, **options))

    def test_open_positions_and_unallocated_cash_apply_before_issuance(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            portfolio = SimpleNamespace(observed_at=now - 10, open_symbols={"BTC/USDT"},
                                        stressed_risk_usdt=6.0, available_cash_usdt=40.0)
            plan = {"loss": 1.0, "qty": 0.5, "price": 100.0}
            for symbol, candidate in (
                ("BTC/USDT", plan),
                ("ETH/USDT", plan),
                ("ETH/USDT", {**plan, "qty": 0.25, "loss": 5.0}),
            ):
                self.assertIsNone(db.issue(event, "okx", symbol, candidate, cap=10, as_of=now, portfolio=portfolio))
            self.assertIsNotNone(db.issue(event, "okx", "ETH/USDT", {**plan, "qty": 0.25},
                                          cap=10, as_of=now, portfolio=portfolio))

    def test_uses_open_portfolio_as_of_latest_snapshot_without_double_counting_older_alerts(self):
        now = time.time()
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            event = db.observe(release(now))
            plan = {"loss": 5.0, "qty": 1.0, "price": 45.0}
            self.assertIsNotNone(db.issue(event, "okx", "BTC/USDT", plan, cap=10, budget=80, as_of=now))
            self.assertIsNone(db.issue(event, "okx", "ETH/USDT", plan, cap=10, budget=80, as_of=now + 1))
            portfolio = SimpleNamespace(observed_at=now + 2, open_symbols=set(),
                                        stressed_risk_usdt=0.0, available_cash_usdt=80.0)
            self.assertIsNotNone(db.issue(event, "okx", "ETH/USDT", plan,
                                          cap=10, budget=80, as_of=now + 3, portfolio=portfolio))


def team_results():
    return [{"agent": name, "group": group, "decision": "support", "reason": "fixture", "pnl": "fixture"}
            for group, roles in ROLE_GROUPS for name, _ in roles]


def reaction(direction="up"):
    return {"direction": direction, "reaction_pct": 2.5 if direction == "up" else -3.0,
            "btc_reaction_pct": -1.0, "latest_closed_at": 1_000_000,
            "baseline_closed_at": 999_000, "first_post_event_open": "1970-01-12T13:38:20+00:00"}


class LiveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_scanner_runs_again_while_review_is_waiting(self):
        review_started = asyncio.Event()
        scan_continued = asyncio.Event()
        stop_review = asyncio.Event()
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], team=True, once=False, all_pairs=False,
                                  manual_replies=False, collect_market_evidence=False, daily_risk_cap=None,
                                  interval=0.01)
        monitor = LiveMonitor(None, None, None, options)
        scan_count = 0

        async def scan(*, background_reviews=False):
            nonlocal scan_count
            self.assertTrue(background_reviews)
            scan_count += 1
            if scan_count == 1:
                monitor.schedule_reviews(["first"])
            elif review_started.is_set():
                scan_continued.set()

        async def review(_candidates, *, history_snapshot):
            review_started.set()
            await stop_review.wait()

        with (
            patch("news_live.LiveMonitor", return_value=monitor),
            patch.object(monitor, "scan_once", side_effect=scan),
            patch.object(monitor, "review_ranked", side_effect=review),
        ):
            service = asyncio.create_task(run_monitor(options, None))
            try:
                await asyncio.wait_for(scan_continued.wait(), timeout=2)
                self.assertGreaterEqual(scan_count, 2)
            finally:
                service.cancel()
                await asyncio.gather(service, return_exceptions=True)

    async def test_background_review_keeps_only_latest_waiting_batch(self):
        started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()
        latest_started = asyncio.Event()
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"])
        monitor = LiveMonitor(None, None, None, options)
        reviewed = []

        async def review(candidates, *, history_snapshot):
            reviewed.append((candidates, history_snapshot))
            if candidates == ["first"]:
                started.set()
                await allow_first_to_finish.wait()
            else:
                latest_started.set()

        with patch.object(monitor, "review_ranked", side_effect=review):
            worker = asyncio.create_task(monitor.review_loop())
            try:
                monitor.schedule_reviews(["first"])
                await asyncio.wait_for(started.wait(), timeout=1)
                monitor.schedule_reviews(["superseded"])
                monitor.cached_history = [{"new": True}]
                monitor.schedule_reviews(["latest"])
                allow_first_to_finish.set()
                await asyncio.wait_for(latest_started.wait(), timeout=1)
                self.assertEqual(reviewed, [(["first"], []), (["latest"], [{"new": True}])])
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_queued_expired_release_skips_quote_and_team(self):
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], team=True, max_reviews_per_scan=1)
        monitor = LiveMonitor(None, None, None, options)
        monitor.listed_news = {("event", "r1")}
        event = {"id": "event", "revision": "r1", "published_at": time.time() - 7200}
        row = {"event": event, "symbol": "BTC/USDT", "draft": {}, "stats": {}}
        with (
            patch.object(monitor, "watch") as watch,
            patch.object(monitor, "current_plan") as plan,
            patch.object(monitor, "review") as review,
        ):
            await monitor.review_ranked([row], history_snapshot=monitor.cached_history)
            watch.assert_called_once_with(event, "BTC/USDT", "release_window_expired")
            plan.assert_not_called()
            review.assert_not_called()

    async def test_release_expiring_during_precheck_skips_team(self):
        now = time.time()
        options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], team=True, max_reviews_per_scan=1,
                                  history_file=None)
        journal = SimpleNamespace(has_review=Mock(return_value=False), claim_review=Mock())
        monitor = LiveMonitor(None, None, journal, options)
        monitor.listed_news = {("event", "r1")}
        event = {"id": "event", "revision": "r1", "published_at": now - 5399}
        row = {"event": event, "symbol": "BTC/USDT", "draft": {}, "stats": {}}
        with (
            patch("news_live.time.time", return_value=now) as clock,
            patch.object(monitor, "watch") as watch,
            patch.object(monitor, "review") as review,
        ):
            async def plan(*_):
                clock.return_value = now + 2
                return {}, None, ""

            with patch.object(monitor, "current_plan", side_effect=plan):
                await monitor.review_ranked([row], history_snapshot=monitor.cached_history)
            watch.assert_called_once_with(event, "BTC/USDT", "release_window_expired")
            journal.claim_review.assert_not_called()
            review.assert_not_called()

    async def test_catalog_error_does_not_preserve_obsolete_history(self):
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], history_file=None)
            monitor = LiveMonitor(None, None, journal, options, all_pairs=True)
            monitor.cached_history = [{"old": True}]
            with (
                patch.object(monitor, "refresh_sources", new=AsyncMock(return_value=([], {}, set()))),
                patch("news_live.fetch_discovery_news", new=AsyncMock(return_value=([], {}))),
                patch.object(monitor, "scan_symbols", new=AsyncMock(side_effect=MarketError("unavailable"))),
                patch("news_live.emit"),
            ):
                await monitor.scan_once(background_reviews=True)
            self.assertEqual(monitor.cached_history, [])

    async def test_catalog_checks_reference_pairs_each_scan_and_rotates_every_other_pair(self):
        priority = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
        rotating = tuple(f"TEST{i}/USDT" for i in range(130))
        catalog = (*rotating[:9], priority[0], *rotating[9:37], priority[1], priority[2], *rotating[37:])
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"])
            monitor = LiveMonitor(None, None, journal, options, all_pairs=True)
            universe = AsyncMock(return_value=["ABSENT/USDT", "SOL/USDT", "BTC/USDT"])
            with (
                patch.object(monitor.market, "spot_symbols", new=AsyncMock(return_value=catalog)),
                patch.object(monitor.market, "universe", new=universe),
            ):
                batches = [await monitor.scan_symbols() for _ in range(4)]
            universe.assert_awaited_once()
            self.assertEqual(monitor.catalog_size, len(catalog))
            self.assertEqual([batch[:3] for batch in batches], [priority] * 4)
            self.assertEqual([len(batch) for batch in batches], [64, 64, 11, 64])
            self.assertTrue(all(len(set(batch)) == len(batch) for batch in batches))
            self.assertEqual(tuple(pair for batch in batches[:3] for pair in batch[3:]), rotating)
            self.assertEqual(batches[0], batches[3])

    async def test_catalog_checks_available_reference_pairs_only(self):
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"])
            monitor = LiveMonitor(None, None, journal, options, all_pairs=True)
            get_symbols = AsyncMock(side_effect=[("ETH/USDT",), ("OTHER/USDT", "ETH/USDT"), ()])
            with (
                patch.object(monitor.market, "spot_symbols", new=get_symbols),
                patch.object(monitor.market, "universe", new=AsyncMock(side_effect=MarketError("ticker unavailable"))),
            ):
                self.assertEqual(await monitor.scan_symbols(), ("ETH/USDT",))
                self.assertEqual(await monitor.scan_symbols(), ("ETH/USDT", "OTHER/USDT"))
                self.assertEqual(await monitor.scan_symbols(), ())

    async def test_closed_candle_cache_reloads_at_boundary_and_retries_stale_and_failed_frames(self):
        close = 1_800_000_000

        def frame(opened):
            return pd.DataFrame({"ts": [pd.Timestamp(opened, unit="s", tz="UTC")]})

        stale = frame(close - 600)
        current = frame(close - 300)
        next_frame = frame(close)
        fetch = AsyncMock(side_effect=[stale, MarketError("temporarily unavailable"), current, next_frame])
        market = ScanMarket(SimpleNamespace(exchange="okx", quote=None, candles=fetch))
        with patch("news_live.time.time", return_value=close + 1) as clock:
            self.assertIs(await market.candles("BTC/USDT", "5m", 50), stale)
            market.begin_scan()
            with self.assertRaises(MarketError):
                await market.candles("BTC/USDT", "5m", 50)
            market.begin_scan()
            self.assertIs(await market.candles("BTC/USDT", "5m", 50), current)
            clock.return_value = close + 240
            market.begin_scan()
            self.assertIs(await market.candles("BTC/USDT", "5m", 50), current)
            clock.return_value = close + 300
            market.begin_scan()
            self.assertIs(await market.candles("BTC/USDT", "5m", 50), next_frame)
        self.assertEqual(fetch.await_count, 4)

    async def verify_flow(self, *, history_supported=True, revised=False, team_complete=True, telegram=False,
                          symbols=("BTC/USDT",), stale_symbol=None, manual_replies=False, history_changed=False,
                          source_lost_on_final_quote=False):
        now = time.time()
        initial = release(now)
        updated = release(now + 1800, "r2")
        rows = team_results()
        if not team_complete:
            rows.pop()
        stats = {"status": "supported" if history_supported else "watch", "reason": "outcome_statistics",
                 "recent_mean_stressed_net_r": 0.1, "day_count": 20}
        plan = {
            "strategy": "trend_v1", "price": 101.0, "stop": 90.0, "tp1": 112.0, "tp2": 123.0,
            "ratio": 1.4, "entry_min": 98.0, "entry_max": 103.0, "quote_observed_at": now,
            "loss": 1.2, "qty": 0.1, "reward": 1.68,
        }
        steps = []

        async def sources(*_args):
            steps.append("source")
            return [updated if revised and steps.count("source") > 1 else initial], {"fed": "ok"}

        async def prepare(_market, symbol, _event_known_at):
            steps.append("chart")
            return {"setup": {"symbol": symbol}, "market": {"move": 0.025, "symbol": symbol}}, ""

        def assess(*_args, **kwargs):
            self.assertLessEqual(kwargs["before"], kwargs["as_of"])
            steps.append("math")
            return {**stats, "lower_stressed_net_r": 0.1 + 0.01 * symbols.index(kwargs["symbol"])}

        async def review(_session, evidence, **_kwargs):
            self.assertEqual(evidence["mode"], "live")
            self.assertNotIn("qty", evidence["market"]["plan"])
            steps.append("agents")
            if history_changed:
                monitor.cached_history = [{"changed": True}]
            return rows

        async def refresh(_market, setup, _budget, _fee_rate):
            steps.append("quote")
            if source_lost_on_final_quote and steps.count("quote") == 2:
                monitor.listed_news.clear()
            if setup["symbol"] == stale_symbol:
                return None, "quote_stale"
            return {**plan, "quote_observed_at": time.time()}, ""

        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=symbols, budget=300.0, fee_rate=0.001, risk_cap=2.0,
                                      team=True, concurrency=50, history_file=None, portfolio_file=None,
                                      max_reviews_per_scan=2 if manual_replies else 1, allocation_slots=len(symbols))
            publisher = TelegramPublisher(object(), 1234, "owner", db.telegram,
                                          slots=db.slots if manual_replies else None) if telegram else None
            monitor = LiveMonitor(None, None, db, options, publisher=publisher, manual_replies=manual_replies)
            with (
                patch("news_live.fetch_official_news", new=AsyncMock(side_effect=sources)),
                patch("news_live.fetch_discovery_news", new=AsyncMock(return_value=([], {}))),
                patch("news_live.observe_reaction", new=AsyncMock(return_value=(reaction(), ""))),
                patch("news_live.prepare_candidate", new=AsyncMock(side_effect=prepare)),
                patch("news_live.assess_recorded_history", side_effect=assess),
                patch("news_live.analyze_team", new=AsyncMock(side_effect=review)),
                patch("news_live.refresh_plan", new=AsyncMock(side_effect=refresh)),
                patch("live_delivery.send_signal",
                      new=AsyncMock(return_value=DeliveryResult("sent", message_id=42))) as send,
                patch("live_delivery.emit"),
                patch("news_live.emit") as output,
            ):
                await monitor.scan_once()
                if manual_replies:
                    await monitor.scan_once()
            if (telegram and history_supported and not revised and team_complete
                    and not history_changed and not source_lost_on_final_quote):
                send.assert_awaited_once()
                self.assertEqual(send.await_args.args[1], 1234)
            else:
                send.assert_not_awaited()
            with closing(db.connect()) as connection:
                tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables, {
                    "official_headlines", "news_reviews", "live_signals", "market_reactions", "discovery_headlines",
                    "telegram_deliveries", "live_manual_slots",
                    "market_quote_samples", "market_quote_checks",
                })
            return steps, [call.args[0] for call in output.call_args_list], db.report()

    async def test_signal_requires_ordered_chain_and_keeps_no_paper_positions(self):
        steps, output, report = await self.verify_flow()
        self.assertEqual(steps, ["source", "chart", "math", "quote", "agents", "source", "quote"])
        signal = next(item for item in output if item["type"] == "live_signal")
        self.assertEqual(signal["evidence"]["completed_agents"], 50)
        self.assertGreater(signal["plan"]["stressed_loss_usdt"], signal["plan"]["loss"])
        self.assertEqual(signal["plan"]["entry_max"], signal["plan"]["price"])
        self.assertLess(signal["plan"]["stressed_ratio"], signal["plan"]["ratio"])
        self.assertEqual(report["finished_reviews"], 1)
        self.assertEqual(report["signals"], {"active": 1})

    async def test_missing_history_does_not_call_agents(self):
        steps, output, _ = await self.verify_flow(history_supported=False)
        self.assertEqual(steps, ["source", "chart", "math"])
        self.assertEqual(output[-1]["type"], "watch")

    async def test_reviewed_signal_is_delivered_to_telegram(self):
        _, _, report = await self.verify_flow(telegram=True)
        self.assertEqual(report["telegram_deliveries"], {"sent": 1})

    async def test_changed_history_during_review_blocks_delivery(self):
        _, output, report = await self.verify_flow(telegram=True, history_changed=True)
        self.assertEqual(output[-1]["reason"], "history_changed_during_review")
        self.assertEqual(report["telegram_deliveries"], {})

    async def test_release_lost_while_waiting_for_final_quote_blocks_delivery(self):
        _, output, report = await self.verify_flow(telegram=True, source_lost_on_final_quote=True)
        self.assertEqual(output[-1]["reason"], "release_changed_during_review")
        self.assertEqual(report["telegram_deliveries"], {})

    async def test_one_signal_waits_for_manual_reply_after_both_pairs_are_ranked(self):
        steps, output, _ = await self.verify_flow(symbols=("BTC/USDT", "ETH/USDT"), telegram=True,
                                                manual_replies=True)
        ranking = next(item for item in output if item["type"] == "candidate_ranking")
        self.assertEqual(ranking["ranked"], 2)
        self.assertEqual(steps.count("agents"), 1)
        self.assertEqual(steps.count("chart"), 4)
        signals = [item for item in output if item["type"] == "live_signal"]
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["reply_required"])
        manual = next(item for item in output if item["type"] == "manual_queue")
        self.assertEqual(manual["status"], "waiting_for_reply")

    async def test_ranks_all_sixteen_pairs_before_requesting_quotes_or_agents(self):
        symbols = tuple(f"ASSET{index}/USDT" for index in range(16))
        steps, output, _ = await self.verify_flow(symbols=symbols, stale_symbol=symbols[-1])
        self.assertEqual(steps[:33], ["source", *(["chart", "math"] * 16)])
        self.assertEqual(steps[33:], ["quote", "quote", "agents", "source", "quote"])
        ranking = next(item for item in output if item["type"] == "candidate_ranking")
        self.assertEqual(ranking["ranked"], 16)
        self.assertEqual(ranking["top"][0]["symbol"], symbols[-1])
        alert = next(item for item in output if item["type"] == "live_signal")
        self.assertEqual(alert["symbol"], symbols[-2])

    async def test_release_correction_after_agents_suppresses_signal(self):
        steps, output, report = await self.verify_flow(revised=True, telegram=True)
        self.assertEqual(steps, ["source", "chart", "math", "quote", "agents", "source"])
        self.assertEqual(output[-1]["reason"], "release_changed_during_review")
        self.assertEqual(report["signals"], {})
        self.assertEqual(report["telegram_deliveries"], {})

    async def test_incomplete_team_suppresses_signal(self):
        steps, output, report = await self.verify_flow(team_complete=False)
        self.assertEqual(steps, ["source", "chart", "math", "quote", "agents"])
        self.assertEqual(output[-1]["reason"], "agent_team_incomplete")
        self.assertEqual(report["signals"], {})

    async def test_negative_energy_reaction_is_measured_without_budget_or_model(self):
        headline = replace(release(time.time(), source="eia"), title="Crude oil inventories increase")
        uncatalogued = replace(headline, id="another-release", title="Monthly statistical report released")
        media_report = replace(headline, id="media", source="bbc_world", publisher="BBC World",
                               title="Ceasefire reported", url="https://www.bbc.co.uk/news/articles/example")
        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT", "ETH/USDT"], budget=None,
                                      team=False, history_file=None)
            monitor = LiveMonitor(None, None, db, options)
            with (
                patch("news_live.fetch_official_news",
                      new=AsyncMock(return_value=([headline, uncatalogued], {"eia": "ok"}))),
                patch("news_live.fetch_discovery_news",
                      new=AsyncMock(return_value=([media_report], {"bbc_world": "ok"}))),
                patch("news_live.observe_reaction", new=AsyncMock(return_value=(reaction("down"), ""))),
                patch("news_live.prepare_candidate", new=AsyncMock()) as trade_candidate,
                patch("news_live.emit") as output,
            ):
                await monitor.scan_once()
                await monitor.scan_once()
            trade_candidate.assert_not_called()
            updates = [call.args[0] for call in output.call_args_list if call.args[0]["type"] == "market_reaction"]
            self.assertEqual(len(updates), 4)
            self.assertEqual({row["family"] for row in updates}, {"energy_supply", "uncatalogued"})
            self.assertEqual(updates[0]["direction"], "down")
            self.assertEqual(updates[0]["vs_btc_percentage_points"], -2.0)
            report = db.report()
            self.assertEqual(report["reactions"], 4)
            self.assertEqual(report["signals"], {})
            self.assertEqual(report["news"], 2)
            self.assertEqual(report["discovery_candidates"][0]["event_id"], "media")
            self.assertFalse(report["discovery_candidates"][0]["confirmed"])

    async def test_quote_evidence_collects_candidates_without_spending_or_ordering(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "signals.sqlite3")
            event = journal.observe(release(now))
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], budget=None)
            monitor = LiveMonitor(None, None, journal, options, collect_market_evidence=True)
            collector = AsyncMock(return_value=1)
            with (
                patch("news_live.observe_reaction", new=AsyncMock(return_value=(reaction(), ""))),
                patch("news_live.prepare_candidate", new=AsyncMock(return_value=({"setup": {}}, ""))),
                patch("news_live.assess_recorded_history") as fills,
                patch.object(monitor.sampler, "collect", collector),
                patch("news_live.emit"),
            ):
                candidates = await monitor.scan_event(monitor.market, event, [], options.symbols)
            self.assertEqual(candidates, [])
            fills.assert_not_called()
            collector.assert_awaited_once_with(event, "us_monetary_policy", ["BTC/USDT"])
            self.assertEqual(journal.report()["signals"], {})
            self.assertEqual(journal.report()["market_quote_evidence"]["samples"], 0)

    def test_veto_and_majority_are_enforced(self):
        rows = team_results()
        for row in rows[:25]:
            row["decision"] = "watch"
        self.assertEqual(review_verdict(rows), "agent_support_below_majority")
        rows[0]["decision"] = "veto"
        self.assertEqual(review_verdict(rows), "agent_veto")
