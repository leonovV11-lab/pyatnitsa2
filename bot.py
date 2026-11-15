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

CONF_FLOOR = 0.55
SENIOR_PENALTY = 0.10
OFI_PENALTY = 0.10
OFI_BOOST = 0.05
KELLY_CAP = 0.03
KELLY_MIN = 0.005
KELLY_FRACTION = 0.25
KELLY_MIN_TRADES = 20

MIN_NET_PCT = 0.2
FEE_RT_PCT = 0.004
TP_MIN_DIST = 0.004
SPREAD_MAX_PCT = 0.15
DEPTH_MIN_USDT = 50000
ATR_MIN_PCT = 0.3
ATR_MAX_PCT = 3.0
FUNDING_EXTREME = 0.1
VOL_GROWTH_MIN = 0.8

PANIC_DROP = 0.02
PANIC_INTERVAL = 5
TRAIL_INTERVAL = 30
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 2
SCAN_INTERVAL = 20
DAILY_DD_LIMIT = 0.05
SIGNAL_COOLDOWN = 300
SYMBOL_COOLDOWN = 600
CONF_SIGNAL = 0.85
TFS_ALL = ["1m", "5m", "15m", "1h", "4h"]
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

_EX_CACHE = {}

STATE = {
    "balance": 0.0, "daily_budget": 0.0, "city": "", "tz": "UTC",
    "loss_streak": 0, "sleep_until": None,
    "day_started": datetime.date.today().isoformat(),
    "day_pnl_usdt": 0.0, "day_halt": False,
    "metrics": {
        "trades": 0, "wins": 0, "losses": 0,
        "gross_win": 0.0, "gross_loss": 0.0,
        "peak_pnl": 0.0, "current_pnl": 0.0, "max_dd": 0.0,
        "consecutive_wins": 0, "consecutive_losses": 0,
    },
    "auto": {
        "active": False, "task": None, "exchange": "okx", "tf": "15m",
        "work_hours": 24, "multi_day": False,
        "symbol": None, "have_coin": None, "coin_qty": 0.0,
        "last_signal_key": None, "last_signal_ts": 0,
        "symbol_cooldown": {},
        "tf_resolved": None,
    },
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"tasks": {}, "active": {}, "current_sl": {}},
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

def register_loss(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl
    STATE["loss_streak"] += 1
    m = STATE["metrics"]
    m["trades"] += 1
    m["losses"] += 1
    m["gross_loss"] += abs(pnl)
    m["current_pnl"] += pnl
    dd = m["peak_pnl"] - m["current_pnl"]
    if dd > m["max_dd"]: m["max_dd"] = dd
    m["consecutive_wins"] = 0
    m["consecutive_losses"] += 1
    if symbol:
        STATE["auto"]["symbol_cooldown"][symbol] = time.time() + SYMBOL_COOLDOWN
    if STATE["loss_streak"] >= LOSS_LIMIT:
        STATE["sleep_until"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLEEP_MIN)
        STATE["loss_streak"] = 0
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget > 0 and STATE["day_pnl_usdt"] <= -budget * DAILY_DD_LIMIT:
        STATE["day_halt"] = True

def register_win(pnl=0.0, symbol=None):
    STATE["day_pnl_usdt"] += pnl
    STATE["loss_streak"] = 0
    m = STATE["metrics"]
    m["trades"] += 1
    m["wins"] += 1
    m["gross_win"] += abs(pnl)
    m["current_pnl"] += pnl
    if m["current_pnl"] > m["peak_pnl"]: m["peak_pnl"] = m["current_pnl"]
    m["consecutive_wins"] += 1
    m["consecutive_losses"] = 0

def exposure_multiplier():
    cl = STATE["metrics"].get("consecutive_losses", 0)
    if cl <= 0: return 1.0
    if cl == 1: return 0.75
    if cl == 2: return 0.5
    return 0.0

def adaptive_conf_floor():
    base = CONF_FLOOR
    cl = STATE["metrics"].get("consecutive_losses", 0)
    if cl >= 2: base += 0.05
    return base

def kelly_size(budget, price, sl, confidence):
    if budget <= 0 or price <= 0: return 0.0, 0.0
    diff = abs(price - sl)
    if diff <= 0: return 0.0, 0.0
    m = STATE["metrics"]
    if m["trades"] >= KELLY_MIN_TRADES and m["gross_loss"] > 0:
        p = m["wins"] / m["trades"]
        q = 1.0 - p
        avg_w = m["gross_win"] / max(m["wins"], 1)
        avg_l = m["gross_loss"] / max(m["losses"], 1)
        b = avg_w / avg_l if avg_l > 0 else 1.0
        f = (b * p - q) / b if b > 0 else 0.0
        f = max(KELLY_MIN, min(KELLY_CAP, f * KELLY_FRACTION))
    else:
        if confidence >= 0.85: f = 0.03
        elif confidence >= 0.75: f = 0.02
        else: f = 0.01
    f *= exposure_multiplier()
    if f <= 0: return 0.0, 0.0
    qty_risk = (budget * f) / diff
    qty_cap = (budget * 0.20) / price
    qty = min(qty_risk, qty_cap)
    lot = qty * price
    return qty, lot, f, budget

def tf_from_hours(hours):
    h = int(hours) if hours else 24
    if h <= 2: return "1m"
    if h <= 6: return "5m"
    if h <= 12: return "15m"
    return "1h"

def tf_label(tf):
    return {"1m": "1 минута", "5m": "5 минут", "15m": "15 минут",
            "1h": "1 час", "4h": "4 часа", "auto": "Авто"}.get(tf, tf)

def net_pct(sig):
    if sig.price <= 0: return 0.0
    return (abs(sig.take_profit - sig.price) / sig.price - FEE_RT_PCT) * 100

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

async def validate_symbol(sym_input):
    s = sym_input.strip().upper()
    if "/" not in s: s = s + "/USDT"
    if not s.endswith("/USDT"): s = s + "/USDT"
    try:
        f = Friday(symbol=s, exchange_name="binance")
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: f.fetch("15m", 30))
        if df is None or len(df) < 10: return None
        return s
    except Exception:
        return None

