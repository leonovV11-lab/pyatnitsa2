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
        "exchange": "bybit",
        "waiting_tf_confirm": False,
        "current_offer": None,
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

# ---------- ХЕЛПЕРЫ ----------
def kb(rows):
    return InlineKeyboardMarkup(rows)

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)

def nav_btns(back_data):
    return [
        [btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]
    ]

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
        f15 = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
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
        "Сколько у тебя сейчас на балансе? (USDT)",
        reply_markup=kb(nav_btns("start"))
    )

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = "Понял, баланс " + str(STATE["balance"]) + " USDT.\nСколько готов потратить сегодня? (USDT)"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_start")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "city"
    msg = "Понял, работаем с " + str(STATE["daily_budget"]) + " USDT.\nВ каком ты городе?"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))

async def auto_after_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "check_time"
    local = local_now().strftime("%H:%M")
    msg = (
        "Понял, " + STATE["city"] + ".\n"
        "Сколько сейчас у тебя времени по часам? (например " + local + ")"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))

async def auto_after_check_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = "Подтверждено! Ты находишься в " + STATE["city"] + ".\nСколько ты будешь работать? (например '2 часа' или '2 дня')"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))

async def auto_ask_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for key, name in exchanges.EXCHANGES.items():
        rows.append([btn(name, "auto_ex_set_" + key)])
    rows.append(nav_btns("auto_retry_check_time")[0])
    msg = "Выбери биржу для торговли:"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    await update.callback_query.edit_message_text(
        "Биржа: " + exchanges.EXCHANGES.get(key, key) + ".\nИщем оптимальный вариант...",
        reply_markup=kb(nav_btns("start"))
    )
    if STATE["auto"]["task"] is None:
        STATE["auto"]["active"] = True
        STATE["auto"]["task"] = asyncio.create_task(_auto_loop(ctx.application))

async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("1m", "auto_tf_1m"), btn("15m", "auto_tf_15m"), btn("4h", "auto_tf_4h")],
        [btn("✏️ Своё время", "auto_tf_custom")],
        nav_btns("start")[0]
    ]
    await update.callback_query.edit_message_text(
        "Выбери ТФ:", reply_markup=kb(rows)
    )

async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    tf = data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text(
            "Впиши ТФ (например 5m, 30m, 1h):",
            reply_markup=kb(nav_btns("tf_bad"))
        )
        return
    STATE["auto"]["tf"] = tf
    STATE["auto"]["waiting_tf_confirm"] = False
    msg = "Ок, работаем " + tf + ". Ждём сигналов."
    await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))
    
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
        STATE["auto"]["current_offer"] = None
        await _ask_hascoins(update, ctx)

async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUY"
    await update.callback_query.edit_message_text(
        "Понял. Ждём только BUY.", reply_markup=kb(nav_btns("start"))
    )

async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUYSELL"
    await update.callback_query.edit_message_text(
        "Понял. Ждём BUY (докупить) или SELL (не потерять).", reply_markup=kb(nav_btns("start"))
    )

async def multi_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "multi_count"
    await update.callback_query.edit_message_text(
        "Сколько монет торгуем? (2–10)", reply_markup=kb(nav_btns("start"))
    )

