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
from friday import Friday, fmt_price
from chart import build_chart
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# --- НАСТРОЙКИ СНАЙПЕРА ---
CONF_FLOOR = 0.65 # Высокая уверенность
VOLUME_SPIKE_MULT = 2.0 # Объем должен быть в 2 раза выше среднего
MIN_ATR_PCT = 0.4 # Монета должна двигаться
FIXED_RISK = 0.015
MIN_NET_PCT = 0.2
FEE_RT_PCT = 0.004
LIMIT_OFFSET = 0.001
LIMIT_TIMEOUT = 60
MAX_TRADE_TIME_MIN = 20 # Закрываем, если висит больше 20 мин

PANIC_DROP = 0.02
PANIC_INTERVAL = 5
TRAIL_INTERVAL = 5
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 2
SCAN_INTERVAL = 15 # Сканируем каждые 15 секунд
DAILY_DD_LIMIT = 0.05
TOP_N_SCAN = 30
VOL_MIN_24H = 1000000.0

STATE = {
    "balance": 0.0, "daily_budget": 0.0, "tz": "UTC",
    "loss_streak": 0, "sleep_until": None,
    "day_started": datetime.date.today().isoformat(),
    "day_pnl_usdt": 0.0, "day_halt": False,
    "metrics": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "peak_pnl": 0.0, "current_pnl": 0.0, "max_dd": 0.0, "consecutive_wins": 0, "consecutive_losses": 0},
    "auto": {"active": False, "task": None, "exchange": "binance", "symbol": None, "have_coin": None, "coin_qty": 0.0, "last_found": None, "symbol_cooldown": {}, "work_hours": 2},
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"tasks": {}, "active": {}, "current_sl": {}},
}

def kb(rows): return InlineKeyboardMarkup(rows)
def btn(text, data): return InlineKeyboardButton(text, callback_data=data)
def nav_btns(back_data): return [[btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]]
def is_sleeping(): return STATE["sleep_until"] and datetime.datetime.utcnow() < STATE["sleep_until"]
def net_pct(sig): return (abs(sig.take_profit - sig.price) / sig.price - FEE_RT_PCT) * 100 if sig.price > 0 else 0.0

