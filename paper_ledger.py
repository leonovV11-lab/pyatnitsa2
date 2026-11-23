"""Transactional paper portfolio and a separate register of manually reported fills."""

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class LedgerError(ValueError):
    """An invalid execution, configuration, or risk limit."""


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int = 3
    max_trade_risk_fraction: float = 0.015
    max_open_risk_fraction: float = 0.03
    max_daily_loss_fraction: float = 0.03


SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    capital REAL NOT NULL,
    daily_budget REAL NOT NULL,
    cash REAL NOT NULL,
    equity_peak REAL NOT NULL,
    max_drawdown REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    initial_qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    fee_rate REAL NOT NULL,
    opened_at REAL NOT NULL,
    tp1_hit INTEGER NOT NULL DEFAULT 0,
    peak REAL NOT NULL,
    last_bid REAL NOT NULL,
    marked_at REAL NOT NULL,
    pending_exit TEXT,
    closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_paper_position
    ON paper_positions (exchange, symbol) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS closed_paper_positions
    ON paper_positions (closed_at) WHERE closed_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    pnl REAL NOT NULL,
    reason TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_by_time ON paper_fills (at);
CREATE INDEX IF NOT EXISTS fills_by_position ON paper_fills (position_id);
CREATE TABLE IF NOT EXISTS equity_points (
    minute INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_archive (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    source TEXT NOT NULL,
    snapshot BLOB NOT NULL,
    open_count INTEGER NOT NULL,
    history_count INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS manual_positions (
    position_id TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry REAL NOT NULL,
    entry_fee REAL NOT NULL,
    initial_qty REAL NOT NULL,
    qty REAL NOT NULL,
    opened_at REAL NOT NULL,
    last_fill_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS manual_fills (
    external_key TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES manual_positions(position_id),
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    pnl REAL NOT NULL,
    at REAL NOT NULL
);
"""


def _number(value, name, *, allow_zero=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise LedgerError(f"{name}: укажите число.")
    try:
        value = float(value)
    except OverflowError as exc:
        raise LedgerError(f"{name}: число вне допустимого диапазона.") from exc
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise LedgerError(f"{name}: недопустимое значение.")
    return value


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label}: пустое значение.")
    return value.strip()


def _timestamp(now):
    when = time.time() if now is None else _number(now, "Время", allow_zero=True)
    if when >= 253402300799:  # Last supported year in datetime is 9999.
        raise LedgerError("Время вне допустимого диапазона.")
    return when


def _stop_risk(qty, entry, stop, rate):
    return qty * max(0.0, entry * (1 + rate) - stop * (1 - rate))


def _open_risk(positions):
    return sum(_stop_risk(p["qty"], p["entry"], p["stop"], p["fee_rate"]) for p in positions)


def _snapshot_equity(account, positions):
    return account["cash"] + sum(p["qty"] * p["last_bid"] * (1 - p["fee_rate"]) for p in positions)


def _history_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise LedgerError("Укажите от 1 до 100 записей истории.")
    return limit


class PaperLedger:
    def __init__(self, path, *, legacy_path=None, day_timezone=UTC):
        self.path = Path(path)
        self.day_timezone = day_timezone
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(SCHEMA)
        with self._transaction() as db:
            columns = db.execute("PRAGMA table_info(paper_positions)").fetchall()
            if not any(column["name"] == "pending_exit" for column in columns):
                db.execute("ALTER TABLE paper_positions ADD COLUMN pending_exit TEXT")
        if legacy_path is not None:
            self._archive_legacy(Path(legacy_path))

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self, *, write=True):
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _archive_legacy(self, path):
        if not path.exists():
            return
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM legacy_archive").fetchone():
                return
            snapshot = path.read_bytes()
            open_count, history_count = 0, 0
            error = None
            try:
                legacy = json.loads(snapshot)
                if not isinstance(legacy, dict):
                    raise TypeError("Expected a JSON object")
                opens = legacy.get("opens", [legacy["open"]] if legacy.get("open") else [])
                history = legacy.get("history", [])
                if not isinstance(opens, list) or not isinstance(history, list):
                    raise TypeError("Unexpected history or open positions")
                open_count, history_count = len(opens), len(history)
            except (ValueError, TypeError, UnicodeError):
                error = "Старый журнал сохранён в архиве, но прочитать его записи не удалось."
            db.execute("INSERT INTO legacy_archive VALUES (1, ?, ?, ?, ?, ?)",
                       (str(path), snapshot, open_count, history_count, error))

    @staticmethod
    def _account(db):
        return db.execute("SELECT * FROM account WHERE id = 1").fetchone()

    @staticmethod
    def _opens(db):
        return db.execute("SELECT * FROM paper_positions WHERE closed_at IS NULL ORDER BY opened_at, position_id").fetchall()

    def configure(self, capital, daily_budget, *, now=None):
        capital = _number(capital, "Капитал")
        daily_budget = _number(daily_budget, "Дневной бюджет", allow_zero=True)
        if daily_budget > capital:
            raise LedgerError("Бюджет превышает капитал.")
        now = _timestamp(now)
        with self._transaction() as db:
            account = self._account(db)
            if account is None:
                db.execute("INSERT INTO account (id, capital, daily_budget, cash, equity_peak) VALUES (1, ?, ?, ?, ?)",
                           (capital, daily_budget, capital, capital))
            elif account["capital"] != capital:
                if db.execute("SELECT 1 FROM paper_fills LIMIT 1").fetchone():
                    raise LedgerError("После первой бумажной сделки стартовый капитал фиксирован.")
                db.execute("UPDATE account SET capital = ?, daily_budget = ?, cash = ?, equity_peak = ?",
                           (capital, daily_budget, capital, capital))
                db.execute("DELETE FROM equity_points")
            else:
                db.execute("UPDATE account SET daily_budget = ?", (daily_budget,))
            self._record_equity(db, now)
            return self._report(db, now)

    def _daily_pnl(self, db, now):
        day_start = datetime.fromtimestamp(now, self.day_timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        return db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_fills WHERE side = 'sell' AND at BETWEEN ? AND ?",
            (day_start.timestamp(), now),
        ).fetchone()[0]

    def _record_equity(self, db, now):
        account = self._account(db)
        equity = _snapshot_equity(account, self._opens(db))
        if not math.isfinite(equity):
            raise LedgerError("Рыночная оценка портфеля вне допустимого диапазона.")
        peak = max(account["equity_peak"], equity)
        dd = peak - equity
        db.execute(
            "UPDATE account SET equity_peak = ?, max_drawdown = MAX(max_drawdown, ?), "
            "max_drawdown_pct = MAX(max_drawdown_pct, ?)",
            (peak, dd, dd / peak * 100),
        )
        db.execute(
            "INSERT INTO equity_points VALUES (?, ?, ?) ON CONFLICT (minute) DO UPDATE "
            "SET at = excluded.at, equity = excluded.equity WHERE excluded.at >= equity_points.at",
            (int(now // 60), now, equity),
        )

    def open(self, candidate_id, exchange, symbol, qty, ask, stop, tp1, tp2, fee_rate, *,
             now=None, limits=RiskLimits(), bid=None):
        position = self._submit_open(candidate_id, exchange, symbol, qty, ask, stop, tp1, tp2, fee_rate,
                                     now=now, limits=limits, bid=bid, execute=True)
        if position is None:
            raise LedgerError("Журнал не вернул открытую позицию.")
        return position

    def check_open(self, candidate_id, exchange, symbol, qty, ask, stop, tp1, tp2, fee_rate, *,
                   now=None, limits=RiskLimits(), bid=None):
        """Validate current capacity without reserving cash or recording a fill."""
        self._submit_open(candidate_id, exchange, symbol, qty, ask, stop, tp1, tp2, fee_rate,
                          now=now, limits=limits, bid=bid, execute=False)

    def _submit_open(self, candidate_id, exchange, symbol, qty, ask, stop, tp1, tp2, fee_rate, *,
                     now, limits, bid, execute):
        candidate_id = _name(candidate_id, "Сигнал")
        exchange = _name(exchange, "Биржа")
        symbol = _name(symbol, "Инструмент")
        qty = _number(qty, "Количество")
        ask = _number(ask, "Цена входа")
        stop = _number(stop, "Стоп")
        tp1 = _number(tp1, "Первая цель")
        tp2 = _number(tp2, "Вторая цель")
        bid = ask if bid is None else _number(bid, "Цена продажи")
        fee_rate = _number(fee_rate, "Ставка комиссии", allow_zero=True)
        if not stop < bid <= ask < tp1 < tp2 or fee_rate >= 1:
            raise LedgerError("Цена, стоп, цели или комиссия вышли за допустимые пределы.")
        fee = qty * ask * fee_rate
        debit = _number(qty * ask + fee, "Стоимость исполнения")
        risk = _number(_stop_risk(qty, ask, stop, fee_rate), "Риск исполнения")
        now = _timestamp(now)
        with self._transaction(write=execute) as db:
            account = self._account(db)
            if account is None:
                raise LedgerError("Сначала настройте бумажный капитал и бюджет.")
            opens = self._opens(db)
            self._check_limits(db, account, opens, exchange, symbol, candidate_id, debit, risk, now, limits)
            if not execute:
                return None
            position_id = uuid4().hex
            db.execute(
                "INSERT INTO paper_positions (position_id, candidate_id, exchange, symbol, qty, initial_qty, "
                "entry, stop, tp1, tp2, fee_rate, opened_at, peak, last_bid, marked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, candidate_id, exchange, symbol, qty, qty, ask, stop, tp1, tp2, fee_rate,
                 now, bid, bid, now),
            )
            db.execute("INSERT INTO paper_fills (position_id, side, qty, price, fee, pnl, reason, at) "
                       "VALUES (?, 'buy', ?, ?, ?, 0, 'entry', ?)", (position_id, qty, ask, fee, now))
            db.execute("UPDATE account SET cash = cash - ?", (debit,))
            self._record_equity(db, now)
            position = db.execute("SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)).fetchone()
            return dict(position)

    def _check_limits(self, db, account, opens, exchange, symbol, candidate_id, debit, risk, now, limits):
        fractions = (limits.max_trade_risk_fraction, limits.max_open_risk_fraction, limits.max_daily_loss_fraction)
        if (type(limits.max_positions) is not int or limits.max_positions < 1
                or any(not 0 < _number(fraction, "Доля риска") <= 1 for fraction in fractions)):
            raise LedgerError("Некорректные лимиты риска.")
        if db.execute("SELECT 1 FROM paper_positions WHERE candidate_id = ?", (candidate_id,)).fetchone():
            raise LedgerError("Этот сигнал уже исполнен в бумажном режиме.")
        if any(p["exchange"] == exchange and p["symbol"] == symbol for p in opens):
            raise LedgerError("По этому инструменту на бирже уже есть открытая позиция.")
        if len(opens) >= limits.max_positions:
            raise LedgerError("Достигнут лимит открытых позиций.")

        reserved = sum(p["qty"] * p["entry"] * (1 + p["fee_rate"]) for p in opens)
        eps = account["capital"] * 1e-12
        if debit > account["cash"] + eps or reserved + debit > account["daily_budget"] + eps:
            raise LedgerError("Недостаточно доступных средств или превышен дневной бюджет.")
        capital_at_risk = min(_snapshot_equity(account, opens), account["daily_budget"])
        if risk > capital_at_risk * limits.max_trade_risk_fraction + eps:
            raise LedgerError("Превышен лимит риска одной позиции.")
        total_risk = _open_risk(opens) + risk
        if total_risk > capital_at_risk * limits.max_open_risk_fraction + eps:
            raise LedgerError("Превышен суммарный риск открытых позиций.")
        if max(0, -self._daily_pnl(db, now)) + total_risk > capital_at_risk * limits.max_daily_loss_fraction + eps:
            raise LedgerError("Достигнут дневной лимит потерь с учётом стопов.")

    def open_positions(self):
        with self._transaction(write=False) as db:
            return [dict(position) for position in self._opens(db)]

    @staticmethod
    def _active_position(db, position_id):
        row = db.execute("SELECT * FROM paper_positions WHERE position_id = ? AND closed_at IS NULL",
                         (position_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _sell(db, position, qty, bid, reason, now):
        if math.isclose(qty, position["qty"], rel_tol=1e-12):
            qty = position["qty"]
        fee = _number(qty * bid * position["fee_rate"], "Комиссия выхода", allow_zero=True)
        proceeds = _number(qty * bid - fee, "Сумма выхода")
        cost_basis = qty * position["entry"] * (1 + position["fee_rate"])
        pnl = proceeds - cost_basis
        remaining = position["qty"] - qty
        closed_at = now if remaining == 0 else None
        result = db.execute(
            "INSERT INTO paper_fills (position_id, side, qty, price, fee, pnl, reason, at) "
            "VALUES (?, 'sell', ?, ?, ?, ?, ?, ?)", (position["position_id"], qty, bid, fee, pnl, reason, now),
        )
        db.execute("UPDATE account SET cash = cash + ?", (proceeds,))
        db.execute("UPDATE paper_positions SET qty = ?, closed_at = ?, pending_exit = NULL WHERE position_id = ?",
                   (remaining, closed_at, position["position_id"]))
        position["qty"] = remaining
        return {"fill_id": result.lastrowid, "position_id": position["position_id"], "exchange": position["exchange"],
                "symbol": position["symbol"], "qty": qty, "price": bid, "fee": fee, "pnl": pnl,
                "reason": reason, "at": now, "remaining": remaining, "mode": "paper"}

    def mark(self, position_id, bid, *, now=None, max_age_seconds=7200, trailing_fraction=None, fillable=True):
        """Mark a fresh bid and keep triggered exits pending until a fill is available."""
        bid = _number(bid, "Цена продажи")
        now = _timestamp(now)
        max_age_seconds = _number(max_age_seconds, "Тайм-аут позиции")
        if trailing_fraction is not None:
            trailing_fraction = _number(trailing_fraction, "Доля трейлинга")
            if trailing_fraction >= 1:
                raise LedgerError("Доля трейлинга должна быть меньше единицы.")
        with self._transaction() as db:
            position = self._active_position(db, position_id)
            if position is None or now < position["marked_at"]:
                return []
            fills = []
            peak = max(position["peak"], bid)
            stop = position["stop"]
            tp1_hit = bool(position["tp1_hit"])
            pending = position["pending_exit"]
            if bid <= stop:
                pending = "stop"
            elif pending not in {"stop", "timeout", "tp2"}:
                if bid >= position["tp2"]:
                    pending = "tp2"
                elif now - position["opened_at"] >= max_age_seconds:
                    pending = "timeout"
                elif not tp1_hit and bid >= position["tp1"]:
                    pending = "tp1"

            if fillable and pending:
                if not tp1_hit and pending in {"tp1", "tp2"}:
                    fills.append(self._sell(db, position, position["qty"] / 2, bid, "tp1", now))
                    tp1_hit = True
                    rate = position["fee_rate"]
                    break_even = position["entry"] * (1 + rate) / (1 - rate)
                    stop = max(stop, min(bid, break_even))
                if pending != "tp1":
                    fills.append(self._sell(db, position, position["qty"], bid, pending, now))
                pending = None
            if position["qty"] > 0 and tp1_hit and pending is None and trailing_fraction is not None:
                stop = max(stop, peak * (1 - trailing_fraction))
            db.execute("UPDATE paper_positions SET last_bid = ?, marked_at = ?, peak = ?, stop = ?, tp1_hit = ?, "
                       "pending_exit = ? WHERE position_id = ?", (bid, now, peak, stop, tp1_hit, pending, position_id))
            self._record_equity(db, now)
            return fills

    def close(self, position_id, bid, *, now=None):
        bid = _number(bid, "Цена продажи")
        now = _timestamp(now)
        with self._transaction() as db:
            position = self._active_position(db, position_id)
            if position is None:
                raise LedgerError("Позиция уже закрыта или не найдена.")
            if now < position["marked_at"]:
                raise LedgerError("Котировка старше последней цены позиции.")
            fill = self._sell(db, position, position["qty"], bid, "manual_paper_exit", now)
            self._record_equity(db, now)
            return [fill]

    def report(self, now=None):
        with self._transaction(write=False) as db:
            return self._report(db, _timestamp(now))

    def pace_report(self, now=None, window_seconds=86400):
        """Report complete paper rounds whose final exit falls in the rolling window."""
        now = _timestamp(now)
        window_seconds = _number(window_seconds, "Окно отчёта")
        with self._transaction(write=False) as db:
            completed, wins, losses, pnl, average_trade, holding, fees = db.execute(
                "WITH rounds AS ("
                "SELECT SUM(f.pnl) AS net, SUM(f.fee) AS fees, p.closed_at - p.opened_at AS held "
                "FROM paper_positions p JOIN paper_fills f USING (position_id) "
                "WHERE p.closed_at > ? AND p.closed_at <= ? GROUP BY p.position_id) "
                "SELECT COUNT(*), COALESCE(SUM(net > 1e-9), 0), COALESCE(SUM(net < -1e-9), 0), "
                "COALESCE(SUM(net), 0), AVG(net), AVG(held), COALESCE(SUM(fees), 0) FROM rounds",
                (now - window_seconds, now),
            ).fetchone()
        return {"window_seconds": window_seconds, "completed": completed, "wins": wins, "losses": losses,
                "flat": completed - wins - losses, "realized_net_pnl": pnl, "average_trade": average_trade,
                "average_holding_seconds": holding, "round_trip_fees": fees}

    def _report(self, db, now):
        account = self._account(db)
        opens = self._opens(db)
        realized, fees = db.execute("SELECT COALESCE(SUM(pnl), 0), COALESCE(SUM(fee), 0) FROM paper_fills").fetchone()
        closed, wins = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(net > 0), 0) FROM ("
            "SELECT SUM(f.pnl) AS net FROM paper_positions p JOIN paper_fills f USING (position_id) "
            "WHERE p.closed_at IS NOT NULL GROUP BY p.position_id)"
        ).fetchone()
        archive = db.execute("SELECT open_count, history_count, error FROM legacy_archive").fetchone()
        capital = account["capital"] if account else 0.0
        equity = _snapshot_equity(account, opens) if account else 0.0
        net = equity - capital
        return {
            "capital": capital, "daily_budget": account["daily_budget"] if account else 0.0,
            "cash": account["cash"] if account else 0.0, "equity": equity,
            "open_count": len(opens), "closed_count": closed,
            "net_pnl": net, "realized_pnl": realized, "unrealized_pnl": net - realized,
            "daily_realized_pnl": self._daily_pnl(db, now), "open_risk": _open_risk(opens),
            "max_drawdown": account["max_drawdown"] if account else 0.0,
            "max_drawdown_pct": account["max_drawdown_pct"] if account else 0.0,
            "win_rate": wins / closed if closed else None, "fees": fees,
            "legacy_open_count": archive["open_count"] if archive else 0,
            "legacy_history_count": archive["history_count"] if archive else 0,
            "legacy_error": archive["error"] if archive else None,
        }

    def equity_history(self):
        with self._transaction(write=False) as db:
            return [dict(row) for row in db.execute("SELECT at, equity FROM equity_points ORDER BY minute")]

    def recent_fills(self, limit=5):
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT f.*, p.exchange, p.symbol, 'paper' AS mode FROM paper_fills f "
                "JOIN paper_positions p USING (position_id) ORDER BY f.at DESC, f.fill_id DESC LIMIT ?",
                (_history_limit(limit),),
            )
            return [dict(row) for row in rows]

    def backup(self, destination):
        """Write a consistent snapshot without replacing an existing backup."""
        destination = Path(destination)
        destination.touch(exist_ok=False)
        try:
            with self._connection() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def manual_positions(self):
        with self._transaction(write=False) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM manual_positions WHERE closed_at IS NULL ORDER BY opened_at, position_id"
            )]

    def manual_report(self, limit=5):
        with self._transaction(write=False) as db:
            open_count, closed_count = db.execute(
                "SELECT COUNT(*) - COUNT(closed_at), COUNT(closed_at) FROM manual_positions"
            ).fetchone()
            pnl, fees = db.execute(
                "SELECT COALESCE(SUM(pnl), 0), COALESCE(SUM(fee), 0) FROM manual_fills"
            ).fetchone()
            fills = db.execute(
                "SELECT f.*, p.exchange, p.symbol, 'manual' AS mode FROM manual_fills f "
                "JOIN manual_positions p USING (position_id) ORDER BY f.at DESC, f.rowid DESC LIMIT ?",
                (_history_limit(limit),),
            )
            return {"open_count": open_count, "closed_count": closed_count, "realized_pnl": pnl, "fees": fees,
                    "recent_fills": [dict(fill) for fill in fills]}

    @staticmethod
    def _manual_key(db, key):
        key = _name(key, "Идентификатор исполнения")
        if db.execute("SELECT 1 FROM manual_fills WHERE external_key = ?", (key,)).fetchone():
            raise LedgerError("Это фактическое исполнение уже записано.")
        return key

    def record_manual_open(self, key, exchange, symbol, qty, price, fee, *, now=None):
        exchange = _name(exchange, "Биржа")
        symbol = _name(symbol, "Инструмент")
        qty = _number(qty, "Количество")
        price = _number(price, "Фактическая цена")
        fee = _number(fee, "Фактическая комиссия", allow_zero=True)
        _number(qty * price + fee, "Сумма сделки")
        now = _timestamp(now)
        position_id = uuid4().hex
        with self._transaction() as db:
            key = self._manual_key(db, key)
            db.execute("INSERT INTO manual_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                       (position_id, exchange, symbol, price, fee, qty, qty, now, now))
            db.execute("INSERT INTO manual_fills VALUES (?, ?, 'buy', ?, ?, ?, 0, ?)",
                       (key, position_id, qty, price, fee, now))
            return {"position_id": position_id, "exchange": exchange, "symbol": symbol, "qty": qty,
                    "price": price, "fee": fee, "at": now, "mode": "manual"}

    def record_manual_close(self, key, position_id, qty, price, fee, *, now=None):
        qty = _number(qty, "Количество")
        price = _number(price, "Фактическая цена")
        fee = _number(fee, "Фактическая комиссия", allow_zero=True)
        now = _timestamp(now)
        with self._transaction() as db:
            key = self._manual_key(db, key)
            position = db.execute("SELECT * FROM manual_positions WHERE position_id = ? AND closed_at IS NULL",
                                  (position_id,)).fetchone()
            if position is None or now < position["last_fill_at"]:
                raise LedgerError("Позиция не найдена или исполнение старше последней записи.")
            if math.isclose(qty, position["qty"], rel_tol=1e-12):
                qty = position["qty"]
            if qty > position["qty"]:
                raise LedgerError("Объём выхода превышает остаток позиции.")
            _number(qty * price, "Оборот сделки")
            entry_fee = position["entry_fee"] * (qty / position["initial_qty"])
            pnl = qty * (price - position["entry"]) - entry_fee - fee
            if not math.isfinite(pnl):
                raise LedgerError("Недопустимый финансовый результат.")
            remaining = position["qty"] - qty
            db.execute("UPDATE manual_positions SET qty = ?, last_fill_at = ?, closed_at = ? WHERE position_id = ?",
                       (remaining, now, now if remaining == 0 else None, position_id))
            db.execute("INSERT INTO manual_fills VALUES (?, ?, 'sell', ?, ?, ?, ?, ?)",
                       (key, position_id, qty, price, fee, pnl, now))
            return {"position_id": position_id, "exchange": position["exchange"], "symbol": position["symbol"],
                    "qty": qty, "price": price, "fee": fee, "pnl": pnl, "at": now, "remaining": remaining,
                    "mode": "manual"}
