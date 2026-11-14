# language: Python 3.10+, file: bot.py
import asyncio
import datetime
import json
import os
import re
import time
from zoneinfo import ZoneInfo

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
CONF_CHANCE = 0.75
CONF_MIN_AUTO = 0.55
CONF_SIDEWAYS = 0.85
CONF_MANUAL = 0.65
FEE_MIN_TP = 0.006
MIN_NET_PCT = 0.2
VOL_MIN_24H = 100000.0
SPREAD_MAX = 0.0015
ATR_MIN_PCT = 0.001
ATR_MAX_PCT = 0.03
DAILY_DD_LIMIT = 0.05
PANIC_DROP = 0.02
PANIC_INTERVAL = 5
LOSS_LIMIT = 3
SLEEP_MIN = 30
MAX_POSITIONS = 2
MAX_MULTI = 10
SCAN_TOP = 200
SCAN_CONCURRENCY = 30
SCAN_INTERVAL = 300
MARKETS_TTL = 3600
EMA200_TTL = 300
TICKERS_TTL = 30
CMC_TOP_N = 100
CMC_TTL = 600
BIG_EXCHANGES = {"okx", "binance", "bybit", "kraken"}
SIDEWAYS_ATR_MIN = 0.02
SIDEWAYS_ATR_MAX = 0.06
SIDEWAYS_RANGE_MIN = 0.005
PROFIT_COEF = 0.35

# ---------- КЕШ ----------
_CACHE = {
    "markets": {}, "markets_ts": {},
    "ema200": {}, "ema200_ts": {},
    "tickers": {}, "tickers_ts": {},
    "cmc": {}, "cmc_ts": {},
}
_EX_CACHE = {}

def cache_get(kind, key, ttl):
    ts = _CACHE.get(kind + "_ts", {}).get(key, 0)
    if time.time() - ts > ttl:
        return None
    return _CACHE.get(kind, {}).get(key)

def cache_set(kind, key, val):
    _CACHE.setdefault(kind, {})[key] = val
    _CACHE.setdefault(kind + "_ts", {})[key] = time.time()

