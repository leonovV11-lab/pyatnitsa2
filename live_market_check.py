"""Point-in-time USDT spot observations and plans for verified news."""

import asyncio
import math
import time

from pandas import DataFrame

from market import DEFAULT_QUOTE_MAX_AGE, MarketError
from strategies import BASELINE, build_plan, candidate_setups, is_bullish

FIVE_MINUTES = 5 * 60
FIFTEEN_MINUTES = 15 * 60
MAX_EVENT_AGE = 90 * 60
HISTORY_LENGTH = 50
EVIDENCE_LENGTH = 18


def _latest_close(now, interval):
    return int(now // interval) * interval


def _has_latest_candle(frame, now, interval):
    return len(frame) >= 30 and frame["ts"].iloc[-1].timestamp() + interval == _latest_close(now, interval)


def _setup_expiry_reason(setup, now):
    if setup["snapshot_5m_close"] != _latest_close(now, FIVE_MINUTES):
        return "После анализа закрылась другая 5-минутная свеча"
    if not 0 <= now - setup["event_known_at"] < MAX_EVENT_AGE:
        return "Истекло окно рыночного наблюдения"
    return ""


def _bars(frame):
    return [
        {
            "time": bar.ts.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in frame.tail(EVIDENCE_LENGTH).itertuples(index=False)
    ]


async def refresh_plan(market, setup, budget, fee_rate) -> tuple[dict | None, str]:
    """Rebuild the plan for the reviewed setup against a fresh order book."""
    if not math.isfinite(budget) or budget <= 0:
        return None, "Бюджет должен быть положительным и конечным"
    if not math.isfinite(fee_rate) or not 0 <= fee_rate < 1:
        return None, "Комиссия должна быть долей от 0 до 1"
    if reason := _setup_expiry_reason(setup, time.time()):
        return None, reason

    try:
        quote = await market.quote(setup["symbol"])
    except MarketError:
        return None, "Нет свежей котировки"

    if reason := _setup_expiry_reason(setup, time.time()):
        return None, reason
    plan, reason = build_plan(setup, quote, budget, fee_rate)
    if plan is None:
        return None, reason
    if plan["qty"] <= 0 or not all(math.isfinite(plan[key]) for key in ("qty", "loss", "reward", "ratio")):
        return None, "Размер позиции или оценка риска вне допустимого диапазона"
    return {
        **plan, "venue": quote.exchange, "fee_rate": fee_rate,
        "bid_size": quote.bid_size, "ask_size": quote.ask_size,
        "valid_until": min(
            quote.observed_at + DEFAULT_QUOTE_MAX_AGE,
            setup["snapshot_5m_close"] + FIVE_MINUTES,
            setup["event_known_at"] + MAX_EVENT_AGE,
        ),
    }, ""


def _event_bars(frame, event_known_at):
    opens = frame["ts"].map(lambda stamp: stamp.timestamp())
    return frame.loc[opens + FIVE_MINUTES <= event_known_at], frame.loc[opens >= event_known_at]


def _reaction_direction(move):
    if move > 0:
        return "up"
    if move < 0:
        return "down"
    return "flat"


async def _prepare_reaction(market, symbol, event_known_at) -> tuple[tuple[dict, DataFrame, DataFrame] | None, str]:
    if not math.isfinite(event_known_at) or not 0 <= time.time() - event_known_at < MAX_EVENT_AGE:
        return None, "Время подтверждения события вне окна рыночного наблюдения"

    requests = [market.candles(symbol, tf, HISTORY_LENGTH) for tf in ("5m", "15m")]
    if symbol != "BTC/USDT":
        requests.extend(market.candles("BTC/USDT", tf, HISTORY_LENGTH) for tf in ("5m", "15m"))
    try:
        five, fifteen, *benchmark = await asyncio.gather(*requests)
    except MarketError:
        return None, "Биржа не вернула непрерывный ряд свежих закрытых свечей"
    btc_five, btc_fifteen = benchmark or (five, fifteen)

    now = time.time()
    if now - event_known_at >= MAX_EVENT_AGE:
        return None, "Истекло окно рыночного наблюдения"
    if not (_has_latest_candle(five, now, FIVE_MINUTES) and _has_latest_candle(fifteen, now, FIFTEEN_MINUTES)):
        return None, "Недостаточно свежих закрытых свечей инструмента"
    if not (_has_latest_candle(btc_five, now, FIVE_MINUTES)
            and _has_latest_candle(btc_fifteen, now, FIFTEEN_MINUTES)):
        return None, "Недостаточно свежих закрытых свечей BTC"

    before, after = _event_bars(five, event_known_at)
    if before.empty or after.empty:
        return None, "Нет полной закрытой свечи после подтверждения события или предшествующей базы"
    _, btc_after = _event_bars(btc_five, event_known_at)
    if btc_after.empty or not btc_after["ts"].reset_index(drop=True).equals(after["ts"].reset_index(drop=True)):
        return None, "Свечи BTC и инструмента покрывают разные интервалы наблюдения"

    baseline_close = float(before["close"].iloc[-1])
    first_post_open = float(after["open"].iloc[0])
    reaction_close = float(after["close"].iloc[-1])
    move = reaction_close / first_post_open - 1
    reaction_pct = move * 100
    btc_reaction_pct = (float(btc_after["close"].iloc[-1]) / float(btc_after["open"].iloc[0]) - 1) * 100
    if not (math.isfinite(reaction_pct) and math.isfinite(btc_reaction_pct)):
        return None, "Движение цены по свечам вне допустимого диапазона"

    observation = {
        "symbol": symbol,
        "venue": market.exchange,
        "observed_at": now,
        "event_known_at": event_known_at,
        "baseline_close": baseline_close,
        "baseline_closed_at": before["ts"].iloc[-1].timestamp() + FIVE_MINUTES,
        "first_post_event_open": after["ts"].iloc[0].isoformat(),
        "first_post_event_open_price": first_post_open,
        "reaction_close": reaction_close,
        "latest_closed_at": _latest_close(now, FIVE_MINUTES),
        "direction": _reaction_direction(move),
        "move": move,
        "reaction_pct": reaction_pct,
        "btc_reaction_pct": btc_reaction_pct,
        "bars_5m": _bars(five),
        "bars_15m": _bars(fifteen),
        "btc_bars_15m": _bars(btc_fifteen),
        "btc_bullish": bool(is_bullish(btc_fifteen["close"])),
    }
    return (observation, five, fifteen), ""


async def observe_reaction(market, symbol, event_known_at) -> tuple[dict | None, str]:
    """Describe the signed move on complete candles following confirmed news."""
    prepared, reason = await _prepare_reaction(market, symbol, event_known_at)
    return (prepared[0], "") if prepared is not None else (None, reason)


async def prepare_candidate(market, symbol, event_known_at) -> tuple[dict | None, str]:
    """Filter a candidate using closed candles before sizing it against the book."""
    prepared, reason = await _prepare_reaction(market, symbol, event_known_at)
    if prepared is None:
        return None, reason
    observation, five, fifteen = prepared
    if observation["direction"] != "up" or observation["reaction_close"] <= observation["baseline_close"]:
        return None, "Закрытые свечи после подтверждения события не показывают рост для спотового входа"
    candidates = candidate_setups(symbol, five, fifteen, observation["btc_bullish"])
    candidate = next((row for row in candidates if row["strategy"] == BASELINE and row["eligible"]), None)
    if candidate is None:
        return None, "Тренд, режим BTC или оценка стратегии не подтверждают спотовый вход"

    setup = {**candidate, "event_known_at": event_known_at, "snapshot_5m_close": observation["latest_closed_at"]}
    return {"setup": setup, "market": observation}, ""


async def inspect_market(market, symbol, event_known_at, budget, fee_rate) -> tuple[dict | None, str]:
    """Build a spot entry plan from closed-candle evidence and a fresh order book."""
    draft, reason = await prepare_candidate(market, symbol, event_known_at)
    if draft is None:
        return None, reason
    plan, reason = await refresh_plan(market, draft["setup"], budget, fee_rate)
    if plan is None:
        return None, reason
    evidence = {
        **draft["market"],
        "book": {key: plan[key] for key in ("price", "bid", "ask_size", "bid_size", "quote_observed_at")},
    }
    return {"setup": draft["setup"], "plan": plan, "market": evidence}, ""
