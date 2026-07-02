import os

# ==========================================================
# BINANCE API
# ==========================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# ==========================================================
# DISCORD
# ==========================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# ==========================================================
# BOT SETTINGS
# ==========================================================

BOT_NAME = "Crypto AI Trader Pro"

CHECK_INTERVAL = 30      # Seconds between scan cycles

MAX_OPEN_TRADES = 10

DEFAULT_LEVERAGE = 5

DEFAULT_MARGIN_TYPE = "ISOLATED"

# ==========================================================
# TIMEFRAMES
# ==========================================================

TIMEFRAMES = [
    "5m",
    "15m",
    "1h"
]

# ==========================================================
# TOP 40 COINS
# ==========================================================

COINS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "TRX/USDT",
    "LINK/USDT",
    "AVAX/USDT",
    "SUI/USDT",
    "DOT/USDT",
    "LTC/USDT",
    "BCH/USDT",
    "ATOM/USDT",
    "APT/USDT",
    "ARB/USDT",
    "OP/USDT",
    "NEAR/USDT",
    "INJ/USDT",
    "FIL/USDT",
    "AAVE/USDT",
    "ETC/USDT",
    "UNI/USDT",
    "ICP/USDT",
    "SEI/USDT",
    "WLD/USDT",
    "PEPE/USDT",
    "SHIB/USDT",
    "FET/USDT",
    "RENDER/USDT",
    "IMX/USDT",
    "RUNE/USDT",
    "TIA/USDT",
    "JUP/USDT",
    "PYTH/USDT",
    "ONDO/USDT",
    "ENA/USDT",
    "TAO/USDT",
    "XLM/USDT"
]

# ==========================================================
# NEWS SETTINGS
# ==========================================================

ENABLE_NEWS = True

NEWS_REFRESH_MINUTES = 15

# ==========================================================
# STRATEGY SETTINGS
# ==========================================================

ENABLE_ICT = True

ENABLE_SMC = True

ENABLE_TREND = True

ENABLE_VOLUME = True

ENABLE_MOMENTUM = True

MIN_SIGNAL_SCORE = 5

# ==========================================================
# RISK SETTINGS
# ==========================================================

ENABLE_TRAILING_STOP = True

ENABLE_AUTO_SLTP = True

MAX_DAILY_LOSS_PERCENT = 5

MAX_RISK_PER_TRADE = 1

# ==========================================================
# LOGGING
# ==========================================================

LOG_LEVEL = "INFO"

SAVE_LOGS = True
