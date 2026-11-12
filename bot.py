# language: Python 3.10+, file: bot.py
import asyncio
import datetime
import json
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import exchanges
import portfolio
from friday import Friday, Mode, MODE_CONFIG, is_good_time, fmt_price
from chart import build_chart
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# ---------- КОНСТАНТЫ ПРИБЫЛИ ----------
CONF_SIGNAL = 0.85
CONF_CHANCE = 0.75
CONF_MIN_AUTO = 0.65
SWITCH_THRESHOLD = 0.05
PANIC_DROP = 0.02
PANIC_INTERVAL = 5
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_MULTI = 10
ANALYSIS_TF = "15m"
ANALYSIS_LIMIT = 384
FEE_MIN_TP = 3.0

# ---------- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ----------
STATE = {
    "balance": 0.0,
    "daily_budget": 0.0,
    "city": "",
    "tz": "UTC",
    "loss_streak": 0,
    "sleep_until": None,
    "auto": {
        "active": False,
        "task": None,
        "symbols": [],
        "allowed": "BUY",
        "tf": "15m",
        "mode": "focused",
        "last_switch_offer": None,
    },
    "manual": {
        "exchange": config.EXCHANGE,
        "pairs": [],
        "page": 0,
        "symbol": None,
        "tf": "15m",
    },
    "pending": {},
    "panic": {"task": None, "active": False},
}

TF_CHOICES = ["1m", "15m", "4h"]


# ---------- ХЕЛПЕРЫ ----------
def kb(rows):
    return InlineKeyboardMarkup(rows)


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def is_sleeping():
    t = STATE["sleep_until"]
    if not t:
        return False
    return datetime.datetime.utcnow() < t


def sleep_left_min():
    t = STATE["sleep_until"]
    if not t:
        return 0
    sec = (t - datetime.datetime.utcnow()).total_seconds()
    return max(0, int(sec / 60))


def register_loss():
    STATE["loss_streak"] += 1
    if STATE["loss_streak"] >= LOSS_LIMIT:
        STATE["sleep_until"] = (
            datetime.datetime.utcnow()
            + datetime.timedelta(minutes=SLEEP_MIN)
        )
        STATE["loss_streak"] = 0


def register_win():
    STATE["loss_streak"] = 0


def calc_lot(price, sl_price, confidence):
    if STATE["balance"] <= 0:
        return 0.0
    if confidence >= 0.85:
        risk_pct = 0.03
    elif confidence >= 0.75:
        risk_pct = 0.02
    else:
        risk_pct = 0.01
    risk_usdt = STATE["balance"] * risk_pct
    sl_pct = abs(price - sl_price) / price if price else 0
    if sl_pct <= 0:
        return 0.0
    lot_usdt = risk_usdt / sl_pct
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    return min(lot_usdt, budget)


def fee_ok(price, tp):
    move = abs(tp - price) / price if price else 0
    return move >= config.FEE_ROUND * FEE_MIN_TP


def mtf_confirm(symbol, direction):
    try:
        f15 = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        df15 = f15.fetch("15m", 120)
        df15 = f15.enrich(df15, "15m").dropna()
        if len(df15) < 2:
            return True
        last = df15.iloc[-1]
        if direction == "BUY" and last["ema_fast"] > last["ema_slow"]:
            return True
        if direction == "SELL" and last["ema_fast"] < last["ema_slow"]:
            return True
        return False
    except Exception:
        return True


def session_ok(multi_day):
    if not multi_day:
        return True
    hour = datetime.datetime.utcnow().hour
    return 12 <= hour < 20


def long_trade(multi_day):
    return multi_day


# ---------- ГЕОЛОКАЦИЯ ----------
def city_to_tz(city):
    try:
        geo = Nominatim(user_agent="friday_bot")
        loc = geo.geocode(city, timeout=10)
        if not loc:
            return "UTC"
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=loc.latitude, lng=loc.longitude)
        return tz_name or "UTC"
    except Exception:
        return "UTC"


def local_now():
    try:
        return datetime.datetime.now(ZoneInfo(STATE["tz"]))
    except Exception:
        return datetime.datetime.utcnow()