# ---------- СОСТОЯНИЕ ----------
STATE = {
    "balance": 0.0,
    "daily_budget": 0.0,
    "city": "",
    "tz": "UTC",
    "loss_streak": 0,
    "sleep_until": None,
    "day_started": datetime.date.today().isoformat(),
    "day_pnl_usdt": 0.0,
    "day_halt": False,
    "auto": {
        "active": False, "task": None, "scan_task": None,
        "symbols": [], "watched": [],
        "tf": "15m", "exchange": "bybit",
        "current_offer": None, "best_score": 0,
        "multi_day": False, "work_hours": 24,
        "last_scan_ts": 0, "last_scan_result": "",
        "first_scan_done": False,
        "seen_signals": set(),
        "forecast": None,
        "forecast_candidates": [],
        "forecast_confirmed": False,
    },
    "manual": {"exchange": config.EXCHANGE, "pairs": [], "page": 0, "symbol": None, "tf": "15m"},
    "pending": {},
    "panic": {"tasks": {}, "active": {}},
    "demo": {"active": False, "symbol": None, "buy_price": 0.0, "amount": 0.0, "action": "BUY"},
    "trailing": {"tasks": {}, "active": {}},
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
    if budget <= 0 or price <= 0:
        return 0.0
    risk_pct = 0.03 if confidence >= 0.85 else (0.02 if confidence >= 0.75 else 0.01)
    diff = abs(price - sl_price)
    if diff <= 0:
        return 0.0
    qty = (budget * risk_pct) / diff
    max_qty = (budget * 0.2) / price
    return round(min(qty, max_qty), 8)

def tf_from_hours(hours):
    h = int(hours) if hours else 24
    if h <= 2:
        return "1m"
    if h <= 6:
        return "5m"
    if h <= 12:
        return "15m"
    return "4h"

def tf_label(tf):
    return {"1m": "1 минута", "5m": "5 минут", "15m": "15 минут",
            "1h": "1 час", "4h": "4 часа"}.get(tf, tf)

def get_exchange_obj(key):
    if key in _EX_CACHE:
        return _EX_CACHE[key]
    cls = getattr(ccxt, key)
    params = {"enableRateLimit": True, "timeout": 15000,
              "options": {"defaultType": "spot"}}
    if key == "okx":
        params["options"]["fetchMarkets"] = ["spot"]
    ex = cls(params)
    _EX_CACHE[key] = ex
    return ex

def get_markets(key):
    cached = cache_get("markets", key, MARKETS_TTL)
    if cached is not None:
        return cached
    ex = get_exchange_obj(key)
    m = ex.load_markets()
    cache_set("markets", key, m)
    return m

def get_tickers(key):
    cached = cache_get("tickers", key, TICKERS_TTL)
    if cached is not None:
        return cached
    ex = get_exchange_obj(key)
    try:
        if key == "okx":
            t = ex.fetch_tickers(params={"instType": "SPOT"})
        else:
            t = ex.fetch_tickers()
    except Exception:
        t = ex.fetch_tickers()
    cache_set("tickers", key, t)
    return t

def get_cmc_top(n=CMC_TOP_N):
    cached = cache_get("cmc", "top", CMC_TTL)
    if cached is not None:
        return cached
    try:
        import urllib.request
        url = ("https://api.coingecko.com/api/v3/coins/markets"
               "?vs_currency=usd&order=market_cap_desc&per_page=" + str(n) +
               "&page=1&sparkline=false")
        req = urllib.request.Request(url, headers={"User-Agent": "friday_bot"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        tops = set()
        for item in data:
            sym = (item.get("symbol") or "").upper()
            if sym:
                tops.add(sym)
        cache_set("cmc", "top", tops)
        return tops
    except Exception:
        return None

def get_ema200_1h(key, symbol):
    k = key + ":" + symbol
    cached = cache_get("ema200", k, EMA200_TTL)
    if cached is not None:
        return cached
    try:
        ex = get_exchange_obj(key)
        raw = ex.fetch_ohlcv(symbol, "1h", limit=250)
        if not raw or len(raw) < 200:
            cache_set("ema200", k, None)
            return None
        import pandas as pd
        df = pd.DataFrame(raw, columns=["ts", "o", "h", "l", "c", "v"])
        ema = float(df["c"].ewm(span=200, adjust=False).mean().iloc[-1])
        last = float(df["c"].iloc[-1])
        cache_set("ema200", k, {"ema": ema, "close": last})
        return {"ema": ema, "close": last}
    except Exception:
        cache_set("ema200", k, None)
        return None

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

def market_regime(atr_pct, price, df_15m):
    if atr_pct < ATR_MIN_PCT:
        return "dead"
    if atr_pct > ATR_MAX_PCT:
        return "chaos"
    try:
        tail = df_15m.tail(12)
        hi = float(tail["high"].max())
        lo = float(tail["low"].min())
        rng_pct = (hi - lo) / price if price else 0
        if SIDEWAYS_ATR_MIN <= atr_pct <= SIDEWAYS_ATR_MAX and rng_pct < SIDEWAYS_RANGE_MIN:
            return "sideways"
    except Exception:
        pass
    return "normal"

# ---------- СТРОГИЕ ПРОВЕРКИ СИГНАЛА ----------
def mtf_direction(tf):
    if tf in ("1m", "5m"):
        return "15m"
    if tf == "15m":
        return "4h"
    return None

def mtf_confirm(symbol, direction, tf):
    higher = mtf_direction(tf)
    if not higher:
        return True
    try:
        ex = get_exchange_obj(STATE["auto"]["exchange"])
        raw = ex.fetch_ohlcv(symbol, higher, limit=120)
        if not raw or len(raw) < 50:
            return True
        import pandas as pd
        df = pd.DataFrame(raw, columns=["ts", "o", "h", "l", "c", "v"])
        ef = float(df["c"].ewm(span=20, adjust=False).mean().iloc[-1])
        es = float(df["c"].ewm(span=50, adjust=False).mean().iloc[-1])
        if direction == "BUY":
            return ef > es
        return ef < es
    except Exception:
        return True

def trend_1h_ok(symbol, direction, tf="15m"):
    if tf in ("1m", "5m"):
        return True
    d = get_ema200_1h(STATE["auto"]["exchange"], symbol)
    if not d:
        return True
    if direction == "BUY":
        return d["close"] > d["ema"]
    return d["close"] < d["ema"]

def strict_signal_check(symbol, sig, tf, atr_val):
    if "signal" in STATE["pending"]:
        return (False, "pending занят")
    if not sig.stop_loss or sig.stop_loss <= 0:
        return (False, "нет стопа")
    if sig.confidence < CONF_MIN_AUTO:
        return (False, "низкая уверенность")
    if not mtf_confirm(symbol, sig.action, tf):
        sig.confidence = max(sig.confidence - 0.10, 0.0)
        if sig.confidence < CONF_MIN_AUTO:
            return (False, "mtf штраф")
    tp_dist = abs(sig.take_profit - sig.price) / sig.price if sig.price else 0
    if tp_dist < FEE_MIN_TP:
        return (False, "тейк < 3× комиссии")
    net = (tp_dist - 0.004) * 100
    if net < MIN_NET_PCT:
        return (False, "чистая < 0.2%")
    return (True, "ok")

# ---------- СКАН (3 ФИЛЬТРА) ----------
async def prefilter_symbols_async(exchange_key, tf="15m"):
    loop = asyncio.get_event_loop()
    task_m = loop.run_in_executor(None, lambda: get_markets(exchange_key))
    task_t = loop.run_in_executor(None, lambda: get_tickers(exchange_key))
    use_cmc = exchange_key in BIG_EXCHANGES and tf in ("15m", "1h", "4h")
    task_c = loop.run_in_executor(None, lambda: get_cmc_top(CMC_TOP_N)) if use_cmc else None
    markets = await task_m
    tickers = await task_t
    cmc = await task_c if task_c else None
    result = []
    for sym, m in markets.items():
        if not (m.get("active") and m.get("spot") and m.get("quote") == "USDT"):
            continue
        t = tickers.get(sym)
        if not t:
            continue
        base = (m.get("base") or "").upper()
        if cmc is not None and use_cmc and base not in cmc:
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < VOL_MIN_24H:
            continue
        bid = float(t.get("bid") or 0)
        ask = float(t.get("ask") or 0)
        spread = (ask - bid) / bid if bid > 0 and ask > 0 else 0.0
        if spread > SPREAD_MAX:
            continue
        change = abs(float(t.get("percentage") or 0))
        score = qv * (change / 100.0 + 0.001)
        result.append({"symbol": sym, "score": score, "vol24": qv,
                       "spread": spread, "change": change,
                       "price": float(t.get("last") or 0)})
    result.sort(key=lambda x: x["score"], reverse=True)
    return result

async def fetch_and_score(ex, item, tf, sem):
    async with sem:
        try:
            symbol = item["symbol"]
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None, lambda: ex.fetch_ohlcv(symbol, tf, limit=120))
            if not raw or len(raw) < 60:
                return None
            import pandas as pd
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.enrich(df, tf).dropna().reset_index(drop=True)
            if len(df) < 20:
                return None
            last = df.iloc[-1]
            price = float(last["close"])
            atr_val = float(last["atr"])
            atr_pct = atr_val / price if price else 0
            try:
                sig = f.analyze_filtered(tf, tp_mult=3.0)
            except Exception:
                return None
            vol24 = item.get("vol24", 0)
            hours = STATE["auto"].get("work_hours", 24)
            liq_coef = vol24 / 10000000.0
            hours_coef = max(0.5, hours / 2.0)
            profit_potential = (atr_pct * 100) * liq_coef * hours_coef
            return {"symbol": symbol, "score": profit_potential,
                    "profit_potential": profit_potential,
                    "chance": sig.confidence * 100,
                    "signal": sig, "price": price,
                    "atr_pct": atr_pct, "atr_val": atr_val,
                    "vol24": vol24, "regime": None, "tf": tf,
                    "df": df, "f": f}
        except Exception:
            return None

async def fast_scan(tf, need=1, batch_size=100):
    ex_key = STATE["auto"]["exchange"]
    candidates = await prefilter_symbols_async(ex_key, tf)
    if not candidates:
        return []
    ex = get_exchange_obj(ex_key)
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    good = []
    for i in range(0, len(candidates), batch_size):
        chunk = candidates[i:i + batch_size]
        tasks = [fetch_and_score(ex, c, tf, sem) for c in chunk]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            good.append(r)
        if len(good) >= need:
            break
    good.sort(key=lambda x: x.get("profit_potential", 0), reverse=True)
    return good

def run_strict_check_and_build_signal(r, tf):
    sym = r["symbol"]
    f = r["f"]
    df = r["df"]
    atr_val = r["atr_val"]
    price = r["price"]
    sig = r.get("signal")
    if sig is None:
        try:
            sig = f.analyze_filtered(tf, tp_mult=3.0)
        except Exception:
            return None
    regime = market_regime(r["atr_pct"], price, df)
    if regime in ("dead", "chaos"):
        return None
    if not trend_1h_ok(sym, sig.action, tf):
        return None
    min_conf = CONF_SIDEWAYS if regime == "sideways" else CONF_MIN_AUTO
    if sig.confidence < min_conf:
        return None
    ok, reason = strict_signal_check(sym, sig, tf, atr_val)
    if not ok:
        return None
    r2 = dict(r)
    r2["signal"] = sig
    r2["regime"] = regime
    r2["chance"] = sig.confidence * 100
    return r2

# ---------- ПРОГНОЗ ----------
def estimate_profit(candidates, hours, tf):
    if not candidates:
        return None
    best = candidates[0]
    atr_pct = best["atr_pct"]
    est = atr_pct * 100 * (hours / 1.5) * PROFIT_COEF
    return {
        "symbol": best["symbol"],
        "tf": tf_label(tf),
        "tf_code": tf,
        "est_pct": est,
        "profit_potential": best.get("profit_potential", 0),
        "chance": best.get("chance", 0),
        "atr_pct": atr_pct,
        "signal": best["signal"],
        "regime": best.get("regime"),
        "score": best["score"],
        "hours": hours,
    }

# ---------- СИГНАЛЫ ----------
async def send_signal(app, r, reason=""):
    sig = r["signal"]
    budget = STATE["daily_budget"] if STATE["daily_budget"] > 0 else STATE["balance"]
    if budget <= 0:
        return
    risk_pct = 0.03 if sig.confidence >= 0.85 else (0.02 if sig.confidence >= 0.75 else 0.01)
    sl_diff = abs(sig.price - sig.stop_loss)
    if sl_diff <= 0:
        return
    max_loss_usdt = budget * risk_pct
    qty = min((budget * risk_pct) / sl_diff, (budget * 0.2) / sig.price)
    lot_usdt = qty * sig.price
    if lot_usdt <= 0:
        return
    net = (abs(sig.take_profit - sig.price) / sig.price - 0.004) * 100
    STATE["pending"]["signal"] = {
        "action": sig.action, "symbol": r["symbol"],
        "price": sig.price, "lot": lot_usdt, "qty": qty,
        "sl": sig.stop_loss, "tp": sig.take_profit,
    }
    text = ("🎯 " + r["symbol"] + " · " + r["regime"] + " · " + r["tf"] +
            ("\n" + reason if reason else "") +
            "\n" + sig.action + " @ " + fmt_price(sig.price) +
            "\n🎯 Шанс: " + str(int(sig.confidence * 100)) + "%" +
            "\n💵 Лот: " + str(round(lot_usdt, 2)) + " USDT" +
            "\n🛡 Риск: " + str(round(max_loss_usdt, 2)) + " USDT (" +
            str(round(risk_pct * 100, 1)) + "%)" +
            "\nСтоп " + fmt_price(sig.stop_loss) +
            "\nТейк " + fmt_price(sig.take_profit) +
            "\n💰 Чистая ~" + str(round(net, 2)) + "%")
    rows = [[btn("✅ " + ("Купил" if sig.action == "BUY" else "Продал"),
                 "sig_buy_signal" if sig.action == "BUY" else "sig_sell_signal")],
            [btn("✏️ Своя сумма", "sig_buy_custom" if sig.action == "BUY" else "sig_sell_custom")],
            [btn("❌ Пропустил", "sig_skipped")],
            [btn("🛑 СТОП", "auto_stop_signals")]]
    chat_id = app.bot_data.get("chat_id", 0)
    if chat_id:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb(rows))

async def _first_scan_then_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while STATE["auto"]["active"]:
        try:
            day_rollover()
            if STATE["day_halt"] or is_sleeping():
                await asyncio.sleep(30)
                continue
            tf = STATE["auto"]["tf"]
            hours = STATE["auto"].get("work_hours", 24)
            t0 = time.time()
            candidates = await fast_scan(tf, need=50)
            dt = round(time.time() - t0, 1)
            STATE["auto"]["last_scan_ts"] = time.time()
            STATE["auto"]["first_scan_done"] = True
            checked = []
            for r in candidates:
                r2 = run_strict_check_and_build_signal(r, tf)
                if r2 is not None:
                    checked.append(r2)
                if len(checked) >= 5:
                    break
            if not checked:
                STATE["auto"]["last_scan_result"] = "нет сигналов (" + str(dt) + "s), продолжаю"
                await asyncio.sleep(5)
                continue
            checked.sort(key=lambda x: x.get("profit_potential", 0), reverse=True)
            forecast = estimate_profit(checked, hours, tf)
            STATE["auto"]["forecast"] = forecast
            STATE["auto"]["forecast_candidates"] = checked
            STATE["auto"]["forecast_confirmed"] = False
            text = ("📊 <b>Лучшая монета для заработка</b>\n\n"
                    "🪙 <b>" + forecast["symbol"] + "</b>\n"
                    "⏱ ТФ: <b>" + forecast["tf"] + "</b>\n"
                    "💰 Примерный потенциал за " + str(hours) + "ч: <b>+" +
                    str(round(forecast["est_pct"], 1)) + "%</b>\n"
                    "💵 Денежный потенциал: <b>" +
                    str(round(forecast["profit_potential"], 2)) + "</b>\n"
                    "🎯 Шанс: " + str(int(forecast["chance"])) + "%\n"
                    "⚡ Направление: <b>" + forecast["signal"].action + "</b>\n"
                    "📊 Волатильность: " + str(round(forecast["atr_pct"] * 100, 2)) + "%")
            rows = [[btn("✅ Берём", "forecast_go")],
                    [btn("🔄 Ещё скан", "forecast_rescan")],
                    [btn("🛑 СТОП", "auto_stop_signals")]]
            if chat_id:
                await app.bot.send_message(chat_id=chat_id, text=text,
                    reply_markup=kb(rows), parse_mode="HTML")
            while STATE["auto"]["active"] and not STATE["auto"].get("forecast_confirmed"):
                await asyncio.sleep(3)
            if not STATE["auto"]["active"]:
                return
        except Exception as e:
            print("first_scan:", e)
            await asyncio.sleep(5)

async def scan_loop(app):
    chat_id = app.bot_data.get("chat_id", 0)
    while STATE["auto"]["active"]:
        try:
            day_rollover()
            if STATE["day_halt"] or is_sleeping():
                await asyncio.sleep(30)
                continue
            tf = STATE["auto"]["tf"]
            if "signal" in STATE["pending"]:
                await asyncio.sleep(5)
                continue
            if portfolio.count_open() >= MAX_POSITIONS:
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            t0 = time.time()
            candidates = await fast_scan(tf, need=50)
            dt = round(time.time() - t0, 1)
            STATE["auto"]["last_scan_ts"] = time.time()
            sent = False
            for r in candidates:
                if "signal" in STATE["pending"]:
                    break
                if portfolio.count_open() >= MAX_POSITIONS:
                    break
                r2 = run_strict_check_and_build_signal(r, tf)
                if r2 is None:
                    continue
                key = r2["symbol"] + ":" + r2["signal"].action
                if key in STATE["auto"]["seen_signals"]:
                    continue
                STATE["auto"]["seen_signals"].add(key)
                reason = ""
                if STATE["auto"]["symbols"] and r2["symbol"] not in STATE["auto"]["symbols"]:
                    reason = "🔥 лучше текущей"
                await send_signal(app, r2, reason)
                sent = True
                break
            if not sent:
                STATE["auto"]["last_scan_result"] = "нет сигналов (" + str(dt) + "s), продолжаю"
            await asyncio.sleep(5)
        except Exception as e:
            print("scan_loop:", e)
            await asyncio.sleep(5)

# ---------- МЕНЮ ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    day_rollover()
    opens = portfolio.load().get("opens", [])
    pos_txt = ""
    if opens:
        pos_txt = "\n📌 Позиции: " + str(len(opens)) + "/" + str(MAX_POSITIONS)
        for p in opens:
            pos_txt += "\n  • " + p["symbol"] + " (" + p["side"] + ")"
    rows = [
        [btn("🤖 Автопилот", "auto_start")],
        [btn("✋ Ручной режим", "man_menu")],
        [btn("🎮 Демо", "demo")],
        [btn("📜 История", "hist_menu")],
        [btn("📊 Статистика", "stats_menu")],
        [btn("❌ Закрыть все", "close_all")],
    ]
    txt = "Пятница на связи."
    if is_sleeping():
        txt += "\n😴 Пауза: " + str(sleep_left_min()) + " мин"
    if STATE["day_halt"]:
        txt += "\n🛑 Дневной лимит"
    if STATE["auto"]["last_scan_result"]:
        txt += "\n🔍 Скан: " + STATE["auto"]["last_scan_result"]
    txt += pos_txt
    if update.message:
        await update.message.reply_text(txt, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(txt, reply_markup=kb(rows))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["active"] = False
    for k in ("task", "scan_task"):
        t = STATE["auto"].get(k)
        if t:
            t.cancel()
            STATE["auto"][k] = None
    await update.message.reply_text("🛑 Остановлено.", reply_markup=kb([[btn("🏠 В меню", "start")]]))

# ---------- ДЕМО ----------
async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("🚀 Найти монету", "demo_scan")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("🎮 Демо.", reply_markup=kb(rows))

async def demo_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Сканирую...")
    tf = STATE["auto"]["tf"]
    candidates = await fast_scan(tf, need=50)
    best = None
    for r in candidates:
        r2 = run_strict_check_and_build_signal(r, tf)
        if r2 is not None:
            best = r2
            break
    if not best:
        await update.callback_query.edit_message_text("Не нашёл.",
            reply_markup=kb(nav_btns("demo")))
        return
    sig = best["signal"]
    STATE["demo"]["symbol"] = best["symbol"]
    STATE["demo"]["buy_price"] = sig.price
    STATE["demo"]["action"] = sig.action
    text = ("🎮 ДЕМО\n" + best["symbol"] + "\n" + sig.action +
            " @ " + fmt_price(sig.price) +
            "\nШанс: " + str(int(sig.confidence * 100)) + "%" +
            "\nРежим: " + best["regime"])
    if sig.action == "BUY":
        rows = [[btn("✅ Купил", "demo_buy")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    else:
        rows = [[btn("✅ Продал", "demo_sell")], [btn("❌ Пропустил", "demo_skip")], nav_btns("demo")[0]]
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def demo_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["demo"]["symbol"] = None
    await update.callback_query.edit_message_text("Ок.",
        reply_markup=kb([[btn("🏠 Демо", "demo")]]))

async def demo_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько купил?",
        reply_markup=kb(nav_btns("demo")))

async def demo_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["demo_amount"] = True
    await update.callback_query.edit_message_text("Сколько продал?",
        reply_markup=kb(nav_btns("demo")))

# ---------- АВТОПИЛОТ ----------
async def auto_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "balance"
    await update.callback_query.edit_message_text("Баланс (USDT)?",
        reply_markup=kb(nav_btns("start")))

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
    msg = "Город: " + STATE["city"] + ".\nВремя? (" + local + ")"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_budget")))

async def auto_after_check_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "work_time"
    msg = "Сколько работаем? ('2 часа', '1 день')"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(nav_btns("auto_retry_city")))

async def auto_ask_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for key, name in exchanges.EXCHANGES.items():
        rows.append([btn(name, "auto_ex_set_" + key)])
    rows.append(nav_btns("auto_retry_check_time")[0])
    msg = "Биржа:"
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb(rows))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=kb(rows))

