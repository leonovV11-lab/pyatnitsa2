"""Headless paper research for verified public news and market reactions."""

import argparse
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import aiohttp

import config
from market import SUPPORTED_EXCHANGES, Market, MarketError
from news_agent_team import analyze_team
from news_quant import assess_outcomes
from official_news import NewsItem, fetch_official_news
from paper_ledger import LedgerError, PaperLedger
from strategies import BASELINE, build_plan, candidate_setups, is_bullish

FAMILY_RULES = {
    "nvidia": ("nvidia_leadership", r"\b(?:CEO|Chief Executive Officer|interim chief executive)\b", False),
    "fed": ("us_monetary_policy", r"\b(?:FOMC statement|federal funds rate|monetary policy decision)\b", True),
    "ecb": ("eu_monetary_policy", r"\bmonetary policy decisions?\b", True),
    "bls": ("us_economic_release", r"\b(?:Consumer Price Index|Employment Situation|Producer Price Index)\b", True),
}
DISCOVERY_WINDOW = 36 * 3600
REACTION_WINDOW = 90 * 60
STRESS_RATE_PER_FILL = 0.001
MONITOR_SECONDS = 5


def classify_headline(source, title):
    pattern = FAMILY_RULES.get(source)
    if pattern and re.search(pattern[1], title, re.IGNORECASE):
        return pattern[0], pattern[2]
    return "uncatalogued", False


def decision_after_review(math_gate, results):
    if math_gate["status"] != "supported":
        return "watch", math_gate["reason"]
    if (len(results) != 50 or len({row["agent"] for row in results}) != 50
            or any(row["decision"] not in {"support", "watch", "veto"} for row in results)):
        return "watch", "Команда из 50 агентов не завершила разбор"
    if any(row["decision"] == "veto" for row in results):
        return "watch", "Команда отметила возражение"
    if not any(row["decision"] == "support" for row in results):
        return "watch", "Команда оставила ситуацию в наблюдении"
    return "paper_signal", "Проверки источника, расчёта и команды пройдены"


class NewsJournal:
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
                CREATE TABLE IF NOT EXISTS news_trials (
                    trial_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, family TEXT NOT NULL,
                    event_at REAL NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL,
                    risk_usdt REAL NOT NULL
                );
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

    def register_trial(self, trial_id, event_id, family, event_at, venue, symbol, risk):
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO news_trials VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trial_id) DO UPDATE SET risk_usdt=excluded.risk_usdt
                WHERE NOT EXISTS (SELECT 1 FROM paper_positions WHERE candidate_id=excluded.trial_id)
            """, (trial_id, event_id, family, event_at, venue, symbol, risk))

    def outcomes(self, family, venue, symbol, as_of):
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT n.trial_id AS outcome_id, n.event_id, n.event_at, n.risk_usdt,
                    p.opened_at, p.closed_at, SUM(f.pnl) AS net_pnl_usdt,
                    SUM(f.pnl - ? * f.qty * f.price) AS stressed_net_pnl_usdt
                FROM news_trials n JOIN paper_positions p ON p.candidate_id=n.trial_id
                JOIN paper_fills f USING(position_id)
                WHERE n.family=? AND n.exchange=? AND n.symbol=? AND p.closed_at<=?
                GROUP BY n.trial_id
                ORDER BY p.closed_at
            """, (STRESS_RATE_PER_FILL, family, venue, symbol, as_of))
            return [dict(row, available_at=row["closed_at"], provenance="paper_journal") for row in rows]

    def claim_review(self, review_id, event, now):
        with closing(self.connect()) as db, db:
            row = db.execute("INSERT OR IGNORE INTO news_reviews VALUES (?, ?, NULL, ?, ?, NULL)",
                             (review_id, now, event["id"], event["revision"]))
            return row.rowcount == 1

    def counts(self):
        with closing(self.connect()) as db:
            row = db.execute("""
                SELECT (SELECT COUNT(DISTINCT event_id) FROM official_headlines) AS news,
                    (SELECT COUNT(*) FROM news_reviews WHERE finished_at IS NOT NULL) AS finished_reviews,
                    (SELECT COUNT(*) FROM news_trials JOIN paper_positions ON trial_id=candidate_id) AS trials
            """).fetchone()
            return dict(row)

    def finish_review(self, review_id, result):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE news_reviews SET finished_at=?, result=? WHERE review_id=?",
                       (time.time(), json.dumps(result, ensure_ascii=False, allow_nan=False), review_id))


