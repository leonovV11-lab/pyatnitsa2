"""Read the operator's current open positions and available quote balance."""

# Invalid JSON field types are malformed snapshot values in this API.
# ruff: noqa: TRY004

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_PORTFOLIO_AGE_SECONDS = 300
_VENUE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_ASSET = re.compile(r"[^\W_]{1,32}")


@dataclass(frozen=True)
class PortfolioSnapshot:
    """An operator-supplied portfolio observation in UTC epoch seconds and USDT."""

    observed_at: float
    available_cash_usdt: float
    stressed_risk_usdt: float
    open_symbols: frozenset[str]
    quote_balances: dict[str, float] = field(default_factory=dict)

    def available_balance(self, asset: str) -> float:
        return self.available_cash_usdt if asset == "USDT" else self.quote_balances.get(asset, 0.0)


def _distinct_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def _number(value: object, *, message: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(message) from None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(message)
    return number


def _load(path: Path | str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, TypeError, ValueError):
        raise ValueError("Не удалось прочитать снимок портфеля в UTF-8.") from None
    try:
        data = json.loads(text, object_pairs_hook=_distinct_keys, parse_constant=_reject_constant)
    except (RecursionError, ValueError):
        raise ValueError("Снимок портфеля должен содержать корректный JSON без повторяющихся полей.") from None
    if not isinstance(data, dict):
        raise ValueError("Снимок портфеля должен быть объектом JSON.")
    return data


def read_portfolio(path: Path | str, *, as_of: float) -> PortfolioSnapshot:
    """Read a complete, current portfolio snapshot without making exchange requests.

    Each position's stressed_risk_usdt must account for trading costs. Positions from
    different venues all contribute to risk. Every open symbol is included even if
    it has a zero risk estimate. Same-venue positions must be aggregated in the input.
    """
    now = _number(as_of, message="Некорректное время оценки портфеля.", positive=True)
    data = _load(path)
    observed_at = _number(
        data.get("observed_at"), message="Укажите положительное время снимка observed_at.", positive=True,
    )
    age = now - observed_at
    if age < 0:
        raise ValueError("Снимок портфеля датирован будущим.")
    if age > MAX_PORTFOLIO_AGE_SECONDS:
        raise ValueError(f"Снимок портфеля устарел. Максимальный возраст {MAX_PORTFOLIO_AGE_SECONDS} секунд.")

    cash = _number(data.get("available_cash_usdt"), message="Укажите неотрицательный остаток available_cash_usdt.")
    raw_balances = data.get("quote_balances", {})
    if not isinstance(raw_balances, dict) or any(
        asset == "USDT" or not _ASSET.fullmatch(asset) or asset != asset.upper() for asset in raw_balances
    ):
        raise ValueError("Укажите quote_balances как остатки по отдельным валютам котировки без USDT.")
    quote_balances = {asset: _number(value, message="Остаток валюты котировки должен быть неотрицательным.")
                      for asset, value in raw_balances.items()}
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("Укажите открытые позиции списком positions.")

    open_markets = set()
    risk_amounts = []
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("Каждая открытая позиция должна быть объектом JSON.")
        venue, symbol = position.get("venue"), position.get("symbol")
        if not isinstance(venue, str) or not _VENUE.fullmatch(venue):
            raise ValueError("Укажите биржу в поле venue строчными латинскими буквами.")
        if (not isinstance(symbol, str) or symbol != symbol.upper() or symbol.count("/") != 1
                or not all(_ASSET.fullmatch(asset) for asset in symbol.split("/"))):
            raise ValueError("Укажите инструмент в поле symbol в формате BTC/USDT.")
        if (venue, symbol) in open_markets:
            raise ValueError("В снимке повторяются позиции по одной бирже и инструменту.")
        open_markets.add((venue, symbol))
        risk_amounts.append(_number(
            position.get("stressed_risk_usdt"), message="Укажите неотрицательный риск позиции stressed_risk_usdt.",
        ))

    try:
        risk = math.fsum(risk_amounts)
    except OverflowError:
        raise ValueError("Общий риск портфеля превышает допустимое числовое значение.") from None
    return PortfolioSnapshot(observed_at, cash, risk, frozenset(symbol for _, symbol in open_markets), quote_balances)
