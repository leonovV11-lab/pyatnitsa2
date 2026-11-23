"""Private Telegram assistant for signals, research and paper trading."""

import asyncio
import logging
import math
import time
from dataclasses import asdict
from html import escape
from pathlib import Path

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import config
from market import SUPPORTED_EXCHANGES, Market, MarketError
from paper_ledger import LedgerError, PaperLedger
from research import Research, candidate_key, review_key
from strategies import BASELINE, EXPERIMENTAL, build_plan, candidate_setups, is_bullish
from trade_reviewer import REVIEW_INSTRUCTIONS, review_candidates

LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = 30
MONITOR_INTERVAL = 5
SIGNAL_TTL = 90
UNIVERSE_LIMIT = 60
SIGNALS_PER_SCAN = 5
REVIEW_BATCH_SIZE = 10
STUDY_ARMS = ("baseline", "strict", "ai", *EXPERIMENTAL)


def menu(paused):
    action = ("Запустить поиск", "resume") if paused else ("Пауза новых сигналов", "pause")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(action[0], callback_data=action[1])],
        [InlineKeyboardButton("Наблюдение", callback_data="watch"),
         InlineKeyboardButton("Результаты", callback_data="report")],
    ])


def paper_entry(ledger, candidate, plan):
    return ledger.open(
        candidate["id"], candidate["exchange"], candidate["symbol"], plan["qty"],
        plan["price"], plan["stop"], plan["tp1"], plan["tp2"], config.PAPER_FEE_RATE, bid=plan["bid"],
    )