def register_loss(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl; STATE["loss_streak"] += 1
    m = STATE["metrics"]; m["trades"] += 1; m["losses"] += 1; m["gross_loss"] += abs(pnl); m["current_pnl"] += pnl
    dd = m["peak_pnl"] - m["current_pnl"]
    if dd > m["max_dd"]: m["max_dd"] = dd
    m["consecutive_wins"] = 0; m["consecutive_losses"] += 1
    if symbol: STATE["auto"]["symbol_cooldown"][symbol] = time.time() + 600
    if STATE["loss_streak"] >= LOSS_LIMIT: STATE["sleep_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLEEP_MIN); STATE["loss_streak"] = 0
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget > 0 and STATE["day_pnl_usdt"] <= -budget * DAILY_DD_LIMIT: STATE["day_halt"] = True

def register_win(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl; STATE["loss_streak"] = 0
    m = STATE["metrics"]; m["trades"] += 1; m["wins"] += 1; m["gross_win"] += abs(pnl); m["current_pnl"] += pnl
    if m["current_pnl"] > m["peak_pnl"]: m["peak_pnl"] = m["current_pnl"]
    m["consecutive_wins"] += 1; m["consecutive_losses"] = 0

def kelly_size(budget, price, sl, confidence):
    if budget <= 0 or price <= 0: return 0.0, 0.0, 0.0
    diff = abs(price - sl)
    if diff <= 0: return 0.0, 0.0, 0.0
    m = STATE["metrics"]
    if m["trades"] >= 50 and m["gross_loss"] > 0:
        p = m["wins"] / m["trades"]; b = (m["gross_win"] / max(m["wins"], 1)) / (m["gross_loss"] / max(m["losses"], 1))
        f = max(0.005, min(0.02, ((b * p - (1 - p)) / b) * 0.25))
    else: f = FIXED_RISK
    cl = m["consecutive_losses"]
    f *= (1.0 if cl <= 0 else (0.75 if cl == 1 else (0.5 if cl == 2 else 0.0)))
    if f <= 0: return 0.0, 0.0, 0.0
    return min((budget * f) / diff, (budget * 0.20) / price), (budget * f), f

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

async def bg_scan_market(session):
    try:
        async with session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=3.0) as resp:
            if resp.status != 200: return []
            raw = await resp.json()
        pre = []
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in ["BTCUSDT", "ETHUSDT"]: continue
            qv = float(t.get("quoteVolume") or 0)
            price = float(t.get("lastPrice") or 0)
            change = abs(float(t.get("priceChangePercent") or 0))
            if qv < VOL_MIN_24H or price <= 0 or change < 1.5: continue
            pre.append({"symbol": sym[:-4] + "/USDT", "vol": qv, "chg": change})
        pre.sort(key=lambda x: x["chg"] * (x["vol"] ** 0.5), reverse=True)
        return pre[:TOP_N_SCAN]
    except Exception: return []

async def process_batch(session, candidates):
    loop = asyncio.get_event_loop()
    tf_scan = "5m" 
    tp_mult = 1.5

    async def check_one(c):
        try:
            # 1. БЫСТРЫЙ ПРЕ-ФИЛЬТР (Только объем на 1m)
            df1 = await fetch_candles(session, c["symbol"], "1m", 30)
            if df1 is None or len(df1) < 20: return None
            vol_avg = df1["volume"].iloc[:-1].mean()
            if df1["volume"].iloc[-1] < vol_avg * VOLUME_SPIKE_MULT: return None # Нет всплеска - пропускаем

            # 2. ТЯЖЕЛЫЙ АНАЛИЗ (Только для прошедших)
            df = await fetch_candles(session, c["symbol"], tf_scan, 100)
            if df is None or len(df) < 50: return None
            
            # Тренд 15m
            df15 = await fetch_candles(session, c["symbol"], "15m", 50)
            if df15 is None: return None
            ema15_f = df15["close"].ewm(span=9, adjust=False).mean().iloc[-1]
            ema15_s = df15["close"].ewm(span=21, adjust=False).mean().iloc[-1]
            is_uptrend = ema15_f > ema15_s

            # Волатильность
            atr = (df["high"] - df["low"]).rolling(14).mean().iloc[-1]
            if atr / df["close"].iloc[-1] * 100 < MIN_ATR_PCT: return None

            # Friday
            f = Friday(symbol=c["symbol"], exchange_name="binance")
            df = await loop.run_in_executor(None, lambda: f.enrich(df, tf_scan).dropna())
            if len(df) < 30: return None
            sig = await loop.run_in_executor(None, lambda: f.analyze_filtered(tf_scan, tp_mult=tp_mult))
            
            if sig.confidence < CONF_FLOOR or net_pct(sig) < MIN_NET_PCT: return None
            if sig.action == "BUY" and not is_uptrend: return None
            if sig.action == "SELL" and is_uptrend: return None
            if sig.action == "SELL" and STATE["auto"]["have_coin"] == False: return None
            
            # RSI фильтр
            rsi_val = float(df["rsi"].iloc[-1])
            if sig.action == "BUY" and rsi_val > 65: return None
            if sig.action == "SELL" and rsi_val < 35: return None
            
            return {"symbol": c["symbol"], "signal": sig, "net": net_pct(sig), "tf": tf_scan}
        except Exception: return None
        
    tasks = [check_one(c) for c in candidates]
    res = await asyncio.gather(*tasks)
    valid = [r for r in res if r]
    valid.sort(key=lambda x: x["signal"].confidence, reverse=True)
    return valid

async def _first_scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    scan_count = 0
    while STATE["auto"]["active"]:
        try:
            if STATE["day_halt"] or is_sleeping(): await asyncio.sleep(3); continue
            async with aiohttp.ClientSession() as session:
                candidates = await bg_scan_market(session)
                if not candidates: await asyncio.sleep(2); continue
                
                results = await process_batch(session, candidates)
                found = results[0] if results else None
                
                if found:
                    STATE["auto"]["last_found"] = found
                    sym_data = found["symbol"].replace("/", "_")
                    text = (f"🎯 <b>Снайперский вход</b>\n\n🪙 <b>{found['symbol']}</b>\n"
                            f"⏱ ТФ: <b>{found['tf']}</b>\n"
                            f"📊 Направление: <b>{found['signal'].action}</b>\n"
                            f"🎯 Уверенность: <b>{int(found['signal'].confidence * 100)}%</b>\n"
                            f"💰 Чистая: <b>+{round(found['net'], 2)}%</b>")
                    rows = [[btn("✅ Берём", "forecast_go_" + sym_data)], [btn("🔄 Ещё скан", "forecast_rescan")], [btn("🛑 СТОП", "auto_stop_signals")]]
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
                    return
                
                scan_count += 1
                if scan_count % 2 == 0 and chat_id:
                    try:
                        msg = f"⏳ Ищу идеальный вход (уверенность >{int(CONF_FLOOR*100)}%).\nСканов: {scan_count}. Ждем всплеск объема..."
                        await app.bot.edit_message_text(chat_id=chat_id, message_id=app.bot_data.get("scan_msg_id"), text=msg)
                    except Exception: pass
                await asyncio.sleep(1)
        except Exception as e:
            print("scan err:", e)
            await asyncio.sleep(3)

async def scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    loop = asyncio.get_event_loop()
    while STATE["auto"]["active"]:
        try:
            if STATE["day_halt"] or is_sleeping() or "signal" in STATE["pending"]: await asyncio.sleep(SCAN_INTERVAL); continue
            symbol = STATE["auto"]["symbol"]
            if not symbol: await asyncio.sleep(SCAN_INTERVAL); continue

            tf_scan = "5m"
            tp_mult = 1.5

            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, symbol, tf_scan, 100)
                if df is None: await asyncio.sleep(SCAN_INTERVAL); continue
                f = Friday(symbol=symbol, exchange_name="binance")
                
                df = await loop.run_in_executor(None, lambda: f.enrich(df, tf_scan).dropna())
                sig = await loop.run_in_executor(None, lambda: f.analyze_filtered(tf_scan, tp_mult=tp_mult))
                
                if sig.confidence < CONF_FLOOR or net_pct(sig) < MIN_NET_PCT: await asyncio.sleep(SCAN_INTERVAL); continue
                if sig.action == "SELL" and STATE["auto"]["have_coin"] == False: await asyncio.sleep(SCAN_INTERVAL); continue
                
                budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
                qty, risk_usdt, f_pct = kelly_size(budget, sig.price, sig.stop_loss, sig.confidence)
                if qty <= 0: await asyncio.sleep(SCAN_INTERVAL); continue

                limit_price = sig.price * (1 - LIMIT_OFFSET) if sig.action == "BUY" else sig.price * (1 + LIMIT_OFFSET)
                STATE["pending"]["signal"] = {"action": sig.action, "symbol": symbol, "price": sig.price, "lot": qty*sig.price, "qty": qty, "sl": sig.stop_loss, "tp": sig.take_profit, "open_time": time.time()}
                
                text = (f"🎯 {symbol} · {tf_scan}\n{sig.action} @ {fmt_price(sig.price)}\n🎯 Уверенность: {int(sig.confidence * 100)}%\n"
                        f"💵 Лот: {round(qty*sig.price, 2)} USDT\n🛡 Риск: {round(risk_usdt, 2)} USDT\n"
                        f"Стоп {fmt_price(sig.stop_loss)} / Тейк {fmt_price(sig.take_profit)}\n"
                        f"🔖 Лимит: {fmt_price(limit_price)}\n\nℹ️ Ордера ставишь сам.")
                
                rows = [[btn("✅ Исполнил", "sig_buy_signal" if sig.action == "BUY" else "sig_sell_signal")], [btn("❌ Пропустил", "sig_skipped")]]
                if chat_id: await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))
                await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("scan err:", e)
            await asyncio.sleep(SCAN_INTERVAL)

