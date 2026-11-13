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
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import config
import exchanges
import portfolio
from friday import Friday, Mode, MODE_CONFIG, is_good_time, fmt_price
from chart import build_chart
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# ---------- КОНСТАНТЫ ----------
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

# ---------- СОСТОЯНИЕ ----------
STATE = {
    "balance": 0.0,
    "daily_budget": 0.0,
    "city": "",
    "tz": "UTC",
    "loss_streak": 0,
    "sleep_until": None,
    "auto": {
        "active": False, "task": None, "symbols": [], "allowed": "BUY",
        "tf": "15m", "exchange": "bybit", "waiting_tf_confirm": False,
        "current_offer": None, "best_score": 0,
        "switch_wait_until": None, "switch_offer": None,
    },
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None, "tf": "15m"},
    "pending": {},
    "panic": {"task": None, "active": False},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"active": False, "task": None},
}

# ---------- ХЕЛПЕРЫ ----------
def kb(rows):
    return InlineKeyboardMarkup(rows)

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)

def nav_btns(back_data):
    return [[btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]]

def is_sleeping():
    t = STATE["sleep_until"]
    if not t:
        return False
    return datetime.datetime.utcnow() < t

def sleep_left_min():
    t = STATE["sleep_until"]
    if not t:
        return 0
    return max(0, int((t - datetime.datetime.utcnow()).total_seconds() / 60))

def register_loss():
    STATE["loss_streak"] += 1
    if STATE["loss_streak"] >= LOSS_LIMIT:
        STATE["sleep_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLEEP_MIN)
        STATE["loss_streak"] = 0

def register_win():
    STATE["loss_streak"] = 0

def calc_lot(price, sl_price, confidence):
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget <= 0:
        return 0.0
    risk_pct = 0.03 if confidence >= 0.85 else (0.02 if confidence >= 0.75 else 0.01)
    sl_pct = abs(price - sl_price) / price if price else 0
    if sl_pct <= 0:
        return 0.0
    return min((budget * risk_pct) / sl_pct, budget)

def mtf_confirm(symbol, direction):
    try:
        f15 = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
        df15 = f15.enrich(f15.fetch("15m", 120), "15m").dropna()
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
    return 12 <= datetime.datetime.utcnow().hour < 20

def city_to_tz(city):
    try:
        geo = Nominatim(user_agent="friday_bot")
        loc = geo.geocode(city, timeout=10)
        if not loc:
            return "UTC"
        tf = TimezoneFinder()
        return tf.timezone_at(lat=loc.latitude, lng=loc.longitude) or "UTC"
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
        txt += "\n😴 Пауза, осталось " + str(sleep_left_min()) + " мин"
    if update.message:
        await update.message.reply_text(txt, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = False
    STATE["trailing"]["active"] = False
    if STATE["auto"]["task"]:
        STATE["auto"]["task"].cancel()
        STATE["auto"]["task"] = None
    STATE["auto"]["symbols"] = []
    STATE["auto"]["current_offer"] = None
    STATE["auto"]["waiting_tf_confirm"] = False
    STATE["auto"]["switch_wait_until"] = None
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

# ---------- ДЕМО ----------
async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("🚀 Найти монету", "demo_scan")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("🎮 Демо-режим.\nНажми кнопку.", reply_markup=kb(rows))

async def demo_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Сканирую...")
    best = await _scan_best()
    if not best:
        await update.callback_query.edit_message_text("Не нашёл.", reply_markup=kb(nav_btns("demo")))
        return
    r = await _score_pair(best["symbol"])
    if not r:
        await update.callback_query.edit_message_text("Ошибка анализа.", reply_markup=kb(nav_btns("demo")))
        return
    sig = r["signal"]
    STATE["demo"]["symbol"] = best["symbol"]
    STATE["demo"]["buy_price"] = sig.price
    STATE["demo"]["action"] = sig.action
    text = ("🎮 ДЕМО\n\n" + best["symbol"] + "\nСигнал: " + sig.action + "\nЦена: " + fmt_price(sig.price) + "\nУверенность: " + str(int(sig.confidence * 100)) + "%")
    if sig.action == "BUY":
        rows = [[btn("✅ Купил", "demo_buy")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    else:
        rows = [[btn("✅ Продал", "demo_sell")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def demo_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок.", reply_markup=kb([[btn("🏠 В меню Демо", "demo")]]))

async def demo_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько купил?", reply_markup=kb(nav_btns("demo")))

async def demo_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько продал?", reply_markup=kb(nav_btns("demo")))

# ---------- АВТОПИЛОТ ----------
async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "balance"
    await update.callback_query.edit_message_text("Баланс (USDT)?", reply_markup=kb(nav_btns("start")))

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = "Баланс " + str(STATE["balance"]) + ".\nБюджет на день?"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_start")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "city"
    msg = "Бюджет " + str(STATE["daily_budget"]) + ".\nГород?"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))

async def auto_after_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "check_time"
    local = local_now().strftime("%H:%M")
    msg = "Город: " + STATE["city"] + ".\nВремя сейчас? (например " + local + ")"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))

async def auto_after_check_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = "Ок.\nСколько работаем? (например '2 часа')"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))

