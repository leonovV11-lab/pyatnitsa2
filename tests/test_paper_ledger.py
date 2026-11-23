import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from paper_ledger import LedgerError, PaperLedger


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portfolio.sqlite3"
        self.ledger = PaperLedger(self.path)
        self.ledger.configure(1_000, 750, now=0)

    def open(self, candidate="first", symbol="ABC/USDT", qty=2.0, now=1):
        return self.ledger.open(candidate, "okx", symbol, qty, 100, 95, 105, 110, 0.001, now=now, bid=99.99)

    def test_crossing_both_targets_atomically_closes_remaining_quantity(self):
        position = self.open()
        fills = self.ledger.mark(position["position_id"], 112, now=2)
        self.assertEqual([fill["reason"] for fill in fills], ["tp1", "tp2"])
        self.assertEqual([fill["qty"] for fill in fills], [1, 1])
        self.assertEqual([fill["remaining"] for fill in fills], [1, 0])
        self.assertEqual(self.ledger.open_positions(), [])
        report = self.ledger.report(now=2)
        expected = 2 * (112 * 0.999 - 100 * 1.001)
        self.assertAlmostEqual(report["net_pnl"], expected)
        self.assertAlmostEqual(report["realized_pnl"], expected)
        self.assertAlmostEqual(report["fees"], 0.424)
        self.assertEqual(report["closed_count"], 1)
        self.assertEqual(report["win_rate"], 1)
        self.assertEqual([fill["mode"] for fill in self.ledger.recent_fills()], ["paper"] * 3)

    def test_restart_retains_partial_fill_and_fee_adjusted_stop(self):
        position = self.open()
        position_id = position["position_id"]
        self.ledger.mark(position_id, 106, now=2)
        restarted = PaperLedger(self.path)
        remaining = restarted.open_positions()[0]
        self.assertEqual(remaining["qty"], 1)
        self.assertTrue(remaining["tp1_hit"])
        self.assertAlmostEqual(remaining["stop"], 100 * 1.001 / 0.999)
        self.assertEqual(restarted.mark(position_id, 107, now=3), [])
        self.assertEqual(restarted.mark(position_id, 100.1, now=4)[0]["reason"], "stop")

    def test_stale_quote_is_ignored_and_timeout_uses_executable_bid(self):
        position = self.open(now=2)
        position_id = position["position_id"]
        self.assertEqual(self.ledger.mark(position_id, 90, now=1), [])
        fills = self.ledger.mark(position_id, 101, now=122, max_age_seconds=120)
        self.assertEqual(fills[0]["reason"], "timeout")
        self.assertEqual(fills[0]["price"], 101)

    def test_thin_stop_marks_drawdown_and_survives_restart_and_price_recovery(self):
        position_id = self.open()["position_id"]
        self.assertEqual(self.ledger.mark(position_id, 90, now=2, fillable=False), [])
        self.assertEqual(self.ledger.open_positions()[0]["pending_exit"], "stop")
        self.assertGreater(self.ledger.report(now=2)["max_drawdown"], 20)
        restarted = PaperLedger(self.path)
        self.assertEqual(restarted.mark(position_id, 110, now=1.5), [])
        fills = restarted.mark(position_id, 101, now=3)
        self.assertEqual([(fill["reason"], fill["price"], fill["qty"]) for fill in fills], [("stop", 101, 2)])
        self.assertEqual(restarted.open_positions(), [])

    def test_pending_targets_escalate_and_stop_takes_precedence(self):
        position_id = self.open()["position_id"]
        self.assertEqual(self.ledger.mark(position_id, 106, now=2, fillable=False), [])
        self.assertEqual(self.ledger.open_positions()[0]["pending_exit"], "tp1")
        self.assertEqual(self.ledger.mark(position_id, 111, now=3, fillable=False), [])
        self.assertEqual(self.ledger.open_positions()[0]["pending_exit"], "tp2")
        fills = self.ledger.mark(position_id, 94, now=4)
        self.assertEqual([(fill["reason"], fill["qty"]) for fill in fills], [("stop", 2)])

    def test_pending_tp2_executes_both_legs_on_retracement(self):
        position_id = self.open()["position_id"]
        self.ledger.mark(position_id, 112, now=2, fillable=False)
        fills = self.ledger.mark(position_id, 102, now=3)
        self.assertEqual([(fill["reason"], fill["qty"], fill["price"]) for fill in fills],
                         [("tp1", 1, 102), ("tp2", 1, 102)])

    def test_thin_timeout_waits_for_next_fillable_quote(self):
        position_id = self.open()["position_id"]
        self.assertEqual(self.ledger.mark(position_id, 100, now=121, max_age_seconds=120, fillable=False), [])
        self.assertEqual(self.ledger.open_positions()[0]["pending_exit"], "timeout")
        fills = self.ledger.mark(position_id, 104, now=122)
        self.assertEqual([(fill["reason"], fill["price"]) for fill in fills], [("timeout", 104)])

    def test_pending_exit_migration_preserves_active_position(self):
        position_id = self.open()["position_id"]
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE paper_positions DROP COLUMN pending_exit")
        restarted = PaperLedger(self.path)
        restored = restarted.open_positions()[0]
        self.assertEqual(restored["position_id"], position_id)
        self.assertIsNone(restored["pending_exit"])
        restarted.mark(position_id, 90, now=2, fillable=False)
        self.assertEqual(restarted.open_positions()[0]["pending_exit"], "stop")

    def test_confirmation_again_twice_only_opens_once(self):
        def attempt():
            try:
                self.open()
                return True
            except LedgerError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        position_id = self.ledger.open_positions()[0]["position_id"]
        self.ledger.close(position_id, 100, now=2)
        with self.assertRaisesRegex(LedgerError, "уже исполнен"):
            self.open(now=3)

    def test_check_open_reuses_execution_limits_without_reserving_cash(self):
        order = ("candidate", "okx", "ABC/USDT", 2.0, 100, 95, 105, 110, .001)
        self.ledger.check_open(*order, now=1, bid=99.99)
        self.assertEqual(self.ledger.open_positions(), [])
        self.assertEqual(self.ledger.report(now=1)["cash"], 1000)
        self.ledger.open(*order, now=2, bid=99.99)
        with self.assertRaisesRegex(LedgerError, "уже исполнен"):
            self.ledger.check_open(*order, now=3, bid=99.99)
        unsafe = ("next", "okx", "NEXT/USDT", 3.0, 100, 95, 105, 110, .001)
        for operation in (self.ledger.open, self.ledger.check_open):
            with self.assertRaisesRegex(LedgerError, "одной позиции"):
                operation(*unsafe, now=3, bid=99.99)

    def test_limits_validate_numbers_and_reserve_risk(self):
        with self.assertRaises(LedgerError):
            self.open(qty=float("nan"))
        with self.assertRaises(LedgerError):
            self.ledger.configure(float("inf"), 100)
        with self.assertRaisesRegex(LedgerError, "одной позиции"):
            self.open(qty=3)
        first = self.open()
        self.open("second", "DEF/USDT")
        with self.assertRaisesRegex(LedgerError, "суммарный риск"):
            self.open("third", "GHI/USDT")
        self.ledger.mark(first["position_id"], 92, now=3)
        with self.assertRaisesRegex(LedgerError, "дневной лимит"):
            self.open("third", "GHI/USDT", now=4)
        self.assertLess(self.ledger.report(now=4)["daily_realized_pnl"], 0)
        self.open("next-day", "GHI/USDT", now=86_400)

    def test_configure_never_resets_existing_pnl(self):
        position = self.open()
        self.ledger.close(position["position_id"], 97, now=2)
        old_report = self.ledger.report(now=2)
        new_report = self.ledger.configure(1000, 700, now=3)
        self.assertAlmostEqual(new_report["equity"], old_report["equity"])
        self.assertEqual(new_report["daily_budget"], 700)
        self.assertGreater(new_report["max_drawdown"], 0)
        with self.assertRaisesRegex(LedgerError, "фиксирован"):
            self.ledger.configure(1200, 700, now=4)

    def test_pace_counts_complete_rounds_and_includes_their_prior_partial_fills(self):
        old = self.open("old", "OLD/USDT", .5, now=1)["position_id"]
        win = self.open("win", "WIN/USDT", .5, now=3)["position_id"]
        self.ledger.mark(win, 106, now=80)
        self.ledger.close(old, 97, now=100)
        flat = self.open("flat", "FLAT/USDT", .5, now=110)["position_id"]
        loss = self.open("loss", "LOSS/USDT", .5, now=115)["position_id"]
        self.ledger.mark(win, 110, now=180)
        self.ledger.close(flat, 100 * 1.001 / .999, now=190)
        later = self.open("later", "LATER/USDT", .5, now=191)["position_id"]
        self.ledger.mark(later, 106, now=195)
        self.ledger.close(loss, 98, now=200)
        self.ledger.close(later, 105, now=201)

        pace = self.ledger.pace_report(now=200, window_seconds=100)
        expected_win = .25 * (106 + 110) * .999 - 50 * 1.001
        expected_loss = .5 * 98 * .999 - 50 * 1.001
        self.assertEqual((pace["completed"], pace["wins"], pace["losses"], pace["flat"]), (3, 1, 1, 1))
        self.assertAlmostEqual(pace["realized_net_pnl"], expected_win + expected_loss)
        self.assertAlmostEqual(pace["average_trade"], (expected_win + expected_loss) / 3)
        self.assertAlmostEqual(pace["average_holding_seconds"], (177 + 80 + 85) / 3)
        expected_fees = .001 * (150 + .25 * (106 + 110) + .5 * (98 + 100 * 1.001 / .999))
        self.assertAlmostEqual(pace["round_trip_fees"], expected_fees)

    def test_pace_empty_window_and_validation(self):
        pace = self.ledger.pace_report(now=10)
        self.assertEqual(pace["window_seconds"], 86400)
        self.assertEqual(pace["completed"], 0)
        self.assertEqual(pace["realized_net_pnl"], 0)
        self.assertIsNone(pace["average_trade"])
        self.assertIsNone(pace["average_holding_seconds"])
        for invalid in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(window_seconds=invalid), self.assertRaises(LedgerError):
                self.ledger.pace_report(now=10, window_seconds=invalid)

    def test_archives_legacy_without_creating_unresolved_positions(self):
        legacy = self.path.with_name("portfolio.json")
        raw = json.dumps({"open": {"symbol": "ABC/USDT"}, "history": [{}]}).encode()
        legacy.write_bytes(raw)
        archived = PaperLedger(self.path, legacy_path=legacy)
        report = archived.report(now=0)
        self.assertEqual(report["legacy_open_count"], 1)
        self.assertEqual(report["legacy_history_count"], 1)
        self.assertEqual(archived.open_positions(), [])
        self.assertEqual(legacy.read_bytes(), raw)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT snapshot FROM legacy_archive").fetchone()[0], raw)

    def test_corrupt_legacy_is_preserved_and_disclosed(self):
        legacy = self.path.with_name("portfolio.json")
        legacy.write_bytes(b"{broken")
        archived = PaperLedger(self.path, legacy_path=legacy)
        self.assertIsNotNone(archived.report(now=0)["legacy_error"])
        self.assertEqual(legacy.read_bytes(), b"{broken")

    def test_backup_is_consistent_and_cannot_overwrite_existing_snapshot(self):
        position = self.open()
        destination = self.path.with_name("backup.sqlite3")
        self.ledger.backup(destination)
        self.ledger.close(position["position_id"], 100, now=2)
        snapshot = PaperLedger(destination)
        self.assertEqual(snapshot.open_positions()[0]["position_id"], position["position_id"])
        with self.assertRaises(FileExistsError):
            self.ledger.backup(destination)

    def test_factual_fills_are_idempotent_and_separate_from_paper_capital(self):
        pos = self.ledger.record_manual_open("order-1", "okx", "ABC/USDT", 3, 102, .3, now=1)
        fill = self.ledger.record_manual_close("order-2", pos["position_id"], 1, 105, .08, now=2)
        self.assertEqual(fill["remaining"], 2)
        self.assertAlmostEqual(fill["pnl"], 2.82)
        with self.assertRaisesRegex(LedgerError, "уже записано"):
            self.ledger.record_manual_close("order-2", pos["position_id"], 1, 105, .08, now=2)
        self.assertEqual(self.ledger.report(now=2)["equity"], 1000)
        manual = self.ledger.manual_report()
        self.assertEqual(manual["open_count"], 1)
        self.assertAlmostEqual(manual["realized_pnl"], 2.82)
        self.assertAlmostEqual(manual["fees"], 0.38)
        self.assertEqual([fill["mode"] for fill in manual["recent_fills"]], ["manual"] * 2)