async def _score_pair(symbol):
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
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
        STATE["auto"]["exchange"], quote="USDT", max_pairs=200
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
                    STATE["auto"]["current_offer"] = best["symbol"]
                    STATE["auto"]["waiting_tf_confirm"] = True
                    await _offer_found(app, best)
            else:
                if STATE["auto"]["waiting_tf_confirm"]:
                    pass
                else:
                    for sym in list(symbols):
                        await _process_symbol(app, sym)
        except Exception as e:
            print("Ошибка цикла:", e)
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
        "Рекомендуемый лот: " + str(round(lot, 2)) + " USDT\n"
        "Стоп: " + fmt_price(sig.stop_loss) + "\n"
        "Тейк: " + fmt_price(sig.take_profit)
    )
    if sig.action == "BUY":
        rows = [
            [btn("✅ Купил", "sig_bought_" + symbol.replace("/", "_") + "_" + str(sig.price))],
            [btn("❌ Пропустил", "sig_skipped")],
            nav_btns("start")[0]
        ]
    else:
        rows = [
            [btn("✅ Продал", "sig_sold_" + symbol.replace("/", "_") + "_" + str(sig.price))],
            [btn("❌ Пропустил", "sig_skipped")],
            nav_btns("start")[0]
        ]
    
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def _offer_found(app, best):
    text = (
        "Нашёл монету: " + best["symbol"] + "\n"
        "Потенциал: " + str(round(best["atr_pct"] * 100, 2)) + "% ATR\n"
        "Будем работать по времени: 15 минут. Устраивает?"
    )
    rows = [
        [btn("✅ Да, устраивает", "tf_ok_15m")],
        [btn("❌ Нет, не устраивает", "tf_bad")],
        nav_btns("start")[0]
    ]
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def tf_ok_15m(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["tf"] = "15m"
    STATE["auto"]["waiting_tf_confirm"] = False
    await update.callback_query.edit_message_text(
        "Ок, работаем 15m. Ждём сигналов.", reply_markup=kb(nav_btns("start"))
    )
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
        STATE["auto"]["current_offer"] = None
        await _ask_hascoins(update, ctx)

async def _ask_hascoins(update, ctx):
    rows = [
        [btn("Нет, монеты нет", "hascoins_no")],
        [btn("Да, есть", "hascoins_yes")],
        nav_btns("start")[0]
    ]
    text = "У тебя уже есть эта монета?"
    if update.message:
        await update.message.reply_text(text, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

# ---------- ОБРАБОТКА СИГНАЛОВ (КУПИЛ/ПРОДАЛ/ПРОПУСТИЛ) ----------
async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Ок, пропустил. Ждём следующий сигнал.", reply_markup=kb(nav_btns("start"))
    )

async def sig_bought_ask_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("sig_bought_", "")
    parts = raw.rsplit("_", 1)
    symbol = parts[0].replace("_", "/")
    price = float(parts[1])
    
    STATE["pending"]["signal"] = {
        "action": "BUY",
        "symbol": symbol,
        "price": price,
    }
    await update.callback_query.edit_message_text(
        "Сколько USDT ты купил по " + symbol + "?",
        reply_markup=kb(nav_btns("start"))
    )

async def sig_sold_ask_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("sig_sold_", "")
    parts = raw.rsplit("_", 1)
    symbol = parts[0].replace("_", "/")
    price = float(parts[1])
    
    STATE["pending"]["signal"] = {
        "action": "SELL",
        "symbol": symbol,
        "price": price,
    }
    await update.callback_query.edit_message_text(
        "Сколько USDT ты продал по " + symbol + "?",
        reply_markup=kb(nav_btns("start"))
    )

# ---------- ПАНИКА ----------
async def _panic_loop(app, symbol):
    STATE["panic"]["active"] = True
    while STATE["panic"]["active"]:
        try:
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
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
                        chat_id = app.bot_data.get("chat_id", 0)
                        if chat_id:
                            await app.bot.send_message(
                                chat_id=chat_id,
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
    await update.callback_query.edit_message_text("Позиция закрыта.", reply_markup=kb(nav_btns("start")))

# ---------- РУЧНОЙ РЕЖИМ ----------
async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🏦 " + exchanges.EXCHANGES.get(STATE["manual"]["exchange"], "биржа"), "man_ex")],
        [btn("📈 Пары", "man_pairs")],
        [btn("🔍 Поиск пары", "man_search")],
        nav_btns("start")[0]
    ]
    await update.callback_query.edit_message_text(
        "Ручной режим. Выбери действие:", reply_markup=kb(rows)
    )

async def man_ex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for key, name in exchanges.EXCHANGES.items():
        rows.append([btn(name, "man_ex_set_" + key)])
    rows.append(nav_btns("man_menu")[0])
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
        await update.callback_query.edit_message_text("Загружаю пары...")
        try:
            STATE["manual"]["pairs"] = exchanges.load_all_pairs(
                STATE["manual"]["exchange"], quote="USDT", max_pairs=500
            )
        except Exception as e:
            await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_menu")))
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
        "Впиши тикер (например BTC):", reply_markup=kb(nav_btns("man_menu"))
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
        nav_btns("man_pairs")[0]
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
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back")))
        return
    if sig.confidence < CONF_CHANCE:
        text = (
            "📊 " + symbol + "\n"
            "Уверенность низкая: " + str(int(sig.confidence * 100)) + "%\n"
            "Сигнала нет. Ждём."
        )
        await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))
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
                reply_markup=kb(nav_btns("man_pick_back"))
            )
        await update.callback_query.delete_message()
    except Exception:
        await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))