async def auto_ask_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for key, name in exchanges.EXCHANGES.items():
        rows.append([btn(name, "auto_ex_set_" + key)])
    rows.append(nav_btns("auto_retry_check_time")[0])
    msg = "Выбери биржу:"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    ctx.application.bot_data["chat_id"] = update.callback_query.message.chat_id
    await update.callback_query.edit_message_text("Биржа: " + exchanges.EXCHANGES.get(key, key) + ".\nПоиск...", reply_markup=kb(nav_btns("start")))
    if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
        STATE["auto"]["active"] = True
        STATE["auto"]["symbols"] = []
        STATE["auto"]["best_score"] = 0
        STATE["auto"]["waiting_tf_confirm"] = False
        STATE["auto"]["current_offer"] = None
        STATE["auto"]["switch_wait_until"] = None
        STATE["auto"]["task"] = asyncio.create_task(_auto_loop(ctx.application))

async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("1m", "auto_tf_1m"), btn("15m", "auto_tf_15m"), btn("4h", "auto_tf_4h")],
            [btn("✏️ Своё время", "auto_tf_custom")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("Выбери ТФ:", reply_markup=kb(rows))

async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    tf = data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text("Впиши ТФ:", reply_markup=kb(nav_btns("tf_bad")))
        return
    STATE["auto"]["tf"] = tf
    STATE["auto"]["waiting_tf_confirm"] = False
    await update.callback_query.edit_message_text("Ок, " + tf + ".", reply_markup=kb(nav_btns("start")))
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
        STATE["auto"]["current_offer"] = None
        await _ask_hascoins(update, ctx)

async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUY"
    await update.callback_query.edit_message_text("Ждём только BUY.", reply_markup=kb(nav_btns("start")))

async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["allowed"] = "BUYSELL"
    await update.callback_query.edit_message_text("Ждём BUY или SELL.", reply_markup=kb(nav_btns("start")))

async def multi_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "multi_count"
    await update.callback_query.edit_message_text("Сколько монет? (2–10)", reply_markup=kb(nav_btns("start")))
    async def _score_pair(symbol, relaxed=False):
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
        tf = STATE["auto"].get("tf", "15m")
        df = f.enrich(f.fetch(tf, 100), tf).dropna().reset_index(drop=True)
        if len(df) < 20:
            return None
        last = df.iloc[-1]
        price = float(last["close"])
        vol24 = float(df["volume"].tail(96).sum() * price)
        atr_pct = float(last["atr"]) / price if price else 0
        if vol24 < 100000 or atr_pct < 0.005:
            return None
        sig = f.analyze_filtered(tf, tp_mult=3.0)
        base_conf = sig.confidence
        if atr_pct > 0.025:
            base_conf = max(base_conf, 0.90)
        elif atr_pct > 0.02:
            base_conf = max(base_conf, 0.80)
        elif atr_pct > 0.015:
            base_conf = max(base_conf, 0.70)
        elif atr_pct > 0.01:
            base_conf = max(base_conf, 0.65)
        sig.confidence = base_conf
        return {"symbol": symbol, "score": vol24 * atr_pct, "signal": sig,
                "price": price, "atr_pct": atr_pct, "vol24": vol24}
    except Exception:
        return None

async def _scan_best(exclude=None):
    try:
        ex = getattr(exchanges, STATE["auto"]["exchange"])
        exchange = getattr(ex, "ccxt")({"enableRateLimit": True})
        markets = exchange.load_markets()
        valid = [s for s, m in markets.items()
                 if m.get("active") and m.get("spot") and m.get("quote") == "USDT"
                 and (not exclude or s not in exclude)]
        if not valid:
            return None
        top = valid[:40]
        best = None
        for i in range(0, len(top), 10):
            chunk = top[i:i + 10]
            results = await asyncio.gather(*[_score_pair(s) for s in chunk], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) or r is None:
                    continue
                if best is None or r["score"] > best["score"]:
                    best = r
            await asyncio.sleep(0.2)
        return best
    except Exception as e:
        print("Ошибка скана:", e)
        return None

async def _auto_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
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
                    STATE["auto"]["best_score"] = best["score"]
                    STATE["auto"]["waiting_tf_confirm"] = True
                    await _offer_found(app, best)
            elif not STATE["auto"]["waiting_tf_confirm"]:
                await _process_symbol(app, symbols[0])
        except Exception as e:
            print("Ошибка цикла:", e)
        await asyncio.sleep(60)

async def _process_symbol(app, symbol):
    if "signal" in STATE["pending"]:
        return
    r = await _score_pair(symbol)
    if r is None:
        return
    sig = r["signal"]
    if sig.confidence < 0.55:
        return
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
    net = (abs(sig.take_profit - sig.price) / sig.price - 0.002) * 100
    if net < 0.2:
        return
    STATE["pending"]["signal"] = {"action": sig.action, "symbol": symbol, "price": sig.price, "lot": lot}
    text = ("🎯 " + symbol + "\nСигнал: " + sig.action + "\nЦена: " + fmt_price(sig.price) +
            "\nУверенность: " + str(int(sig.confidence * 100)) + "%\nЛот: " + str(round(lot, 2)) +
            " USDT\nСтоп: " + fmt_price(sig.stop_loss) + "\nТейк: " + fmt_price(sig.take_profit) +
            "\nЧистая прибыль: ~" + str(round(net, 2)) + "%")
    if sig.action == "BUY":
        rows = [[btn("✅ Купил", "sig_buy_signal")], [btn("✏️ Своя сумма", "sig_buy_custom")],
                [btn("❌ Пропустил", "sig_skipped")], [btn("🛑 СТОП", "auto_stop_signals")],
                nav_btns("start")[0]]
    else:
        rows = [[btn("✅ Продал", "sig_sell_signal")], [btn("✏️ Своя сумма", "sig_sell_custom")],
                [btn("❌ Пропустил", "sig_skipped")], [btn("🛑 СТОП", "auto_stop_signals")],
                nav_btns("start")[0]]
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def _trailing_loop(app, symbol, entry_price, side):
    chat_id = app.bot_data.get("chat_id", 0)
    best_price = entry_price
    be = False
    while STATE["trailing"]["active"]:
        await asyncio.sleep(30)
        try:
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            price = float(f.fetch("1m", 10).iloc[-1]["close"])
            if side == "BUY":
                best_price = max(best_price, price)
                p = (best_price - entry_price) / entry_price
                if p >= 0.015 and not be:
                    be = True
                    if chat_id:
                        await app.bot.send_message(chat_id=chat_id,
                            text="📈 " + symbol + " +" + str(round(p * 100, 1)) + "%! Стоп в безубыток.")
                    break
        except Exception:
            pass

async def _offer_found(app, best):
    text = ("Нашёл: " + best["symbol"] + "\nПотенциал: " + str(round(best["atr_pct"] * 100, 2)) +
            "% ATR\n15 минут. Ок?")
    rows = [[btn("✅ Да", "tf_ok_15m")], [btn("❌ Нет", "tf_bad")], nav_btns("start")[0]]
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def _offer_better(app, best):
    text = ("🔥 Лучше на 5%: " + best["symbol"] + "\nПотенциал: " +
            str(round(best["atr_pct"] * 100, 2)) + "% ATR")
    rows = [[btn("✅ Да", "switch_yes")], [btn("❌ Нет", "switch_no")], nav_btns("start")[0]]
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def switch_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
        STATE["auto"]["current_offer"] = None
        STATE["auto"]["switch_wait_until"] = None
        STATE["auto"]["switch_offer"] = None
        await update.callback_query.edit_message_text("Переключился на " + STATE["auto"]["symbols"][0],
            reply_markup=kb(nav_btns("start")))

async def switch_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["switch_offer"] = {"symbol": STATE["auto"]["current_offer"]}
        STATE["auto"]["switch_wait_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
    STATE["auto"]["current_offer"] = None
    await update.callback_query.edit_message_text("Ок, вернусь через 30 мин.",
        reply_markup=kb(nav_btns("start")))

async def tf_ok_15m(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["tf"] = "15m"
    STATE["auto"]["waiting_tf_confirm"] = False
    chat_id = update.callback_query.message.chat_id
    await ctx.bot.send_message(chat_id=chat_id, text="Ок, 15m. Ждём.")
    await update.callback_query.delete_message()
    if STATE["auto"]["current_offer"]:
        STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
        STATE["auto"]["current_offer"] = None
        await _ask_hascoins(update, ctx)

async def _ask_hascoins(update, ctx):
    rows = [[btn("Нет", "hascoins_no")], [btn("Да", "hascoins_yes")], nav_btns("start")[0]]
    chat_id = update.callback_query.message.chat_id
    await ctx.bot.send_message(chat_id=chat_id, text="У тебя есть эта монета?", reply_markup=kb(rows))

async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "signal" in STATE["pending"]:
        del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Ок, пропустил.", reply_markup=kb(nav_btns("start")))

async def sig_buy_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True)
        return
    lot = sig["lot"]
    price = sig["price"]
    qty = lot / price if price else 0
    portfolio.open_position(sig["symbol"], "BUY", price, qty, source="signal")
    STATE["trailing"]["active"] = True
    if STATE["trailing"]["task"] is None or STATE["trailing"]["task"].done():
        STATE["trailing"]["task"] = asyncio.create_task(
            _trailing_loop(ctx.application, sig["symbol"], price, "BUY"))
    del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Купил на " + str(round(lot, 2)) + " USDT.",
        reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_buy_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        return
    STATE["pending"]["wait_custom_amount"] = {"action": "BUY", "symbol": sig["symbol"], "price": sig["price"]}
    await update.callback_query.edit_message_text("Сумма покупки (USDT):", reply_markup=kb(nav_btns("start")))

async def sig_sell_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True)
        return
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start")))
        return
    trade = portfolio.close_position(sig["price"], pos["qty"])
    STATE["trailing"]["active"] = False
    del STATE["pending"]["signal"]
    if "error" in trade:
        await update.callback_query.edit_message_text("Ошибка: " + trade["error"],
            reply_markup=kb(nav_btns("start")))
        return
    if trade["pnl_pct"] < 0:
        register_loss()
    else:
        register_win()
    msg = ("Продал.\nPnL: " + str(round(trade["pnl_pct"], 2)) + "% (" +
           str(round(trade["pnl_abs"], 2)) + " USDT)")
    await update.callback_query.edit_message_text(msg, reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_sell_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        return
    STATE["pending"]["wait_custom_amount"] = {"action": "SELL", "symbol": sig["symbol"], "price": sig["price"]}
    await update.callback_query.edit_message_text("Сумма продажи (USDT):", reply_markup=kb(nav_btns("start")))
    async def _panic_loop(app, symbol):
    STATE["panic"]["active"] = True
    while STATE["panic"]["active"]:
        try:
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.enrich(f.fetch("1m", 60), "1m").dropna().reset_index(drop=True)
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
                    bearish = (last["ema_fast"] < last["ema_slow"]
                               and last["macd_hist"] < 0
                               and last["close"] < last["bb_lower"])
                    if bearish:
                        chat_id = app.bot_data.get("chat_id", 0)
                        if chat_id:
                            await app.bot.send_message(chat_id=chat_id,
                                text="🔴 ЗАКРЫВАЙ!",
                                reply_markup=kb([[btn("🔴 ПРОДАЛ", "panic_sold")]]))
        except Exception:
            pass
        await asyncio.sleep(PANIC_INTERVAL)

async def panic_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["panic"]["active"] = False
    STATE["trailing"]["active"] = False
    pos = portfolio.load().get("open")
    if pos:
        trade = portfolio.close_position(pos["entry"] * 0.98, pos["qty"])
        if "error" not in trade:
            if trade["pnl_pct"] < 0:
                register_loss()
            else:
                register_win()
    await update.callback_query.edit_message_text("Закрыто.", reply_markup=kb(nav_btns("start")))

async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🏦 " + exchanges.EXCHANGES.get(STATE["manual"]["exchange"], "биржа"), "man_ex")],
        [btn("📈 Пары", "man_pairs")],
        [btn("🔍 Поиск пары", "man_search")],
        nav_btns("start")[0],
    ]
    await update.callback_query.edit_message_text("Ручной режим:", reply_markup=kb(rows))

async def man_ex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn(name, "man_ex_set_" + key)] for key, name in exchanges.EXCHANGES.items()]
    rows.append(nav_btns("man_menu")[0])
    await update.callback_query.edit_message_text("Биржа:", reply_markup=kb(rows))

