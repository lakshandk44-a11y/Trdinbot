#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Offline Profit-Chance Calibration Backtest
========================================================================

මොකද කරන්නෙ:
  Live bot එකේ කිසිම දෙයක් (coin scan, trade size, SL/TP, order execution)
  මේ script එකෙන් වෙනස් වෙන්නෙ නෑ. මේක වෙනම, offline script එකක්.

  1. ඔයාගෙම AnalysisEngine එකම (5 tools, weighted MTF) අතීත candle
     data (default: මාස 9ක්, 6-12 අතර) මතට run කරනවා - හැම historical
     point එකකදීම bot එක ඒ මොහොතේ දකින්නෙ මොකද්ද කියලා simulate කරලා.
  2. ඒ score එකේදී ඇත්තටම trade එකක් ගත්තෙනම් (TAKE_PROFIT_PERCENT /
     STOP_LOSS_PERCENT අනුව) win වුනාද loss වුනාද කියලා ඉදිරි candles
     වලින් check කරනවා.
  3. score එක (0-100) 10-buckets වලට group කරලා, bucket එකකට තිබ්බ
     ඇත්තම actual win-rate එක ගණනය කරලා calibration_table.json ලියනවා.

  analysis_engine.py එකේ _calculate_profit_chance() එක මේ ෆයිල් එක
  තියෙනවනම් calibrated % එක return කරනවා, නැත්නම් පරණ formula එකම වැඩ
  කරනවා - මේ script එක run කරන කම් කිසිම වෙනසක් නෑ.

වැදගත්:
  මේක ඔයාගෙම server එකේ (Binance API access තියෙන තැන) run කරන්න ඕන.
  Claude සිටින sandbox එකේ internet access නැති නිසා මෙතන test කරන්න බැහැ.

Usage:
  python3 backtest_calibration.py
  python3 backtest_calibration.py --symbols BTCUSDT,ETHUSDT --months 9
