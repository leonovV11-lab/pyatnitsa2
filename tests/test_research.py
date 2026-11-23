import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research import Research, candidate_key, review_key


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "paper.sqlite"
        self.store = Research(self.path)
        self.candidate = candidate_key("okx", "BTC/USDT", "scalp-v1", 300)

    def record_candidate(self, **overrides):
        args = {
            "exchange": "okx",
            "symbol": "BTC/USDT",
            "strategy": "scalp-v1",
            "candle": 300,
            "observed_at": 310,
            "payload": {"ask": 100},
            "decision": "eligible",
        }
        return self.store.record_candidate(self.candidate, **(args | overrides))

    def test_candidate_snapshot_is_immutable(self):
        self.assertTrue(self.record_candidate())
        self.assertFalse(self.record_candidate(payload={"ask": 102}, observed_at=340))
        restored = Research(self.path).get_candidate(self.candidate)
        self.assertEqual(restored["payload"], {"ask": 100})
        self.assertEqual(restored["observed_at"], 310)
        self.assertNotEqual(self.candidate, candidate_key("binance", "BTC/USDT", "scalp-v1", 300))
        self.assertNotEqual(self.candidate, candidate_key("okx", "BTC/USDT", "scalp-v2", 300))

    def test_event_key_stays_deduplicated_after_reopening(self):
        self.record_candidate()
        self.assertTrue(self.store.record_event(
            self.candidate, stage="notification", decision="sent", event_key="delivery",
            observed_at=315,
        ))
        new_store = Research(self.path)
        self.assertFalse(new_store.record_event(
            self.candidate, stage="notification", decision="sent", event_key="delivery",
            observed_at=340,
        ))
        self.assertEqual(new_store.report(now=400)["events_by_stage"], {"notification": {"sent": 1}})

    def test_only_one_worker_claims_a_review(self):
        self.record_candidate()
        other = Research(self.path)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda store: store.claim_review([self.candidate], "model-v1", now=320),
                [self.store, other],
            ))
        self.assertEqual(sum(map(len, results)), 1)
        self.assertTrue(other.finish_review(
            self.candidate, "model-v1", "watch", "thin_book", now=323, latency_ms=3000,
        ))
        self.assertFalse(self.store.finish_review(self.candidate, "model-v1", "approve", now=325))
        report = other.report(now=400)
        self.assertEqual(report["reviews_by_decision"], {"watch": 1})
        self.assertEqual(report["reviewers"]["model-v1"]["watch"]["mean_latency_ms"], 3000)
        self.assertEqual(other.report(now=321)["reviews_by_decision"], {"pending": 1})

    def test_crashed_claim_is_unavailable_and_not_rebilled(self):
        self.record_candidate()
        self.assertEqual(self.store.claim_review([self.candidate], "model-v1", now=315), [self.candidate])
        other = Research(self.path)
        self.assertEqual(other.get_review(self.candidate, "model-v1", now=330)["decision"], "pending")
        self.assertEqual(other.report(now=380)["reviews_by_decision"], {"unavailable": 1})
        self.assertEqual(other.claim_review([self.candidate], "model-v1", now=380), [])
        self.assertEqual(other.get_review(self.candidate, "model-v1", now=380)["reason"], "claim_timed_out")
        self.assertEqual(other.claim_review([self.candidate], "model-v2", now=380), [self.candidate])
        self.assertNotEqual(review_key("model-v1", "Prompt 1"), review_key("model-v1", "Prompt 2"))

    def test_late_provider_result_is_not_promoted_to_approval(self):
        self.record_candidate()
        self.store.claim_review([self.candidate], "model-v1", now=315)
        self.store.finish_review(self.candidate, "model-v1", "approve", now=380)
        report = self.store.report(now=390)
        self.assertEqual(report["reviews_by_decision"], {"unavailable": 1})

    def test_report_filters_candidate_and_event_by_observation_time(self):
        self.record_candidate(decision="reject", reason="stale_quote")
        self.store.record_event(self.candidate, stage="quote", decision="available", observed_at=340)
        self.assertEqual(self.store.report(now=325)["reasons"], {"reject": {"stale_quote": 1}})
        self.assertEqual(self.store.report(since=330, now=350)["candidates"], 0)
        self.assertEqual(self.store.report(since=330, now=350)["events_by_stage"], {"quote": {"available": 1}})
        self.assertEqual(self.store.report(since=330, now=335)["events_by_stage"], {})

    def test_delivery_reservations_are_looked_up_in_one_batch(self):
        self.record_candidate()
        self.store.record_event(
            self.candidate, stage="delivery", decision="attempted",
            event_key=f"push:{self.candidate}",
        )
        self.assertEqual(self.store.attempted_deliveries([]), set())
        restored = Research(self.path)
        self.assertEqual(
            restored.attempted_deliveries(["missing", self.candidate, self.candidate]),
            {self.candidate},
        )

    def test_pace_counts_distinct_signals_and_only_causal_delivery_delays(self):
        def record(candidate, stamp, decision="eligible"):
            self.store.record_candidate(
                candidate, exchange="okx", symbol="BTC/USDT", strategy="baseline-v1",
                candle=stamp, observed_at=stamp, payload={}, decision=decision,
            )

        def event(candidate, stage, decision, stamp, reason=""):
            self.store.record_event(
                candidate, stage=stage, decision=decision, observed_at=stamp, reason=reason,
            )

        record("one", 305)
        event("one", "quote", "watch", 310, "Нет свежей котировки")
        event("one", "quote", "tradable", 315)
        event("one", "quote", "tradable", 330)
        event("one", "delivery", "sent", 340)
        event("one", "delivery", "sent", 345)
        event("one", "confirmation", "opened", 350)
        event("one", "confirmation", "opened", 351)

        record("two", 312)
        event("two", "quote", "tradable", 322)
        event("two", "delivery", "sent", 332)
        event("two", "confirmation", "rejected", 340, "stale_quote")

        record("old", 100)
        event("old", "quote", "tradable", 200)
        event("old", "delivery", "sent", 350)

        record("ordering", 310)
        event("ordering", "delivery", "sent", 342)
        event("ordering", "quote", "tradable", 342)
        record("rejected", 315, "reject")
        record("future", 400)

        self.assertEqual(self.store.pace_report(300, now=360), {
            "candidates": 5,
            "eligible": 4,
            "tradable": 3,
            "delivered": 4,
            "confirmed": 1,
            "stale_rejections": 2,
            "delivery_delay_samples": 2,
            "median_delivery_delay_seconds": 17.5,
        })


if __name__ == "__main__":
    unittest.main()
