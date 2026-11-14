# language: Python 3.10+, file: bot.py
import asyncio
import datetime
import json
import os
import re
import time
from zoneinfo import ZoneInfo
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
import ccxt

import config
import exchanges
import portfolio
from friday import Friday, Mode, MODE_CONFIG, fmt_price
from chart import build_chart
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# ---------- КОНСТАНТЫ ----------
CONF_SIGNAL = 0.85
CONF_MIN_AUTO = 0.55
FEE_MIN_TP = 0.006
MIN_NET_PCT = 0.2
VOL_MIN_24H = 1000000.0
PANIC_DROP = 0.02
PANIC_INTERVAL = 5
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 2
SCAN_INTERVAL = 300
DAILY_DD_LIMIT = 0.05

_EX_CACHE = {}

STATE = {
    "balance": 0.0, "daily_budget": 0.0, "city": "", "tz": "UTC",
    "loss_streak": 0, "sleep_until": None,
    "day_started": datetime.date.today().isoformat(),
    "day_pnl_usdt": 0.0, "day_halt": False,
    "auto": {
        "active": False, "task": None, "exchange": "okx", "tf": "1m",
        "work_hours": 24, "symbols": [], "seen_signals": set(),
        "current_offer": None, "current_chance": 0,
        "bg_scan_done": False, "bg_candidates": [],
        "have_coin": None, "coin_qty": 0.0, # <--- НОВОЕ
    },
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"tasks": {}, "active": {}},
}

def kb(rows): return InlineKeyboardMarkup(rows)
def btn(text, data): return InlineKeyboardButton(text, callback_data=data)
def nav_btns(back_data): return [[btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]]

def is_sleeping():
    return STATE["sleep_until"] and datetime.datetime.utcnow() < STATE["sleep_until"]

def sleep_left_min():
    if not STATE["sleep_until"]: return 0
    return max(0, int((STATE["sleep_until"] - datetime.datetime.utcnow()).total_seconds() / 60))

def day_rollover():
    today = datetime.date.today().isoformat()
    if STATE["day_started"] != today:
        STATE["day_started"] = today
        STATE["day_pnl_usdt"] = 0.0
        STATE["day_halt"] = False
        STATE["loss_streak"] = 0

