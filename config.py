# language: Python, file: config.py
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DEFAULT_SYMBOL = "BTC/USDT"
EXCHANGE = "bybit"
WATCH_INTERVAL = 300
ALERT_COOLDOWN = 900

MIN_CONFIDENCE_LIVE = 0.75
SIGNAL_COOLDOWN_SEC = 600
FEE_ROUND = 0.001
PORTFOLIO_FILE = "portfolio.json"
