# language: Python 3.10+, file: bot.py
import asyncio
import datetime
import os
import re
import time
from zoneinfo import ZoneInfo
import aiohttp
import pandas as pd
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
import ccxt

import config
import exchanges
import portfolio
import analysis
from friday import Friday, fmt_price
from chart import build_chart
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# --- НАСТРОЙКИ HYBRID SCANNER ---
DAILY_TARGET_PCT = 0.05  
LOCK_IN_GIVEBACK = 0.015 
FEE_RT_PCT = 0.004

PANIC_DROP = 0.015 
PANIC_INTERVAL = 2 
TRAIL_INTERVAL = 2 
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 5  
DAILY_DD_LIMIT = 0.05    
TOP_N_SCAN = 50          
VOL_MIN_24H = 500000.0
CHUNK_SIZE = 15          

STATE = {
    "balance": 0.0, "daily_budget": 0.0, "tz": "UTC",
    "loss_streak": 0, "sleep_until": None,
    "day_started": datetime.date.today().isoformat(),
    "day_pnl_usdt": 0.0, "day_halt": False,
    "metrics": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "peak_pnl": 0.0, "peak_day_pnl": 0.0, "current_pnl": 0.0, "max_dd": 0.0, "consecutive_wins": 0, "consecutive_losses": 0},
    "auto": {"active": False, "scanning": False, "exchange": "binance", "symbol": None, "last_found": None, "work_hours": 2, "my_coins": [], "scan_msg_id": None, "params": {}, "target_hit": False, "locked": False},
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"tasks": {}, "active": {}, "current_sl": {}, "tp1_hit": {}},
}

def kb(rows): return InlineKeyboardMarkup(rows)
def btn(text, data): return InlineKeyboardButton(text, callback_data=data)
def nav_btns(back_data): return [[btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]]
def is_sleeping(): return STATE["sleep_until"] and datetime.datetime.utcnow() < STATE["sleep_until"]
def net_pct(sig): return (abs(sig.take_profit - sig.price) / sig.price - FEE_RT_PCT) * 100 if sig.price > 0 else 0.0

def get_total_pnl():
    opens = portfolio.load().get("opens", [])
    total_float = 0.0
    for pos in opens:
        cp = pos.get("current_price", pos["entry"])
        if pos["side"] == "BUY":
            total_float += (cp - pos["entry"]) * pos["qty"]
        else:
            total_float += (pos["entry"] - cp) * pos["qty"]
    return STATE["day_pnl_usdt"] + total_float

def check_daily_ratchet():
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget <= 0: return
    
    total_pnl = get_total_pnl()
    pnl_pct = total_pnl / budget
    
    if pnl_pct > STATE["metrics"]["peak_day_pnl"]:
        STATE["metrics"]["peak_day_pnl"] = pnl_pct
        
    if STATE["metrics"]["peak_day_pnl"] >= DAILY_TARGET_PCT:
        STATE["auto"]["target_hit"] = True
        if pnl_pct <= (STATE["metrics"]["peak_day_pnl"] - LOCK_IN_GIVEBACK):
            STATE["auto"]["locked"] = True
            STATE["day_halt"] = True 

