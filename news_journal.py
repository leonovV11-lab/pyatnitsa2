"""Durable first-party headline revisions and completed agent reviews."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from official_news import NewsItem

DISCOVERY_WINDOW = 36 * 3600
REACTION_WINDOW = 90 * 60


FAMILY_RULES = {
    "nvidia": ("nvidia_leadership", r"\b(?:CEO|Chief Executive Officer|interim chief executive)\b", False),
    "fed": ("us_monetary_policy", r"\b(?:FOMC statement|federal funds rate|monetary policy decision)\b", True),
    "ecb": ("eu_monetary_policy", r"\bmonetary policy decisions?\b", True),
    "bls": ("us_economic_release", r"\b(?:Consumer Price Index|Employment Situation|Producer Price Index)\b", True),
}


def classify_headline(source, title):
    pattern = FAMILY_RULES.get(source)
    if pattern and re.search(pattern[1], title, re.IGNORECASE):
        return pattern[0], pattern[2]
    return "uncatalogued", False


class HeadlineJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS official_headlines (
                    event_id TEXT NOT NULL, revision TEXT NOT NULL, source TEXT NOT NULL,
                    published_at REAL NOT NULL, known_at REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(event_id, revision)
                );
                CREATE INDEX IF NOT EXISTS official_headlines_known ON official_headlines (known_at);
                CREATE TABLE IF NOT EXISTS news_reviews (
                    review_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                    event_id TEXT NOT NULL, revision TEXT NOT NULL, result TEXT
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def observe(self, item: NewsItem):
        payload = asdict(item)
        known = max(item.published_at, item.fetched_at)
        payload["known_at"] = known
        with closing(self.connect()) as db, db:
            result = db.execute(
                "INSERT OR IGNORE INTO official_headlines VALUES (?, ?, ?, ?, ?, ?)",
                (item.id, item.revision, item.source, item.published_at, known, json.dumps(payload, ensure_ascii=False)),
            )
            return payload if result.rowcount else None

    def current_news(self, as_of):
        with closing(self.connect()) as db:
            rows = db.execute("""
                WITH ranked AS (
                    SELECT payload, published_at, event_id,
                        ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY known_at DESC, rowid DESC) AS latest
                    FROM official_headlines
                    WHERE known_at <= ? AND published_at BETWEEN ? AND ?
                )
                SELECT payload FROM ranked WHERE latest = 1 ORDER BY published_at DESC, event_id
            """, (as_of, as_of - DISCOVERY_WINDOW, as_of))
            return [json.loads(row["payload"]) for row in rows]

    def claim_review(self, review_id, event, now):
        with closing(self.connect()) as db, db:
            row = db.execute("INSERT OR IGNORE INTO news_reviews VALUES (?, ?, NULL, ?, ?, NULL)",
                             (review_id, now, event["id"], event["revision"]))
            return row.rowcount == 1

    def finish_review(self, review_id, result):
        with closing(self.connect()) as db, db:
            self.write_review(db, review_id, result)

    @staticmethod
    def write_review(db, review_id, result):
        db.execute("UPDATE news_reviews SET finished_at=?, result=? WHERE review_id=?",
                   (time.time(), json.dumps(result, ensure_ascii=False, allow_nan=False), review_id))


def key(*values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:32]


def emit(event):
    print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)