async def fetch_orderbook(session, symbol, limit=50):
    url = "https://api.binance.com/api/v3/depth?symbol=" + symbol.replace("/", "") + "&limit=" + str(limit)
    try:
        async with session.get(url, timeout=3.0) as resp:
            if resp.status != 200: return None
            return await resp.json()
    except Exception:
        return None

def calc_ofi(orderbook):
    if not orderbook: return None
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])
    if not bids or not asks: return None
    bid_vol = sum(float(p) * float(q) for p, q in bids)
    ask_vol = sum(float(p) * float(q) for p, q in asks)
    total = bid_vol + ask_vol
    if total <= 0: return None
    imbalance = bid_vol / total
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    spread_pct = (best_ask - best_bid) / mid * 100 if mid else 0
    low = mid * 0.995
    high = mid * 1.005
    bid_depth = sum(float(p) * float(q) for p, q in bids if float(p) >= low)
    ask_depth = sum(float(p) * float(q) for p, q in asks if float(p) <= high)
    depth_usdt = min(bid_depth, ask_depth)
    return {
        "imbalance": imbalance, "spread_pct": spread_pct, "depth_usdt": depth_usdt,
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
    }

def calc_cvd(df, n=5):
    if df is None or len(df) < n + 1: return 0.0
    try:
        tail = df.tail(n)
        if "taker_base" not in tail.columns: return 0.0
        buy = float((tail["taker_base"] * tail["close"]).sum())
        sell = float(((tail["volume"] - tail["taker_base"]) * tail["close"]).sum())
        return buy - sell
    except Exception:
        return 0.0

async def fetch_funding(session, symbol):
    sym = symbol.replace("/", "")
    try:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=" + sym
        async with session.get(url, timeout=3.0) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return float(data.get("lastFundingRate", 0)) * 100
    except Exception:
        return None

def check_freshness(df, tf):
    try:
        if df is None or len(df) < 2: return False
        last_ts = df["ts"].iloc[-1]
        dur = TF_SECONDS.get(tf, 900)
        now_utc = datetime.datetime.utcnow()
        age = (now_utc - last_ts.to_pydatetime()).total_seconds()
        return age < 2 * dur
    except Exception:
        return True

def check_candle_closed(df, tf):
    try:
        if df is None or len(df) < 2: return False
        last_ts = df["ts"].iloc[-1]
        dur = TF_SECONDS.get(tf, 900)
        now_utc = datetime.datetime.utcnow()
        age = (now_utc - last_ts.to_pydatetime()).total_seconds()
        return age >= 0.1 * dur
    except Exception:
        return True

def check_volume_growth(df):
    try:
        if len(df) < 3: return True
        v = [float(df["volume"].iloc[-i]) for i in (1, 2, 3)]
        return v[0] >= v[1] * VOL_GROWTH_MIN and v[1] >= v[2] * VOL_GROWTH_MIN
    except Exception:
        return True

def check_atr_band(df, price):
    try:
        if "atr" not in df.columns or price <= 0: return True
        atr_pct = float(df["atr"].iloc[-1]) / price * 100
        return ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT
    except Exception:
        return True

def mtf_senior(tf):
    if tf in ("1m", "5m"): return "15m"
    if tf == "15m": return "4h"
    return None

def mtf_junior(tf):
    if tf == "15m": return "5m"
    if tf == "1h": return "15m"
    if tf == "5m": return "1m"
    return None

async def check_senior_alignment(f, senior_tf, action):
    try:
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: f.fetch(senior_tf, 60))
        df = f.enrich(df, senior_tf).dropna().reset_index(drop=True)
        if len(df) < 20: return True
        last = df.iloc[-1]
        ef = float(last["ema_fast"]); es = float(last["ema_slow"])
        if action == "BUY" and ef < es: return False
        if action == "SELL" and ef > es: return False
        return True
    except Exception:
        return True

async def check_junior_alignment(f, junior_tf, action):
    try:
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: f.fetch(junior_tf, 40))
        df = f.enrich(df, junior_tf).dropna().reset_index(drop=True)
        if len(df) < 10: return True
        last = df.iloc[-1]
        ef = float(last["ema_fast"]); es = float(last["ema_slow"])
        if action == "BUY" and ef < es: return False
        if action == "SELL" and ef > es: return False
        return True
    except Exception:
        return True

async def pick_dynamic_tf(symbol):
    f = Friday(symbol=symbol, exchange_name="binance")
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, lambda: f.fetch("5m", 60))
        df = f.enrich(df, "5m").dropna()
        if len(df) < 20: return "15m"
        atr_pct = float(df["atr"].iloc[-1]) / float(df["close"].iloc[-1]) * 100
    except Exception:
        return "15m"
    if atr_pct < 0.5: return "15m"
    if atr_pct <= 2.0: return "5m"
    return "1m"