def register_loss(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl; STATE["loss_streak"] += 1
    m = STATE["metrics"]; m["trades"] += 1; m["losses"] += 1; m["gross_loss"] += abs(pnl); m["current_pnl"] += pnl
    dd = m["peak_pnl"] - m["current_pnl"]
    if dd > m["max_dd"]: m["max_dd"] = dd
    m["consecutive_wins"] = 0; m["consecutive_losses"] += 1
    if STATE["loss_streak"] >= LOSS_LIMIT: STATE["sleep_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLEEP_MIN); STATE["loss_streak"] = 0
    
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget > 0 and STATE["day_pnl_usdt"] <= -budget * DAILY_DD_LIMIT: STATE["day_halt"] = True
    check_daily_ratchet()

def register_win(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl; STATE["loss_streak"] = 0
    m = STATE["metrics"]; m["trades"] += 1; m["wins"] += 1; m["gross_win"] += abs(pnl); m["current_pnl"] += pnl
    if m["current_pnl"] > m["peak_pnl"]: m["peak_pnl"] = m["current_pnl"]
    m["consecutive_wins"] += 1; m["consecutive_losses"] = 0
    check_daily_ratchet()

def calc_lot(budget, price, sl, open_count):
    if budget <= 0 or price <= 0: return 0.0, 0.0
    diff = abs(price - sl)
    if diff <= 0: return 0.0, 0.0
    
    base_risk = 0.02 
    cl = STATE["metrics"]["consecutive_losses"]
    base_risk *= (1.0 if cl <= 0 else (0.75 if cl == 1 else (0.5 if cl == 2 else 0.0)))
    if base_risk <= 0: return 0.0, 0.0
    
    qty = min((budget * base_risk) / diff, (budget * 0.30) / price) 
    return qty, qty * price

async def fetch_candles(session, symbol, tf, limit=100):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.replace('/', '')}&interval={tf}&limit={limit}"
        async with session.get(url, timeout=3.0) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"])
            df = df[["ts", "open", "high", "low", "close", "volume", "taker_base"]]
            for c in ["open", "high", "low", "close", "volume", "taker_base"]: df[c] = df[c].astype(float)
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
    except Exception: return None

# --- СТРАТЕГИЯ 1: ОТСКОК (Trend Retest) ---
async def check_trend_retest(session, symbol):
    try:
        df15 = await fetch_candles(session, symbol, "15m", 50)
        if df15 is None or len(df15) < 50: return None
        ema9_15 = df15["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema50_15 = df15["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        if ema9_15 < ema50_15: return None

        df5 = await fetch_candles(session, symbol, "5m", 50)
        if df5 is None or len(df5) < 30: return None
        
        last = df5.iloc[-1]; prev = df5.iloc[-2]
        ema21_5 = df5["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        
        pulled_back = prev["low"] <= ema21_5 * 1.005
        bounced = last["close"] > last["open"] and last["close"] > prev["close"]
        
        if not (pulled_back and bounced): return None

        btc_df = await fetch_candles(session, "BTC/USDT", "15m", 10)
        if btc_df is not None and len(btc_df) >= 5:
            btc_ema = btc_df["close"].ewm(span=9, adjust=False).mean().iloc[-1]
            if btc_df["close"].iloc[-1] < btc_ema: return None

        price = last["close"]
        sl = last["low"] * 0.998 
        risk = price - sl
        if risk <= 0: return None
        tp = price + (risk * 2.0)
        if (tp - price) / price < 0.015: return None

        class Sig: pass
        sig = Sig()
        sig.price = price; sig.action = "BUY"; sig.stop_loss = sl; sig.take_profit = tp
        sig.confidence = 0.70 # Вероятность успеха 70%
        
        # EV = (P * TP) - (1-P * SL)
        net = net_pct(sig)
        ev = (0.70 * net) - (0.30 * (abs(price - sl)/price * 100))
        
        return {"symbol": symbol, "signal": sig, "net": net, "tf": "5m", "strategy": "Отскок (Trend Retest)", "ev": ev}
    except Exception: return None

# --- СТРАТЕГИЯ 2: СЖАТИЕ (Volatility Squeeze) ---
async def check_squeeze_breakout(session, symbol):
    try:
        df = await fetch_candles(session, symbol, "5m", 100)
        if df is None or len(df) < 50: return None
        
        ma = df["close"].rolling(20).mean()
        sd = df["close"].rolling(20).std()
        upper = ma + (sd * 2)
        lower = ma - (sd * 2)
        width = (upper - lower) / ma
        
        prev_width = width.iloc[-2]
        avg_width = width.rolling(20).mean().iloc[-2]
        is_squeeze = prev_width < avg_width * 0.7
        if not is_squeeze: return None

        last = df.iloc[-1]; prev_close = df["close"].iloc[-2]
        breakout = last["close"] > upper.iloc[-1] and prev_close <= upper.iloc[-1]
        vol_avg = df["volume"].iloc[-22:-2].mean()
        vol_spike = last["volume"] > vol_avg * 1.8
        
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_rising = rsi.iloc[-1] > rsi.iloc[-2] and rsi.iloc[-1] > 50

        if not (breakout and vol_spike and rsi_rising): return None

        btc_df = await fetch_candles(session, "BTC/USDT", "15m", 20)
        if btc_df is not None and len(btc_df) >= 10:
            btc_ema = btc_df["close"].ewm(span=9, adjust=False).mean().iloc[-1]
            if btc_df["close"].iloc[-1] < btc_ema: return None

        price = last["close"]
        sl = lower.iloc[-1] * 0.998 
        risk = price - sl
        if risk <= 0: return None
        tp = price + (risk * 2.5)
        if (tp - price) / price < 0.02: return None

        class Sig: pass
        sig = Sig()
        sig.price = price; sig.action = "BUY"; sig.stop_loss = sl; sig.take_profit = tp
        sig.confidence = 0.65 # Вероятность успеха 65%
        
        # EV = (P * TP) - (1-P * SL)
        net = net_pct(sig)
        ev = (0.65 * net) - (0.35 * (abs(price - sl)/price * 100))
        
        return {"symbol": symbol, "signal": sig, "net": net, "tf": "5m", "strategy": "Сжатие (Squeeze)", "ev": ev}
    except Exception: return None

async def bg_scan_market(session, open_symbols=[]):
    try:
        async with session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=3.0) as resp:
            if resp.status != 200: return []
            raw = await resp.json()
        pre = []
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in ["BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "TUSDUSDT"]: continue
            sym_fmt = sym[:-4] + "/USDT"
            if sym_fmt in open_symbols: continue
            
            qv = float(t.get("quoteVolume") or 0)
            price = float(t.get("lastPrice") or 0)
            change = abs(float(t.get("priceChangePercent") or 0))
            
            if qv < VOL_MIN_24H or price <= 0: continue
            pre.append({"symbol": sym_fmt, "vol": qv, "chg": change})
            
        pre.sort(key=lambda x: x["chg"] * (x["vol"] ** 0.5), reverse=True)
        return pre[:TOP_N_SCAN]
    except Exception: return []

async def _first_scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    scan_count = 0
    while STATE["auto"]["active"]:
        try:
            if STATE["day_halt"] or is_sleeping(): await asyncio.sleep(5); continue
            if "signal" in STATE["pending"]: await asyncio.sleep(5); continue

            open_positions = portfolio.load().get("opens", [])
            if len(open_positions) >= MAX_POSITIONS: 
                await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession() as session:
                candidates = await bg_scan_market(session, open_symbols=[p["symbol"] for p in open_positions])
                all_valid_results = []
                
                for i in range(0, len(candidates), CHUNK_SIZE):
                    chunk = candidates[i:i+CHUNK_SIZE]
                    
                    # ПАРАЛЛЕЛЬНО ЗАПУСКАЕМ ОБЕ СТРАТЕГИИ ДЛЯ КАЖДОЙ МОНЕТЫ
                    tasks_retest = [check_trend_retest(session, c["symbol"]) for c in chunk]
                    tasks_squeeze = [check_squeeze_breakout(session, c["symbol"]) for c in chunk]
                    
                    res_retest = await asyncio.gather(*tasks_retest)
                    res_squeeze = await asyncio.gather(*tasks_squeeze)
                    
                    for r in res_retest:
                        if r: all_valid_results.append(r)
                    for s in res_squeeze:
                        if s: all_valid_results.append(s)
                        
                    await asyncio.sleep(0.5)

                if all_valid_results:
                    # СОРТИРУЕМ ПО МАТЕМАТИЧЕСКОМУ ОЖИДАНИЮ (EV)
                    all_valid_results.sort(key=lambda x: x["ev"], reverse=True)
                    found = all_valid_results[0] # Берем абсолютно лучший
                    
                    STATE["auto"]["last_found"] = found
                    sym_data = found["symbol"].replace("/", "_")
                    
                    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
                    qty, lot = calc_lot(budget, found["signal"].price, found["signal"].stop_loss, len(open_positions))
                    
                    if qty <= 0: await asyncio.sleep(5); continue

                    text = (f"🎯 <b>Лучший вход (по EV)</b>\n\n"
                            f"🪙 <b>{found['symbol']}</b>\n"
                            f"🧠 Стратегия: <b>{found['strategy']}</b>\n"
                            f"🔖 Вход: <b>{fmt_price(found['signal'].price)}</b>\n"
                            f"💵 Лот: <b>{round(lot, 2)} USDT</b>\n"
                            f"🛡 Стоп: <b>{fmt_price(found['signal'].stop_loss)}</b>\n"
                            f"🎯 Тейк: <b>{fmt_price(found['signal'].take_profit)}</b>\n"
                            f"💰 Чистая: <b>+{round(found['net'], 2)}%</b>\n"
                            f"📊 EV: <b>{round(found['ev'], 2)}</b>")
                    
                    rows = [[btn("✅ Я купил", "sig_go_" + sym_data)], [btn("❌ Пропустил", "sig_skipped")]]
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
                    
                    STATE["pending"]["signal"] = {"action": "BUY", "symbol": found["symbol"], "price": found["signal"].price, "lot": lot, "qty": qty, "sl": found["signal"].stop_loss, "tp": found["signal"].take_profit}
                    
                    for _ in range(15): 
                        if "signal" not in STATE["pending"]: break
                        await asyncio.sleep(2)
                    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
                    continue
                
                scan_count += 1
                if scan_count % 2 == 0 and chat_id:
                    try:
                        now_str = datetime.datetime.now().strftime("%H:%M:%S")
                        msg_text = f"⏳ Сканирую (Отскок + Пружина)...\nОбновлено: {now_str}"
                        if not STATE["auto"]["scan_msg_id"]:
                            msg = await app.bot.send_message(chat_id=chat_id, text=msg_text)
                            STATE["auto"]["scan_msg_id"] = msg.message_id
                        else:
                            await app.bot.edit_message_text(chat_id=chat_id, message_id=STATE["auto"]["scan_msg_id"], text=msg_text)
                    except Exception:
                        STATE["auto"]["scan_msg_id"] = None
                        
                await asyncio.sleep(2)
        except Exception as e:
            print("scan err:", e)
            await asyncio.sleep(3)

# ... (ФУНКЦИИ ТРЕЙЛИНГА, ПАНИКИ, МЕНЮ - БЕЗ ИЗМЕНЕНИЙ, БЕРЕМ ИЗ ПРЕДЫДУЩЕГО КОДА)
async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    STATE["trailing"]["tp1_hit"][symbol] = False
    
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(TRAIL_INTERVAL)
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break
            
            try:
                opened_at = datetime.datetime.fromisoformat(pos["opened_at"])
                held_min = (datetime.datetime.utcnow() - opened_at).total_seconds() / 60
                if held_min > 120:
                    async with aiohttp.ClientSession() as session:
                        df = await fetch_candles(session, symbol, "1m", 5)
                        price = float(df["close"].iloc[-1]) if df is not None else pos["entry"]
                    trade = portfolio.close_position(price, pos["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                        else: register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"⏰ {symbol} Time Stop: {round(trade['pnl_pct'], 2)}%")
                    break
            except Exception: pass

            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, symbol, "1m", 30)
                if df is None: continue
                price = float(df["close"].iloc[-1])
                atr_v = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
            
            pos["current_price"] = price
            data = portfolio.load()
            for p in data.get("opens", []):
                if p["symbol"] == symbol: p["current_price"] = price
            portfolio.save(data)
            
            check_daily_ratchet() 

            portfolio.update_peak(price, symbol)
            pos2 = portfolio.find_open(symbol)
            if not pos2: break
            
            entry, side, init_sl = float(pos2["entry"]), pos2["side"], float(pos2.get("sl", 0)) or float(pos2["entry"])
            R_abs = abs(entry - init_sl); R_pct = R_abs / entry if entry else 0
            best = float(pos2.get("peak", entry))
            cur_sl = STATE["trailing"]["current_sl"].get(symbol, init_sl)
            new_sl = cur_sl

            if side == "BUY":
                profit = (best - entry) / entry if entry else 0
                
                if R_pct > 0:
                    if profit >= 1.0 * R_pct: new_sl = max(cur_sl, entry + R_abs * 0.5)
                    elif profit >= 0.5 * R_pct: new_sl = max(cur_sl, entry) 
                
                if new_sl >= entry and best > entry:
                    trail = best - 0.2 * atr_v 
                    if trail > new_sl: new_sl = trail
                STATE["trailing"]["current_sl"][symbol] = new_sl
                
                if price <= new_sl:
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                        else: register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} PnL: {round(trade['pnl_pct'], 2)}%")
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
            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, symbol, "1m", 5)
                if df is None: 
                    await asyncio.sleep(PANIC_INTERVAL); continue
                price = float(df["close"].iloc[-1])
            pnl = portfolio.current_pnl(price, symbol)
            if pnl and pnl["side"] == "BUY":
                peak = float(pnl.get("peak", pnl["entry"]))
                drop = (peak - price) / peak if peak else 0
                if drop >= PANIC_DROP:
                    if chat_id:
                        await app.bot.send_message(chat_id=chat_id, text=f"🔴 ЗАКРЫВАЙ {symbol}!\nОткат: -{round(drop*100, 2)}%",
                            reply_markup=kb([[btn(f"🔴 ПРОДАЛ {symbol}", "panic_sold_" + symbol.replace("/", "_"))]]))
        except Exception: pass
        await asyncio.sleep(PANIC_INTERVAL)
    STATE["panic"]["active"][symbol] = False

async def _open_and_watch(ctx, symbol, side, price, qty, sl, tp):
    if portfolio.count_open() >= MAX_POSITIONS: return {"error": f"максимум {MAX_POSITIONS} позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal", sl=sl, tp=tp)
    if "error" in res: return res
    STATE["trailing"]["current_sl"][symbol] = sl
    STATE["trailing"]["tp1_hit"][symbol] = False
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(_trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(_panic_loop(ctx.application, symbol))
    return res

async def sig_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: await update.callback_query.answer("Устарел.", show_alert=True); return
    res = await _open_and_watch(ctx, sig["symbol"], sig["action"], sig["price"], sig["qty"], sig["sl"], sig["tp"])
    del STATE["pending"]["signal"]
    if "error" in res: await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb([[btn("🏠 В меню", "start")]])); return
    await update.callback_query.edit_message_text(f"BUY {sig['symbol']} на {round(sig['lot'], 2)} USDT.\nИщу следующую...", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Пропустил. Ищу следующую...", reply_markup=kb(nav_btns("start")))

async def panic_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = update.callback_query.data.replace("panic_sold_", "").replace("_", "/")
    STATE["panic"]["active"][sym] = False; STATE["trailing"]["active"][sym] = False
    pos = portfolio.find_open(sym)
    if pos:
        try:
            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, sym, "1m", 5)
                price = float(df["close"].iloc[-1]) if df is not None else pos["entry"]
        except Exception: price = pos["entry"]
        trade = portfolio.close_position(price, pos["qty"], sym)
        if "error" not in trade:
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), sym)
            else: register_win(trade.get("pnl_abs", 0), sym)
    await update.callback_query.edit_message_text("Закрыто " + sym + ".", reply_markup=kb(nav_btns("start")))

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"] = {}
    STATE["auto"]["scan_msg_id"] = None
    opens = portfolio.load().get("opens", [])
    pos_txt = ("\n📌 Позиции: " + str(len(opens)) + "/" + str(MAX_POSITIONS)) if opens else ""
    
    if STATE["auto"]["locked"]: auto_txt = "\n🛡 Храповик сработал!"
    elif STATE["auto"]["active"]: auto_txt = "\n🤖 Авто: Работает"
    else: auto_txt = ""
    
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    total_pnl = get_total_pnl()
    pnl_pct = (total_pnl / budget)*100 if budget > 0 else 0
    peak_pct = STATE["metrics"]["peak_day_pnl"]*100
    tgt_txt = f"\n📈 День: {round(pnl_pct, 1)}% (Пик: {round(peak_pct, 1)}%)" if budget > 0 else ""
    
    m = STATE["metrics"]
    pf = m["gross_win"] / m["gross_loss"] if m["gross_loss"] > 0 else 0.0
    txt = "Пятница на связи."
    if is_sleeping(): txt += "\n😴 Пауза: " + str(max(0, int((STATE["sleep_until"] - datetime.datetime.utcnow()).total_seconds() / 60))) + " мин"
    if m["trades"] > 0: txt += f"\n📊 PF {round(pf, 2)} · WR {round(m['wins'] / m['trades'] * 100, 1)}%"
    txt += tgt_txt + auto_txt + pos_txt
    rows = [
        [btn("🤖 Автопилот", "auto_start")],
        [btn("✋ Ручной режим", "man_menu")],
        [btn("🎮 Демо", "demo")],
        [btn("📜 История", "hist_menu")],
        [btn("📊 Статистика", "stats_menu")],
        [btn("❌ Закрыть все", "close_all")],
    ]
    if update.message: await update.message.reply_text(txt, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = False
    STATE["pending"] = {}
    STATE["auto"]["scan_msg_id"] = None
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"] = {}
    STATE["auto"].update({"active": False, "symbol": None, "my_coins": [], "scan_msg_id": None})
    STATE["pending"]["auto"] = "balance"
    msg = "Баланс (USDT)?"
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = f"Баланс {STATE['balance']}.\nБюджет на день?"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = f"Бюджет {STATE['daily_budget']}.\nСколько работаем? ('2 часа', '1 день')"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_start")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_start")))