async def man_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["exchange"] = update.callback_query.data.replace("man_ex_set_", "")
    STATE["manual"]["pairs"] = []
    STATE["manual"]["page"] = 0
    await man_menu(update, ctx)

async def man_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE["manual"]["pairs"]:
        await update.callback_query.edit_message_text("Загружаю...")
        try:
            STATE["manual"]["pairs"] = exchanges.load_all_pairs(
                STATE["manual"]["exchange"], quote="USDT", max_pairs=500)
        except Exception as e:
            await update.callback_query.edit_message_text("Ошибка: " + str(e),
                reply_markup=kb(nav_btns("man_menu")))
            return
    await _render_pairs(update, ctx)

async def _render_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pairs = STATE["manual"]["pairs"]
    page = STATE["manual"]["page"]
    start = page * 50
    chunk = pairs[start:start + 50]
    rows = []
    row = []
    for sym in chunk:
        row.append(btn(sym.replace("/USDT", ""), "man_pick_" + sym.replace("/", "_")))
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
    text = ("Пары " + str(start + 1) + "-" + str(min(start + 50, len(pairs))) +
            " из " + str(len(pairs)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["page"] = int(update.callback_query.data.replace("man_page_", ""))
    await _render_pairs(update, ctx)

async def man_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["manual"] = "search"
    await update.callback_query.edit_message_text("Впиши тикер:", reply_markup=kb(nav_btns("man_menu")))

async def man_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("man_pick_", "")
    symbol = raw.replace("_", "/")
    if not symbol.endswith("/USDT"):
        symbol += "/USDT"
    STATE["manual"]["symbol"] = symbol
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⏱ Time Frame", "man_tf")],
        [btn("⚡ Кнопки", "man_quick")],
        nav_btns("man_pairs")[0],
    ]
    await update.callback_query.edit_message_text("Пара: " + symbol, reply_markup=kb(rows))

async def man_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    await update.callback_query.edit_message_text("Анализирую " + symbol + "...")
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    if sig.confidence < CONF_CHANCE:
        await update.callback_query.edit_message_text(
            "Уверенность " + str(int(sig.confidence * 100)) + "%. Нет сигнала.",
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    tag = "СИГНАЛ" if sig.confidence >= CONF_SIGNAL else "ШАНС"
    text = ("📊 " + symbol + " · " + tag + "\nДействие: " + sig.action +
            "\nЦена: " + fmt_price(sig.price) +
            "\nУверенность: " + str(int(sig.confidence * 100)) + "%" +
            "\nСтоп: " + fmt_price(sig.stop_loss) +
            "\nТейк: " + fmt_price(sig.take_profit))
    try:
        path = build_chart(symbol, ANALYSIS_TF, sig)
        with open(path, "rb") as photo:
            await ctx.bot.send_photo(
                chat_id=update.callback_query.message.chat_id,
                photo=photo, caption=text,
                reply_markup=kb(nav_btns("man_pick_back")))
        await update.callback_query.delete_message()
    except Exception:
        await update.callback_query.edit_message_text(text,
            reply_markup=kb(nav_btns("man_pick_back")))

async def man_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    d = "⬆️ вверх" if sig.action == "BUY" else "⬇️ вниз"
    await update.callback_query.edit_message_text("⏱ " + symbol + "\n" + d,
        reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    rows = [
        [btn("🟢 Сейчас покупать", "man_buy")],
        [btn("🔴 Сейчас продавать", "man_sell")],
        nav_btns("man_pick_back")[0],
    ]
    text = "⚡ " + symbol + "\nКуда: " + sig.action + "\n" + str(int(sig.confidence * 100)) + "%"
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_pick_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⏱ Time Frame", "man_tf")],
        [btn("⚡ Кнопки", "man_quick")],
        nav_btns("man_pairs")[0],
    ]
    await update.callback_query.edit_message_text(
        "Пара: " + str(STATE["manual"]["symbol"]), reply_markup=kb(rows))

async def man_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(ANALYSIS_TF, tp_mult=3.0)
        lot = calc_lot(sig.price, sig.stop_loss, sig.confidence)
        qty = lot / sig.price if sig.price else 0
        portfolio.open_position(symbol, "BUY", sig.price, qty, source="manual")
        await update.callback_query.edit_message_text(
            "BUY " + symbol + " на " + str(round(lot, 2)) + " USDT",
            reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет позиции.",
            reply_markup=kb(nav_btns("start")))
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        price = float(f.fetch("1m", 30).iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"],
                reply_markup=kb(nav_btns("start")))
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыл " + pos["symbol"] + " PnL: " + str(round(trade["pnl_pct"], 2)) + "%",
            reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("start")))

