# language: Python, file: portfolio.py
import json
import os
from datetime import datetime

from config import PORTFOLIO_FILE, FEE_ROUND


def load() -> dict:
    if not os.path.exists(PORTFOLIO_FILE):
        return {"open": None, "history": []}
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"open": None, "history": []}


def save(data: dict):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def open_position(symbol: str, side: str, price: float):
    data = load()
    data["open"] = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "opened_at": datetime.utcnow().isoformat(),
    }
    save(data)


def close_position(price: float) -> dict:
    data = load()
    pos = data.get("open")
    if not pos:
        return {"error": "нет открытой позиции"}
    entry = pos["entry"]
    side = pos["side"]
    if side == "BUY":
        raw = (price - entry) / entry
    else:
        raw = (entry - price) / entry
    pnl = (raw - FEE_ROUND) * 100

    opened_at = datetime.fromisoformat(pos["opened_at"])
    held_min = int((datetime.utcnow() - opened_at).total_seconds() / 60)

    trade = {
        "symbol": pos["symbol"],
        "side": side,
        "entry": entry,
        "exit": price,
        "pnl_pct": round(pnl, 3),
        "held_min": held_min,
        "closed_at": datetime.utcnow().isoformat(),
    }
    data["open"] = None
    data["history"].append(trade)
    save(data)
    return trade


def stats() -> dict:
    data = load()
    history = data.get("history", [])
    if not history:
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0,
                "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "pf": 0,
                "open": data.get("open")}
    wins = [t for t in history if t["pnl_pct"] > 0]
    losses = [t for t in history if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in history)
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    return {
        "total": len(history),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(history) * 100,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pf": pf,
        "open": data.get("open"),
    }


def current_pnl(price: float):
    data = load()
    pos = data.get("open")
    if not pos:
        return None
    entry = pos["entry"]
    side = pos["side"]
    if side == "BUY":
        raw = (price - entry) / entry
    else:
        raw = (entry - price) / entry
    return {
        "pnl_pct": raw * 100,
        "entry": entry,
        "side": side,
    }
