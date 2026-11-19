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

# --- ИДЕАЛЬНЫЕ НАСТРОЙКИ АВТОПИЛОТА ---
CONF_SCORE_MIN = 65      # Минимальный рейтинг для входа (из 100)
RISK_PER_TRADE = 0.015   # Строго 1.5% от бюджета на сделку
MAX_POSITIONS = 3        # Не более 3 сделок одновременно
FEE_RT_PCT = 0.004       # Комиссия туда-обратно
TRAIL_INTERVAL = 3       # Проверка цены каждые 3 секунды
PANIC_INTERVAL = 5       # Проверка на панику каждые 5 секунд
TIMEOUT_MINUTES = 120    # Если висит 2 часа и не идет в плюс — закрываем

STATE = {
    "balance": 0.0, "daily_budget": 0.0, "tz": "UTC",
    "metrics": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "current_pnl": 0.0},
    "auto": {"active": False, "scanning": False, "exchange": "binance", "scan_msg_id": None, "last_found": None},
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0},
    "trailing": {"tasks": {}, "active": {}},
}

def kb(rows): return InlineKeyboardMarkup(rows)
def btn(text, data): return InlineKeyboardButton(text, callback_data=data)
def nav_btns(back_data): return [[btn("⬅️ На шаг назад", back_data), btn("🏠 В меню", "start")]]

def register_loss(pnl=0.0):
    m = STATE["metrics"]; m["trades"] += 1; m["losses"] += 1; m["gross_loss"] += abs(pnl); m["current_pnl"] += pnl

def register_win(pnl=0.0):
    m = STATE["metrics"]; m["trades"] += 1; m["wins"] += 1; m["gross_win"] += abs(pnl); m["current_pnl"] += pnl

def calc_lot(budget, price, sl):
    if budget <= 0 or price <= 0: return 0.0, 0.0
    diff = abs(price - sl)
    if diff <= 0: return 0.0, 0.0
    # Риск строго 1.5%
    qty = min((budget * RISK_PER_TRADE) / diff, (budget * 0.20) / price) 
    return qty, qty * price

async def fetch_candles(session, symbol, tf, limit=100):
    ex = STATE["auto"]["exchange"] if STATE["auto"]["active"] else STATE["manual"]["exchange"]
    try:
        if ex == "okx":
            okx_tf = tf.upper() if tf in ["1h", "4h"] else tf.lower()
            okx_sym = symbol.replace("/", "-")
            url = f"https://www.okx.com/api/v5/market/candles?instId={okx_sym}&bar={okx_tf}&limit={limit}"
            async with session.get(url, timeout=3.0) as resp:
                if resp.status != 200: return None
                data = (await resp.json()).get("data", [])
                if not data: return None
                data.reverse()
                df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
                df = df[["ts", "open", "high", "low", "close", "vol"]].rename(columns={"vol": "volume"})
                for c in ["open", "high", "low", "close", "volume"]: df[c] = df[c].astype(float)
                df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms")
                return df
        else:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol.replace('/', '')}&interval={tf}&limit={limit}"
            async with session.get(url, timeout=3.0) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"])
                df = df[["ts", "open", "high", "low", "close", "volume"]]
                for c in ["open", "high", "low", "close", "volume"]: df[c] = df[c].astype(float)
                df["ts"] = pd.to_datetime(df["ts"], unit="ms")
                return df
    except Exception: return None