def register_loss(abs_pnl=0.0):
    STATE["day_pnl_usdt"] += abs_pnl
    STATE["loss_streak"] += 1
    if STATE["loss_streak"] >= LOSS_LIMIT:
        STATE["sleep_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLEEP_MIN)
        STATE["loss_streak"] = 0
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget > 0 and STATE["day_pnl_usdt"] <= -budget * DAILY_DD_LIMIT:
        STATE["day_halt"] = True

def register_win(abs_pnl=0.0):
    STATE["day_pnl_usdt"] += abs_pnl
    STATE["loss_streak"] = 0

def calc_qty(price, sl_price, confidence):
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget <= 0 or price <= 0: return 0.0
    risk_pct = 0.03 if confidence >= 0.85 else (0.02 if confidence >= 0.75 else 0.01)
    diff = abs(price - sl_price)
    if diff <= 0: return 0.0
    return round(min((budget * risk_pct) / diff, (budget * 0.2) / price), 8)

def tf_from_hours(hours):
    h = int(hours) if hours else 24
    if h <= 2: return "1m"
    if h <= 6: return "5m"
    if h <= 12: return "15m"
    return "4h"

def tf_label(tf):
    return {"1m": "1 минута", "5m": "5 минут", "15m": "15 минут", "1h": "1 час", "4h": "4 часа"}.get(tf, tf)

def get_exchange_obj(key):
    if key in _EX_CACHE: return _EX_CACHE[key]
    params = {"timeout": 10000, "options": {"defaultType": "spot"}}
    ex = getattr(ccxt, key)(params)
    _EX_CACHE[key] = ex
    return ex

def city_to_tz(city):
    try:
        loc = Nominatim(user_agent="friday_bot").geocode(city, timeout=10)
        if not loc: return "UTC"
        return TimezoneFinder().timezone_at(lat=loc.latitude, lng=loc.longitude) or "UTC"
    except Exception: return "UTC"

def local_now():
    try: return datetime.datetime.now(ZoneInfo(STATE["tz"]))
    except Exception: return datetime.datetime.utcnow()

# ---------- СКАН И СИГНАЛЫ ----------
async def bg_scan_market():
    try:
        ex = get_exchange_obj("binance")
        loop = asyncio.get_event_loop()
        tickers = await loop.run_in_executor(None, lambda: ex.fetch_tickers())
        
        pre_candidates = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT"): continue
            price = float(t.get("last") or 0)
            qv = float(t.get("quoteVolume") or 0)
            if qv < VOL_MIN_24H or price <= 0: continue
            pre_candidates.append({"symbol": sym, "vol24": qv, "price": price, "change": float(t.get("percentage") or 0)})
        
        pre_candidates.sort(key=lambda x: x["vol24"], reverse=True)
        top_20 = pre_candidates[:20]
        
        async def get_chance(c):
            try:
                f = Friday(symbol=c["symbol"], exchange_name="binance")
                df = await loop.run_in_executor(None, lambda: f.fetch("1m", 60))
                df = f.enrich(df, "1m").dropna()
                if len(df) < 10: return None
                sig = f.analyze_filtered("1m", tp_mult=3.0)
                chance = sig.confidence * 100
                return {"symbol": c["symbol"], "chance": chance,
                        "vol24": c["vol24"], "price": sig.price,
                        "signal": sig, "tf": "1m", "regime": "normal"}
            except Exception: return None
                
        tasks = [get_chance(c) for c in top_20]
        results = await asyncio.gather(*tasks)
        valid = [r for r in results if r is not None]
        
        valid.sort(key=lambda x: x["chance"], reverse=True)
        STATE["auto"]["bg_candidates"] = valid
        STATE["auto"]["bg_scan_done"] = True
    except Exception as e:
        print("BG Scan error:", e)
        STATE["auto"]["bg_scan_done"] = True

async def send_signal(app, r, reason=""):
    sig = r["signal"]
    
    # Если у нас НЕТ монеты, но пришел сигнал SELL — игнорируем
    if STATE["auto"]["have_coin"] == False and sig.action == "SELL":
        return
    # Если у нас ЕСТЬ монета, но пришел сигнал BUY — тоже можно докупать, но по твоей логике "только SELL чтобы продать" — можно ограничить. 
    # Оставим возможность докупать (BUY), если он не запретил. Но если он сказал "только SELL" — тогда блочим BUY.
    # По твоему тексту: если есть монета, он ждет Sell чтобы продать, либо Buy чтобы докупить.
    
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget <= 0: return
    risk_pct = 0.03 if sig.confidence >= 0.85 else (0.02 if sig.confidence >= 0.75 else 0.01)
    sl_diff = abs(sig.price - sig.stop_loss)
    if sl_diff <= 0: return
    
    # Если это SELL и у нас есть монеты, продаем то, что есть (или часть)
    if sig.action == "SELL" and STATE["auto"]["coin_qty"] > 0:
        qty = STATE["auto"]["coin_qty"]
        lot = qty * sig.price
    else:
        max_loss = budget * risk_pct
        qty = min((budget * risk_pct) / sl_diff, (budget * 0.2) / sig.price)
        lot = qty * sig.price
        
    if lot <= 0: return
    
    net_pct = (abs(sig.take_profit - sig.price) / sig.price - 0.004) * 100
    net_usdt = lot * (net_pct / 100.0)
    
    STATE["pending"]["signal"] = {"action": sig.action, "symbol": r["symbol"], "price": sig.price, "lot": lot, "qty": qty, "sl": sig.stop_loss, "tp": sig.take_profit}
    
    text = ("🎯 " + r["symbol"] + " · " + r["tf"] + ("\n" + reason if reason else "") +
            "\n" + sig.action + " @ " + fmt_price(sig.price) +
            "\n🎯 Шанс: " + str(int(sig.confidence * 100)) + "%" +
            "\n💵 Лот: " + str(round(lot, 2)) + " USDT" +
            "\n🛡 Риск: " + str(round(budget * risk_pct, 2)) + " USDT (" + str(round(risk_pct * 100, 1)) + "%)" +
            "\nСтоп " + fmt_price(sig.stop_loss) + "\nТейк " + fmt_price(sig.take_profit) +
            "\n💰 Чистая ~" + str(round(net_pct, 2)) + "% (~$" + str(round(net_usdt, 2)) + ")")
    
    rows = [[btn("✅ " + ("Купил" if sig.action == "BUY" else "Продал"), "sig_buy_signal" if sig.action == "BUY" else "sig_sell_signal")],
            [btn("✏️ Своя сумма", "sig_buy_custom" if sig.action == "BUY" else "sig_sell_custom")],
            [btn("❌ Пропустил", "sig_skipped")], [btn("🛑 СТОП", "auto_stop_signals")]]
            
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def _first_scan_then_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while not STATE["auto"]["bg_scan_done"] and STATE["auto"]["active"]:
        await asyncio.sleep(1)
        
    candidates = STATE["auto"].get("bg_candidates", [])
    if candidates:
        best = candidates[0]
        STATE["auto"]["current_offer"] = best["symbol"]
        STATE["auto"]["current_chance"] = best["chance"]
        hours = STATE["auto"].get("work_hours", 24)
        
        est_pct = best["signal"].confidence * 100 * (hours / 1.5) * 0.35
        est_usdt = STATE["daily_budget"] * (est_pct / 100.0)
        
        text = ("📊 <b>Лучшая монета</b>\n\n"
                "🪙 <b>" + best["symbol"] + "</b>\n"
                "🎯 Шанс: <b>" + str(int(best["chance"])) + "%</b>\n"
                "📈 Прогноз за " + str(hours) + "ч: <b>+" + str(round(est_pct, 1)) + "%</b> (~$" + str(round(est_usdt, 2)) + ")\n"
                "💰 Объем: <b>" + str(round(best["vol24"]/1000000, 1)) + "M</b>")
        
        rows = [[btn("✅ Берём", "forecast_go")], [btn("🔄 Ещё скан", "forecast_rescan")], [btn("🛑 СТОП", "auto_stop_signals")]]
        if chat_id:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
            
    while STATE["auto"]["active"]:
        await asyncio.sleep(SCAN_INTERVAL)
        if not STATE["auto"]["active"]: return
        await bg_scan_market()

async def scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while STATE["auto"]["active"]:
        try:
            if STATE["day_halt"] or is_sleeping() or "signal" in STATE["pending"] or portfolio.count_open() >= MAX_POSITIONS:
                await asyncio.sleep(5)
                continue
            
            current_sym = STATE["auto"]["current_offer"]
            candidates = STATE["auto"].get("bg_candidates", [])
            current_data = next((c for c in candidates if c["symbol"] == current_sym), None)
            
            if current_data:
                if current_data["chance"] >= CONF_MIN_AUTO * 100:
                    key = current_data["symbol"] + ":" + current_data["signal"].action
                    if key not in STATE["auto"]["seen_signals"]:
                        STATE["auto"]["seen_signals"].add(key)
                        await send_signal(app, current_data, "по текущей")
                        continue
            
            if not STATE["auto"]["symbols"]:
                for c in candidates:
                    if c["symbol"] != current_sym and c["chance"] > STATE["auto"]["current_chance"] * 1.10:
                        text = ("🔥 <b>Есть лучше!</b>\n\n"
                                "Сейчас: " + current_sym + " (" + str(int(STATE["auto"]["current_chance"])) + "%)\n"
                                "Новая: <b>" + c["symbol"] + "</b> (" + str(int(c["chance"])) + "%)")
                        rows = [[btn("✅ Поменять", "switch_yes")], [btn("❌ Оставить", "switch_no")]]
                        if chat_id:
                            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
                        break
                        
            await asyncio.sleep(5)
        except Exception as e:
            print("scan_loop:", e)
            await asyncio.sleep(5)

async def forecast_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    checked = STATE["auto"].get("bg_candidates", [])
    if not checked: return
    best = checked[0]
    STATE["auto"]["symbols"] = [best["symbol"]]
    STATE["auto"]["current_offer"] = best["symbol"]
    STATE["auto"]["current_chance"] = best["chance"]
    
    rows = [[btn("Нет", "hascoins_no")], [btn("Да", "hascoins_yes")]]
    await update.callback_query.edit_message_text("Есть у тебя " + best["symbol"] + "?", reply_markup=kb(rows))

async def forecast_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔄 Пересканирую...", reply_markup=kb(nav_btns("start")))

async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["have_coin"] = False
    STATE["auto"]["coin_qty"] = 0.0
    await update.callback_query.edit_message_text("Ок. Ждем только BUY.", reply_markup=kb(nav_btns("start")))
    
    checked = STATE["auto"].get("bg_candidates", [])
    best = checked[0]
    key = best["symbol"] + ":" + best["signal"].action
    STATE["auto"]["seen_signals"].add(key)
    await send_signal(ctx.application, best, "стартовая")
    if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
        STATE["auto"]["task"] = asyncio.create_task(scan_loop(ctx.application))

async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["wait_qty"] = True
    await update.callback_query.edit_message_text("Сколько монет у тебя есть?", reply_markup=kb(nav_btns("start")))

async def switch_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    candidates = STATE["auto"].get("bg_candidates", [])
    current_sym = STATE["auto"]["current_offer"]
    better = None
    for c in candidates:
        if c["symbol"] != current_sym and c["chance"] > STATE["auto"]["current_chance"] * 1.10:
            better = c; break
            
    if better:
        STATE["auto"]["symbols"] = [better["symbol"]]
        STATE["auto"]["current_offer"] = better["symbol"]
        STATE["auto"]["current_chance"] = better["chance"]
        await update.callback_query.edit_message_text("✅ Поменял на " + better["symbol"], reply_markup=kb(nav_btns("start")))
        key = better["symbol"] + ":" + better["signal"].action
        STATE["auto"]["seen_signals"].add(key)
        await send_signal(ctx.application, better, "новая лучшая")
    else:
        await update.callback_query.edit_message_text("Оставил текущую.", reply_markup=kb(nav_btns("start")))

async def switch_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок, оставил текущую.", reply_markup=kb(nav_btns("start")))

# ---------- МЕНЮ ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    day_rollover()
    opens = portfolio.load().get("opens", [])
    pos_txt = ("\n📌 Позиции: " + str(len(opens)) + "/" + str(MAX_POSITIONS)) if opens else ""
    rows = [[btn("🤖 Автопилот", "auto_start")], [btn("✋ Ручной режим", "man_menu")], [btn("🎮 Демо", "demo")], [btn("📜 История", "hist_menu")], [btn("📊 Статистика", "stats_menu")], [btn("❌ Закрыть все", "close_all")]]
    txt = "Пятница на связи."
    if is_sleeping(): txt += "\n😴 Пауза: " + str(sleep_left_min()) + " мин"
    if STATE["day_halt"]: txt += "\n🛑 Дневной лимит"
    txt += pos_txt
    if update.message: await update.message.reply_text(txt, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = False
    if STATE["auto"]["task"]: STATE["auto"]["task"].cancel(); STATE["auto"]["task"] = None
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["bg_scan_done"] = False
    STATE["auto"]["bg_candidates"] = []
    STATE["auto"]["have_coin"] = None
    STATE["auto"]["coin_qty"] = 0.0
    asyncio.create_task(bg_scan_market())
    STATE["pending"]["auto"] = "balance"
    await update.callback_query.edit_message_text("Баланс (USDT)?", reply_markup=kb(nav_btns("start")))

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = "Баланс " + str(STATE["balance"]) + ".\nБюджет на день?"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_start")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "city"
    msg = "Бюджет " + str(STATE["daily_budget"]) + ".\nГород?"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_balance")))

async def auto_after_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "check_time"
    msg = "Город: " + STATE["city"] + ".\nВремя? (" + local_now().strftime("%H:%M") + ")"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))