async def auto_ex_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.replace("auto_ex_set_", "")
    STATE["auto"]["exchange"] = key
    ctx.application.bot_data["chat_id"] = update.callback_query.message.chat_id
    STATE["auto"]["first_scan_done"] = False
    STATE["auto"]["seen_signals"] = set()
    STATE["auto"]["forecast"] = None
    STATE["auto"]["forecast_candidates"] = []
    STATE["auto"]["forecast_confirmed"] = False
    tf = STATE["auto"]["tf"]
    await update.callback_query.edit_message_text(
        "Биржа: " + exchanges.EXCHANGES.get(key, key) +
        "\nТФ: " + tf_label(tf) + "\nСкан...",
        reply_markup=kb(nav_btns("start")))
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, lambda: get_markets(key))
    loop.run_in_executor(None, lambda: get_tickers(key))
    if key in BIG_EXCHANGES:
        loop.run_in_executor(None, lambda: get_cmc_top(CMC_TOP_N))
    if STATE["auto"]["task"] is None or STATE["auto"]["task"].done():
        STATE["auto"]["active"] = True
        STATE["auto"]["symbols"] = []
        STATE["auto"]["best_score"] = 0
        STATE["auto"]["current_offer"] = None
        STATE["auto"]["task"] = asyncio.create_task(_first_scan_then_loop(ctx.application))

