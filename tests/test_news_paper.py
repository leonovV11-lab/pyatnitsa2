import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

from market import Quote
from news_paper import (
    NewsJournal,
    NewsPaperRunner,
    classify_headline,
    decision_after_review,
)
from official_news import NewsItem
from paper_ledger import PaperLedger

START = 1_795_000_000.0


def headline(title, revision="a", fetched=START + 4):
    return NewsItem(
        id="article1", source="nvidia", publisher="NVIDIA", title=title,
        url="https://nvidianews.nvidia.com/news/example",
        published_at=START, fetched_at=fetched, revision=revision,
    )


class PaperNewsTests(unittest.TestCase):
    def test_published_fact_preserves_known_time_and_revision(self):
        with TemporaryDirectory() as directory:
            journal = NewsJournal(Path(directory) / "scans.sqlite3")
            event = headline("NVIDIA Board Reinstates Jensen Huang as President, CEO and Board Director")
            revised = headline("NVIDIA updates leadership announcement", "b", START + 20)
            self.assertIsNotNone(journal.observe(event))
            self.assertIsNone(journal.observe(event))
            journal.observe(revised)
            self.assertEqual(journal.current_news(START + 19)[0]["revision"], "a")
            self.assertEqual(journal.current_news(START + 20)[0]["revision"], "b")
            self.assertEqual(journal.current_news(START + 20)[0]["known_at"], START + 20)

    def test_nvidia_is_cross_market_research_only(self):
        news = classify_headline("nvidia", "NVIDIA board reinstates Jensen Huang as CEO")
        self.assertEqual(news, ("nvidia_leadership", False))

    def test_initial_review_does_not_create_an_unmeasured_signal(self):
        answers = [{"agent": f"role_{i}", "decision": "support"} for i in range(50)]
        self.assertEqual(decision_after_review({"status": "watch", "reason": "missing data"}, answers)[0], "watch")
        supported = {"status": "supported"}
        self.assertEqual(decision_after_review(supported, answers)[0], "paper_signal")
        self.assertEqual(decision_after_review(supported, answers[:-1])[0], "watch")
        answers[-1]["decision"] = "veto"
        self.assertEqual(decision_after_review(supported, answers)[0], "watch")

    def test_first_target_uses_quantity_to_be_sold(self):
        with TemporaryDirectory() as directory:
            ledger = PaperLedger(Path(directory) / "paper.sqlite3")
            ledger.configure(1000, 300, now=START)
            position = ledger.open("abc", "okx", "ETH/USDT", 2, 100, 99, 102, 104, 0.001, now=START)
            fills = ledger.mark(position["position_id"], 102.0, now=START + 10, available_qty=1.0)
            self.assertEqual([(row["reason"], row["qty"]) for row in fills], [("tp1", 1.0)])

    def test_historical_outcomes_ignore_incomplete_positions_and_include_costs(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "study.sqlite3"
            ledger = PaperLedger(path)
            journal = NewsJournal(path)
            ledger.configure(1000, 300, now=START)
            journal.register_trial("trade1", "article1", "risk_on", START, "okx", "ETH/USDT", 3.0)
            position = ledger.open("trade1", "okx", "ETH/USDT", 2, 100, 99, 102, 104, 0.001, now=START + 1)
            self.assertEqual(journal.outcomes("risk_on", "okx", "ETH/USDT", START + 2), [])
            ledger.close(position["position_id"], 102, now=START + 3)
            sample = journal.outcomes("risk_on", "okx", "ETH/USDT", START + 5)[0]
            self.assertAlmostEqual(sample["net_pnl_usdt"], 3.596)
            self.assertLess(sample["stressed_net_pnl_usdt"], sample["net_pnl_usdt"])
            self.assertEqual(sample["event_id"], "article1")



class NewsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, revised):
        now = time.time()
        item = NewsItem(
            id="policy", source="fed", publisher="Fed", title="FOMC statement",
            url="https://www.federalreserve.gov/example",
            published_at=now - 1450, fetched_at=now - 1400, revision="original",
        )
        current = NewsItem(
            id=item.id, source=item.source, publisher=item.publisher, title=item.title, url=item.url,
            published_at=item.published_at, fetched_at=now - 1, revision="amended",
        ) if revised else item
        quote = Quote("ETH/USDT", "okx", 99.99, 100.0, 10.0, 10.0, now)
        frame = pd.DataFrame({
            "ts": pd.to_datetime([now - 1900, now - 1100], unit="s", utc=True),
            "open": [98.0, 99.2], "high": [99.2, 100.1], "low": [97.9, 99.1],
            "close": [99.0, 100.0], "volume": [100.0, 300.0],
        })
        setup = {"symbol": "ETH/USDT", "score": 80, "strategy": "trend_v1",
                 "reference": 100.0, "atr": 2.0, "stop": 98.0, "tp1": 102.0, "tp2": 105.0}
        reviewers = [{"agent": f"r{i}", "decision": "support"} for i in range(50)]
        with TemporaryDirectory() as directory:
            options = SimpleNamespace(team=True, concurrency=10)
            runner = NewsPaperRunner(None, Path(directory), "okx", "ETH/USDT", 1000, 300, options)
            runner.market = SimpleNamespace(quote=AsyncMock(return_value=quote))
            event = runner.journal.observe(item)
            runner.review_slot = True
            with (patch("news_paper.analyze_team", new=AsyncMock(return_value=reviewers)) as team,
                  patch("news_paper.fetch_official_news", new=AsyncMock(return_value=([current], {"fed": "ok"}))),
                  patch("news_paper.assess_outcomes", return_value={"status": "supported", "reason": "fixture"}),
                  patch("news_paper.emit") as output):
                await runner.inspect_event(event, "us_monetary_policy", True, frame, setup)
                await runner.inspect_event(event, "us_monetary_policy", True, frame, setup)
                team.assert_awaited_once()
                self.assertIn("paper_results", team.call_args.args[1])
                self.assertTrue(any(call.args[0].get("agent_count") == 50 for call in output.call_args_list))
            self.assertEqual(len(runner.signals.open_positions()), 0 if revised else 1)
            self.assertEqual(runner.journal.counts()["finished_reviews"], 1)

    async def test_news_to_paper_signal_after_fifty_results(self):
        await self.exercise(revised=False)

    async def test_correction_blocks_signal_before_paper_entry(self):
        await self.exercise(revised=True)
