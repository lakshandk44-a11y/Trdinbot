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
MIN_TOOLS_MATCH = 3  # Tools 5à¶±à·Š à¶…à·€à¶¸ à¶œà·à¶½à¶´à·™à¶± à¶œà¶«à¶± (5/3 rule)
MIN_SUBCONCEPTS_PER_TOOL = 1  # FIX (user request, reverted from 2): each of
# the 5 tools has many of its own named sub-concepts internally (Tool 1
# alone has 9+: BOS, CHoCH, MSS, SMT Divergence, Macro Break, Unicorn Model,
# Inverse Fairy Tale, Old High/Low reaction, Wyckoff breakout). A tool only
# counts as "agreeing" toward MIN_TOOLS_MATCH if at least this many of its
# own sub-concepts agree on the same direction at the same time.
# NOTE: this was set to 2 for a while, but combined with MIN_TOOLS_MATCH=4
# AND requiring ALL 3 timeframes (4h/1h/15m) to independently clear that bar,
# a full 9-month/50-coin calibration backtest came back with ZERO qualifying
# setups on ANY coin - the combination was too strict for real market data.
# Reverted to 1 (a tool agrees as soon as ANY one of its own sub-concepts
# fires) so MIN_TOOLS_MATCH=4 and the 3-timeframe rule are the only strict
# filters left - same as before MIN_SUBCONCEPTS_PER_TOOL was introduced.

STRONG_SUBCONCEPTS_PER_TOOL = 2  # FIX (user request): a SECOND, independent
# entry path, alongside (not instead of) the normal MIN_TOOLS_MATCH-on-all-
# 3-timeframes rule above. If ANY ONE of the 3 timeframes (4h/1h/15m) on its
# OWN has at least STRONG_TOOLS_MATCH tools where each of THOSE tools has at
# least this many (STRONG_SUBCONCEPTS_PER_TOOL) of its own sub-concepts
# agreeing - a trade opens right there, without needing the other 2
# timeframes to also confirm. Meant to catch a single very-strong timeframe
# setup that the stricter "all 3 timeframes" rule would otherwise block.
ENABLE_SINGLE_TF_STRONG_ENTRY = False  # FIX (user request): turns Path A
# (a single very-strong timeframe alone can open a trade, without the other
# 2 timeframes confirming - see analysis_engine._weighted_mtf_decision)
# OFF. With this False, ONLY the original rule applies: all 3 timeframes
# (4h/1h/15m) must EACH independently have >= MIN_TOOLS_MATCH tools
# agreeing, in the same direction - exactly the behavior before Path A was
# added. Set back to True to re-enable Path A as an additional, alternate
# way to open a trade alongside the all-3-timeframes rule.
STRONG_TOOLS_MATCH = 4  # how many tools (out of 5) must each independently
# clear STRONG_SUBCONCEPTS_PER_TOOL, on a single timeframe, for this
# alternate path to fire.

# ============================================================
# PATTERN RECOGNITION ENGINE (user request, Phase 1 - 6 patterns)
# ============================================================
PATTERN_ENGINE_ENABLED = False  # SAFETY DEFAULT: off. With this False (or
# pattern_engine.py deleted entirely), the bot's existing scan/decision/
# execute path is 100% unaffected - this is a completely separate, opt-in
# "second opinion" checked ONLY for a candidate that the normal Tool 5 /
# MIN_TOOLS_MATCH / MIN_PROFIT_CHANCE gate has ALREADY REJECTED (see
# bot_core._scan_coins_247). It never runs before or instead of that gate,
# and never blocks/changes a trade the normal gate already approves.
PATTERN_MIN_CONFIDENCE = 90.0  # RAISED (user request, per explicit
# instruction) from 80.0 to 90.0, together with a new breakout-volume-spike
# check added to all 6 detectors in pattern_engine.py (previously every
# check was price-structure-only; a breakout with no volume pickup vs the
# pattern's own formation is a classical false-breakout risk that wasn't
# being scored at all before). a rejected candidate only gets opened via a
# pattern match if the best-matching classical chart pattern (Double Top/
# Bottom, Head & Shoulders/Inverse, Bull/Bear Flag) scores at least this
# confidence (0-100, see pattern_engine.py for exactly how each pattern's
# score is built). NOTE: tested against pure random-walk data, ~24% of
# random noise still scored >=80% on at least one of the 6 patterns at the
# old 80 threshold/old scoring - this is a known limitation of pure
# geometric pattern-matching (real human chart-pattern trading has the same
# "seeing patterns in noise" risk); 90 + the volume check should filter
# meaningfully more of that out, but re-verify against fresh data.
PATTERN_COOLDOWN_MINUTES = 240  # FIX (user request, re-entry loop): after
# a pattern-engine trade closes (win OR loss) on a symbol, no new pattern-
# engine trade can open on that SAME symbol for this many minutes. Without
# this, the exact same pattern (same swing points, barely-changed price
# structure) could re-trigger a new trade on the very next scan (30s later)
# right after a losing close, repeatedly re-losing on the same setup. Does
# NOT affect normal Tool-5 trades on the same symbol at all.