async def tf_bad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("1m", "auto_tf_1m"), btn("5m", "auto_tf_5m"),
             btn("15m", "auto_tf_15m"), btn("4h", "auto_tf_4h")],
            [btn("✏️ Своё", "auto_tf_custom")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("ТФ:", reply_markup=kb(rows))

async def auto_tf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    tf = data.replace("auto_tf_", "")
    if tf == "custom":
        STATE["pending"]["auto"] = "tf_custom"
        await update.callback_query.edit_message_text("Впиши ТФ (1m, 5m, 15m, 1h, 4h):",
            reply_markup=kb(nav_btns("tf_bad")))
        return
    STATE["auto"]["tf"] = tf
    await update.callback_query.edit_message_text("Ок, " + tf_label(tf) + ".",
        reply_markup=kb(nav_btns("start")))

async def hascoins_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок.",
        reply_markup=kb(nav_btns("start")))

async def hascoins_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок.",
        reply_markup=kb(nav_btns("start")))

async def multi_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["auto"] = "multi_count"
    await update.callback_query.edit_message_text("Сколько монет? (2–10)",
        reply_markup=kb(nav_btns("start")))

async def forecast_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["forecast_confirmed"] = True
    checked = STATE["auto"].get("forecast_candidates", [])
    if not checked:
        await update.callback_query.edit_message_text("Нет кандидатов.",
            reply_markup=kb(nav_btns("start")))
        return
    r2 = checked[0]
    STATE["auto"]["symbols"] = [r2["symbol"]]
    STATE["auto"]["best_score"] = r2["score"]
    await update.callback_query.edit_message_text(
        "✅ Взял " + r2["symbol"] + " (" + tf_label(STATE["auto"]["tf"]) + ").\nШлю сигнал...",
        reply_markup=kb(nav_btns("start")))
    key = r2["symbol"] + ":" + r2["signal"].action
    STATE["auto"]["seen_signals"].add(key)
    await send_signal(ctx.application, r2, "стартовая")
    await scan_loop(ctx.application)