async def auto_ask_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn(name, "auto_ex_set_" + key)] for key, name in exchanges.EXCHANGES.items()]
    rows.append(nav_btns("start")[0])
    msg = "Выбери биржу:"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    ctx.application.bot_data["chat_id"] = update.callback_query.message.chat_id
    
    STATE["pending"]["auto"] = "my_coins"
    msg = f"Биржа выбрана. Стратегия: Hybrid Scanner (Отскок + Сжатие).\n\n<b>Какие монеты у тебя есть сейчас?</b>\nВпиши через запятую (например: BTC, ETH, ZRO). Если нет — напиши 'нет'."
    await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")), parse_mode="HTML")

async def auto_ex_start_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = True
    STATE["auto"]["scanning"] = False
    STATE["pending"] = {}
    msg = "⏳ Портфель учтен. Ищу лучшие точки входа (Гибрид)..."
    if update.message: await update.message.reply_text(msg, reply_markup=kb([]))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb([]))
    asyncio.create_task(_first_scan_loop(ctx.application))

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🎮 Демо недоступно в этой версии.")

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
    await update.callback_query.edit_message_text(f"Пары {start + 1}-{min(start + 50, len(pairs))} из {len(pairs)}", reply_markup=kb(rows))

async def man_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["page"] = int(update.callback_query.data.replace("man_page_", ""))
    await _render_pairs(update, ctx)

