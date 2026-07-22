"""
Telegram notification helper for the trading bot.

Replaces the earlier WhatsApp attempts entirely (Baileys/QR kept failing
with "Connection Failure" - almost certainly WhatsApp blocking the AWS
EC2 IP range; Twilio Sandbox worked but isn't free/permanent long-term).
Telegram's Bot API is official, simple (one HTTP POST per message), has
no cloud-IP restrictions, and is completely free with no message limits.

This is intentionally fire-and-forget and fully fail-safe: if credentials
aren't set, Telegram is unreachable, or anything else goes wrong, this
NEVER raises - it just logs a warning and the bot carries on exactly as
if this module didn't exist. Trading logic must never depend on or be
blocked by a notification succeeding.

Setup:
  1. Open Telegram, search for "BotFather", start a chat
  2. Send: /newbot   and follow the prompts (choose any name/username)
  3. BotFather gives you a token like "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  4. Send YOUR new bot any message (e.g. "hi") so it can message you back
  5. Get your chat_id by visiting (in a browser, replace <TOKEN>):
       https://api.telegram.org/bot<TOKEN>/getUpdates
     Look for "chat":{"id": <number>, ...} in the response - that number
     is your TELEGRAM_CHAT_ID.
  6. Set these environment variables before starting the bot:
       export TELEGRAM_BOT_TOKEN="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
       export TELEGRAM_CHAT_ID="your_chat_id_number"
"""

import logging
import os
import requests

logger = logging.getLogger("telegram_notify")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEOUT_SECONDS = 5  # never let a slow notify stall the bot


def send_telegram(message: str):
    """Best-effort Telegram notification. Always safe to call - never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram notify skipped - TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.warning(f"Telegram notify failed ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        logger.warning(f"Telegram notify unavailable: {e}")


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


def format_trade_closed(trade: dict, balance: float = None, real_pnl_usdt: float = None) -> str:
    result = "PROFIT" if trade["pnl_percent"] > 0 else "LOSS"
    icon = "🟢" if trade["pnl_percent"] > 0 else "🔴"
    real_pnl_line = f"\nReal PnL (Binance): {real_pnl_usdt:+.2f} USDT" if real_pnl_usdt is not None else ""
    balance_line = f"\nBalance now: {balance:.2f} USDT" if balance is not None else ""
    return (
        f"{icon} *TRADE CLOSED — {result}*\n"
        f"Coin: {trade['symbol']}\n"
        f"Side: {trade['side']}\n"
        f"PnL: {trade['pnl_percent']:+.2f}%"
        f"{real_pnl_line}\n"
        f"Reason: {trade.get('close_reason', 'N/A')}\n"
        f"Entry: {trade['entry_price']:.8f}\n"
        f"Exit: {trade.get('close_price', 0):.8f}"
        f"{balance_line}"
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
    outcome = "profit" if locked_pnl_pct >= 0 else "loss"
    return (
        f"🔺 *STOP LOSS MOVED*\n"
        f"Coin: {symbol}\n"
        f"New Stop Loss: {new_sl:.8f}\n"
        f"If hit now: {locked_pnl_pct:+.2f}% ({outcome})"
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