async def auto_after_check_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = "Сколько работаем? ('2 часа', '1 день')"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))

async def auto_ask_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn(name, "auto_ex_set_" + key)] for key, name in exchanges.EXCHANGES.items()]
    rows.append(nav_btns("auto_retry_check_time")[0])
    msg = "Биржа:"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    ctx.application.bot_data["chat_id"] = update.callback_query.message.chat_id
    STATE["auto"]["active"] = True
    STATE["auto"]["symbols"] = []
    await update.callback_query.edit_message_text("Биржа: " + exchanges.EXCHANGES.get(key, key) + "\n⏳ Жду скан...", reply_markup=kb([]))
    if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
        STATE["auto"]["task"] = asyncio.create_task(_first_scan_then_loop(ctx.application))

async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("1m", "auto_tf_1m"), btn("5m", "auto_tf_5m"), btn("15m", "auto_tf_15m"), btn("4h", "auto_tf_4h")], [btn("✏️ Своё", "auto_tf_custom")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("ТФ:", reply_markup=kb(rows))

async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tf = update.callback_query.data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text("Впиши ТФ:", reply_markup=kb(nav_btns("tf_bad"))); return
    STATE["auto"]["tf"] = tf
    await update.callback_query.edit_message_text("Ок, " + tf_label(tf) + ".", reply_markup=kb(nav_btns("start")))

async def tf_ok_15m(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["tf"] = "15m"
    await update.callback_query.edit_message_text("Ок, 15m.", reply_markup=kb(nav_btns("start")))

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("🚀 Найти монету", "demo_scan")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("🎮 Демо.", reply_markup=kb(rows))

async def demo_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Сканирую...")
    if not STATE["auto"]["bg_scan_done"]: await bg_scan_market()
    candidates = STATE["auto"].get("bg_candidates", [])
    if not candidates: await update.callback_query.edit_message_text("Не нашёл.", reply_markup=kb(nav_btns("demo"))); return
    best = candidates[0]
    STATE["demo"]["symbol"] = best["symbol"]; STATE["demo"]["buy_price"] = best["price"]
    text = ("🎮 ДЕМО\n" + best["symbol"] + "\nШанс: " + str(int(best["chance"])) + "%")
    rows = [[btn("✅ Купил", "demo_buy")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def demo_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["demo"]["symbol"] = None
    await update.callback_query.edit_message_text("Ок.", reply_markup=kb([[btn("🏠 Демо", "demo")]]))

async def demo_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько купил?", reply_markup=kb(nav_btns("demo")))

async def demo_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько продал?", reply_markup=kb(nav_btns("demo")))

async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    live_pnl_sent = False
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(30)
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.fetch("1m", 20)
            price = float(df["close"].iloc[-1])
            atr_v = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
            portfolio.update_peak(price, symbol)
            pos2 = portfolio.find_open(symbol)
            if not pos2: break
            
            pnl_now = portfolio.current_pnl(price, symbol)
            if pnl_now and pnl_now["pnl_pct"] >= 0.5 and not live_pnl_sent and chat_id:
                live_pnl_sent = True
                await app.bot.send_message(chat_id=chat_id, text="📈 " + symbol + " сейчас +" + str(round(pnl_now["pnl_pct"], 2)) + "% (~$" + str(round(pnl_now["pnl_abs"], 2)) + ")")
            
            best = pos2.get("peak", pos["entry"])
            if pos2["side"] == "BUY":
                gain = (best - pos["entry"]) / pos["entry"]
                sl = pos["entry"]
                if gain >= 2.0 * (atr_v / pos["entry"] if pos["entry"] else 0): sl = best - atr_v * 0.3
                elif gain >= 1.5 * (atr_v / pos["entry"] if pos["entry"] else 0): sl = pos["entry"] + (best - pos["entry"]) * 0.33
                if price <= sl and gain >= (atr_v / pos["entry"] if pos["entry"] else 0):
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
                        else: register_win(trade.get("pnl_abs", 0))
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text="🛡 " + symbol + " PnL: " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)")
                    break
            else:
                gain = (pos["entry"] - best) / pos["entry"]
                sl = pos["entry"]
                if gain >= 2.0 * (atr_v / pos["entry"] if pos["entry"] else 0): sl = best + atr_v * 0.3
                elif gain >= 1.5 * (atr_v / pos["entry"] if pos["entry"] else 0): sl = pos["entry"] - (pos["entry"] - best) * 0.33
                if price >= sl and gain >= (atr_v / pos["entry"] if pos["entry"] else 0):
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
                        else: register_win(trade.get("pnl_abs", 0))
                    break
        except Exception: pass
    STATE["trailing"]["active"][symbol] = False

async def _panic_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["panic"]["active"][symbol] = True
    while STATE["panic"]["active"].get(symbol):
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.enrich(f.fetch("1m", 60), "1m").dropna().reset_index(drop=True)
            if len(df) < 5: await asyncio.sleep(PANIC_INTERVAL); continue
            last = df.iloc[-1]; price = float(last["close"])
            pnl = portfolio.current_pnl(price, symbol)
            if pnl:
                peak = pnl.get("peak", pnl["entry"])
                drop = (peak - price) / peak if peak else 0
                if pnl["side"] == "BUY" and drop >= PANIC_DROP:
                    bearish = (last["ema_fast"] < last["ema_slow"] and last["macd_hist"] < 0 and last["close"] < last["bb_lower"])
                    if bearish and chat_id: await app.bot.send_message(chat_id=chat_id, text="🔴 ЗАКРЫВАЙ " + symbol + "!", reply_markup=kb([[btn("🔴 ПРОДАЛ " + symbol, "panic_sold_" + symbol.replace("/", "_"))]]))
        except Exception: pass
        await asyncio.sleep(PANIC_INTERVAL)
    STATE["panic"]["active"][symbol] = False

async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Пропустил.", reply_markup=kb(nav_btns("start")))

async def _open_and_watch(ctx, symbol, side, price, qty, sl, tp):
    if portfolio.count_open() >= MAX_POSITIONS: return {"error": "максимум позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal", sl=sl, tp=tp)
    if "error" in res: return res
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(_trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(_panic_loop(ctx.application, symbol))
    return res

async def sig_buy_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: await update.callback_query.answer("Устарел.", show_alert=True); return
    res = await _open_and_watch(ctx, sig["symbol"], "BUY", sig["price"], sig["qty"], sig["sl"], sig["tp"])
    del STATE["pending"]["signal"]
    if "error" in res: await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb([[btn("🏠 В меню", "start")]])); return
    await update.callback_query.edit_message_text("Купил " + sig["symbol"] + " на " + str(round(sig["lot"], 2)) + " USDT.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_sell_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: await update.callback_query.answer("Устарел.", show_alert=True); return
    pos = portfolio.find_open(sig["symbol"])
    if not pos: del STATE["pending"]["signal"]; await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
    try:
        f = Friday(symbol=sig["symbol"], exchange_name=STATE["auto"]["exchange"])
        price = float(f.fetch("1m", 5).iloc[-1]["close"])
    except Exception: price = sig["price"]
    trade = portfolio.close_position(price, pos["qty"], sig["symbol"])
    STATE["trailing"]["active"][sig["symbol"]] = False; STATE["panic"]["active"][sig["symbol"]] = False
    del STATE["pending"]["signal"]
    if "error" in trade: await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
    if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
    else: register_win(trade.get("pnl_abs", 0))
    await update.callback_query.edit_message_text("PnL " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_buy_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: return
    STATE["pending"]["wait_custom_amount"] = {"action": "BUY", "symbol": sig["symbol"], "price": sig["price"], "sl": sig["sl"], "tp": sig["tp"]}
    await update.callback_query.edit_message_text("Сумма USDT:", reply_markup=kb(nav_btns("start")))

async def sig_sell_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: return
    STATE["pending"]["wait_custom_amount"] = {"action": "SELL", "symbol": sig["symbol"], "price": sig["price"], "sl": sig["sl"], "tp": sig["tp"]}
    await update.callback_query.edit_message_text("Сумма USDT:", reply_markup=kb(nav_btns("start")))

async def panic_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = update.callback_query.data.replace("panic_sold_", "").replace("_", "/")
    STATE["panic"]["active"][sym] = False; STATE["trailing"]["active"][sym] = False
    pos = portfolio.find_open(sym)
    if pos:
        try:
            f = Friday(symbol=sym, exchange_name=STATE["auto"]["exchange"])
            price = float(f.fetch("1m", 5).iloc[-1]["close"])
        except Exception: price = pos["entry"]
        trade = portfolio.close_position(price, pos["qty"], sym)
        if "error" not in trade:
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
            else: register_win(trade.get("pnl_abs", 0))
    await update.callback_query.edit_message_text("Закрыто " + sym + ".", reply_markup=kb(nav_btns("start")))

async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("🏦 " + exchanges.EXCHANGES.get(STATE["manual"]["exchange"], "биржа"), "man_ex")], [btn("📈 Пары", "man_pairs")], [btn("🔍 Поиск", "man_search")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("Ручной:", reply_markup=kb(rows))

async def man_ex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn(name, "man_ex_set_" + key)] for key, name in exchanges.EXCHANGES.items()]
    rows.append(nav_btns("man_menu")[0])
    await update.callback_query.edit_message_text("Биржа:", reply_markup=kb(rows))

async def man_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["exchange"] = update.callback_query.data.replace("man_ex_set_", "")
    STATE["manual"]["pairs"] = []; STATE["manual"]["page"] = 0
    await man_menu(update, ctx)

async def man_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE["manual"]["pairs"]:
        await update.callback_query.edit_message_text("Загружаю...")
        try: STATE["manual"]["pairs"] = exchanges.load_all_pairs(STATE["manual"]["exchange"], quote="USDT", max_pairs=2000)
        except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_menu"))); return
    await _render_pairs(update, ctx)

async def _render_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pairs = STATE["manual"]["pairs"]; page = STATE["manual"]["page"]; start = page * 50; chunk = pairs[start:start + 50]
    rows = []; row = []
    for sym in chunk:
        row.append(btn(sym.replace("/USDT", ""), "man_pick_" + sym.replace("/", "_")))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    nav = []
    if page > 0: nav.append(btn("⬅️", "man_page_" + str(page - 1)))
    nav.append(btn(str(page + 1), "noop"))
    if start + 50 < len(pairs): nav.append(btn("➡️", "man_page_" + str(page + 1)))
    rows.append(nav); rows.append([btn("🔍 Поиск", "man_search"), btn("⬅️ Меню", "man_menu")])
    text = "Пары " + str(start + 1) + "-" + str(min(start + 50, len(pairs))) + " из " + str(len(pairs))
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["page"] = int(update.callback_query.data.replace("man_page_", ""))
    await _render_pairs(update, ctx)

async def man_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["manual"] = "search"
    await update.callback_query.edit_message_text("Тикер:", reply_markup=kb(nav_btns("man_menu")))

async def man_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("man_pick_", "")
    symbol = raw.replace("_", "/")
    if not symbol.endswith("/USDT"): symbol += "/USDT"
    STATE["manual"]["symbol"] = symbol
    rows = [[btn("📊 Сигналы", "man_signals")], [btn("⚡ Кнопки", "man_quick")], nav_btns("man_pairs")[0]]
    await update.callback_query.edit_message_text("Пара: " + symbol, reply_markup=kb(rows))

async def man_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol: return
    await update.callback_query.edit_message_text("Анализирую...")
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(STATE["auto"]["tf"], tp_mult=3.0)
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
    if sig.confidence < CONF_SIGNAL: await update.callback_query.edit_message_text("Шанс " + str(int(sig.confidence * 100)) + "% < " + str(int(CONF_SIGNAL * 100)) + "%", reply_markup=kb(nav_btns("man_pick_back"))); return
    text = ("📊 " + symbol + " · СИГНАЛ\n" + sig.action + " @ " + fmt_price(sig.price) + "\nШанс: " + str(int(sig.confidence * 100)) + "%\nСтоп " + fmt_price(sig.stop_loss) + "\nТейк " + fmt_price(sig.take_profit))
    try:
        path = build_chart(symbol, STATE["auto"]["tf"], sig)
        with open(path, "rb") as photo: await ctx.bot.send_photo(chat_id=update.callback_query.message.chat_id, photo=photo, caption=text, reply_markup=kb(nav_btns("man_pick_back")))
        await update.callback_query.delete_message()
    except Exception: await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol: return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(STATE["auto"]["tf"], tp_mult=3.0)
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
    rows = [[btn("🟢 Купить", "man_buy")], [btn("🔴 Продать", "man_sell")], nav_btns("man_pick_back")[0]]
    text = "⚡ " + symbol + "\n" + sig.action + " " + str(int(sig.confidence * 100)) + "%"
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_pick_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📊 Сигналы", "man_signals")], [btn("⚡ Кнопки", "man_quick")], nav_btns("man_pairs")[0]]
    await update.callback_query.edit_message_text("Пара: " + str(STATE["manual"]["symbol"]), reply_markup=kb(rows))

async def man_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered(STATE["auto"]["tf"], tp_mult=3.0)
        qty = calc_qty(sig.price, sig.stop_loss, sig.confidence)
        res = await _open_and_watch(ctx, symbol, "BUY", sig.price, qty, sig.stop_loss, sig.take_profit)
        if "error" in res: await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb(nav_btns("start"))); return
        await update.callback_query.edit_message_text("BUY " + symbol + " " + str(round(qty * sig.price, 2)) + " USDT", reply_markup=kb(nav_btns("start")))
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    pos = portfolio.find_open(symbol)
    if not pos: await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        price = float(f.fetch("1m", 30).iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"], symbol)
        if "error" in trade: await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
        else: register_win(trade.get("pnl_abs", 0))
        await update.callback_query.edit_message_text("Закрыл " + symbol + " PnL " + str(round(trade["pnl_pct"], 2)) + "%", reply_markup=kb(nav_btns("start")))
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opens = portfolio.load().get("opens", [])
    if not opens: await update.callback_query.edit_message_text("Нет сделок.", reply_markup=kb(nav_btns("start"))); return
    total_pnl = 0.0; total_abs = 0.0; count = 0
    for pos in list(opens):
        try:
            f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
            price = float(f.fetch("1m", 30).iloc[-1]["close"])
            trade = portfolio.close_position(price, pos["qty"], pos["symbol"])
            STATE["trailing"]["active"][pos["symbol"]] = False; STATE["panic"]["active"][pos["symbol"]] = False
            if "error" not in trade:
                total_pnl += trade["pnl_pct"]; total_abs += trade.get("pnl_abs", 0); count += 1
                if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
                else: register_win(trade.get("pnl_abs", 0))
        except Exception: pass
    await update.callback_query.edit_message_text("Закрыто " + str(count) + ".\nИтог PnL " + str(round(total_pnl, 2)) + "% (" + str(round(total_abs, 2)) + " USDT)", reply_markup=kb(nav_btns("start")))

async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📅 День", "hist_day")], [btn("📆 Неделя", "hist_week")], [btn("🗓 Всё", "hist_all")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("Период:", reply_markup=kb(rows))

async def hist_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = update.callback_query.data.replace("hist_", "")
    rows = [[btn("🎮 Кнопки", "hist_src_" + period + "_manual")], [btn("📊 Сигналы", "hist_src_" + period + "_signal")], [btn("🤖 Авто", "hist_src_" + period + "_auto")], [btn("📋 Всё", "hist_src_" + period + "_all")], nav_btns("hist_menu")[0]]
    await update.callback_query.edit_message_text("Источник:", reply_markup=kb(rows))

async def hist_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("hist_src_", ""); parts = raw.split("_")
    period, source = parts[0], parts[1]
    s = portfolio.stats_filtered(period, source)
    text = ("📜 " + period + " / " + source + "\nСделок " + str(s["total"]) + "\n+ " + str(s["wins"]) + " / - " + str(s["losses"]) + "\nWinrate " + str(round(s["winrate"], 1)) + "%" + "\nPnL % " + str(round(s["total_pnl"], 2)) + "\nPnL USDT " + str(round(s["total_abs"], 2)) + "\nPF " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = portfolio.stats_filtered("all", "all")
    text = ("📊 Всего\nСделок " + str(s["total"]) + "\nWinrate " + str(round(s["winrate"], 1)) + "%\nPnL USDT " + str(round(s["total_abs"], 2)) + "\nPF " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]

    if "wait_qty" in pend:
        try: qty = float(txt)
        except Exception: await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
        pend.pop("wait_qty")
        STATE["auto"]["have_coin"] = True
        STATE["auto"]["coin_qty"] = qty
        await update.message.reply_text("Ок, у тебя " + str(qty) + " монет.\nЖдем сигнала SELL (для продажи) или BUY (докупить).", reply_markup=kb(nav_btns("start")))
        
        checked = STATE["auto"].get("bg_candidates", [])
        best = checked[0]
        # Если у нас есть монеты, мы ждем только SELL? Или и то и то. Отправим первый сигнал, если он SELL или если это BUY, но мы можем докупить.
        # По твоей логике "Он говорит хорошо ждём сигнала в Sell или by Sell чтобы продать и не уйти в минус ну либо бай чтобы докупить" - значит ждем оба.
        key = best["symbol"] + ":" + best["signal"].action
        STATE["auto"]["seen_signals"].add(key)
        await send_signal(ctx.application, best, "стартовая")
        if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
            STATE["auto"]["task"] = asyncio.create_task(scan_loop(ctx.application))
        return

    if "demo_amount" in pend:
        try: amount = float(txt)
        except Exception: await update.message.reply_text("Число.", reply_markup=kb(nav_btns("demo"))); return
        pend.pop("demo_amount"); sig_a = STATE["demo"]["action"]; sym = STATE["demo"]["symbol"]; bp = STATE["demo"]["buy_price"]
        try:
            f = Friday(symbol=sym, exchange_name=STATE["manual"]["exchange"])
            cp = float(f.fetch("1m", 5).iloc[-1]["close"])
        except Exception: cp = bp
        pnl_pct = (cp - bp) / bp if sig_a == "BUY" else (bp - cp) / bp
        pnl = amount * pnl_pct; res = "📈 +" if pnl >= 0 else "📉 "
        await update.message.reply_text("🎮 ДЕМО " + sym + "\n" + res + str(round(pnl, 2)) + " USDT (" + str(round(pnl_pct * 100, 2)) + "%)", reply_markup=kb([[btn("🏠 Демо", "demo")]]))
        return

    if "wait_custom_amount" in pend:
        sd = pend["wait_custom_amount"]
        try: amount = float(txt)
        except Exception: await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
        pend.pop("wait_custom_amount")
        if sd["action"] == "BUY":
            qty = amount / sd["price"] if sd["price"] else 0
            res = await _open_and_watch(ctx, sd["symbol"], "BUY", sd["price"], qty, sd["sl"], sd["tp"])
            if "error" in res: await update.message.reply_text("Ошибка: " + res["error"], reply_markup=kb(nav_btns("start"))); return
            await update.message.reply_text("Купил " + sd["symbol"] + " " + str(amount) + " USDT.", reply_markup=kb([[btn("🏠 В меню", "start")]]))
        else:
            pos = portfolio.find_open(sd["symbol"])
            if not pos: await update.message.reply_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
            qty = min(amount / sd["price"] if sd["price"] else 0, pos["qty"])
            trade = portfolio.close_position(sd["price"], qty, sd["symbol"])
            STATE["trailing"]["active"][sd["symbol"]] = False; STATE["panic"]["active"][sd["symbol"]] = False
            if "error" in trade: await update.message.reply_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
            else: register_win(trade.get("pnl_abs", 0))
            await update.message.reply_text("PnL " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)", reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return

    if "auto" in pend:
        k = pend["auto"]
        if k == "balance":
            try: STATE["balance"] = float(txt)
            except Exception: await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
            pend.pop("auto"); await auto_after_balance(update, ctx); return
        if k == "budget":
            try: STATE["daily_budget"] = float(txt)
            except Exception: await update.message.reply_text("Число.", reply_markup=kb(nav_btns("auto_start"))); return
            pend.pop("auto"); await auto_after_budget(update, ctx); return
        if k == "city":
            STATE["city"] = txt; STATE["tz"] = city_to_tz(txt)
            pend.pop("auto"); await auto_after_city(update, ctx); return
        if k == "check_time":
            pend.pop("auto"); await auto_after_check_time(update, ctx); return
        if k == "work_time":
            hours = _parse_hours(txt); STATE["auto"]["work_hours"] = int(hours)
            STATE["auto"]["multi_day"] = ("д" in txt.lower() or "day" in txt.lower())
            STATE["auto"]["tf"] = tf_from_hours(hours)
            pend.pop("auto"); await auto_ask_exchange(update, ctx); return
        if k == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf): await update.message.reply_text("5m, 1h.", reply_markup=kb(nav_btns("tf_bad"))); return
            STATE["auto"]["tf"] = tf; pend.pop("auto"); await update.message.reply_text("Ок, " + tf_label(tf) + ".", reply_markup=kb(nav_btns("start"))); return

    if "manual" in pend:
        if pend["manual"] == "search":
            ticker = txt.upper(); found = None
            for sym in STATE["manual"]["pairs"]:
                if sym.startswith(ticker + "/"): found = sym; break
            pend.pop("manual")
            if not found: await update.message.reply_text("Не нашёл.", reply_markup=kb(nav_btns("man_menu"))); return
            STATE["manual"]["symbol"] = found
            rows = [[btn("📊 Сигналы", "man_signals")], [btn("⚡ Кнопки", "man_quick")], nav_btns("man_pairs")[0]]
            await update.message.reply_text("Пара: " + found, reply_markup=kb(rows)); return

def _parse_hours(txt):
    low = txt.lower(); m = re.search(r"(\d+(\.\d+)?)", low)
    if not m: return 1
    v = float(m.group(1))
    if "д" in low or "day" in low: return v * 24
    return v

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    mapping = {
        "demo": demo, "demo_scan": demo_scan, "demo_skip": demo_skip, "demo_buy": demo_buy, "demo_sell": demo_sell,
        "auto_start": auto_start, "auto_retry_balance": auto_start,
        "tf_ok_15m": tf_ok_15m, "tf_bad": tf_bad, "switch_yes": switch_yes, "switch_no": switch_no,
        "hascoins_no": hascoins_no, "hascoins_yes": hascoins_yes,
        "forecast_go": forecast_go, "forecast_rescan": forecast_rescan,
        "sig_skipped": sig_skipped, "sig_buy_signal": sig_buy_signal, "sig_buy_custom": sig_buy_custom, "sig_sell_signal": sig_sell_signal, "sig_sell_custom": sig_sell_custom,
        "man_menu": man_menu, "man_ex": man_ex, "man_pairs": man_pairs, "man_search": man_search, "man_signals": man_signals, "man_quick": man_quick, "man_pick_back": man_pick_back, "man_buy": man_buy, "man_sell": man_sell,
        "close_all": close_all, "hist_menu": hist_menu, "stats_menu": stats_menu,
    }

    if data.startswith("panic_sold_"): await panic_sold(update, ctx); return
    if data in mapping: await mapping[data](update, ctx); return

    if data == "start":
        if STATE["auto"]["active"]:
            STATE["auto"]["active"] = False
            if STATE["auto"].get("task"): STATE["auto"]["task"].cancel(); STATE["auto"]["task"] = None
        await start(update, ctx); return

    if data == "auto_retry_budget": STATE["pending"]["auto"] = "budget"; await q.edit_message_text("Бюджет?", reply_markup=kb(nav_btns("auto_start"))); return
    if data == "auto_retry_city": STATE["pending"]["auto"] = "city"; await q.edit_message_text("Город?", reply_markup=kb(nav_btns("auto_retry_budget"))); return
    if data == "auto_retry_check_time":
        STATE["pending"]["auto"] = "check_time"; local = local_now().strftime("%H:%M")
        await q.edit_message_text("Время? (" + local + ")", reply_markup=kb(nav_btns("auto_retry_city"))); return
    if data == "auto_stop_signals":
        STATE["auto"]["active"] = False
        if STATE["auto"].get("task"): STATE["auto"]["task"].cancel(); STATE["auto"]["task"] = None
        await q.edit_message_text("🛑 Стоп.", reply_markup=kb([[btn("🏠 В меню", "start")]])); return
    if data.startswith("auto_ex_set_"): await auto_ex_set(update, ctx); return
    if data.startswith("auto_tf_"): await auto_tf_set(update, ctx); return
    if data.startswith("man_ex_set_"): await man_ex_set(update, ctx); return
    if data.startswith("man_page_"): await man_page(update, ctx); return
    if data.startswith("man_pick_"): await man_pick(update, ctx); return
    if data in ("hist_day", "hist_week", "hist_all"): await hist_period(update, ctx); return
    if data.startswith("hist_src_"): await hist_show(update, ctx); return

def main():
    if not os.path.exists(config.PORTFOLIO_FILE): portfolio.save(portfolio._empty())
    else: portfolio.save(portfolio.load())
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()