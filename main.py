#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot v2.0 - Main Entry Point
24/7 Auto Trading | Balance Crash Protection | 65%+ Profit Filter
ICT/SMC + 5 Tools | 3 Timeframes | Unlimited Trades
"""

import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    return logging.getLogger(__name__)

def validate_config():
    """Validate config"""
    errors = []
    
    if BINANCE_API_KEY == "YOUR_BINANCE_API_KEY_HERE":
        errors.append("❌ BINANCE_API_KEY not set!")
    if BINANCE_API_SECRET == "YOUR_BINANCE_API_SECRET_HERE":
        errors.append("❌ BINANCE_API_SECRET not set!")
    if BALANCE_PERCENTAGE <= 0 or BALANCE_PERCENTAGE > 100:
        errors.append("❌ BALANCE_PERCENTAGE must be 1-100")
    if MAX_LEVERAGE <= 0 or MAX_LEVERAGE > 125:
        errors.append("❌ MAX_LEVERAGE must be 1-125")
    if MAX_OPEN_TRADES <= 0:
        errors.append("❌ MAX_OPEN_TRADES must be > 0")
    if MIN_PROFIT_CHANCE < 0 or MIN_PROFIT_CHANCE > 100:
        errors.append("❌ MIN_PROFIT_CHANCE must be 0-100")
    
    return errors

def print_banner():
    """Print bot banner"""
    banner = """
╔══════════════════════════════════════════════════════════╗
║              🤖 HACKERAI BOT v2.0 🤖                     ║
║           Auto Trading Bot - Binance Futures             ║
║                                                          ║
║  🕐 24/7 Operation                                      ║
║  📊 Top 40 Coins | 3 Timeframes                         ║
║  🔧 5/3 Tools Rule | 65%+ Profit Filter                 ║
║  💰 Balance Crash Protection                            ║
║  🔄 Unlimited Concurrent Trades                         ║
╚══════════════════════════════════════════════════════════╝
    """
    print(banner)

def main():
    """Main entry point"""
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
    logger.info("=" * 50)
    
    errors = validate_config()
    if errors:
        for error in errors:
            logger.error(error)
        logger.error("❌ Fix config errors and restart.")
        sys.exit(1)
    
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
        "MAX_OPEN_TRADES": MAX_OPEN_TRADES
    }
    
    print(f"\n⚠️  REAL TRADING BOT - Balance: {BALANCE_PERCENTAGE}% | Lev: {MAX_LEVERAGE}x")
    print(f"   Tools filter: {MIN_TOOLS_MATCH}/5 | Profit: {MIN_PROFIT_CHANCE}%+")
    print(f"   24/7 mode | Crash protection: ON")
    
    if not BINANCE_TESTNET:
        confirm = input("\n❓ Start with REAL account? (yes/NO): ").strip().lower()
        if confirm != "yes":
            logger.info("❌ Cancelled by user")
            sys.exit(0)
    
    try:
        bot = HackerAIBot(bot_config)
        bot.start()
    except KeyboardInterrupt:
        logger.info("⏹️ Bot stopped by user")
    except Exception as e:
        logger.error(f"💥 Fatal: {e}")
        import traceback
        traceback.print_exc()
    finally:
        logger.info("👋 HackerAI Bot shutting down.")

if __name__ == "__main__":
    main()