# ---------- ГЛАВНОЕ МЕНЮ ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🤖 Автопилот", "auto_start")],
        [btn("✋ Ручной режим", "man_menu")],
        [btn("🎮 Демо", "demo")],
        [btn("📜 История", "hist_menu")],
        [btn("📊 Статистика", "stats_menu")],
        [btn("❌ Закрыть сделку", "close_all")],
    ]
    txt = "Пятница на связи."
    if is_sleeping():
        txt = txt + "\n😴 Пауза, осталось " + str(sleep_left_min()) + " мин"
    if update.message:
        await update.message.reply_text(txt, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))


# ---------- АВТОПИЛОТ ----------
async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "balance"
    await update.callback_query.edit_message_text(
        "Сколько у тебя сейчас на балансе? (USDT)"
    )


async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    await update.message.reply_text(
        "Понял, баланс " + str(STATE["balance"]) + " USDT.\n"
        "Сколько готов потратить сегодня? (USDT)"
    )


async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "city"
    await update.message.reply_text(
        "Понял, работаем с " + str(STATE["daily_budget"]) + " USDT.\n"
        "В каком ты городе?"
    )


async def auto_after_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "time"
    await update.message.reply_text(
        "Понял, " + STATE["city"] + ".\n"
        "Сколько у тебя времени? (например '2 часа' или '2 дня')"
    )


async def auto_after_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("✅ Устраивает", "time_ok")],
        [btn("❌ Не устраивает", "time_bad")],
    ]
    local = local_now().strftime("%H:%M")
    await update.message.reply_text(
        "Сейчас в " + STATE["city"] + " " + local + ".\n"
        "Время работы: " + STATE["pending"].get("auto_hours", "?") + " ч.\n"
        "Устраивает?",
        reply_markup=kb(rows),
    )


async def time_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("⏱ В часах", "time_hours")],
        [btn("📅 В днях", "time_days")],
    ]
    await update.callback_query.edit_message_text(
        "Как задать время?", reply_markup=kb(rows)
    )


async def time_hours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "hours_input"
    await update.callback_query.edit_message_text(
        "Впиши часы (например 2 или 5):"
    )


async def time_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "days_input"
    await update.callback_query.edit_message_text(
        "Впиши дни (например 2 или 3), бот сам пересчитает:"
    )


async def time_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("✅ Торгуем 15 минут", "tf_ok_15m")],
        [btn("❌ Не устраивает ТФ", "tf_bad")],
    ]
    await update.callback_query.edit_message_text(
        "Начинаю скан рынка. Проверю монеты и предложу лучшую.\n"
        "После находки уточню ТФ.",
        reply_markup=kb(rows),
    )
    if STATE["auto"]["task"] is None:
        STATE["auto"]["active"] = True
        STATE["auto"]["task"] = asyncio.create_task(_auto_loop(ctx.application))


async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("1m", "auto_tf_1m")],
        [btn("15m", "auto_tf_15m")],
        [btn("4h", "auto_tf_4h")],
        [btn("✏️ Своё время", "auto_tf_custom")],
    ]
    await update.callback_query.edit_message_text(
        "Выбери ТФ:", reply_markup=kb(rows)
    )


