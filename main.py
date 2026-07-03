#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Main Entry Point
Railway.app deployable - 24/7 auto trading
"""

import logging
import sys
import os
import time
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
            logging.StreamHandler(sys.stdout)  # Railway logs වලට stdout
        ]
    )
    
    return logging.getLogger(__name__)

def validate_config():
    """Validate required config"""
    errors = []
    
    if not BINANCE_API_KEY:
        errors.append("❌ BINANCE_API_KEY not set! Add to Railway Environment Variables")
    if not BINANCE_API_SECRET:
        errors.append("❌ BINANCE_API_SECRET not set! Add to Railway Environment Variables")
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
║  🕐 24/7 Operation  |  🚄 Railway Deploy                 ║
║  📊 Top 40 Coins    |  3 Timeframes                      ║
║  🔧 5/3 Tools Rule | 65%+ Profit Filter                 ║
║  💰 Crash Protection | Unlimited Trades                  ║
╚══════════════════════════════════════════════════════════╝
    """
    print(banner)

def main():
    """Main entry point"""
    print_banner()
    
    logger = setup_logging()
    logger.info("=" * 50)
    logger.info(f"🚀 HackerAI Bot v2.0 Starting on Railway at {datetime.now().isoformat()}")
    logger.info(f"📈 Balance: {BALANCE_PERCENTAGE}% | Leverage: {MAX_LEVERAGE}x")
    logger.info(f"🔧 Tools: {MIN_TOOLS_MATCH}/5 | Profit: {MIN_PROFIT_CHANCE}%+")
    logger.info(f"🔄 Max Trades: {MAX_OPEN_TRADES} | Scan: {SCAN_INTERVAL_SECONDS}s")
    logger.info(f"🏦 Live Mode: {not BINANCE_TESTNET}")
    logger.info("=" * 50)
    
    errors = validate_config()
    if errors:
        for error in errors:
            logger.error(error)
        logger.error("\n❌ Set missing variables in Railway Dashboard → Variables tab")
        logger.error("   Required: BINANCE_API_KEY, BINANCE_API_SECRET")
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
    
    # Railway එකේ 24/7 run කරන්න - restart loop
    while True:
        try:
            bot = HackerAIBot(bot_config)
            bot.start()
        except KeyboardInterrupt:
            logger.info("⏹️ Bot stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"💥 Bot crashed: {e}")
            logger.info("🔄 Restarting in 30 seconds...")
            import traceback
            traceback.print_exc()
            time.sleep(30)
            continue

if __name__ == "__main__":
    main()
