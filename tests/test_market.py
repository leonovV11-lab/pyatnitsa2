import asyncio
import json
import time
import unittest
from unittest.mock import patch

from market import Market, MarketError


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.body

    async def iter_chunked(self, size):
        payload = json.dumps(self.body).encode()
        for offset in range(0, len(payload), size):
            yield payload[offset:offset + size]


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


def okx_instrument(base, quote, *, state="live", instrument_type="SPOT"):
    return {"baseCcy": base, "quoteCcy": quote, "instId": f"{base}-{quote}",
            "state": state, "instType": instrument_type}


def binance_instrument(base, quote, *, status="TRADING", allowed=True):
    return {"baseAsset": base, "quoteAsset": quote, "symbol": f"{base}{quote}",
            "status": status, "isSpotTradingAllowed": allowed}


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

    async def test_okx_all_spot_lists_active_instruments_and_reads_mixed_quote_batch(self):
        now = time.time()
        session = Session(
            Response({"code": "0", "data": [
                okx_instrument("ETH", "BTC"), okx_instrument("BTC", "USDT"),
                okx_instrument("XRP", "EUR", state="suspend"), okx_instrument("BTC", "USD", instrument_type="SWAP"),
                {**okx_instrument("SOL", "USDT"), "instId": "SOL-USDT-SWAP"},
                okx_instrument("BAD/NAME", "USDT"),
            ]}),
            Response({"code": "0", "data": [okx_quote("ETH-BTC", now), okx_quote("BTC-USDT", now)]}),
        )
        market = Market(session, "okx", all_spot=True)
        catalogues = await asyncio.gather(market.spot_symbols(), market.spot_symbols())
        self.assertEqual(catalogues, [("BTC/USDT", "ETH/BTC")] * 2)
        quotes = await market.quotes(("BTC/USDT", "ETH/BTC"))
        self.assertEqual({quote.symbol for quote in quotes.values()}, {"BTC/USDT", "ETH/BTC"})
        with self.assertRaises(MarketError):
            await market.quote("XRP/EUR")
        self.assertEqual([url.rsplit("/", 1)[1] for url, _ in session.requests], ["instruments", "tickers"])
        self.assertEqual(session.requests[0][1]["params"], {"instType": "SPOT"})

    async def test_binance_all_spot_uses_listed_base_quote_mapping(self):
        unicode_pair = "币安人生/USDC"
        now = 1_800_000_010
        boundary = now // 60 * 60 * 1000
        session = Session(
            Response({"symbols": [
                binance_instrument("BTC", "EUR"), binance_instrument("币安人生", "USDC"),
                binance_instrument("ETH", "USDC", status="HALT"), binance_instrument("ETH", "BTC", allowed=False),
                {**binance_instrument("ALT", "USDT"), "symbol": "ALTUSDC"},
            ]}),
            Response({"symbol": "币安人生USDC", "bidPrice": "99", "bidQty": "2", "askPrice": "100", "askQty": "3"}),
            Response([binance_candle(boundary - 60_000)]),
        )
        market = Market(session, "binance", all_spot=True)
        self.assertEqual(await market.spot_symbols(), ("BTC/EUR", unicode_pair))
        with patch("market.time.time", return_value=now):
            self.assertEqual((await market.quote(unicode_pair)).symbol, unicode_pair)
            self.assertEqual(len(await market.candles("BTC/EUR", "1m")), 1)
        self.assertEqual(session.requests[0][1]["params"]["permissions"], "SPOT")
        self.assertEqual(session.requests[1][1]["params"]["symbol"], "币安人生USDC")
        self.assertEqual(session.requests[2][1]["params"]["symbol"], "BTCEUR")

    async def test_catalog_rejects_ambiguous_binance_alias_and_oversized_response(self):
        ambiguous = Response({"symbols": [binance_instrument("AB", "C"), binance_instrument("A", "BC")]})
        with self.assertRaises(MarketError):
            await Market(Session(ambiguous), "binance", all_spot=True).spot_symbols()
        oversize = Response({"code": "0", "data": [okx_instrument("ETH", "BTC")]})
        with patch("market._MAX_CATALOG_BYTES", 64), self.assertRaises(MarketError):
            await Market(Session(oversize), "okx", all_spot=True).spot_symbols()

    async def test_expired_catalog_does_not_keep_removed_pairs_or_serve_a_failed_refresh(self):
        session = Session(
            Response({"code": "0", "data": [okx_instrument("ETH", "BTC")]}),
            Response({"code": "0", "data": [okx_instrument("BTC", "EUR")]}),
            Response({}, status=503),
        )
        market = Market(session, "okx", all_spot=True)
        self.assertEqual(await market.spot_symbols(), ("ETH/BTC",))
        market._catalog_expires_at = 0
        with self.assertRaises(MarketError):
            await market.quote("ETH/BTC")
        self.assertEqual(await market.spot_symbols(), ("BTC/EUR",))
        market._catalog_expires_at = 0
        with self.assertRaises(MarketError):
            await market.spot_symbols()
        self.assertEqual(len(session.requests), 3)

    async def test_existing_market_mode_still_rejects_non_usdt_quotes_without_network(self):
        session = Session()
        with self.assertRaisesRegex(ValueError, "BTC/USDT"):
            await Market(session, "okx").quote("ETH/BTC")
        self.assertEqual(session.requests, [])

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
