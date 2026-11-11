# language: Python 3.10+, file: friday.py
from __future__ import annotations
from html import escape
import ccxt
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Literal
from enum import Enum
import analysis
import datetime

class Mode(Enum):
    SCALP = "scalp"
    INTRADAY = "intraday"
    SWING = "swing"

MODE_CONFIG = {
    Mode.SCALP: {"tf": "1m", "lookback": 300, "ema_fast": 9, "ema_slow": 21, "rsi_len": 7, "atr_mult": 1.2, "min_votes": 4},
    Mode.INTRADAY: {"tf": "15m", "lookback": 300, "ema_fast": 20, "ema_slow": 50, "rsi_len": 14, "atr_mult": 1.5, "min_votes": 4},
    Mode.SWING: {"tf": "4h", "lookback": 500, "ema_fast": 50, "ema_slow": 200, "rsi_len": 14, "atr_mult": 2.0, "min_votes": 4},
}

MAX_VOTES = 10.0

def is_good_time():
    """Фильтр по времени: торгуем только когда рынок активен (Азия + США)"""
    hour = datetime.datetime.utcnow().hour
    # Плохие часы: с 03:00 до 07:00 UTC (глубокая ночь, низкая волатильность)
    if 3 <= hour < 7:
        return False
    return True

def fmt_price(price):
    if price == 0: return "0"
    p = float(price)
    if p < 0.0001: return f"{p:.8f}"
    elif p < 0.01: return f"{p:.6f}"
    else: return f"{p:.4f}"

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    d = series.diff()
    gain = d.where(d > 0, 0.0)
    loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1.0 / length, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / length, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def atr(high, low, close, length=14):
    pc = close.shift(1)
    tr1 = high - low
    tr2 = (high - pc).abs()
    tr3 = (low - pc).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()

def macd_calc(series, fast=12, slow=26, signal=9):
    ef = series.ewm(span=fast, adjust=False).mean()
    es = series.ewm(span=slow, adjust=False).mean()
    m = ef - es
    s = m.ewm(span=signal, adjust=False).mean()
    h = m - s
    return m, s, h

@dataclass
class Signal:
    mode: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    price: float
    reasons: list
    stop_loss: float
    take_profit: float
    partial_tp: float
    symbol: str = ""

    def pretty(self) -> str:
        lines = []
        lines.append("<b>" + self.action + "</b> · " + str(self.mode).upper())
        lines.append("пара: <code>" + escape(self.symbol) + "</code>")
        lines.append("цена: <code>" + fmt_price(self.price) + "</code>")
        lines.append("уверенность: <b>" + str(int(self.confidence * 100)) + "%</b>")
        if self.action != "HOLD":
            lines.append("стоп: <code>" + fmt_price(self.stop_loss) + "</code>")
            lines.append("тейк 1 (50%): <code>" + fmt_price(self.partial_tp) + "</code>")
            lines.append("тейк 2 (50%): <code>" + fmt_price(self.take_profit) + "</code>")
        lines.append("")
        lines.append("причины:")
        for r in self.reasons:
            lines.append("  • " + escape(str(r)))
        return "\n".join(lines)

