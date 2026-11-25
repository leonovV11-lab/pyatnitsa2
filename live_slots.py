"""Durable acknowledgements for Telegram signals and manually entered positions."""

import math
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass

MAX_POSITIONS = 16
_REPLY_TRANSITIONS = {
    "enter": ("waiting", "entered"),
    "skip": ("waiting", "skipped"),
    "close": ("entered", "closed"),
}
_UNDELIVERED_REASONS = {
    "skipped": frozenset({"signal_expired", "signal_expired_or_cancelled"}),
    "rejected": frozenset({
        "chat_id_invalid", "chat_mismatch", "rate_limited", "signal_expired", "signal_invalid",
        "telegram_rejected", "token_invalid",
    }),
}


@dataclass(frozen=True)
class OpenPosition:
    """A manually acknowledged position, valued at the original signal's plan."""

    signal_id: str
    venue: str
    symbol: str
    stressed_risk_usdt: float
    cash_required_usdt: float
    issued_at: float


def _known_undelivered(slot) -> bool:
    return (slot["message_id"] is None
            and slot["reason"] in _UNDELIVERED_REASONS.get(slot["delivery_state"], ()))


def _effective_state(slot) -> str:
    if slot["slot_state"] == "waiting" and _known_undelivered(slot):
        return "skipped"
    return slot["slot_state"]