async def send_signal(app, sig, symbol, tf, ofi=None, cvd=0.0, funding=None, atr_pct=0.0):
    conf = sig.confidence
    n = net_pct(sig)
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    qty, lot, f_pct, _ = kelly_size(budget, sig.price, sig.stop_loss, conf)
    if qty <= 0 or lot <= 0:
        print("reject qty/lot 0")
        return False
    if sig.action == "BUY":
        limit_price = sig.price * (1 - 0.0005)
    else:
        limit_price = sig.price * (1 + 0.0005)
    net_usdt = lot * (n / 100.0)
    STATE["pending"]["signal"] = {
        "action": sig.action, "symbol": symbol, "price": sig.price,
        "lot": lot, "qty": qty, "sl": sig.stop_loss, "tp": sig.take_profit,
    }
    STATE["auto"]["last_signal_key"] = symbol + ":" + sig.action
    STATE["auto"]["last_signal_ts"] = time.time()

    ofi_txt = ""
    if ofi:
        ofi_txt = "\n📊 OFI: " + str(round(ofi["imbalance"], 3)) + " · CVD $" + str(int(cvd))
    fund_txt = ""
    if funding is not None:
        fund_txt = "\n💸 Funding: " + str(round(funding, 4)) + "%"
    expo_txt = ""
    em = exposure_multiplier()
    if em < 1.0:
        expo_txt = "\n⚠️ Экспозиция: " + str(int(em * 100)) + "% (серия убытков)"

    text = (
        "🎯 " + symbol + " · " + tf_label(tf) + "\n" +
        sig.action + " @ " + fmt_price(sig.price) + "\n" +
        "🎯 Уверенность: " + str(int(conf * 100)) + "%\n" +
        "💵 Лот: " + str(round(lot, 2)) + " USDT\n" +
        "🛡 Риск: " + str(round(budget * f_pct, 2)) + " USDT (" + str(round(f_pct * 100, 1)) + "%)\n" +
        "Стоп " + fmt_price(sig.stop_loss) + " / Тейк " + fmt_price(sig.take_profit) + "\n" +
        "💰 Чистая ~" + str(round(n, 2)) + "% (~$" + str(round(net_usdt, 2)) + ")\n" +
        "🔖 Лимит: " + fmt_price(limit_price) +
        ofi_txt + fund_txt + expo_txt + "\n" +
        "ℹ️ Сигнал информационный. Ордера ставишь сам."
    )
    rows = [
        [btn("✅ Купил по сигналу", "sig_buy_signal" if sig.action == "BUY" else "sig_sell_signal")],
        [btn("✏️ Своя сумма", "sig_buy_custom" if sig.action == "BUY" else "sig_sell_custom")],
        [btn("❌ Пропустил", "sig_skipped")],
        [btn("🛑 СТОП", "auto_stop_signals")],
    ]
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))
    return True

async def scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while STATE["auto"]["active"]:
        try:
            day_rollover()
            if STATE["day_halt"] or is_sleeping() or "signal" in STATE["pending"]:
                await asyncio.sleep(SCAN_INTERVAL); continue
            if portfolio.count_open() >= MAX_POSITIONS:
                await asyncio.sleep(SCAN_INTERVAL); continue
            if exposure_multiplier() <= 0:
                await asyncio.sleep(SCAN_INTERVAL); continue
            symbol = STATE["auto"]["symbol"]
            if not symbol:
                await asyncio.sleep(SCAN_INTERVAL); continue

            tf = STATE["auto"]["tf"]
            if tf == "auto":
                tf = await pick_dynamic_tf(symbol)
                STATE["auto"]["tf_resolved"] = tf
            else:
                STATE["auto"]["tf_resolved"] = tf

            f = Friday(symbol=symbol, exchange_name="binance")
            loop = asyncio.get_event_loop()
            try:
                df = await loop.run_in_executor(None, lambda: f.fetch(tf, 100))
                df = f.enrich(df, tf).dropna().reset_index(drop=True)
                if len(df) < 30:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                sig = f.analyze_filtered(tf, tp_mult=3.0)
            except Exception as e:
                print("analyze err:", e)
                await asyncio.sleep(SCAN_INTERVAL); continue

            if not check_freshness(df, tf): await asyncio.sleep(SCAN_INTERVAL); continue
            if not check_candle_closed(df, tf): await asyncio.sleep(SCAN_INTERVAL); continue

            floor = adaptive_conf_floor()
            if sig.confidence < floor: await asyncio.sleep(SCAN_INTERVAL); continue

            senior = mtf_senior(tf)
            if senior:
                if not await check_senior_alignment(f, senior, sig.action):
                    sig.confidence -= SENIOR_PENALTY
                    if sig.confidence < floor:
                        await asyncio.sleep(SCAN_INTERVAL); continue

            junior = mtf_junior(tf)
            if junior:
                if not await check_junior_alignment(f, junior, sig.action):
                    await asyncio.sleep(SCAN_INTERVAL); continue

            if STATE["auto"]["have_coin"] is False and sig.action == "SELL":
                await asyncio.sleep(SCAN_INTERVAL); continue

            if not sig.stop_loss or sig.stop_loss <= 0 or sig.stop_loss == sig.price:
                await asyncio.sleep(SCAN_INTERVAL); continue

            tp_dist = abs(sig.take_profit - sig.price) / sig.price
            if tp_dist < TP_MIN_DIST: await asyncio.sleep(SCAN_INTERVAL); continue

            n = net_pct(sig)
            if n < MIN_NET_PCT: await asyncio.sleep(SCAN_INTERVAL); continue

            atr_pct = float(df["atr"].iloc[-1]) / sig.price * 100 if sig.price > 0 else 0
            if not check_atr_band(df, sig.price): await asyncio.sleep(SCAN_INTERVAL); continue
            if not check_volume_growth(df): await asyncio.sleep(SCAN_INTERVAL); continue

            async with aiohttp.ClientSession() as session:
                ob = await fetch_orderbook(session, symbol)
                funding = await fetch_funding(session, symbol)
            if not ob: await asyncio.sleep(SCAN_INTERVAL); continue
            ofi = calc_ofi(ob)
            if not ofi: await asyncio.sleep(SCAN_INTERVAL); continue
            if ofi["spread_pct"] > SPREAD_MAX_PCT: await asyncio.sleep(SCAN_INTERVAL); continue
            if ofi["depth_usdt"] < DEPTH_MIN_USDT: await asyncio.sleep(SCAN_INTERVAL); continue

            cvd = calc_cvd(df, 5)
            if sig.action == "BUY" and cvd <= 0: await asyncio.sleep(SCAN_INTERVAL); continue
            if sig.action == "SELL" and cvd >= 0: await asyncio.sleep(SCAN_INTERVAL); continue

            if sig.action == "BUY":
                if ofi["imbalance"] < 0.45: sig.confidence -= OFI_PENALTY
                elif ofi["imbalance"] > 0.55: sig.confidence += OFI_BOOST
            else:
                if ofi["imbalance"] > 0.55: sig.confidence -= OFI_PENALTY
                elif ofi["imbalance"] < 0.45: sig.confidence += OFI_BOOST
            if sig.confidence < floor: await asyncio.sleep(SCAN_INTERVAL); continue

            if funding is not None and abs(funding) > FUNDING_EXTREME:
                await asyncio.sleep(SCAN_INTERVAL); continue

            key = symbol + ":" + sig.action
            if (key == STATE["auto"].get("last_signal_key")
                    and time.time() - STATE["auto"].get("last_signal_ts", 0) < SIGNAL_COOLDOWN):
                await asyncio.sleep(SCAN_INTERVAL); continue

            cd = STATE["auto"]["symbol_cooldown"].get(symbol, 0)
            if time.time() < cd: await asyncio.sleep(SCAN_INTERVAL); continue

            await send_signal(app, sig, symbol, tf, ofi=ofi, cvd=cvd, funding=funding, atr_pct=atr_pct)
            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("scan_loop err:", e)
            await asyncio.sleep(SCAN_INTERVAL)