async def forecast_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["forecast_confirmed"] = True
    STATE["auto"]["forecast_candidates"] = []
    STATE["auto"]["forecast"] = None
    await update.callback_query.edit_message_text("🔄 Пересканирую...",
        reply_markup=kb(nav_btns("start")))

async def switch_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок.",
        reply_markup=kb(nav_btns("start")))

async def switch_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Ок.",
        reply_markup=kb(nav_btns("start")))

async def tf_ok_15m(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]["tf"] = "15m"
    await update.callback_query.edit_message_text("Ок, 15m.",
        reply_markup=kb(nav_btns("start")))

async def _ask_hascoins(update, ctx):
    rows = [[btn("Нет", "hascoins_no")], [btn("Да", "hascoins_yes")], nav_btns("start")[0]]
    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.message.chat_id
    await ctx.bot.send_message(chat_id=chat_id, text="Есть эта монета?", reply_markup=kb(rows))

# ---------- ТРЕЙЛИНГ ----------
async def _trailing_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["trailing"]["active"][symbol] = True
    while STATE["trailing"]["active"].get(symbol):
        await asyncio.sleep(30)
        try:
            pos = portfolio.find_open(symbol)
            if not pos:
                break
            side = pos["side"]
            entry = pos["entry"]
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.fetch("1m", 20)
            price = float(df["close"].iloc[-1])
            atr_v = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
            portfolio.update_peak(price, symbol)
            pos2 = portfolio.find_open(symbol)
            if not pos2:
                break
            best = pos2.get("peak", entry)
            if side == "BUY":
                gain = (best - entry) / entry
                sl = entry
                if gain >= 2.0 * (atr_v / entry if entry else 0):
                    sl = best - atr_v * 0.3
                elif gain >= 1.5 * (atr_v / entry if entry else 0):
                    sl = entry + (best - entry) * 0.33
                if price <= sl and gain >= (atr_v / entry if entry else 0):
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0:
                            register_loss(trade.get("pnl_abs", 0))
                        else:
                            register_win(trade.get("pnl_abs", 0))
                        if chat_id:
                            await app.bot.send_message(chat_id=chat_id,
                                text="🛡 " + symbol + " PnL: " + str(round(trade["pnl_pct"], 2)) + "%")
                    break
            else:
                gain = (entry - best) / entry
                sl = entry
                if gain >= 2.0 * (atr_v / entry if entry else 0):
                    sl = best + atr_v * 0.3
                elif gain >= 1.5 * (atr_v / entry if entry else 0):
                    sl = entry - (entry - best) * 0.33
                if price >= sl and gain >= (atr_v / entry if entry else 0):
                    trade = portfolio.close_position(price, pos2["qty"], symbol)
                    if "error" not in trade:
                        if trade["pnl_pct"] < 0:
                            register_loss(trade.get("pnl_abs", 0))
                        else:
                            register_win(trade.get("pnl_abs", 0))
                    break
        except Exception as e:
            print("trailing:", e)
    STATE["trailing"]["active"][symbol] = False

# ---------- ПАНИКА ----------
async def _panic_loop(app, symbol):
    chat_id = app.bot_data.get("chat_id", 0)
    STATE["panic"]["active"][symbol] = True
    while STATE["panic"]["active"].get(symbol):
        try:
            pos = portfolio.find_open(symbol)
            if not pos:
                break
            f = Friday(symbol=symbol, exchange_name=STATE["auto"]["exchange"])
            df = f.enrich(f.fetch("1m", 60), "1m").dropna().reset_index(drop=True)
            if len(df) < 5:
                await asyncio.sleep(PANIC_INTERVAL)
                continue
            last = df.iloc[-1]
            price = float(last["close"])
            pnl = portfolio.current_pnl(price, symbol)
            if pnl:
                peak = pnl.get("peak", pnl["entry"])
                drop = (peak - price) / peak if peak else 0
                if pnl["side"] == "BUY" and drop >= PANIC_DROP:
                    bearish = (last["ema_fast"] < last["ema_slow"]
                               and last["macd_hist"] < 0
                               and last["close"] < last["bb_lower"])
                    if bearish and chat_id:
                        await app.bot.send_message(chat_id=chat_id,
                            text="🔴 ЗАКРЫВАЙ " + symbol + "!",
                            reply_markup=kb([[btn("🔴 ПРОДАЛ " + symbol,
                                "panic_sold_" + symbol.replace("/", "_"))]]))
        except Exception:
            pass
        await asyncio.sleep(PANIC_INTERVAL)
    STATE["panic"]["active"][symbol] = False