"""

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

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

# ============================================================
# BACKTEST SETTINGS
# ============================================================
OUTPUT_FILE = "calibration_table.json"

# How many candles the analysis engine expects to look back at, per
# timeframe (mirrors bot_core._fetch_multi_timeframe limits).
LOOKBACK_LIMIT = {"higher": 100, "medium": 150, "lower": 200}

# How far forward (in lower-timeframe candles) to look for TP/SL being hit
# before giving up on that setup (no data = sample discarded, not counted
# as a loss).
FORWARD_LOOKAHEAD_CANDLES = 200  # 200 * 15m = ~50 hours

# Evaluate every Nth medium-timeframe (1h) candle instead of every single
# one, purely to keep the backtest runtime reasonable. 1 = every candle.
STRIDE = 3

# A bucket needs at least this many backtested trades before
# analysis_engine.py will trust it over the raw heuristic score.
MIN_SAMPLES_PER_BUCKET = 20

BINANCE_KLINE_LIMIT = 1500  # Binance futures max klines per request


def fetch_full_history(client: BinanceFuturesClient, symbol: str, interval: str,
                        months_back: int) -> Optional[pd.DataFrame]:
    """Paginate Binance futures klines back `months_back` months."""
    end_time = int(time.time() * 1000)
    start_time = int((datetime.utcnow() - timedelta(days=months_back * 30)).timestamp() * 1000)

    all_rows: List[list] = []
    cursor = start_time

    while cursor < end_time:
        try:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "limit": BINANCE_KLINE_LIMIT,
            }
            resp = client.session.get(f"{client.base_url}/fapi/v1/klines", params=params)
            rows = resp.json()
        except Exception as e:
            logger.warning(f"{symbol} {interval}: fetch error at cursor {cursor}: {e}")
            break

        if not rows or not isinstance(rows, list):
            break

        all_rows.extend(rows)
        last_open_time = rows[-1][0]
        if last_open_time <= cursor:
            break  # safety: no progress, stop
        cursor = last_open_time + 1

        if len(rows) < BINANCE_KLINE_LIMIT:
            break  # reached the end of available history

        time.sleep(0.25)  # be gentle on rate limits

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset="timestamp").reset_index(drop=True)
    return df


def slice_up_to(df: pd.DataFrame, timestamp: int, tail_n: int) -> pd.DataFrame:
    """Everything the bot would have seen at `timestamp`, same as live."""
    sliced = df[df["timestamp"] <= timestamp]
    return sliced.tail(tail_n).reset_index(drop=True)


def simulate_outcome(lower_df: pd.DataFrame, entry_idx: int, direction: str,
                      entry_price: float) -> Optional[bool]:
    """
    Walk forward from entry_idx on the lower timeframe and return True if
    TAKE_PROFIT_PERCENT is hit before STOP_LOSS_PERCENT, False if SL is hit
    first, or None if neither is hit within FORWARD_LOOKAHEAD_CANDLES (in
    which case this sample is discarded - not counted as a win or a loss).
    """
    tp_pct = TAKE_PROFIT_PERCENT / 100.0
    sl_pct = STOP_LOSS_PERCENT / 100.0

    if direction == "BUY":
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
    else:
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)

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

        # If both would trigger within the same candle we can't know which
        # came first from OHLC alone - conservatively count it as the loss.
        if hit_sl and hit_tp:
            return False
        if hit_tp:
            return True
        if hit_sl:
            return False

    return None  # no clear outcome within the lookahead window


def run_backtest_for_symbol(engine: AnalysisEngine, client: BinanceFuturesClient,
                             symbol: str, months_back: int) -> List[Tuple[float, bool]]:
    """Returns a list of (raw_profit_chance_score, won) tuples for one symbol."""
    logger.info(f"Fetching history for {symbol}...")

    higher_df = fetch_full_history(client, symbol, TIMEFRAMES["higher"], months_back)
    medium_df = fetch_full_history(client, symbol, TIMEFRAMES["medium"], months_back)
    lower_df = fetch_full_history(client, symbol, TIMEFRAMES["lower"], months_back)

    if higher_df is None or medium_df is None or lower_df is None:
        logger.warning(f"{symbol}: incomplete history, skipping")
        return []

    logger.info(f"{symbol}: {len(higher_df)} {TIMEFRAMES['higher']} / "
                f"{len(medium_df)} {TIMEFRAMES['medium']} / "
                f"{len(lower_df)} {TIMEFRAMES['lower']} candles fetched")

    samples: List[Tuple[float, bool]] = []
    warmup = 60  # enough candles for the analysis tools to have real data

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
        raw_score = final.get("profit_chance", 0.0)

        if decision not in ("BUY", "SELL") or tools_agreeing < MIN_TOOLS_MATCH:
            continue

        # Find where this decision point lands in the lower-timeframe
        # series so we can walk forward candle-by-candle for the outcome.
        lower_full_idx = lower_df[lower_df["timestamp"] <= current_time].index
        if len(lower_full_idx) == 0:
            continue
        entry_idx = int(lower_full_idx[-1])
        entry_price = float(lower_df["close"].iloc[entry_idx])

        won = simulate_outcome(lower_df, entry_idx, decision, entry_price)
        if won is None:
            continue  # no clear outcome yet, discard sample

        samples.append((raw_score, won))

    logger.info(f"{symbol}: {len(samples)} labeled setups collected")
    return samples


def build_calibration_table(all_samples: List[Tuple[float, bool]]) -> Dict:
    """Bucket raw scores into 0-10, 10-20, ... 90-100 and compute actual win-rate."""
    buckets = {f"{b}-{b+10}": {"wins": 0, "total": 0} for b in range(0, 100, 10)}

    for raw_score, won in all_samples:
        b = min(int(raw_score // 10) * 10, 90)
        key = f"{b}-{b+10}"
        buckets[key]["total"] += 1
        if won:
            buckets[key]["wins"] += 1

    output_buckets = {}
    for key, counts in buckets.items():
        total = counts["total"]
        win_rate = round((counts["wins"] / total) * 100, 2) if total > 0 else None
        output_buckets[key] = {
            "win_rate": win_rate,
            "samples": total,
        }

    return output_buckets


def main():
    parser = argparse.ArgumentParser(description="Calibrate profit-chance score against real historical win-rate")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbols (default: TOP_40_COINS from config.py)")
    parser.add_argument("--months", type=int, default=9,
                         help="Months of history to backtest (default: 9, i.e. within the 6-12 month range)")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE,
                         help=f"Output file path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else TOP_40_COINS

    client = BinanceFuturesClient(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)

    # IMPORTANT: point this engine instance at a calibration file that does
    # NOT exist, so profit_chance here is always the RAW heuristic score -
    # never a previously-calibrated value. Otherwise re-running the
    # backtest after a calibration table already exists would calibrate
    # against already-calibrated numbers.
    engine_config = {"TIMEFRAMES": TIMEFRAMES, "MIN_TOOLS_MATCH": MIN_TOOLS_MATCH,
                     "CALIBRATION_TABLE_FILE": "__no_such_calibration_file__.json"}
    engine = AnalysisEngine(engine_config)

    all_samples: List[Tuple[float, bool]] = []
    for symbol in symbols:
        try:
            samples = run_backtest_for_symbol(engine, client, symbol, args.months)
            all_samples.extend(samples)
        except Exception as e:
            logger.error(f"{symbol}: backtest failed: {e}")
            continue

    if not all_samples:
        logger.error("No samples collected - nothing to write. Check API access/logs above.")
        return

    buckets = build_calibration_table(all_samples)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "symbols_used": symbols,
        "total_samples": len(all_samples),
        "min_samples_per_bucket": MIN_SAMPLES_PER_BUCKET,
        "buckets": buckets,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("=" * 60)
    logger.info(f"Calibration table written to {args.output}")
    logger.info(f"Total labeled setups: {len(all_samples)}")
    for key, b in buckets.items():
        flag = "" if (b["samples"] or 0) >= MIN_SAMPLES_PER_BUCKET else "  (not enough samples yet)"
        logger.info(f"  score {key:8s} -> actual win-rate: {b['win_rate']}%  "
                    f"({b['samples']} samples){flag}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