class TradingAssistant:
    def __init__(self, owner, path, exchanges):
        self.owner = owner
        self.exchanges = exchanges
        self.ledger = PaperLedger(path, legacy_path=config.PORTFOLIO_FILE)
        self.research = Research(path)
        path = Path(path)
        self.studies = {
            arm: PaperLedger(path.with_name(f"{path.stem}-{arm}{path.suffix}"))
            for arm in STUDY_ARMS
        }
        self.session = None
        self.markets = {}
        self.application = None
        self.tasks = []
        self.review_task = None
        self.paused = True
        self.review_enabled = bool(
            config.TRADE_REVIEW_ENABLED and config.TRADE_REVIEW_MODEL and config.TRADE_REVIEW_KEY
            and config.TRADE_REVIEW_URL.startswith("https://")
        )
        if config.TRADE_REVIEW_ENABLED and not self.review_enabled:
            LOGGER.warning("Shadow review unavailable: configure model, key and HTTPS URL")
        self.pending = {}
        self.watch = []
        self.health = {}
        self.scan_health = {}
        self.candle_cache = {}

    async def start(self, app):
        self.application = app
        self.session = aiohttp.ClientSession()
        self.markets = {venue: Market(self.session, venue) for venue in SUPPORTED_EXCHANGES}
        self.tasks = [
            asyncio.create_task(self.scan_loop(), name="market-scan"),
            asyncio.create_task(self.monitor_loop(), name="paper-monitor"),
        ]

    async def stop(self, _app):
        tasks = self.tasks + ([self.review_task] if self.review_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def notify(self, text, **kwargs):
        if self.application is None:
            raise RuntimeError("Telegram application has not been started")
        return await self.application.bot.send_message(chat_id=self.owner, text=text, **kwargs)

    async def closed_candles(self, venue, symbol, timeframe):
        seconds = {"5m": 300, "15m": 900}[timeframe]
        interval = int(time.time()) // seconds
        key = (venue, symbol, timeframe)
        cached = self.candle_cache.get(key)
        if cached and cached[0] == interval:
            return cached[1]
        candles = await self.markets[venue].candles(symbol, timeframe, 51)
        if int(candles["ts"].iloc[-1].timestamp()) + seconds != interval * seconds:
            raise MarketError(f"{venue} {symbol} {timeframe}: ожидается закрытая свеча")
        self.candle_cache[key] = (interval, candles)
        return candles

    async def candidates(self, venue):
        market = self.markets[venue]
        symbols = await market.universe(limit=UNIVERSE_LIMIT)
        btc = await self.closed_candles(venue, "BTC/USDT", "15m")
        btc_bullish = is_bullish(btc["close"])
        candidates = []
        failures = 0
        for offset in range(0, len(symbols), 8):
            chunk = symbols[offset:offset + 8]
            for symbol, result in zip(chunk, await asyncio.gather(
                *(self.analyze(venue, symbol, btc_bullish) for symbol in chunk),
                return_exceptions=True,
            )):
                if isinstance(result, MarketError):
                    failures += 1
                    LOGGER.info("%s market data unavailable for %s: %s", venue, symbol, result)
                    continue
                if isinstance(result, BaseException):
                    raise result
                candidates.extend(result)
        self.scan_health[venue] = (f"данные {len(symbols) - failures}/{len(symbols)}", time.time())
        return candidates

    async def analyze(self, venue, symbol, btc_bullish):
        five, fifteen = await asyncio.gather(
            self.closed_candles(venue, symbol, "5m"),
            self.closed_candles(venue, symbol, "15m"),
        )
        candidates = candidate_setups(symbol, five, fifteen, btc_bullish)
        for candidate in candidates:
            strategy, candle = candidate["strategy"], candidate["candle"]
            candidate.update(id=candidate_key(venue, symbol, strategy, candle), exchange=venue)
            decision = "eligible" if candidate["eligible"] else "reject"
            self.research.record_candidate(
                candidate["id"], exchange=venue, symbol=symbol, strategy=strategy,
                candle=candle, observed_at=time.time(), payload=candidate,
                decision=decision, reason="" if candidate["eligible"] else candidate["rule_reason"],
            )
        return candidates

    async def scan_loop(self):
        while True:
            if not self.paused:
                try:
                    await self.scan_once()
                except Exception:
                    LOGGER.exception("Signal scan failed")
            await asyncio.sleep(SCAN_INTERVAL)

    async def scan_market(self, venue):
        try:
            candidates = [c for c in await self.candidates(venue) if c["eligible"]]
            quotes = await self.markets[venue].quotes(sorted({c["symbol"] for c in candidates}))
            return candidates, quotes
        except MarketError as exc:
            self.scan_health[venue] = ("котировки недоступны", time.time())
            LOGGER.warning("Scan unavailable for %s: %s", venue, exc)
            return [], {}

    async def scan_once(self):
        budget = self.sizing_budget(self.ledger)
        actionable = []
        watching = []
        study_candidates = []
        markets = await asyncio.gather(*(self.scan_market(venue) for venue in self.exchanges))
        for candidates, quotes in markets:
            for candidate in candidates:
                quote = quotes.get(candidate["symbol"])
                plan, reason = build_plan(candidate, quote, budget, config.PAPER_FEE_RATE)
                watching.append((candidate, plan, reason))
                if quote is not None:
                    study_candidates.append((candidate, quote))
                self.research.record_event(
                    candidate["id"], stage="quote", decision="tradable" if plan else "watch",
                    reason=reason, payload={"plan": plan, "quote": asdict(quote) if quote else None},
                    event_key=f"quote:{candidate['id']}:{int(time.time()) // 30}",
                )
                if plan and (candidate["strategy"] == BASELINE or config.EXPERIMENTAL_SIGNALS):
                    actionable.append((candidate, plan))
        actionable.sort(key=lambda pair: (pair[1]["score"], pair[1]["ratio"]), reverse=True)
        self.watch = sorted(watching, key=lambda row: (row[1] is not None, row[0]["score"]), reverse=True)

        if self.paused:
            return
        study_candidates.sort(key=lambda item: item[0]["score"], reverse=True)
        cohort = [(c, q) for c, q in study_candidates if c["strategy"] == BASELINE][:REVIEW_BATCH_SIZE]
        self.feed_studies(cohort, ("baseline", "strict"))
        for strategy in EXPERIMENTAL:
            experiment = [(c, q) for c, q in study_candidates if c["strategy"] == strategy][:REVIEW_BATCH_SIZE]
            self.feed_studies(experiment, (strategy,))
        if (self.review_enabled and cohort
                and (not self.review_task or self.review_task.done())):
            self.review_task = asyncio.create_task(self.shadow_review(cohort), name="shadow-ai-review")

        await self.send_current_signals(actionable)

    async def delivery_quotes(self, venue, symbols):
        try:
            return await self.markets[venue].quotes(sorted(symbols))
        except MarketError as exc:
            LOGGER.warning("Fresh delivery quotes unavailable for %s: %s", venue, exc)
            return {}

    async def send_current_signals(self, actionable):
        attempted = self.research.attempted_deliveries([c["id"] for c, _ in actionable])
        queue = [c for c, _ in actionable if c["id"] not in attempted and c["id"] not in self.pending]
        grouped = {}
        for candidate in queue:
            grouped.setdefault(candidate["exchange"], set()).add(candidate["symbol"])
        fresh = dict(zip(grouped, await asyncio.gather(
            *(self.delivery_quotes(venue, symbols) for venue, symbols in grouped.items())
        )))

        sent = 0
        for candidate in queue:
            if self.paused or sent >= SIGNALS_PER_SCAN:
                break
            quote = fresh[candidate["exchange"]].get(candidate["symbol"])
            if quote is not None and not quote.is_fresh():
                try:
                    quote = await self.markets[candidate["exchange"]].quote(candidate["symbol"])
                except MarketError:
                    quote = None
            plan, reason = build_plan(candidate, quote, self.sizing_budget(self.ledger), config.PAPER_FEE_RATE)
            if self.paused:
                break
            if not plan:
                self.research.record_event(candidate["id"], stage="delivery", decision="watch", reason=reason)
                continue
            try:
                self.ledger.check_open(
                    candidate["id"], candidate["exchange"], candidate["symbol"], plan["qty"],
                    plan["price"], plan["stop"], plan["tp1"], plan["tp2"], config.PAPER_FEE_RATE, bid=plan["bid"],
                )
            except LedgerError as exc:
                self.research.record_event(candidate["id"], stage="delivery", decision="risk_rejected", reason=str(exc))
                continue
            reserved = self.research.record_event(
                candidate["id"], stage="delivery", decision="attempted", event_key=f"push:{candidate['id']}",
            )
            if reserved and await self.deliver(candidate, plan):
                sent += 1

    @staticmethod
    def sizing_budget(ledger):
        account = ledger.report()
        return min(account["daily_budget"], account["equity"])

    async def deliver(self, candidate, plan):
        token = candidate["id"]
        venue, symbol = candidate["exchange"], candidate["symbol"]
        why = ", ".join(candidate["evidence"]) or candidate["strategy"]
        message = (
            f"<b>Бумажный сигнал · {escape(symbol)} · {escape(venue)}</b>\n"
            f"Основание: {escape(why)}\n"
            f"Вход: {plan['price']:.8g} · Диапазон: {plan['entry_min']:.8g}–{plan['entry_max']:.8g}\n"
            f"Стоп: {plan['stop']:.8g} · Тейки: {plan['tp1']:.8g} / {plan['tp2']:.8g}\n"
            f"Сценарий после комиссии: риск до уровня стопа {plan['loss']:.2f} USDT, "
            f"оба тейка {plan['reward']:.2f} USDT. При скачке цена исполнения может быть хуже.\n"
            f"Перед бумажной покупкой цена проверяется повторно. Карточка действует {SIGNAL_TTL} с."
        )
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Бумажная покупка", callback_data=f"paper:{token}"),
            InlineKeyboardButton("Пропустить", callback_data=f"skip:{token}"),
        ]])
        self.pending[token] = (candidate, time.monotonic() + SIGNAL_TTL, None)
        try:
            sent = await self.notify(message, parse_mode="HTML", reply_markup=buttons)
        except Exception:
            self.pending.pop(token, None)
            self.research.record_event(token, stage="delivery", decision="error")
            LOGGER.exception("Signal delivery failed")
            return False
        if token in self.pending:
            self.pending[token] = (candidate, self.pending[token][1], sent.message_id)
        self.research.record_event(token, stage="delivery", decision="sent", payload=plan)
        return True

    def feed_studies(self, cohort, arms):
        for arm in arms:
            ledger = self.studies[arm]
            for candidate, quote in cohort:
                plan, reason = build_plan(candidate, quote, self.sizing_budget(ledger), config.PAPER_FEE_RATE)
                if not plan:
                    self.research.record_event(candidate["id"], stage=f"paper_{arm}", decision="watch", reason=reason)
                    continue
                if arm == "strict" and (plan["score"] < 80 or plan["ratio"] < 1.25):
                    continue
                try:
                    paper_entry(ledger, candidate, plan)
                except LedgerError:
                    continue
                self.research.record_event(candidate["id"], stage=f"paper_{arm}", decision="opened", payload=plan)

    async def shadow_review(self, cohort):
        try:
            reviewer = review_key(config.TRADE_REVIEW_MODEL, REVIEW_INSTRUCTIONS)
            for venue in self.exchanges:
                by_id = {}
                for candidate, quote in cohort:
                    if candidate["exchange"] != venue:
                        continue
                    plan, _ = build_plan(candidate, quote, self.sizing_budget(self.studies["ai"]), config.PAPER_FEE_RATE)
                    if plan:
                        by_id[candidate["id"]] = (candidate, plan)
                ids = self.research.claim_review(list(by_id), reviewer)
                by_symbol = {}
                duplicate_ids = []
                for candidate_id in ids:
                    candidate, plan = by_id[candidate_id]
                    if candidate["symbol"] in by_symbol:
                        duplicate_ids.append(candidate_id)
                    else:
                        by_symbol[candidate["symbol"]] = (candidate, plan)
                for candidate_id in duplicate_ids:
                    self.research.finish_review(candidate_id, reviewer, "unavailable", "Символ уже выбран в этой пачке")
                if not by_symbol:
                    continue
                started = time.monotonic()
                try:
                    reviews = await review_candidates(
                        self.session,
                        [{"exchange": venue, "evidence": c["evidence"], "btc_bullish": c["btc_bullish"], **p}
                         for c, p in by_symbol.values()],
                        self.studies["ai"].open_positions(),
                        url=config.TRADE_REVIEW_URL, key=config.TRADE_REVIEW_KEY, model=config.TRADE_REVIEW_MODEL,
                    )
                except Exception:
                    LOGGER.exception("Shadow review failed")
                    reviews = {}
                latency = (time.monotonic() - started) * 1000
                approved = []
                for symbol, (candidate, _) in by_symbol.items():
                    review = reviews.get(symbol, {"decision": "unavailable", "reason": "Нет валидного ответа модели"})
                    self.research.finish_review(
                        candidate["id"], reviewer, review["decision"], review["reason"], latency_ms=latency,
                    )
                    if review["decision"] == "approve":
                        approved.append(candidate)
                for candidate in approved:
                    try:
                        quote = await self.markets[venue].quote(candidate["symbol"])
                        plan, reason = build_plan(candidate, quote, self.sizing_budget(self.studies["ai"]), config.PAPER_FEE_RATE)
                        if not plan:
                            self.research.record_event(candidate["id"], stage="paper_ai", decision="watch", reason=reason)
                            continue
                        paper_entry(self.studies["ai"], candidate, plan)
                        self.research.record_event(candidate["id"], stage="paper_ai", decision="opened")
                    except (LedgerError, MarketError) as exc:
                        self.research.record_event(candidate["id"], stage="paper_ai", decision="skipped", reason=str(exc))
        except Exception:
            LOGGER.exception("Shadow study failed")

    async def monitor_loop(self):
        while True:
            try:
                await self.monitor_once()
            except Exception:
                LOGGER.exception("Paper position monitoring failed")
            await asyncio.sleep(MONITOR_INTERVAL)

    async def monitor_once(self):
        ledgers = {"paper": self.ledger, **self.studies}
        grouped = {}
        for label, ledger in ledgers.items():
            for position in ledger.open_positions():
                grouped.setdefault(position["exchange"], []).append((label, ledger, position))

        invalidated = []
        pending_by_venue = {}
        for token, (candidate, expires, message_id) in list(self.pending.items()):
            if time.monotonic() >= expires or self.paused:
                self.pending.pop(token, None)
                reason = "Поиск на паузе." if self.paused else "Срок сигнала истёк."
                self.research.record_event(token, stage="delivery", decision="expired", reason=reason)
                if message_id:
                    invalidated.append((message_id, reason))
            else:
                pending_by_venue.setdefault(candidate["exchange"], []).append((token, candidate, message_id))

        notifications = []
        for venue in set(grouped) | set(pending_by_venue):
            positions = grouped.get(venue, [])
            pending = pending_by_venue.get(venue, [])
            market = self.markets.get(venue)
            if not market:
                LOGGER.error("Unsupported exchange in open paper positions: %s", venue)
                continue
            symbols = {p["symbol"] for _, _, p in positions} | {c["symbol"] for _, c, _ in pending}
            try:
                quotes = await market.quotes(sorted(symbols))
            except MarketError as exc:
                self.health[venue] = ("сопровождение ждёт котировки", time.time())
                LOGGER.warning("Paper monitoring waits for %s quotes: %s", venue, exc)
                continue
            health = "котировки получены"
            for label, ledger, position in positions:
                quote = quotes.get(position["symbol"])
                if quote is None or not quote.is_fresh():
                    health = "сопровождение ждёт свежую котировку"
                    continue
                fillable = quote.bid_size >= position["qty"]
                if not fillable:
                    health = "исполнение ждёт ликвидность"
                fills = ledger.mark(
                    position["position_id"], quote.bid, max_age_seconds=7200,
                    trailing_fraction=0.01, fillable=fillable,
                )
                if label == "paper":
                    for fill in fills:
                        notifications.append(
                            f"Бумажный выход {fill['symbol']} · {fill['reason']}\n"
                            f"Цена {fill['price']:.8g} · Объём {fill['qty']:.8g} · P&L {fill['pnl']:+.2f} USDT"
                        )
            self.health[venue] = (health, time.time())
            for token, candidate, message_id in pending:
                quote = quotes.get(candidate["symbol"])
                if token not in self.pending or quote is None or not quote.is_fresh():
                    continue
                plan, reason = build_plan(candidate, quote, self.sizing_budget(self.ledger), config.PAPER_FEE_RATE)
                if plan is None:
                    self.pending.pop(token, None)
                    self.research.record_event(token, stage="delivery", decision="invalidated", reason=reason)
                    if message_id:
                        invalidated.append((message_id, f"{candidate['symbol']} · {reason}."))

        for message_id, reason in invalidated:
            await self.invalidate(message_id, reason)
        for notification in notifications:
            try:
                await self.notify(notification)
            except Exception:
                LOGGER.exception("Paper exit recorded; Telegram delivery failed")

    async def invalidate(self, message_id, reason):
        if self.application is None:
            return
        try:
            await self.application.bot.edit_message_text(chat_id=self.owner, message_id=message_id, text=reason)
        except TelegramError:
            LOGGER.info("Telegram card could not be updated: %s", message_id)

    async def paper_confirm(self, token):
        pending = self.pending.pop(token, None)
        if self.paused or not pending or pending[1] <= time.monotonic():
            return "Сигнал устарел или поиск на паузе."
        candidate = pending[0]
        try:
            quote = await self.markets[candidate["exchange"]].quote(candidate["symbol"])
            if self.paused or pending[1] <= time.monotonic():
                return "Сигнал устарел или поиск на паузе."
            plan, reason = build_plan(candidate, quote, self.sizing_budget(self.ledger), config.PAPER_FEE_RATE)
            if not plan:
                self.research.record_event(token, stage="confirmation", decision="rejected", reason=reason)
                return reason
            position = paper_entry(self.ledger, candidate, plan)
            self.research.record_event(token, stage="confirmation", decision="opened", payload=plan)
        except (MarketError, LedgerError) as exc:
            self.research.record_event(token, stage="confirmation", decision="rejected", reason=str(exc))
            return f"Бумажная позиция не открыта. {exc}"
        return f"Бумажная покупка {candidate['symbol']} по {plan['price']:.8g}. ID {position['position_id']}"

    def watch_text(self):
        if not self.watch:
            return "Список наблюдения пока пуст."
        lines = ["Список наблюдения · первые 12 идей"]
        for candidate, plan, reason in self.watch[:12]:
            if plan and not self.paused and time.time() - plan["quote_observed_at"] <= 5:
                status = f"вход {plan['entry_min']:.7g}–{plan['entry_max']:.7g}"
            else:
                status = reason or "ожидаю свежую котировку"
            lines.append(f"{candidate['exchange']} {candidate['symbol']} · {candidate['strategy']} · {status}")
        return "\n".join(lines)

    def report_text(self):
        now = time.time()
        report = self.ledger.report(now=now)
        pace = self.research.pace_report(now - 86400, now=now)
        paper = self.ledger.pace_report(now=now)
        median_delay = pace["median_delivery_delay_seconds"]
        delay_label = f"{median_delay:.1f} с" if median_delay is not None else "нет выборки"
        holding = paper["average_holding_seconds"]
        holding_label = f"{holding / 60:.1f} мин" if holding is not None else "нет выборки"
        outcomes = self.research.report()
        lines = [
            "Бумажный портфель",
            f"Капитал {report['equity']:.2f} USDT · Чистый P&L {report['net_pnl']:+.2f} USDT",
            f"Свободно {report['cash']:.2f} · Открытый риск {report['open_risk']:.2f} USDT",
            f"Открыто {report['open_count']} · Закрыто {report['closed_count']} · Просадка {report['max_drawdown_pct']:.1f}%",
            f"Кандидатов в журнале {outcomes['candidates']} · Оценки ИИ: {outcomes['reviews_by_decision']}",
            (f"За 24 ч · кандидаты {pace['candidates']} · входы по котировке {pace['tradable']} · "
             f"карточки {pace['delivered']} · подтверждения {pace['confirmed']}"),
            (f"Медиана от подходящей котировки до карточки: {delay_label} "
             f"({pace['delivery_delay_samples']} наблюдений)"),
            (f"Закрытые за 24 ч: {paper['realized_net_pnl']:+.2f} USDT после комиссий · "
             f"прибыльных {paper['wins']} · убыточных {paper['losses']} · среднее время {holding_label}"),
        ]
        if report["legacy_error"]:
            lines.append("Старый журнал сохранён как архив, но его структуру прочитать не удалось.")
        if report["legacy_open_count"]:
            lines.append(f"Старых незакрытых записей в архиве: {report['legacy_open_count']}. Проверьте фактические сделки вручную.")
        for arm, ledger in self.studies.items():
            row = ledger.report(now=now)
            period = ledger.pace_report(now=now)
            lines.append(
                f"{arm}: за 24 ч {period['realized_net_pnl']:+.2f} USDT · "
                f"закрыто {period['completed']}, из них убыточных {period['losses']} · "
                f"всего {row['net_pnl']:+.2f} USDT · просадка {row['max_drawdown_pct']:.1f}%"
            )
        lines.append(f"ИИ-ревью: {'теневой режим' if self.review_enabled else 'выключено'}.")
        lines.append("Экспериментальные портфели. Данных для вывода о прибыли может быть недостаточно.")
        return "\n".join(lines)