async def sig_skipped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "signal" in STATE["pending"]:
        del STATE["pending"]["signal"]
    await update.callback_query.edit_message_text("Пропустил.",
        reply_markup=kb(nav_btns("start")))

async def _open_and_watch(ctx, symbol, side, price, qty, sl, tp):
    if portfolio.count_open() >= MAX_POSITIONS:
        return {"error": "максимум позиций"}
    res = portfolio.open_position(symbol, side, price, qty, source="signal",
                                  sl=sl, tp=tp)
    if "error" in res:
        return res
    if symbol not in STATE["trailing"]["tasks"] or STATE["trailing"]["tasks"][symbol].done():
        STATE["trailing"]["tasks"][symbol] = asyncio.create_task(
            _trailing_loop(ctx.application, symbol))
    if symbol not in STATE["panic"]["tasks"] or STATE["panic"]["tasks"][symbol].done():
        STATE["panic"]["tasks"][symbol] = asyncio.create_task(
            _panic_loop(ctx.application, symbol))
    return res

async def sig_buy_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True)
        return
    res = await _open_and_watch(ctx, sig["symbol"], "BUY",
                                sig["price"], sig["qty"], sig["sl"], sig["tp"])
    del STATE["pending"]["signal"]
    if "error" in res:
        await update.callback_query.edit_message_text("Ошибка: " + res["error"],
            reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return
    await update.callback_query.edit_message_text(
        "Купил " + sig["symbol"] + " на " + str(round(sig["lot"], 2)) + " USDT.",
        reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_sell_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        await update.callback_query.answer("Устарел.", show_alert=True)
        return
    pos = portfolio.find_open(sig["symbol"])
    if not pos:
        del STATE["pending"]["signal"]
        await update.callback_query.edit_message_text("Нет позиции по " + sig["symbol"],
            reply_markup=kb(nav_btns("start")))
        return
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
        await update.callback_query.edit_message_text("Ошибка: " + trade["error"],
            reply_markup=kb(nav_btns("start")))
        return
    if trade["pnl_pct"] < 0:
        register_loss(trade.get("pnl_abs", 0))
    else:
        register_win(trade.get("pnl_abs", 0))
    await update.callback_query.edit_message_text(
        "PnL " + str(round(trade["pnl_pct"], 2)) + "% (" +
        str(round(trade["pnl_abs"], 2)) + " USDT)",
        reply_markup=kb([[btn("🏠 В меню", "start")]]))

async def sig_buy_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        return
    STATE["pending"]["wait_custom_amount"] = {"action": "BUY", "symbol": sig["symbol"],
                                              "price": sig["price"], "sl": sig["sl"], "tp": sig["tp"]}
    await update.callback_query.edit_message_text("Сумма USDT:",
        reply_markup=kb(nav_btns("start")))

async def sig_sell_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = STATE["pending"].get("signal")
    if not sig:
        return
    STATE["pending"]["wait_custom_amount"] = {"action": "SELL", "symbol": sig["symbol"],
                                              "price": sig["price"], "sl": sig["sl"], "tp": sig["tp"]}
    await update.callback_query.edit_message_text("Сумма USDT:",
        reply_markup=kb(nav_btns("start")))

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
            if trade["pnl_pct"] < 0:
                register_loss(trade.get("pnl_abs", 0))
            else:
                register_win(trade.get("pnl_abs", 0))
    await update.callback_query.edit_message_text("Закрыто " + sym + ".",
        reply_markup=kb(nav_btns("start")))

# ---------- РУЧНОЙ ----------
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
    STATE["manual"]["pairs"] = []
    STATE["manual"]["page"] = 0
    await man_menu(update, ctx)

async def man_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE["manual"]["pairs"]:
        await update.callback_query.edit_message_text("Загружаю...")
        try:
            STATE["manual"]["pairs"] = exchanges.load_all_pairs(
                STATE["manual"]["exchange"], quote="USDT", max_pairs=2000)
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
    text = "Пары " + str(start + 1) + "-" + str(min(start + 50, len(pairs))) + " из " + str(len(pairs))
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["manual"]["page"] = int(update.callback_query.data.replace("man_page_", ""))
    await _render_pairs(update, ctx)

async def man_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["pending"]["manual"] = "search"
    await update.callback_query.edit_message_text("Тикер:",
        reply_markup=kb(nav_btns("man_menu")))

async def man_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("man_pick_", "")
    symbol = raw.replace("_", "/")
    if not symbol.endswith("/USDT"):
        symbol += "/USDT"
    STATE["manual"]["symbol"] = symbol
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⚡ Кнопки", "man_quick")],
        nav_btns("man_pairs")[0],
    ]
    await update.callback_query.edit_message_text("Пара: " + symbol, reply_markup=kb(rows))

async def man_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    await update.callback_query.edit_message_text("Анализирую...")
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        tf = STATE["auto"]["tf"]
        sig = f.analyze_filtered(tf, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    if sig.confidence < CONF_SIGNAL:
        await update.callback_query.edit_message_text(
            "Шанс " + str(int(sig.confidence * 100)) + "% < " + str(int(CONF_SIGNAL * 100)) + "%",
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    text = ("📊 " + symbol + " · СИГНАЛ" +
            "\n" + sig.action + " @ " + fmt_price(sig.price) +
            "\nШанс: " + str(int(sig.confidence * 100)) + "%" +
            "\nСтоп " + fmt_price(sig.stop_loss) +
            "\nТейк " + fmt_price(sig.take_profit))
    try:
        path = build_chart(symbol, tf, sig)
        with open(path, "rb") as photo:
            await ctx.bot.send_photo(
                chat_id=update.callback_query.message.chat_id,
                photo=photo, caption=text,
                reply_markup=kb(nav_btns("man_pick_back")))
        await update.callback_query.delete_message()
    except Exception:
        await update.callback_query.edit_message_text(text,
            reply_markup=kb(nav_btns("man_pick_back")))

async def man_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    if not symbol:
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        tf = STATE["auto"]["tf"]
        sig = f.analyze_filtered(tf, tp_mult=3.0)
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("man_pick_back")))
        return
    rows = [
        [btn("🟢 Купить", "man_buy")],
        [btn("🔴 Продать", "man_sell")],
        nav_btns("man_pick_back")[0],
    ]
    text = "⚡ " + symbol + "\n" + sig.action + " " + str(int(sig.confidence * 100)) + "%"
    await update.callback_query.edit_message_text(text, reply_markup=kb(rows))

async def man_pick_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [
        [btn("📊 Сигналы", "man_signals")],
        [btn("⚡ Кнопки", "man_quick")],
        nav_btns("man_pairs")[0],
    ]
    await update.callback_query.edit_message_text(
        "Пара: " + str(STATE["manual"]["symbol"]), reply_markup=kb(rows))

async def man_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        tf = STATE["auto"]["tf"]
        sig = f.analyze_filtered(tf, tp_mult=3.0)
        qty = calc_qty(sig.price, sig.stop_loss, sig.confidence)
        res = await _open_and_watch(ctx, symbol, "BUY", sig.price, qty, sig.stop_loss, sig.take_profit)
        if "error" in res:
            await update.callback_query.edit_message_text("Ошибка: " + res["error"],
                reply_markup=kb(nav_btns("start")))
            return
        await update.callback_query.edit_message_text(
            "BUY " + symbol + " " + str(round(qty * sig.price, 2)) + " USDT",
            reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("start")))

