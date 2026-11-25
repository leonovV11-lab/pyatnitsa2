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
from pathlib import Path

import aiohttp
from telegram import Bot
from telegram.error import InvalidToken, TelegramError

import config
from live_delivery import TelegramOutbox, TelegramPublisher
from live_evidence import assess_recorded_history, read_recorded_history
from live_market_check import observe_reaction, prepare_candidate, refresh_plan
from live_portfolio import MAX_PORTFOLIO_AGE_SECONDS, read_portfolio
from live_telegram import parse_chat_id, verify_chat
from market import DEFAULT_QUOTE_MAX_AGE, SUPPORTED_EXCHANGES, Market
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
from strategies import BASELINE, MAX_POSITION_FRACTION

SIGNAL_TTL = 5
RISK_RESERVATION_SECONDS = 300
MAX_SYMBOLS = 16
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
_PUBLIC_PLAN_FIELDS = ("strategy", "price", "stop", "tp1", "tp2", "ratio", "entry_min", "entry_max", "quote_observed_at")


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
    def capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio):
        risk = plan.get("stressed_loss_usdt", plan["loss"])
        cash_needed = plan.get("qty", 0) * plan.get("price", 0) * (1 + plan.get("fee_rate", 0))
        since = as_of - RISK_RESERVATION_SECONDS
        open_risk = 0.0
        cash_available = math.inf if budget is None else budget
        if portfolio is not None:
            if not 0 <= as_of - portfolio.observed_at <= MAX_PORTFOLIO_AGE_SECONDS:
                return "portfolio_snapshot_expired"
            if symbol in portfolio.open_symbols:
                return "symbol_already_held"
            since = max(since, portfolio.observed_at)
            open_risk = portfolio.stressed_risk_usdt
            cash_available = min(cash_available, portfolio.available_cash_usdt)
        reserved = db.execute("""
            SELECT COALESCE(SUM(risk), 0) AS risk,
                COALESCE(MAX(symbol=?), 0) AS occupied,
                COALESCE(SUM(json_extract(payload,'$.plan.qty') * json_extract(payload,'$.plan.price')
                    * (1 + COALESCE(json_extract(payload,'$.plan.fee_rate'), 0))), 0) AS cash
            FROM live_signals WHERE issued_at>? AND issued_at<=?
        """, (symbol, since, as_of)).fetchone()
        if reserved["occupied"]:
            return "symbol_already_reserved"
        if open_risk + reserved["risk"] + risk > cap:
            return "portfolio_stressed_risk_limit"
        if reserved["cash"] + cash_needed > cash_available:
            return "insufficient_unreserved_cash"
        return ""

    def check_capacity(self, symbol, plan, *, cap, budget, as_of, portfolio):
        with closing(self.connect()) as db:
            return self.capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio)

    def has_review(self, review_id):
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM news_reviews WHERE review_id=?", (review_id,)).fetchone() is not None

    def issue(self, event, venue, symbol, plan, *, cap, as_of, details=None, review_result=None,
              telegram_route=None, budget=None, portfolio=None):
        risk = plan.get("stressed_loss_usdt", plan["loss"])
        quote_time = plan.get("quote_observed_at", as_of)
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
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.current_revision(db, event["id"], as_of) != event["revision"]:
                return None
            if self.capacity_reason(db, symbol, plan, cap, budget, as_of, portfolio):
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
                "telegram_deliveries": self.telegram.counts()}


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


def ranking_key(candidate):
    stats = candidate["stats"]
    return (-stats["lower_stressed_net_r"], -stats["recent_mean_stressed_net_r"],
            -stats["day_count"], candidate["symbol"], candidate["event"]["id"])


class ScanMarket:
    """Reuse each public candle response within one scan."""

    def __init__(self, market):
        self.exchange = market.exchange
        self.quote = market.quote
        self.market = market
        self.requests = {}

    async def candles(self, symbol, interval, limit):
        identity = symbol, interval, limit
        if identity not in self.requests:
            self.requests[identity] = asyncio.create_task(self.market.candles(symbol, interval, limit))
        return await self.requests[identity]