async def man_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back")))
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
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back")))
        return
    if sig.confidence < CONF_SIGNAL:
        text = (
            "⚡ " + symbol + "\n"
            "Уверенность " + str(int(sig.confidence * 100)) + "% < 85%.\n"
            "Сигнала нет."
        )
        await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))
        return
    rows = [
        [btn("🟢 Сейчас покупать", "man_buy")],
        [btn("🔴 Сейчас продавать", "man_sell")],
        nav_btns("man_pick_back")[0]
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
        nav_btns("man_pairs")[0]
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
            "\nЛот: " + str(round(lot, 2)) + " USDT", reply_markup=kb(nav_btns("start"))
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет открытой позиции.", reply_markup=kb(nav_btns("start")))
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        df = f.fetch("1m", 30)
        price = float(df.iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start")))
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыл " + pos["symbol"] + "\n"
            "PnL: " + str(round(trade["pnl_pct"], 2)) + "%", reply_markup=kb(nav_btns("start"))
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

# ---------- ЗАКРЫТИЕ ВСЕХ ----------
async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет открытых сделок.", reply_markup=kb(nav_btns("start")))
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        df = f.fetch("1m", 30)
        price = float(df.iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start")))
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыто: " + pos["symbol"] + "\n"
            "PnL: " + str(round(trade["pnl_pct"], 2)) + "%", reply_markup=kb(nav_btns("start"))
        )
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

# ---------- ИСТОРИЯ ----------
async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("📅 За день", "hist_day")],
        [btn("📆 За неделю", "hist_week")],
        [btn("🗓 За всё время", "hist_all")],
        nav_btns("start")[0]
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
        nav_btns("hist_menu")[0]
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
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = portfolio.stats_filtered("all", "all")
    text = (
        "📊 Общая статистика\n"
        "Сделок: " + str(s["total"]) + "\n"
        "Winrate: " + str(round(s["winrate"], 1)) + "%\n"
        "PnL USDT: " + str(round(s["total_abs"], 2)) + "\n"
        "Profit Factor: " + str(round(s["pf"], 2))
    )
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Демо-режим (старая функция).", reply_markup=kb(nav_btns("start"))
    )

