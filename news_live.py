"""Turn first-party news and live spot prices into expiring, read-only alerts."""

import argparse
import asyncio
import json
import logging
import math
import re
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path

import aiohttp
from telegram import Bot
from telegram.error import InvalidToken, TelegramError

import config
from live_delivery import TelegramOutbox, TelegramPublisher
from live_evidence import assess_recorded_history, read_recorded_history
from live_market_check import (
    FIFTEEN_MINUTES,
    FIVE_MINUTES,
    observe_reaction,
    prepare_candidate,
    refresh_plan,
)
from live_portfolio import MAX_PORTFOLIO_AGE_SECONDS, read_portfolio
from live_readiness import readiness_report
from live_reply_worker import check_reply_transport, run_replies
from live_slots import SlotJournal
from live_telegram import MOSCOW_TIME, parse_chat_id, verify_chat
from market import DEFAULT_QUOTE_MAX_AGE, SUPPORTED_EXCHANGES, Market, MarketError
from market_evidence import MarketSampler, QuoteEvidenceJournal
from news_agent_team import ROLE_GROUPS, analyze_team
from news_journal import (
    DISCOVERY_WINDOW,
    REACTION_WINDOW,
    HeadlineJournal,
    classify_headline,
    emit,
    key,
)
from official_news import NewsSource, fetch_official_news
from strategies import BASELINE, MAX_POSITION_FRACTION, MIN_NET_PAYOFF

SIGNAL_TTL = 5
RISK_RESERVATION_SECONDS = 300
MAX_SYMBOLS = 16
PAIRS_PER_CATALOG_SCAN = 64
CATALOG_REFERENCE_PAIRS = ("BTC/USDT", "ETH/USDT")
CATALOG_PRIORITY_LIMIT = 8
CATALOG_VOLUME_TTL = 300
CATALOG_VOLUME_RETRY = 30
CHART_MINUTES_SECONDS = FIVE_MINUTES
SCAN_CANDLE_INTERVALS = {"5m": FIVE_MINUTES, "15m": FIFTEEN_MINUTES}
MAX_REVIEWS_PER_SCAN = 4
EXTRA_COST_PER_SIDE = 0.001
DISCOVERY_SOURCES = (
    NewsSource("bbc_world", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml",
               "www.bbc.co.uk", "/news/"),
    NewsSource("le_monde", "Le Monde", "https://www.lemonde.fr/en/international/rss_full.xml",
               "www.lemonde.fr", "/en/"),
)
SOURCE_RULES = {
    "boe": (("uk_monetary_policy", r"\bbank rate (?:maintained|reduced|increased|held|cut|raised)\b", True),),
    "boj": (("japan_monetary_policy", r"^statement on monetary policy\b", True),),
    "bea": (
        ("us_gdp_release", r"^(?:gross domestic product|gdp)\b", False),
        ("us_income_and_outlays", r"^personal income and outlays\b", True),
    ),
    "sec": (("us_crypto_regulation", r"\b(?:bitcoin|ether(?:eum)?|crypto(?:currency)?|digital assets?)\b", False),),
}
WORLD_RULES = (
    ("international_trade",
     r"\b(?:tariffs?|trade agreement|export controls?|trade restrictions?|import quotas?|anti-dumping)\b"),
    ("sanctions", r"\b(?:sanctions?|embargo|asset freeze)\b"),
    ("geopolitics", r"\b(?:ceasefire|peace agreement|military escalation|border closure|shipping disruption)\b"),
    ("energy_supply", r"\b(?:opec\+?|oil production|crude oil|natural gas|petroleum|strategic reserves?)\b"),
    ("macro_and_rates",
     r"\b(?:interest rates?|monetary policy|inflation|unemployment|payrolls?|gdp|gross domestic product)\b"),
    ("financial_stability", r"\b(?:financial stability|bank failure|bank resolution|bailout|liquidity support)\b"),
    ("corporate_events", r"\b(?:chief executive|ceo|merger|acquisition|earnings|bankruptcy|initial public offering)\b"),
    ("market_infrastructure", r"\b(?:cyberattack|cyber incident|exchange outage|clearing outage|trading halt)\b"),
    ("natural_disasters", r"\b(?:earthquake|hurricane|tsunami|flooding|wildfire)\b"),
)
_AGENT_NAMES = {name for _, roles in ROLE_GROUPS for name, _ in roles}
_PUBLIC_PLAN_FIELDS = ("strategy", "price", "stop", "tp1", "tp2", "ratio", "stressed_ratio",
                       "entry_min", "entry_max", "quote_observed_at")


def classify_live_event(source, title):
    family, eligible = classify_headline(source, title)
    if family != "uncatalogued":
        return family, eligible
    for family, pattern, eligible in SOURCE_RULES.get(source, ()):
        if re.search(pattern, title, re.IGNORECASE):
            return family, eligible
    for family, pattern in WORLD_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return family, False
    return "uncatalogued", False


async def fetch_discovery_news(session):
    return await fetch_official_news(session, sources=DISCOVERY_SOURCES)