async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(TRAIL_INTERVAL)
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break
            
            # Тайм-стоп
            open_time = float(pos.get("open_time", time.time()))
            if time.time() - open_time > MAX_TRADE_TIME_MIN * 60:
                f = Friday(symbol=symbol, exchange_name="binance")
                price = float(f.fetch("1m", 5).iloc[-1]["close"])
                trade = portfolio.close_position(price, pos["qty"], symbol)
                if "error" not in trade:
                    if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                    else: register_win(trade.get("pnl_abs", 0), symbol)
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"⏰ {symbol} Time Stop: {round(trade['pnl_pct'], 2)}%")
                break

            f = Friday(symbol=symbol, exchange_name="binance")
            df = f.fetch("1m", 30)
            price = float(df["close"].iloc[-1])
            atr_v = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
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
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} PnL: {round(trade['pnl_pct'], 2)}% ({round(trade['pnl_abs'], 2)} USDT)")
                    break
            else:
                profit = (entry - best) / entry if entry else 0
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
                        if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} PnL: {round(trade['pnl_pct'], 2)}% ({round(trade['pnl_abs'], 2)} USDT)")
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
            f = Friday(symbol=symbol, exchange_name="binance")
            df = f.enrich(f.fetch("1m", 60), "1m").dropna().reset_index(drop=True)
            if len(df) < 15: await asyncio.sleep(PANIC_INTERVAL); continue
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
    if portfolio.count_open() >= MAX_POSITIONS: return {"error": "максимум позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal", sl=sl, tp=tp, open_time=time.time())
    if "error" in res: return res
    STATE["trailing"]["current_sl"][symbol] = sl
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(_trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(_panic_loop(ctx.application, symbol))
    return res

# ... (Далее идут все стандартные функции sig_buy_signal, sig_sell_signal, start, auto_start, on_button и т.д. Они не менялись, вставь их из предыдущего полного кода, они рабочие)