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

# --- НАСТРОЙКИ СКОРОСТИ ---
DAILY_TARGET_PCT = 0.05  
LOCK_IN_GIVEBACK = 0.015 
CONF_FLOOR = 0.58        # Снижено до 58% для частоты
MIN_ATR_PCT = 0.2        # Снижено до 0.2% (ночью волатильность ниже)
FEE_RT_PCT = 0.004
LIMIT_OFFSET = 0.001
MAX_TRADE_TIME_MIN = 120 

PANIC_DROP = 0.02 
PANIC_INTERVAL = 5
TRAIL_INTERVAL = 3 
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 20       
DAILY_DD_LIMIT = 0.05    
TOP_N_SCAN = 30          # Сканируем Топ-30, а не 100 (быстрее в 3 раза)
VOL_MIN_24H = 300000.0
CHUNK_SIZE = 15          # Пачки по 15 монет

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

def get_trade_params(work_hours):
    if work_hours <= 4:
        return {"tf": "5m", "conf": 0.58, "tp_mult": 2.0, "sl_mult": 1.0} 
    elif work_hours <= 12:
        return {"tf": "15m", "conf": 0.58, "tp_mult": 2.5, "sl_mult": 1.2} 
    else:
        return {"tf": "1h", "conf": 0.65, "tp_mult": 3.0, "sl_mult": 1.5}

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

def calc_lot(budget, price, sl, params, open_count):
    if budget <= 0 or price <= 0: return 0.0, 0.0
    diff = abs(price - sl)
    if diff <= 0: return 0.0, 0.0
    
    if STATE["auto"]["target_hit"]: base_risk = 0.01 
    else:
        if open_count >= 12: base_risk = 0.0075 
        elif open_count >= 6: base_risk = 0.015  
        else: base_risk = 0.025  
        
    cl = STATE["metrics"]["consecutive_losses"]
    base_risk *= (1.0 if cl <= 0 else (0.75 if cl == 1 else (0.5 if cl == 2 else 0.0)))
    if base_risk <= 0: return 0.0, 0.0
    
    qty = min((budget * base_risk) / diff, (budget * 0.15) / price) 
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

async def check_setup(session, symbol, params):
    loop = asyncio.get_event_loop()
    tf = params["tf"]
    try:
        df = await fetch_candles(session, symbol, tf, 100)
        if df is None or len(df) < 30: return None
        
        # Быстрая проверка волатильности
        atr = (df["high"] - df["low"]).rolling(14).mean().iloc[-1]
        if atr / df["close"].iloc[-1] * 100 < MIN_ATR_PCT: return None

        f = Friday(symbol=symbol, exchange_name="binance")
        df = await loop.run_in_executor(None, lambda: f.enrich(df, tf).dropna())
        if len(df) < 30: return None
        
        sig = await loop.run_in_executor(None, lambda: f.analyze_filtered(tf, tp_mult=params["tp_mult"]))
        
        if sig.confidence < CONF_FLOOR: return None
        if net_pct(sig) < 0.5: return None # Снизили до 0.5%
        
        return {"symbol": symbol, "signal": sig, "net": net_pct(sig), "tf": tf}
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
            change = abs(float(t.get("priceChangePercent") or 0)) # Берем изменение цены
            
            if qv < VOL_MIN_24H or price <= 0: continue
            
            # Сортируем по волатильности + объему
            pre.append({"symbol": sym_fmt, "vol": qv, "chg": change})
            
        pre.sort(key=lambda x: x["chg"] * (x["vol"] ** 0.5), reverse=True)
        return pre[:TOP_N_SCAN]
    except Exception: return []