async def tf_ok_15m(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["tf"] = "15m"
    await update.callback_query.edit_message_text(
        "Ок, работаем 15m. Ждём сигналов."
    )


async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    tf = data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text(
            "Впиши ТФ (например 5m, 30m, 1h):"
        )
        return
    STATE["auto"]["tf"] = tf
    await update.callback_query.edit_message_text(
        "Ок, работаем " + tf + ". Ждём сигналов."
    )


async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUY"
    await update.callback_query.edit_message_text(
        "Понял. Ждём только BUY."
    )


async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUYSELL"
    await update.callback_query.edit_message_text(
        "Понял. Ждём BUY (докупить) или SELL (не потерять)."
    )


async def multi_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "multi_count"
    await update.callback_query.edit_message_text(
        "Сколько монет торгуем? (2–10)"
    )


async def _score_pair(symbol):
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        df = f.fetch(ANALYSIS_TF, ANALYSIS_LIMIT)
        df = f.enrich(df, ANALYSIS_TF).dropna().reset_index(drop=True)
        if len(df) < 20:
            return None
        last = df.iloc[-1]
        price = float(last["close"])
        vol24 = float(df["volume"].tail(96).sum() * price)
        atr_pct = float(last["atr"]) / price if price else 0
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
        conf = sig.confidence
        if conf < CONF_MIN_AUTO:
            return None
        if not fee_ok(price, sig.take_profit):
            return None
        if not mtf_confirm(symbol, sig.action):
            return None
        score = vol24 * atr_pct * conf
        return {
            "symbol": symbol,
            "score": score,
            "signal": sig,
            "price": price,
            "atr_pct": atr_pct,
            "vol24": vol24,
        }
    except Exception:
        return None


async def _scan_best(exclude=None):
    pairs = exchanges.load_all_pairs(
        STATE["manual"]["exchange"], quote="USDT", max_pairs=200
    )
    best = None
    for sym in pairs:
        if exclude and sym in exclude:
            continue
        r = await _score_pair(sym)
        if r is None:
            continue
        if best is None or r["score"] > best["score"]:
            best = r
        await asyncio.sleep(0.05)
    return best


async def _auto_loop(app):
    while STATE["auto"]["active"]:
        if is_sleeping():
            await asyncio.sleep(60)
            continue
        if not session_ok(STATE["pending"].get("multi_day", False)):
            await asyncio.sleep(300)
            continue
        try:
            symbols = STATE["auto"]["symbols"]
            if not symbols:
                best = await _scan_best()
                if best:
                    STATE["auto"]["symbols"] = [best["symbol"]]
                    await _offer_found(app, best)
            else:
                for sym in list(symbols):
                    await _process_symbol(app, sym)
        except Exception:
            pass
        await asyncio.sleep(60)


async def _process_symbol(app, symbol):
    r = await _score_pair(symbol)
    if r is None:
        return
    sig = r["signal"]
    allowed = STATE["auto"]["allowed"]
    if sig.action == "BUY":
        pass
    elif sig.action == "SELL" and allowed == "BUYSELL":
        pass
    else:
        return
    lot = calc_lot(sig.price, sig.stop_loss, sig.confidence)
    if lot <= 0:
        return
    text = (
        "🎯 " + symbol + "\n"
        "Сигнал: " + sig.action + "\n"
        "Цена: " + fmt_price(sig.price) + "\n"
        "Уверенность: " + str(int(sig.confidence * 100)) + "%\n"
        "Лот: " + str(round(lot, 2)) + " USDT\n"
        "Стоп: " + fmt_price(sig.stop_loss) + "\n"
        "Тейк: " + fmt_price(sig.take_profit)
    )
    rows = [
        [btn("✅ Взял", "auto_took_" + symbol.replace("/", "_"))],
        [btn("⏭ Пропустить", "auto_skip")],
    ]
    await app.bot.send_message(
        chat_id=app.bot_data.get("chat_id", 0),
        text=text,
        reply_markup=kb(rows),
    )


async def _offer_found(app, best):
    rows = [
        [btn("Нет, монеты нет", "hascoins_no")],
        [btn("Да, есть", "hascoins_yes")],
    ]
    text = (
        "Нашёл монету: " + best["symbol"] + "\n"
        "Потенциал: " + str(round(best["atr_pct"] * 100, 2)) + "% ATR\n"
        "У тебя уже есть эта монета?"
    )
    await app.bot.send_message(
        chat_id=app.bot_data.get("chat_id", 0),
        text=text,
        reply_markup=kb(rows),
    )


# ---------- ПАНИКА ----------
async def _panic_loop(app, symbol):
    STATE["panic"]["active"] = True
    while STATE["panic"]["active"]:
        try:
            f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
            df = f.fetch("1m", 60)
            df = f.enrich(df, "1m").dropna().reset_index(drop=True)
            if len(df) < 5:
                await asyncio.sleep(PANIC_INTERVAL)
                continue
            last = df.iloc[-1]
            price = float(last["close"])
            pnl = portfolio.current_pnl(price)
            if pnl:
                peak = pnl.get("peak", pnl["entry"])
                drop = (peak - price) / peak if peak else 0
                if pnl["side"] == "BUY" and drop >= PANIC_DROP:
                    bearish = (
                        last["ema_fast"] < last["ema_slow"]
                        and last["macd_hist"] < 0
                        and last["close"] < last["bb_lower"]
                    )
                    if bearish:
                        rows = [[btn("🔴 ПРОДАЛ", "panic_sold")]]
                        await app.bot.send_message(
                            chat_id=app.bot_data.get("chat_id", 0),
                            text="🔴 ЗАКРЫВАЙ! ЗАКРЫВАЙ! ЗАКРЫВАЙ!",
                            reply_markup=kb(rows),
                        )
        except Exception:
            pass
        await asyncio.sleep(PANIC_INTERVAL)


async def panic_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["panic"]["active"] = False
    pos = portfolio.load().get("open")
    if pos:
        trade = portfolio.close_position(pos["entry"] * 0.98, pos["qty"])
        if "error" not in trade:
            if trade["pnl_pct"] < 0:
                register_loss()
            else:
                register_win()
    await update.callback_query.edit_message_text("Позиция закрыта.")


# ---------- РУЧНОЙ РЕЖИМ ----------
async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🏦 " + exchanges.EXCHANGES.get(STATE["manual"]["exchange"], "биржа"), "man_ex")],
        [btn("📈 Пары", "man_pairs")],
        [btn("🔍 Поиск пары", "man_search")],
    ]
    await update.callback_query.edit_message_text(
        "Ручной режим. Выбери действие:", reply_markup=kb(rows)
    )


