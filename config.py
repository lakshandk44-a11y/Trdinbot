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
BINANCE_TESTNET = True  # True for testnet, False for real account

# ============================================================
# TRADING PARAMETERS
# ============================================================
BALANCE_PERCENTAGE = 5  # à¶¶à·à¶½à¶±à·Šà·ƒà·Š à¶‘à¶šà·™à¶±à·Š 5%
MAX_LEVERAGE = 10  # Maximum leverage (auto-adjusted based on coin)
RISK_PER_TRADE = 0.02  # 2% risk per trade (for position sizing)

# ============================================================
# SIGNAL REQUIREMENTS (à¶”à¶¶à·š à¶…à¶½à·”à¶­à·Š conditions)
# ============================================================
MIN_TOOLS_MATCH = 4  # Tools 5à¶±à·Š à¶…à·€à¶¸ à¶œà·à¶½à¶´à·™à¶± à¶œà¶«à¶± (5/3 rule)
MIN_PROFIT_CHANCE = 40.0  # FIX: calibration_table.json (27,042 real backtested
# setups) shows NO score bucket ever reaches 65% real win-rate — the
# highest bucket (90-100 raw score) only wins 51.7% of the time. Since
# analysis_engine._get_calibrated_profit_chance() replaces the raw score
# with this real win-rate once the table is loaded, a 65% threshold would
# silently reject every single trade forever. Breakeven here (TP 2% / SL 1%
# / 0.05% fee per side) is ~36.7%; 45.0 keeps a real safety margin above
# breakeven while only admitting buckets with genuine historical edge
# (70-80: 40.0%, 80-90: 45.7%, 90-100: 51.7%). Re-tune this after each
# fresh calibration run — it should track whatever the real buckets show,
# not an assumed number.
SCAN_INTERVAL_SECONDS = 30  # à·ƒà·‘à¶¸ à¶­à¶­à·Š 30à¶šà¶§ à·€à¶»à¶šà·Š scan (24/7)

# ============================================================
# TRADING HOURS FILTER (2026-07-11 hourly_breakdown.json, STRIDE=1,
# full 24h coverage, 64,359 setups, ~2,500-2,900 samples per hour)
# ============================================================
# Breakeven with TP=5%/SL=3%/0.1% round-trip fee is ~38.75%. Only
# 12:00-16:59 UTC cleared it (12:00=40.13%, 13:00=39.64%, 14:00=40.44%,
# 15:00=38.76%, 16:00=38.76%) - all 5 hours with large, comparable sample
# sizes, consistent with the London-afternoon/NY-morning liquidity
# overlap. Every other hour of the day was below breakeven (31-37%).
# When enabled, NEW trades only open during these hours - trades already
# open outside this window keep being managed normally (SL/TP/trailing
# untouched; this only gates new entries). Re-verify against a fresh
# hourly_breakdown.json periodically, since this reflects one backtest
# window, not a permanent law of the market.
TRADING_HOURS_FILTER_ENABLED = False  # TEMPORARILY disabled for diagnostic testing -
# zero trades opened for an extended period with this on. 5/24 allowed
# hours combined with the 45% calibrated MIN_PROFIT_CHANCE may simply be
# too restrictive together. Re-enable (set back to True) once confirmed
# trades open normally without this filter - if they do, the hours filter
# was the cause (just very strict, not a bug); if trades still don't open,
# the cause is elsewhere and this rules the hours filter out.
ALLOWED_TRADING_HOURS_UTC = [12, 13, 14, 15, 16]
BALANCE_CHECK_INTERVAL = 60  # Balance check interval seconds
WAIT_FOR_BALANCE = True  # Balance à¶±à·à¶­à·’ à·€à·™à¶½à·à·€à¶§ crash à¶±à·œà·€à·“ wait à¶šà¶»à¶±à·Šà¶±

# ============================================================
# TOP 40 COINS (Binance USDT Perpetual Futures)
# ============================================================
TOP_40_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "SOLUSDT", "DOGEUSDT", "DOTUSDT", "MATICUSDT", "1000SHIBUSDT",  # FIX: SHIBUSDT doesn't exist on Binance Futures - it's listed as 1000SHIBUSDT
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
TAKE_PROFIT_PERCENT = 5.0      # 5% take profit (also the clamp-range base for analysis-based TP)
STOP_LOSS_PERCENT = 3.0        # 3% stop loss (also the clamp-range base for analysis-based SL)
TRAILING_STOP_ACTIVATE = 0.5   # Activate trailing at 0.5% profit
TRAILING_STOP_DISTANCE = 0.3   # Fallback only: used if a trade's entry ATR wasn't captured
ATR_TRAILING_MULTIPLIER = 2.0  # Trailing distance = entry ATR(14) x this - adapts to each coin's own volatility instead of one fixed % for all coins
MAX_OPEN_TRADES = 15           # Maximum concurrent trades

# FIX (TP1 -> TP2 continuation): the moment a trade's first take-profit
# (TP1) is hit, instead of closing immediately, the bot re-analyzes that
# symbol right then with the exact same tools/timeframes used for entries.
# If the fresh analysis still confirms continuation in the trade's
# direction (same rule used for entries/reversals: MIN_TOOLS_MATCH tools
# agreeing), the stop loss is moved up to the TP1 price (locking in that
# profit) and a further TP2 target is set instead of closing. If the
# fresh analysis does NOT confirm continuation, or fresh market data can't
# be fetched at that moment, the trade closes at TP1 exactly as before —
# nothing changes for that case. This applies automatically to every open
# trade the bot manages; set to False to fully restore the old behavior
# (close immediately on any TP hit).
TP1_REANALYSIS_ENABLED = True

# These three previously had NO entry in config.py at all - they only
# worked because the code's internal .get(key, default) fallback happened
# to match a sane value. Making them explicit here means they're visible
# and tunable like every other setting, not silently dependent on a
# default buried in bot_core.py/trade_manager.py/analysis_engine.py.
CALIBRATION_TABLE_FILE = "calibration_table.json"
CALIBRATION_MIN_SAMPLES = 20  # a score bucket needs at least this many backtested samples to be trusted
REVERSAL_COOLDOWN_SECONDS = 240  # grace period after entry before reversal-based early-close can trigger

# FIX (Real win-rate): Binance USDT-M futures taker fee, charged on BOTH
# the entry fill and the exit fill. Used to subtract real trading cost
# from a closed trade's PnL so the win-rate stat reflects actual net
# results instead of just the raw ideal entry/exit price move.
TRADING_FEE_PERCENT = 0.05     # % per side (Binance default taker fee)

# ============================================================
# STATE PERSISTENCE (FIX: survive bot/VPS restarts without losing
# track of open positions and their SL/TP levels)
# ============================================================
TRADE_STATE_FILE = os.getenv("TRADE_STATE_FILE", "trade_state.json")

# ============================================================
# LOGGING
# ============================================================
LOG_LEVEL = "INFO"
LOG_FILE = "hackerai_bot.log"