async def man_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["manual"] = "search"
    await update.callback_query.edit_message_text("Тикер:", reply_markup=kb(nav_btns("man_menu")))

async def man_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = update.callback_query.data.replace("man_pick_", "").replace("_", "/")
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
        sig = f.analyze_filtered("15m", tp_mult=3.0)
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
    text = f"📊 {symbol} · СИГНАЛ\nТФ: 15m\n{sig.action} @ {fmt_price(sig.price)}\nУверенность: {int(sig.confidence * 100)}%\nЧистая: +{round(net_pct(sig), 2)}%\nСтоп {fmt_price(sig.stop_loss)}\nТейк {fmt_price(sig.take_profit)}"
    try:
        path = build_chart(symbol, "15m", sig)
        with open(path, "rb") as photo: await ctx.bot.send_photo(chat_id=update.callback_query.message.chat_id, photo=photo, caption=text, reply_markup=kb(nav_btns("man_pick_back")))
        await update.callback_query.delete_message()
    except Exception: await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol: return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered("15m", tp_mult=3.0)
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
    rows = [[btn("🟢 Купить", "man_buy")], [btn("🔴 Продать", "man_sell")], nav_btns("man_pick_back")[0]]
    await update.callback_query.edit_message_text(f"⚡ {symbol}\n{sig.action} {int(sig.confidence * 100)}%", reply_markup=kb(rows))

