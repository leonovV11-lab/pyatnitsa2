# language: Python, file: exchanges.py
# *список бирж + функция загрузки всех пар с биржи*

import ccxt

EXCHANGES = {
    "bybit":    "Bybit",
    "binance":  "Binance",
    "okx":      "OKX",
    "kucoin":   "KuCoin",
    "gate":     "Gate.io",
    "mexc":     "MEXC",
    "htx":      "HTX (Huobi)",
    "bitget":   "Bitget",
    "kraken":   "Kraken",
    "coinbase": "Coinbase",
    "bitfinex": "Bitfinex",
    "poloniex": "Poloniex",
    "coinex":   "CoinEx",
    "lbank":    "LBank",
    "bingx":    "BingX",
}

DEFAULT_QUOTES = ["USDT", "USDC", "BTC", "ETH"]


def load_all_pairs(exchange_key: str, quote: str = "USDT", max_pairs: int = 500) -> list:
    """тянет с биржи все активные спотовые пары с заданной котировкой."""
    try:
        ex = getattr(ccxt, exchange_key)({"enableRateLimit": True})
        ex.load_markets()
        pairs = []
        for symbol, market in ex.markets.items():
            if not market.get("active"):
                continue
            if market.get("spot") is False:
                continue
            if market.get("quote") != quote:
                continue
            pairs.append(symbol)
        pairs.sort()
        return pairs[:max_pairs]
    except Exception as e:
        raise RuntimeError(f"не удалось загрузить пары с {exchange_key}: {e}")
