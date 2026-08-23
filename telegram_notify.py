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
import time
import requests

logger = logging.getLogger("telegram_notify")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEOUT_SECONDS = 5  # never let a slow notify stall the bot


def send_telegram(message: str, max_attempts: int = 3, retry_delay_seconds: float = 1.0):
    """
    Best-effort Telegram notification. Always safe to call - never raises.

    FIX (user-reported): a single transient network/API hiccup used to
    silently drop the notification entirely - one attempt, and ANY
    failure (network error, timeout, Telegram-side outage) just logged a
    warning server-side and returned, with no retry. This meant the
    bot's own internal state could be 100% correct (e.g. a trade
    genuinely closed and its $ loss correctly recorded toward the daily
    limit) while the corresponding Telegram message never arrived - from
    the outside, this looked exactly like a "missing" trade with no way
    to tell the accounting was actually fine.

    Now retries up to max_attempts times, with a short (1s default)
    delay between attempts, before giving up - short enough that the
    very next scan cycle isn't meaningfully delayed even in the rare
    worst case, since almost all real transient failures (a dropped
    connection, a brief Telegram-side 5xx, a rate-limit 429) clear up
    within 1-2 seconds, not the full 5s connect timeout.

    A genuinely malformed request (any other 4xx, e.g. a bad chat_id or
    unparseable Markdown) is NOT retried - sending the exact same
    message again would just fail identically every time, so those fail
    immediately after the first attempt instead of wasting the retry
    budget on an error retrying can never fix.

    Nothing about the call signature callers already use changes -
    send_telegram(message) still works exactly as before; max_attempts/
    retry_delay_seconds are optional and only need to be touched by a
    caller that wants to override the defaults.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram notify skipped - TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=TIMEOUT_SECONDS,
            )

            if resp.status_code == 200:
                if attempt > 1:
                    logger.info(f"Telegram notify succeeded on attempt {attempt}/{max_attempts} "
                                f"(after {attempt - 1} earlier failure(s)).")
                return  # success - done

            # A 429 (rate limited) or any 5xx (Telegram-side issue) is
            # worth retrying - the same request will likely succeed a
            # moment later. Any OTHER 4xx (400 bad request, 403 blocked,
            # 404 bad chat_id, etc.) is a permanent problem with THIS
            # request - retrying it unchanged would just fail again, so
            # stop immediately instead of burning the retry budget.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.warning(f"Telegram notify failed permanently ({resp.status_code}) - "
                                f"not retrying (this error won't clear on retry): "
                                f"{resp.text[:300]}")
                return

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"

        except Exception as e:
            last_error = str(e)

        if attempt < max_attempts:
            logger.warning(f"Telegram notify attempt {attempt}/{max_attempts} failed "
                            f"({last_error}) - retrying in {retry_delay_seconds:.1f}s...")
            time.sleep(retry_delay_seconds)

    logger.warning(f"Telegram notify FAILED after {max_attempts} attempts - message NOT "
                    f"delivered: {last_error}")


def _md_safe(text) -> str:
    """
    FIX (Telegram 400 "can't find end of the entity"): Telegram's legacy
    `Markdown` parse_mode treats _ * ` [ as formatting markers and (unlike
    MarkdownV2) does NOT support escaping them with a backslash - an odd
    number of any of these in the message makes Telegram reject the WHOLE
    message. Values we interpolate that come from trade data are safe
    (symbol/side are plain uppercase letters, numbers have no markers) with
    ONE exception: close_reason values like "STOP_LOSS" / "TAKE_PROFIT" /
    "REVERSAL_SIGNAL" contain underscores, which is exactly what broke
    every SL-hit close notification. Since legacy Markdown can't escape
    these, the safe fix is to strip/neutralize the marker characters from
    any dynamic text before it goes into the message - formatting is only
    ever applied to the static labels we write ourselves, never to dynamic
    values, so this is safe for every current and future caller.
    """
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "")


def format_trade_opened(trade: dict) -> str:
    """
    FIX (user request): now also shows WHICH of the 5 tools agreed and
    WHICH of their own named sub-concepts fired, per timeframe (4h/1h/15m)
    - exactly the same tools/sub-concepts that decided this trade, read
    straight from trade["analysis"]["tool_breakdown"] (see bot_core
    ._execute_trade). Falls back to the original simple message untouched
    if that breakdown isn't present (e.g. an older trade from before this
    change, still open across a bot restart).

    FIX (user request): also shows WHICH entry path triggered this trade -
    a single very-strong timeframe on its own ("Path A" /
    single_tf_strong), or all 3 timeframes independently confirming
    ("Path B" / all_tf_confirmed) - read from trade["analysis"]["entry_path"].
    """
    entry_path = (trade.get("analysis") or {}).get("entry_path")
    path_labels = {
        "single_tf_strong": "🔥 Single Strong Timeframe",
        "all_tf_confirmed": "✅ All 3 Timeframes Confirmed",
        "pattern_match": "🔷 Pattern Match",
    }
    path_line = f"\nEntry Path: {path_labels.get(entry_path, 'Unknown')}" if entry_path in path_labels else ""

    pattern_name = (trade.get("analysis") or {}).get("pattern_name")
    pattern_confidence = (trade.get("analysis") or {}).get("pattern_confidence")
    if entry_path == "pattern_match" and pattern_name:
        path_line += f"\nPattern: {_md_safe(pattern_name)} ({pattern_confidence}% confidence)"

    header = (
        f"🟢 *TRADE OPENED*\n"
        f"Coin: {_md_safe(trade['symbol'])}  ({_md_safe(trade['side'])})\n"
        f"Entry: {trade['entry_price']:.8f}\n"
        f"Take Profit: {trade['take_profit']:.8f}\n"
        f"Stop Loss: {trade['stop_loss']:.8f}\n"
        f"Qty: {trade['quantity']}  |  Leverage: {trade['leverage']}x"
        f"{path_line}"
    )

    breakdown = (trade.get("analysis") or {}).get("tool_breakdown") or {}
    tf_labels = [("higher", "4h"), ("medium", "1h"), ("lower", "15m")]
    lines = []
    for tf_key, tf_label in tf_labels:
        tools = breakdown.get(tf_key) or []
        if not tools:
            continue
        tool_bits = [f"{_md_safe(t['tool'])} ({_md_safe(', '.join(t['subconcepts']))})" for t in tools]
        lines.append(f"⏱ {tf_label}: " + "  |  ".join(tool_bits))

    if lines:
        header += "\n\n📊 *Why this trade opened:*\n" + "\n".join(lines)

    return header


_CLOSE_REASON_LABELS = {
    "TAKE_PROFIT":     ("🎯", "Take Profit Hit"),
    "STOP_LOSS":       ("🛑", "Stop Loss Hit"),
    "REVERSAL_SIGNAL": ("🔄", "Reversal Signal"),
    "MANUAL_CLOSE":    ("✋", "Manually Closed"),
}


def format_trade_closed(trade: dict, balance: float = None, real_pnl_usdt: float = None) -> str:
    """
    FIX (user request): clearer layout - friendly reason label/icon instead
    of a raw code like "STOP_LOSS", and PROFIT/LOSS + reason + balance each
    on their own clearly-labeled line. Same underlying data as before
    (pnl_percent, close_reason, entry/exit price, real_pnl_usdt, balance) -
    nothing about what triggers this message or what data it receives changed.
    """
    result = "PROFIT" if trade["pnl_percent"] > 0 else "LOSS"
    icon = "🟢" if trade["pnl_percent"] > 0 else "🔴"
    reason_key = trade.get("close_reason", "N/A")
    reason_icon, reason_label = _CLOSE_REASON_LABELS.get(reason_key, ("ℹ️", _md_safe(reason_key)))

    real_pnl_line = f"\nReal PnL (Binance): {real_pnl_usdt:+.2f} USDT" if real_pnl_usdt is not None else ""
    balance_line = f"\n💰 Balance now: {balance:.2f} USDT" if balance is not None else ""

    return (
        f"{icon} *TRADE CLOSED — {result}*\n"
        f"Coin: {_md_safe(trade['symbol'])}  ({_md_safe(trade['side'])})\n"
        f"Reason: {reason_icon} {reason_label}\n"
        f"Entry: {trade['entry_price']:.8f}\n"
        f"Exit: {trade.get('close_price', 0):.8f}\n"
        f"PnL: {trade['pnl_percent']:+.2f}%"
        f"{real_pnl_line}"
        f"{balance_line}"
    )


def format_trailing_activated(symbol: str, side: str, pnl_pct: float) -> str:
    return (
        f"🔺 *TRAILING STOP ACTIVATED*\n"
        f"Coin: {_md_safe(symbol)}\n"
        f"Side: {_md_safe(side)}\n"
        f"Profit now: +{pnl_pct:.2f}%\n"
        f"Stop Loss will now follow the price to protect profit."
    )


def format_trailing_moved(symbol: str, new_sl: float, locked_pnl_pct: float) -> str:
    outcome = "profit" if locked_pnl_pct >= 0 else "loss"
    return (
        f"🔺 *STOP LOSS MOVED*\n"
        f"Coin: {_md_safe(symbol)}\n"
        f"New Stop Loss: {new_sl:.8f}\n"
        f"If hit now: {locked_pnl_pct:+.2f}% ({outcome})"
    )


def format_tp1_hit(symbol: str, from_stage: int = 1, to_stage: int = 2) -> str:
    return (
        f"🎯 *TP{from_stage} HIT*\n"
        f"Coin: {_md_safe(symbol)}\n"
        f"Re-analyzing market to decide: close here, or extend to TP{to_stage}..."
    )


def format_tp1_extended(symbol: str, tools_agreeing: int, min_tools: int, new_sl: float, new_tp: float,
                         to_stage: int = 2) -> str:
    return (
        f"✅ *CONTINUING TO TP{to_stage}*\n"
        f"Coin: {_md_safe(symbol)}\n"
        f"Fresh analysis confirmed continuation ({tools_agreeing}/{min_tools} tools)\n"
        f"New Stop Loss: {new_sl:.8f} (profit locked)\n"
        f"New Take Profit (TP{to_stage}): {new_tp:.8f}"
    )


def format_tp1_closed(symbol: str, tools_agreeing: int, min_tools: int, at_stage: int = 1) -> str:
    return (
        f"🔒 *CLOSING AT TP{at_stage}*\n"
        f"Coin: {_md_safe(symbol)}\n"
        f"Fresh analysis did NOT confirm continuation ({tools_agreeing}/{min_tools} tools)\n"
        f"Taking profit here."
    )
