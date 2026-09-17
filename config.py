# language: Python, file: config.py
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DEFAULT_SYMBOL = "BTC/USDT"
EXCHANGE = "bybit"
WATCH_INTERVAL = 300
ALERT_COOLDOWN = 900

# live-режим
MIN_CONFIDENCE_LIVE = 0.50
SIGNAL_COOLDOWN_SEC = 300