class SlotJournal:
    """One waiting acknowledgement per Telegram route and up to 16 distinct held symbols."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self.connect = connect
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_manual_slots (
                    signal_id TEXT PRIMARY KEY,
                    route TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('waiting','entered','skipped','closed'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS live_manual_slots_waiting
                    ON live_manual_slots(route) WHERE state='waiting';
                CREATE UNIQUE INDEX IF NOT EXISTS live_manual_slots_entered
                    ON live_manual_slots(route,symbol) WHERE state='entered';
            """)

    @staticmethod
    def _waiting(db: sqlite3.Connection, route: str):
        return db.execute("""
            SELECT sl.signal_id, sl.state AS slot_state, d.state AS delivery_state, d.message_id, d.reason
            FROM live_manual_slots sl
            LEFT JOIN telegram_deliveries d ON d.signal_id=sl.signal_id AND d.route=sl.route
            WHERE sl.route=? AND sl.state='waiting'
        """, (route,)).fetchone()

    @staticmethod
    def _held_symbols(db: sqlite3.Connection, route: str) -> set[str]:
        rows = db.execute("SELECT symbol FROM live_manual_slots WHERE route=? AND state='entered'", (route,))
        return {row["symbol"] for row in rows}

    def capacity(
        self, route: str, external_symbols: frozenset[str] = frozenset(), *,
        db: sqlite3.Connection | None = None, max_positions: int = MAX_POSITIONS,
    ) -> str:
        """Return the current reason to postpone sending a new signal, or an empty string."""
        with (closing(self.connect()) if db is None else nullcontext(db)) as connection:
            waiting = self._waiting(connection, route)
            if waiting is not None and not _known_undelivered(waiting):
                return "waiting_for_reply"
            if len(self._held_symbols(connection, route) | external_symbols) >= max_positions:
                return "position_limit_reached"
        return ""

    def reserve(
        self, db: sqlite3.Connection, signal_id: str, route: str, *,
        external_symbols: frozenset[str] = frozenset(), max_positions: int = MAX_POSITIONS,
    ) -> bool:
        """Reserve and reconcile a slot inside the caller's BEGIN IMMEDIATE transaction."""
        if not (db.in_transaction and signal_id and route) or self.capacity(
            route, external_symbols, db=db, max_positions=max_positions,
        ):
            return False
        signal = db.execute("SELECT symbol FROM live_signals WHERE id=? AND status='active'", (signal_id,)).fetchone()
        if signal is None or signal["symbol"] in (self._held_symbols(db, route) | external_symbols):
            return False
        waiting = self._waiting(db, route)
        if waiting is not None:
            db.execute("UPDATE live_manual_slots SET state='skipped' WHERE signal_id=?", (waiting["signal_id"],))
        result = db.execute("""
            INSERT OR IGNORE INTO live_manual_slots (signal_id, route, symbol, state) VALUES (?, ?, ?, 'waiting')
        """, (signal_id, route, signal["symbol"]))
        return result.rowcount == 1

    def respond(self, signal_id: str, action: str, *, route: str, message_id: int) -> str:
        """Apply an authenticated reply for the exact recorded Telegram delivery.

        The caller verifies the owner's identity and callback signature. Return
        entered, skipped or closed on a transition, unchanged for a repeated
        action, or invalid for a mismatched delivery or an invalid transition.
        """
        if (
            not isinstance(action, str) or action not in _REPLY_TRANSITIONS
            or type(message_id) is not int or not 0 < message_id < 2 ** 63
        ):
            return "invalid"
        expected, target = _REPLY_TRANSITIONS[action]
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT sl.state FROM live_manual_slots sl
                JOIN telegram_deliveries d ON d.signal_id=sl.signal_id AND d.route=sl.route
                WHERE sl.signal_id=? AND sl.route=? AND d.state='sent' AND d.message_id=?
            """, (signal_id, route, message_id)).fetchone()
            if row is None:
                return "invalid"
            if row["state"] == target:
                return "unchanged"
            if row["state"] != expected:
                return "invalid"
            db.execute("UPDATE live_manual_slots SET state=? WHERE signal_id=?", (target, signal_id))
            return target

    def skip_pending(self, route: str) -> str:
        """Clear a waiting slot with a terminal, unverified delivery on explicit owner request."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._waiting(db, route)
            if row is None:
                return "unchanged"
            if row["delivery_state"] not in {"unconfirmed", "rejected", "skipped"}:
                return "invalid"
            db.execute("UPDATE live_manual_slots SET state='skipped' WHERE signal_id=?", (row["signal_id"],))
            return "skipped"

    def state(self, signal_id: str, *, route: str, db: sqlite3.Connection | None = None) -> str | None:
        with (closing(self.connect()) if db is None else nullcontext(db)) as connection:
            row = connection.execute("""
                SELECT sl.state AS slot_state, d.state AS delivery_state, d.message_id, d.reason
                FROM live_manual_slots sl
                LEFT JOIN telegram_deliveries d ON d.signal_id=sl.signal_id AND d.route=sl.route
                WHERE sl.signal_id=? AND sl.route=?
            """, (signal_id, route)).fetchone()
            return _effective_state(row) if row is not None else None

    def open_positions(self, route: str | None, *, db: sqlite3.Connection | None = None) -> tuple[OpenPosition, ...]:
        """Return manually entered positions until a manual close is recorded."""
        with (closing(self.connect()) if db is None else nullcontext(db)) as connection:
            rows = connection.execute("""
                SELECT sl.signal_id, s.venue, sl.symbol, s.risk AS stressed_risk_usdt,
                    COALESCE(json_extract(s.payload,'$.plan.notional_usdt'),
                             json_extract(s.payload,'$.plan.qty') * json_extract(s.payload,'$.plan.price'),
                             0.0)
                    * (1 + COALESCE(json_extract(s.payload,'$.plan.fee_rate'), 0.0)) AS cash_required_usdt, s.issued_at
                FROM live_manual_slots sl
                JOIN live_signals s ON s.id=sl.signal_id
                WHERE sl.state='entered' AND (? IS NULL OR sl.route=?)
                ORDER BY s.issued_at, sl.signal_id
            """, (route, route))
            return tuple(OpenPosition(**dict(row)) for row in rows)

    def report(self, route: str | None = None) -> dict:
        """Summarize only the manual ledger's own observations and planned exposure."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            rows = db.execute("""
                SELECT sl.state AS slot_state, d.state AS delivery_state, d.message_id, d.reason
                FROM live_manual_slots sl
                LEFT JOIN telegram_deliveries d ON d.signal_id=sl.signal_id AND d.route=sl.route
                WHERE (? IS NULL OR sl.route=?)
            """, (route, route))
            counts = Counter(_effective_state(row) for row in rows)
            positions = self.open_positions(route, db=db)
        return {
            "max_positions": MAX_POSITIONS,
            "waiting_for_reply": counts["waiting"],
            "open_count": len(positions),
            "open_symbols": sorted({position.symbol for position in positions}),
            "stressed_risk_usdt": math.fsum(position.stressed_risk_usdt for position in positions),
            "cash_required_usdt": math.fsum(position.cash_required_usdt for position in positions),
            "skipped_count": counts["skipped"],
            "closed_count": counts["closed"],
        }