class LiveMonitor:
    def __init__(self, public_session, private_session, journal, options, publisher=None):
        self.public_session = public_session
        self.private_session = private_session
        self.journal = journal
        self.options = options
        self.publisher = publisher
        self.market = Market(public_session, options.venue)
        self.last_decisions = {}
        self.listed_news = set()

    def watch(self, event, symbol, reason, **details):
        identity = event["id"], symbol
        state = event["revision"], reason, details
        if self.last_decisions.get(identity) == state:
            return
        self.last_decisions[identity] = state
        emit({"type": "watch", "event_id": event["id"], "source": event["source"],
              "headline": event["title"], "url": event["url"], "symbol": symbol, "reason": reason, **details})

    async def refresh_sources(self):
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

    async def current_plan(self, draft, symbol):
        options = self.options
        sizing_budget = options.budget * min(1.0, 1.0 / (MAX_POSITION_FRACTION * options.allocation_slots))
        plan, reason = await refresh_plan(self.market, draft["setup"], sizing_budget, options.fee_rate)
        if plan is None:
            return None, None, reason
        portfolio, reason = self.portfolio()
        if reason:
            return None, None, reason
        plan["stressed_loss_usdt"] = stressed_loss(plan)
        reason = self.journal.check_capacity(symbol, plan, cap=options.risk_cap, budget=options.budget,
                                             as_of=time.time(), portfolio=portfolio)
        return (None, None, reason) if reason else (plan, portfolio, "")

    async def review(self, event, symbol, check, stats, review_id):
        options = self.options
        plan = check["plan"]
        book = {field: plan.get(field) for field in ("price", "bid", "ask_size", "bid_size", "quote_observed_at")}
        snapshot = {
            "mode": "live", "event": event,
            "market": {**check["market"], "book": book, "plan": {field: plan[field] for field in _PUBLIC_PLAN_FIELDS}},
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
        if not reason:
            _, _, still_listed = await self.refresh_sources()
            if (event["id"], event["revision"]) not in still_listed:
                reason = "release_changed_during_review"
            else:
                plan, portfolio, reason = await self.current_plan(check, symbol)
                if plan is not None:
                    details = {"review_id": review_id, "pnl": stats, "completed_agents": len(results),
                               "reaction_pct": check["market"].get("reaction_pct"),
                               "btc_reaction_pct": check["market"].get("btc_reaction_pct")}
                    alert = self.journal.issue(event, options.venue, symbol, plan, cap=options.risk_cap,
                                               as_of=time.time(), details=details, review_result=review_result,
                                               budget=options.budget, portfolio=portfolio,
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

    async def scan_event(self, market, event, history):
        options = self.options
        family, eligible = classify_live_event(event["source"], event["title"])
        observations = await asyncio.gather(*(
            observe_reaction(market, symbol, event["known_at"]) for symbol in options.symbols
        ))
        candidates = []
        for symbol, (observation, reason) in zip(options.symbols, observations, strict=True):
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
            if options.budget is None:
                self.watch(event, symbol, "sizing_parameters_required", family=family)
                continue
            draft, reason = await prepare_candidate(market, symbol, event["known_at"])
            if draft is None:
                self.watch(event, symbol, reason)
                continue
            stats = assess_recorded_history(history, family=family, venue=options.venue, symbol=symbol,
                                            strategy=BASELINE, as_of=time.time(), before=event["known_at"])
            if stats["status"] != "supported":
                self.watch(event, symbol, stats["reason"], pnl=stats)
                continue
            candidates.append({"event": event, "symbol": symbol, "draft": draft, "stats": stats})
        return candidates

    async def review_ranked(self, candidates):
        options = self.options
        reviews = 0
        for candidate in candidates:
            event, symbol, draft, stats = (
                candidate[field] for field in ("event", "symbol", "draft", "stats")
            )
            if not options.team:
                self.watch(event, symbol, "agent_team_disabled", pnl=stats)
                continue
            if reviews >= options.max_reviews_per_scan:
                break
            if (event["id"], event["revision"]) not in self.listed_news:
                self.watch(event, symbol, "official_release_unavailable")
                continue
            review_id = key("live-review", event["id"], event["revision"], options.venue, symbol,
                            BASELINE, config.TRADE_REVIEW_MODEL)
            if self.journal.has_review(review_id):
                continue
            plan, _, reason = await self.current_plan(draft, symbol)
            if plan is None:
                self.watch(event, symbol, reason, pnl=stats)
                continue
            if self.journal.claim_review(review_id, event, time.time()):
                await self.review(event, symbol, {**draft, "plan": plan}, stats, review_id)
                reviews += 1

    async def scan_once(self):
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
        try:
            history = read_recorded_history(options.history_file)
        except (OSError, ValueError):
            history = []
            emit({"type": "history_unavailable", "reason": "cannot_read_real_fill_export"})
        emit({"type": "scan", "source_health": health, "received": len(items), "recent": len(events),
              "discovery_health": discovery_health, "discovery_candidates": discovered,
              "history_rows": len(history), "sizing_configured": options.budget is not None,
              "team_enabled": options.team, "symbols": len(options.symbols)})
        market = ScanMarket(self.market)
        candidates = []
        for event in events:
            if (event["id"], event["revision"]) not in listed:
                self.watch(event, None, "official_release_unavailable")
                continue
            candidates.extend(await self.scan_event(market, event, history))
        candidates.sort(key=ranking_key)
        if candidates:
            emit({"type": "candidate_ranking", "ranked": len(candidates), "top": [
                {"symbol": row["symbol"], "event_id": row["event"]["id"],
                 "lower_stressed_net_r": row["stats"]["lower_stressed_net_r"]}
                for row in candidates[:MAX_SYMBOLS]
            ]})
            await self.review_ranked(candidates)


async def run_monitor(options, journal, publisher=None):
    async with aiohttp.ClientSession(trust_env=True) as public, aiohttp.ClientSession() as private:
        monitor = LiveMonitor(public, private, journal, options, publisher=publisher)
        if options.once:
            await monitor.scan_once()
            if publisher is not None:
                await publisher.flush()
            return
        async with asyncio.TaskGroup() as tasks:
            if publisher is not None:
                tasks.create_task(publisher.run())
            while True:
                await monitor.scan_once()
                await asyncio.sleep(options.interval)


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
                route = key("telegram", bot.id, options.telegram_chat_id)
                publisher = TelegramPublisher(bot, options.telegram_chat_id, route, journal.telegram)
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
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT"], metavar="PAIR")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--history-file", type=Path)
    parser.add_argument("--portfolio-file", type=Path, help="Снимок свободных USDT и открытых позиций")
    parser.add_argument("--budget", type=float, help="Капитал для расчёта позиции, USDT")
    parser.add_argument("--allocation-slots", type=int, choices=range(1, MAX_SYMBOLS + 1), default=1, metavar="1..16",
                        help="На сколько позиций разделить предел капитала при расчёте размера")
    parser.add_argument("--fee-rate", type=float, help="Комиссия биржи за одну сторону, доля")
    parser.add_argument("--risk-cap", type=float, help="Лимит суммарного сценарного риска, USDT")
    parser.add_argument("--team", action="store_true", help="Подключить 50 агентов к финальной проверке")
    parser.add_argument("--telegram", action="store_true", help="Отправлять сигналы в личный чат BOT_OWNER_ID")
    parser.add_argument("--concurrency", type=int, choices=range(1, 51), default=10, metavar="N")
    parser.add_argument("--max-reviews-per-scan", type=int, choices=range(1, MAX_REVIEWS_PER_SCAN + 1), default=1,
                        help="Сколько кандидатов можно передать команде за цикл")
    parser.add_argument("--interval", type=int, default=60, help="Интервал между циклами в секундах")
    options = parser.parse_args()
    if options.telegram_check or (options.telegram and not options.report):
        if not config.TELEGRAM_TOKEN.strip():
            parser.error("Для Telegram задайте TELEGRAM_TOKEN.")
        try:
            options.telegram_chat_id = parse_chat_id(config.BOT_OWNER_ID)
        except ValueError:
            parser.error("Для Telegram задайте числовой BOT_OWNER_ID личного чата.")
    if not (options.report or options.telegram_check):
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
        if options.team:
            if options.budget is None:
                parser.error("Для команды нужны настроенные параметры риска.")
            if not (config.TRADE_REVIEW_KEY and config.TRADE_REVIEW_URL and config.TRADE_REVIEW_MODEL):
                parser.error("Для команды нужны TRADE_REVIEW_KEY, TRADE_REVIEW_URL и TRADE_REVIEW_MODEL.")
            if (len(options.symbols) > 4 or options.max_reviews_per_scan > 1) and options.portfolio_file is None:
                parser.error("Для нескольких сигналов за цикл или более четырёх пар укажите --portfolio-file.")
    if options.report:
        emit(LiveJournal(options.data_dir / "news-live.sqlite3").report())
        return
    journal = None if options.telegram_check else LiveJournal(options.data_dir / "news-live.sqlite3")
    try:
        if not asyncio.run(run(options, journal)):
            raise SystemExit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