async def analyze_coin(session, symbol):
    """Идеальный скоринг: Тренд + Объем + Паттерн + Импульс"""
    try:
        # 1. Глобальный тренд (15m)
        df15 = await fetch_candles(session, symbol, "15m", 50)
        if df15 is None or len(df15) < 50: return None
        ema9_15 = df15["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21_15 = df15["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        if ema9_15 < ema21_15: return None # Только лонги по тренду

        # 2. Локальный анализ (5m)
        df5 = await fetch_candles(session, symbol, "5m", 50)
        if df5 is None or len(df5) < 30: return None
        
        last = df5.iloc[-1]
        prev = df5.iloc[-2]
        
        score = 0
        # Тренд EMA (5m)
        if last["close"] > df5["close"].ewm(span=9, adjust=False).mean().iloc[-1]: score += 15
        # MACD
        exp1 = df5["close"].ewm(span=12, adjust=False).mean()
        exp2 = df5["close"].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        macd_sig = macd.ewm(span=9, adjust=False).mean()
        if macd.iloc[-1] > macd_sig.iloc[-1]: score += 20
        # RSI
        delta = df5["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        if 50 < rsi.iloc[-1] < 75: score += 20
        # Объем
        vol_avg = df5["volume"].rolling(20).mean().iloc[-1]
        if last["volume"] > vol_avg * 1.5: score += 25
        # Паттерн (Бычье поглощение)
        if (last["close"] > last["open"]) and (prev["close"] < prev["open"]) and (last["close"] > prev["open"]): score += 20

        if score < CONF_SCORE_MIN: return None

        # Глобальный фон (BTC)
        btc_df = await fetch_candles(session, "BTC/USDT", "15m", 20)
        if btc_df is not None and len(btc_df) >= 20:
            if btc_df["close"].ewm(span=9, adjust=False).mean().iloc[-1] < btc_df["close"].ewm(span=21, adjust=False).mean().iloc[-1]: return None

        price = last["close"]
        atr = (df5["high"] - df5["low"]).rolling(14).mean().iloc[-1]
        
        # Стоп строго под минимум свечи (или 1.5 ATR)
        sl = min(last["low"] * 0.998, price - atr * 1.2)
        risk = price - sl
        
        if risk <= 0 or (risk / price) < 0.005: return None # Стоп не может быть слишком близко
        
        # Тейки: 1R (для фиксации 50%) и 2.5R (для трейлинга)
        tp1 = price + risk
        tp2 = price + (risk * 2.5)
        
        class Sig: pass
        sig = Sig()
        sig.price = price; sig.action = "BUY"; sig.stop_loss = sl; sig.take_profit = tp2; sig.tp1 = tp1
        
        return {"symbol": symbol, "signal": sig, "score": score, "tf": "5m"}
    except Exception: return None

async def bg_scan_market(session, open_symbols=[]):
    ex = STATE["auto"]["exchange"]
    try:
        pre = []
        if ex == "okx":
            async with session.get("https://www.okx.com/api/v5/market/tickers?instType=SPOT", timeout=3.0) as resp:
                raw = (await resp.json()).get("data", [])
            for t in raw:
                sym = t.get("instId", "")
                if not sym.endswith("-USDT") or sym in ["BTC-USDT", "ETH-USDT"]: continue
                sym_fmt = sym.replace("-", "/")
                if sym_fmt in open_symbols: continue
                qv = float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
                if qv < 500000: continue
                pre.append({"symbol": sym_fmt, "vol": qv})
        else:
            async with session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=3.0) as resp:
                raw = await resp.json()
            for t in raw:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT") or sym in ["BTCUSDT", "ETHUSDT"]: continue
                sym_fmt = sym[:-4] + "/USDT"
                if sym_fmt in open_symbols: continue
                qv = float(t.get("quoteVolume") or 0)
                if qv < 500000: continue
                pre.append({"symbol": sym_fmt, "vol": qv})
        pre.sort(key=lambda x: x["vol"], reverse=True)
        return pre[:50] # Сканируем Топ-50
    except Exception: return []

async def _first_scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while STATE["auto"]["active"]:
        try:
            if "signal" in STATE["pending"]: await asyncio.sleep(5); continue
            open_positions = portfolio.load().get("opens", [])
            if len(open_positions) >= MAX_POSITIONS: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession() as session:
                candidates = await bg_scan_market(session, open_symbols=[p["symbol"] for p in open_positions])
                found = None
                
                for i in range(0, len(candidates), 10):
                    chunk = candidates[i:i+10]
                    tasks = [analyze_coin(session, c["symbol"]) for c in chunk]
                    results = await asyncio.gather(*tasks)
                    valid = [r for r in results if r]
                    if valid:
                        valid.sort(key=lambda x: x["score"], reverse=True)
                        found = valid[0]
                        break

                if found:
                    STATE["auto"]["last_found"] = found
                    sym_data = found["symbol"].replace("/", "_")
                    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
                    qty, lot = calc_lot(budget, found["signal"].price, found["signal"].stop_loss)
                    
                    if qty <= 0: await asyncio.sleep(5); continue

                    text = (f"🏆 <b>Идеальный сетап (Score {found['score']})</b>\n\n"
                            f"🪙 <b>{found['symbol']}</b>\n"
                            f"📊 ТФ: <b>5m</b> (Тренд 15m ВВЕРХ)\n"
                            f"🔖 Вход: <b>{fmt_price(found['signal'].price)}</b>\n"
                            f"💵 Лот: <b>{round(lot, 2)} USDT</b>\n"
                            f"🛡 Стоп: <b>{fmt_price(found['signal'].stop_loss)}</b>\n"
                            f"🎯 Тейк 1 (50%): <b>{fmt_price(found['signal'].tp1)}</b>\n"
                            f"🎯 Тейк 2 (Трейлинг): <b>{fmt_price(found['signal'].take_profit)}</b>")
                    
                    rows = [[btn("✅ Я купил", "sig_go_" + sym_data)], [btn("❌ Пропустил", "sig_skipped")]]
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows), parse_mode="HTML")
                    
                    STATE["pending"]["signal"] = {
                        "action": "BUY", "symbol": found["symbol"], "price": found["signal"].price, 
                        "lot": lot, "qty": qty, "sl": found["signal"].stop_loss, 
                        "tp": found["signal"].take_profit, "tp1": found["signal"].tp1
                    }
                    
                    for _ in range(15): 
                        if "signal" not in STATE["pending"]: break
                        await asyncio.sleep(2)
                    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
                    continue
                
                await asyncio.sleep(3)
        except Exception as e:
            print("scan err:", e)
            await asyncio.sleep(3)

