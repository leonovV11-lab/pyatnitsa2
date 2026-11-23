"""Persist the inputs and decisions needed to compare signal policies."""

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from statistics import median

REVIEW_DECISIONS = frozenset({"approve", "watch", "reject", "unavailable"})
REVIEW_CLAIM_TIMEOUT = 60.0


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _timestamp(value):
    if value is None:
        return time.time()
    instant = float(value)
    if not math.isfinite(instant) or instant < 0:
        raise ValueError("Invalid observation timestamp")
    return instant


def _label(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Research identifiers must be nonempty strings")
    return value


def candidate_key(exchange, symbol, strategy, candle):
    identity = [exchange, symbol, strategy, str(candle)]
    return hashlib.sha256(_json(identity).encode()).hexdigest()[:32]


def review_key(model, instructions):
    digest = hashlib.sha256(instructions.encode()).hexdigest()[:16]
    return f"{_label(model)}:{digest}"


class Research:
    """An immutable first snapshot plus subsequent observations of each candidate."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    candle TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_candidates_time
                    ON research_candidates(observed_at);

                CREATE TABLE IF NOT EXISTS research_reviews (
                    candidate_id TEXT NOT NULL REFERENCES research_candidates(candidate_id),
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (
                        decision IN ('pending', 'approve', 'watch', 'reject', 'unavailable')
                    ),
                    reason TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    completed_at REAL,
                    latency_ms REAL,
                    PRIMARY KEY (candidate_id, reviewer)
                );
                CREATE INDEX IF NOT EXISTS research_reviews_time
                    ON research_reviews(requested_at);

                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES research_candidates(candidate_id),
                    stage TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    event_key TEXT,
                    UNIQUE (candidate_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS research_events_time
                    ON research_events(observed_at);
                CREATE INDEX IF NOT EXISTS research_events_candidate_stage
                    ON research_events(candidate_id, stage, decision, observed_at);
                """
            )

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db

    def record_candidate(
        self, candidate_id, *, exchange, symbol, strategy, candle, observed_at=None,
        payload, decision, reason="",
    ):
        """Save the first snapshot. Later market observations belong in events."""
        with self._connect() as db:
            inserted = db.execute(
                """
                INSERT INTO research_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO NOTHING
                """,
                (
                    _label(candidate_id), _label(exchange), _label(symbol), _label(strategy),
                    str(candle), _timestamp(observed_at), _json(payload), _label(decision), reason,
                ),
            )
            return inserted.rowcount == 1

    def record_event(
        self, candidate_id, *, stage, decision, reason="", payload=None,
        observed_at=None, event_key=None,
    ):
        """Attach an observation. A supplied event key makes a retry idempotent."""
        with self._connect() as db:
            inserted = db.execute(
                """
                INSERT INTO research_events (
                    candidate_id, stage, decision, reason, observed_at, payload, event_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id, event_key) DO NOTHING
                """,
                (
                    _label(candidate_id), _label(stage), _label(decision), reason,
                    _timestamp(observed_at), _json(payload), event_key,
                ),
            )
            return inserted.rowcount == 1

    def claim_review(self, candidate_ids, reviewer, *, now=None):
        """Reserve one provider attempt per candidate and version before doing network I/O."""
        reviewer = _label(reviewer)
        now = _timestamp(now)
        claimed = []
        with self._connect() as db:
            db.execute(
                """
                UPDATE research_reviews
                SET decision = 'unavailable', reason = 'claim_timed_out', completed_at = ?
                WHERE decision = 'pending' AND requested_at <= ?
                """,
                (now, now - REVIEW_CLAIM_TIMEOUT),
            )
            for candidate_id in candidate_ids:
                inserted = db.execute(
                    """
                    INSERT INTO research_reviews (
                        candidate_id, reviewer, decision, reason, requested_at
                    ) VALUES (?, ?, 'pending', '', ?)
                    ON CONFLICT(candidate_id, reviewer) DO NOTHING
                    """,
                    (_label(candidate_id), reviewer, now),
                )
                if inserted.rowcount == 1:
                    claimed.append(candidate_id)
        return claimed

    def finish_review(self, candidate_id, reviewer, decision, reason="", *, now=None, latency_ms=None):
        """Finalize a reservation exactly once. Provider failure is an explicit outcome."""
        if decision not in REVIEW_DECISIONS:
            raise ValueError("Invalid research review decision")
        if latency_ms is not None:
            latency_ms = float(latency_ms)
            if not math.isfinite(latency_ms) or latency_ms < 0:
                raise ValueError("Invalid review latency")
        now = _timestamp(now)
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE research_reviews
                SET decision = CASE WHEN requested_at <= ? THEN 'unavailable' ELSE ? END,
                    reason = CASE WHEN requested_at <= ? THEN 'claim_timed_out' ELSE ? END,
                    completed_at = ?, latency_ms = ?
                WHERE candidate_id = ? AND reviewer = ? AND decision = 'pending'
                """,
                (
                    now - REVIEW_CLAIM_TIMEOUT, decision, now - REVIEW_CLAIM_TIMEOUT, reason,
                    now, latency_ms, _label(candidate_id), _label(reviewer),
                ),
            )
            return updated.rowcount == 1

    def get_candidate(self, candidate_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM research_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "payload": json.loads(row["payload"])}

    def get_review(self, candidate_id, reviewer, *, now=None):
        now = _timestamp(now)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM research_reviews
                WHERE candidate_id = ? AND reviewer = ?
                """,
                (candidate_id, reviewer),
            ).fetchone()
        if row is None or row["requested_at"] > now:
            return None
        result = dict(row)
        if result["completed_at"] is not None and result["completed_at"] > now:
            result.update(decision="pending", reason="", completed_at=None, latency_ms=None)
        if result["decision"] == "pending" and result["requested_at"] <= now - REVIEW_CLAIM_TIMEOUT:
            result.update(decision="unavailable", reason="claim_timed_out")
        return result

    def attempted_deliveries(self, candidate_ids):
        """Look up persistent notification reservations for a bounded scan batch."""
        candidate_ids = tuple(dict.fromkeys(candidate_ids))
        if not candidate_ids:
            return set()
        placeholders = ", ".join("?" for _ in candidate_ids)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT candidate_id FROM research_events
                WHERE candidate_id IN ({placeholders}) AND stage = 'delivery'
                    AND decision = 'attempted' AND event_key = 'push:' || candidate_id
                """,
                candidate_ids,
            ).fetchall()
        return {row["candidate_id"] for row in rows}

    def pace_report(self, since, *, now=None):
        """Count distinct active signals and measure latency on a causal delivery cohort."""
        since, now = _timestamp(since), _timestamp(now)
        with self._connect() as db:
            db.execute("BEGIN")
            candidate_stats = db.execute(
                """
                WITH active AS (
                    SELECT candidate_id FROM research_candidates WHERE observed_at BETWEEN ? AND ?
                    UNION
                    SELECT candidate_id FROM research_events WHERE observed_at BETWEEN ? AND ?
                )
                SELECT COUNT(*) AS candidates,
                    COUNT(CASE WHEN candidate.decision = 'eligible' THEN 1 END) AS eligible
                FROM active JOIN research_candidates AS candidate USING (candidate_id)
                """,
                (since, now, since, now),
            ).fetchone()
            event_stats = db.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN stage = 'quote' AND decision = 'tradable'
                                        THEN candidate_id END) AS tradable,
                    COUNT(DISTINCT CASE WHEN stage = 'delivery' AND decision = 'sent'
                                        THEN candidate_id END) AS delivered,
                    COUNT(DISTINCT CASE WHEN stage = 'confirmation' AND decision = 'opened'
                                        THEN candidate_id END) AS confirmed,
                    COUNT(CASE WHEN stage IN ('quote', 'confirmation')
                                AND decision IN ('watch', 'rejected', 'reject')
                                AND reason IN ('Нет свежей котировки', 'stale_quote', 'quote_unavailable')
                               THEN 1 END) AS stale_rejections
                FROM research_events WHERE observed_at BETWEEN ? AND ?
                """,
                (since, now),
            ).fetchone()
            delays = db.execute(
                """
                WITH ordered_quotes AS (
                    SELECT quote.candidate_id, quote.id, quote.observed_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY quote.candidate_id ORDER BY quote.observed_at, quote.id
                        ) AS occurrence
                    FROM research_events AS quote
                    JOIN research_candidates AS candidate USING (candidate_id)
                    WHERE candidate.observed_at BETWEEN ? AND ?
                      AND quote.observed_at BETWEEN candidate.observed_at AND ?
                      AND quote.stage = 'quote' AND quote.decision = 'tradable'
                )
                SELECT MIN(sent.observed_at) - quote.observed_at AS delay
                FROM ordered_quotes AS quote
                JOIN research_events AS sent USING (candidate_id)
                WHERE quote.occurrence = 1 AND sent.stage = 'delivery' AND sent.decision = 'sent'
                  AND sent.observed_at <= ?
                  AND (sent.observed_at > quote.observed_at OR
                      (sent.observed_at = quote.observed_at AND sent.id > quote.id))
                GROUP BY quote.candidate_id
                """,
                (since, now, now, now),
            ).fetchall()
        return {
            **dict(candidate_stats),
            **dict(event_stats),
            "delivery_delay_samples": len(delays),
            "median_delivery_delay_seconds": median(row["delay"] for row in delays) if delays else None,
        }

    def report(self, since=None, *, now=None):
        """Group observations by status; portfolio P&L comes from the paper ledgers."""
        since = 0.0 if since is None else _timestamp(since)
        now = _timestamp(now)
        with self._connect() as db:
            db.execute("BEGIN")
            candidates = db.execute(
                """
                SELECT decision, reason, COUNT(*) AS n
                FROM research_candidates WHERE observed_at >= ? AND observed_at <= ?
                GROUP BY decision, reason
                """,
                (since, now),
            ).fetchall()
            reviews = db.execute(
                """
                SELECT reviewer, status AS decision, COUNT(*) AS n, COUNT(latency_ms) AS measured,
                       SUM(latency_ms) AS total_latency, MAX(latency_ms) AS max_latency
                FROM (
                    SELECT reviewer, CASE WHEN completed_at <= ? THEN latency_ms END AS latency_ms,
                        CASE
                        WHEN (decision = 'pending' OR completed_at > ?) AND requested_at <= ?
                            THEN 'unavailable'
                        WHEN completed_at > ? THEN 'pending'
                        ELSE decision
                    END AS status
                    FROM research_reviews WHERE requested_at >= ? AND requested_at <= ?
                )
                GROUP BY reviewer, status
                """,
                (now, now, now - REVIEW_CLAIM_TIMEOUT, now, since, now),
            ).fetchall()
            events = db.execute(
                """
                SELECT stage, decision, COUNT(*) AS n FROM research_events
                WHERE observed_at >= ? AND observed_at <= ?
                GROUP BY stage, decision
                """,
                (since, now),
            ).fetchall()

        result = {
            "candidates": 0, "candidates_by_decision": {}, "reasons": {},
            "reviews_by_decision": {}, "reviewers": {}, "events_by_stage": {},
        }
        for row in candidates:
            result["candidates"] += row["n"]
            counts = result["candidates_by_decision"]
            counts[row["decision"]] = counts.get(row["decision"], 0) + row["n"]
            if row["reason"]:
                reasons = result["reasons"].setdefault(row["decision"], {})
                reasons[row["reason"]] = row["n"]
        for row in reviews:
            counts = result["reviews_by_decision"]
            counts[row["decision"]] = counts.get(row["decision"], 0) + row["n"]
            metrics = result["reviewers"].setdefault(row["reviewer"], {})
            metrics[row["decision"]] = {
                "count": row["n"],
                "mean_latency_ms": (row["total_latency"] / row["measured"]
                                    if row["measured"] else None),
                "max_latency_ms": row["max_latency"],
            }
        for row in events:
            result["events_by_stage"].setdefault(row["stage"], {})[row["decision"]] = row["n"]
        return result