def private_owner(update, app):
    user, chat = update.effective_user, update.effective_chat
    return bool(user and chat and user.id == app.owner and chat.type == ChatType.PRIVATE)


async def commands(update: Update, ctx):
    assistant = ctx.application.bot_data["assistant"]
    if not private_owner(update, assistant):
        return
    message = update.effective_message
    if message is None or not message.text:
        return
    command = message.text.split()[0].split("@")[0].lower()
    try:
        if command == "/setup":
            if len(ctx.args) != 2:
                raise ValueError("Формат /setup КАПИТАЛ ДНЕВНОЙ_БЮДЖЕТ")
            capital, budget = map(float, ctx.args)
            for ledger in [assistant.ledger, *assistant.studies.values()]:
                previous = ledger.report()["capital"]
                if previous and previous != capital:
                    raise ValueError(f"Начальный капитал этого эксперимента закреплён: {previous:g} USDT.")
            assistant.ledger.configure(capital, budget)
            for ledger in assistant.studies.values():
                ledger.configure(capital, budget)
            text = "Бумажный капитал и дневной бюджет сохранены."
        elif command == "/resume":
            if not assistant.ledger.report()["daily_budget"]:
                raise ValueError("Сначала настройте /setup КАПИТАЛ ДНЕВНОЙ_БЮДЖЕТ.")
            assistant.paused = False
            text = "Поиск запущен."
        elif command == "/stop":
            assistant.paused = True
            text = "Новые сигналы на паузе. Сопровождение открытых бумажных позиций работает."
        elif command == "/watch":
            text = assistant.watch_text()
        elif command == "/report":
            text = assistant.report_text()
        elif command == "/positions":
            lines = [f"{p['position_id']} · {p['exchange']} · {p['symbol']} · {p['qty']:.8g}"
                     for p in assistant.ledger.open_positions()]
            text = "\n".join(lines) or "Открытых бумажных позиций нет."
        elif command == "/close_paper":
            if len(ctx.args) != 1:
                raise ValueError("Формат /close_paper ID")
            position = next((p for p in assistant.ledger.open_positions() if p["position_id"] == ctx.args[0]), None)
            if not position:
                raise ValueError("Открытая бумажная позиция с этим ID отсутствует.")
            quote = await assistant.markets[position["exchange"]].quote(position["symbol"])
            if not quote.is_fresh() or quote.bid_size < position["qty"]:
                raise ValueError("Ожидаю свежую котировку с достаточным объёмом.")
            results = assistant.ledger.close(position["position_id"], quote.bid)
            text = f"Бумажная позиция закрыта. P&L {sum(fill['pnl'] for fill in results):+.2f} USDT."
        elif command == "/manual_open":
            if len(ctx.args) != 5:
                raise ValueError("Формат /manual_open БИРЖА ПАРА ОБЪЁМ ЦЕНА КОМИССИЯ_USDT")
            venue, symbol, quantity, price, fee = ctx.args
            if not symbol.upper().endswith("/USDT"):
                raise ValueError("Ручной журнал принимает сделки с расчётом в USDT.")
            result = assistant.ledger.record_manual_open(
                str(update.update_id), venue.lower(), symbol.upper(), float(quantity), float(price), float(fee),
            )
            text = f"Фактическая ручная покупка записана. ID {result['position_id']}."
        elif command == "/manual_close":
            if len(ctx.args) != 4:
                raise ValueError("Формат /manual_close ID ОБЪЁМ ЦЕНА КОМИССИЯ_USDT")
            position_id, quantity, price, fee = ctx.args
            assistant.ledger.record_manual_close(str(update.update_id), position_id, float(quantity), float(price), float(fee))
            text = "Фактическое ручное исполнение записано."
        elif command == "/manual":
            lines = [f"{p['position_id']} · {p['exchange']} · {p['symbol']} · {p['qty']:.8g}"
                     for p in assistant.ledger.manual_positions()]
            manual = assistant.ledger.manual_report()
            text = (f"Ручной журнал · закрыто {manual['closed_count']} · "
                    f"P&L {manual['realized_pnl']:+.2f} USDT\n"
                    + ("\n".join(lines) or "Открытых записей нет."))
        else:
            venue_lines = [
                f"{venue}: {assistant.scan_health.get(venue, ('нет данных сканера', 0))[0]} · "
                f"{assistant.health.get(venue, ('нет отметки сопровождения', 0))[0]}"
                for venue in assistant.exchanges
            ]
            text = ("Пятница 2 · бумажные сигналы\n"
                    + ("Поиск на паузе" if assistant.paused else "Поиск активен")
                    + "\n" + "\n".join(venue_lines)
                    + "\n/setup, /watch, /report, /positions, /close_paper, /manual, /manual_open, /manual_close")
    except (ValueError, LedgerError, MarketError) as exc:
        text = str(exc)
    await message.reply_text(text[:4000], reply_markup=menu(assistant.paused))