# --- УМНЫЙ ТРЕЙЛИНГ (Фиксация 50% на 1R) ---
async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    tp1_hit = False
    
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(TRAIL_INTERVAL)
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break

            async with aiohttp.ClientSession() as session:
                df = await fetch_candles(session, symbol, "1m", 5)
                if df is None: continue
                price = float(df["close"].iloc[-1])
            
            portfolio.update_peak(price, symbol)
            pos2 = portfolio.find_open(symbol)
            if not pos2: break
            
            entry = float(pos2["entry"])
            sl = float(pos2.get("sl", 0)) or entry
            tp1 = float(pos2.get("tp1", 0)) or entry
            tp2 = float(pos2.get("tp", 0)) or (entry * 1.05)
            best = float(pos2.get("peak", entry))
            
            # 1. Частичная фиксация на Тейк 1 (50% позиции)
            if not tp1_hit and price >= tp1:
                partial_qty = pos2["qty"] * 0.5
                trade = portfolio.close_position(price, partial_qty, symbol)
                if "error" not in trade:
                    tp1_hit = True
                    register_win(trade.get("pnl_abs", 0))
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"💰 {symbol} Взяли 50% (Тейк 1): +{round(trade['pnl_pct'], 2)}%\nСтоп переведен в БЕЗУБЫТОК.")
                    # Перевод стопа в безубыток
                    data = portfolio.load()
                    for p in data.get("opens", []):
                        if p["symbol"] == symbol: p["sl"] = entry
                    portfolio.save(data)
                    sl = entry

            # 2. Трейлинг остатка (0.2 ATR от пика)
            if price > best:
                best = price
            
            if best > entry and tp1_hit: # Трейлим только после взятия Тейк 1
                atr_v = (df["high"] - df["low"]).mean()
                new_sl = best - (atr_v * 2.0)
                if new_sl > sl:
                    data = portfolio.load()
                    for p in data.get("opens", []):
                        if p["symbol"] == symbol: p["sl"] = new_sl
                    portfolio.save(data)
                    sl = new_sl

            # 3. Выход по стопу или Тейк 2
            if price <= sl or price >= tp2:
                reason = "Трейлинг/Стоп" if price <= sl else "Тейк 2"
                trade = portfolio.close_position(price, pos2["qty"], symbol)
                if "error" not in trade:
                    if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
                    else: register_win(trade.get("pnl_abs", 0))
                    if chat_id: await app.bot.send_message(chat_id=chat_id, text=f"🛡 {symbol} Остаток закрыт ({reason}): {round(trade['pnl_pct'], 2)}%")
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
                if drop >= 0.02: # Экстренный откат на 2%
                    if chat_id:
                        await app.bot.send_message(chat_id=chat_id, text=f"🔴 ЗАКРЫВАЙ {symbol}!\nОткат: -{round(drop*100, 2)}%",
                            reply_markup=kb([[btn(f"🔴 ПРОДАЛ {symbol}", "panic_sold_" + symbol.replace("/", "_"))]]))
        except Exception: pass
        await asyncio.sleep(PANIC_INTERVAL)
    STATE["panic"]["active"][symbol] = False

