import time
import unittest
from unittest.mock import patch

from market import Market, MarketError


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


def okx_quote(symbol, timestamp):
    return {
        "instId": symbol,
        "ts": str(int(timestamp * 1000)),
        "bidPx": "99.50",
        "askPx": "100.00",
        "bidSz": "4",
        "askSz": "2.5",
    }


def binance_candle(start_ms):
    return [start_ms, "100", "101", "99", "100.5", "10", start_ms + 59_999]


class MarketTests(unittest.IsolatedAsyncioTestCase):
    def test_unsupported_exchange_is_not_routed_to_binance(self):
        with self.assertRaisesRegex(ValueError, "Unsupported spot exchange"):
            Market(Session(), "bybit")

    async def test_okx_uses_quote_volume_and_isolates_invalid_rows(self):
        session = Session(Response({"code": "0", "data": [
            {"instId": "ETH-USDT", "volCcy24h": "8000000", "vol24h": "10"},
            {"instId": "BTC-USDT", "volCcy24h": "9000000"},
            {"instId": "BAD-USDT", "volCcy24h": "inf"},
            {"instId": "XRP-USDC", "volCcy24h": "9000000000"},
            {"instId": "Y-USDT", "volCcy24h": "1000"},
            None,
        ]}))
        self.assertEqual(await Market(session, "okx").universe(), ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(session.requests[0][1]["params"], {"instType": "SPOT"})

    async def test_okx_batch_discards_stale_crossed_and_wrong_symbols(self):
        now = time.time()
        rows = [
            okx_quote("BTC-USDT", now),
            okx_quote("ETH-USDT", now - 10),
            {**okx_quote("XRP-USDT", now), "askPx": "98"},
            okx_quote("OTHER-USDT", now),
        ]
        market = Market(Session(Response({"code": "0", "data": rows})), "okx")
        quotes = await market.quotes(["BTC/USDT", "ETH/USDT", "XRP/USDT"])
        self.assertEqual(set(quotes), {"BTC/USDT"})
        self.assertEqual((quotes["BTC/USDT"].bid_size, quotes["BTC/USDT"].ask_size), (4, 2.5))

    async def test_binance_reads_size_from_book_ticker(self):
        session = Session(Response({
            "symbol": "BTCUSDT", "bidPrice": "99", "bidQty": "2", "askPrice": "100", "askQty": "3",
        }))
        quote = await Market(session, "binance").quote("BTC/USDT")
        self.assertEqual((quote.exchange, quote.bid, quote.ask, quote.bid_size, quote.ask_size),
                         ("binance", 99, 100, 2, 3))
        self.assertTrue(quote.is_fresh())
        self.assertTrue(session.requests[0][0].endswith("/api/v3/ticker/bookTicker"))

    async def test_rate_limit_is_visible_and_backs_off(self):
        session = Session(Response({}, status=429, headers={"Retry-After": "10"}))
        market = Market(session, "binance")
        with self.assertRaisesRegex(MarketError, "HTTP 429"):
            await market.quote("BTC/USDT")
        with self.assertRaisesRegex(MarketError, "rate-limited"):
            await market.quote("BTC/USDT")
        self.assertEqual(len(session.requests), 1)

    async def test_binance_candles_are_closed_ordered_and_utc(self):
        now = 1_800_000_010
        boundary = now // 60 * 60 * 1000
        raw = [binance_candle(boundary), binance_candle(boundary - 60_000), binance_candle(boundary - 120_000)]
        with patch("market.time.time", return_value=now):
            frame = await Market(Session(Response(raw)), "binance").candles("BTC/USDT", "1m")
        self.assertEqual([int(ts.timestamp() * 1000) for ts in frame["ts"]],
                         [boundary - 120_000, boundary - 60_000])
        self.assertEqual(str(frame["ts"].dt.tz), "UTC")

    async def test_candles_reject_internal_gaps(self):
        now = 1_800_000_010
        boundary = now // 60 * 60 * 1000
        raw = [binance_candle(boundary - 180_000), binance_candle(boundary - 60_000)]
        with patch("market.time.time", return_value=now), self.assertRaisesRegex(MarketError, "gap"):
            await Market(Session(Response(raw)), "binance").candles("BTC/USDT", "1m")

    async def test_okx_requires_confirmed_candle_and_rejects_stale_history(self):
        now = 1_800_000_010
        boundary = now // 60 * 60 * 1000
        rows = [
            [str(boundary - 60_000), "100", "101", "99", "100.5", "10", "0", "0", "0"],
            [str(boundary - 180_000), "100", "101", "99", "100.5", "10", "0", "0", "1"],
        ]
        with patch("market.time.time", return_value=now), self.assertRaisesRegex(MarketError, "missing or stale"):
            await Market(Session(Response({"code": "0", "data": rows})), "okx").candles("BTC/USDT", "1m")
