"""
HackerAI Auto Trading Bot - Configuration
Railway.app compatible - Reads from environment variables
"""

import os

# ============================================================
# BINANCE API - Railway variables වලින් read කරන්න
# ============================================================
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BINANCE_TESTNET = os.environ.get("BINANCE_TESTNET", "False").lower() == "true"

# ============================================================
# TRADING PARAMETERS
# ============================================================
BALANCE_PERCENTAGE = int(os.environ.get("BALANCE_PERCENTAGE", "5"))
MAX_LEVERAGE = int(os.environ.get("MAX_LEVERAGE", "5"))
RISK_PER_TRADE = float(os.environ.get("RISK_PER_TRADE", "0.02"))

# ============================================================
# SIGNAL REQUIREMENTS
# ============================================================
MIN_TOOLS_MATCH = int(os.environ.get("MIN_TOOLS_MATCH", "3"))
MIN_PROFIT_CHANCE = float(os.environ.get("MIN_PROFIT_CHANCE", "65.0"))
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "30"))
BALANCE_CHECK_INTERVAL = int(os.environ.get("BALANCE_CHECK_INTERVAL", "60"))

# ============================================================
# TOP 40 COINS
# ============================================================
TOP_40_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "SOLUSDT", "DOGEUSDT", "DOTUSDT", "MATICUSDT", "SHIBUSDT",
    "AVAXUSDT", "LINKUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "ETCUSDT", "XLMUSDT", "BCHUSDT", "ALGOUSDT", "VETUSDT",
    "FILUSDT", "TRXUSDT", "NEARUSDT", "SANDUSDT", "MANAUSDT",
    "APEUSDT", "AXSUSDT", "THETAUSDT", "FTMUSDT", "EGLDUSDT",
    "HBARUSDT", "ICPUSDT", "XMRUSDT", "EOSUSDT", "AAVEUSDT",
    "CAKEUSDT", "KLAYUSDT", "ARUSDT", "CRVUSDT", "GRTUSDT"
]

# ============================================================
# TIMEFRAMES
# ============================================================
TIMEFRAMES = {
    "higher": "4h",
    "medium": "1h",
    "lower": "15m"
}

# ============================================================
# ANALYSIS TOOLS
# ============================================================
ANALYSIS_TOOLS = {
    "ict_smc": True,
    "fvg": True,
    "order_block": True,
    "liquidity": True,
    "market_structure": True
}

# ============================================================
# NEWS API (optional)
# ============================================================
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
NEWS_ENABLED = os.environ.get("NEWS_ENABLED", "True").lower() == "true"

# ============================================================
# DISCORD (optional)
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ENABLE_DISCORD = os.environ.get("ENABLE_DISCORD", "False").lower() == "true"

# ============================================================
# TRADE MANAGEMENT
# ============================================================
TAKE_PROFIT_PERCENT = float(os.environ.get("TAKE_PROFIT_PERCENT", "2.0"))
STOP_LOSS_PERCENT = float(os.environ.get("STOP_LOSS_PERCENT", "1.0"))
TRAILING_STOP_ACTIVATE = float(os.environ.get("TRAILING_STOP_ACTIVATE", "0.5"))
TRAILING_STOP_DISTANCE = float(os.environ.get("TRAILING_STOP_DISTANCE", "0.3"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES", "15"))

# ============================================================
# LOGGING
# ============================================================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE = os.environ.get("LOG_FILE", "hackerai_bot.log")
