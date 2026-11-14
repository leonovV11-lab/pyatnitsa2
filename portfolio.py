# language: Python, file: portfolio.py
import json
import os
from datetime import datetime
from config import PORTFOLIO_FILE, FEE_ROUND

MAX_POSITIONS = 2

def _empty():
    return {"opens": [], "history": []}

def _migrate(data):
    if "opens" not in data:
        data["opens"] = []
        if data.get("open"):
            data["opens"].append(data["open"])
            data["open"] = None
    if "history" not in data:
        data["history"] = []
    return data

def load():
    if not os.path.exists(PORTFOLIO_FILE):
        return _empty()
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _migrate(data)
    except Exception:
        return _empty()

def save(data):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def count_open():
    return len(load().get("opens", []))

def find_open(symbol=None):
    data = load()
    for p in data.get("opens", []):
        if symbol is None or p["symbol"] == symbol:
            return p
    return None

def open_position(symbol, side, price, qty, source="manual", sl=0.0, tp=0.0, partial_tp=0.0):
    data = load()
    opens = data.get("opens", [])
    if len(opens) >= MAX_POSITIONS:
        return {"error": "максимум " + str(MAX_POSITIONS) + " позиции"}
    for p in opens:
        if p["symbol"] == symbol:
            return {"error": "уже есть позиция по " + symbol}
    pos = {"symbol": symbol, "side": side, "entry": price, "qty": qty,
           "source": source, "peak": price, "sl": sl, "tp": tp,
           "partial_tp": partial_tp,
           "opened_at": datetime.utcnow().isoformat()}
    opens.append(pos)
    data["opens"] = opens
    save(data)
    return {"ok": True, "pos": pos}

def update_peak(price, symbol=None):
    data = load()
    opens = data.get("opens", [])
    changed = False
    for pos in opens:
        if symbol and pos["symbol"] != symbol:
            continue
        peak = pos.get("peak", pos["entry"])
        if pos["side"] == "BUY" and price > peak:
            pos["peak"] = price
            changed = True
        if pos["side"] == "SELL" and price < peak:
            pos["peak"] = price
            changed = True
    if changed:
        data["opens"] = opens
        save(data)

def close_position(price, qty_to_sell, symbol=None):
    data = load()
    opens = data.get("opens", [])
    target = None
    for p in opens:
        if symbol is None or p["symbol"] == symbol:
            target = p
            break
    if not target:
        return {"error": "нет позиции"}
    if qty_to_sell > target["qty"] + 1e-8:
        return {"error": "мало монет"}
    entry, side = target["entry"], target["side"]
    src = target.get("source", "manual")
    if side == "BUY":
        raw = (price - entry) / entry
        abs_pnl = (price - entry) * qty_to_sell
    else:
        raw = (entry - price) / entry
        abs_pnl = (entry - price) * qty_to_sell
    fee = entry * qty_to_sell * FEE_ROUND
    pnl_pct = (raw - FEE_ROUND) * 100
    abs_pnl = abs_pnl - fee
    try:
        opened_at = datetime.fromisoformat(target["opened_at"])
    except Exception:
        opened_at = datetime.utcnow()
    held_min = int((datetime.utcnow() - opened_at).total_seconds() / 60)
    trade = {"symbol": target["symbol"], "side": side, "entry": entry,
             "exit": price, "qty": qty_to_sell,
             "pnl_pct": round(pnl_pct, 3), "pnl_abs": round(abs_pnl, 4),
             "held_min": held_min, "source": src,
             "closed_at": datetime.utcnow().isoformat()}
    target["qty"] -= qty_to_sell
    if target["qty"] <= 1e-8:
        opens.remove(target)
        trade["remaining"] = 0
    else:
        trade["remaining"] = target["qty"]
    data["opens"] = opens
    data["history"].append(trade)
    save(data)
    return trade

def current_pnl(price, symbol=None):
    pos = find_open(symbol)
    if not pos:
        return None
    entry, qty, side = pos["entry"], pos["qty"], pos["side"]
    if side == "BUY":
        raw = (price - entry) / entry
        abs_pnl = (price - entry) * qty
    else:
        raw = (entry - price) / entry
        abs_pnl = (entry - price) * qty
    return {"pnl_pct": raw * 100, "pnl_abs": abs_pnl, "entry": entry,
            "qty": qty, "side": side, "symbol": pos["symbol"],
            "peak": pos.get("peak", entry)}

def all_pnl(price_map):
    data = load()
    out = []
    for pos in data.get("opens", []):
        sym = pos["symbol"]
        price = price_map.get(sym)
        if not price:
            continue
        p = current_pnl(price, sym)
        if p:
            out.append(p)
    return out

def close_all_positions(price_map):
    data = load()
    results = []
    for pos in list(data.get("opens", [])):
        sym = pos["symbol"]
        price = price_map.get(sym, pos["entry"])
        t = close_position(price, pos["qty"], sym)
        results.append(t)
    return results

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
        if period == "day" and (now - closed).total_seconds() > 86400:
            continue
        if period == "week" and (now - closed).total_seconds() > 604800:
            continue
        if source != "all" and t.get("source", "manual") != source:
            continue
        out.append(t)
    return out

def stats_filtered(period="all", source="all"):
    hist = history_filtered(period, source)
    if not hist:
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0,
                "total_pnl": 0, "total_abs": 0, "avg_win": 0,
                "avg_loss": 0, "pf": 0}
    wins = [t for t in hist if t["pnl_pct"] > 0]
    losses = [t for t in hist if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in hist)
    total_abs = sum(t.get("pnl_abs", 0) for t in hist)
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    gw = sum(t["pnl_pct"] for t in wins)
    gl = abs(sum(t["pnl_pct"] for t in losses))
    pf = gw / gl if gl > 0 else 0
    return {"total": len(hist), "wins": len(wins), "losses": len(losses),
            "winrate": len(wins) / len(hist) * 100, "total_pnl": total_pnl,
            "total_abs": total_abs, "avg_win": avg_win,
            "avg_loss": avg_loss, "pf": pf}