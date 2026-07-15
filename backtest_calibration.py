#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Profit-Chance Calibration Backtest
========================================================================

මොකද කරන්නෙ:
  analysis_engine.py එකේ _calculate_profit_chance() එකෙන් දෙන raw heuristic
  score එක (0-100), ඇත්තටම historical data එකේදී කොච්චර % ට්‍රේඩ් ජයග්‍රහණය
  කරනවද කියලා walk-forward විදියට Binance real historical candles උඩ
  test කරලා, score එක bucket 10ක් (0-10, 10-20, ... 90-100) වලට කඩලා,
  bucket එකකට තියෙන ඇත්ත win-rate එක calculate කරලා
  calibration_table.json ගොනුවට ලියනවා.

  analysis_engine.py (_get_calibrated_profit_chance) මේ ගොනුව load කරලා,
  raw heuristic score එකක් ආවම ඒ score එකේ bucket එකට ගැලපෙන ඇත්ත
  historical win-rate එක return කරනවා (ප්‍රමාණවත් samples තියෙනවනම්) -
  score එක ම නිකම් "heuristic" එකක් නෙවෙයි, ඇත්තටම backtest කරපු
  win-rate එකක් වෙනවා.

  මේකෙන් bot_core.py, trade_manager.py, config.py, analysis_engine.py -
  කිසිම existing file එකකට වෙනසක් වෙන්නෙ නෑ (bot_core.klines() එකට
  optional startTime/endTime pagination params 2ක් විතරයි add කරලා
  තියෙන්නෙ, පරණ callers කිසිවක්වත් break කරන්නෙ නෑ). මේක සම්පූර්ණයෙන්ම
  වෙනම, read-only analysis script එකක්.

වැදගත්:
  මේක ඔයාගෙම server එකේ (Binance API access තියෙන තැන) run කරන්න ඕන -
  Claude සිටින sandbox එකේ internet access නැති නිසා මෙතන test කරන්න බැහැ.

Usage:
  python3 backtest_calibration.py
  python3 backtest_calibration.py --symbols BTCUSDT,ETHUSDT --months 9