async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    live_pnl_sent = False
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(TRAIL_INTERVAL)
        try:
            pos = portfolio.find_open(symbol)
            if not pos: break
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.fetch("1m", 30)
            price = float(df["close"].iloc[-1])
            atr_v = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
            portfolio.update_peak(price, symbol)
            pos2 = portfolio.find_open(symbol)
            if not pos2: break
            entry = float(pos2["entry"]); side = pos2["side"]
            init_sl = float(pos2.get("sl", 0)) or entry
            R_abs = abs(entry - init_sl)
            R_pct = R_abs / entry if entry else 0
            best = float(pos2.get("peak", entry))
            cur_sl = STATE["trailing"]["current_sl"].get(symbol, init_sl)
            new_sl = cur_sl

            if side == "BUY":
                profit = (best - entry) / entry if entry else 0
                if R_pct > 0:
                    if profit >= 2.0 * R_pct: new_sl = max(cur_sl, entry + R_abs)
                    elif profit >= 1.5 * R_pct: new_sl = max(cur_sl, entry + 0.5 * R_abs)
                    elif profit >= 1.0 * R_pct: new_sl = max(cur_sl, entry)
                if new_sl >= entry and best > entry:
                    trail = best - 0.3 * atr_v
                    if trail > new_sl: new_sl = trail
                STATE["trailing"]["current_sl"][symbol] = new_sl
                pnl_now = portfolio.current_pnl(price, symbol)
                if pnl_now and pnl_now["pnl_pct"] >= 0.5 and not live_pnl_sent and chat_id:
                    live_pnl_sent = True
                    await app.bot.send_message(chat_id=chat_id, text="📈 " + symbol + " +" + str(round(pnl_now["pnl_pct"], 2)) + "% (~$" + str(round(pnl_now["pnl_abs"], 2)) + ")")
                if price <= new_sl:
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                        else: register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id:
                            await app.bot.send_message(chat_id=chat_id, text="🛡 " + symbol + " PnL: " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)")
                    break
            else:
                profit = (entry - best) / entry if entry else 0
                if R_pct > 0:
                    if profit >= 2.0 * R_pct: new_sl = min(cur_sl if cur_sl else entry * 2, entry - R_abs)
                    elif profit >= 1.5 * R_pct: new_sl = min(cur_sl if cur_sl else entry * 2, entry - 0.5 * R_abs)
                    elif profit >= 1.0 * R_pct: new_sl = min(cur_sl if cur_sl else entry * 2, entry)
                if new_sl <= entry and best < entry:
                    trail = best + 0.3 * atr_v
                    if new_sl == 0 or trail < new_sl: new_sl = trail
                STATE["trailing"]["current_sl"][symbol] = new_sl
                if price >= new_sl:
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
                        else: register_win(trade.get("pnl_abs", 0), symbol)
                        if chat_id:
                            await app.bot.send_message(chat_id=chat_id, text="🛡 " + symbol + " PnL: " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)")
                    break
        except Exception as e:
            print("trail err:", e)
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
            if len(df) < 15:
                await asyncio.sleep(PANIC_INTERVAL); continue
            price = float(df["close"].iloc[-1])
            async with aiohttp.ClientSession() as session:
                ob = await fetch_orderbook(session, symbol)
            ofi = calc_ofi(ob) if ob else None
            cvd_recent = calc_cvd(df, 5)
            cvd_prev = calc_cvd(df.iloc[:-5], 5)
            cvd_drop = cvd_recent < 0 and cvd_recent < cvd_prev * 0.5
            obi_bearish = ofi and ofi["imbalance"] < 0.35
            pnl = portfolio.current_pnl(price, symbol)
            if pnl and pnl["side"] == "BUY":
                peak = float(pnl.get("peak", pnl["entry"]))
                drop = (peak - price) / peak if peak else 0
                if drop >= PANIC_DROP and (cvd_drop or obi_bearish):
                    reason = "CVD" if cvd_drop else "OFI"
                    if chat_id:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text="🔴 ЗАКРЫВАЙ " + symbol + "! (" + reason + ")\nОткат от пика: -" + str(round(drop * 100, 2)) + "%",
                            reply_markup=kb([[btn("🔴 ПРОДАЛ " + symbol, "panic_sold_" + symbol.replace("/", "_"))]]),
                        )
        except Exception: pass
        await asyncio.sleep(PANIC_INTERVAL)
    STATE["panic"]["active"][symbol] = False

