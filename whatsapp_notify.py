"""
WhatsApp notification helper for the trading bot.

Sends a message to the local whatsapp_bridge.js service (POST
http://localhost:3001/send). This is intentionally fire-and-forget and
fully fail-safe: if the bridge isn't running, is still connecting, or the
network hiccups, this NEVER raises - it just logs a warning and the bot
carries on exactly as if this module didn't exist. Trading logic must
never depend on or be blocked by a notification succeeding.
"""

import logging
import requests

logger = logging.getLogger("whatsapp_notify")

WHATSAPP_BRIDGE_URL = "http://localhost:3001/send"
TIMEOUT_SECONDS = 3  # short timeout - never let a slow notify stall the bot


def send_whatsapp(message: str):
    """Best-effort WhatsApp notification. Always safe to call - never raises."""
    try:
        resp = requests.post(WHATSAPP_BRIDGE_URL, json={"message": message}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning(f"WhatsApp notify failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"WhatsApp notify unavailable: {e}")


def format_trade_opened(trade: dict) -> str:
    return (
        f"🟢 *TRADE OPENED*\n"
        f"Coin: {trade['symbol']}\n"
        f"Side: {trade['side']}\n"
        f"Entry: {trade['entry_price']:.8f}\n"
        f"Take Profit: {trade['take_profit']:.8f}\n"
        f"Stop Loss: {trade['stop_loss']:.8f}\n"
        f"Qty: {trade['quantity']}  |  Leverage: {trade['leverage']}x"
    )


def format_trade_closed(trade: dict) -> str:
    result = "PROFIT" if trade["pnl_percent"] > 0 else "LOSS"
    icon = "🟢" if trade["pnl_percent"] > 0 else "🔴"
    return (
        f"{icon} *TRADE CLOSED — {result}*\n"
        f"Coin: {trade['symbol']}\n"
        f"Side: {trade['side']}\n"
        f"PnL: {trade['pnl_percent']:+.2f}%\n"
        f"Reason: {trade.get('close_reason', 'N/A')}\n"
        f"Entry: {trade['entry_price']:.8f}\n"
        f"Exit: {trade.get('close_price', 0):.8f}"
    )


def format_trailing_activated(symbol: str, side: str, pnl_pct: float) -> str:
    return (
        f"🔺 *TRAILING STOP ACTIVATED*\n"
        f"Coin: {symbol}\n"
        f"Side: {side}\n"
        f"Profit now: +{pnl_pct:.2f}%\n"
        f"Stop Loss will now follow the price to protect profit."
    )


def format_trailing_moved(symbol: str, new_sl: float, locked_pnl_pct: float) -> str:
    return (
        f"🔺 *STOP LOSS MOVED*\n"
        f"Coin: {symbol}\n"
        f"New Stop Loss: {new_sl:.8f}\n"
        f"If hit now, locks in: {locked_pnl_pct:+.2f}% profit"
    )


def format_tp1_hit(symbol: str) -> str:
    return (
        f"🎯 *TP1 HIT*\n"
        f"Coin: {symbol}\n"
        f"Re-analyzing market to decide: close here, or extend to TP2..."
    )


def format_tp1_extended(symbol: str, tools_agreeing: int, min_tools: int, new_sl: float, new_tp: float) -> str:
    return (
        f"✅ *CONTINUING TO TP2*\n"
        f"Coin: {symbol}\n"
        f"Fresh analysis confirmed continuation ({tools_agreeing}/{min_tools} tools)\n"
        f"New Stop Loss: {new_sl:.8f} (profit locked)\n"
        f"New Take Profit (TP2): {new_tp:.8f}"
    )


def format_tp1_closed(symbol: str, tools_agreeing: int, min_tools: int) -> str:
    return (
        f"🔒 *CLOSING AT TP1*\n"
        f"Coin: {symbol}\n"
        f"Fresh analysis did NOT confirm continuation ({tools_agreeing}/{min_tools} tools)\n"
        f"Taking profit here."
    )
