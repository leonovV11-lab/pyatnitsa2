# language: Python, file: config.py
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DEFAULT_SYMBOL = "BTC/USDT"
EXCHANGE = "bybit"
WATCH_INTERVAL = 300
ALERT_COOLDOWN = 900
FEE_ROUND = 0.001
PORTFOLIO_FILE = "portfolio.json"

PROFILES = {
    "high": {
        "label": "большая прибыль",
        "min_confidence": 0.75,
        "tp_mult": 4.0,
        "cooldown": 900,
    },
    "low": {
        "label": "малая прибыль",
        "min_confidence": 0.55,
        "tp_mult": 1.5,
        "cooldown": 180,
    },
}