# ---------- ТЕКСТОВЫЙ ВВОД ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]
    
    if "signal" in pend:
        sig_data = pend["signal"]
        try:
            amount = float(txt)
        except Exception:
            await update.message.reply_text("Введи число (USDT).", reply_markup=kb(nav_btns("start")))
            return
        pend.pop("signal")
        
        if sig_data["action"] == "BUY":
            qty = amount / sig_data["price"] if sig_data["price"] else 0
            portfolio.open_position(sig_data["symbol"], "BUY", sig_data["price"], qty, source="signal")
            rows = [
                [btn("🏠 В меню", "start")]
            ]
            await update.message.reply_text(
                "Записал: купил " + sig_data["symbol"] + " на " + str(amount) + " USDT.\nЖдём сигнал SELL.",
                reply_markup=kb(rows)
            )
        else:
            pos = portfolio.load().get("open")
            if not pos:
                await update.message.reply_text("Нет открытой позиции для продажи.", reply_markup=kb(nav_btns("start")))
                return
            qty = amount / sig_data["price"] if sig_data["price"] else 0
            if qty > pos["qty"]:
                qty = pos["qty"]
            trade = portfolio.close_position(sig_data["price"], qty)
            if "error" in trade:
                await update.message.reply_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start")))
                return
            if trade["pnl_pct"] < 0:
                register_loss()
            else:
                register_win()
            
            rows = [
                [btn("🏠 В меню", "start")]
            ]
            msg = (
                "Записал продажу.\n"
                "PnL: " + str(round(trade["pnl_pct"], 2)) + "%\n"
                "Прибыль: " + str(round(trade["pnl_abs"], 2)) + " USDT"
            )
            await update.message.reply_text(msg, reply_markup=kb(rows))
        return

    if "auto" in pend:
        kind = pend["auto"]
        if kind == "balance":
            try:
                STATE["balance"] = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.", reply_markup=kb(nav_btns("start")))
                return
            pend.pop("auto")
            await auto_after_balance(update, ctx)
            return
        if kind == "budget":
            try:
                STATE["daily_budget"] = float(txt)
            except Exception:
                await update.message.reply_text("Число нужно.", reply_markup=kb(nav_btns("auto_start")))
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
        if kind == "check_time":
            pend.pop("auto")
            await auto_after_check_time(update, ctx)
            return
        if kind == "work_time":
            hours = _parse_hours(txt)
            pend["auto_hours"] = str(hours)
            pend["multi_day"] = ("д" in txt.lower() or "day" in txt.lower())
            pend.pop("auto")
            await auto_ask_exchange(update, ctx)
            return
        if kind == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf):
                await update.message.reply_text("Формат: 5m, 1h, 4h.")
                return
            STATE["auto"]["tf"] = tf
            STATE["auto"]["waiting_tf_confirm"] = False
            pend.pop("auto")
            await update.message.reply_text("Ок, работаем " + tf + ".", reply_markup=kb(nav_btns("start")))
            if STATE["auto"]["current_offer"]:
                STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
                STATE["auto"]["current_offer"] = None
                await _ask_hascoins(update, ctx)
            return
        if kind == "multi_count":
            try:
                n = int(txt)
            except Exception:
                await update.message.reply_text("Число от 2 до 10.", reply_markup=kb(nav_btns("start")))
                return
            if n < 2 or n > MAX_MULTI:
                await update.message.reply_text("Только 2–10.", reply_markup=kb(nav_btns("start")))
                return
            pend.pop("auto")
            rows = [
                [btn("✅ Запустить " + str(n) + " монет", "multi_start_" + str(n))],
                nav_btns("start")[0]
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
                await update.message.reply_text("Не нашёл: " + ticker, reply_markup=kb(nav_btns("man_menu")))
                return
            idx = STATE["manual"]["pairs"].index(found)
            STATE["manual"]["page"] = idx // 50
            await update.message.reply_text(
                "Нашёл " + found + ", страница " + str(STATE["manual"]["page"] + 1),
                reply_markup=kb(nav_btns("man_menu"))
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

async def multi_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(update.callback_query.data.replace("multi_start_", ""))
    await update.callback_query.edit_message_text(
        "Сканирую топ-" + str(n) + " монет...", reply_markup=kb(nav_btns("start"))
    )
    bests = []
    pairs = exchanges.load_all_pairs(
        STATE["auto"]["exchange"], quote="USDT", max_pairs=150
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
        await update.callback_query.edit_message_text("Ничего не нашёл.", reply_markup=kb(nav_btns("start")))
        return
    STATE["auto"]["symbols"] = [t["symbol"] for t in top]
    lines = ["🤖 Взял " + str(len(top)) + " монет:"]
    for t in top:
        lines.append("• " + t["symbol"] + " (" + str(round(t["atr_pct"] * 100, 1)) + "%)")
    await update.callback_query.edit_message_text("\n".join(lines), reply_markup=kb(nav_btns("start")))

# ---------- РОУТЕР ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    if data == "start":
        await start(update, ctx)
        return
    if data == "auto_start":
        await auto_start(update, ctx)
        return
    if data == "auto_retry_balance":
        await auto_start(update, ctx)
        return
    if data == "auto_retry_budget":
        STATE["pending"]["auto"] = "budget"
        await q.edit_message_text("Сколько готов потратить сегодня? (USDT)", reply_markup=kb(nav_btns("auto_start")))
        return
    if data == "auto_retry_city":
        STATE["pending"]["auto"] = "city"
        await q.edit_message_text("В каком ты городе?", reply_markup=kb(nav_btns("auto_retry_budget")))
        return
    if data == "auto_retry_check_time":
        STATE["pending"]["auto"] = "check_time"
        local = local_now().strftime("%H:%M")
        await q.edit_message_text("Сколько сейчас у тебя времени по часам? (например " + local + ")", reply_markup=kb(nav_btns("auto_retry_city")))
        return
    
    if data.startswith("auto_ex_set_"):
        await auto_ex_set(update, ctx)
        return
    if data == "tf_ok_15m":
        await tf_ok_15m(update, ctx)
        return
    if data == "tf_bad":
        await tf_bad(update, ctx)
        return
    if data.startswith("auto_tf_"):
        await auto_tf_set(update, ctx)
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
    
    if data == "sig_skipped":
        await sig_skipped(update, ctx)
        return
    if data.startswith("sig_bought_"):
        await sig_bought_ask_amount(update, ctx)
        return
    if data.startswith("sig_sold_"):
        await sig_sold_ask_amount(update, ctx)
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