def key(*values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:32]


def emit(event):
    print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)


class NewsPaperRunner:
    def __init__(self, session, directory, venue, symbol, capital, budget, options, *, news_session=None):
        self.session = session
        self.news_session = news_session if news_session is not None else session
        self.market = Market(session, venue)
        self.venue = venue
        self.symbol = symbol
        self.team = options.team
        self.concurrency = options.concurrency
        self.last_decisions = {}
        self.review_slot = False
        self.shadow = PaperLedger(directory / "news-studies.sqlite3")
        self.signals = PaperLedger(directory / "news-paper-signals.sqlite3")
        self.journal = NewsJournal(directory / "news-studies.sqlite3")
        for ledger in (self.shadow, self.signals):
            account = ledger.report()
            if not account["capital"] or account["daily_budget"] != budget:
                ledger.configure(capital, budget)
            elif account["capital"] != capital:
                raise LedgerError("Капитал существующего бумажного журнала отличается от --capital")

    def refresh_news(self, items):
        for item in items:
            self.journal.observe(item)

    def report_decision(self, event, body):
        event_id = event["id"]
        state = (event["revision"], body["decision"], body["reason"])
        if self.last_decisions.get(event_id) == state:
            return
        self.last_decisions[event_id] = state
        emit({"type": "analysis", "source": event["source"], "headline": event["title"],
              "url": event["url"], "published_at": event["published_at"], "known_at": event["known_at"], **body})

    def paper_plan(self, ledger, setup, quote):
        account = ledger.report()
        budget = min(account["daily_budget"], account["equity"])
        return build_plan(setup, quote, budget, config.PAPER_FEE_RATE)

    def open_paper(self, ledger, trial_id, setup, plan):
        if time.time() - plan["quote_observed_at"] > 5:
            raise LedgerError("Срок действия снимка стакана истёк")
        return ledger.open(trial_id, self.venue, self.symbol, plan["qty"], plan["price"], setup["stop"],
                           setup["tp1"], setup["tp2"], config.PAPER_FEE_RATE, bid=plan["bid"])

    async def market_inputs(self):
        five, fifteen, btc = await asyncio.gather(
            self.market.candles(self.symbol, "5m", 50), self.market.candles(self.symbol, "15m", 50),
            self.market.candles("BTC/USDT", "15m", 50),
        )
        candidates = candidate_setups(self.symbol, five, fifteen, is_bullish(btc["close"]))
        setup = next((row for row in candidates if row["strategy"] == BASELINE and row["eligible"]), None)
        return five, setup

    async def inspect_event(self, event, family, can_signal, five, setup):
        now = time.time()
        if now - event["published_at"] > REACTION_WINDOW:
            self.report_decision(event, {"decision": "watch", "reason": "Истекло окно рыночной реакции"})
            return
        candle_start = five["ts"].map(lambda stamp: stamp.timestamp())
        before = five.loc[candle_start + 300 <= event["known_at"]]
        after = five.loc[candle_start >= event["known_at"]]
        if before.empty or after.empty or setup is None:
            self.report_decision(event, {"decision": "watch", "reason": "Рыночная реакция на закрытых свечах пока не подтверждена"})
            return
        event_move = float(after["close"].iloc[-1] / before["close"].iloc[-1] - 1)
        if event_move <= 0:
            self.report_decision(event, {"decision": "watch", "reason": "Реакция рынка не поддерживает спотовый вход"})
            return

        try:
            quote = await self.market.quote(self.symbol)
        except MarketError:
            self.report_decision(event, {"decision": "watch", "reason": "Нет пригодной котировки"})
            return
        plan, reason = self.paper_plan(self.shadow, setup, quote)
        if plan is None:
            self.report_decision(event, {"decision": "watch", "reason": reason})
            return
        trial_id = key("trial", event["id"], self.venue, self.symbol, BASELINE)
        self.journal.register_trial(trial_id, event["id"], family, event["known_at"],
                                    self.venue, self.symbol, plan["loss"])
        try:
            self.open_paper(self.shadow, trial_id, setup, plan)
        except LedgerError:
            # Existing trial or capital/risk limits can prevent a new shadow fill.
            pass
        samples = self.journal.outcomes(family, self.venue, self.symbol, now)
        stats = assess_outcomes(samples, as_of=now, before=event["known_at"])
        if not can_signal:
            stats = dict(stats, status="watch", reason="Для крипторынка это косвенная гипотеза, нужна отдельная проверка")
        if not self.team:
            self.report_decision(event, {"decision": "watch", "reason": stats["reason"] if stats["status"] != "supported"
                                         else "Команда из 50 агентов выключена", "paper_results": stats})
            return

        if not self.review_slot:
            self.report_decision(event, {"decision": "watch", "reason": "Ожидает следующего цикла команды"})
            return
        review_id = key(event["id"], event["revision"], self.venue, self.symbol, config.TRADE_REVIEW_MODEL)
        if not self.journal.claim_review(review_id, event, now):
            return
        self.review_slot = False
        bars = [{"time": row.ts.isoformat(), "open": float(row.open), "high": float(row.high),
                 "low": float(row.low), "close": float(row.close), "volume": float(row.volume)}
                for row in five.tail(18).itertuples(index=False)]
        snapshot = {"event": event, "market": {"venue": self.venue, "symbol": self.symbol, "move": event_move,
                                              "plan": plan, "bars_5m": bars},
                    "paper_results": {"statistics": stats, "recent_comparable": [
                        row for row in samples if row["available_at"] < event["known_at"]
                    ][-32:]}}
        results = await analyze_team(self.session, snapshot, url=config.TRADE_REVIEW_URL,
                                     key=config.TRADE_REVIEW_KEY, model=config.TRADE_REVIEW_MODEL,
                                     concurrency=self.concurrency)
        decision, reason = decision_after_review(stats, results)
        if decision == "paper_signal":
            decision, reason = await self.recheck_and_enter(event, setup)
        result = {"decision": decision, "reason": reason, "paper_results": stats,
                  "agent_count": len(results), "completed_agents": sum(r["decision"] != "unavailable" for r in results)}
        self.journal.finish_review(review_id, {"result": result, "evidence": snapshot, "agent_results": results})
        self.report_decision(event, result)

    async def recheck_and_enter(self, event, setup):
        items, health = await fetch_official_news(self.news_session)
        self.refresh_news(items)
        current = next((row for row in self.journal.current_news(time.time()) if row["id"] == event["id"]), None)
        still_listed = any(item.id == event["id"] and item.revision == event["revision"] for item in items)
        if (not still_listed or current is None or current["revision"] != event["revision"]
                or health.get(event["source"]) != "ok" or time.time() - event["published_at"] > REACTION_WINDOW):
            return "watch", "Источник, актуальность новости или её редакция изменились"
        try:
            quote = await self.market.quote(self.symbol)
            plan, reason = self.paper_plan(self.signals, setup, quote)
            if plan is None:
                return "watch", reason
            signal_id = key("signal", event["id"], self.venue, self.symbol)
            self.open_paper(self.signals, signal_id, setup, plan)
        except (LedgerError, MarketError) as exc:
            return "watch", str(exc)
        return "paper_signal", "Зафиксировано бумажное исполнение после проверки источника, команды и котировки"

    async def scan_once(self):
        self.review_slot = True
        items, health = await fetch_official_news(self.news_session)
        self.refresh_news(items)
        events = []
        for item in self.journal.current_news(time.time()):
            family, can_signal = classify_headline(item["source"], item["title"])
            if family != "uncatalogued":
                if health.get(item["source"]) == "ok":
                    events.append((item, family, can_signal))
                else:
                    self.report_decision(item, {"decision": "watch", "reason": "Первоисточник временно недоступен"})
        emit({"type": "scan", "source_health": health, "received": len(items), "matched": len(events)})
        if not events:
            return
        try:
            five, setup = await self.market_inputs()
        except MarketError as exc:
            emit({"type": "market_unavailable", "reason": str(exc)})
            return
        if five.empty:
            return
        for event, family, can_signal in events:
            await self.inspect_event(event, family, can_signal, five, setup)

    async def monitor_once(self):
        positions = [(ledger, position) for ledger in (self.shadow, self.signals) for position in ledger.open_positions()]
        if not positions:
            return
        try:
            quote = await self.market.quote(self.symbol)
        except MarketError:
            return
        for ledger, position in positions:
            if position["symbol"] != self.symbol or position["exchange"] != self.venue or not quote.is_fresh():
                continue
            ledger.mark(position["position_id"], quote.bid, trailing_fraction=0.01, available_qty=quote.bid_size)

    async def loop(self, work, interval):
        while True:
            await work()
            await asyncio.sleep(interval)