async def man_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = STATE["manual"]["symbol"]
    pos = portfolio.find_open(symbol)
    if not pos:
        await update.callback_query.edit_message_text("Нет позиции по " + str(symbol),
            reply_markup=kb(nav_btns("start")))
        return
    try:
        f = Friday(symbol=symbol, exchange_name=STATE["manual"]["exchange"])
        price = float(f.fetch("1m", 30).iloc[-1]["close"])
        trade = portfolio.close_position(price, pos["qty"], symbol)
        if "error" in trade:
            await update.callback_query.edit_message_text("Ошибка: " + trade["error"],
                reply_markup=kb(nav_btns("start")))
            return
        if trade["pnl_pct"] < 0:
            register_loss(trade.get("pnl_abs", 0))
        else:
            register_win(trade.get("pnl_abs", 0))
        await update.callback_query.edit_message_text(
            "Закрыл " + symbol + " PnL " + str(round(trade["pnl_pct"], 2)) + "%",
            reply_markup=kb(nav_btns("start")))
    except Exception as e:
        await update.callback_query.edit_message_text("Ошибка: " + str(e),
            reply_markup=kb(nav_btns("start")))

async def close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opens = portfolio.load().get("opens", [])
    if not opens:
        await update.callback_query.edit_message_text("Нет сделок.",
            reply_markup=kb(nav_btns("start")))
        return
    total_pnl = 0.0
    total_abs = 0.0
    count = 0
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
                if trade["pnl_pct"] < 0:
                    register_loss(trade.get("pnl_abs", 0))
                else:
                    register_win(trade.get("pnl_abs", 0))
        except Exception:
            pass
    await update.callback_query.edit_message_text(
        "Закрыто " + str(count) + ".\nИтог PnL " + str(round(total_pnl, 2)) +
        "% (" + str(round(total_abs, 2)) + " USDT)",
        reply_markup=kb(nav_btns("start")))

# ---------- ИСТОРИЯ ----------
async def hist_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = [[btn("📅 День", "hist_day")], [btn("📆 Неделя", "hist_week")],
            [btn("🗓 Всё", "hist_all")], nav_btns("start")[0]]
    await update.callback_query.edit_message_text("Период:", reply_markup=kb(rows))

async def hist_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = update.callback_query.data.replace("hist_", "")
    rows = [[btn("🎮 Кнопки", "hist_src_" + period + "_manual")],
            [btn("📊 Сигналы", "hist_src_" + period + "_signal")],
            [btn("🤖 Авто", "hist_src_" + period + "_auto")],
            [btn("📋 Всё", "hist_src_" + period + "_all")],
            nav_btns("hist_menu")[0]]
    await update.callback_query.edit_message_text("Источник:", reply_markup=kb(rows))

async def hist_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query.data.replace("hist_src_", "")
    parts = raw.split("_")
    period, source = parts[0], parts[1]
    s = portfolio.stats_filtered(period, source)
    text = ("📜 " + period + " / " + source +
            "\nСделок " + str(s["total"]) +
            "\n+ " + str(s["wins"]) + " / - " + str(s["losses"]) +
            "\nWinrate " + str(round(s["winrate"], 1)) + "%" +
            "\nPnL % " + str(round(s["total_pnl"], 2)) +
            "\nPnL USDT " + str(round(s["total_abs"], 2)) +
            "\nPF " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("hist_menu")))

async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = portfolio.stats_filtered("all", "all")
    text = ("📊 Всего" +
            "\nСделок " + str(s["total"]) +
            "\nWinrate " + str(round(s["winrate"], 1)) + "%" +
            "\nPnL USDT " + str(round(s["total_abs"], 2)) +
            "\nPF " + str(round(s["pf"], 2)))
    await update.callback_query.edit_message_text(text, reply_markup=kb(nav_btns("start")))

