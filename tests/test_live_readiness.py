"""Pre-trade diagnostics use synthetic operator-provided snapshots and executions."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from live_readiness import readiness_report
from news_quant import DAY_SECONDS

NOW = 21_000 * DAY_SECONDS


def recorded_history():
    rows = []
    for i in range(20):
        event_at = NOW - (i // 2 + 1) * DAY_SECONDS + i * 30
        rows.append({
            "family": "us_monetary_policy", "venue": "okx", "symbol": "BTC/USDT", "strategy": "trend_v1",
            "provenance": "recorded_fill", "outcome_id": f"synthetic-receipt-{i}", "event_id": f"event-{i}",
            "event_at": event_at, "opened_at": event_at + 1, "closed_at": event_at + 2,
            "available_at": event_at + 3, "risk_usdt": 3, "net_pnl_usdt": 2, "stressed_net_pnl_usdt": 1,
        })
    return rows


class LiveReadinessTests(TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.options = SimpleNamespace(
            venue="okx", symbols=["BTC/USDT"], all_pairs=False, telegram=True, team=True,
            manual_replies=True, budget=137.53, fee_rate=0.001, risk_cap=1.25, daily_risk_cap=None,
            portfolio_file=self.directory / "private-portfolio.json",
            history_file=self.directory / "private-history.json",
        )

    def write_portfolio(self, **changes):
        snapshot = {"observed_at": NOW - 1, "available_cash_usdt": 117.5113, "positions": [], **changes}
        self.options.portfolio_file.write_text(json.dumps(snapshot), encoding="utf8")

    def write_history(self, rows=None):
        self.options.history_file.write_text(json.dumps(recorded_history() if rows is None else rows), encoding="utf8")

    def report(self):
        return readiness_report(self.options, as_of=NOW)

    def test_missing_inputs_keep_the_monitor_in_research(self):
        self.options.budget = self.options.fee_rate = self.options.risk_cap = None
        self.options.team = self.options.manual_replies = False
        self.options.portfolio_file = self.options.history_file = None
        report = self.report()
        self.assertEqual(report["status"], "research_only")
        self.assertEqual(set(report["blockers"]), {
            "sizing_missing", "team_disabled", "portfolio_missing", "history_missing",
        })
        self.assertEqual(report["manual_replies"], "disabled")

    def test_invalid_history_and_stale_snapshot_are_reported_without_sensitive_values(self):
        self.options.history_file.write_text("{synthetic-sensitive-content", encoding="utf8")
        self.write_portfolio(observed_at=NOW - 301)
        report = self.report()
        self.assertEqual(report["status"], "research_only")
        self.assertEqual(set(report["blockers"]), {"portfolio_unavailable", "history_unavailable"})
        serialized = json.dumps(report)
        for private_value in ("synthetic-sensitive-content", "private-history.json", "117.5113", "137.53"):
            self.assertNotIn(private_value, serialized)

    def test_wrong_venue_symbol_strategy_and_non_recorded_rows_do_not_support_readiness(self):
        self.write_portfolio()
        for changes in (
            {"venue": "binance"}, {"symbol": "ETH/USDT"}, {"strategy": "different"},
            {"provenance": "public_market_quotes"}, {"family": []},
        ):
            with self.subTest(changes=changes):
                self.write_history([dict(row, **changes) for row in recorded_history()])
                report = self.report()
                self.assertEqual(report["status"], "research_only")
                self.assertEqual(report["recorded_history"]["eligible_cohorts"], 0)
                self.assertIn("history_not_comparable", report["blockers"])

    def test_positive_cohort_allows_candidate_review_but_conflicting_receipts_block_it(self):
        self.write_portfolio()
        self.write_history()
        report = self.report()
        self.assertEqual(report["status"], "candidate_review_required")
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["recorded_history"]["supported_cohorts"], 1)
        self.assertNotIn("synthetic-receipt-", json.dumps(report))
        rows = recorded_history()
        self.write_history([*rows, {**rows[0], "net_pnl_usdt": 4}])
        report = self.report()
        self.assertEqual(report["status"], "research_only")
        self.assertEqual(report["recorded_history"]["supported_cohorts"], 0)
        self.assertIn("history_unqualified", report["blockers"])

    def test_daily_cap_requires_manual_replies_and_a_positive_in_budget_value(self):
        self.write_portfolio()
        self.write_history()
        self.options.daily_risk_cap = float("inf")
        self.assertIn("daily_risk_cap_invalid", self.report()["blockers"])
        self.options.daily_risk_cap = 0.75
        self.options.manual_replies = False
        self.assertIn("daily_risk_cap_invalid", self.report()["blockers"])
        self.options.manual_replies = True
        self.options.telegram = False
        self.assertIn("manual_replies_unavailable", self.report()["blockers"])

    def test_empty_history_and_no_cash_do_not_pass_preconditions(self):
        self.write_portfolio(available_cash_usdt=0)
        self.write_history([])
        report = self.report()
        self.assertEqual(report["status"], "research_only")
        self.assertEqual(set(report["blockers"]), {"portfolio_cash_unavailable", "history_empty"})