async def _open_and_watch(ctx, symbol, side, price, qty, sl, tp, tp1):
    if portfolio.count_open() >= MAX_POSITIONS: return {"error": f"максимум {MAX_POSITIONS} позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal", sl=sl, tp=tp, tp1=tp1)
    if "error" in res: return res
    
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(_trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(_panic_loop(ctx.application, symbol))
    return res

# --- ХЕНДЛЕРЫ БОТА ---
async def sig_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig: await update.callback_query.answer("Устарел.", show_alert=True); return
    res = await _open_and_watch(ctx, sig["symbol"], sig["action"], sig["price"], sig["qty"], sig["sl"], sig["tp"], sig["tp1"])
    del STATE["pending"]["signal"]
    if "error" in res: await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb([[btn("🏠 В меню", "start")]])); return
    await update.callback_query.edit_message_text(f"BUY {sig['symbol']} на {round(sig['lot'], 2)} USDT.\nЖду Тейк 1.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

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
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0))
            else: register_win(trade.get("pnl_abs", 0))
    await update.callback_query.edit_message_text("Закрыто " + sym + ".", reply_markup=kb(nav_btns("start")))

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opens = portfolio.load().get("opens", [])
    pos_txt = ("\n📌 Позиции: " + str(len(opens)) + "/" + str(MAX_POSITIONS)) if opens else ""
    auto_txt = "\n🤖 Авто: Работает (Score System)" if STATE["auto"]["active"] else ""
    m = STATE["metrics"]
    pf = m["gross_win"] / m["gross_loss"] if m["gross_loss"] > 0 else 0.0
    txt = "Пятница на связи."
    if m["trades"] > 0: txt += f"\n📊 PF {round(pf, 2)} · WR {round(m['wins'] / m['trades'] * 100, 1)}%"
    txt += auto_txt + pos_txt
    rows = [
        [btn("🤖 Автопилот", "auto_start")],
        [btn("✋ Ручной режим", "man_menu")],
        [btn("🎮 Демо", "demo")],
        [btn("❌ Закрыть все", "close_all")]
    ]
    if update.message: await update.message.reply_text(txt, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = False
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"] = {"auto": "balance"}
    msg = "Баланс (USDT)?"
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = f"Баланс {STATE['balance']}.\nБюджет на день?"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "exchange"
    rows = [[btn(name, "auto_ex_set_" + key)] for key, name in exchanges.EXCHANGES.items()]
    rows.append(nav_btns("start")[0])
    msg = "Бюджет принят. Выбери биржу:"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(rows))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    STATE["auto"]["active"] = True
    STATE["pending"] = {}
    msg = f"Биржа: {exchanges.EXCHANGES.get(key, key)}\n⏳ Ищу идеальные сетапы..."
    await update.callback_query.edit_message_text(msg)
    asyncio.create_task(_first_scan_loop(ctx.application))

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
            if "error" not in trade:
                total_pnl += trade["pnl_pct"]; total_abs += trade.get("pnl_abs", 0); count += 1
        except Exception: pass
    await update.callback_query.edit_message_text(f"Закрыто {count}.\nИтог PnL {round(total_pnl, 2)}% ({round(total_abs, 2)} USDT)", reply_markup=kb(nav_btns("start")))

# Ручной режим и Демо оставлены как заглушки для сохранения архитектуры
async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ручной режим (в разработке для этой версии).")
async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Демо режим (в разработке для этой версии).")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]
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

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "start":
        STATE["auto"]["active"] = False
        await start(update, ctx); return
    if data == "auto_start": await auto_start(update, ctx); return
    if data.startswith("auto_ex_set_"): await auto_ex_set(update, ctx); return
    if data.startswith("panic_sold_"): await panic_sold(update, ctx); return
    if data.startswith("sig_go_"): await sig_go(update, ctx); return
    if data == "sig_skipped": await sig_skipped(update, ctx); return
    if data == "close_all": await close_all(update, ctx); return
    if data == "man_menu": await man_menu(update, ctx); return
    if data == "demo": await demo(update, ctx); return

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