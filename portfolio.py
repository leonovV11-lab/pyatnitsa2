# language: Python, file: portfolio.py
import json
import os
from datetime import datetime
from config import PORTFOLIO_FILE, FEE_ROUND


def load():
    if not os.path.exists(PORTFOLIO_FILE):
        return {"open": None, "history": []}
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "history" not in data:
            data["history"] = []
        return data
    except Exception:
        return {"open": None, "history": []}


def save(data):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def open_position(symbol, side, price, qty, source="manual"):
    data = load()
    data["open"] = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "qty": qty,
        "source": source,
        "peak": price,
        "opened_at": datetime.utcnow().isoformat(),
    }
    save(data)


def update_peak(price):
    data = load()
    pos = data.get("open")
    if not pos:
        return
    peak = pos.get("peak", pos["entry"])
    if pos["side"] == "BUY" and price > peak:
        pos["peak"] = price
        data["open"] = pos
        save(data)
    if pos["side"] == "SELL" and price < peak:
        pos["peak"] = price
        data["open"] = pos
        save(data)


def close_position(price, qty_to_sell):
    data = load()
    pos = data.get("open")
    if not pos:
        return {"error": "нет позиции"}
    if qty_to_sell > pos["qty"] + 1e-8:
        return {"error": "мало монет"}
    entry = pos["entry"]
    side = pos["side"]
    source = pos.get("source", "manual")
    if side == "BUY":
        raw = (price - entry) / entry
        abs_pnl = (price - entry) * qty_to_sell
    else:
        raw = (entry - price) / entry
        abs_pnl = (entry - price) * qty_to_sell
    fee = entry * qty_to_sell * FEE_ROUND
    pnl_pct = (raw - FEE_ROUND) * 100
    abs_pnl = abs_pnl - fee
    opened_at = datetime.fromisoformat(pos["opened_at"])
    held_min = int((datetime.utcnow() - opened_at).total_seconds() / 60)
    trade = {
        "symbol": pos["symbol"],
        "side": side,
        "entry": entry,
        "exit": price,
        "qty": qty_to_sell,
        "pnl_pct": round(pnl_pct, 3),
        "pnl_abs": round(abs_pnl, 4),
        "held_min": held_min,
        "source": source,
        "closed_at": datetime.utcnow().isoformat(),
    }
    pos["qty"] = pos["qty"] - qty_to_sell
    if pos["qty"] <= 1e-8:
        data["open"] = None
        trade["remaining"] = 0
    else:
        data["open"] = pos
        trade["remaining"] = pos["qty"]
    data["history"].append(trade)
    save(data)
    return trade


def current_pnl(price):
    data = load()
    pos = data.get("open")
    if not pos:
        return None
    entry = pos["entry"]
    qty = pos["qty"]
    side = pos["side"]
    if side == "BUY":
        raw = (price - entry) / entry
        abs_pnl = (price - entry) * qty
    else:
        raw = (entry - price) / entry
        abs_pnl = (entry - price) * qty
    return {
        "pnl_pct": raw * 100,
        "pnl_abs": abs_pnl,
        "entry": entry,
        "qty": qty,
        "side": side,
        "symbol": pos["symbol"],
        "peak": pos.get("peak", entry),
    }


def history_filtered(period="all", source="all"):
    data = load()
    hist = data.get("history", [])
    now = datetime.utcnow()
    out = []
    for t in hist:
        try:
            closed = datetime.fromisoformat(t["closed_at"])
        except Exception:
            continue
        if period == "day":
            if (now - closed).total_seconds() > 86400:
                continue
        elif period == "week":
            if (now - closed).total_seconds() > 604800:
                continue
        if source != "all" and t.get("source", "manual") != source:
            continue
        out.append(t)
    return out


def stats_filtered(period="all", source="all"):
    hist = history_filtered(period, source)
    if not hist:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0,
            "total_pnl": 0,
            "total_abs": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "pf": 0,
        }
    wins = [t for t in hist if t["pnl_pct"] > 0]
    losses = [t for t in hist if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in hist)
    total_abs = sum(t.get("pnl_abs", 0) for t in hist)
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    return {
        "total": len(hist),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(hist) * 100,
        "total_pnl": total_pnl,
        "total_abs": total_abs,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pf": pf,
    }