"""

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    TOP_40_COINS, TIMEFRAMES, MIN_TOOLS_MATCH,
    TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT,
)
from bot_core import BinanceFuturesClient
from analysis_engine import AnalysisEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s"
)
logger = logging.getLogger("backtest_calibration")

OUTPUT_FILE = "calibration_table.json"

# Same lookback windows the live bot fetches per timeframe (see
# bot_core._fetch_multi_timeframe), so every walk-forward "snapshot" the
# engine sees here has the same amount of context the live bot would have.
LOOKBACK_LIMIT = {"higher": 100, "medium": 150, "lower": 200}
FORWARD_LOOKAHEAD_CANDLES = 200  # 200 * 15m = ~50 hours to resolve TP/SL
STRIDE = 1  # FIX: was 3 (every 3rd medium-timeframe candle) to save runtime,
# but that meant the hour-of-day breakdown only ever sampled hours at a
# fixed 3-hour offset (2,5,8,11,14,17,20,23 UTC), leaving the other 16
# hours completely unsampled (0 setups) - not "bad", just never checked.
# STRIDE=1 evaluates every medium-timeframe (1h) candle, giving full
# 24-hour coverage. Runtime will be ~3x longer.

# Binance kline interval -> milliseconds, used to paginate klines() by
# startTime/endTime instead of only ever getting the most recent `limit`.
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

KLINE_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore"
]

# FIX (IP-ban cascade bug): the script used to have no delay between
# symbols/timeframes (only a 0.25s sleep between *pages* of the SAME
# symbol+timeframe), so 40 coins x 3 timeframes x several pages each fired
# far faster than Binance's rate limit tolerates, tripping a -1003 IP ban.
# Once banned, every request for the rest of the run just got the same
# "banned until <ms>" error back instantly - the script never waited it
# out, so it burned through most of the coin list getting nothing.
# This constant adds a small pause between every individual request
# (page, timeframe, or symbol) so the run stays under the limit and
# shouldn't get banned in the first place.
REQUEST_PACING_SECONDS = 1.0

_BAN_MSG_RE = re.compile(r"banned until (\d+)")


def _extract_ban_until_ms(payload) -> Optional[int]:
    """Pull the 'banned until <epoch_ms>' timestamp out of a Binance -1003
    error payload/message, if present."""
    msg = ""
    if isinstance(payload, dict):
        msg = str(payload.get("msg", ""))
    else:
        msg = str(payload)
    match = _BAN_MSG_RE.search(msg)
    return int(match.group(1)) if match else None


def _wait_out_ban(ban_until_ms: int, context: str):
    """FIX (IP-ban cascade bug): if Binance has banned this IP, sleep until
    the ban actually lifts (plus a small safety buffer) instead of
    continuing to fire requests into the ban window - repeated requests
    during an active ban don't get more data, they just risk the ban being
    extended and waste the whole rest of the run."""
    now_ms = int(time.time() * 1000)
    wait_seconds = max(0.0, (ban_until_ms - now_ms) / 1000.0) + 2.0
    logger.warning(f"⏳ {context}: IP is rate-limit banned by Binance. "
                    f"Waiting {wait_seconds:.0f}s for the ban to lift before continuing...")
    time.sleep(wait_seconds)


def klines_with_ban_handling(client: BinanceFuturesClient, context: str, **kwargs):
    """Wraps client.klines() so a -1003 IP ban pauses and retries instead of
    being treated as a permanent per-symbol failure."""
    max_ban_retries = 5
    for attempt in range(max_ban_retries):
        batch = client.klines(**kwargs)
        if isinstance(batch, dict) and batch.get("code") == -1003:
            ban_until = _extract_ban_until_ms(batch)
            if ban_until:
                _wait_out_ban(ban_until, context)
                continue  # retry the same request now that the ban should be lifted
        return batch
    return batch  # give up after max_ban_retries, let the caller's normal error handling deal with it


def fetch_full_history(client: BinanceFuturesClient, symbol: str, interval: str,
                        months_back: int) -> Optional[pd.DataFrame]:
    """
    Pages through Binance's klines endpoint (1500 candles per request, the
    exchange's max) using startTime, from `months_back` months ago up to
    now, and returns one combined DataFrame - same column layout/typing as
    bot_core._fetch_multi_timeframe uses live, so the analysis engine sees
    identical data shape whether it's backtesting or trading live.
    """
    interval_ms = INTERVAL_MS.get(interval)
    if interval_ms is None:
        logger.error(f"Unknown interval '{interval}' - add it to INTERVAL_MS.")
        return None

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=30 * months_back)).timestamp() * 1000)

    all_rows: List[list] = []
    cursor = start_ms
    max_requests = 200  # safety cap (200 * 1500 candles is far more than any months_back needs)

    for _ in range(max_requests):
        if cursor >= end_ms:
            break
        try:
            batch = klines_with_ban_handling(
                client, f"{symbol} {interval}",
                symbol=symbol, interval=interval, limit=1500,
                start_time=cursor, end_time=end_ms
            )
        except Exception as e:
            logger.warning(f"{symbol} {interval}: klines request failed at cursor={cursor}: {e}")
            break  # FIX (partial-history discard bug): break, don't return None -
            # whatever was already collected in all_rows is still used below.

        # FIX ('backtest failed: -1' bug, round 2): the known -1121-style
        # error dict is handled below, but ANY other unexpected response
        # shape (a differently-shaped rate-limit body, a transient
        # malformed payload, etc.) used to crash unguarded on
        # `batch[-1][0]` with a bare, opaque exception (e.g. KeyError(-1))
        # that propagated all the way up to main() as the unhelpful
        # "{symbol}: backtest failed: -1" message - with NO clue what
        # actually went wrong. Everything below is now wrapped so any such
        # surprise is caught here, logged with the actual type/content of
        # the bad response, and the symbol is skipped cleanly instead of
        # crashing.
        try:
            # FIX ('backtest failed: -1' bug): when a symbol is invalid, delisted,
            # or renamed on Binance Futures (e.g. an old TOP_40_COINS entry that
            # no longer exists as-is), the klines endpoint returns a JSON error
            # OBJECT like {"code": -1121, "msg": "Invalid symbol."} instead of a
            # list of candles. That dict is truthy and iterable, so without this
            # check it silently got extended into all_rows (as its string keys)
            # and then `batch[-1][0]` crashed with a bare KeyError(-1) deep in
            # the pagination loop - which surfaced up in main() as the
            # unhelpful "{symbol}: backtest failed: -1" message. Now it's
            # detected immediately and skipped with a clear reason instead.
            #
            # FIX (partial-history discard bug): confirmed via web search that
            # BATUSDT/AVAXUSDT/ICPUSDT are all still valid, actively-traded
            # Binance Futures symbols (not delisted/renamed) - yet they were
            # showing up as "incomplete history, skipping". The real cause:
            # if a LATER page (e.g. month 7 of 9) hit a persistent rate-limit
            # ban that outlasted klines_with_ban_handling's retries, this
            # branch fired and discarded ALL already-collected months of
            # good data via `return None`, even though the symbol/data were
            # completely fine. Now: if we already have SOME rows, we keep
            # them (break out and use what we've got) instead of throwing
            # everything away. Only a symbol that fails on its very FIRST
            # page (all_rows still empty - the real "invalid symbol" case,
            # like the old plain SHIBUSDT) still results in None.
            if isinstance(batch, dict):
                if all_rows:
                    logger.warning(f"{symbol} {interval}: Binance returned an error "
                                    f"(code={batch.get('code')}, msg={batch.get('msg')}) after already "
                                    f"collecting {len(all_rows)} candles - using partial history instead "
                                    f"of discarding it.")
                    break
                logger.warning(f"{symbol} {interval}: Binance returned an error instead of candles "
                                f"(code={batch.get('code')}, msg={batch.get('msg')}) - symbol is "
                                f"likely invalid/delisted/renamed on Futures. Skipping this symbol.")
                return None

            if not batch:
                break

            all_rows.extend(batch)

            last_open_time = int(batch[-1][0])
            next_cursor = last_open_time + interval_ms
            if next_cursor <= cursor:
                break  # exchange returned no forward progress - stop instead of looping forever
            cursor = next_cursor

            if len(batch) < 1500:
                break  # short batch means we've caught up to "now"
        except Exception as e:
            if all_rows:
                logger.warning(f"{symbol} {interval}: unexpected response shape after already "
                                f"collecting {len(all_rows)} candles ({e!r}) - using partial history "
                                f"instead of discarding it.")
                break
            logger.warning(f"{symbol} {interval}: unexpected response shape while paginating "
                            f"(type={type(batch).__name__}, sample={str(batch)[:200]!r}) - {e!r}. "
                            f"Skipping this symbol.")
            return None

        time.sleep(REQUEST_PACING_SECONDS)  # be gentle with rate limits across a multi-month pull

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=KLINE_COLUMNS)
    for col in ["timestamp", "open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def slice_up_to(df: pd.DataFrame, current_time: int, limit: int) -> pd.DataFrame:
    """Returns the last `limit` candles at or before current_time - i.e. exactly
    the window the live bot would have seen if 'now' were current_time."""
    sliced = df[df["timestamp"] <= current_time]
    return sliced.tail(limit).reset_index(drop=True)


def simulate_outcome(lower_df: pd.DataFrame, entry_idx: int, direction: str,
                      tp_price: float, sl_price: float) -> Optional[bool]:
    """Walk forward on the lower timeframe candle-by-candle until TP or SL is
    hit. Returns True (win), False (loss), or None if neither resolved
    within the lookahead window (that setup is discarded - not a fixed
    "loss", genuinely unresolved so counting it either way would be wrong)."""
    end_idx = min(entry_idx + FORWARD_LOOKAHEAD_CANDLES, len(lower_df) - 1)
    for i in range(entry_idx + 1, end_idx + 1):
        high = lower_df["high"].iloc[i]
        low = lower_df["low"].iloc[i]
        if direction == "BUY":
            hit_sl = low <= sl_price
            hit_tp = high >= tp_price
        else:
            hit_sl = high >= sl_price
            hit_tp = low <= tp_price

        if hit_sl and hit_tp:
            return False  # can't tell which came first intra-candle - count as loss
        if hit_tp:
            return True
        if hit_sl:
            return False
    return None


def run_calibration_for_symbol(engine: AnalysisEngine, client: BinanceFuturesClient,
                                symbol: str, months_back: int) -> List[Dict]:
    """Returns a list of {"score": float, "won": bool} labeled setups for one symbol,
    using the bot's OWN current fixed-percent TP/SL (TAKE_PROFIT_PERCENT/
    STOP_LOSS_PERCENT) - this calibrates the score against exactly what the
    live bot actually trades with today."""
    logger.info(f"Fetching history for {symbol}...")

    higher_df = fetch_full_history(client, symbol, TIMEFRAMES["higher"], months_back)
    time.sleep(REQUEST_PACING_SECONDS)
    medium_df = fetch_full_history(client, symbol, TIMEFRAMES["medium"], months_back)
    time.sleep(REQUEST_PACING_SECONDS)
    lower_df = fetch_full_history(client, symbol, TIMEFRAMES["lower"], months_back)

    if higher_df is None or medium_df is None or lower_df is None:
        logger.warning(f"{symbol}: incomplete history, skipping")
        return []

    logger.info(f"{symbol}: {len(higher_df)} {TIMEFRAMES['higher']} / "
                f"{len(medium_df)} {TIMEFRAMES['medium']} / "
                f"{len(lower_df)} {TIMEFRAMES['lower']} candles fetched")

    tp_pct = TAKE_PROFIT_PERCENT / 100.0
    sl_pct = STOP_LOSS_PERCENT / 100.0
    warmup = 60

    labeled: List[Dict] = []

    for i in range(warmup, len(medium_df), STRIDE):
        current_time = int(medium_df["timestamp"].iloc[i])

        higher_slice = slice_up_to(higher_df, current_time, LOOKBACK_LIMIT["higher"])
        medium_slice = slice_up_to(medium_df, current_time, LOOKBACK_LIMIT["medium"])
        lower_slice = slice_up_to(lower_df, current_time, LOOKBACK_LIMIT["lower"])

        if len(higher_slice) < 50 or len(medium_slice) < 50 or len(lower_slice) < 50:
            continue

        try:
            result = engine.multi_timeframe_analysis({
                "higher": higher_slice, "medium": medium_slice, "lower": lower_slice
            })
        except Exception as e:
            logger.debug(f"{symbol}: analysis error at {current_time}: {e}")
            continue

        final = result.get("final_signal", {})
        decision = final.get("decision", "HOLD")
        tools_agreeing = final.get("tools_agreeing", 0)

        if decision not in ("BUY", "SELL") or tools_agreeing < MIN_TOOLS_MATCH:
            continue

        lower_full_idx = lower_df[lower_df["timestamp"] <= current_time].index
        if len(lower_full_idx) == 0:
            continue
        entry_idx = int(lower_full_idx[-1])
        entry_price = float(lower_df["close"].iloc[entry_idx])

        if decision == "BUY":
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        won = simulate_outcome(lower_df, entry_idx, decision, tp_price, sl_price)
        if won is None:
            continue  # unresolved within lookahead - discard, don't guess

        raw_score = float(final.get("profit_chance", 0.0))
        entry_hour_utc = datetime.utcfromtimestamp(current_time / 1000).hour
        labeled.append({"score": raw_score, "won": won, "hour": entry_hour_utc})

    logger.info(f"{symbol}: {len(labeled)} labeled setups collected")
    return labeled


def build_buckets(labeled: List[Dict]) -> Dict[str, Dict]:
    """Groups labeled setups into 10-wide score buckets (0-10 ... 90-100)
    and computes the actual win-rate observed in each bucket - this is the
    table analysis_engine._get_calibrated_profit_chance() looks up."""
    buckets: Dict[str, Dict] = {}
    for floor in range(0, 100, 10):
        buckets[f"{floor}-{floor + 10}"] = {"wins": 0, "samples": 0}

    for item in labeled:
        floor = int(item["score"] // 10) * 10
        floor = min(floor, 90)
        key = f"{floor}-{floor + 10}"
        buckets[key]["samples"] += 1
        if item["won"]:
            buckets[key]["wins"] += 1

    result: Dict[str, Dict] = {}
    for key, b in buckets.items():
        win_rate = round(b["wins"] / b["samples"] * 100, 2) if b["samples"] else None
        result[key] = {"win_rate": win_rate, "samples": b["samples"]}
    return result


def build_hour_buckets(labeled: List[Dict]) -> Dict[str, Dict]:
    """Groups labeled setups by entry hour (UTC, 0-23) and computes the
    real win-rate observed in each hour - lets us check whether trading
    session/time-of-day (e.g. Asian vs London/NY overlap) actually makes a
    measurable difference for this bot's setups, instead of assuming it
    does or doesn't based on theory alone."""
    buckets: Dict[str, Dict] = {f"{h:02d}:00-{h:02d}:59": {"wins": 0, "samples": 0} for h in range(24)}

    for item in labeled:
        key = f"{item['hour']:02d}:00-{item['hour']:02d}:59"
        buckets[key]["samples"] += 1
        if item["won"]:
            buckets[key]["wins"] += 1

    result: Dict[str, Dict] = {}
    for key, b in buckets.items():
        win_rate = round(b["wins"] / b["samples"] * 100, 2) if b["samples"] else None
        result[key] = {"win_rate": win_rate, "samples": b["samples"]}
    return result