async def man_ex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for key, name in exchanges.EXCHANGES.items():
        rows.append([btn(name, "man_ex_set_" + key)])
    rows.append([btn("⬅️ Назад", "man_menu")])
    await update.callback_query.edit_message_text(
        "Выбери биржу:", reply_markup=kb(rows)
    )


async def man_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("man_ex_set_", "")
    STATE["manual"]["exchange"] = key
    STATE["manual"]["pairs"] = []
    STATE["manual"]["page"] = 0
    await man_menu(update, ctx)


async def man_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE["manual"]["pairs"]:
        try:
            STATE["manual"]["pairs"] = exchanges.load_all_pairs(
                STATE["manual"]["exchange"], quote="USDT", max_pairs=500
            )
        except Exception as e:
            await update.callback_query.edit_message_text("Ошибка: " + str(e))
            return
    await _render_pairs(update, ctx)


async def _render_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pairs = STATE["manual"]["pairs"]
    page = STATE["manual"]["page"]
    start = page * 50
    chunk = pairs[start:start + 50]
    rows = []
    row = []
    for i, sym in enumerate(chunk):
        short = sym.replace("/USDT", "")
        row.append(btn(short, "man_pick_" + sym.replace("/", "_")))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(btn("⬅️", "man_page_" + str(page - 1)))
    nav.append(btn(str(page + 1), "noop"))
    if start + 50 < len(pairs):
        nav.append(btn("➡️", "man_page_" + str(page + 1)))
    rows.append(nav)
    rows.append([btn("🔍 Поиск", "man_search"), btn("⬅️ Меню", "man_menu")])
    await update.callback_query.edit_message_text(
        "Пары " + str(start + 1) + "-" + str(min(start + 50, len(pairs))) +
        " из " + str(len(pairs)),
        reply_markup=kb(rows),
    )


async def man_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    page = int(update.callback_query.data.replace("man_page_", ""))
    STATE["manual"]["page"] = page
    await _render_pairs(update, ctx)


async def man_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["manual"] = "search"
    await update.callback_query.edit_message_text(
        "Впиши тикер (например BTC):"
    )


async def man_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("man_pick_", "")
    symbol = raw.replace("_", "/")
    if not symbol.endswith("/USDT"):
        symbol = symbol + "/USDT"
    STATE["manual"]["symbol"] = symbol
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⏱ Time Frame", "man_tf")],
        [btn("⚡ Кнопки", "man_quick")],
        [btn("⬅️ Пары", "man_pairs")],
    ]
    await update.callback_query.edit_message_text(
        "Пара: " + symbol + "\nЧто смотрим?", reply_markup=kb(rows)
    )


