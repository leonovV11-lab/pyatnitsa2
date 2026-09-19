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


class Mode(Enum):
    SCALP    = "scalp"
    INTRADAY = "intraday"
    SWING    = "swing"


MODE_CONFIG = {
    Mode.SCALP:    {"tf": "1m",  "lookback": 300, "ema_fast": 9,  "ema_slow": 21,  "rsi_len": 7,  "atr_mult": 1.2, "min_votes": 3},
    Mode.INTRADAY: {"tf": "15m", "lookback": 300, "ema_fast": 20, "ema_slow": 50,  "rsi_len": 14, "atr_mult": 1.5, "min_votes": 3},
    Mode.SWING:    {"tf": "4h",  "lookback": 500, "ema_fast": 50, "ema_slow": 200, "rsi_len": 14, "atr_mult": 2.0, "min_votes": 3},
}

SENIOR_TF = {
    Mode.SCALP:    Mode.INTRADAY,
    Mode.INTRADAY: Mode.SWING,
    Mode.SWING:    None,
}

MAX_VOTES = 8.0


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
    tr = pd.concat([
        high - low,
        (high - pc).abs(),
        (low - pc).abs(),
    ], axis=1).max(axis=1)
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
    mode: Mode
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    price: float
    reasons: list
    stop_loss: float
    take_profit: float
    symbol: str = ""

    def pretty(self) -> str:
        arrow = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}[self.action]
        lines = [
            f"{arrow} <b>{self.action}</b>  ·  {self.mode.value.upper()}",
            f"пара: <code>{escape(self.symbol)}</code>",
            f"цена: <code>{self.price:.4f}</code>",
            f"уверенность: <b>{self.confidence:.0%}</b>",
        ]
        if self.action != "HOLD":
            lines.append(f"стоп: <code>{self.stop_loss:.4f}</code>")
            lines.append(f"тейк: <code>{self.take_profit:.4f}</code>")
        lines.append("")
        lines.append("причины:")
        for r in self.reasons:
            lines.append(f"  • {escape(str(r))}")
        return "\n".join(lines)


