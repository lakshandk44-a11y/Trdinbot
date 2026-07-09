#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Main Entry Point
PM2/EC2 compatible - 24/7 auto trading
"""

import logging
import sys
import os
import time
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Try to load .env if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import *
from bot_core import HackerAIBot

def setup_logging():
    """Setup logging"""
    log_format = "%(asctime)s | %(levelname)-8s | %(message)s"
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    return logging.getLogger(__name__)

def validate_config():
    """Validate required config"""
    errors = []
    
    if not BINANCE_API_KEY:
        errors.append("❌ BINANCE_API_KEY not set!")
    if not BINANCE_API_SECRET:
        errors.append("❌ BINANCE_API_SECRET not set!")
    if BALANCE_PERCENTAGE <= 0 or BALANCE_PERCENTAGE > 100:
        errors.append("❌ BALANCE_PERCENTAGE must be 1-100")
    if MAX_LEVERAGE <= 0 or MAX_LEVERAGE > 125:
        errors.append("❌ MAX_LEVERAGE must be 1-125")
    if MAX_OPEN_TRADES <= 0:
        errors.append("❌ MAX_OPEN_TRADES must be > 0")
    
    return errors

def print_banner():
    """Print bot banner"""
    banner = """
╔══════════════════════════════════════════════════════════╗
║              🤖 HACKERAI BOT v2.0 🤖                     ║
║           Auto Trading Bot - Binance Futures             ║
║                                                          ║
║  🕐 24/7 Operation  |  🖥️  EC2/PM2 Deploy               ║
║  📊 Top 40 Coins    |  3 Timeframes                      ║
║  🔧 5/3 Tools Rule | 65%+ Profit Filter                 ║
║  💰 Crash Protection | Unlimited Trades                  ║
╚══════════════════════════════════════════════════════════╝
    """
    print(banner)

def run_calibration_if_needed(logger):
    """
    FIX (auto-calibration on startup): previously calibration_table.json
    had to be produced by manually running `python3 backtest_calibration.py`
    on the server. This runs that same script automatically, but only the
    FIRST time the bot starts on this server — i.e. only if
    calibration_table.json doesn't exist yet.

    It's intentionally NOT re-run on every start/restart: the backtest
    pulls up to 9 months of historical candles for all 40 coins across 3
    timeframes and replays the analysis engine over each one, which can
    take a long time (potentially tens of minutes). Since PM2 restarts
    this process automatically on any crash, re-running the full backtest
    every single restart would repeatedly delay trading and hammer the
    Binance API for no benefit — once a table exists, it's reused as-is,
    exactly like manually running the script once would behave.

    This calls backtest_calibration.py as a separate process (not an
    import) so its own argparse/logging setup never interacts with
    main.py's — nothing about the live bot's own logic changes.
    """
    calibration_file = "calibration_table.json"
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "backtest_calibration.py")

    if os.path.exists(calibration_file):
        logger.info(f"📐 Calibration table found ({calibration_file}) — using it as-is, skipping backtest.")
        return

    if not os.path.exists(script_path):
        logger.warning("⚠️ backtest_calibration.py not found — skipping auto-calibration, "
                        "bot will use the raw (uncalibrated) profit-chance score.")
        return

    logger.info("📐 No calibration table found. Running the one-time profit-chance "
                "calibration backtest before trading starts — this fetches months of "
                "history for all coins and can take a while. Please wait...")
    try:
        result = subprocess.run([sys.executable, script_path], check=False)
        if result.returncode == 0 and os.path.exists(calibration_file):
            logger.info("✅ Calibration backtest complete — calibration_table.json is ready.")
        else:
            logger.warning("⚠️ Calibration backtest finished without producing a table "
                            "(see output above for details). Continuing with the raw "
                            "profit-chance score; you can re-run "
                            "'python3 backtest_calibration.py' manually later.")
    except Exception as e:
        logger.warning(f"⚠️ Could not run the calibration backtest automatically ({e}). "
                        f"Continuing without it — you can still run "
                        f"'python3 backtest_calibration.py' manually later.")


def main():
    """Main entry point - No input required for PM2"""
    print_banner()
    logger = setup_logging()
    
    logger.info("=" * 50)
    logger.info(f"🚀 HackerAI Bot v2.0 Starting at {datetime.now().isoformat()}")
    logger.info(f"📈 Balance Allocation: {BALANCE_PERCENTAGE}%")
    logger.info(f"⚙️ Max Leverage: {MAX_LEVERAGE}x")
    logger.info(f"📊 Timeframes: {TIMEFRAMES}")
    logger.info(f"🔧 Min Tools Match: {MIN_TOOLS_MATCH}/5")
    logger.info(f"🎯 Min Profit Chance: {MIN_PROFIT_CHANCE}%")
    logger.info(f"🔄 Max Open Trades: {MAX_OPEN_TRADES}")
    logger.info(f"⏱️ Scan Interval: {SCAN_INTERVAL_SECONDS}s")
    logger.info(f"🏦 Live Mode: {not BINANCE_TESTNET}")
    logger.info("=" * 50)
    
    errors = validate_config()
    if errors:
        for error in errors:
            logger.error(error)
        logger.error("❌ Configuration errors found. Please fix and restart.")
        sys.exit(1)

    run_calibration_if_needed(logger)

    bot_config = {
        "BINANCE_API_KEY": BINANCE_API_KEY,
        "BINANCE_API_SECRET": BINANCE_API_SECRET,
        "BINANCE_TESTNET": BINANCE_TESTNET,
        "BALANCE_PERCENTAGE": BALANCE_PERCENTAGE,
        "MAX_LEVERAGE": MAX_LEVERAGE,
        "RISK_PER_TRADE": RISK_PER_TRADE,
        "MIN_TOOLS_MATCH": MIN_TOOLS_MATCH,
        "MIN_PROFIT_CHANCE": MIN_PROFIT_CHANCE,
        "SCAN_INTERVAL_SECONDS": SCAN_INTERVAL_SECONDS,
        "BALANCE_CHECK_INTERVAL": BALANCE_CHECK_INTERVAL,
        "TOP_40_COINS": TOP_40_COINS,
        "TIMEFRAMES": TIMEFRAMES,
        "ANALYSIS_TOOLS": ANALYSIS_TOOLS,
        "NEWS_API_KEY": NEWS_API_KEY,
        "TAKE_PROFIT_PERCENT": TAKE_PROFIT_PERCENT,
        "STOP_LOSS_PERCENT": STOP_LOSS_PERCENT,
        "TRAILING_STOP_ACTIVATE": TRAILING_STOP_ACTIVATE,
        "TRAILING_STOP_DISTANCE": TRAILING_STOP_DISTANCE,
        "MAX_OPEN_TRADES": MAX_OPEN_TRADES,
        "TRADE_STATE_FILE": TRADE_STATE_FILE,
        "TRADING_FEE_PERCENT": TRADING_FEE_PERCENT
    }
    
    # PM2 mode - auto restart on crash
    while True:
        try:
            bot = HackerAIBot(bot_config)
            bot.start()
        except KeyboardInterrupt:
            logger.info("⏹️ Bot stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"💥 Bot crashed: {e}")
            import traceback
            traceback.print_exc()
            logger.info("🔄 Restarting in 30 seconds...")
            time.sleep(30)
            continue

if __name__ == "__main__":
    main()
