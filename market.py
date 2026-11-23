"""Only the public Binance and OKX USDT spot market data used by the bot."""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from itertools import pairwise

import aiohttp
import pandas as pd

SUPPORTED_EXCHANGES = {"binance": "Binance", "okx": "OKX"}
DEFAULT_QUOTE_MAX_AGE = 5.0
_ENDPOINTS = {"binance": "https://data-api.binance.vision", "okx": "https://www.okx.com"}
_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
_SYMBOL = re.compile(r"[A-Z0-9]+/USDT")
_LOG = logging.getLogger(__name__)


class MarketError(RuntimeError):
    """Market data is missing, invalid, or temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    exchange: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    observed_at: float

    def is_fresh(self, max_age=DEFAULT_QUOTE_MAX_AGE):
        return 0 <= time.time() - self.observed_at <= max_age


def _number(value):
    if isinstance(value, bool):
        raise TypeError("Boolean market value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite market value")
    return number


class Market:
    def __init__(self, session, exchange):
        if exchange not in SUPPORTED_EXCHANGES:
            raise ValueError(f"Unsupported spot exchange: {exchange}")
        self.session = session
        self.exchange = exchange
        self._requests = asyncio.Semaphore(6)
        self._pace = asyncio.Lock()
        self._next_request = 0.0
        self._retry_at = 0.0

    async def _get(self, path, **params):
        async with self._requests:
            async with self._pace:
                if time.monotonic() < self._retry_at:
                    raise MarketError(f"{self.exchange} market data is rate-limited")
                await asyncio.sleep(max(0, self._next_request - time.monotonic()))
                interval = 0.11 if self.exchange == "okx" else 0.04
                self._next_request = time.monotonic() + interval
            started = time.time()
            try:
                async with self.session.get(
                    _ENDPOINTS[self.exchange] + path,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=4),
                    allow_redirects=False,
                ) as response:
                    if response.status in (418, 429):
                        try:
                            delay = max(2.0, _number(response.headers.get("Retry-After", "5")))
                        except (OverflowError, TypeError, ValueError):
                            delay = 5.0
                        self._retry_at = max(self._retry_at, time.monotonic() + delay)
                    if response.status != 200:
                        raise MarketError(f"{self.exchange} {path}: HTTP {response.status}")
                    body = await response.json()
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                raise MarketError(f"{self.exchange} {path}: {type(exc).__name__}") from exc

        if self.exchange == "okx":
            if isinstance(body, dict) and str(body.get("code")) == "50011":
                self._retry_at = max(self._retry_at, time.monotonic() + 5.0)
            if not isinstance(body, dict) or str(body.get("code")) != "0":
                raise MarketError(f"{self.exchange} {path}: exchange rejected request")
            body = body.get("data")
        if isinstance(body, dict) and "code" in body:
            raise MarketError(f"{self.exchange} {path}: exchange rejected request")
        return body, started

    def _instrument(self, symbol):
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("Expected a spot market symbol like BTC/USDT")
        return symbol.replace("/", "-" if self.exchange == "okx" else "")

    def _symbol(self, value):
        if not isinstance(value, str):
            return None
        if self.exchange == "okx":
            symbol = value.replace("-", "/")
        else:
            symbol = f"{value[:-4]}/USDT" if value.endswith("USDT") else ""
        return symbol if _SYMBOL.fullmatch(symbol) else None

    @staticmethod
    def _rows(body):
        if not isinstance(body, list):
            raise MarketError("Unexpected market data response shape")
        return body

    async def universe(self, limit=50, min_quote_volume=500_000):
        """Return USDT spot candidates ordered by 24-hour quote-currency volume."""
        if self.exchange == "okx":
            body, _ = await self._get("/api/v5/market/tickers", instType="SPOT")
            instrument_key, volume_key = "instId", "volCcy24h"
        else:
            body, _ = await self._get("/api/v3/ticker/24hr", type="MINI", symbolStatus="TRADING")
            instrument_key, volume_key = "symbol", "quoteVolume"
        volumes = {}
        for row in self._rows(body):
            if not isinstance(row, dict):
                continue
            symbol = self._symbol(row.get(instrument_key))
            if symbol is None:
                continue
            try:
                volume = _number(row.get(volume_key))
            except (OverflowError, TypeError, ValueError):
                continue
            if volume >= min_quote_volume:
                volumes[symbol] = volume
        return sorted(volumes, key=lambda symbol: volumes[symbol], reverse=True)[:limit]

    def _read_quote(self, row, started):
        if self.exchange == "okx":
            symbol = self._symbol(row.get("instId"))
            market_time = _number(row.get("ts")) / 1000
            if market_time > time.time() + 1:
                raise ValueError("Future exchange timestamp")
            observed_at = min(started, market_time)
            fields = ("bidPx", "askPx", "bidSz", "askSz")
        else:
            symbol = self._symbol(row.get("symbol"))
            # Binance bookTicker omits source time. Age starts before the HTTP request.
            observed_at = started
            fields = ("bidPrice", "askPrice", "bidQty", "askQty")
        bid, ask, bid_size, ask_size = (_number(row.get(field)) for field in fields)
        if not symbol or not 0 < bid <= ask or min(bid_size, ask_size) <= 0:
            raise ValueError("Invalid order book quote")
        quote = Quote(symbol, self.exchange, bid, ask, bid_size, ask_size, observed_at)
        if not quote.is_fresh():
            raise ValueError("Stale order book quote")
        return quote

    async def quote(self, symbol):
        """Requested symbol only, with available quantity at the best bid and ask."""
        instrument = self._instrument(symbol)
        if self.exchange == "okx":
            body, started = await self._get("/api/v5/market/ticker", instId=instrument)
            rows = self._rows(body)
        else:
            body, started = await self._get("/api/v3/ticker/bookTicker", symbol=instrument, symbolStatus="TRADING")
            rows = [body]
        quotes = self._parse_quotes(rows, {symbol}, started)
        if symbol not in quotes:
            raise MarketError(f"{self.exchange} {symbol}: no fresh order book quote")
        return quotes[symbol]

    async def quotes(self, symbols):
        """Get a batch of fresh quotes and omit instruments without a usable book."""
        instruments = {symbol: self._instrument(symbol) for symbol in symbols}
        if not instruments:
            return {}
        if self.exchange == "okx":
            body, started = await self._get("/api/v5/market/tickers", instType="SPOT")
        else:
            params = {"symbolStatus": "TRADING"}
            if len(instruments) <= 100:
                params["symbols"] = json.dumps(list(instruments.values()), separators=(",", ":"))
            body, started = await self._get("/api/v3/ticker/bookTicker", **params)
        return self._parse_quotes(self._rows(body), instruments, started)

    def _parse_quotes(self, rows, symbols, started):
        quotes = {}
        instrument_key = "instId" if self.exchange == "okx" else "symbol"
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = self._symbol(row.get(instrument_key))
            if symbol not in symbols:
                continue
            try:
                quotes[symbol] = self._read_quote(row, started)
            except (OverflowError, TypeError, ValueError):
                _LOG.debug("Ignored unusable %s quote for %s", self.exchange, symbol)
        return quotes

    async def candles(self, symbol, tf, limit=100):
        """Return a continuous, up-to-date UTC sequence of closed candles only."""
        instrument = self._instrument(symbol)
        if tf not in _INTERVALS or not 1 <= limit <= 298:
            raise ValueError("Unsupported timeframe or candle count")
        if self.exchange == "okx":
            bar = tf.upper() if tf.endswith("h") else tf
            body, started = await self._get(
                "/api/v5/market/candles", instId=instrument, bar=bar, limit=limit + 2,
            )
        else:
            body, started = await self._get(
                "/api/v3/klines", symbol=instrument, interval=tf, limit=limit + 2,
            )

        interval_ms = _INTERVALS[tf] * 1000
        # Anchor to request start so a partial candle at a just-crossed boundary cannot slip in.
        cutoff = int(started * 1000)
        records = {}
        for raw in self._rows(body):
            try:
                if not isinstance(raw, list):
                    continue
                timestamp = int(raw[0])
                if timestamp <= 0 or timestamp % interval_ms or timestamp + interval_ms > cutoff:
                    continue
                if self.exchange == "okx" and str(raw[8]) != "1":
                    continue
                if self.exchange == "binance" and int(raw[6]) != timestamp + interval_ms - 1:
                    continue
                opening, high, low, closing, volume = map(_number, raw[1:6])
                if not 0 < low <= min(opening, closing) <= max(opening, closing) <= high or volume < 0:
                    continue
                records[timestamp] = (timestamp, opening, high, low, closing, volume)
            except (IndexError, TypeError, ValueError, OverflowError):
                continue

        ordered = sorted(records)[-limit:]
        if not ordered or cutoff - (ordered[-1] + interval_ms) >= interval_ms + 5000:
            raise MarketError(f"{self.exchange} {symbol} {tf}: candles missing or stale")
        if any(right - left != interval_ms for left, right in pairwise(ordered)):
            raise MarketError(f"{self.exchange} {symbol} {tf}: gap in closed candles")
        frame = pd.DataFrame(
            (records[timestamp] for timestamp in ordered),
            columns=pd.Index(["ts", "open", "high", "low", "close", "volume"]),
        )
        frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
        return frame
