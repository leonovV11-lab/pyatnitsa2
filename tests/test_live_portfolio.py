import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from live_portfolio import PortfolioSnapshot, read_portfolio

NOW = 1_800_000_000


def snapshot(**fields):
    return {
        "observed_at": NOW - 3,
        "available_cash_usdt": 123.45,
        "positions": [
            {"venue": "okx", "symbol": "BTC/USDT", "stressed_risk_usdt": 1.5},
            {"venue": "binance", "symbol": "BTC/USDT", "stressed_risk_usdt": 2},
            {"venue": "okx", "symbol": "ETH/USDT", "stressed_risk_usdt": 0},
        ],
        **fields,
    }


class LivePortfolioTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portfolio.json"

    def read(self, data, *, as_of: float = NOW):
        self.path.write_text(json.dumps(data), encoding="utf-8-sig")
        return read_portfolio(self.path, as_of=as_of)

    def test_aggregates_all_position_risk_and_tracks_zero_risk_symbols(self):
        result = self.read(snapshot())
        self.assertEqual(result, PortfolioSnapshot(
            observed_at=NOW - 3,
            available_cash_usdt=123.45,
            stressed_risk_usdt=3.5,
            open_symbols=frozenset({"BTC/USDT", "ETH/USDT"}),
        ))

    def test_allows_zero_cash_and_no_open_positions(self):
        result = self.read(snapshot(available_cash_usdt=0, positions=[]))
        self.assertEqual(result.available_cash_usdt, 0)
        self.assertEqual(result.stressed_risk_usdt, 0)
        self.assertEqual(result.open_symbols, frozenset())

    def test_reads_a_string_path(self):
        self.read(snapshot())
        self.assertEqual(read_portfolio(str(self.path), as_of=NOW).observed_at, NOW - 3)

    def test_rejects_stale_snapshot_after_300_seconds_and_any_future_time(self):
        self.assertEqual(self.read(snapshot(observed_at=NOW - 300)).observed_at, NOW - 300)
        self.assertEqual(self.read(snapshot(observed_at=NOW)).observed_at, NOW)
        with self.assertRaisesRegex(ValueError, "устарел"):
            self.read(snapshot(observed_at=NOW - 300.001))
        with self.assertRaisesRegex(ValueError, "будущим"):
            self.read(snapshot(observed_at=NOW + 0.001))

    def test_rejects_duplicate_venue_symbol_pair(self):
        data = snapshot()
        data["positions"].append(dict(data["positions"][0]))
        with self.assertRaisesRegex(ValueError, "повторяются"):
            self.read(data)

    def test_validates_case_and_structure_of_open_position_symbols(self):
        for venue, symbol in (("OKX", "BTC/USDT"), ("okx", "btc/usdt"), ("okx", "BTC"), ("", "BTC/USDT")):
            with self.subTest(venue=venue, symbol=symbol), self.assertRaises(ValueError):
                self.read(snapshot(positions=[{"venue": venue, "symbol": symbol, "stressed_risk_usdt": 0}]))
        self.assertEqual(self.read(snapshot(positions=[
            {"venue": "okx", "symbol": "ETH/BTC", "stressed_risk_usdt": 1},
        ])).open_symbols, frozenset({"ETH/BTC"}))

    def test_rejects_non_numeric_negative_and_nonfinite_cash_or_position_risk(self):
        for invalid in ("12", -0.01, True, None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.read(snapshot(available_cash_usdt=invalid))
                with self.assertRaises(ValueError):
                    self.read(snapshot(positions=[{"venue": "okx", "symbol": "BTC/USDT", "stressed_risk_usdt": invalid}]))

    def test_rejects_invalid_time_and_overflowed_total_risk(self):
        for invalid in (0, -1, False, "1", None, float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.read(snapshot(observed_at=invalid))
        with self.assertRaisesRegex(ValueError, "оценки"):
            self.read(snapshot(), as_of=float("inf"))
        with self.assertRaisesRegex(ValueError, "Общий риск"):
            self.read(snapshot(positions=[
                {"venue": "okx", "symbol": "BTC/USDT", "stressed_risk_usdt": 1e308},
                {"venue": "binance", "symbol": "BTC/USDT", "stressed_risk_usdt": 1e308},
            ]))

    def test_rejects_missing_or_malformed_files_without_echoing_contents_or_path(self):
        with self.assertRaisesRegex(ValueError, "прочитать снимок") as missing:
            read_portfolio(self.path, as_of=NOW)
        self.assertNotIn(str(self.path), str(missing.exception))
        for content in ("", "[]", "{}", "private test invalid !",
                        '{"observed_at":1800000000,"available_cash_usdt":123.45,"positions":[],"positions":null}',
                        '{"observed_at":1800000000,"available_cash_usdt":123.45,"positions":[null]}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError) as error:
                    read_portfolio(self.path, as_of=NOW)
                self.assertNotIn("private test invalid", str(error.exception))
