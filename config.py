"""
HackerAI Auto Trading Bot - Configuration
à¶”à¶¶à¶œà·š Binance API, Discord Webhook, News API settings
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# BINANCE API CONFIGURATION
# ============================================================
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY_HERE")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET_HERE")
BINANCE_TESTNET = False  # True for testnet, False for real account

# ============================================================
# TRADING PARAMETERS
# ============================================================
BALANCE_PERCENTAGE = 5  # à¶¶à·à¶½à¶±à·Šà·ƒà·Š à¶‘à¶šà·™à¶±à·Š 5%
MAX_LEVERAGE = 5  # Maximum leverage (auto-adjusted based on coin)
RISK_PER_TRADE = 0.02  # 2% risk per trade (for position sizing)

# ============================================================
# SIGNAL REQUIREMENTS (à¶”à¶¶à·š à¶…à¶½à·”à¶­à·Š conditions)
# ============================================================
MIN_TOOLS_MATCH = 3  # Tools 5à¶±à·Š à¶…à·€à¶¸ à¶œà·à¶½à¶´à·™à¶± à¶œà¶«à¶± (5/3 rule)
MIN_PROFIT_CHANCE = 65.0  # à¶…à·€à¶¸ profit chance à¶‘à¶š 65%
SCAN_INTERVAL_SECONDS = 30  # à·ƒà·‘à¶¸ à¶­à¶­à·Š 30à¶šà¶§ à·€à¶»à¶šà·Š scan (24/7)
BALANCE_CHECK_INTERVAL = 60  # Balance check interval seconds
WAIT_FOR_BALANCE = True  # Balance à¶±à·à¶­à·’ à·€à·™à¶½à·à·€à¶§ crash à¶±à·œà·€à·“ wait à¶šà¶»à¶±à·Šà¶±

# ============================================================
# TOP 40 COINS (Binance USDT Perpetual Futures)
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
# TIMEFRAMES (Best 3 Timeframes)
# ============================================================
TIMEFRAMES = {
    "higher": "4h",    # Higher timeframe (trend direction)
    "medium": "1h",    # Medium timeframe (confirmation)
    "lower": "15m"     # Lower timeframe (entry execution)
}

# ============================================================
# ANALYSIS TOOLS (5 Tools)
# ============================================================
ANALYSIS_TOOLS = {
    "ict_smc": True,           # Tool 1: ICT/Smart Money Concepts
    "fvg": True,               # Tool 2: Fair Value Gap
    "order_block": True,       # Tool 3: Order Blocks
    "liquidity": True,         # Tool 4: Liquidity Sweeps
    "market_structure": True   # Tool 5: Market Structure (BOS/CHoCH)
}

# ============================================================
# NEWS API CONFIGURATION
# ============================================================
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "YOUR_NEWS_API_KEY_HERE")
NEWS_ENABLED = True

# ============================================================
# DISCORD / TELEGRAM NOTIFICATIONS
# ============================================================
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")
ENABLE_DISCORD = True

# ============================================================
# TRADE MANAGEMENT
# ============================================================
TAKE_PROFIT_PERCENT = 2.0      # 2% take profit
STOP_LOSS_PERCENT = 1.0        # 1% stop loss
TRAILING_STOP_ACTIVATE = 0.5   # Activate trailing at 0.5% profit
TRAILING_STOP_DISTANCE = 0.3   # Trailing stop distance 0.3%
MAX_OPEN_TRADES = 15           # Maximum concurrent trades

# ============================================================
# LOGGING
# ============================================================
LOG_LEVEL = "INFO"
LOG_FILE = "hackerai_bot.log"