async def _open_and_watch(ctx, symbol, side, price, qty, sl, tp):
    if portfolio.count_open() >= MAX_POSITIONS: return {"error": "максимум позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal", sl=sl, tp=tp)
    if "error" in res: return res
    STATE["trailing"]["current_sl"][symbol] = sl
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(_trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(_panic_loop(ctx.application, symbol))
    return res

async def sig_buy_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True); return
    res = await _open_and_watch(ctx, sig["symbol"], "BUY", sig["price"], sig["qty"], sig["sl"], sig["tp"])
    del STATE["pending"]["signal"]
    if "error" in res:
        await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb([[btn("🏠 В меню", "start")]])); return
    await update.callback_query.edit_message_text("Купил " + sig["symbol"] + " на " + str(round(sig["lot"], 2)) + " USDT.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_sell_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True); return
    pos = portfolio.find_open(sig["symbol"])
    if not pos:
        del STATE["pending"]["signal"]
        await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
    try:
        f = Friday(symbol=sig["symbol"], exchange_name=STATE["auto"]["exchange"])
        price = float(f.fetch("1m", 5).iloc[-1]["close"])
    except Exception:
        price = sig["price"]
    trade = portfolio.close_position(price, pos["qty"], sig["symbol"])
    STATE["trailing"]["active"][sig["symbol"]] = False
    STATE["panic"]["active"][sig["symbol"]] = False
    del STATE["pending"]["signal"]
    if "error" in trade:
        await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
    if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), sig["symbol"])
    else: register_win(trade.get("pnl_abs", 0), sig["symbol"])
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

async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "signal" in STATE["pending"]: del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Пропустил.", reply_markup=kb(nav_btns("start")))

async def panic_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = update.callback_query.data.replace("panic_sold_", "").replace("_", "/")
    STATE["panic"]["active"][sym] = False
    STATE["trailing"]["active"][sym] = False
    pos = portfolio.find_open(sym)
    if pos:
        try:
            f = Friday(symbol=sym, exchange_name=STATE["auto"]["exchange"])
            price = float(f.fetch("1m", 5).iloc[-1]["close"])
        except Exception:
            price = pos["entry"]
        trade = portfolio.close_position(price, pos["qty"], sym)
        if "error" not in trade:
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), sym)
            else: register_win(trade.get("pnl_abs", 0), sym)
    await update.callback_query.edit_message_text("Закрыто " + sym + ".", reply_markup=kb(nav_btns("start")))

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    day_rollover()
    opens = portfolio.load().get("opens", [])
    pos_txt = ("\n📌 Позиции: " + str(len(opens)) + "/" + str(MAX_POSITIONS)) if opens else ""
    auto_txt = ""
    if STATE["auto"]["active"]:
        sym = STATE["auto"]["symbol"] or "—"
        auto_txt = "\n🤖 Авто: " + str(sym) + " · " + tf_label(STATE["auto"].get("tf_resolved") or STATE["auto"]["tf"])
    m = STATE["metrics"]
    pf = m["gross_win"] / m["gross_loss"] if m["gross_loss"] > 0 else 0.0
    txt = "Пятница на связи."
    if is_sleeping(): txt += "\n😴 Пауза: " + str(sleep_left_min()) + " мин"
    if STATE["day_halt"]: txt += "\n🛑 Дневной лимит"
    if m["trades"] > 0:
        txt += "\n📊 PF " + str(round(pf, 2)) + " · WR " + str(round(m["wins"] / m["trades"] * 100, 1)) + "%"
    txt += auto_txt + pos_txt
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
    if STATE["auto"]["task"]:
        STATE["auto"]["task"].cancel()
        STATE["auto"]["task"] = None
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"].update({
        "symbol": None, "have_coin": None, "coin_qty": 0.0,
        "last_signal_key": None, "last_signal_ts": 0, "multi_day": False,
        "tf_resolved": None,
    })
    STATE["pending"]["auto"] = "balance"
    msg = "Баланс (USDT)?"
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "budget"
    msg = "Баланс " + str(STATE["balance"]) + ".\nБюджет на день?"
    if update.message: await update.message.reply_text(msg, reply_markup=kb(nav_btns("start")))
    else: await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("start")))

async def auto_after_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = "Бюджет " + str(STATE["daily_budget"]) + ".\nСколько работаем? ('2 часа', '1 день')"
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
    STATE["pending"]["wait_symbol"] = True
    await update.callback_query.edit_message_text(
        "Биржа: " + exchanges.EXCHANGES.get(key, key) + "\n\nВпиши монету (BTC, ETH, SOL):",
        reply_markup=kb(nav_btns("auto_start")))

async def auto_ask_has_coin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = STATE["auto"]["symbol"]
    tf_now = STATE["auto"].get("tf_resolved") or STATE["auto"]["tf"]
    rows = [
        [btn("Нет", "hascoins_no"), btn("Да", "hascoins_yes")],
        [btn("⏱ Сменить ТФ (" + tf_label(tf_now) + ")", "tf_bad")],
        nav_btns("start")[0],
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Монета: <b>" + str(sym) + "</b>\nТФ: <b>" + tf_label(tf_now) + "</b>\n\nЕсть у тебя " + str(sym) + "?",
            reply_markup=kb(rows), parse_mode="HTML")
    else:
        await update.message.reply_text(
            "Монета: <b>" + str(sym) + "</b>\nТФ: <b>" + tf_label(tf_now) + "</b>\n\nЕсть у тебя " + str(sym) + "?",
            reply_markup=kb(rows), parse_mode="HTML")

async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["have_coin"] = False
    STATE["auto"]["coin_qty"] = 0.0
    STATE["auto"]["active"] = True
    await update.callback_query.edit_message_text(
        "Ок, только BUY.\nСканирую " + str(STATE["auto"]["symbol"]) + "...",
        reply_markup=kb([[btn("🛑 СТОП", "auto_stop_signals")]]))
    if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
        STATE["auto"]["task"] = asyncio.create_task(scan_loop(ctx.application))

async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["wait_qty"] = True
    await update.callback_query.edit_message_text(
        "Сколько " + str(STATE["auto"]["symbol"]) + " у тебя?", reply_markup=kb(nav_btns("start")))