# ---------- ТЕКСТ ----------
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
        res = "📈 +" if pnl >= 0 else "📉 "
        msg = ("🎮 ДЕМО " + sym + "\n" + res + str(round(pnl, 2)) +
               " USDT (" + str(round(pnl_pct * 100, 2)) + "%)")
        await update.message.reply_text(msg, reply_markup=kb([[btn("🏠 Демо", "demo")]]))
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
            res = await _open_and_watch(ctx, sd["symbol"], "BUY",
                                        sd["price"], qty, sd["sl"], sd["tp"])
            if "error" in res:
                await update.message.reply_text("Ошибка: " + res["error"],
                    reply_markup=kb(nav_btns("start")))
                return
            await update.message.reply_text(
                "Купил " + sd["symbol"] + " " + str(amount) + " USDT.",
                reply_markup=kb([[btn("🏠 В меню", "start")]]))
        else:
            pos = portfolio.find_open(sd["symbol"])
            if not pos:
                await update.message.reply_text("Нет позиции.",
                    reply_markup=kb(nav_btns("start")))
                return
            qty = min(amount / sd["price"] if sd["price"] else 0, pos["qty"])
            trade = portfolio.close_position(sd["price"], qty, sd["symbol"])
            STATE["trailing"]["active"][sd["symbol"]] = False
            STATE["panic"]["active"][sd["symbol"]] = False
            if "error" in trade:
                await update.message.reply_text("Ошибка: " + trade["error"],
                    reply_markup=kb(nav_btns("start")))
                return
            if trade["pnl_pct"] < 0:
                register_loss(trade.get("pnl_abs", 0))
            else:
                register_win(trade.get("pnl_abs", 0))
            await update.message.reply_text(
                "PnL " + str(round(trade["pnl_pct"], 2)) + "% (" +
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
                await update.message.reply_text("Число.", reply_markup=kb(nav_btns("auto_start")))
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
            STATE["auto"]["work_hours"] = int(hours)
            STATE["auto"]["multi_day"] = ("д" in txt.lower() or "day" in txt.lower())
            STATE["auto"]["tf"] = tf_from_hours(hours)
            pend.pop("auto")
            await auto_ask_exchange(update, ctx)
            return
        if k == "tf_custom":
            tf = txt.strip()
            if not re.match(r"^\d+[mhd]$", tf):
                await update.message.reply_text("5m, 1h.",
                    reply_markup=kb(nav_btns("tf_bad")))
                return
            STATE["auto"]["tf"] = tf
            pend.pop("auto")
            await update.message.reply_text("Ок, " + tf_label(tf) + ".",
                reply_markup=kb(nav_btns("start")))
            return
        if k == "multi_count":
            try:
                n = int(txt)
            except Exception:
                await update.message.reply_text("2-10.", reply_markup=kb(nav_btns("start")))
                return
            if n < 2 or n > MAX_MULTI:
                await update.message.reply_text("2-10.", reply_markup=kb(nav_btns("start")))
                return
            pend.pop("auto")
            rows = [[btn("✅ Запустить " + str(n), "multi_start_" + str(n))],
                    nav_btns("start")[0]]
            await update.message.reply_text("Ок " + str(n), reply_markup=kb(rows))
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
            rows = [[btn("📊 Сигналы", "man_signals")],
                    [btn("⚡ Кнопки", "man_quick")],
                    nav_btns("man_pairs")[0]]
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
    await update.callback_query.edit_message_text("Сканирую топ-" + str(n) + "...",
        reply_markup=kb(nav_btns("start")))
    tf = STATE["auto"]["tf"]
    candidates = await fast_scan(tf, need=n * 5)
    top = candidates[:n]
    if not top:
        await update.callback_query.edit_message_text("Ничего.",
            reply_markup=kb(nav_btns("start")))
        return
    STATE["auto"]["symbols"] = [t["symbol"] for t in top]
    lines = ["🤖 Взял " + str(len(top)) + ":"]
    for t in top:
        lines.append("• " + t["symbol"] + " " + str(round(t.get("profit_potential", 0), 1)))
    await update.callback_query.edit_message_text("\n".join(lines),
        reply_markup=kb(nav_btns("start")))

# ---------- КНОПКИ ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if ctx.application.bot_data.get("chat_id") != q.message.chat_id:
        ctx.application.bot_data["chat_id"] = q.message.chat_id

    mapping = {
        "demo": demo, "demo_scan": demo_scan, "demo_skip": demo_skip,
        "demo_buy": demo_buy, "demo_sell": demo_sell,
        "auto_start": auto_start, "auto_retry_balance": auto_start,
        "tf_ok_15m": tf_ok_15m, "tf_bad": tf_bad,
        "switch_yes": switch_yes, "switch_no": switch_no,
        "hascoins_no": hascoins_no, "hascoins_yes": hascoins_yes,
        "multi_choose": multi_choose,
        "forecast_go": forecast_go, "forecast_rescan": forecast_rescan,
        "sig_skipped": sig_skipped, "sig_buy_signal": sig_buy_signal,
        "sig_buy_custom": sig_buy_custom, "sig_sell_signal": sig_sell_signal,
        "sig_sell_custom": sig_sell_custom,
        "man_menu": man_menu, "man_ex": man_ex, "man_pairs": man_pairs,
        "man_search": man_search, "man_signals": man_signals,
        "man_quick": man_quick, "man_pick_back": man_pick_back,
        "man_buy": man_buy, "man_sell": man_sell,
        "close_all": close_all,
        "hist_menu": hist_menu, "stats_menu": stats_menu,
    }

    if data.startswith("panic_sold_"):
        await panic_sold(update, ctx)
        return
    if data in mapping:
        await mapping[data](update, ctx)
        return

    if data == "start":
        if STATE["auto"]["active"]:
            STATE["auto"]["active"] = False
            for k in ("task", "scan_task"):
                if STATE["auto"].get(k):
                    STATE["auto"][k].cancel()
                    STATE["auto"][k] = None
        await start(update, ctx)
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
        await q.edit_message_text("Время? (" + local + ")",
            reply_markup=kb(nav_btns("auto_retry_city")))
        return
    if data == "auto_stop_signals":
        STATE["auto"]["active"] = False
        STATE["auto"]["forecast_confirmed"] = True
        for k in ("task", "scan_task"):
            if STATE["auto"].get(k):
                STATE["auto"][k].cancel()
                STATE["auto"][k] = None
        await q.edit_message_text("🛑 Стоп.", reply_markup=kb([[btn("🏠 В меню", "start")]]))
        return
    if data.startswith("auto_ex_set_"):
        await auto_ex_set(update, ctx)
        return
    if data.startswith("auto_tf_"):
        await auto_tf_set(update, ctx)
        return
    if data.startswith("multi_start_"):
        await multi_start(update, ctx)
        return
    if data.startswith("man_ex_set_"):
        await man_ex_set(update, ctx)
        return
    if data.startswith("man_page_"):
        await man_page(update, ctx)
        return
    if data.startswith("man_pick_"):
        await man_pick(update, ctx)
        return
    if data in ("hist_day", "hist_week", "hist_all"):
        await hist_period(update, ctx)
        return
    if data.startswith("hist_src_"):
        await hist_show(update, ctx)
        return

# ---------- MAIN ----------
def main():
    if not os.path.exists(config.PORTFOLIO_FILE):
        portfolio.save(portfolio._empty())
    else:
        data = portfolio.load()
        portfolio.save(data)
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()