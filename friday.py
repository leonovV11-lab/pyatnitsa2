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

SENIOR_TF = {Mode.SCALP: Mode.INTRADAY, Mode.INTRADAY: Mode.SWING, Mode.SWING: None}
MAX_VOTES = 10.0


def is_good_time():
    """Фильтр времени: не торгуем ночью (флэт)"""
    hour = datetime.datetime.utcnow().hour
    return not (3 <= hour < 7)


def fmt_price(price):
    p = float(price)
    if p == 0: return "0"
    if p < 0.0001: return f"{p:.8f}"
    if p < 0.01: return f"{p:.6f}"
    return f"{p:.4f}"


def ema(series, length): return series.ewm(span=length, adjust=False).mean()


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
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()


def macd_calc(series, fast=12, slow=26, signal=9):
    ef = series.ewm(span=fast, adjust=False).mean()
    es = series.ewm(span=slow, adjust=False).mean()
    m = ef - es
    s = m.ewm(span=signal, adjust=False).mean()
    return m, s, m - s


@dataclass
class Signal:
    mode: str
    action: Literal["BUY", "SELL"]
    confidence: float
    price: float
    reasons: list
    stop_loss: float
    take_profit: float
    partial_tp: float
    symbol: str = ""

    def pretty(self) -> str:
        lines = ["<b>" + self.action + "</b> · " + str(self.mode).upper()]
        lines.append("пара: <code>" + escape(self.symbol) + "</code>")
        lines.append("цена: <code>" + fmt_price(self.price) + "</code>")
        lines.append("уверенность: <b>" + str(int(self.confidence * 100)) + "%</b>")
        lines.append("стоп: <code>" + fmt_price(self.stop_loss) + "</code>")
        lines.append("тейк 1: <code>" + fmt_price(self.partial_tp) + "</code>")
        lines.append("тейк 2: <code>" + fmt_price(self.take_profit) + "</code>")
        lines.append("")
        lines.append("причины:")
        for r in self.reasons: lines.append("  • " + escape(str(r)))
        return "\n".join(lines)


class Friday:
    def __init__(self, symbol="BTC/USDT", exchange_name="bybit"):
        self.symbol = symbol
        self.exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})

    def fetch(self, tf, lookback=300):
        raw = self.exchange.fetch_ohlcv(self.symbol, timeframe=tf, limit=lookback)
        if not raw or len(raw) < 60: raise RuntimeError("мало данных")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df

    def enrich(self, df, tf):
        df["ema_fast"] = ema(df["close"], 9 if tf=="1m" else 20)
        df["ema_slow"] = ema(df["close"], 21 if tf=="1m" else 50)
        df["rsi"] = rsi(df["close"], 7 if tf=="1m" else 14)
        df["atr"] = atr(df["high"], df["low"], df["close"], 14)
        m, s, h = macd_calc(df["close"])
        df["macd"], df["macd_sig"], df["macd_hist"] = m, s, h
        df["vol_ma"] = df["volume"].rolling(20).mean()
        ma, up, lo = analysis.bollinger(df["close"], 20, 2.0)
        df["bb_upper"], df["bb_lower"] = up, lo
        k, d = analysis.stochastic(df["high"], df["low"], df["close"], 14, 3)
        df["stoch_k"], df["stoch_d"] = k, d
        adx_v, pdi, ndi = analysis.adx(df["high"], df["low"], df["close"], 14)
        df["adx"], df["plus_di"], df["minus_di"] = adx_v, pdi, ndi
        df["obv"] = analysis.obv_series(df["close"], df["volume"])
        df["obv_ma"] = df["obv"].rolling(20).mean()
        df["rsi_div"] = "none"
        try:
            r = rsi(df["close"], 7 if tf=="1m" else 14)
            df.iloc[-1, df.columns.get_loc("rsi_div")] = analysis.rsi_divergence(df["close"], r, 20)
        except: pass
        return df

    def score(self, df):
        last, prev = df.iloc[-1], df.iloc[-2]
        votes, reasons = 0, []
        closes, opens = df["close"].tail(30).values, df["open"].tail(30).values
        bias = sum(1 for i in range(len(closes)) if closes[i] > opens[i]) / len(closes)
        if bias > 0.6: votes += 1; reasons.append("свечи BUY")
        elif bias < 0.4: votes -= 1; reasons.append("свечи SELL")
        if last["ema_fast"] > last["ema_slow"]: votes += 1; reasons.append("EMA вверх")
        else: votes -= 1; reasons.append("EMA вниз")
        if last["rsi"] < 30: votes += 1; reasons.append("RSI перепродан")
        elif last["rsi"] > 70: votes -= 1; reasons.append("RSI перекуплен")
        if prev["macd"] < prev["macd_sig"] and last["macd"] > last["macd_sig"]: votes += 2; reasons.append("MACD бычье")
        elif prev["macd"] > prev["macd_sig"] and last["macd"] < last["macd_sig"]: votes -= 2; reasons.append("MACD медвежье")
        if last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]: votes += 1; reasons.append("MACD гистограмма вверх")
        elif last["macd_hist"] < 0 and last["macd_hist"] < prev["macd_hist"]: votes -= 1; reasons.append("MACD гистограмма вниз")
        if last["vol_ma"] and last["volume"] > last["vol_ma"] * 1.5:
            reasons.append("объем")
            votes += 1 if votes > 0 else -1
        if last["close"] < last["bb_lower"]: votes += 1; reasons.append("ниже Боллинджер")
        elif last["close"] > last["bb_upper"]: votes -= 1; reasons.append("выше Боллинджер")
        if last["stoch_k"] < 20 and last["stoch_d"] < 20: votes += 1; reasons.append("стохастик перепродан")
        elif last["stoch_k"] > 80 and last["stoch_d"] > 80: votes -= 1; reasons.append("стохастик перекуплен")
        if last["adx"] > 25:
            if last["plus_di"] > last["minus_di"]: votes += 1; reasons.append("ADX вверх")
            else: votes -= 1; reasons.append("ADX вниз")
        if last["rsi_div"] == "bull": votes += 2; reasons.append("дивергенция BUY")
        elif last["rsi_div"] == "bear": votes -= 2; reasons.append("дивергенция SELL")
        try:
            for name, direction in analysis.candle_patterns(df):
                if direction == "bull": votes += 1; reasons.append("свеча " + name)
                elif direction == "bear": votes -= 1; reasons.append("свеча " + name)
        except: pass
        return votes, reasons

    def analyze_filtered(self, tf, tp_mult=3.0):
        df = self.fetch(tf, 300)
        df = self.enrich(df, tf).dropna().reset_index(drop=True)
        if len(df) < 2: raise RuntimeError("мало данных")
        votes, reasons = self.score(df)
        last = df.iloc[-1]
        price, atr_v = float(last["close"]), float(last["atr"])
        atr_mult = 1.2 if tf == "1m" else 1.5
        conf = min(abs(votes) / MAX_VOTES, 1.0)
        
        # Если голосов нет или они слабые, принудительно делаем SELL,
        # чтобы бот никогда не выдавал HOLD, а показывал хоть какое-то направление.
        if votes > 0:
            action = "BUY"
            sl = price - atr_v * atr_mult
            partial_tp = price + (atr_v * atr_mult * 2)
            tp = price + (atr_v * atr_mult * tp_mult)
        else:
            action = "SELL"
            sl = price + atr_v * atr_mult
            partial_tp = price - (atr_v * atr_mult * 2)
            tp = price - (atr_v * atr_mult * tp_mult)
            
        return Signal(tf, action, conf, price, reasons, sl, tp, partial_tp, self.symbol)