class Friday:
    def __init__(self, symbol="BTC/USDT", exchange_name="bybit"):
        self.symbol = symbol
        self.exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})

    def fetch(self, mode):
        cfg = MODE_CONFIG[mode]
        raw = self.exchange.fetch_ohlcv(
            self.symbol, timeframe=cfg["tf"], limit=cfg["lookback"])
        if not raw or len(raw) < 60:
            raise RuntimeError(f"мало данных для {mode.value}")
        df = pd.DataFrame(
            raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df

    def enrich(self, df, mode):
        cfg = MODE_CONFIG[mode]
        df["ema_fast"] = ema(df["close"], cfg["ema_fast"])
        df["ema_slow"] = ema(df["close"], cfg["ema_slow"])
        df["rsi"] = rsi(df["close"], cfg["rsi_len"])
        df["atr"] = atr(df["high"], df["low"], df["close"], 14)
        m, s, h = macd_calc(df["close"])
        df["macd"] = m
        df["macd_sig"] = s
        df["macd_hist"] = h
        df["vol_ma"] = df["volume"].rolling(20).mean()

        ma, up, lo = analysis.bollinger(df["close"], 20, 2.0)
        df["bb_mid"] = ma
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

        divs = ["none"] * len(df)
        try:
            r = rsi(df["close"], cfg["rsi_len"])
            divs[-1] = analysis.rsi_divergence(df["close"], r, 20)
        except Exception:
            pass
        df["rsi_div"] = divs

        near_sup = [False] * len(df)
        near_res = [False] * len(df)
        try:
            sup, res = analysis.support_resistance(df, 5, 3)
            lc = float(df["close"].iloc[-1])
            if sup:
                near_sup[-1] = any(abs(lc - x) / lc < 0.01 for x in sup)
            if res:
                near_res[-1] = any(abs(lc - x) / lc < 0.01 for x in res)
        except Exception:
            pass
        df["near_support"] = near_sup
        df["near_resistance"] = near_res

        return df

    def score(self, df):
        last = df.iloc[-1]
        prev = df.iloc[-2]
        votes = 0
        reasons = []

        if last["ema_fast"] > last["ema_slow"]:
            votes += 1
            reasons.append("EMA тренд вверх")
        else:
            votes -= 1
            reasons.append("EMA тренд вниз")

        if last["rsi"] < 30:
            votes += 1
            reasons.append(f"RSI перепродан ({last['rsi']:.0f})")
        elif last["rsi"] > 70:
            votes -= 1
            reasons.append(f"RSI перекуплен ({last['rsi']:.0f})")

        if prev["macd"] < prev["macd_sig"] and last["macd"] > last["macd_sig"]:
            votes += 2
            reasons.append("MACD бычье пересечение")
        elif prev["macd"] > prev["macd_sig"] and last["macd"] < last["macd_sig"]:
            votes -= 2
            reasons.append("MACD медвежье пересечение")

        if last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]:
            votes += 1
            reasons.append("гистограмма MACD растёт")
        elif last["macd_hist"] < 0 and last["macd_hist"] < prev["macd_hist"]:
            votes -= 1
            reasons.append("гистограмма MACD падает")

        if last["vol_ma"] and last["volume"] > last["vol_ma"] * 1.5:
            mult = last["volume"] / last["vol_ma"]
            reasons.append(f"всплеск объёма ({mult:.1f}x)")
            votes += 1 if votes > 0 else -1

        if last["close"] < last["bb_lower"]:
            votes += 1
            reasons.append("ниже нижней Боллинджер")
        elif last["close"] > last["bb_upper"]:
            votes -= 1
            reasons.append("выше верхней Боллинджер")

        if last["stoch_k"] < 20 and last["stoch_d"] < 20:
            votes += 1
            reasons.append("стохастик перепродан")
        elif last["stoch_k"] > 80 and last["stoch_d"] > 80:
            votes -= 1
            reasons.append("стохастик перекуплен")

        if prev["stoch_k"] < prev["stoch_d"] and last["stoch_k"] > last["stoch_d"]:
            votes += 1
            reasons.append("стохастик бычье пересечение")
        elif prev["stoch_k"] > prev["stoch_d"] and last["stoch_k"] < last["stoch_d"]:
            votes -= 1
            reasons.append("стохастик медвежье пересечение")

        if last["adx"] > 25:
            av = last["adx"]
            if last["plus_di"] > last["minus_di"]:
                votes += 1
                reasons.append(f"ADX {av:.0f} тренд вверх")
            else:
                votes -= 1
                reasons.append(f"ADX {av:.0f} тренд вниз")

        if last["rsi_div"] == "bull":
            votes += 2
            reasons.append("бычья дивергенция RSI")
        elif last["rsi_div"] == "bear":
            votes -= 2
            reasons.append("медвежья дивергенция RSI")

        try:
            patterns = analysis.candle_patterns(df)
        except Exception:
            patterns = []
        for name, direction in patterns:
            if direction == "bull":
                votes += 1
                reasons.append(f"свеча: {name}")
            elif direction == "bear":
                votes -= 1
                reasons.append(f"свеча: {name}")

        if last["near_support"]:
            votes += 1
            reasons.append("цена у поддержки")
        if last["near_resistance"]:
            votes -= 1
            reasons.append("цена у сопротивления")

        if (last["obv_ma"] and last["obv"] > last["obv_ma"]
                and last["obv"] > prev["obv"]):
            votes += 1
            reasons.append("OBV растёт")
        elif (last["obv_ma"] and last["obv"] < last["obv_ma"]
                and last["obv"] < prev["obv"]):
            votes -= 1
            reasons.append("OBV падает")

        return votes, reasons

    def analyze(self, mode):
        df = self.fetch(mode)
        df = self.enrich(df, mode).dropna().reset_index(drop=True)
        if len(df) < 2:
            raise RuntimeError("недостаточно данных после индикаторов")
        votes, reasons = self.score(df)
        last = df.iloc[-1]
        price = float(last["close"])
        atr_v = float(last["atr"])
        cfg = MODE_CONFIG[mode]
        conf = min(abs(votes) / MAX_VOTES, 1.0)

        if votes >= cfg["min_votes"]:
            action = "BUY"
            sl = price - atr_v * cfg["atr_mult"]
            tp = price + atr_v * cfg["atr_mult"] * 3
        elif votes <= -cfg["min_votes"]:
            action = "SELL"
            sl = price + atr_v * cfg["atr_mult"]
            tp = price - atr_v * cfg["atr_mult"] * 3
        else:
            action = "HOLD"
            sl = tp = price

        return Signal(mode, action, conf, price, reasons, sl, tp, self.symbol)

    def senior_trend(self, mode):
        senior = SENIOR_TF.get(mode)
        if senior is None:
            return None
        try:
            df = self.fetch(senior)
            df = self.enrich(df, senior).dropna()
            if len(df) < 2:
                return None
            last = df.iloc[-1]
            if last["ema_fast"] > last["ema_slow"]:
                return "up"
            return "down"
        except Exception:
            return None

    def analyze_filtered(self, mode):
        sig = self.analyze(mode)
        trend = self.senior_trend(mode)
        if trend is None:
            return sig
        if sig.action == "BUY" and trend == "down":
            sig.reasons.append("[фильтр] старший ТФ вниз — BUY отменён")
            sig.action = "HOLD"
        elif sig.action == "SELL" and trend == "up":
            sig.reasons.append("[фильтр] старший ТФ вверх — SELL отменён")
            sig.action = "HOLD"
        else:
            sig.reasons.append(f"[фильтр] старший ТФ {trend} — согласовано")
        return sig