async def _first_scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    scan_count = 0
    while STATE["auto"]["active"]:
        try:
            if STATE["day_halt"]:
                await asyncio.sleep(10); continue
            if is_sleeping(): await asyncio.sleep(5); continue

            if "signal" in STATE["pending"]: await asyncio.sleep(5); continue

            open_positions = portfolio.load().get("opens", [])
            if len(open_positions) >= MAX_POSITIONS: 
                await asyncio.sleep(10); continue
            
            my_coins = STATE["auto"].get("my_coins", [])
            params = STATE["auto"]["params"]
            
            async with aiohttp.ClientSession() as session:
                found = None
                
                if my_coins:
                    for sym in my_coins:
                        res = await check_setup(session, sym, params)
                        if res and res["signal"].action == "SELL":
                            found = res
                            break
                
                if not found:
                    candidates = await bg_scan_market(session, open_symbols=[p["symbol"] for p in open_positions])
                    for i in range(0, len(candidates), CHUNK_SIZE):
                        chunk = candidates[i:i+CHUNK_SIZE]
                        tasks = [check_setup(session, c["symbol"], params) for c in chunk]
                        results = await asyncio.gather(*tasks)
                        valid = [r for r in results if r and r["signal"].action == "BUY"]
                        if valid:
                            valid.sort(key=lambda x: x["signal"].confidence, reverse=True)
                            found = valid[0]
                            break
                        await asyncio.sleep(0.2)

                if found:
                    STATE["auto"]["last_found"] = found
                    sym_data = found["symbol"].replace("/", "_")
                    action_text = "Купить" if found["signal"].action == "BUY" else "Продать"
                    
                    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
                    total_pnl = get_total_pnl()
                    pnl_pct = (total_pnl / budget)*100 if budget > 0 else 0
                    peak_pct = STATE["metrics"]["peak_day_pnl"]*100
                    
                    text = (f"🎯 <b>Точка входа найдена</b>\n\n🪙 <b>{found['symbol']}</b>\n"
                            f"⏱ ТФ: <b>{found['tf']}</b>\n"
                            f"📊 Направление: <b>{found['signal'].action}</b>\n"
                            f"🎯 Уверенность: <b>{int(found['signal'].confidence * 100)}%</b>\n"
                            f"💰 Чистая: <b>+{round(found['net'], 2)}%</b>\n"
                            f"📈 День: {round(pnl_pct, 1)}% (Пик: {round(peak_pct, 1)}%)")
                    
                    rows = [[btn(f"✅ {action_text}", "sig_go_" + sym_data)], [btn("❌ Пропустил", "sig_skipped")]]
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
                    
                    qty, lot = calc_lot(budget, found["signal"].price, found["signal"].stop_loss, params, len(open_positions))
                    if qty <= 0:
                        await asyncio.sleep(5); continue
                    
                    STATE["pending"]["signal"] = {"action": found["signal"].action, "symbol": found["symbol"], "price": found["signal"].price, "lot": lot, "qty": qty, "sl": found["signal"].stop_loss, "tp": found["signal"].take_profit}
                    
                    for _ in range(15): 
                        if "signal" not in STATE["pending"]: break
                        await asyncio.sleep(2)
                    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
                    continue
                
                scan_count += 1
                if scan_count % 4 == 0 and chat_id:
                    try:
                        budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
                        total_pnl = get_total_pnl()
                        pnl_pct = (total_pnl / budget)*100 if budget > 0 else 0
                        peak_pct = STATE["metrics"]["peak_day_pnl"]*100
                        now_ts = int(time.time())
                        # Добавляем таймстамп, чтобы текст всегда менялся
                        msg_text = f"⏳ Сканирую Топ-{TOP_N_SCAN} на {params['tf']}...\nДень: {round(pnl_pct, 1)}% (Пик: {round(peak_pct, 1)}%)\nОбновлено: {now_ts}"
                        
                        if not STATE["auto"]["scan_msg_id"]:
                            msg = await app.bot.send_message(chat_id=chat_id, text=msg_text)
                            STATE["auto"]["scan_msg_id"] = msg.message_id
                        else:
                            await app.bot.edit_message_text(chat_id=chat_id, message_id=STATE["auto"]["scan_msg_id"], text=msg_text)
                    except Exception:
                        STATE["auto"]["scan_msg_id"] = None
                        
                await asyncio.sleep(1) # Ускорили цикл
        except Exception as e:
            print("scan err:", e)
            await asyncio.sleep(3)