class Friday:
    def __init__(self, symbol="BTC/USDT", exchange_name="bybit"):
        self.symbol = symbol
        self.exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})

    def fetch(self, tf, lookback=300):
        raw = self.exchange.fetch_ohlcv(self.symbol, timeframe=tf, limit=lookback)
        if not raw or len(raw) < 60:
            raise RuntimeError("мало данных")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df

    def enrich(self, df, tf):
        df["ema_fast"] = ema(df["close"], 9 if tf=="1m" else 20)
        df["ema_slow"] = ema(df["close"], 21 if tf=="1m" else 50)
        df["rsi"] = rsi(df["close"], 7 if tf=="1m" else 14)
        df["atr"] = atr(df["high"], df["low"], df["close"], 14)
        m, s, h = macd_calc(df["close"])
        df["macd"] = m
        df["macd_sig"] = s
        df["macd_hist"] = h
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # 6 алгоритмов
        ma, up, lo = analysis.bollinger(df["close"], 20, 2.0)
        df["bb_upper"] = up
        df["bb_lower"] = lo
        k, d = analysis.stochastic(df["high"], df["low"], df["close"], 14, 3)
        df["stoch_k"] = k
        df["stoch_d"] = d
        adx_v, pdi, ndi = analysis.adx(df["high"], df["low"], df["close"], 14)
        df["adx"] = adx_v
        df["plus_di"] = pdi
        df["minus_di"] = ndi
        df["obv"] = analysis.obv_series(df["close"], df["volume"])
        df["obv_ma"] = df["obv"].rolling(20).mean()
        df["rsi_div"] = "none"
        try:
            r = rsi(df["close"], 7 if tf=="1m" else 14)
            df["rsi_div"] = analysis.rsi_divergence(df["close"], r, 20)
        except: pass

        return df

    def score(self, df):
        last = df.iloc[-1]
        prev = df.iloc[-2]
        votes = 0
        reasons = []

        # 1. Свечи
        closes = df["close"].tail(30).values
        opens = df["open"].tail(30).values
        green = sum(1 for i in range(len(closes)) if closes[i] > opens[i])
        bias = green / len(closes)
        if bias > 0.6: votes += 1; reasons.append("история свечей: BUY")
        elif bias < 0.4: votes -= 1; reasons.append("история свечей: SELL")

        # 2. EMA
        if last["ema_fast"] > last["ema_slow"]: votes += 1; reasons.append("EMA тренд вверх")
        else: votes -= 1; reasons.append("EMA тренд вниз")

        # 3. RSI
        if last["rsi"] < 30: votes += 1; reasons.append("RSI перепродан")
        elif last["rsi"] > 70: votes -= 1; reasons.append("RSI перекуплен")

        # 4. MACD
        if prev["macd"] < prev["macd_sig"] and last["macd"] > last["macd_sig"]:
            votes += 2; reasons.append("MACD бычье")
        elif prev["macd"] > prev["macd_sig"] and last["macd"] < last["macd_sig"]:
            votes -= 2; reasons.append("MACD медвежье")

        # 5. Bollinger + Volume
        if last["close"] < last["bb_lower"]: votes += 1; reasons.append("ниже Боллинджер")
        elif last["close"] > last["bb_upper"]: votes -= 1; reasons.append("выше Боллинджер")
        if last["vol_ma"] and last["volume"] > last["vol_ma"] * 1.5:
            reasons.append("всплеск объема")
            votes += 1 if votes > 0 else -1

        # 6. Дивергенция / Паттерны
        if last["rsi_div"] == "bull": votes += 2; reasons.append("дивергенция BUY")
        elif last["rsi_div"] == "bear": votes -= 2; reasons.append("дивергенция SELL")
        
        try:
            patterns = analysis.candle_patterns(df)
            for name, direction in patterns:
                if direction == "bull": votes += 1; reasons.append("свеча: " + name)
                elif direction == "bear": votes -= 1; reasons.append("свеча: " + name)
        except: pass

        return votes, reasons

    def analyze_filtered(self, tf, tp_mult=3.0):
        # Проверяем время (торгуем только в активные часы)
        if not is_good_time():
            return Signal(tf, "HOLD", 0, 0, ["[фильтр] ночное время, ждем утро"], 0, 0, 0, self.symbol)

        lookback = 300
        df = self.fetch(tf, lookback)
        df = self.enrich(df, tf).dropna().reset_index(drop=True)
        if len(df) < 2:
            raise RuntimeError("недостаточно данных")
            
        votes, reasons = self.score(df)
        last = df.iloc[-1]
        price = float(last["close"])
        atr_v = float(last["atr"])
        
        # ATR-стоп (защита от шума)
        atr_mult = 1.2 if tf == "1m" else 1.5
        min_votes = 4
        
        conf = min(abs(votes) / MAX_VOTES, 1.0)

        if votes >= min_votes:
            action = "BUY"
            sl = price - atr_v * atr_mult
            tp_partial = price + (atr_v * atr_mult * 2)
            tp = price + (atr_v * atr_mult * tp_mult)
        elif votes <= -min_votes:
            action = "SELL"
            sl = price + atr_v * atr_mult
            tp_partial = price - (atr_v * atr_mult * 2)
            tp = price - (atr_v * atr_mult * tp_mult)
        else:
            action = "HOLD"
            sl = tp_partial = tp = price

        return Signal(tf, action, conf, price, reasons, sl, tp, tp_partial, self.symbol)