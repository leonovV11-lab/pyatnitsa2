# language: Python, file: analysis.py
import pandas as pd
import numpy as np


def bollinger(close, length=20, mult=2.0):
    ma = close.rolling(length).mean()
    sd = close.rolling(length).std()
    upper = ma + mult * sd
    lower = ma - mult * sd
    return ma, upper, lower


def stochastic(high, low, close, k=14, d=3):
    ll = low.rolling(k).min()
    hh = high.rolling(k).max()
    rng = (hh - ll).replace(0, np.nan)
    fk = 100 * (close - ll) / rng
    fd = fk.rolling(d).mean()
    return fk.fillna(50), fd.fillna(50)


def adx(high, low, close, length=14):
    up = high.diff()
    dn = -low.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1.0 / length, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1.0 / length, adjust=False).mean() / atr_v
    ndi = 100 * ndm.ewm(alpha=1.0 / length, adjust=False).mean() / atr_v
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    adx_v = dx.ewm(alpha=1.0 / length, adjust=False).mean()
    return adx_v.fillna(0), pdi.fillna(0), ndi.fillna(0)


def obv_series(close, volume):
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def rsi_divergence(close, rsi, lookback=20):
    if len(close) < lookback:
        return "none"
    c = close.tail(lookback).values
    r = rsi.tail(lookback).values
    mid = lookback // 2
    c1, c2 = float(c[:mid].min()), float(c[mid:].min())
    r1, r2 = float(r[:mid].min()), float(r[mid:].min())
    if c2 < c1 and r2 > r1:
        return "bull"
    c1, c2 = float(c[:mid].max()), float(c[mid:].max())
    r1, r2 = float(r[:mid].max()), float(r[mid:].max())
    if c2 > c1 and r2 < r1:
        return "bear"
    return "none"


def candle_patterns(df):
    """возвращает список найденных паттернов на последней свече."""
    if len(df) < 3:
        return []
    c = df.iloc[-1]
    p = df.iloc[-2]
    pp = df.iloc[-3]
    o = float(c["open"])
    h = float(c["high"])
    l = float(c["low"])
    cl = float(c["close"])
    body = abs(cl - o)
    rng = h - l
    if rng == 0:
        return []
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    out = []

    if body / rng < 0.1:
        out.append(("дожи", "neutral"))

    if lower > body * 2 and upper < body:
        out.append(("молот", "bull"))

    if upper > body * 2 and lower < body:
        out.append(("падающая звезда", "bear"))

    p_open = float(p["open"])
    p_close = float(p["close"])

    if p_close < p_open and cl > o and cl > p_open and o < p_close:
        out.append(("бычье поглощение", "bull"))

    if p_close > p_open and cl < o and cl < p_open and o > p_close:
        out.append(("медвежье поглощение", "bear"))

    pp_mid = (float(pp["open"]) + float(pp["close"])) / 2

    if (pp["close"] < pp["open"] and abs(p_close - p_open) < body * 0.5
            and cl > o and cl > pp_mid):
        out.append(("утренняя звезда", "bull"))

    if (pp["close"] > pp["open"] and abs(p_close - p_open) < body * 0.5
            and cl < o and cl < pp_mid):
        out.append(("вечерняя звезда", "bear"))

    if (p_close > p_open and pp["close"] > pp["open"] and cl > o
            and cl > p_close > float(pp["close"])):
        out.append(("три белых солдата", "bull"))

    if (p_close < p_open and pp["close"] < pp["open"] and cl < o
            and cl < p_close < float(pp["close"])):
        out.append(("три чёрных вороны", "bear"))

    if body / rng > 0.9:
        if cl > o:
            out.append(("марибозу бычья", "bull"))
        else:
            out.append(("марибозу медвежья", "bear"))

    return out


def support_resistance(df, lookback=5, max_levels=3):
    highs = df["high"].values
    lows = df["low"].values
    res = []
    sup = []
    for i in range(lookback, len(df) - lookback):
        h = highs[i]
        lo = lows[i]
        if h == max(highs[i - lookback:i + lookback + 1]):
            res.append(float(h))
        if lo == min(lows[i - lookback:i + lookback + 1]):
            sup.append(float(lo))

    def cluster(levels, tol=0.005):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for x in levels[1:]:
            if abs(x - clusters[-1][-1]) / clusters[-1][-1] < tol:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        return [sum(c) / len(c) for c in clusters]

    return cluster(sup)[-max_levels:], cluster(res)[-max_levels:]
