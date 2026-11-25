"""Public spot market data from Binance and OKX."""

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
_OKX_CATALOG_PATH = "/api/v5/public/instruments"
_BINANCE_CATALOG_PATH = "/api/v3/exchangeInfo"
_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_MAX_CATALOG_ROWS = 10_000
_CATALOG_TTL = 300.0
_CATALOG_RETRY_SECONDS = 5.0
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


def _valid_asset(value):
    # Binance lists some spot assets with Unicode names. Punctuation and control characters are not asset names.
    return isinstance(value, str) and 0 < len(value) <= 32 and value.isalnum() and value.upper() == value


class Market:
    def __init__(self, session, exchange, *, all_spot=False):
        if exchange not in SUPPORTED_EXCHANGES:
            raise ValueError(f"Unsupported spot exchange: {exchange}")
        self.session = session
        self.exchange = exchange
        self._all_spot = all_spot
        self._requests = asyncio.Semaphore(6)
        self._pace = asyncio.Lock()
        self._next_request = 0.0
        self._retry_at = 0.0
        self._catalog_lock = asyncio.Lock()
        self._catalog: dict[str, str] = {}
        self._catalog_expires_at = 0.0
        self._catalog_retry_at = 0.0

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
                    if path in {_OKX_CATALOG_PATH, _BINANCE_CATALOG_PATH}:
                        payload = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if len(payload) + len(chunk) > _MAX_CATALOG_BYTES:
                                raise MarketError(f"{self.exchange} spot catalog exceeds its size limit")
                            payload.extend(chunk)
                        body = json.loads(payload)
                    else:
                        body = await response.json()
            except (TimeoutError, aiohttp.ClientError, ValueError, RecursionError) as exc:
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

    async def _read_catalog(self):
        if self.exchange == "okx":
            body, _ = await self._get(_OKX_CATALOG_PATH, instType="SPOT")
            rows = self._rows(body)
            base_key, quote_key, symbol_key, separator = "baseCcy", "quoteCcy", "instId", "-"
        else:
            body, _ = await self._get(_BINANCE_CATALOG_PATH, permissions="SPOT", symbolStatus="TRADING",
                                      showPermissionSets="false")
            rows = self._rows(body.get("symbols") if isinstance(body, dict) else None)
            base_key, quote_key, symbol_key, separator = "baseAsset", "quoteAsset", "symbol", ""
        if len(rows) > _MAX_CATALOG_ROWS:
            raise MarketError(f"{self.exchange} spot catalog exceeds its instrument limit")
        catalog = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if self.exchange == "okx":
                active = row.get("instType") == "SPOT" and row.get("state") == "live"
            else:
                active = row.get("status") == "TRADING" and row.get("isSpotTradingAllowed") is True
            if not active:
                continue
            base, quote, instrument = row.get(base_key), row.get(quote_key), row.get(symbol_key)
            if (_valid_asset(base) and _valid_asset(quote) and base != quote
                    and instrument == f"{base}{separator}{quote}"):
                catalog[f"{base}/{quote}"] = instrument
        if not catalog or len(set(catalog.values())) != len(catalog):
            raise MarketError(f"{self.exchange} spot catalog is empty or ambiguous")
        return catalog

    async def _spot_catalog(self):
        async with self._catalog_lock:
            now = time.monotonic()
            if now < self._catalog_expires_at:
                return self._catalog
            if now < self._catalog_retry_at:
                raise MarketError(f"{self.exchange} spot catalog is temporarily unavailable")
            try:
                self._catalog = await self._read_catalog()
            except MarketError:
                self._catalog_retry_at = time.monotonic() + _CATALOG_RETRY_SECONDS
                raise
            self._catalog_expires_at = time.monotonic() + _CATALOG_TTL
            return self._catalog

    async def spot_symbols(self) -> tuple[str, ...]:
        """Return the sorted active public spot catalog, including non-USDT quotes."""
        return tuple(sorted(await self._spot_catalog()))

    async def _instruments(self, symbols):
        if not self._all_spot:
            return {symbol: self._instrument(symbol) for symbol in symbols}
        requested = tuple(symbols)
        if not requested:
            return {}
        catalog = await self._spot_catalog()
        instruments = {}
        for symbol in requested:
            if not isinstance(symbol, str) or symbol not in catalog:
                raise MarketError(f"{self.exchange} {symbol!r}: not in the active public spot catalog")
            instruments[symbol] = catalog[symbol]
        return instruments

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

    def _read_quote(self, row, symbol, started):
        if self.exchange == "okx":
            market_time = _number(row.get("ts")) / 1000
            if market_time > time.time() + 1:
                raise ValueError("Future exchange timestamp")
            observed_at = min(started, market_time)
            fields = ("bidPx", "askPx", "bidSz", "askSz")
        else:
            # Binance bookTicker omits source time. Age starts before the HTTP request.
            observed_at = started
            fields = ("bidPrice", "askPrice", "bidQty", "askQty")
        bid, ask, bid_size, ask_size = (_number(row.get(field)) for field in fields)
        if not 0 < bid <= ask or min(bid_size, ask_size) <= 0:
            raise ValueError("Invalid order book quote")
        quote = Quote(symbol, self.exchange, bid, ask, bid_size, ask_size, observed_at)
        if not quote.is_fresh():
            raise ValueError("Stale order book quote")
        return quote

    async def quote(self, symbol):
        """Requested symbol only, with available quantity at the best bid and ask."""
        instruments = await self._instruments((symbol,))
        instrument = instruments[symbol]
        if self.exchange == "okx":
            body, started = await self._get("/api/v5/market/ticker", instId=instrument)
            rows = self._rows(body)
        else:
            body, started = await self._get("/api/v3/ticker/bookTicker", symbol=instrument, symbolStatus="TRADING")
            rows = [body]
        quotes = self._parse_quotes(rows, instruments, started)
        if symbol not in quotes:
            raise MarketError(f"{self.exchange} {symbol}: no fresh order book quote")
        return quotes[symbol]

    async def quotes(self, symbols):
        """Get a batch of fresh quotes and omit instruments without a usable book."""
        instruments = await self._instruments(symbols)
        if not instruments:
            return {}
        if self.exchange == "okx":
            body, started = await self._get("/api/v5/market/tickers", instType="SPOT")
        else:
            params = {"symbolStatus": "TRADING"}
            if len(instruments) <= 100:
                params["symbols"] = json.dumps(list(instruments.values()), separators=(",", ":"), ensure_ascii=False)
            body, started = await self._get("/api/v3/ticker/bookTicker", **params)
        return self._parse_quotes(self._rows(body), instruments, started)

    def _parse_quotes(self, rows, instruments, started):
        quotes = {}
        by_instrument = {instrument: symbol for symbol, instrument in instruments.items()}
        instrument_key = "instId" if self.exchange == "okx" else "symbol"
        for row in rows:
            if not isinstance(row, dict):
                continue
            instrument = row.get(instrument_key)
            if not isinstance(instrument, str) or instrument not in by_instrument:
                continue
            symbol = by_instrument[instrument]
            try:
                quotes[symbol] = self._read_quote(row, symbol, started)
            except (OverflowError, TypeError, ValueError):
                _LOG.debug("Ignored unusable %s quote for %s", self.exchange, symbol)
        return quotes

    async def candles(self, symbol, tf, limit=100):
        """Return a continuous, up-to-date UTC sequence of closed candles only."""
        if tf not in _INTERVALS or not 1 <= limit <= 298:
            raise ValueError("Unsupported timeframe or candle count")
        instrument = (await self._instruments((symbol,)))[symbol]
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