async def man_pick_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📊 Сигналы", "man_signals")], [btn("⚡ Кнопки", "man_quick")], nav_btns("man_pairs")[0]]
    await update.callback_query.edit_message_text("Пара: " + str(STATE["manual"]["symbol"]), reply_markup=kb(rows))

async def man_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered("15m", tp_mult=3.0)
        budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
        qty, lot = calc_lot(budget, sig.price, sig.stop_loss, portfolio.count_open())
        if qty <= 0: await update.callback_query.edit_message_text("Лот 0 (цель дня / бюджет / риск).", reply_markup=kb(nav_btns("start"))); return
        res = await _open_and_watch(ctx, symbol, "BUY", sig.price, qty, sig.stop_loss, sig.take_profit)
        if "error" in res: await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb(nav_btns("start"))); return
        await update.callback_query.edit_message_text(f"BUY {symbol} {round(qty*sig.price, 2)} USDT", reply_markup=kb(nav_btns("start")))
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    pos = portfolio.find_open(symbol)
    if not pos: await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
    try:
        async with aiohttp.ClientSession() as session:
            df = await fetch_candles(session, symbol, "1m", 30)
            price = float(df["close"].iloc[-1]) if df is not None else pos["entry"]
        trade = portfolio.close_position(price, pos["qty"], symbol)
        if "error" in trade: await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
        else: register_win(trade.get("pnl_abs", 0), symbol)
        await update.callback_query.edit_message_text(f"Закрыл {symbol} PnL {round(trade['pnl_pct'], 2)}%", reply_markup=kb(nav_btns("start")))
    except Exception as e: await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opens = portfolio.load().get("opens", [])
    if not opens: await update.callback_query.edit_message_text("Нет сделок.", reply_markup=kb(nav_btns("start"))); return
    total_pnl = 0.0; total_abs = 0.0; count = 0
    for pos in list(opens):
        try:
            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, pos["symbol"], "1m", 30)
                price = float(df["close"].iloc[-1]) if df is not None else pos["entry"]
            trade = portfolio.close_position(price, pos["qty"], pos["symbol"])
            STATE["trailing"]["active"][pos["symbol"]] = False; STATE["panic"]["active"][pos["symbol"]] = False
            if "error" not in trade:
                total_pnl += trade["pnl_pct"]; total_abs += trade.get("pnl_abs", 0); count += 1
                if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), pos["symbol"])
                else: register_win(trade.get("pnl_abs", 0), pos["symbol"])
        except Exception: pass
    await update.callback_query.edit_message_text(f"Закрыто {count}.\nИтог PnL {round(total_pnl, 2)}% ({round(total_abs, 2)} USDT)", reply_markup=kb(nav_btns("start")))