# ============================================================
# TELEGRAM REMOTE CONTROL (user request)
# ============================================================
TELEGRAM_ADMIN_CHAT_ID = "8804792847"  # ONLY this chat's commands/button-taps
# are accepted by telegram_control.py - anyone else messaging the bot is
# silently ignored. Change this if you ever need to control the bot from a
# different Telegram account/chat.
SETTINGS_OVERRIDE_FILE = "settings_override.json"  # where Telegram-toggled
# settings (and pause state) are saved, so they survive a bot/VPS restart.
MIN_PROFIT_CHANCE = 35.0  # FIX: calibration_table.json (27,042 real backtested
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
TOP_N_COIN_COUNT = 50  # FIX (user request, was 40): how many coins the live
# top-by-volume fetch (bot_core._get_top_coins) pulls from Binance each scan.
# TOP_N_COINS below is only the STATIC FALLBACK list used if that live fetch
# fails entirely - it's kept the same length (50) so the fallback behaves
# identically to the normal (live) path either way.
TOP_N_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "SOLUSDT", "DOGEUSDT", "DOTUSDT", "MATICUSDT", "1000SHIBUSDT",  # FIX: SHIBUSDT doesn't exist on Binance Futures - it's listed as 1000SHIBUSDT
    "AVAXUSDT", "LINKUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "ETCUSDT", "XLMUSDT", "BCHUSDT", "ALGOUSDT", "VETUSDT",
    "FILUSDT", "TRXUSDT", "NEARUSDT", "SANDUSDT", "MANAUSDT",
    "APEUSDT", "AXSUSDT", "THETAUSDT", "FTMUSDT", "EGLDUSDT",
    "HBARUSDT", "ICPUSDT", "XMRUSDT", "EOSUSDT", "AAVEUSDT",
    "CAKEUSDT", "KLAYUSDT", "ARUSDT", "CRVUSDT", "GRTUSDT",
    # FIX (user request): 10 more added to go from 40 -> 50. Same rule as the
    # rest of this list - all are commonly-listed, liquid Binance USDT-M
    # futures pairs - but since this is only a fallback (the live top-by-
    # volume fetch is what actually runs every scan), double-check each
    # symbol is still listed on your account's Binance Futures before
    # relying on this fallback path.
    "OPUSDT", "ARBUSDT", "SUIUSDT", "APTUSDT", "INJUSDT",
    "RUNEUSDT", "DYDXUSDT", "LDOUSDT", "STXUSDT", "GALAUSDT",
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
# TOOL 1 (ICT/SMC) - EXTENDED CONCEPTS
# SMT Divergence, Kill Zones, Macro Structure, Unicorn Model,
# Inverse Fairy Tale, Old Highs/Lows, Accumulation/Distribution.
# All purely additive to Tool 1 - if a fetch below ever fails, the
# relevant sub-feature just doesn't trigger that scan; nothing else
# in the bot is affected (see bot_core._fetch_multi_timeframe).
# ============================================================
SMT_DIVERGENCE_ENABLED = True   # fetch a correlated symbol's candles for SMT Divergence

# ADDED (user request): fetches this coin's current funding rate once per
# scan and folds it into the profit_chance score as a crowded-positioning
# check (see AnalysisEngine._calculate_profit_chance) - purely additive,
# same pattern as SMT_DIVERGENCE_ENABLED above. Set False to disable.
FUNDING_RATE_ENABLED = True

# ADDED (user request): daily realized-loss limit. Purely local, no extra
# API calls - see TradeManager.record_realized_loss/is_daily_loss_limit_
# reached in trade_manager.py. Resets automatically at the next calendar
# day, survives bot restarts (persisted in trade_state.json), and only
# ever counts realized losses from trades that actually CLOSED - never an
# open/still-running trade's unrealized PnL. Toggle from Telegram /menu.
DAILY_LOSS_LIMIT_ENABLED = True
DAILY_LOSS_LIMIT_USDT = 20.0   # change this $ amount to whatever fits your account size

SMT_CORRELATED_MAP = {}         # optional per-symbol override, e.g. {"SOLUSDT": "ETHUSDT"} - falls back to BTCUSDT (or ETHUSDT when scanning BTCUSDT itself) when a symbol isn't listed
DAILY_HISTORY_CANDLES = 200     # ~6.5 months of daily candles fetched per symbol for Macro Structure (PDH/PDL/PWH/PWL) and Old Highs/Lows
OLD_HIGH_LOW_MIN_DAYS = 30      # an "old" swing high/low must be at least this many days back
OLD_HIGH_LOW_MAX_DAYS = 180     # ...and at most this many days back (~6 months)

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
MAX_OPEN_TRADES = 5           # Maximum concurrent trades

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

# ADDED (user request): Telegram-toggleable Isolated/Cross margin mode.
# True = ISOLATED, False = CROSSED. Read fresh from this same config dict
# right before every trade opens (bot_core._execute_trade), so flipping
# the "Isolated Margin" button in Telegram takes effect on the very next
# trade - no restart needed. Default False (Cross) matches whatever the
# account's existing/previous margin mode already was, so a bot that
# never touches this toggle behaves exactly as before.
USE_ISOLATED_MARGIN = False

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