async def man_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    await update.callback_query.edit_message_text("Анализирую " + symbol + "...")
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))
        return
    if sig.confidence < CONF_CHANCE:
        text = (
            "📊 " + symbol + "\n"
            "Уверенность низкая: " + str(int(sig.confidence * 100)) + "%\n"
            "Сигнала нет. Ждём."
        )
        await update.callback_query.edit_message_text(text)
        return
    tag = "СИГНАЛ" if sig.confidence >= CONF_SIGNAL else "ШАНС"
    text = (
        "📊 " + symbol + " · " + tag + "\n"
        "Действие: " + sig.action + "\n"
        "Цена: " + fmt_price(sig.price) + "\n"
        "Уверенность: " + str(int(sig.confidence * 100)) + "%\n"
        "Стоп: " + fmt_price(sig.stop_loss) + "\n"
        "Тейк: " + fmt_price(sig.take_profit) + "\n"
        "\nПричины:\n"
    )
    for r in sig.reasons[:6]:
        text = text + " • " + str(r) + "\n"
    try:
        path = build_chart(symbol, ANALYSIS_TF, sig)
        with open(path, "rb") as photo:
            await ctx.bot.send_photo(
                chat_id=update.callback_query.message.chat_id,
                photo=photo,
                caption=text,
            )
    except Exception:
        await update.callback_query.edit_message_text(text)


async def man_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))
        return
    direction = "флэт"
    if sig.action == "BUY":
        direction = "⬆️ вверх"
    elif sig.action == "SELL":
        direction = "⬇️ вниз"
    text = (
        "⏱ " + symbol + "\n"
        "Направление: " + direction + "\n"
        "ТФ: " + ANALYSIS_TF
    )
    await update.callback_query.edit_message_text(text)


async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))
        return
    if sig.confidence < CONF_SIGNAL:
        text = (
            "⚡ " + symbol + "\n"
            "Уверенность " + str(int(sig.confidence * 100)) + "% < 85%.\n"
            "Сигнала нет."
        )
        rows = [[btn("⬅️ Назад", "man_pick_back")]]
        await update.callback_query.edit_message_text(text, reply_markup=kb(rows))
        return
    rows = [
        [btn("🟢 Сейчас покупать", "man_buy")],
        [btn("🔴 Сейчас продавать", "man_sell")],
        [btn("⬅️ Назад", "man_pick_back")],
    ]
    text = (
        "⚡ " + symbol + "\n"
        "Сигнал: " + sig.action + "\n"
        "Уверенность: " + str(int(sig.confidence * 100)) + "%"
    )
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))


async def man_pick_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⏱ Time Frame", "man_tf")],
        [btn("⚡ Кнопки", "man_quick")],
        [btn("⬅️ Пары", "man_pairs")],
    ]
    await update.callback_query.edit_message_text(
        "Пара: " + str(symbol) + "\nЧто смотрим?", reply_markup=kb(rows)
    )


async def man_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
        lot = calc_lot(sig.price, sig.stop_loss, sig.confidence)
        qty = lot / sig.price if sig.price else 0
        portfolio.open_position(symbol, "BUY", sig.price, qty, source="manual")
        await update.callback_query.edit_message_text(
            "Открыл BUY " + symbol + "\nЦена: " + fmt_price(sig.price) +
            "\nЛот: " + str(round(lot, 2)) + " USDT"
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))


async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет открытой позиции.")
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        df = f.fetch("1m", 30)
        price = float(df.iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"])
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыл " + pos["symbol"] + "\n"
            "PnL: " + str(round(trade["pnl_pct"], 2)) + "%"
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))


# ---------- ЗАКРЫТИЕ ВСЕХ ----------
async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет открытых сделок.")
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        df = f.fetch("1m", 30)
        price = float(df.iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"])
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыто: " + pos["symbol"] + "\n"
            "PnL: " + str(round(trade["pnl_pct"], 2)) + "%"
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e))


# ---------- ИСТОРИЯ ----------
async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("📅 За день", "hist_day")],
        [btn("📆 За неделю", "hist_week")],
        [btn("🗓 За всё время", "hist_all")],
    ]
    await update.callback_query.edit_message_text(
        "Период:", reply_markup=kb(rows)
    )


async def hist_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = update.callback_query.data.replace("hist_", "")
    rows = [
        [btn("🎮 Только кнопки", "hist_src_" + period + "_manual")],
        [btn("📊 Только сигналы", "hist_src_" + period + "_signal")],
        [btn("🤖 Только автопилот", "hist_src_" + period + "_auto")],
        [btn("📋 Всё", "hist_src_" + period + "_all")],
    ]
    await update.callback_query.edit_message_text(
        "Источник:", reply_markup=kb(rows)
    )


