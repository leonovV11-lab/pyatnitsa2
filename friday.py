# language: Python 3.10+, file: friday.py
from __future__ import annotations
from html import escape
import ccxt
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Literal
from enum import Enum


class Mode(Enum):
    SCALP    = "scalp"
    INTRADAY = "intraday"
    SWING    = "swing"


MODE_CONFIG = {
    Mode.SCALP:    {"tf": "1m",  "lookback": 300, "ema_fast": 9,  "ema_slow": 21,  "rsi_len": 7,  "atr_mult": 1.2, "min_votes": 2},
    Mode.INTRADAY: {"tf": "15m", "lookback": 300, "ema_fast": 20, "ema_slow": 50,  "rsi_len": 14, "atr_mult": 1.5, "min_votes": 2},
    Mode.SWING:    {"tf": "4h",  "lookback": 500, "ema_fast": 50, "ema_slow": 200, "rsi_len": 14, "atr_mult": 2.0, "min_votes": 2},
}

SENIOR_TF = {
    Mode.SCALP:    Mode.INTRADAY,
    Mode.INTRADAY: Mode.SWING,
    Mode.SWING:    None,
}


# ─────────── ИНДИКАТОРЫ (чистый pandas, без pandas-ta) ───────────

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


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
    def __init__(self, symbol: str = "BTC/USDT", exchange_name: str = "bybit"):
        self.symbol = symbol
        self.exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})

    def fetch(self, mode: Mode) -> pd.DataFrame:
        cfg = MODE_CONFIG[mode]
        raw = self.exchange.fetch_ohlcv(self.symbol, timeframe=cfg["tf"], limit=cfg["lookback"])
        if not raw or len(raw) < 60:
            raise RuntimeError(f"мало данных с биржи для {mode.value}")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df

    def enrich(self, df: pd.DataFrame, mode: Mode) -> pd.DataFrame:
        cfg = MODE_CONFIG[mode]
        df["ema_fast"] = ema(df["close"], cfg["ema_fast"])
        df["ema_slow"] = ema(df["close"], cfg["ema_slow"])
        df["rsi"]      = rsi(df["close"], cfg["rsi_len"])
        df["atr"]      = atr(df["high"], df["low"], df["close"], 14)
        macd_line, signal_line, hist = macd(df["close"])
        df["macd"]      = macd_line
        df["macd_sig"]  = signal_line
        df["macd_hist"] = hist
        df["vol_ma"]    = df["volume"].rolling(20).mean()
        return df

    def score(self, df: pd.DataFrame):
        last = df.iloc[-1]
        prev = df.iloc[-2]
        votes = 0
        reasons = []
        if last["ema_fast"] > last["ema_slow"]:
            votes += 1; reasons.append("тренд вверх")
        else:
            votes -= 1; reasons.append("тренд вниз")
        if last["rsi"] < 30:
            votes += 1; reasons.append(f"RSI перепродан ({last['rsi']:.1f})")
        elif last["rsi"] > 70:
            votes -= 1; reasons.append(f"RSI перекуплен ({last['rsi']:.1f})")
        if prev["macd"] < prev["macd_sig"] and last["macd"] > last["macd_sig"]:
            votes += 2; reasons.append("MACD бычье пересечение")
        elif prev["macd"] > prev["macd_sig"] and last["macd"] < last["macd_sig"]:
            votes -= 2; reasons.append("MACD медвежье пересечение")
        if last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]:
            votes += 1; reasons.append("гистограмма растёт")
        elif last["macd_hist"] < 0 and last["macd_hist"] < prev["macd_hist"]:
            votes -= 1; reasons.append("гистограмма падает")
        if last["vol_ma"] and last["volume"] > last["vol_ma"] * 1.5:
            reasons.append(f"всплеск объёма ({last['volume']/last['vol_ma']:.1f}x)")
            votes += 1 if votes > 0 else -1
        return votes, reasons

    def analyze(self, mode: Mode) -> Signal:
        df = self.fetch(mode)
        df = self.enrich(df, mode).dropna().reset_index(drop=True)
        if len(df) < 2:
            raise RuntimeError("недостаточно данных")
        votes, reasons = self.score(df)
        last = df.iloc[-1]
        price = float(last["close"])
        atr_val = float(last["atr"])
        cfg = MODE_CONFIG[mode]
        conf = min(abs(votes) / 6.0, 1.0)
        if votes >= cfg["min_votes"]:
            action = "BUY"
            sl = price - atr_val * cfg["atr_mult"]
            tp = price + atr_val * cfg["atr_mult"] * 2
        elif votes <= -cfg["min_votes"]:
            action = "SELL"
            sl = price + atr_val * cfg["atr_mult"]
            tp = price - atr_val * cfg["atr_mult"] * 2
        else:
            action = "HOLD"
            sl = tp = price
        return Signal(mode, action, conf, price, reasons, sl, tp, self.symbol)

    def senior_trend(self, mode: Mode):
        senior = SENIOR_TF.get(mode)
        if senior is None:
            return None
        try:
            df = self.fetch(senior)
            df = self.enrich(df, senior).dropna()
            if len(df) < 2:
                return None
            last = df.iloc[-1]
            return "up" if last["ema_fast"] > last["ema_slow"] else "down"
        except Exception:
            return None

    def analyze_filtered(self, mode: Mode) -> Signal:
        sig = self.analyze(mode)
        trend = self.senior_trend(mode)
        if trend is None:
            return sig
        if sig.action == "BUY" and trend == "down":
            sig.reasons.append(f"[фильтр] старший ТФ {trend} — BUY отменён")
            sig.action = "HOLD"
        elif sig.action == "SELL" and trend == "up":
            sig.reasons.append(f"[фильтр] старший ТФ {trend} — SELL отменён")
            sig.action = "HOLD"
        else:
            sig.reasons.append(f"[фильтр] старший ТФ {trend} — согласовано")
        return sig