async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🔄 Авто", "auto_tf_auto")],
        [btn("1m", "auto_tf_1m"), btn("5m", "auto_tf_5m")],
        [btn("15m", "auto_tf_15m"), btn("1h", "auto_tf_1h")],
        [btn("4h", "auto_tf_4h"), btn("✏️ Своё", "auto_tf_custom")],
        nav_btns("start")[0],
    ]
    if update.callback_query: await update.callback_query.edit_message_text("ТФ:", reply_markup=kb(rows))
    else: await update.message.reply_text("ТФ:", reply_markup=kb(rows))

async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tf = update.callback_query.data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text("Впиши ТФ (3m, 45m, 2h):", reply_markup=kb(nav_btns("tf_bad"))); return
    STATE["auto"]["tf"] = tf
    if tf == "auto":
        STATE["auto"]["tf_resolved"] = await pick_dynamic_tf(STATE["auto"]["symbol"]) if STATE["auto"]["symbol"] else None
    else:
        STATE["auto"]["tf_resolved"] = tf
    await auto_ask_has_coin(update, ctx)

async def auto_go_after_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await auto_ask_has_coin(update, ctx)

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("🚀 Найти монету", "demo_scan")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("🎮 Демо.", reply_markup=kb(rows))

async def demo_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Сканирую...")
    best = None
    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]:
        try:
            f = Friday(symbol=sym, exchange_name="binance")
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(None, lambda: f.fetch("5m", 100))
            df = f.enrich(df, "5m").dropna().reset_index(drop=True)
            if len(df) < 20: continue
            sig = f.analyze_filtered("5m", tp_mult=3.0)
            if best is None or sig.confidence > best["signal"].confidence:
                best = {"symbol": sym, "signal": sig, "price": sig.price}
        except Exception: continue
    if not best:
        await update.callback_query.edit_message_text("Не нашёл.", reply_markup=kb(nav_btns("demo"))); return
    STATE["demo"]["symbol"] = best["symbol"]
    STATE["demo"]["buy_price"] = best["price"]
    n = net_pct(best["signal"])
    text = ("🎮 ДЕМО\n" + best["symbol"] + "\n" + best["signal"].action + " @ " + fmt_price(best["price"]) +
            "\nУверенность: " + str(int(best["signal"].confidence * 100)) + "%\nЧистая: +" + str(round(n, 2)) + "%")
    rows = [[btn("✅ Купил", "demo_buy")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def demo_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["demo"]["symbol"] = None
    await update.callback_query.edit_message_text("Ок.", reply_markup=kb([[btn("🏠 Демо", "demo")]]))

async def demo_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько купил?", reply_markup=kb(nav_btns("demo")))

async def man_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("🏦 " + exchanges.EXCHANGES.get(STATE["manual"]["exchange"], "биржа"), "man_ex")],
        [btn("📈 Пары", "man_pairs")],
        [btn("🔍 Поиск", "man_search")],
        nav_btns("start")[0],
    ]
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
        except Exception as e:
            await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_menu"))); return
    await _render_pairs(update, ctx)

async def _render_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pairs = STATE["manual"]["pairs"]; page = STATE["manual"]["page"]
    start = page * 50; chunk = pairs[start:start + 50]
    rows = []; row = []
    for sym in chunk:
        row.append(btn(sym.replace("/USDT", ""), "man_pick_" + sym.replace("/", "_")))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    nav = []
    if page > 0: nav.append(btn("⬅️", "man_page_" + str(page - 1)))
    nav.append(btn(str(page + 1), "noop"))
    if start + 50 < len(pairs): nav.append(btn("➡️", "man_page_" + str(page + 1)))
    rows.append(nav)
    rows.append([btn("🔍 Поиск", "man_search"), btn("⬅️ Меню", "man_menu")])
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
        sig = f.analyze_filtered(STATE["auto"]["tf"] if STATE["auto"]["tf"] != "auto" else "15m", tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
    n = net_pct(sig)
    text = ("📊 " + symbol + " · СИГНАЛ\n" + sig.action + " @ " + fmt_price(sig.price) +
            "\nУверенность: " + str(int(sig.confidence * 100)) + "%\nЧистая: +" + str(round(n, 2)) + "%" +
            "\nСтоп " + fmt_price(sig.stop_loss) + "\nТейк " + fmt_price(sig.take_profit))
    try:
        path = build_chart(symbol, "15m", sig)
        with open(path, "rb") as photo:
            await ctx.bot.send_photo(chat_id=update.callback_query.message.chat_id, photo=photo, caption=text, reply_markup=kb(nav_btns("man_pick_back")))
        await update.callback_query.delete_message()
    except Exception:
        await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol: return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        sig = f.analyze_filtered("15m", tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("man_pick_back"))); return
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
        sig = f.analyze_filtered("15m", tp_mult=3.0)
        budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
        qty, lot, _, _ = kelly_size(budget, sig.price, sig.stop_loss, sig.confidence)
        if qty <= 0:
            await update.callback_query.edit_message_text("Лот 0 (бюджет/риск).", reply_markup=kb(nav_btns("start"))); return
        res = await _open_and_watch(ctx, symbol, "BUY", sig.price, qty, sig.stop_loss, sig.take_profit)
        if "error" in res:
            await update.callback_query.edit_message_text("Ошибка: " + res["error"], reply_markup=kb(nav_btns("start"))); return
        await update.callback_query.edit_message_text("BUY " + symbol + " " + str(round(lot, 2)) + " USDT", reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    pos = portfolio.find_open(symbol)
    if not pos:
        await update.callback_query.edit_message_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        price = float(f.fetch("1m", 30).iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"], symbol)
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
        if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), symbol)
        else: register_win(trade.get("pnl_abs", 0), symbol)
        await update.callback_query.edit_message_text("Закрыл " + symbol + " PnL " + str(round(trade["pnl_pct"], 2)) + "%", reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e), reply_markup=kb(nav_btns("start")))

