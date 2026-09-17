# language: Python, file: exchanges.py
# *список популярных бирж и топовых пар для кнопок*

EXCHANGES = {
    "bybit":    "Bybit",
    "binance":  "Binance",
    "okx":      "OKX",
    "kucoin":   "KuCoin",
    "gateio":   "Gate.io",
    "mexc":     "MEXC",
}

DEFAULT_QUOTES = ["USDT", "USDC", "BTC"]

POPULAR_BASES = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
    "TON", "TRX", "DOT", "MATIC", "LINK", "LTC", "ATOM", "NEAR",
]


def build_pairs(quote: str = "USDT", bases: list = None) -> list:
    if bases is None:
        bases = POPULAR_BASES
    return [f"{b}/{quote}" for b in bases]
