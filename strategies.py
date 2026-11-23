"""Point-in-time spot entry strategies and executable paper trade plans."""

import math

import pandas as pd

BASELINE = "trend_v1"
EXPERIMENTAL = ("pullback_v1", "breakout_v1")
RISK_PER_TRADE = 0.015
MAX_POSITION_FRACTION = 0.20
MIN_NET_PAYOFF = 1.0
MAX_SPREAD = 0.003


def is_bullish(closes):
    return closes.ewm(span=9, adjust=False).mean().iloc[-1] > closes.ewm(span=21, adjust=False).mean().iloc[-1]


def candidate_setups(symbol, five, fifteen, btc_bullish):
    if min(len(five), len(fifteen)) < 30:
        return []
    close = five["close"]
    previous_close = close.shift()
    true_range = pd.concat([
        five["high"] - five["low"], (five["high"] - previous_close).abs(),
        (five["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(pd.Series(true_range).tail(14).mean())
    last, prev = five.iloc[-1], five.iloc[-2]
    price = float(last["close"])
    if not (math.isfinite(price) and price > 0 and math.isfinite(atr) and atr > 0):
        return []
    ema9 = close.ewm(span=9, adjust=False).mean()
    trend = bool(is_bullish(fifteen["close"]))
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = -delta.clip(upper=0).rolling(14).mean().iloc[-1]
    rsi = 100 * gain / (gain + loss) if gain + loss else 50
    avg_volume = five["volume"].shift().rolling(20).mean().iloc[-1]
    high_volume = bool(avg_volume > 0 and last["volume"] >= 1.5 * avg_volume)
    engulfing = bool(last["close"] > last["open"] and prev["close"] < prev["open"]
                     and last["open"] <= prev["close"] and last["close"] >= prev["open"])
    conditions = [
        (price > ema9.iloc[-1], 15, "Цена выше EMA9"),
        (macd.iloc[-1] > macd.ewm(span=9, adjust=False).mean().iloc[-1], 20, "MACD выше сигнальной"),
        (50 < rsi < 75, 20, "RSI между 50 и 75"),
        (high_volume, 25, "Повышенный объём"),
        (engulfing, 20, "Бычье поглощение"),
    ]
    score = sum(weight for matched, weight, _ in conditions if matched)
    evidence = [label for matched, _, label in conditions if matched]
    baseline = trend and btc_bullish and score >= 65
    pullback = trend and last["low"] <= ema9.iloc[-1] < price and price > last["open"] and 45 <= rsi <= 68
    breakout = trend and high_volume and price > five["high"].shift().rolling(20).max().iloc[-1]
    stop = min(float(last["low"]) * 0.998, price - atr * 1.2)
    risk = max(price - stop, price * 0.005)
    stop = price - risk
    if stop <= 0:
        return []
    return [
        {
            "strategy": strategy, "symbol": symbol, "candle": last["ts"].isoformat(),
            "reference": price, "stop": stop, "tp1": price + risk, "tp2": price + 2.5 * risk,
            "score": score, "atr": atr, "btc_bullish": bool(btc_bullish), "evidence": reasons,
            "eligible": bool(eligible), "rule_reason": reason,
        }
        for strategy, eligible, reason, reasons in (
            (BASELINE, baseline, "Тренд, режим BTC или рейтинг ниже требований", evidence),
            ("pullback_v1", pullback, "Нет подтверждённого отката к EMA9", ["Откат к EMA9"]),
            ("breakout_v1", breakout, "Нет пробоя диапазона с объёмом", ["Пробой максимума 20 свечей с объёмом"]),
        )
    ]


def build_plan(candidate, quote, budget, fee_rate):
    """Return a fresh plan, or a reason why the current quote cannot support it."""
    if not math.isfinite(budget) or budget <= 0:
        return None, "Бумажный бюджет не настроен"
    if quote is None or not quote.is_fresh():
        return None, "Нет свежей котировки"
    ask, bid = quote.ask, quote.bid
    stop, tp1, tp2 = (candidate[key] for key in ("stop", "tp1", "tp2"))
    if bid <= stop:
        return None, "Цена достигла уровня отмены"
    spread = (ask - bid) / ask
    if spread > MAX_SPREAD:
        return None, "Спред выше лимита"

    # The reward case assumes both profit targets are reached. It is not expectancy.
    target = (tp1 + tp2) / 2
    upper = (target * (1 - fee_rate) + MIN_NET_PAYOFF * stop * (1 - fee_rate)) / (
        (1 + MIN_NET_PAYOFF) * (1 + fee_rate)
    )
    lower = candidate["reference"] - min(candidate["atr"] * 0.5, (candidate["reference"] - stop) / 2)
    upper = min(upper, tp1, candidate["reference"] + candidate["atr"] * 0.5)
    if not lower <= ask <= upper or ask >= tp1:
        return None, "Цена вне диапазона входа"
    per_unit_loss = ask * (1 + fee_rate) - stop * (1 - fee_rate)
    per_unit_reward = target * (1 - fee_rate) - ask * (1 + fee_rate)
    quantity = min(budget * RISK_PER_TRADE / per_unit_loss,
                   budget * MAX_POSITION_FRACTION / (ask * (1 + fee_rate)))
    if quantity > quote.ask_size or quantity > quote.bid_size:
        return None, "Недостаточный объём на лучших ценах"
    return {
        "symbol": candidate["symbol"], "score": candidate["score"], "strategy": candidate["strategy"],
        "price": ask, "bid": bid, "qty": quantity, "stop": stop, "tp1": tp1, "tp2": tp2,
        "loss": quantity * per_unit_loss, "reward": quantity * per_unit_reward,
        "ratio": per_unit_reward / per_unit_loss, "spread_pct": spread,
        "entry_min": lower, "entry_max": upper, "quote_observed_at": quote.observed_at,
    }, ""