async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = portfolio.load().get("open")
    if not pos:
        await update.callback_query.edit_message_text("Нет сделок.",
            reply_markup=kb(nav_btns("start")))
        return
    try:
        f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
        price = float(f.fetch("1m", 30).iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"])
        STATE["trailing"]["active"] = False
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"],
                reply_markup=kb(nav_btns("start")))
            return
        if trade["pnl_pct"] < 0:
            register_loss()
        else:
            register_win()
        await update.callback_query.edit_message_text(
            "Закрыто: " + pos["symbol"] + " PnL: " + str(round(trade["pnl_pct"], 2)) + "%",
            reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("start")))

async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("📅 За день", "hist_day")],
        [btn("📆 За неделю", "hist_week")],
        [btn("🗓 За всё", "hist_all")],
        nav_btns("start")[0],
    ]
    await update.callback_query.edit_message_text("Период:", reply_markup=kb(rows))

async def hist_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = update.callback_query.data.replace("hist_", "")
    rows = [
        [btn("🎮 Кнопки", "hist_src_" + period + "_manual")],
        [btn("📊 Сигналы", "hist_src_" + period + "_signal")],
        [btn("🤖 Авто", "hist_src_" + period + "_auto")],
        [btn("📋 Всё", "hist_src_" + period + "_all")],
        nav_btns("hist_menu")[0],
    ]
    await update.callback_query.edit_message_text("Источник:", reply_markup=kb(rows))