def main():
    parser = argparse.ArgumentParser(description="Calibrate the profit-chance heuristic score against real historical win-rates")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbols (default: TOP_40_COINS from config.py)")
    parser.add_argument("--months", type=int, default=9,
                         help="Months of history to backtest (default: 9)")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE,
                         help=f"Output file path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else TOP_40_COINS

    client = BinanceFuturesClient(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)

    # CALIBRATION_TABLE_FILE points at a file that doesn't exist so the
    # engine used purely for backtesting always returns the RAW heuristic
    # score (never an already-calibrated one) - we're building the table,
    # not consuming it.
    engine_config = {"TIMEFRAMES": TIMEFRAMES, "MIN_TOOLS_MATCH": MIN_TOOLS_MATCH,
                      "CALIBRATION_TABLE_FILE": "__no_such_calibration_file__.json"}
    engine = AnalysisEngine(engine_config)

    all_labeled: List[Dict] = []
    for symbol in symbols:
        try:
            labeled = run_calibration_for_symbol(engine, client, symbol, args.months)
            all_labeled.extend(labeled)
        except Exception as e:
            logger.error(f"{symbol}: calibration failed: {e}")
            continue
        time.sleep(REQUEST_PACING_SECONDS)  # pace between symbols too

    if not all_labeled:
        logger.error("No samples collected - nothing to write. Check API access/logs above.")
        return

    buckets = build_buckets(all_labeled)
    hour_buckets = build_hour_buckets(all_labeled)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "symbols_used": symbols,
        "total_samples": len(all_labeled),
        "buckets": buckets,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    # Hour-of-day breakdown is written to its own file, separate from
    # calibration_table.json. analysis_engine._get_calibrated_profit_chance()
    # only knows how to read the score-bucket schema above - keeping this
    # separate means the live bot's calibration lookup is completely
    # unaffected; this file is purely for us to inspect (e.g. "is UTC 13-16
    # genuinely better than UTC 02-05?") before deciding whether a
    # session/time filter is worth adding.
    hourly_output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "total_samples": len(all_labeled),
        "note": "hour is UTC, entry candle time. Informational only - not read by the live bot.",
        "hour_buckets": hour_buckets,
    }
    hourly_path = "hourly_breakdown.json"
    with open(hourly_path, "w") as f:
        json.dump(hourly_output, f, indent=2)

    logger.info("=" * 65)
    logger.info(f"Calibration table written to {args.output}")
    logger.info(f"Total setups evaluated: {len(all_labeled)}")
    logger.info("-" * 65)
    for key, b in buckets.items():
        logger.info(f"  {key:>7}: win_rate={b['win_rate']}%  samples={b['samples']}")
    logger.info("=" * 65)
    logger.info(f"Hour-of-day (UTC) breakdown written to {hourly_path}")
    logger.info("-" * 65)
    for key, b in hour_buckets.items():
        logger.info(f"  {key}: win_rate={b['win_rate']}%  samples={b['samples']}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