async def run(options):
    async with aiohttp.ClientSession() as session, aiohttp.ClientSession(trust_env=True) as news_session:
        runner = NewsPaperRunner(session, options.data_dir, options.venue, options.symbol,
                                 options.capital, options.budget, options, news_session=news_session)
        if options.once:
            await runner.monitor_once()
            await runner.scan_once()
            return
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(runner.loop(runner.monitor_once, MONITOR_SECONDS))
            tasks.create_task(runner.loop(runner.scan_once, options.interval))


def main():
    parser = argparse.ArgumentParser(description="Публичные новости, график и автономный бумажный прогон")
    parser.add_argument("--venue", choices=SUPPORTED_EXCHANGES, default="okx")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/news"))
    parser.add_argument("--capital", type=float, default=1000)
    parser.add_argument("--budget", type=float, default=300)
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--report", action="store_true", help="Показать итоги из локального журнала")
    parser.add_argument("--team", action="store_true", help="Включить до 50 запросов к настроенному ИИ на разбор")
    parser.add_argument("--concurrency", type=int, default=10, choices=range(1, 51), metavar="1..50")
    options = parser.parse_args()
    if (not re.fullmatch(r"[A-Z0-9]+/USDT", options.symbol) or options.interval < 60
            or not math.isfinite(config.PAPER_FEE_RATE) or not 0 <= config.PAPER_FEE_RATE < 0.1):
        parser.error("Укажите спотовую пару вида BTC/USDT, интервал от 60 секунд и корректную комиссию.")
    if options.report:
        study = options.data_dir / "news-studies.sqlite3"
        signals = options.data_dir / "news-paper-signals.sqlite3"
        if not study.is_file() or not signals.is_file():
            parser.error("В этой папке ещё нет журналов бумажного прогона.")
        emit({"type": "report", "journal": NewsJournal(study).counts(),
              "shadow": PaperLedger(study).report(), "signals": PaperLedger(signals).report()})
        return
    if options.team and not (config.TRADE_REVIEW_URL.startswith("https://") and config.TRADE_REVIEW_KEY
                             and config.TRADE_REVIEW_MODEL):
        parser.error("Для команды укажите HTTPS TRADE_REVIEW_URL, TRADE_REVIEW_KEY и TRADE_REVIEW_MODEL.")
    try:
        asyncio.run(run(options))
    except LedgerError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
