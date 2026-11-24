import math
import unittest

from news_quant import assess_outcomes

DAY = 86_400
AS_OF = 100 * DAY


def outcome(index, *, day=None, event=None, net=15.0, stressed=10.0, risk=20.0):
    event_at = (day if day is not None else 70 + index // 2) * DAY + 100
    return {
        "outcome_id": f"trade-{index}",
        "event_id": event if event is not None else f"event-{index}",
        "event_at": event_at,
        "opened_at": event_at + 10,
        "closed_at": event_at + 60,
        "available_at": event_at + 70,
        "provenance": "paper_journal",
        "risk_usdt": risk,
        "net_pnl_usdt": net,
        "stressed_net_pnl_usdt": stressed,
    }


def observations():
    return [outcome(i) for i in range(24)]


class NewsQuantTests(unittest.TestCase):
    def test_empty_history_stays_in_watch(self):
        result = assess_outcomes([], as_of=AS_OF)
        self.assertEqual(result["status"], "watch")
        self.assertEqual(result["reason"], "insufficient_history")
        self.assertIsNone(result["lower_stressed_net_r"])

    def test_positive_stressed_lower_bound_supports_paper_candidate(self):
        result = assess_outcomes(observations(), as_of=AS_OF)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["event_count"], 24)
        self.assertEqual(result["day_count"], 12)
        self.assertAlmostEqual(result["mean_net_r"], 0.75)
        self.assertAlmostEqual(result["lower_stressed_net_r"], 0.50)

    def test_same_headline_cannot_create_an_independent_sample_by_repetition(self):
        data = [outcome(i, day=80, event="one-event") for i in range(100)]
        result = assess_outcomes(data, as_of=AS_OF)
        self.assertEqual(result["sample_count"], 100)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["day_count"], 1)
        self.assertEqual(result["status"], "watch")

    def test_distinct_events_same_day_do_not_create_independent_day_blocks(self):
        result = assess_outcomes([outcome(i, day=80) for i in range(30)], as_of=AS_OF)
        self.assertEqual(result["event_count"], 30)
        self.assertEqual(result["day_count"], 1)
        self.assertEqual(result["status"], "watch")

    def test_future_updates_cannot_change_an_earlier_as_of_result(self):
        data = observations()
        later = outcome(90, day=99, net=-100.0, stressed=-130.0)
        later["available_at"] = AS_OF
        later["outcome_id"] = data[0]["outcome_id"]
        past = assess_outcomes(data, as_of=AS_OF)
        held = assess_outcomes(data + [later], as_of=AS_OF)
        self.assertEqual(held["excluded_count"], past["excluded_count"] + 1)
        self.assertEqual({k: v for k, v in held.items() if k != "excluded_count"},
                         {k: v for k, v in past.items() if k != "excluded_count"})

    def test_before_prevents_using_outcomes_published_after_evaluation_cutoff(self):
        data = observations()
        cutoff = data[0]["available_at"]
        result = assess_outcomes(data, as_of=AS_OF, before=cutoff)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["status"], "watch")

    def test_expired_history_is_not_evidence(self):
        data = [outcome(i, day=i // 2) for i in range(24)]
        result = assess_outcomes(data, as_of=140 * DAY)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["status"], "watch")

    def test_losses_when_costs_increase_block_the_signal(self):
        result = assess_outcomes(
            [outcome(i, stressed=-2.0) for i in range(24)], as_of=AS_OF,
        )
        self.assertGreater(result["mean_net_r"], 0)
        self.assertLess(result["lower_stressed_net_r"], 0)
        self.assertEqual(result["reason"], "stressed_edge_not_supported")

    def test_positive_average_with_large_variance_stays_in_watch(self):
        result = assess_outcomes(
            [outcome(i, net=25 if (i // 2) % 2 else -18,
                     stressed=23 if (i // 2) % 2 else -20)
             for i in range(24)], as_of=AS_OF,
        )
        self.assertGreater(result["mean_stressed_net_r"], 0)
        self.assertLess(result["lower_stressed_net_r"], 0)
        self.assertEqual(result["status"], "watch")

    def test_same_receipt_is_counted_once_and_conflicting_receipt_blocks(self):
        data = observations()
        first = data[0]
        same = assess_outcomes(data + [dict(first)], as_of=AS_OF)
        self.assertEqual(same["sample_count"], 24)
        self.assertEqual(same["excluded_count"], 1)
        conflict = assess_outcomes(data + [dict(first, net_pnl_usdt=400.0)], as_of=AS_OF)
        self.assertEqual(conflict["status"], "watch")
        self.assertEqual(conflict["reason"], "invalid_outcome_history")

    def test_same_day_and_event_results_are_measured_on_different_levels(self):
        data = observations()
        # Weight the two receipts for event-0 by their initial risk before
        # giving event-0 and event-1 equal weight within their UTC day.
        data.append(outcome(88, day=70, event="event-0", net=5, stressed=-10, risk=80))
        result = assess_outcomes(data, as_of=AS_OF)
        self.assertEqual(result["sample_count"], 25)
        self.assertEqual(result["event_count"], 24)
        self.assertAlmostEqual(result["mean_net_r"], (11 * 0.75 + (0.2 + 0.75) / 2) / 12)

    def test_bad_receipts_fail_closed(self):
        for changes in (
            {"provenance": "model_guess"},
            {"risk_usdt": True},
            {"net_pnl_usdt": math.nan},
            {"stressed_net_pnl_usdt": 30.0},
            {"opened_at": AS_OF - 100},
            {"event_id": ""},
        ):
            with self.subTest(changes=changes):
                data = observations()
                data[0].update(changes)
                result = assess_outcomes(data, as_of=AS_OF)
                self.assertEqual(result["status"], "watch")
                self.assertEqual(result["reason"], "invalid_outcome_history")

    def test_invalid_clock_cannot_produce_a_result(self):
        for value in (0, math.inf, "100000", True):
            with self.subTest(value=value):
                result = assess_outcomes(observations(), as_of=value)
                self.assertEqual(result["reason"], "invalid_cutoff")
                self.assertEqual(result["status"], "watch")


if __name__ == "__main__":
    unittest.main()