async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opens = portfolio.load().get("opens", [])
    if not opens:
        await update.callback_query.edit_message_text("Нет сделок.", reply_markup=kb(nav_btns("start"))); return
    total_pnl = 0.0; total_abs = 0.0; count = 0
    for pos in list(opens):
        try:
            f = Friday(symbol=pos["symbol"], exchange_name=STATE["manual"]["exchange"])
            price = float(f.fetch("1m", 30).iloc[-1]["close"])
            trade = portfolio.close_position(price, pos["qty"], pos["symbol"])
            STATE["trailing"]["active"][pos["symbol"]] = False
            STATE["panic"]["active"][pos["symbol"]] = False
            if "error" not in trade:
                total_pnl += trade["pnl_pct"]
                total_abs += trade.get("pnl_abs", 0)
                count += 1
                if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), pos["symbol"])
                else: register_win(trade.get("pnl_abs", 0), pos["symbol"])
        except Exception: pass
    await update.callback_query.edit_message_text(
        "Закрыто " + str(count) + ".\nИтог PnL " + str(round(total_pnl, 2)) + "% (" + str(round(total_abs, 2)) + " USDT)",
        reply_markup=kb(nav_btns("start")))

async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📅 День", "hist_day")], [btn("📆 Неделя", "hist_week")], [btn("🗓 Всё", "hist_all")], nav_btns("start")[0]]
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
    raw = update.callback_query.data.replace("hist_src_", ""); parts = raw.split("_")
    period, source = parts[0], parts[1]
    s = portfolio.stats_filtered(period, source)
    text = ("📜 " + period + " / " + source + "\n" +
            "Сделок " + str(s["total"]) + "\n" +
            "+ " + str(s["wins"]) + " / - " + str(s["losses"]) + "\n" +
            "Winrate " + str(round(s["winrate"], 1)) + "%\n" +
            "PnL % " + str(round(s["total_pnl"], 2)) + "\n" +
            "PnL USDT " + str(round(s["total_abs"], 2)) + "\n" +
            "PF " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = STATE["metrics"]
    pf = m["gross_win"] / m["gross_loss"] if m["gross_loss"] > 0 else 0.0
    rf = m["current_pnl"] / m["max_dd"] if m["max_dd"] > 0 else 0.0
    wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0.0
    avg_w = m["gross_win"] / max(m["wins"], 1)
    avg_l = m["gross_loss"] / max(m["losses"], 1)
    text = ("📊 Метрики\n\n" +
            "Сделок: " + str(m["trades"]) + "\n" +
            "Winrate: " + str(round(wr, 1)) + "%\n" +
            "Profit Factor: " + str(round(pf, 2)) + "\n" +
            "Recovery: " + str(round(rf, 2)) + "\n" +
            "PnL: $" + str(round(m["current_pnl"], 2)) + "\n" +
            "Max DD: $" + str(round(m["max_dd"], 2)) + "\n" +
            "Ср. выигрыш: $" + str(round(avg_w, 2)) + "\n" +
            "Ср. убыток: $" + str(round(avg_l, 2)) + "\n" +
            "Серия: " + str(m["consecutive_wins"]) + "W / " + str(m["consecutive_losses"]) + "L\n\n" +
            ("✅ Работает" if pf >= 1.3 and rf >= 1.0 else "⚠️ Следи за метриками"))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    pend = STATE["pending"]

    if "wait_symbol" in pend:
        sym = await validate_symbol(txt)
        if not sym:
            await update.message.reply_text("Не нашёл пару. Пример: BTC, ETH, SOL.", reply_markup=kb(nav_btns("auto_start"))); return
        pend.pop("wait_symbol")
        STATE["auto"]["symbol"] = sym
        default_tf = tf_from_hours(STATE["auto"].get("work_hours", 24))
        STATE["auto"]["tf"] = default_tf
        STATE["auto"]["tf_resolved"] = default_tf
        await auto_ask_has_coin(update, ctx)
        return

    if "wait_qty" in pend:
        try: qty = float(txt)
        except Exception:
            await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
        pend.pop("wait_qty")
        STATE["auto"]["have_coin"] = True
        STATE["auto"]["coin_qty"] = qty
        STATE["auto"]["active"] = True
        await update.message.reply_text(
            "Ок, " + str(qty) + " " + str(STATE["auto"]["symbol"]) + ".\nСканирую...",
            reply_markup=kb([[btn("🛑 СТОП", "auto_stop_signals")]]))
        if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
            STATE["auto"]["task"] = asyncio.create_task(scan_loop(ctx.application))
        return

    if "demo_amount" in pend:
        try: amount = float(txt)
        except Exception:
            await update.message.reply_text("Число.", reply_markup=kb(nav_btns("demo"))); return
        pend.pop("demo_amount")
        sym = STATE["demo"]["symbol"]; bp = STATE["demo"]["buy_price"]
        try:
            f = Friday(symbol=sym, exchange_name=STATE["manual"]["exchange"])
            cp = float(f.fetch("1m", 5).iloc[-1]["close"])
        except Exception: cp = bp
        pnl_pct = (cp - bp) / bp if bp else 0
        pnl = amount * pnl_pct
        res = "📈 +" if pnl >= 0 else "📉 "
        await update.message.reply_text(
            "🎮 ДЕМО " + str(sym) + "\n" + res + str(round(pnl, 2)) + " USDT (" + str(round(pnl_pct * 100, 2)) + "%)",
            reply_markup=kb([[btn("🏠 Демо", "demo")]]))
        return

    if "wait_custom_amount" in pend:
        sd = pend["wait_custom_amount"]
        try: amount = float(txt)
        except Exception:
            await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
        pend.pop("wait_custom_amount")
        if sd["action"] == "BUY":
            qty = amount / sd["price"] if sd["price"] else 0
            res = await _open_and_watch(ctx, sd["symbol"], "BUY", sd["price"], qty, sd["sl"], sd["tp"])
            if "error" in res:
                await update.message.reply_text("Ошибка: " + res["error"], reply_markup=kb(nav_btns("start"))); return
            await update.message.reply_text("Купил " + sd["symbol"] + " на " + str(amount) + " USDT.", reply_markup=kb([[btn("🏠 В меню", "start")]]))
        else:
            pos = portfolio.find_open(sd["symbol"])
            if not pos:
                await update.message.reply_text("Нет позиции.", reply_markup=kb(nav_btns("start"))); return
            qty = min(amount / sd["price"] if sd["price"] else 0, pos["qty"])
            trade = portfolio.close_position(sd["price"], qty, sd["symbol"])
            STATE["trailing"]["active"][sd["symbol"]] = False
            STATE["panic"]["active"][sd["symbol"]] = False
            if "error" in trade:
                await update.message.reply_text("Ошибка: " + trade["error"], reply_markup=kb(nav_btns("start"))); return
            if trade["pnl_pct"] < 0: register_loss(trade.get("pnl_abs", 0), sd["symbol"])
            else: register_win(trade.get("pnl_abs", 0), sd["symbol"])
            await update.message.reply_text(
                "PnL " + str(round(trade["pnl_pct"], 2)) + "% (" + str(round(trade["pnl_abs"], 2)) + " USDT)",
                reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return

    if "auto" in pend:
        k = pend["auto"]
        if k == "balance":
            try: STATE["balance"] = float(txt)
            except Exception:
                await update.message.reply_text("Число.", reply_markup=kb(nav_btns("start"))); return
            pend.pop("auto"); await auto_after_balance(update, ctx); return
        if k == "budget":
            try: STATE["daily_budget"] = float(txt)
            except Exception:
                await update.message.reply_text("Число.", reply_markup=kb(nav_btns("auto_start"))); return
            pend.pop("auto"); await auto_after_budget(update, ctx); return
        if k == "work_time":
            hours = _parse_hours(txt)
            STATE["auto"]["work_hours"] = int(hours)
            STATE["auto"]["multi_day"] = ("д" in txt.lower() or "day" in txt.lower())
            pend.pop("auto"); await auto_ask_exchange(update, ctx); return
        if k == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf):
                await update.message.reply_text("5m, 1h.", reply_markup=kb(nav_btns("tf_bad"))); return
            STATE["auto"]["tf"] = tf
            STATE["auto"]["tf_resolved"] = tf
            pend.pop("auto")
            await auto_ask_has_coin(update, ctx)
            return

    if "manual" in pend:
        if pend["manual"] == "search":
            ticker = txt.upper(); found = None
            for sym in STATE["manual"]["pairs"]:
                if sym.startswith(ticker + "/"): found = sym; break
            pend.pop("manual")
            if not found:
                await update.message.reply_text("Не нашёл.", reply_markup=kb(nav_btns("man_menu"))); return
            STATE["manual"]["symbol"] = found
            rows = [[btn("📊 Сигналы", "man_signals")], [btn("⚡ Кнопки", "man_quick")], nav_btns("man_pairs")[0]]
            await update.message.reply_text("Пара: " + found, reply_markup=kb(rows)); return

def _parse_hours(txt):
    low = txt.lower(); m = re.search(r"(\d+(\.\d+)?)", low)
    if not m: return 1
    v = float(m.group(1))
    if "д" in low or "day" in low: return v * 24
    return v

async def auto_go_after_tf_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await auto_ask_has_coin(update, ctx)

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    mapping = {
        "demo": demo, "demo_scan": demo_scan, "demo_skip": demo_skip, "demo_buy": demo_buy,
        "auto_start": auto_start, "auto_go_after_tf": auto_go_after_tf_btn,
        "tf_bad": tf_bad, "hascoins_no": hascoins_no, "hascoins_yes": hascoins_yes,
        "sig_skipped": sig_skipped,
        "sig_buy_signal": sig_buy_signal, "sig_buy_custom": sig_buy_custom,
        "sig_sell_signal": sig_sell_signal, "sig_sell_custom": sig_sell_custom,
        "man_menu": man_menu, "man_ex": man_ex, "man_pairs": man_pairs,
        "man_search": man_search, "man_signals": man_signals, "man_quick": man_quick,
        "man_pick_back": man_pick_back, "man_buy": man_buy, "man_sell": man_sell,
        "close_all": close_all, "hist_menu": hist_menu, "stats_menu": stats_menu,
    }

    if data.startswith("panic_sold_"):
        await panic_sold(update, ctx); return
    if data in mapping:
        await mapping[data](update, ctx); return

    if data == "start":
        if STATE["auto"]["active"]:
            STATE["auto"]["active"] = False
            if STATE["auto"].get("task"):
                STATE["auto"]["task"].cancel()
                STATE["auto"]["task"] = None
        await start(update, ctx); return

    if data == "auto_stop_signals":
        STATE["auto"]["active"] = False
        if STATE["auto"].get("task"):
            STATE["auto"]["task"].cancel()
            STATE["auto"]["task"] = None
        await q.edit_message_text("🛑 Стоп.", reply_markup=kb([[btn("🏠 В меню", "start")]])); return

    if data.startswith("auto_ex_set_"):
        await auto_ex_set(update, ctx); return
    if data.startswith("auto_tf_"):
        await auto_tf_set(update, ctx); return
    if data.startswith("man_ex_set_"):
        await man_ex_set(update, ctx); return
    if data.startswith("man_page_"):
        await man_page(update, ctx); return
    if data.startswith("man_pick_"):
        await man_pick(update, ctx); return
    if data in ("hist_day", "hist_week", "hist_all"):
        await hist_period(update, ctx); return
    if data.startswith("hist_src_"):
        await hist_show(update, ctx); return

def main():
    if not os.path.exists(config.PORTFOLIO_FILE):
        portfolio.save(portfolio._empty())
    else:
        portfolio.save(portfolio.load())
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()