async def hist_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("hist_src_", "")
    period, source = raw.split("_")
    s = portfolio.stats_filtered(period, source)
    text = (
        "📜 История (" + period + " / " + source + ")\n"
        "Сделок: " + str(s["total"]) + "\n"
        "Прибыльных: " + str(s["wins"]) + "\n"
        "Убыточных: " + str(s["losses"]) + "\n"
        "Winrate: " + str(round(s["winrate"], 1)) + "%\n"
        "PnL %: " + str(round(s["total_pnl"], 2)) + "\n"
        "PnL USDT: " + str(round(s["total_abs"], 2)) + "\n"
        "Profit Factor: " + str(round(s["pf"], 2))
    )
    rows = [[btn("⬅️ В меню", "start")]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))


async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = portfolio.stats_filtered("all", "all")
    text = (
        "📊 Общая статистика\n"
        "Сделок: " + str(s["total"]) + "\n"
        "Winrate: " + str(round(s["winrate"], 1)) + "%\n"
        "PnL USDT: " + str(round(s["total_abs"], 2)) + "\n"
        "Profit Factor: " + str(round(s["pf"], 2))
    )
    rows = [[btn("⬅️ В меню", "start")]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))


async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Демо-режим (старая функция)."
    )


# ---------- ТЕКСТОВЫЙ ВВОД ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]
    if "auto" in pend:
        kind = pend["auto"]
        if kind == "balance":
            try:
                STATE["balance"] = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.")
                return
            pend["auto"] = None
            pend.pop("auto")
            await auto_after_balance(update, ctx)
            return
        if kind == "budget":
            try:
                STATE["daily_budget"] = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.")
                return
            pend.pop("auto")
            await auto_after_budget(update, ctx)
            return
        if kind == "city":
            STATE["city"] = txt
            STATE["tz"] = city_to_tz(txt)
            pend.pop("auto")
            await auto_after_city(update, ctx)
            return
        if kind == "time":
            hours = _parse_hours(txt)
            pend["auto_hours"] = str(hours)
            pend.pop("auto")
            await auto_after_time(update, ctx)
            return
        if kind == "hours_input":
            try:
                h = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.")
                return
            pend["auto_hours"] = str(h)
            pend["multi_day"] = False
            pend.pop("auto")
            await _confirm_time(update, ctx, h, False)
            return
        if kind == "days_input":
            try:
                d = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.")
                return
            h = d * 24
            pend["auto_hours"] = str(h)
            pend["multi_day"] = d >= 2
            pend.pop("auto")
            await _confirm_time(update, ctx, h, d >= 2)
            return
        if kind == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf):
                await update.message.reply_text("Формат: 5m, 1h, 4h.")
                return
            STATE["auto"]["tf"] = tf
            pend.pop("auto")
            await update.message.reply_text("Ок, работаем " + tf + ".")
            return
        if kind == "multi_count":
            try:
                n = int(txt)
            except Exception:
                await update.message.reply_text("Число от 2 до 10.")
                return
            if n < 2 or n > MAX_MULTI:
                await update.message.reply_text("Только 2–10.")
                return
            pend.pop("auto")
            rows = [
                [btn("✅ Запустить " + str(n) + " монет", "multi_start_" + str(n))],
            ]
            await update.message.reply_text(
                "Ок, " + str(n) + " монет.", reply_markup=kb(rows)
            )
            return
    if "manual" in pend:
        kind = pend["manual"]
        if kind == "search":
            ticker = txt.upper()
            found = None
            for sym in STATE["manual"]["pairs"]:
                if sym.startswith(ticker + "/"):
                    found = sym
                    break
            pend.pop("manual")
            if not found:
                await update.message.reply_text("Не нашёл: " + ticker)
                return
            idx = STATE["manual"]["pairs"].index(found)
            STATE["manual"]["page"] = idx // 50
            await update.message.reply_text(
                "Нашёл " + found + ", страница " + str(STATE["manual"]["page"] + 1)
            )
            return


