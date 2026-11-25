import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from live_slots import SlotJournal

NOW = 1_800_000_000
OWNER = "bot:owner"


class LiveSlotTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "news-live.sqlite3"
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE live_signals (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, revision TEXT NOT NULL,
                    source TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
                    risk REAL NOT NULL, issued_at REAL NOT NULL, expires_at REAL NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT,
                    UNIQUE(event_id, venue, symbol)
                );
                CREATE TABLE telegram_deliveries (
                    signal_id TEXT PRIMARY KEY, route TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    next_attempt_at REAL NOT NULL, attempted_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER, reason TEXT, notice_state TEXT NOT NULL DEFAULT 'waiting',
                    notice_attempted_at REAL, notice_reason TEXT
                );
            """)
        self.slots = SlotJournal(self.connect)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def queue(self, signal_id, symbol="BTC/USDT", *, route=OWNER, external_symbols=frozenset(), notional=None):
        plan = {"qty": 2, "price": 100, "fee_rate": 0.01}
        if notional is not None:
            plan["notional_usdt"] = notional
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT INTO live_signals (
                    id, event_id, revision, source, venue, symbol, risk, issued_at, expires_at, status, payload
                ) VALUES(?, ?, 'revision', 'fed', 'okx', ?, 3.25, ?, ?, 'active', ?)
            """, (signal_id, f"event-{signal_id}", symbol, NOW, NOW + 5, json.dumps({"plan": plan})))
            if not self.slots.reserve(db, signal_id, route, external_symbols=external_symbols):
                db.rollback()
                return False
            db.execute("INSERT INTO telegram_deliveries (signal_id, route, next_attempt_at) VALUES (?, ?, ?)",
                       (signal_id, route, NOW))
            return True

    def delivery(self, signal_id, state, *, message_id=None, reason=None):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE telegram_deliveries SET state=?, message_id=?, reason=? WHERE signal_id=?",
                       (state, message_id, reason, signal_id))

    def reply(self, signal_id, action, message_id=10, *, route=OWNER):
        return self.slots.respond(signal_id, action, route=route, message_id=message_id)

    def test_one_waiting_signal_survives_restart_and_quote_expiry_until_manual_reply(self):
        self.assertTrue(self.queue("btc"))
        self.delivery("btc", "sent", message_id=10)
        with closing(self.connect()) as db, db:
            db.execute("UPDATE live_signals SET status='expired', expires_at=issued_at WHERE id='btc'")
        self.slots = SlotJournal(self.connect)
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.assertFalse(self.queue("eth", "ETH/USDT"))
        self.assertEqual(self.slots.state("btc", route=OWNER), "waiting")
        self.assertEqual(self.reply("btc", "enter"), "entered")
        self.assertEqual(self.slots.capacity(OWNER), "")
        self.assertEqual(self.slots.state("btc", route=OWNER), "entered")
        self.assertFalse(self.queue("other-btc"))
        self.assertTrue(self.queue("eth", "ETH/USDT"))

    def test_response_binds_to_real_sent_delivery_route_and_message(self):
        self.assertTrue(self.queue("one"))
        self.assertEqual(self.reply("one", "enter"), "invalid")
        self.delivery("one", "sent", message_id=10)
        for signal, action, route, message in (
            ("one", "enter", "other", 10), ("one", "enter", OWNER, 11), ("one", "enter", OWNER, True),
            ("one", "open", OWNER, 10), ("one", "close", OWNER, 10), ("nope", "enter", OWNER, 10),
        ):
            with self.subTest(signal=signal, action=action, route=route, message=message):
                self.assertEqual(self.reply(signal, action, message, route=route), "invalid")
        self.assertEqual(self.reply("one", "skip"), "skipped")
        self.assertEqual(self.reply("one", "skip"), "unchanged")
        self.assertEqual(self.reply("one", "enter"), "invalid")

    def test_entered_slot_remains_counted_until_idempotent_manual_close(self):
        self.assertTrue(self.queue("first", "ETH/EUR", notional=320))
        self.delivery("first", "sent", message_id=10)
        self.assertEqual(self.reply("first", "enter"), "entered")
        self.assertEqual(self.reply("first", "enter"), "unchanged")
        self.assertEqual(self.reply("first", "skip"), "invalid")
        self.slots = SlotJournal(self.connect)
        (held,) = self.slots.open_positions(OWNER)
        self.assertEqual((held.signal_id, held.symbol, held.venue, held.issued_at), ("first", "ETH/EUR", "okx", NOW))
        self.assertAlmostEqual(held.stressed_risk_usdt, 3.25)
        self.assertAlmostEqual(held.cash_required_usdt, 323.2)
        self.assertEqual(self.slots.open_positions("other"), ())
        self.assertEqual(self.reply("first", "close"), "closed")
        self.assertEqual(self.reply("first", "close"), "unchanged")
        self.assertEqual(self.slots.open_positions(OWNER), ())
        self.assertEqual(self.slots.report()["closed_count"], 1)

    def test_sixteen_distinct_slots_and_external_symbol_union(self):
        self.assertTrue(self.queue("btc"))
        self.delivery("btc", "sent", message_id=10)
        self.assertEqual(self.reply("btc", "enter"), "entered")
        external = frozenset({"BTC/USDT", *(f"X{i}/USDT" for i in range(14))})
        self.assertEqual(self.slots.capacity(OWNER, external), "")
        self.assertEqual(self.slots.capacity(OWNER, external | {"NEW/USDT"}), "position_limit_reached")
        self.assertFalse(self.queue("same-as-external", "X0/USDT", external_symbols=external))
        self.assertTrue(self.queue("eth", "ETH/USDT", external_symbols=external))
        self.delivery("eth", "sent", message_id=11)
        self.assertEqual(self.reply("eth", "enter", 11), "entered")
        self.assertEqual(self.slots.capacity(OWNER, external), "position_limit_reached")
        report = self.slots.report(OWNER)
        self.assertEqual(report["max_positions"], 16)
        self.assertEqual(report["open_count"], 2)
        self.assertEqual(report["open_symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertAlmostEqual(report["stressed_risk_usdt"], 6.5)
        self.assertAlmostEqual(report["cash_required_usdt"], 404)

    def test_unconfirmed_delivery_blocks_until_explicit_recovery(self):
        self.assertTrue(self.queue("first"))
        self.assertEqual(self.slots.skip_pending(OWNER), "invalid")
        self.delivery("first", "sending")
        self.assertEqual(self.slots.skip_pending(OWNER), "invalid")
        self.delivery("first", "unconfirmed", reason="delivery_confirmation_missing")
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.assertEqual(self.reply("first", "skip"), "invalid")
        self.assertEqual(self.slots.skip_pending(OWNER), "skipped")
        self.assertEqual(self.slots.skip_pending(OWNER), "unchanged")
        self.assertEqual(self.slots.report()["skipped_count"], 1)
        self.assertTrue(self.queue("second", "ETH/USDT"))
        self.delivery("second", "sent", message_id=11)
        self.assertEqual(self.slots.skip_pending(OWNER), "invalid")

    def test_known_undelivered_removes_waiting_but_ambiguous_rejection_does_not(self):
        self.assertTrue(self.queue("first"))
        self.delivery("first", "rejected", reason=None)
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.delivery("first", "rejected", message_id=10, reason="telegram_rejected")
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.delivery("first", "rejected", reason="telegram_rejected")
        self.assertEqual(self.slots.capacity(OWNER), "")
        self.assertEqual(self.slots.state("first", route=OWNER), "skipped")
        self.assertEqual(self.slots.report()["skipped_count"], 1)
        self.assertTrue(self.queue("second", "ETH/USDT"))
        self.delivery("second", "skipped", reason="signal_expired_or_cancelled")
        self.assertEqual(self.slots.capacity(OWNER), "")
        self.assertTrue(self.queue("third", "ADA/USDT"))
        with closing(self.connect()) as db:
            first = db.execute("SELECT state FROM live_manual_slots WHERE signal_id='first'").fetchone()
            self.assertEqual(first["state"], "skipped")
        self.assertEqual(self.slots.report()["waiting_for_reply"], 1)

    def test_unknown_terminal_delivery_reason_can_only_be_cleared_manually(self):
        self.assertTrue(self.queue("first"))
        self.delivery("first", "rejected")
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.assertEqual(self.slots.skip_pending(OWNER), "skipped")
        self.assertEqual(self.slots.capacity(OWNER), "")

    def test_independent_routes_and_atomic_single_waiting_claim(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(self.queue, ("one", "two"), ("BTC/USDT", "ETH/USDT")))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(self.slots.capacity(OWNER), "waiting_for_reply")
        self.assertTrue(self.queue("foreign", "SOL/USDT", route="different-bot"))
        self.assertEqual(self.slots.report()["waiting_for_reply"], 2)
        self.assertEqual(self.slots.report(OWNER)["waiting_for_reply"], 1)

    def test_slot_insert_is_rolled_back_with_caller_transaction(self):
        self.assertTrue(self.queue("first"))
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM live_manual_slots WHERE signal_id='first'")
            db.execute("DELETE FROM telegram_deliveries WHERE signal_id='first'")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.slots.reserve(db, "first", OWNER))
            db.rollback()
        self.assertEqual(self.slots.capacity(OWNER), "")
        self.assertIsNone(self.slots.state("first", route=OWNER))
