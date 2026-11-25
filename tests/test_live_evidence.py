"""Tests use synthetic fixtures for the recorded-execution import contract."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from live_evidence import assess_recorded_history, read_recorded_history
from news_quant import DAY_SECONDS, assess_outcomes

NOW = 21_000 * DAY_SECONDS
GROUP = {"family": "us_monetary_policy", "venue": "okx", "symbol": "BTC/USDT"}


def recorded_receipts():
    rows = []
    for day in range(10):
        for event in range(2):
            timestamp = NOW - (day + 1) * DAY_SECONDS + event * 500
            rows.append({
                **GROUP,
                "strategy": "trend_v1",
                "provenance": "recorded_fill",
                "outcome_id": f"receipt-{day}-{event}",
                "event_id": f"event-{day}-{event}",
                "event_at": timestamp,
                "opened_at": timestamp + 10,
                "closed_at": timestamp + 20,
                "available_at": timestamp + 25,
                "risk_usdt": 4,
                "net_pnl_usdt": 3,
                "stressed_net_pnl_usdt": 2,
            })
    return rows


def calendar_receipts(age_in_days):
    rows = recorded_receipts()
    for row, days in zip(rows, age_in_days, strict=True):
        shift = NOW - days * DAY_SECONDS - row["event_at"]
        for timestamp in ("event_at", "opened_at", "closed_at", "available_at"):
            row[timestamp] += shift
    return rows


def assess(rows, *, before=NOW):
    return assess_recorded_history(rows, **GROUP, as_of=NOW, before=before)


class LiveEvidenceTests(TestCase):
    def test_missing_or_empty_history_stays_in_watch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            self.assertEqual(read_recorded_history(None), [])
            self.assertEqual(read_recorded_history(path), [])
            path.write_text("  \n", encoding="utf-8")
            self.assertEqual(read_recorded_history(path), [])
            self.assertEqual(assess(read_recorded_history(path))["reason"], "insufficient_history")

    def test_reads_unicode_export_and_copies_only_supported_columns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            row = dict(recorded_receipts()[0], note="Локальная заметка", secret="not part of the analysis")
            path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual(read_recorded_history(path), [recorded_receipts()[0]])

    def test_rejects_malformed_json_and_non_array_records(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            for invalid in ("{", "{}", "[null]", '[{"risk_usdt": NaN}]', '[{"x":1,"x":2}]'):
                with self.subTest(invalid=invalid):
                    path.write_text(invalid, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Журнал исполнений"):
                        read_recorded_history(path)

    def test_recorded_history_uses_only_the_exact_cohort(self):
        rows = recorded_receipts()
        rows += [dict(rows[0], outcome_id="other", symbol="ETH/USDT")]
        rows += [dict(rows[0], outcome_id="paper", provenance="paper_journal")]
        result = assess(rows)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["provenance"], "recorded_fill")
        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["event_count"], 20)
        self.assertEqual(result["day_count"], 10)
        self.assertAlmostEqual(result["mean_net_r"], 0.75)
        self.assertAlmostEqual(result["lower_stressed_net_r"], 0.5)
        self.assertEqual(result["ignored_other_cohorts"], 1)
        self.assertEqual(result["ignored_unrecorded"], 1)

    def test_six_week_event_cadence_can_support_a_signal(self):
        rows = calendar_receipts(range(42, 841, 42))
        result = assess(rows)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["lookback_days"], 1_095)
        self.assertEqual(result["event_count"], 20)
        self.assertEqual(result["recent_lookback_days"], 180)
        self.assertEqual(result["recent_event_count"], 4)
        self.assertEqual(result["recent_day_count"], 4)
        self.assertAlmostEqual(result["recent_mean_stressed_net_r"], 0.5)

    def test_fewer_than_three_recent_independent_events_blocks_support(self):
        rows = calendar_receipts([100, 150, *range(200, 200 + 18 * 42, 42)])
        extra_trade = dict(rows[0], outcome_id="repeat-trade")
        result = assess(rows + [extra_trade])
        self.assertEqual(result["event_count"], 20)
        self.assertEqual(result["sample_count"], 21)
        self.assertEqual(result["recent_event_count"], 2)
        self.assertEqual(result["reason"], "insufficient_recent_history")
        self.assertEqual(result["status"], "watch")

    def test_recent_cost_stressed_losses_block_positive_long_term_edge(self):
        rows = calendar_receipts(range(42, 841, 42))
        for row in rows[:4]:
            row["stressed_net_pnl_usdt"] = -0.5
        result = assess(rows)
        self.assertGreater(result["lower_stressed_net_r"], 0)
        self.assertEqual(result["recent_event_count"], 4)
        self.assertAlmostEqual(result["recent_mean_stressed_net_r"], -0.125)
        self.assertEqual(result["reason"], "recent_stressed_edge_not_supported")
        self.assertEqual(result["status"], "watch")

    def test_long_and_recent_windows_include_the_start_day(self):
        days = [30, 120, 180, *range(200, 200 + 16 * 42, 42)]
        self.assertEqual(assess(calendar_receipts([*days, 1095]))["status"], "supported")
        result = assess(calendar_receipts([*days, 1096]))
        self.assertEqual(result["recent_event_count"], 3)
        self.assertEqual(result["event_count"], 19)
        self.assertEqual(result["reason"], "insufficient_history")

    def test_paper_and_unknown_provenance_do_not_support_live_decision(self):
        rows = recorded_receipts()
        for row in rows:
            row["provenance"] = "paper_replay"
        result = assess(rows)
        self.assertEqual(result["status"], "watch")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["ignored_unrecorded"], 20)
        self.assertEqual(assess_outcomes(rows, as_of=NOW)["status"], "supported")

    def test_mismatched_or_missing_strategy_does_not_count(self):
        rows = recorded_receipts()
        mismatched = dict(rows[0], outcome_id="different-strategy", strategy="mean_reversion")
        missing = {key: value for key, value in rows[0].items() if key != "strategy"}
        result = assess(rows + [mismatched, missing])
        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["ignored_other_cohorts"], 2)
        self.assertEqual(result["strategy"], "trend_v1")
        self.assertEqual(assess([mismatched, missing])["reason"], "insufficient_history")

    def test_quant_default_does_not_admit_recorded_fills(self):
        self.assertEqual(assess_outcomes(recorded_receipts(), as_of=NOW)["status"], "watch")

    def test_future_receipts_do_not_influence_past_result(self):
        rows = recorded_receipts()
        cutoff = NOW - DAY_SECONDS + 25
        baseline = assess(rows, before=cutoff)
        rows.append(dict(rows[-1], available_at=NOW + 500, risk_usdt=-1, net_pnl_usdt=100_000))
        after = assess(rows, before=cutoff)
        for key in ("status", "reason", "sample_count", "mean_net_r", "lower_stressed_net_r",
                    "recent_event_count", "recent_day_count", "recent_mean_stressed_net_r"):
            self.assertEqual(after[key], baseline[key], key)
        self.assertEqual(baseline["sample_count"], 18)

    def test_duplicate_receipt_counts_once_and_conflicting_receipt_blocks(self):
        rows = recorded_receipts()
        self.assertEqual(assess(rows + [rows[0]])["sample_count"], 20)
        result = assess(rows + [dict(rows[0], net_pnl_usdt=4)])
        self.assertEqual(result["status"], "watch")
        self.assertEqual(result["reason"], "invalid_outcome_history")

    def test_cost_stress_and_chronology_can_prevent_support(self):
        rows = recorded_receipts()
        for row in rows:
            row["stressed_net_pnl_usdt"] = -1
        self.assertEqual(assess(rows)["reason"], "stressed_edge_not_supported")
        rows[0]["opened_at"] = rows[0]["event_at"] - 1
        self.assertEqual(assess(rows)["reason"], "invalid_outcome_history")

    def test_rejects_missing_cohort_and_non_object_records(self):
        self.assertEqual(assess_recorded_history([], **dict(GROUP, venue=""), as_of=NOW,
                                                 before=NOW)["reason"], "invalid_cohort")
        self.assertEqual(assess([None])["reason"], "invalid_outcome_history")