# --- ОСТАЛЬНЫЕ ФУНКЦИИ (ТРЕЙЛИНГ, ПАНИКА, МЕНЮ) ---
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
                if held_min > MAX_TRADE_TIME_MIN:
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
                if p["symbol"] == symbol:
                    p["current_price"] = price
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
                
                if STATE["auto"]["target_hit"] and not STATE["trailing"]["tp1_hit"].get(symbol) and profit > 0.001:
                    partial_qty = pos2["qty"] * 0.5
                    trade = portfolio.close_position(price, partial_qty, symbol)
                    if "error" not in trade:
                        STATE["trailing"]["tp1_hit"][symbol] = True
                        register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"💰 {symbol} Сверхфиксация 50%: +{round(trade['pnl_pct'], 2)}%")
                elif not STATE["trailing"]["tp1_hit"].get(symbol) and profit >= R_pct and R_pct > 0:
                    partial_qty = pos2["qty"] * 0.5
                    trade = portfolio.close_position(price, partial_qty, symbol)
                    if "error" not in trade:
                        STATE["trailing"]["tp1_hit"][symbol] = True
                        register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"💰 {symbol} Взяли 50% профита: +{round(trade['pnl_pct'], 2)}%")
                
                if R_pct > 0:
                    if profit >= 1.5 * R_pct: new_sl = max(cur_sl, entry + R_abs)
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
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} Закрыт остаток: {round(trade['pnl_pct'], 2)}%")
                    break
            else:
                profit = (entry - best) / entry if entry else 0
                if STATE["auto"]["target_hit"] and not STATE["trailing"]["tp1_hit"].get(symbol) and profit > 0.001:
                    partial_qty = pos2["qty"] * 0.5
                    trade = portfolio.close_position(price, partial_qty, symbol)
                    if "error" not in trade:
                        STATE["trailing"]["tp1_hit"][symbol] = True
                        register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"💰 {symbol} Сверхфиксация 50%: +{round(trade['pnl_pct'], 2)}%")
                elif not STATE["trailing"]["tp1_hit"].get(symbol) and profit >= R_pct and R_pct > 0:
                    partial_qty = pos2["qty"] * 0.5
                    trade = portfolio.close_position(price, partial_qty, symbol)
                    if "error" not in trade:
                        STATE["trailing"]["tp1_hit"][symbol] = True
                        register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"💰 {symbol} Взяли 50% профита: +{round(trade['pnl_pct'], 2)}%")

                if R_pct > 0:
                    if profit >= 1.5 * R_pct: new_sl = min(cur_sl if cur_sl else entry * 2, entry - R_abs)
                    elif profit >= 0.5 * R_pct: new_sl = min(cur_sl if cur_sl else entry * 2, entry)
                if new_sl <= entry and best < entry:
                    trail = best + 0.2 * atr_v
                    if new_sl == 0 or trail < new_sl: new_sl = trail
                STATE["trailing"]["current_sl"][symbol] = new_sl
                
                if price >= new_sl:
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                        else: register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} Закрыт остаток: {round(trade['pnl_pct'], 2)}%")
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
            
            if STATE["auto"]["locked"]:
                async with aiohttp.ClientSession() as session:
                    df = await fetch_candles(session, symbol, "1m", 5)
                    price = float(df["close"].iloc[-1]) if df is not None else pos["entry"]
                trade = portfolio.close_position(price, pos["qty"], symbol)
                if "error" not in trade:
                    if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                    else: register_win(trade.get("pnl_abs", 0), symbol)
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛑 {symbol} Экстренное закрытие по храповику: {round(trade['pnl_pct'], 2)}%")
                break

            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, symbol, "1m", 60)
                if df is None or len(df) < 15: 
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
    action_word = "Купил" if sig["action"] == "BUY" else "Продал (в шорт)"
    await update.callback_query.edit_message_text(f"{action_word} {sig['symbol']} на {round(sig['lot'], 2)} USDT.\nИщу следующую...", reply_markup=kb([[btn("🏠 В меню", "start")]]))

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
    
    if STATE["auto"]["locked"]: auto_txt = "\n🛡 Храповик сработал! Прибыль зафиксирована."
    elif STATE["auto"]["target_hit"]: auto_txt = "\n🎯 Цель 5% взята. Работаем на максимум!"
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
    
    params = get_trade_params(STATE["auto"]["work_hours"])
    STATE["auto"]["params"] = params
    
    STATE["pending"]["auto"] = "my_coins"
    msg = f"Биржа выбрана. Стратегия: ТФ {params['tf']} (Цель +5%, лимит 20 сделок).\n\n<b>Какие монеты у тебя есть сейчас?</b>\nВпиши через запятую (например: BTC, ETH, ZRO). Если нет — напиши 'нет'."
    await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")), parse_mode="HTML")

async def auto_ex_start_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = True
    STATE["auto"]["scanning"] = False
    STATE["pending"] = {}
    msg = "⏳ Портфель учтен. Ищу лучшие точки входа..."
    if update.message: await update.message.reply_text(msg, reply_markup=kb([]))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb([]))
    asyncio.create_task(_first_scan_loop(ctx.application))

# ... (Функции demo, man_menu, man_ex, man_pairs, man_page, man_search, man_pick, man_signals, man_quick, man_pick_back, man_buy, man_sell, close_all, hist_menu, hist_period, hist_show, stats_menu, on_text, on_button, main остаются как в предыдущей версии)