def _parse_hours(txt):
    low = txt.lower()
    m = re.search(r"(\d+(\.\d+)?)", low)
    if not m:
        return 1
    val = float(m.group(1))
    if "д" in low or "day" in low:
        return val * 24
    return val


async def _confirm_time(update, ctx, hours, multi_day):
    rows = [
        [btn("✅ Устраивает", "time_ok")],
        [btn("❌ Не устраивает", "time_bad")],
    ]
    msg = "Время: " + str(hours) + " ч."
    if multi_day:
        msg = msg + "\n(мульти-день: включу сессионный фильтр 12-20 UTC)"
    await update.message.reply_text(msg, reply_markup=kb(rows))


async def multi_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(update.callback_query.data.replace("multi_start_", ""))
    await update.callback_query.edit_message_text(
        "Сканирую топ-" + str(n) + " монет..."
    )
    bests = []
    pairs = exchanges.load_all_pairs(
        STATE["manual"]["exchange"], quote="USDT", max_pairs=150
    )
    for sym in pairs:
        r = await _score_pair(sym)
        if r:
            bests.append(r)
        if len(bests) >= n * 2:
            break
        await asyncio.sleep(0.05)
    bests.sort(key=lambda x: x["score"], reverse=True)
    top = bests[:n]
    if not top:
        await update.callback_query.edit_message_text("Ничего не нашёл.")
        return
    STATE["auto"]["symbols"] = [t["symbol"] for t in top]
    lines = ["🤖 Взял " + str(len(top)) + " монет:"]
    for t in top:
        lines.append("• " + t["symbol"] + " (" + str(round(t["atr_pct"] * 100, 1)) + "%)")
    await update.callback_query.edit_message_text("\n".join(lines))


# ---------- РОУТЕР ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "start":
        await start(update, ctx)
        return
    if data == "auto_start":
        await auto_start(update, ctx)
        return
    if data == "time_ok":
        await time_ok(update, ctx)
        return
    if data == "time_bad":
        await time_bad(update, ctx)
        return
    if data == "time_hours":
        await time_hours(update, ctx)
        return
    if data == "time_days":
        await time_days(update, ctx)
        return
    if data.startswith("auto_tf_"):
        await auto_tf_set(update, ctx)
        return
    if data == "tf_ok_15m":
        await tf_ok_15m(update, ctx)
        return
    if data == "tf_bad":
        await tf_bad(update, ctx)
        return
    if data == "hascoins_no":
        await hascoins_no(update, ctx)
        return
    if data == "hascoins_yes":
        await hascoins_yes(update, ctx)
        return
    if data == "multi_choose":
        await multi_choose(update, ctx)
        return
    if data.startswith("multi_start_"):
        await multi_start(update, ctx)
        return
    if data == "panic_sold":
        await panic_sold(update, ctx)
        return
    if data == "man_menu":
        await man_menu(update, ctx)
        return
    if data == "man_ex":
        await man_ex(update, ctx)
        return
    if data.startswith("man_ex_set_"):
        await man_ex_set(update, ctx)
        return
    if data == "man_pairs":
        await man_pairs(update, ctx)
        return
    if data.startswith("man_page_"):
        await man_page(update, ctx)
        return
    if data == "man_search":
        await man_search(update, ctx)
        return
    if data.startswith("man_pick_"):
        await man_pick(update, ctx)
        return
    if data == "man_signals":
        await man_signals(update, ctx)
        return
    if data == "man_tf":
        await man_tf(update, ctx)
        return
    if data == "man_quick":
        await man_quick(update, ctx)
        return
    if data == "man_pick_back":
        await man_pick_back(update, ctx)
        return
    if data == "man_buy":
        await man_buy(update, ctx)
        return
    if data == "man_sell":
        await man_sell(update, ctx)
        return
    if data == "close_all":
        await close_all(update, ctx)
        return
    if data == "hist_menu":
        await hist_menu(update, ctx)
        return
    if data in ("hist_day", "hist_week", "hist_all"):
        await hist_period(update, ctx)
        return
    if data.startswith("hist_src_"):
        await hist_show(update, ctx)
        return
    if data == "stats_menu":
        await stats_menu(update, ctx)
        return
    if data == "demo":
        await demo(update, ctx)
        return
    if data == "noop":
        return


# ---------- MAIN ----------
def main():
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()