# language: Python, file: config.py
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TRADE_REVIEW_ENABLED = os.environ.get("TRADE_REVIEW_ENABLED", "0") == "1"
TRADE_REVIEW_URL = os.environ.get("TRADE_REVIEW_URL", "")
TRADE_REVIEW_KEY = os.environ.get("TRADE_REVIEW_KEY", "")
TRADE_REVIEW_MODEL = os.environ.get("TRADE_REVIEW_MODEL", "")
DEFAULT_SYMBOL = "BTC/USDT"
EXCHANGE = "bybit"
FEE_ROUND = 0.001
PORTFOLIO_FILE = "portfolio.json"

# Настройки профилей (тейк, стоп, уверенность)
PROFILES = {
    "high": {
        "label": "большая прибыль",
        "min_confidence": 0.70,
        "tp_mult": 7.0,
        "cooldown": 900,
    },
    "low": {
        "label": "малая прибыль",
        "min_confidence": 0.60,
        "tp_mult": 4.0,
        "cooldown": 180,
    },
    "rapid": {
        "label": "микро-профит",
        "min_confidence": 0.55,
        "tp_mult": 1.0,
        "cooldown": 60,
    },
}

# Умный лот: сколько % от депо рисковать в зависимости от уверенности
RISK_PERCENT = {
    0.85: 0.03, # если уверенность 85%+, рискуем 3% от депо
    0.75: 0.02, # если 75%+, рискуем 2%
    0.00: 0.01, # в остальных случаях 1%
}
# Личный Telegram-бот и долговременные файлы.
BOT_OWNER_ID = os.environ.get("BOT_OWNER_ID", "")
PAPER_DB = os.environ.get("PAPER_DB", "data/paper.sqlite3")
SIGNAL_EXCHANGES = tuple(
    venue.strip().lower()
    for venue in os.environ.get("SIGNAL_EXCHANGES", "binance").split(",")
    if venue.strip()
)
# Единственная ставка за сторону используется карточкой и журналом.
PAPER_FEE_RATE = float(os.environ.get("PAPER_FEE_RATE", "0.002"))
# Экспериментальные стратегии всегда наблюдаются, уведомления включаются отдельно.
EXPERIMENTAL_SIGNALS = os.environ.get("EXPERIMENTAL_SIGNALS", "0") == "1"
