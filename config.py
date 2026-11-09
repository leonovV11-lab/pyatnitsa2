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
        "min_confidence": 0.70,  # Снизили с 85% до 70%
        "tp_mult": 7.0,
        "cooldown": 900,
    },
    "low": {
        "label": "малая прибыль",
        "min_confidence": 0.60,  # Снизили с 65% до 60%
        "tp_mult": 4.0,
        "cooldown": 180,
    },
    "rapid": {
        "label": "микро-профит",
        "min_confidence": 0.55,  # Снизили с 75% до 55%
        "tp_mult": 1.0,
        "cooldown": 60,
    },
}