async def hist_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("hist_src_", "")
    period, source = raw.split("_")
    s = portfolio.stats_filtered(period, source)
    text = ("📜 " + period + " / " + source +
            "\nСделок: " + str(s["total"]) +
            "\nПлюс: " + str(s["wins"]) +
            "\nМинус: " + str(s["losses"]) +
            "\nWinrate: " + str(round(s["winrate"], 1)) + "%" +
            "\nPnL %: " + str(round(s["total_pnl"], 2)) +
            "\nPnL USDT: " + str(round(s["total_abs"], 2)) +
            "\nPF: " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = portfolio.stats_filtered("all", "all")
    text = ("📊 Всего\nСделок: " + str(s["total"]) +
            "\nWinrate: " + str(round(s["winrate"], 1)) + "%" +
            "\nPnL USDT: " + str(round(s["total_abs"], 2)) +
            "\nPF: " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]

    if "demo_amount" in pend:
        try:
            amount = float(txt)
        except Exception:
            await update.message.reply_text("Число.", reply_markup=kb(nav_btns("demo")))
            return
        pend.pop("demo_amount")
        STATE["demo"]["amount"] = amount
        sig_a = STATE["demo"]["action"]
        sym = STATE["demo"]["symbol"]
        bp = STATE["demo"]["buy_price"]
        try:
            f = Friday(symbol=sym, exchange_name=STATE["manual"]["exchange"])
            cp = float(f.fetch("1m", 5).iloc[-1]["close"])
        except Exception:
            cp = bp
        pnl_pct = (cp - bp) / bp if sig_a == "BUY" else (bp - cp) / bp
        pnl = amount * pnl_pct
        res = "📈 Плюс" if pnl >= 0 else "📉 Минус"
        msg = ("🎮 ДЕМО\n" + sym + "\nВход: " + fmt_price(bp) +
               "\nВыход: " + fmt_price(cp) + "\nСумма: " + str(amount) + " USDT" +
               "\n\n" + res + ": " + str(round(pnl, 2)) + " USDT (" +
               str(round(pnl_pct * 100, 2)) + "%)")
        await update.message.reply_text(msg, reply_markup=kb([[btn("🏠 В меню Демо", "demo")]]))
        return

    if "wait_custom_amount" in pend:
        sd = pend["wait_custom_amount"]
        try:
            amount = float(txt)
        except Exception:
            await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start")))
            return
        pend.pop("wait_custom_amount")
        if sd["action"] == "BUY":
            qty = amount / sd["price"] if sd["price"] else 0
            portfolio.open_position(sd["symbol"], "BUY", sd["price"], qty, source="signal")
            STATE["trailing"]["active"] = True
            if STATE["trailing"]["task"] is None or STATE["trailing"]["task"].done():
                STATE["trailing"]["task"] = asyncio.create_task(
                    _trailing_loop(ctx.application, sd["symbol"], sd["price"], "BUY"))
            await update.message.reply_text(
                "Купил " + sd["symbol"] + " на " + str(amount) + " USDT.",
                reply_markup=kb([[btn("🏠 В меню", "start")]]))
        else:
            pos = portfolio.load().get("open")
            if not pos:
                await update.message.reply_text("Нет позиции.",
                    reply_markup=kb(nav_btns("start")))
                return
            qty = min(amount / sd["price"] if sd["price"] else 0, pos["qty"])
            trade = portfolio.close_position(sd["price"], qty)
            STATE["trailing"]["active"] = False
            if "error" in trade:
                await update.message.reply_text("Ошибка: " + trade["error"],
                    reply_markup=kb(nav_btns("start")))
                return
            if trade["pnl_pct"] < 0:
                register_loss()
            else:
                register_win()
            await update.message.reply_text(
                "Продал. PnL: " + str(round(trade["pnl_pct"], 2)) + "% (" +
                str(round(trade["pnl_abs"], 2)) + " USDT)",
                reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return

    if "auto" in pend:
        k = pend["auto"]
        if k == "balance":
            try:
                STATE["balance"] = float(txt)
            except Exception:
                await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start")))
                return
            pend.pop("auto")
            await auto_after_balance(update, ctx)
            return
        if k == "budget":
            try:
                STATE["daily_budget"] = float(txt)
            except Exception:
                await update.message.reply_text("Число.",
                    reply_markup=kb(nav_btns("auto_start")))
                return
            pend.pop("auto")
            await auto_after_budget(update, ctx)
            return
        if k == "city":
            STATE["city"] = txt
            STATE["tz"] = city_to_tz(txt)
            pend.pop("auto")
            await auto_after_city(update, ctx)
            return
        if k == "check_time":
            pend.pop("auto")
            await auto_after_check_time(update, ctx)
            return
        if k == "work_time":
            hours = _parse_hours(txt)
            pend["auto_hours"] = str(hours)
            pend["multi_day"] = ("д" in txt.lower() or "day" in txt.lower())
            pend.pop("auto")
            await auto_ask_exchange(update, ctx)
            return
        if k == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf):
                await update.message.reply_text("Формат: 5m, 1h.",
                    reply_markup=kb(nav_btns("tf_bad")))
                return
            STATE["auto"]["tf"] = tf
            STATE["auto"]["waiting_tf_confirm"] = False
            pend.pop("auto")
            await update.message.reply_text("Ок, " + tf + ".",
                reply_markup=kb(nav_btns("start")))
            if STATE["auto"]["current_offer"]:
                STATE["auto"]["symbols"] = [STATE["auto"]["current_offer"]]
                STATE["auto"]["current_offer"] = None
                await _ask_hascoins(update, ctx)
            return
        if k == "multi_count":
            try:
                n = int(txt)
            except Exception:
                await update.message.reply_text("2-10.",
                    reply_markup=kb(nav_btns("start")))
                return
            if n < 2 or n > MAX_MULTI:
                await update.message.reply_text("2-10.",
                    reply_markup=kb(nav_btns("start")))
                return
            pend.pop("auto")
            rows = [[btn("✅ Запустить " + str(n), "multi_start_" + str(n))],
                    nav_btns("start")[0]]
            await update.message.reply_text("Ок, " + str(n), reply_markup=kb(rows))
            return

    if "manual" in pend:
        if pend["manual"] == "search":
            ticker = txt.upper()
            found = None
            for sym in STATE["manual"]["pairs"]:
                if sym.startswith(ticker + "/"):
                    found = sym
                    break
            pend.pop("manual")
            if not found:
                await update.message.reply_text("Не нашёл.",
                    reply_markup=kb(nav_btns("man_menu")))
                return
            STATE["manual"]["symbol"] = found
            rows = [
                [btn("📊 Сигналы", "man_signals")],
                [btn("⏱ Time Frame", "man_tf")],
                [btn("⚡ Кнопки", "man_quick")],
                nav_btns("man_pairs")[0],
            ]
            await update.message.reply_text("Пара: " + found, reply_markup=kb(rows))
            return

def _parse_hours(txt):
    low = txt.lower()
    m = re.search(r"(\d+(\.\d+)?)", low)
    if not m:
        return 1
    v = float(m.group(1))
    if "д" in low or "day" in low:
        return v * 24
    return v

async def multi_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(update.callback_query.data.replace("multi_start_", ""))
    await update.callback_query.edit_message_text("Сканирую " + str(n) + " монет...",
        reply_markup=kb(nav_btns("start")))
    bests = []
    pairs = exchanges.load_all_pairs(STATE["auto"]["exchange"], quote="USDT", max_pairs=150)
    for sym in pairs:
        r = await _score_pair(sym)
        if r:
            bests.append(r)
        if len(bests) >= n * 2:
            break
        await asyncio.sleep(0.1)
    bests.sort(key=lambda x: x["score"], reverse=True)
    top = bests[:n]
    if not top:
        await update.callback_query.edit_message_text("Ничего.",
            reply_markup=kb(nav_btns("start")))
        return
    STATE["auto"]["symbols"] = [t["symbol"] for t in top]
    lines = ["🤖 Взял " + str(len(top)) + ":"]
    for t in top:
        lines.append("• " + t["symbol"] + " (" + str(round(t["atr_pct"] * 100, 1)) + "%)")
    await update.callback_query.edit_message_text("\n".join(lines),
        reply_markup=kb(nav_btns("start")))

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    if data == "demo":
        await demo(update, ctx)
        return
    if data == "demo_scan":
        await demo_scan(update, ctx)
        return
    if data == "demo_skip":
        await demo_skip(update, ctx)
        return
    if data == "demo_buy":
        await demo_buy(update, ctx)
        return
    if data == "demo_sell":
        await demo_sell(update, ctx)
        return
    if data == "auto_stop_signals":
        STATE["auto"]["active"] = False
        STATE["trailing"]["active"] = False
        if STATE["auto"]["task"]:
            STATE["auto"]["task"].cancel()
            STATE["auto"]["task"] = None
        STATE["auto"]["symbols"] = []
        STATE["auto"]["current_offer"] = None
        STATE["auto"]["waiting_tf_confirm"] = False
        STATE["auto"]["switch_wait_until"] = None
        await q.edit_message_text("🛑 Стоп.",
            reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return
    if data == "start":
        if STATE["auto"]["active"]:
            STATE["auto"]["active"] = False
            STATE["trailing"]["active"] = False
            if STATE["auto"]["task"]:
                STATE["auto"]["task"].cancel()
                STATE["auto"]["task"] = None
            STATE["auto"]["symbols"] = []
            STATE["auto"]["current_offer"] = None
            STATE["auto"]["waiting_tf_confirm"] = False
            STATE["auto"]["switch_wait_until"] = None
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
        await q.edit_message_text("Бюджет?", reply_markup=kb(nav_btns("auto_start")))
        return
    if data == "auto_retry_city":
        STATE["pending"]["auto"] = "city"
        await q.edit_message_text("Город?", reply_markup=kb(nav_btns("auto_retry_budget")))
        return
    if data == "auto_retry_check_time":
        STATE["pending"]["auto"] = "check_time"
        local = local_now().strftime("%H:%M")
        await q.edit_message_text("Время? (например " + local + ")",
            reply_markup=kb(nav_btns("auto_retry_city")))
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
    if data == "switch_yes":
        await switch_yes(update, ctx)
        return
    if data == "switch_no":
        await switch_no(update, ctx)
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
    if data == "sig_buy_signal":
        await sig_buy_signal(update, ctx)
        return
    if data == "sig_buy_custom":
        await sig_buy_custom(update, ctx)
        return
    if data == "sig_sell_signal":
        await sig_sell_signal(update, ctx)
        return
    if data == "sig_sell_custom":
        await sig_sell_custom(update, ctx)
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
    if data == "noop":
        return

def main():
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()