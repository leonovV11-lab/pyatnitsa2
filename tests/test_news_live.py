import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from live_delivery import TelegramPublisher
from live_telegram import DeliveryResult
from news_agent_team import ROLE_GROUPS
from news_live import LiveJournal, LiveMonitor, classify_live_event, review_verdict
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


def team_results():
    return [{"agent": name, "group": group, "decision": "support", "reason": "fixture", "pnl": "fixture"}
            for group, roles in ROLE_GROUPS for name, _ in roles]


def reaction(direction="up"):
    return {"direction": direction, "reaction_pct": 2.5 if direction == "up" else -3.0,
            "btc_reaction_pct": -1.0, "latest_closed_at": 1_000_000,
            "baseline_closed_at": 999_000, "first_post_event_open": "1970-01-12T13:38:20+00:00"}


class LiveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def verify_flow(self, *, history_supported=True, revised=False, team_complete=True, telegram=False):
        now = time.time()
        initial = release(now)
        updated = release(now + 1800, "r2")
        rows = team_results()
        if not team_complete:
            rows.pop()
        stats = {"status": "supported" if history_supported else "watch", "reason": "outcome_statistics"}
        plan = {
            "strategy": "trend_v1", "price": 101.0, "stop": 90.0, "tp1": 112.0, "tp2": 123.0,
            "ratio": 1.4, "entry_min": 98.0, "entry_max": 103.0, "quote_observed_at": now,
            "loss": 1.2, "qty": 0.1,
        }
        steps = []

        async def sources(*_args):
            steps.append("source")
            return [updated if revised and steps.count("source") > 1 else initial], {"fed": "ok"}

        async def inspect(*_args):
            steps.append("chart")
            return {"setup": {}, "plan": plan, "market": {"move": 0.025}}, ""

        def assess(*_args, **kwargs):
            self.assertLessEqual(kwargs["before"], kwargs["as_of"])
            steps.append("math")
            return stats

        async def review(_session, evidence, **_kwargs):
            self.assertEqual(evidence["mode"], "live")
            self.assertNotIn("qty", evidence["market"]["plan"])
            steps.append("agents")
            return rows

        async def refresh(*_args):
            steps.append("quote")
            return {**plan, "quote_observed_at": time.time()}, ""

        with TemporaryDirectory() as root:
            db = LiveJournal(Path(root) / "live.sqlite3")
            options = SimpleNamespace(venue="okx", symbols=["BTC/USDT"], budget=300.0, fee_rate=0.001,
                                      risk_cap=2.0, team=True, concurrency=50, history_file=None)
            publisher = TelegramPublisher(object(), 1234, "owner", db.telegram) if telegram else None
            monitor = LiveMonitor(None, None, db, options, publisher=publisher)
            with (
                patch("news_live.fetch_official_news", new=AsyncMock(side_effect=sources)),
                patch("news_live.fetch_discovery_news", new=AsyncMock(return_value=([], {}))),
                patch("news_live.observe_reaction", new=AsyncMock(return_value=(reaction(), ""))),
                patch("news_live.inspect_market", new=AsyncMock(side_effect=inspect)),
                patch("news_live.assess_recorded_history", side_effect=assess),
                patch("news_live.analyze_team", new=AsyncMock(side_effect=review)),
                patch("news_live.refresh_plan", new=AsyncMock(side_effect=refresh)),
                patch("live_delivery.send_signal",
                      new=AsyncMock(return_value=DeliveryResult("sent", message_id=42))) as send,
                patch("live_delivery.emit"),
                patch("news_live.emit") as output,
            ):
                await monitor.scan_once()
            if telegram and history_supported and not revised and team_complete:
                send.assert_awaited_once()
                self.assertEqual(send.await_args.args[1], 1234)
            else:
                send.assert_not_awaited()
            with closing(db.connect()) as connection:
                tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables, {
                    "official_headlines", "news_reviews", "live_signals", "market_reactions", "discovery_headlines",
                    "telegram_deliveries",
                })
            return steps, [call.args[0] for call in output.call_args_list], db.report()

    async def test_signal_requires_ordered_chain_and_keeps_no_paper_positions(self):
        steps, output, report = await self.verify_flow()
        self.assertEqual(steps, ["source", "chart", "math", "agents", "source", "quote"])
        signal = next(item for item in output if item["type"] == "live_signal")
        self.assertEqual(signal["evidence"]["completed_agents"], 50)
        self.assertGreater(signal["plan"]["stressed_loss_usdt"], signal["plan"]["loss"])
        self.assertEqual(report["finished_reviews"], 1)
        self.assertEqual(report["signals"], {"active": 1})

    async def test_missing_history_does_not_call_agents(self):
        steps, output, _ = await self.verify_flow(history_supported=False)
        self.assertEqual(steps, ["source", "chart", "math"])
        self.assertEqual(output[-1]["type"], "watch")

    async def test_reviewed_signal_is_delivered_to_telegram(self):
        _, _, report = await self.verify_flow(telegram=True)
        self.assertEqual(report["telegram_deliveries"], {"sent": 1})

    async def test_release_correction_after_agents_suppresses_signal(self):
        steps, output, report = await self.verify_flow(revised=True, telegram=True)
        self.assertEqual(steps, ["source", "chart", "math", "agents", "source"])
        self.assertEqual(output[-1]["reason"], "release_changed_during_review")
        self.assertEqual(report["signals"], {})
        self.assertEqual(report["telegram_deliveries"], {})

    async def test_incomplete_team_suppresses_signal(self):
        steps, output, report = await self.verify_flow(team_complete=False)
        self.assertEqual(steps, ["source", "chart", "math", "agents"])
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
                patch("news_live.inspect_market", new=AsyncMock()) as trade_candidate,
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

    def test_veto_and_majority_are_enforced(self):
        rows = team_results()
        for row in rows[:25]:
            row["decision"] = "watch"
        self.assertEqual(review_verdict(rows), "agent_support_below_majority")
        rows[0]["decision"] = "veto"
        self.assertEqual(review_verdict(rows), "agent_veto")