class LiveJournal(HeadlineJournal):
    def __init__(self, path):
        super().__init__(path)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_signals (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, revision TEXT NOT NULL,
                    source TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
                    risk REAL NOT NULL, issued_at REAL NOT NULL, expires_at REAL NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT,
                    UNIQUE(event_id, venue, symbol)
                );
                CREATE INDEX IF NOT EXISTS live_signals_active ON live_signals(status, expires_at);
                CREATE INDEX IF NOT EXISTS live_signals_issued ON live_signals(issued_at);
                CREATE TABLE IF NOT EXISTS market_reactions (
                    event_id TEXT NOT NULL, revision TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
                    closed_at REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(event_id, revision, venue, symbol, closed_at)
                );
                CREATE TABLE IF NOT EXISTS discovery_headlines (
                    event_id TEXT PRIMARY KEY, published_at REAL NOT NULL, payload TEXT NOT NULL
                );
            """)
        self.telegram = TelegramOutbox(self.connect)
        self.slots = SlotJournal(self.connect)
        self.market_evidence = QuoteEvidenceJournal(self.connect)

    def record_discoveries(self, items, as_of):
        start = as_of - DISCOVERY_WINDOW
        rows = []
        for item in items:
            if not start <= item.published_at <= as_of:
                continue
            family, _ = classify_live_event(item.source, item.title)
            payload = {"event_id": item.id, "publisher": item.publisher, "headline": item.title,
                       "url": item.url, "published_at": item.published_at, "family": family, "confirmed": False}
            rows.append((item.id, item.published_at, json.dumps(payload, ensure_ascii=False)))
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM discovery_headlines WHERE published_at<?", (start,))
            db.executemany("""
                INSERT INTO discovery_headlines VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET published_at=excluded.published_at, payload=excluded.payload
            """, rows)
        return len(rows)

    def record_reaction(self, item, venue, symbol, family, observation):
        metrics = {field: observation[field] for field in (
            "direction", "reaction_pct", "btc_reaction_pct", "baseline_closed_at", "first_post_event_open",
            "latest_closed_at",
        )}
        metrics["vs_btc_percentage_points"] = metrics["reaction_pct"] - metrics["btc_reaction_pct"]
        payload = {"type": "market_reaction", "event_id": item["id"], "revision": item["revision"],
                   "source": item["source"], "headline": item["title"], "url": item["url"],
                   "known_at": item["known_at"], "venue": venue, "symbol": symbol, "family": family, **metrics}
        with closing(self.connect()) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO market_reactions VALUES (?,?,?,?,?,?)", (
                item["id"], item["revision"], venue, symbol, metrics["latest_closed_at"],
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
            ))
            return payload if inserted.rowcount else None

    @staticmethod
    def current_revision(db, event_id, as_of):
        row = db.execute("""
            SELECT revision FROM official_headlines
            WHERE event_id=? AND known_at<=? ORDER BY known_at DESC, rowid DESC LIMIT 1
        """, (event_id, as_of)).fetchone()
        return row["revision"] if row is not None else None

    @staticmethod
    def capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio, *,
                        daily_risk_cap=None, telegram_route=None):
        risk = plan.get("stressed_loss_usdt", plan["loss"])
        if daily_risk_cap is not None:
            if telegram_route is None or not math.isfinite(daily_risk_cap) or daily_risk_cap <= 0:
                return "daily_planned_risk_not_configured"
            midnight = datetime.fromtimestamp(as_of, MOSCOW_TIME).replace(
                hour=0, minute=0, second=0, microsecond=0,
            ).timestamp()
            used = SlotJournal.risk_during_day(db, telegram_route, midnight)
            if not math.isfinite(risk) or math.fsum((used, risk)) > daily_risk_cap:
                return "daily_planned_risk_limit"
        cash_native, cash_usdt = plan_cost(plan)
        quote_asset = symbol.split("/", 1)[1]
        since = as_of - RISK_RESERVATION_SECONDS
        open_risk = 0.0
        open_symbols = frozenset()
        available_native = math.inf
        available_total = math.inf if budget is None else budget
        if portfolio is not None:
            if not 0 <= as_of - portfolio.observed_at <= MAX_PORTFOLIO_AGE_SECONDS:
                return "portfolio_snapshot_expired"
            if symbol in portfolio.open_symbols:
                return "symbol_already_held"
            since = max(since, portfolio.observed_at)
            open_risk = portfolio.stressed_risk_usdt
            open_symbols = portfolio.open_symbols
            available_native = (portfolio.available_cash_usdt if quote_asset == "USDT"
                                else portfolio.available_balance(quote_asset))
        reserved_risk = reserved_total = reserved_native = 0.0
        reservations = db.execute("""
            SELECT s.symbol, s.risk, s.payload, sl.state AS reply_state, d.state AS delivery_state
            FROM live_signals s LEFT JOIN live_manual_slots sl ON sl.signal_id=s.id
            LEFT JOIN telegram_deliveries d ON d.signal_id=s.id
            WHERE s.issued_at<=? AND (s.issued_at>? OR sl.state='entered')
        """, (as_of, since))
        for reservation in reservations:
            state, held_symbol = reservation["reply_state"], reservation["symbol"]
            if (state in {"skipped", "closed"} or (state == "entered" and held_symbol in open_symbols)
                    or (state == "waiting" and reservation["delivery_state"] in {"rejected", "skipped"})):
                continue
            if held_symbol == symbol:
                return "symbol_already_reserved"
            reserved_risk += reservation["risk"]
            native, total = plan_cost(json.loads(reservation["payload"])["plan"])
            reserved_total += total
            if held_symbol.rsplit("/", 1)[1] == quote_asset:
                reserved_native += native
        if not math.isfinite(risk) or open_risk + reserved_risk + risk > cap:
            return "portfolio_stressed_risk_limit"
        if (not math.isfinite(cash_native) or not math.isfinite(cash_usdt)
                or reserved_total + cash_usdt > available_total
                or reserved_native + cash_native > available_native):
            return "insufficient_unreserved_cash"
        return ""

    def check_capacity(self, symbol, plan, *, cap, budget, as_of, portfolio,
                       daily_risk_cap=None, telegram_route=None):
        with closing(self.connect()) as db:
            return self.capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio,
                                        daily_risk_cap=daily_risk_cap, telegram_route=telegram_route)

    def has_review(self, review_id):
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM news_reviews WHERE review_id=?", (review_id,)).fetchone() is not None

    def issue(self, event, venue, symbol, plan, *, cap, as_of, details=None, review_result=None,
              telegram_route=None, budget=None, portfolio=None, reply_required=False, daily_risk_cap=None):
        risk = plan.get("stressed_loss_usdt", plan["loss"])
        quote_time = plan.get("quote_observed_at", as_of)
        if daily_risk_cap is not None and not reply_required:
            return None
        if not (math.isfinite(risk) and 0 < risk <= cap and math.isfinite(cap)
                and 0 <= as_of - quote_time <= DEFAULT_QUOTE_MAX_AGE):
            return None
        expiry = min(quote_time + SIGNAL_TTL, plan.get("valid_until", math.inf),
                     event["published_at"] + REACTION_WINDOW)
        if expiry <= as_of:
            return None
        signal_id = key("live", event["id"], venue, symbol)
        payload = {
            "type": "live_signal", "status": "active", "id": signal_id, "event_id": event["id"],
            "source": event["source"], "revision": event["revision"], "headline": event["title"],
            "url": event["url"], "venue": venue, "symbol": symbol, "side": "buy",
            "known_at": event["known_at"], "issued_at": as_of, "expires_at": expiry,
            "plan": plan, "evidence": details or {},
        }
        if reply_required:
            if telegram_route is None:
                return None
            payload["reply_required"] = True
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.current_revision(db, event["id"], as_of) != event["revision"]:
                return None
            if self.capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio,
                                    daily_risk_cap=daily_risk_cap, telegram_route=telegram_route):
                return None
            result = db.execute("""
                INSERT OR IGNORE INTO live_signals
                    (id,event_id,revision,source,venue,symbol,risk,issued_at,expires_at,status,payload)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?)
            """, (signal_id, event["id"], event["revision"], event["source"], venue, symbol,
                  risk, as_of, expiry, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            if not result.rowcount:
                return None
            if review_result is not None:
                self.write_review(db, payload["evidence"]["review_id"],
                                  {**review_result, "reason": "", "signal_id": signal_id})
            if telegram_route:
                self.telegram.enqueue(db, signal_id, telegram_route, as_of)
            if reply_required and (
                telegram_route is None or not self.slots.reserve(
                    db, signal_id, telegram_route, external_symbols=portfolio.open_symbols if portfolio else frozenset(),
                )
            ):
                db.rollback()
                return None
            return payload

    def invalidate(self, health, as_of, listed=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changes = []
            for row in db.execute("SELECT * FROM live_signals WHERE status='active'").fetchall():
                status, reason = "cancelled", ""
                if row["expires_at"] <= as_of:
                    status, reason = "expired", "alert_expired"
                elif health is not None and health.get(row["source"]) != "ok":
                    reason = "source_unavailable"
                elif self.current_revision(db, row["event_id"], as_of) != row["revision"]:
                    reason = "source_revision_changed"
                elif listed is not None and (row["event_id"], row["revision"]) not in listed:
                    reason = "source_no_longer_lists_release"
                if reason:
                    db.execute("UPDATE live_signals SET status=?, reason=? WHERE id=?", (status, reason, row["id"]))
                    changes.append({"type": "signal_update", "id": row["id"], "status": status, "reason": reason,
                                    "venue": row["venue"], "symbol": row["symbol"], "observed_at": as_of})
            return changes

    def report(self):
        now = time.time()
        self.invalidate(None, now)
        with closing(self.connect()) as db, db:
            counts = {row["status"]: row["total"] for row in db.execute(
                "SELECT status, COUNT(*) AS total FROM live_signals GROUP BY status"
            )}
            active = [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM live_signals WHERE status='active' AND expires_at>? ORDER BY issued_at",
                (now,),
            )]
            summary = db.execute("""
                SELECT (SELECT COUNT(DISTINCT event_id) FROM official_headlines) AS news,
                    (SELECT COUNT(*) FROM news_reviews WHERE finished_at IS NOT NULL) AS finished_reviews,
                    (SELECT COUNT(*) FROM market_reactions) AS reactions
            """).fetchone()
            latest_reactions = [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM market_reactions ORDER BY closed_at DESC, rowid DESC LIMIT 10"
            )]
            discoveries = [json.loads(row["payload"]) for row in db.execute("""
                SELECT payload FROM discovery_headlines
                WHERE published_at BETWEEN ? AND ? ORDER BY published_at DESC LIMIT 10
            """, (now - DISCOVERY_WINDOW, now))]
            latest = db.execute("""
                SELECT review_id, event_id, result FROM news_reviews
                WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1
            """).fetchone()
        last_review = None
        if latest:
            detail = json.loads(latest["result"])
            last_review = {
                "review_id": latest["review_id"], "event_id": latest["event_id"], "signal_id": detail["signal_id"],
                "reason": detail["reason"],
                "agent_decisions": Counter(row["decision"] for row in detail["agent_results"]),
            }
        return {"type": "report", **dict(summary), "signals": counts, "active": active,
                "latest_review": last_review, "recent_reactions": latest_reactions,
                "recent_headlines": self.current_news(now)[:10], "discovery_candidates": discoveries,
                "telegram_deliveries": self.telegram.counts(), "manual_slots": self.slots.report(),
                "market_quote_evidence": self.market_evidence.report()}


def review_verdict(results):
    decisions = {"support", "watch", "veto"}
    if (len(results) != len(_AGENT_NAMES) or {row["agent"] for row in results} != _AGENT_NAMES
            or any(row["decision"] not in decisions for row in results)):
        return "agent_team_incomplete"
    if any(row["decision"] == "veto" for row in results):
        return "agent_veto"
    if sum(row["decision"] == "support" for row in results) <= len(_AGENT_NAMES) // 2:
        return "agent_support_below_majority"
    return ""


def stressed_loss(plan):
    return plan["loss"] + EXTRA_COST_PER_SIDE * plan["qty"] * (plan["price"] + plan["stop"])


def plan_cost(plan):
    notional = plan.get("qty", 0) * plan.get("price", 0)
    fee = 1 + plan.get("fee_rate", 0)
    return notional * fee, plan.get("notional_usdt", notional) * fee


@dataclass(frozen=True)
class FundingRate:
    buy_rate: float
    sell_rate: float
    valid_until: float


async def funding_rate(market, asset):
    if asset == "USDT":
        return FundingRate(1.0, 1.0, math.inf)
    for symbol, inverse in ((f"{asset}/USDT", False), (f"USDT/{asset}", True)):
        try:
            quote = await market.quote(symbol)
        except (MarketError, ValueError):
            continue
        if quote.is_fresh() and 0 < quote.bid <= quote.ask:
            buy, sell = (1 / quote.bid, 1 / quote.ask) if inverse else (quote.ask, quote.bid)
            if math.isfinite(buy) and math.isfinite(sell):
                return FundingRate(buy, sell, quote.observed_at + DEFAULT_QUOTE_MAX_AGE)
    return None


def ranking_key(candidate):
    stats = candidate["stats"]
    return (-stats["lower_stressed_net_r"], -stats["recent_mean_stressed_net_r"],
            -stats["day_count"], candidate["symbol"], candidate["event"]["id"])


class ScanMarket:
    """Share candle requests and reuse current confirmed candles across scans."""

    def __init__(self, market):
        self.exchange = market.exchange
        self.quote = market.quote
        self.market = market
        self.requests = {}
        self.closed_frames = {}

    @staticmethod
    def candle_boundary(interval, now):
        seconds = SCAN_CANDLE_INTERVALS[interval]
        return int(now // seconds) * seconds

    def begin_scan(self):
        now = time.time()
        self.requests.clear()
        self.closed_frames = {
            request: cached for request, cached in self.closed_frames.items()
            if cached[0] == self.candle_boundary(request[1], now)
        }

    async def candles(self, symbol, interval, limit):
        identity = symbol, interval, limit
        if interval not in SCAN_CANDLE_INTERVALS:
            return await self.market.candles(symbol, interval, limit)
        boundary = self.candle_boundary(interval, time.time())
        cached = self.closed_frames.get(identity)
        if cached is not None and cached[0] == boundary:
            return cached[1]
        key = identity, boundary
        if key not in self.requests:
            self.requests[key] = asyncio.create_task(self.market.candles(symbol, interval, limit))
        frame = await self.requests[key]
        # The exchange can be one candle behind just after a close. Retry such frames next scan.
        if not frame.empty and frame["ts"].iloc[-1].timestamp() + SCAN_CANDLE_INTERVALS[interval] == boundary:
            self.closed_frames[identity] = boundary, frame
        return frame


class LiveMonitor:
    def __init__(self, public_session, private_session, journal, options, publisher=None, *,
                 all_pairs=False, manual_replies=False, collect_market_evidence=False, daily_risk_cap=None):
        self.public_session = public_session
        self.private_session = private_session
        self.journal = journal
        self.options = options
        self.publisher = publisher
        if manual_replies and publisher is None:
            raise ValueError("Режиму ручных ответов нужен отправитель Telegram.")
        self.market = Market(public_session, options.venue, all_spot=all_pairs)
        self.sampler = MarketSampler(self.market, journal.market_evidence) if collect_market_evidence else None
        self.all_pairs = all_pairs
        self.manual_replies = manual_replies
        self.daily_risk_cap = daily_risk_cap
        self.market_cursor = 0
        self.catalog_size = len(options.symbols)
        self.liquid_pairs = ()
        self.liquidity_refresh_at = 0.0
        self.scan_market = ScanMarket(self.market)
        self.candidate_cache = {}
        self.cached_history = []
        self.review_batches = asyncio.Queue(maxsize=1)
        self.source_refresh_lock = asyncio.Lock()
        self.last_decisions = {}
        self.listed_news = set()

    async def scan_symbols(self):
        if not self.all_pairs:
            return tuple(self.options.symbols)
        symbols = await self.market.spot_symbols()
        self.catalog_size = len(symbols)
        if symbols and time.monotonic() >= self.liquidity_refresh_at:
            try:
                self.liquid_pairs = tuple(await self.market.universe(limit=CATALOG_PRIORITY_LIMIT))
            except MarketError:
                self.liquid_pairs = ()
                self.liquidity_refresh_at = time.monotonic() + CATALOG_VOLUME_RETRY
            else:
                self.liquidity_refresh_at = time.monotonic() + CATALOG_VOLUME_TTL
        active = set(symbols)
        priorities = dict.fromkeys(symbol for symbol in (*CATALOG_REFERENCE_PAIRS, *self.liquid_pairs)
                                  if symbol in active)
        priority = tuple(priorities)[:CATALOG_PRIORITY_LIMIT]
        rotating = tuple(symbol for symbol in symbols if symbol not in priority)
        if not rotating:
            self.market_cursor = 0
            return priority
        current = self.market_cursor % len(rotating)
        batch = rotating[current:current + PAIRS_PER_CATALOG_SCAN - len(priority)]
        self.market_cursor = (current + len(batch)) % len(rotating)
        return priority + batch

    def watch(self, event, symbol, reason, **details):
        identity = event["id"], symbol
        state = event["revision"], reason, details
        if self.last_decisions.get(identity) == state:
            return
        self.last_decisions[identity] = state
        emit({"type": "watch", "event_id": event["id"], "source": event["source"],
              "headline": event["title"], "url": event["url"], "symbol": symbol, "reason": reason, **details})

    async def refresh_sources(self):
        async with self.source_refresh_lock:
            items, health = await fetch_official_news(self.public_session)
            for item in items:
                self.journal.observe(item)
            listed = {(item.id, item.revision) for item in items if health.get(item.source) == "ok"}
            self.listed_news = listed
            for change in self.journal.invalidate(health, time.time(), listed):
                emit(change)
            return items, health, listed

    def portfolio(self):
        path = self.options.portfolio_file
        if path is None:
            return None, ""
        try:
            return read_portfolio(path, as_of=time.time()), ""
        except ValueError:
            return None, "portfolio_snapshot_unavailable"

    def refresh_recorded_history(self):
        try:
            history = read_recorded_history(self.options.history_file)
        except (OSError, ValueError):
            history = []
            emit({"type": "history_unavailable", "reason": "cannot_read_real_fill_export"})
        if history != self.cached_history:
            self.candidate_cache.clear()
            self.cached_history = history
        return self.cached_history

    async def current_plan(self, draft, symbol):
        options = self.options
        portfolio, reason = self.portfolio()
        if reason:
            return None, None, reason
        quote_asset = symbol.split("/", 1)[1]
        if quote_asset != "USDT" and portfolio is None:
            return None, None, "quote_balance_required"
        rate = await funding_rate(self.market, quote_asset)
        if rate is None:
            return None, None, "quote_conversion_unavailable"
        sizing_budget = options.budget * min(1.0, 1.0 / (MAX_POSITION_FRACTION * options.allocation_slots))
        sizing_budget /= rate.buy_rate
        if portfolio is not None:
            available = portfolio.available_balance(quote_asset)
            if available <= 0:
                return None, None, "quote_balance_required"
            sizing_budget = min(sizing_budget, available / MAX_POSITION_FRACTION)
        plan, reason = await refresh_plan(self.market, draft["setup"], sizing_budget, options.fee_rate)
        if plan is None:
            return None, None, reason
        if rate.valid_until <= time.time():
            return None, None, "quote_conversion_expired"
        plan["quote_asset"] = quote_asset
        plan["stressed_loss_usdt"] = stressed_loss(plan) * rate.buy_rate
        plan["notional_usdt"] = plan["qty"] * plan["price"] * rate.buy_rate
        plan["loss"] *= rate.buy_rate
        plan["reward"] *= rate.sell_rate
        plan["ratio"] *= rate.sell_rate / rate.buy_rate
        if quote_asset != "USDT":
            plan["valid_until"] = min(plan.get("valid_until", math.inf), rate.valid_until)
            if plan["ratio"] < MIN_NET_PAYOFF:
                return None, None, "quote_conversion_reduces_net_payoff"
        average_target = plan["tp1"] / 2 + plan["tp2"] / 2
        extra_target_cost = EXTRA_COST_PER_SIDE * plan["qty"] * (plan["price"] + average_target) * rate.sell_rate
        stressed_reward = plan["reward"] - extra_target_cost
        stressed_risk = plan["stressed_loss_usdt"]
        stressed_ratio = stressed_reward / stressed_risk if 0 < stressed_risk < math.inf else 0.0
        if not MIN_NET_PAYOFF <= stressed_ratio < math.inf:
            return None, None, "payoff_below_cost_stress"
        plan["stressed_reward_usdt"] = stressed_reward
        plan["stressed_ratio"] = stressed_ratio
        # The risk and cash checks use this ask. A higher entry would invalidate the sizing.
        plan["entry_max"] = plan["price"]
        reason = self.journal.check_capacity(
            symbol, plan, cap=options.risk_cap, budget=options.budget, as_of=time.time(), portfolio=portfolio,
            daily_risk_cap=self.daily_risk_cap,
            telegram_route=self.publisher.route if self.publisher else None,
        )
        return (None, None, reason) if reason else (plan, portfolio, "")

    async def review(self, event, symbol, check, stats, review_id, *, history_snapshot):
        options = self.options
        event_revision = event["id"], event["revision"]
        plan = check["plan"]
        book = {field: plan.get(field) for field in ("price", "bid", "ask_size", "bid_size", "quote_observed_at")}
        snapshot = {
            "mode": "live", "event": event,
            "market": {**check["market"], "book": book, "quote_asset": plan["quote_asset"],
                       "plan": {field: plan[field] for field in _PUBLIC_PLAN_FIELDS}},
            "recorded_results": stats,
        }
        results = await analyze_team(
            self.private_session, snapshot,
            url=config.TRADE_REVIEW_URL, key=config.TRADE_REVIEW_KEY, model=config.TRADE_REVIEW_MODEL,
            concurrency=options.concurrency,
        )
        review_result = {"evidence": snapshot, "agent_results": results}
        reason = review_verdict(results)
        alert = None
        if not reason and history_snapshot is not self.cached_history:
            reason = "history_changed_during_review"
        if not reason:
            _, _, still_listed = await self.refresh_sources()
            if event_revision not in still_listed:
                reason = "release_changed_during_review"
            else:
                plan, portfolio, reason = await self.current_plan(check, symbol)
                if history_snapshot is not self.refresh_recorded_history():
                    reason = "history_changed_during_review"
                elif event_revision not in self.listed_news:
                    reason = "release_changed_during_review"
                elif plan is not None:
                    details = {"review_id": review_id, "pnl": stats, "completed_agents": len(results),
                               "reaction_pct": check["market"].get("reaction_pct"),
                               "btc_reaction_pct": check["market"].get("btc_reaction_pct")}
                    alert = self.journal.issue(event, options.venue, symbol, plan, cap=options.risk_cap,
                                               as_of=time.time(), details=details, review_result=review_result,
                                               budget=options.budget, portfolio=portfolio,
                                               reply_required=self.manual_replies,
                                               daily_risk_cap=self.daily_risk_cap,
                                               telegram_route=self.publisher.route if self.publisher else None)
                    if alert is None:
                        reason = "alert_expired_duplicate_or_risk_limit"
        if alert:
            emit(alert)
            if self.publisher is not None:
                await self.publisher.flush()
        else:
            self.journal.finish_review(review_id, {**review_result, "reason": reason, "signal_id": None})
            self.watch(event, symbol, reason, pnl=stats)

    async def scan_event(self, market, event, history, symbols):
        options = self.options
        family, eligible = classify_live_event(event["source"], event["title"])
        observations = await asyncio.gather(*(
            observe_reaction(market, symbol, event["known_at"]) for symbol in symbols
        ))
        candidates = []
        evidence_symbols = []
        for symbol, (observation, reason) in zip(symbols, observations, strict=True):
            if observation is None:
                self.watch(event, symbol, reason, family=family)
                continue
            reaction = self.journal.record_reaction(event, options.venue, symbol, family, observation)
            if reaction:
                emit(reaction)
            if not eligible:
                reason = "topic_unclassified" if family == "uncatalogued" else "market_link_unvalidated"
                self.watch(event, symbol, reason, family=family)
                continue
            if options.budget is None and self.sampler is None:
                self.watch(event, symbol, "sizing_parameters_required", family=family)
                continue
            draft, reason = await prepare_candidate(market, symbol, event["known_at"])
            if draft is None:
                self.watch(event, symbol, reason)
                continue
            if self.sampler is not None:
                evidence_symbols.append(symbol)
            if options.budget is None:
                self.watch(event, symbol, "sizing_parameters_required", family=family)
                continue
            stats = assess_recorded_history(history, family=family, venue=options.venue, symbol=symbol,
                                            strategy=BASELINE, as_of=time.time(), before=event["known_at"])
            if stats["status"] != "supported":
                self.watch(event, symbol, stats["reason"], pnl=stats)
                continue
            candidates.append({"event": event, "symbol": symbol, "draft": draft, "stats": stats})
        if evidence_symbols and self.sampler is not None:
            await self.sampler.collect(event, family, evidence_symbols)
        return candidates

    def schedule_reviews(self, candidates):
        # Keep only the latest batch while the single review worker is occupied.
        if self.review_batches.full():
            self.review_batches.get_nowait()
            self.review_batches.task_done()
        self.review_batches.put_nowait((candidates, self.cached_history))

    async def review_loop(self):
        while True:
            candidates, history_snapshot = await self.review_batches.get()
            try:
                await self.review_ranked(candidates, history_snapshot=history_snapshot)
            finally:
                self.review_batches.task_done()

    def review_blocker(self, event):
        if (event["id"], event["revision"]) not in self.listed_news:
            return "official_release_unavailable"
        if time.time() >= event["published_at"] + REACTION_WINDOW:
            return "release_window_expired"
        return ""

    def available_reviews(self, candidates):
        for candidate in candidates:
            event, symbol = candidate["event"], candidate["symbol"]
            if reason := self.review_blocker(event):
                self.watch(event, symbol, reason)
                continue
            review_id = key("live-review", event["id"], event["revision"], self.options.venue, symbol,
                            BASELINE, config.TRADE_REVIEW_MODEL)
            if not self.journal.has_review(review_id):
                yield candidate, review_id

    def manual_review_available(self, candidate_count):
        if not self.manual_replies or self.publisher is None:
            return True
        portfolio, reason = self.portfolio()
        if not reason:
            reason = self.journal.slots.capacity(
                self.publisher.route, portfolio.open_symbols if portfolio else frozenset(),
            )
        if reason:
            emit({"type": "manual_queue", "status": reason, "candidates_available": candidate_count})
        return not reason

    async def price_review(self, candidate, review_id):
        event, symbol, stats = candidate["event"], candidate["symbol"], candidate["stats"]
        plan, _, reason = await self.current_plan(candidate["draft"], symbol)
        if blocker := self.review_blocker(event):
            self.watch(event, symbol, blocker)
            return None
        if plan is None:
            self.watch(event, symbol, reason, pnl=stats)
            return None
        # Historical R is normalized by initial risk. Use the same basis for the current plan.
        profit = stats["lower_stressed_net_r"] * plan["loss"]
        if not 0 < profit < math.inf:
            self.watch(event, symbol, "profit_estimate_unavailable")
            return None
        return {"candidate": candidate, "review_id": review_id, "plan": plan, "profit": profit}

    async def review_ranked(self, candidates, *, history_snapshot):
        if not candidates:
            return
        options = self.options
        if not options.team:
            for candidate in candidates:
                self.watch(candidate["event"], candidate["symbol"], "agent_team_disabled", pnl=candidate["stats"])
            return
        eligible = self.available_reviews(candidates)
        reviews = 0
        while reviews < options.max_reviews_per_scan:
            if history_snapshot is not self.cached_history or not self.manual_review_available(len(candidates)):
                return
            batch = list(islice(eligible, MAX_SYMBOLS))
            if not batch:
                return
            estimates = [row for row in await asyncio.gather(
                *(self.price_review(candidate, review_id) for candidate, review_id in batch),
            ) if row is not None]
            refreshed = set()
            while estimates and reviews < options.max_reviews_per_scan:
                if (history_snapshot is not self.refresh_recorded_history()
                        or not self.manual_review_available(len(candidates))):
                    return
                estimates.sort(key=lambda row: (-row["profit"], ranking_key(row["candidate"])))
                row = estimates.pop(0)
                candidate, review_id, plan = row["candidate"], row["review_id"], row["plan"]
                event, symbol = candidate["event"], candidate["symbol"]
                if reason := self.review_blocker(event):
                    self.watch(event, symbol, reason)
                    continue
                now = time.time()
                deadline = min(plan.get("valid_until", math.inf),
                               plan["quote_observed_at"] + DEFAULT_QUOTE_MAX_AGE)
                if now >= deadline:
                    if review_id not in refreshed:
                        refreshed.add(review_id)
                        if updated := await self.price_review(candidate, review_id):
                            estimates.append(updated)
                    continue
                if self.journal.claim_review(review_id, event, now):
                    await self.review(event, symbol, {**candidate["draft"], "plan": plan}, candidate["stats"],
                                      review_id, history_snapshot=history_snapshot)
                    reviews += 1

    async def scan_once(self, *, background_reviews=False):
        (items, health, listed), (discovery, discovery_health) = await asyncio.gather(
            self.refresh_sources(), fetch_discovery_news(self.public_session),
        )
        now = time.time()
        discovered = self.journal.record_discoveries(discovery, now)
        events = [event for event in self.journal.current_news(now)
                  if now - event["published_at"] < REACTION_WINDOW]
        visible = {event["id"] for event in events}
        self.last_decisions = {identity: state for identity, state in self.last_decisions.items()
                               if identity[0] in visible}
        options = self.options
        history = self.refresh_recorded_history()
        try:
            symbols = await self.scan_symbols()
        except MarketError:
            emit({"type": "catalog_unavailable", "venue": options.venue})
            if background_reviews:
                self.schedule_reviews([])
            return
        emit({"type": "scan", "source_health": health, "received": len(items), "recent": len(events),
              "discovery_health": discovery_health, "discovery_candidates": discovered,
              "history_rows": len(history), "sizing_configured": options.budget is not None,
              "team_enabled": options.team, "symbols": len(symbols), "catalog_size": self.catalog_size,
              "collecting_market_evidence": self.sampler is not None,
              "daily_risk_configured": self.daily_risk_cap is not None})
        if self.scan_market.market is not self.market:
            self.scan_market = ScanMarket(self.market)
        self.scan_market.begin_scan()
        candidates = []
        for event in events:
            if (event["id"], event["revision"]) not in listed:
                self.watch(event, None, "official_release_unavailable")
                continue
            candidates.extend(await self.scan_event(self.scan_market, event, history, symbols))
        if self.all_pairs:
            chart_close = int(time.time() // CHART_MINUTES_SECONDS) * CHART_MINUTES_SECONDS
            current_symbols = set(symbols)
            self.candidate_cache = {
                identity: candidate for identity, candidate in self.candidate_cache.items()
                if identity[:2] in listed and identity[2] not in current_symbols
                and candidate["draft"]["setup"]["snapshot_5m_close"] == chart_close
                and time.time() - candidate["event"]["published_at"] < REACTION_WINDOW
            }
            for candidate in candidates:
                event = candidate["event"]
                self.candidate_cache[event["id"], event["revision"], candidate["symbol"]] = candidate
            candidates = list(self.candidate_cache.values())
        candidates.sort(key=ranking_key)
        if candidates:
            emit({"type": "candidate_ranking", "ranked": len(candidates), "top": [
                {"symbol": row["symbol"], "event_id": row["event"]["id"],
                 "lower_stressed_net_r": row["stats"]["lower_stressed_net_r"]}
                for row in candidates[:MAX_SYMBOLS]
            ]})
        if background_reviews:
            self.schedule_reviews(candidates)
        elif candidates:
            await self.review_ranked(candidates, history_snapshot=self.cached_history)


async def run_monitor(options, journal, publisher=None):
    async with aiohttp.ClientSession(trust_env=True) as public, aiohttp.ClientSession() as private:
        monitor = LiveMonitor(public, private, journal, options, publisher=publisher,
                              all_pairs=options.all_pairs, manual_replies=options.manual_replies,
                              collect_market_evidence=options.collect_market_evidence,
                              daily_risk_cap=options.daily_risk_cap)
        if options.once:
            await monitor.scan_once()
            if monitor.sampler is not None:
                await monitor.sampler.sample_due()
            if publisher is not None:
                await publisher.flush()
            return
        async with asyncio.TaskGroup() as tasks:
            wake = asyncio.Event()
            if options.team:
                tasks.create_task(monitor.review_loop())
            if monitor.sampler is not None:
                tasks.create_task(monitor.sampler.run())
            if publisher is not None:
                tasks.create_task(publisher.run())
                if options.manual_replies:
                    tasks.create_task(run_replies(
                        publisher.bot, publisher.chat_id, publisher.route, journal.slots, wake,
                        edit_lock=publisher.edit_lock,
                    ))
            while True:
                await monitor.scan_once(background_reviews=options.team)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=options.interval)
                except TimeoutError:
                    pass
                wake.clear()


async def run(options, journal):
    if not (options.telegram or options.telegram_check):
        await run_monitor(options, journal)
        return True

    # HTTPX logs full Bot API URLs at INFO level, including the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        async with Bot(config.TELEGRAM_TOKEN) as bot:
            status = await verify_chat(bot, options.telegram_chat_id)
            emit({"type": "telegram_check", "status": status})
            if status != "ready":
                return False
            if not options.telegram_check:
                if options.manual_replies:
                    status = await check_reply_transport(bot)
                    emit({"type": "telegram_replies_check", "status": status})
                    if status != "ready":
                        return False
                route = key("telegram", bot.id, options.telegram_chat_id)
                publisher = TelegramPublisher(bot, options.telegram_chat_id, route, journal.telegram,
                                              slots=journal.slots if options.manual_replies else None)
                await run_monitor(options, journal, publisher)
    except InvalidToken:
        emit({"type": "telegram_check", "status": "token_invalid"})
        return False
    except TelegramError:
        emit({"type": "telegram_check", "status": "telegram_unavailable"})
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Сигналы по живым новостям и котировкам")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Выполнить один цикл")
    mode.add_argument("--report", action="store_true", help="Показать журнал без сетевых запросов")
    mode.add_argument("--telegram-check", action="store_true", help="Проверить доступ к личному чату без отправки")
    parser.add_argument("--venue", choices=SUPPORTED_EXCHANGES, default="okx")
    universe = parser.add_mutually_exclusive_group()
    universe.add_argument("--symbols", nargs="+", default=["BTC/USDT"], metavar="PAIR")
    universe.add_argument("--all-pairs", action="store_true", help="По очереди сканировать весь каталог спота биржи")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--history-file", type=Path)
    parser.add_argument("--portfolio-file", type=Path, help="Снимок свободных USDT и открытых позиций")
    parser.add_argument("--budget", type=float, help="Капитал для расчёта позиции, USDT")
    parser.add_argument("--allocation-slots", type=int, choices=range(1, MAX_SYMBOLS + 1), default=1, metavar="1..16",
                        help="На сколько позиций разделить предел капитала при расчёте размера")
    parser.add_argument("--fee-rate", type=float, help="Комиссия биржи за одну сторону, доля")
    parser.add_argument("--risk-cap", type=float, help="Лимит суммарного сценарного риска, USDT")
    parser.add_argument("--daily-risk-cap", type=float,
                        help="Дневной лимит расчётного риска ручных сделок, по умолчанию --risk-cap")
    parser.add_argument("--team", action="store_true", help="Подключить 50 агентов к финальной проверке")
    parser.add_argument("--telegram", action="store_true", help="Отправлять сигналы в личный чат BOT_OWNER_ID")
    parser.add_argument("--manual-replies", action="store_true",
                        help="Ждать ответа в Telegram и вести до 16 открытых позиций")
    parser.add_argument("--collect-market-evidence", action="store_true",
                        help="Собирать публичные котировки кандидатов для последующей оценки")
    parser.add_argument("--concurrency", type=int, choices=range(1, 51), default=10, metavar="N")
    parser.add_argument("--max-reviews-per-scan", type=int, choices=range(1, MAX_REVIEWS_PER_SCAN + 1), default=1,
                        help="Сколько кандидатов можно передать команде за цикл")
    parser.add_argument("--interval", type=int, default=60, help="Интервал между циклами в секундах")
    options = parser.parse_args()
    if options.manual_replies and options.daily_risk_cap is None:
        options.daily_risk_cap = options.risk_cap
    if options.telegram_check or (options.telegram and not options.report):
        if not config.TELEGRAM_TOKEN.strip():
            parser.error("Для Telegram задайте TELEGRAM_TOKEN.")
        try:
            options.telegram_chat_id = parse_chat_id(config.BOT_OWNER_ID)
        except ValueError:
            parser.error("Для Telegram задайте числовой BOT_OWNER_ID личного чата.")
    if not (options.report or options.telegram_check):
        if options.manual_replies and (not options.telegram or options.once):
            parser.error("Для ответов владельца нужен постоянный запуск с --telegram.")
        if (len(options.symbols) > MAX_SYMBOLS or len(set(options.symbols)) != len(options.symbols)
                or any(not re.fullmatch(r"[A-Z0-9]{1,16}/USDT", pair) for pair in options.symbols)):
            parser.error(f"Укажите от 1 до {MAX_SYMBOLS} разных спотовых пар формата BTC/USDT.")
        if options.interval < 60:
            parser.error("Интервал должен быть не меньше 60 секунд.")
        sizing = (options.budget, options.fee_rate, options.risk_cap)
        if any(value is not None for value in sizing) and not (
            all(value is not None and math.isfinite(value) for value in sizing)
            and options.budget > 0 and 0 <= options.fee_rate < 0.05
            and 0 < options.risk_cap <= options.budget
        ):
            parser.error("Укажите капитал, комиссию за сторону и лимит риска одновременно.")
        if options.daily_risk_cap is not None and not (
            options.manual_replies and options.budget is not None and math.isfinite(options.daily_risk_cap)
            and 0 < options.daily_risk_cap <= options.budget
        ):
            parser.error("Для дневного лимита нужны ручные ответы и положительный лимит в пределах капитала.")
        if options.team:
            if options.budget is None:
                parser.error("Для команды нужны настроенные параметры риска.")
            if not (config.TRADE_REVIEW_KEY and config.TRADE_REVIEW_URL and config.TRADE_REVIEW_MODEL):
                parser.error("Для команды нужны TRADE_REVIEW_KEY, TRADE_REVIEW_URL и TRADE_REVIEW_MODEL.")
            if (options.all_pairs or options.manual_replies or len(options.symbols) > 4
                    or options.max_reviews_per_scan > 1) and options.portfolio_file is None:
                parser.error("Для каталога, очереди подтверждений или нескольких сигналов укажите --portfolio-file.")
    if options.report:
        report = LiveJournal(options.data_dir / "news-live.sqlite3").report()
        report["trade_readiness"] = readiness_report(options, as_of=time.time())
        emit(report)
        return
    journal = None if options.telegram_check else LiveJournal(options.data_dir / "news-live.sqlite3")
    try:
        if not asyncio.run(run(options, journal)):
            raise SystemExit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