async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📅 День", "hist_day")], [btn("📆 Неделя", "hist_week")], [btn("🗓 Всё", "hist_all")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("Период:", reply_markup=kb(rows))

async def hist_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = update.callback_query.data.replace("hist_", "")
    rows = [[btn("🎮 Кнопки", f"hist_src_{period}_manual")], [btn("📊 Сигналы", f"hist_src_{period}_signal")], [btn("🤖 Авто", f"hist_src_{period}_auto")], [btn("📋 Всё", f"hist_src_{period}_all")], nav_btns("hist_menu")[0]]
    await update.callback_query.edit_message_text("Источник:", reply_markup=kb(rows))

async def hist_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.callback_query.data.replace("hist_src_", "").split("_")
    s = portfolio.stats_filtered(parts[0], parts[1])
    text = (f"📜 {parts[0]} / {parts[1]}\nСделок {s['total']}\n+ {s['wins']} / - {s['losses']}\nWinrate {round(s['winrate'], 1)}%\nPnL % {round(s['total_pnl'], 2)}\nPnL USDT {round(s['total_abs'], 2)}\nPF {round(s['pf'], 2)}")
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = STATE["metrics"]
    pf = m["gross_win"] / m["gross_loss"] if m["gross_loss"] > 0 else 0.0
    rf = m["current_pnl"] / m["max_dd"] if m["max_dd"] > 0 else 0.0
    wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0.0
    avg_w = m["gross_win"] / max(m["wins"], 1); avg_l = m["gross_loss"] / max(m["losses"], 1)
    text = (f"📊 Метрики\n\nСделок: {m['trades']}\nWinrate: {round(wr, 1)}%\nProfit Factor: {round(pf, 2)}\n"
            f"Recovery: {round(rf, 2)}\nPnL: ${round(m['current_pnl'], 2)}\nMax DD: ${round(m['max_dd'], 2)}\n"
            f"Ср. выигрыш: ${round(avg_w, 2)}\nСр. убыток: ${round(avg_l, 2)}\n"
            f"Серия: {m['consecutive_wins']}W / {m['consecutive_losses']}L\n\n" +
            ("✅ Работает" if pf >= 1.3 and rf >= 1.0 else "⚠️ Следи за метриками"))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]

    if "demo_amount" in pend:
        pend.pop("demo_amount")
        await update.message.reply_text("Демо отключено в этой версии.")
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
        if k == "work_time":
            hours = _parse_hours(txt); STATE["auto"]["work_hours"] = int(hours)
            pend.pop("auto"); await auto_ask_exchange(update, ctx); return
        
        if k == "my_coins":
            if txt.lower() in ["нет", "no", "0", "-"]:
                STATE["auto"]["my_coins"] = []
            else:
                raw_coins = re.split(r'[,;\s]+', txt.upper())
                coins = []
                for c in raw_coins:
                    if not c: continue
                    if c.endswith("USDT"): c = c[:-4]
                    coins.append(c + "/USDT")
                STATE["auto"]["my_coins"] = list(set(coins))
            pend.pop("auto")
            await auto_ex_start_scan(update, ctx)
            return

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
    return v * 24 if ("д" in low or "day" in low) else v

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    mapping = {
        "demo": demo, "auto_start": auto_start, "sig_skipped": sig_skipped,
        "man_menu": man_menu, "man_ex": man_ex, "man_pairs": man_pairs,
        "man_search": man_search, "man_signals": man_signals, "man_quick": man_quick,
        "man_pick_back": man_pick_back, "man_buy": man_buy, "man_sell": man_sell,
        "close_all": close_all, "hist_menu": hist_menu, "stats_menu": stats_menu,
    }

    if data.startswith("panic_sold_"): await panic_sold(update, ctx); return
    if data.startswith("sig_go_"): await sig_go(update, ctx); return
    if data in mapping: await mapping[data](update, ctx); return

    if data == "start":
        STATE["auto"]["active"] = False
        STATE["pending"] = {}
        STATE["auto"]["scan_msg_id"] = None
        await start(update, ctx); return

    if data == "auto_stop_signals":
        STATE["auto"]["active"] = False
        STATE["pending"] = {}
        STATE["auto"]["scan_msg_id"] = None
        await q.edit_message_text("🛑 Стоп.", reply_markup=kb([[btn("🏠 В меню", "start")]])); return

    if data.startswith("auto_ex_set_"): await auto_ex_set(update, ctx); return
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.CONTACT, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()