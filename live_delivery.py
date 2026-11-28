"""Persist one Telegram delivery per live signal and track subsequent card edits."""

import asyncio
import json
import math
import time
from contextlib import closing

from live_telegram import edit_signal, send_signal
from news_journal import emit

ATTEMPT_TIMEOUT = 30
MAX_SEND_ATTEMPTS = 2


class TelegramOutbox:
    def __init__(self, connect):
        self.connect = connect
        with closing(connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_deliveries (
                    signal_id TEXT PRIMARY KEY, route TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    next_attempt_at REAL NOT NULL, attempted_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER, reason TEXT, notice_state TEXT NOT NULL DEFAULT 'waiting',
                    notice_attempted_at REAL, notice_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS telegram_deliveries_queue
                    ON telegram_deliveries(route, state, next_attempt_at);
                CREATE TABLE IF NOT EXISTS live_setups (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, revision TEXT NOT NULL,
                    source TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
                    issued_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, reason TEXT,
                    UNIQUE(event_id, venue, symbol)
                );
                CREATE INDEX IF NOT EXISTS live_setups_active ON live_setups(status, expires_at);
                CREATE VIEW IF NOT EXISTS telegram_signal_records AS
                    SELECT id, status, expires_at, payload, issued_at, reason FROM live_signals
                    UNION ALL
                    SELECT id, status, expires_at, payload, issued_at, reason FROM live_setups;
            """)

    @staticmethod
    def enqueue(db, signal_id, route, now):
        db.execute("INSERT INTO telegram_deliveries (signal_id,route,next_attempt_at) VALUES (?,?,?)",
                   (signal_id, route, now))

    def claim_send(self, route, now):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE telegram_deliveries SET state='unconfirmed', reason='delivery_confirmation_missing'
                WHERE route=? AND state='sending' AND attempted_at<=?
            """, (route, now - ATTEMPT_TIMEOUT))
            db.execute("""
                UPDATE telegram_deliveries SET state='skipped', reason='signal_expired_or_cancelled'
                WHERE route=? AND state='pending' AND EXISTS (
                    SELECT 1 FROM telegram_signal_records s WHERE s.id=telegram_deliveries.signal_id
                    AND (s.status!='active' OR s.expires_at<=?)
                )
            """, (route, now))
            row = db.execute("""
                SELECT d.signal_id, s.payload FROM telegram_deliveries d
                JOIN telegram_signal_records s ON s.id=d.signal_id
                WHERE d.route=? AND d.state='pending' AND d.next_attempt_at<=?
                ORDER BY s.issued_at LIMIT 1
            """, (route, now)).fetchone()
            if row is None:
                return None
            db.execute("""
                UPDATE telegram_deliveries SET state='sending', attempts=attempts+1, attempted_at=?
                WHERE signal_id=?
            """, (now, row["signal_id"]))
            return json.loads(row["payload"])

    def finish_send(self, signal_id, result, now):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT d.attempts, s.status, s.expires_at FROM telegram_deliveries d
                JOIN telegram_signal_records s ON s.id=d.signal_id WHERE d.signal_id=? AND d.state='sending'
            """, (signal_id,)).fetchone()
            if row is None:
                return "unconfirmed"
            state = result.status
            retry = result.retry_after
            next_attempt = now
            if state == "rejected" and result.reason == "signal_expired":
                state = "skipped"
            elif state == "rate_limited":
                if (retry is not None and math.isfinite(retry) and retry > 0
                        and row["attempts"] < MAX_SEND_ATTEMPTS and row["status"] == "active"
                        and now + retry < row["expires_at"]):
                    state, next_attempt = "pending", now + retry
                else:
                    state = "rejected"
            db.execute("""
                UPDATE telegram_deliveries SET state=?, next_attempt_at=?, message_id=?, reason=? WHERE signal_id=?
            """, (state, next_attempt, result.message_id, result.reason, signal_id))
            return state

    def claim_update(self, route, now):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE telegram_deliveries SET notice_state='unconfirmed', notice_reason='edit_confirmation_missing'
                WHERE route=? AND notice_state='editing' AND notice_attempted_at<=?
            """, (route, now - ATTEMPT_TIMEOUT))
            row = db.execute("""
                SELECT d.signal_id, d.message_id, s.payload,
                    CASE WHEN s.status='cancelled' THEN s.reason ELSE 'alert_expired' END AS reason
                FROM telegram_deliveries d JOIN telegram_signal_records s ON s.id=d.signal_id
                WHERE d.route=? AND d.state='sent' AND d.notice_state='waiting'
                    AND (s.status!='active' OR s.expires_at<=?)
                ORDER BY s.expires_at LIMIT 1
            """, (route, now)).fetchone()
            if row is None:
                return None
            db.execute("""
                UPDATE telegram_deliveries SET notice_state='editing', notice_attempted_at=? WHERE signal_id=?
            """, (now, row["signal_id"]))
            return json.loads(row["payload"]), row["message_id"], row["reason"]

    def finish_update(self, signal_id, result):
        with closing(self.connect()) as db, db:
            db.execute("""
                UPDATE telegram_deliveries SET notice_state=?, notice_reason=?
                WHERE signal_id=? AND notice_state='editing'
            """, (result.status, result.reason, signal_id))

    def counts(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT state, COUNT(*) AS total FROM telegram_deliveries GROUP BY state")
            return {row["state"]: row["total"] for row in rows}


class TelegramPublisher:
    def __init__(self, bot, chat_id, route, outbox, *, slots=None):
        self.bot = bot
        self.chat_id = chat_id
        self.route = route
        self.outbox = outbox
        self.slots = slots
        self.edit_lock = asyncio.Lock()

    async def flush(self):
        signal = self.outbox.claim_send(self.route, time.time())
        if signal is not None:
            result = await send_signal(self.bot, self.chat_id, signal)
            status = self.outbox.finish_send(signal["id"], result, time.time())
            emit({"type": "telegram_delivery", "signal_id": signal["id"], "status": status, "reason": result.reason})
        update = self.outbox.claim_update(self.route, time.time())
        if update is not None:
            signal, message_id, reason = update
            async with self.edit_lock:
                if self.slots is not None and signal.get("type") != "technical_setup" and signal.get("reply_required"):
                    state = self.slots.state(signal["id"], route=self.route)
                    result = await edit_signal(self.bot, self.chat_id, message_id, signal, reason, response_state=state)
                else:
                    result = await edit_signal(self.bot, self.chat_id, message_id, signal, reason)
            self.outbox.finish_update(signal["id"], result)
            emit({"type": "telegram_update", "signal_id": signal["id"], "status": result.status,
                  "reason": result.reason})

    async def run(self):
        while True:
            await self.flush()
            await asyncio.sleep(1)
