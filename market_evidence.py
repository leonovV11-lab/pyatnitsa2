"""Measure subsequent public prices for candidates without creating any trade records."""

import asyncio
import math
import time
from contextlib import closing

from market import DEFAULT_QUOTE_MAX_AGE, MarketError, Quote
from news_journal import emit

HORIZONS_SECONDS = (300, 900, 3600)
SAMPLING_INTERVAL = 15
OBSERVATION_WINDOW = 75
MAX_DUE_SYMBOLS = 32
MAX_SINGLE_QUOTES = 4


def _current(quote: Quote, as_of: float) -> bool:
    return (
        0 <= as_of - quote.observed_at <= DEFAULT_QUOTE_MAX_AGE
        and all(math.isfinite(price) and price > 0 for price in (quote.bid, quote.ask, quote.bid_size, quote.ask_size))
        and quote.bid <= quote.ask
    )


class QuoteEvidenceJournal:
    def __init__(self, connect):
        self.connect = connect
        with closing(connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS market_quote_samples (
                    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, revision TEXT NOT NULL,
                    family TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
                    observed_at REAL NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL,
                    bid_size REAL NOT NULL, ask_size REAL NOT NULL,
                    UNIQUE(event_id, venue, symbol)
                );
                CREATE TABLE IF NOT EXISTS market_quote_checks (
                    sample_id INTEGER NOT NULL REFERENCES market_quote_samples(id),
                    horizon_seconds INTEGER NOT NULL, due_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', observed_at REAL, observed_bid REAL,
                    observed_bid_size REAL, price_move_pct REAL,
                    PRIMARY KEY(sample_id, horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS market_quote_checks_due ON market_quote_checks(status, due_at);
            """)

    def has_capture(self, event_id, venue, symbol) -> bool:
        with closing(self.connect()) as db:
            return db.execute("""
                SELECT 1 FROM market_quote_samples WHERE event_id=? AND venue=? AND symbol=?
            """, (event_id, venue, symbol)).fetchone() is not None

    def record(self, event, family, quote: Quote, *, as_of: float) -> bool:
        if not _current(quote, as_of) or quote.observed_at < event["known_at"]:
            return False
        with closing(self.connect()) as db, db:
            inserted = db.execute("""
                INSERT OR IGNORE INTO market_quote_samples
                    (event_id, revision, family, venue, symbol, observed_at, bid, ask, bid_size, ask_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (event["id"], event["revision"], family, quote.exchange, quote.symbol, quote.observed_at,
                  quote.bid, quote.ask, quote.bid_size, quote.ask_size))
            if inserted.rowcount == 0:
                return False
            db.executemany("""
                INSERT INTO market_quote_checks (sample_id, horizon_seconds, due_at) VALUES (?, ?, ?)
            """, [(inserted.lastrowid, horizon, quote.observed_at + horizon) for horizon in HORIZONS_SECONDS])
            return True

    def due_symbols(self, venue, *, as_of: float, limit: int = MAX_DUE_SYMBOLS) -> tuple[str, ...]:
        with closing(self.connect()) as db, db:
            db.execute("""
                UPDATE market_quote_checks SET status='missed'
                WHERE status='pending' AND due_at<? AND sample_id IN (
                    SELECT id FROM market_quote_samples WHERE venue=?
                )
            """, (as_of - OBSERVATION_WINDOW, venue))
            rows = db.execute("""
                SELECT s.symbol FROM market_quote_checks c JOIN market_quote_samples s ON s.id=c.sample_id
                WHERE s.venue=? AND c.status='pending' AND c.due_at<=?
                GROUP BY s.symbol ORDER BY MIN(c.due_at), s.symbol LIMIT ?
            """, (venue, as_of, limit))
            return tuple(row["symbol"] for row in rows)

    def observe(self, quote: Quote, *, as_of: float) -> int:
        if not _current(quote, as_of):
            return 0
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""
                SELECT c.sample_id, c.horizon_seconds, s.ask
                FROM market_quote_checks c JOIN market_quote_samples s ON s.id=c.sample_id
                WHERE s.venue=? AND s.symbol=? AND c.status='pending'
                    AND c.due_at<=? AND c.due_at>=?
            """, (quote.exchange, quote.symbol, quote.observed_at, as_of - OBSERVATION_WINDOW)).fetchall()
            updates = []
            for row in rows:
                move = (quote.bid / row["ask"] - 1) * 100
                if math.isfinite(move):
                    updates.append((quote.observed_at, quote.bid, quote.bid_size, move,
                                    row["sample_id"], row["horizon_seconds"]))
            db.executemany("""
                UPDATE market_quote_checks
                SET status='observed', observed_at=?, observed_bid=?, observed_bid_size=?, price_move_pct=?
                WHERE sample_id=? AND horizon_seconds=?
            """, updates)
            return len(updates)

    def report(self) -> dict:
        horizons = {str(horizon): {"pending": 0, "observed": 0, "missed": 0, "average_move_pct": None,
                                  "positive_moves": 0, "negative_moves": 0}
                    for horizon in HORIZONS_SECONDS}
        with closing(self.connect()) as db:
            samples, events = db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT event_id) FROM market_quote_samples"
            ).fetchone()
            rows = db.execute("""
                SELECT horizon_seconds,
                       SUM(status='pending') AS pending, SUM(status='observed') AS observed,
                       SUM(status='missed') AS missed, AVG(price_move_pct) AS average_move_pct,
                       SUM(CASE WHEN price_move_pct>0 THEN 1 ELSE 0 END) AS positive_moves,
                       SUM(CASE WHEN price_move_pct<0 THEN 1 ELSE 0 END) AS negative_moves
                FROM market_quote_checks GROUP BY horizon_seconds
            """)
            for row in rows:
                result = dict(row)
                horizons[str(result.pop("horizon_seconds"))] = result
        return {"provenance": "public_market_quotes", "samples": samples,
                "distinct_events": events, "horizons": horizons}


class MarketSampler:
    def __init__(self, market, journal: QuoteEvidenceJournal):
        self.market = market
        self.journal = journal

    async def _single(self, symbol) -> Quote | None:
        try:
            return await self.market.quote(symbol)
        except MarketError:
            return None

    async def _quotes(self, symbols) -> list[Quote]:
        if not symbols:
            return []
        try:
            books = await self.market.quotes(symbols)
        except MarketError:
            books = {}
        now = time.time()
        available = {symbol: book for symbol, book in books.items() if _current(book, now)}
        missing = [symbol for symbol in symbols if symbol not in available][:MAX_SINGLE_QUOTES]
        fallback = await asyncio.gather(*(self._single(symbol) for symbol in missing))
        return [book for book in (*available.values(), *fallback) if book is not None]

    async def collect(self, event, family, symbols) -> int:
        pending = [symbol for symbol in dict.fromkeys(symbols)
                   if not self.journal.has_capture(event["id"], self.market.exchange, symbol)]
        books = await self._quotes(pending)
        now = time.time()
        recorded = sum(self.journal.record(event, family, book, as_of=now) for book in books)
        if recorded:
            emit({"type": "market_quote_evidence", "samples_added": recorded})
        return recorded

    async def sample_due(self) -> int:
        symbols = self.journal.due_symbols(self.market.exchange, as_of=time.time())
        books = await self._quotes(symbols)
        now = time.time()
        observed = sum(self.journal.observe(book, as_of=now) for book in books)
        if observed:
            emit({"type": "market_quote_evidence", "horizons_observed": observed})
        return observed

    async def run(self):
        while True:
            await self.sample_due()
            await asyncio.sleep(SAMPLING_INTERVAL)