async def buttons(update: Update, ctx):
    assistant = ctx.application.bot_data["assistant"]
    query = update.callback_query
    if query is None:
        return
    if not private_owner(update, assistant):
        await query.answer()
        return
    await query.answer()
    action, _, token = (query.data or "").partition(":")
    if action == "paper":
        await query.edit_message_text(await assistant.paper_confirm(token))
        return
    if action == "skip":
        assistant.pending.pop(token, None)
        await query.edit_message_text("Сигнал пропущен.")
        return
    if action == "pause":
        assistant.paused = True
        text = "Новые сигналы на паузе. Открытые бумажные позиции сопровождаются."
    elif action == "resume":
        text = "Сначала настройте /setup КАПИТАЛ ДНЕВНОЙ_БЮДЖЕТ."
        if assistant.ledger.report()["daily_budget"]:
            assistant.paused = False
            text = "Поиск запущен."
    elif action == "watch":
        text = assistant.watch_text()
    elif action == "report":
        text = assistant.report_text()
    else:
        return
    await query.edit_message_text(text[:4000], reply_markup=menu(assistant.paused))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.BOT_OWNER_ID.isdecimal() or int(config.BOT_OWNER_ID) <= 0 or not config.TELEGRAM_TOKEN:
        raise SystemExit("Настройте BOT_OWNER_ID и TELEGRAM_TOKEN.")
    if not config.SIGNAL_EXCHANGES or any(venue not in SUPPORTED_EXCHANGES for venue in config.SIGNAL_EXCHANGES):
        raise SystemExit("SIGNAL_EXCHANGES: укажите binance и/или okx.")
    if not math.isfinite(config.PAPER_FEE_RATE) or not 0 <= config.PAPER_FEE_RATE < 0.1:
        raise SystemExit("PAPER_FEE_RATE должна быть конечной долей комиссии за сторону.")
    assistant = TradingAssistant(int(config.BOT_OWNER_ID), config.PAPER_DB, tuple(dict.fromkeys(config.SIGNAL_EXCHANGES)))
    app = (Application.builder().token(config.TELEGRAM_TOKEN)
           .post_init(assistant.start).post_shutdown(assistant.stop).build())
    app.bot_data["assistant"] = assistant
    app.add_handler(CommandHandler(
        ["start", "status", "setup", "resume", "stop", "watch", "report", "positions",
         "close_paper", "manual", "manual_open", "manual_close"], commands,
    ))
    app.add_handler(CallbackQueryHandler(buttons))
    app.run_polling()


if __name__ == "__main__":
    main()
