#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Dynamic (Analysis-Based) TP/SL Backtest
========================================================================

මොකද කරන්නෙ:
  bot_core.py එකේ _get_analysis_based_tp_sl() එකේ තියෙන logic එකම (order
  block / FVG / liquidity levels වලින් TP/SL ගණනය කිරීම, 0.5x-3x clamp
  එකත් ඇතුළුව) මේ script එකේ, පරණ backtest_calibration.py එකේම historical
  data walk-forward loop එක මතට run කරලා, ඒක:

    (A) දැනට bot එක use කරන FIXED TAKE_PROFIT_PERCENT/STOP_LOSS_PERCENT
        approach එකට
    (B) අලුත් DYNAMIC (analysis-based) TP/SL approach එකට

  දෙකටම **එකම historical setups (එකම entry points, එකම market data)**
  මතින්ම win-rate සහ average PnL% compare කරලා report එකක් දෙනවා.

  මේකෙන් bot_core.py, trade_manager.py, config.py, analysis_engine.py,
  backtest_calibration.py - කිසිම existing file එකකට වෙනසක් වෙන්නෙ නෑ.
  මේක සම්පූර්ණයෙන්ම වෙනම, read-only analysis script එකක්.

වැදගත්:
  මේකත් ඔයාගෙම server එකේ (Binance API access තියෙන තැන) run කරන්න ඕන -
  Claude සිටින sandbox එකේ internet access නැති නිසා මෙතන test කරන්න බැහැ.

Usage:
  python3 backtest_dynamic_tpsl.py
  python3 backtest_dynamic_tpsl.py --symbols BTCUSDT,ETHUSDT --months 9
"""

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    TOP_40_COINS, TIMEFRAMES, MIN_TOOLS_MATCH,
    TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT,
)
from bot_core import BinanceFuturesClient
from analysis_engine import AnalysisEngine

# Reuse the exact same historical-data machinery as the existing
# calibration backtest, instead of duplicating it.
from backtest_calibration import fetch_full_history, slice_up_to

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s"
)
logger = logging.getLogger("backtest_dynamic_tpsl")

OUTPUT_FILE = "dynamic_tpsl_backtest_report.json"

LOOKBACK_LIMIT = {"higher": 100, "medium": 150, "lower": 200}
FORWARD_LOOKAHEAD_CANDLES = 200  # 200 * 15m = ~50 hours
STRIDE = 1  # FIX: was 3 - matches backtest_calibration.py's STRIDE=1 fix (full coverage, not every-3rd-candle)


def get_dynamic_tp_sl(side: str, entry_price: float, analysis: Dict) -> Tuple[Optional[float], Optional[float]]:
    """
    Mirrors bot_core.HackerAIBot._get_analysis_based_tp_sl() exactly - same
    candidate levels, same STRENGTH-based selection, NO clamp - so this
    backtest measures precisely what the live bot would have done.

    FIX (sync with live bot): this used to select the NEAREST candidate
    level and clamp the distance to [0.5x, 3x] of the fixed %, which is
    what the live bot's TP/SL logic ALSO used to do - but the live bot was
    later changed (per explicit request) to (1) drop the clamp entirely,
    using the detected level exactly as-is, and (2) select the STRONGEST
    candidate (by OB displacement / FVG size / liquidity-type weight)
    instead of the nearest one. This function had fallen out of sync with
    that change, silently backtesting a version of "dynamic TP/SL" the
    live bot no longer actually runs.
    """
    resistance_candidates = []  # list of (level, strength)
    support_candidates = []

    for tf_name in ["higher", "medium"]:
        tf = analysis.get(tf_name)
        if not tf:
            continue

        ob = tf.get("order_block", {})
        bear_ob = ob.get("bearish_ob")
        if bear_ob and bear_ob.get("level"):
            ob_strength = bear_ob.get("high", bear_ob["level"]) - bear_ob.get("low", bear_ob["level"])
            resistance_candidates.append((bear_ob["level"], ob_strength))
        bull_ob = ob.get("bullish_ob")
        if bull_ob and bull_ob.get("level"):
            ob_strength = bull_ob.get("high", bull_ob["level"]) - bull_ob.get("low", bull_ob["level"])
            support_candidates.append((bull_ob["level"], ob_strength))

        for fvg in tf.get("fvg", {}).get("fvg_levels", []):
            fvg_strength = fvg.get("size", 0)
            if fvg.get("type") == "bearish" and fvg.get("low"):
                resistance_candidates.append((fvg["low"], fvg_strength))
            elif fvg.get("type") == "bullish" and fvg.get("high"):
                support_candidates.append((fvg["high"], fvg_strength))

        liq = tf.get("liquidity", {})
        liq_strength = entry_price * (0.01 if liq.get("external_swept") else 0.005)
        if liq.get("buyside_liquidity"):
            resistance_candidates.append((liq["buyside_liquidity"], liq_strength))
        if liq.get("sellside_liquidity"):
            support_candidates.append((liq["sellside_liquidity"], liq_strength))

    if side == "BUY":
        tp_pool = [(lvl, s) for lvl, s in resistance_candidates if lvl > entry_price]
        sl_pool = [(lvl, s) for lvl, s in support_candidates if lvl < entry_price]
        tp_level = max(tp_pool, key=lambda x: x[1])[0] if tp_pool else None
        sl_level = max(sl_pool, key=lambda x: x[1])[0] if sl_pool else None
    else:
        tp_pool = [(lvl, s) for lvl, s in support_candidates if lvl < entry_price]
        sl_pool = [(lvl, s) for lvl, s in resistance_candidates if lvl > entry_price]
        tp_level = max(tp_pool, key=lambda x: x[1])[0] if tp_pool else None
        sl_level = max(sl_pool, key=lambda x: x[1])[0] if sl_pool else None

    return tp_level, sl_level


def simulate_outcome_at_prices(lower_df: pd.DataFrame, entry_idx: int, direction: str,
                                tp_price: float, sl_price: float) -> Optional[bool]:
    """Same walk-forward TP/SL race as backtest_calibration.simulate_outcome,
    but takes explicit TP/SL prices instead of deriving them from a fixed %,
    so it works for both the fixed and dynamic price levels."""
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


def run_backtest_for_symbol(engine: AnalysisEngine, client: BinanceFuturesClient,
                             symbol: str, months_back: int) -> List[Dict]:
    """
    Returns a list of per-setup comparison records:
    {"fixed_won": bool, "fixed_pnl_pct": float,
     "dynamic_won": bool|None, "dynamic_pnl_pct": float|None}
    dynamic_won is None when the analysis had no usable level for that
    setup (falls back to fixed live, so it's excluded from the dynamic
    comparison rather than counted either way).
    """
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

    records: List[Dict] = []
    warmup = 60

    tp_pct = TAKE_PROFIT_PERCENT / 100.0
    sl_pct = STOP_LOSS_PERCENT / 100.0

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

        # ---- Fixed % outcome (what the bot did before this change) ----
        if decision == "BUY":
            fixed_tp = entry_price * (1 + tp_pct)
            fixed_sl = entry_price * (1 - sl_pct)
        else:
            fixed_tp = entry_price * (1 - tp_pct)
            fixed_sl = entry_price * (1 + sl_pct)

        fixed_won = simulate_outcome_at_prices(lower_df, entry_idx, decision, fixed_tp, fixed_sl)
        if fixed_won is None:
            continue  # no clear outcome within lookahead - discard this setup entirely

        fixed_pnl_pct = (tp_pct * 100) if fixed_won else -(sl_pct * 100)

        # ---- Dynamic (analysis-based) outcome, same entry point ----
        dyn_tp, dyn_sl = get_dynamic_tp_sl(decision, entry_price, result)
        dynamic_won = None
        dynamic_pnl_pct = None
        if dyn_tp is not None and dyn_sl is not None:
            dynamic_won = simulate_outcome_at_prices(lower_df, entry_idx, decision, dyn_tp, dyn_sl)
            if dynamic_won is not None:
                if dynamic_won:
                    dynamic_pnl_pct = abs(dyn_tp - entry_price) / entry_price * 100
                else:
                    dynamic_pnl_pct = -abs(dyn_sl - entry_price) / entry_price * 100

        records.append({
            "fixed_won": fixed_won,
            "fixed_pnl_pct": fixed_pnl_pct,
            "dynamic_won": dynamic_won,
            "dynamic_pnl_pct": dynamic_pnl_pct,
        })

    logger.info(f"{symbol}: {len(records)} labeled setups collected")
    return records


def summarize(records: List[Dict]) -> Dict:
    fixed_total = len(records)
    fixed_wins = sum(1 for r in records if r["fixed_won"])
    fixed_win_rate = round(fixed_wins / fixed_total * 100, 2) if fixed_total else None
    fixed_avg_pnl = round(sum(r["fixed_pnl_pct"] for r in records) / fixed_total, 4) if fixed_total else None

    dyn_records = [r for r in records if r["dynamic_won"] is not None]
    dyn_total = len(dyn_records)
    dyn_wins = sum(1 for r in dyn_records if r["dynamic_won"])
    dyn_win_rate = round(dyn_wins / dyn_total * 100, 2) if dyn_total else None
    dyn_avg_pnl = round(sum(r["dynamic_pnl_pct"] for r in dyn_records) / dyn_total, 4) if dyn_total else None

    return {
        "fixed_percent_tp_sl": {
            "samples": fixed_total,
            "win_rate_pct": fixed_win_rate,
            "avg_pnl_per_trade_pct": fixed_avg_pnl,
        },
        "dynamic_analysis_tp_sl": {
            "samples": dyn_total,
            "coverage_pct": round(dyn_total / fixed_total * 100, 1) if fixed_total else None,
            "win_rate_pct": dyn_win_rate,
            "avg_pnl_per_trade_pct": dyn_avg_pnl,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest analysis-based dynamic TP/SL against the fixed-percent baseline")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbols (default: TOP_40_COINS from config.py)")
    parser.add_argument("--months", type=int, default=9,
                         help="Months of history to backtest (default: 9)")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE,
                         help=f"Output file path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else TOP_40_COINS

    client = BinanceFuturesClient(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)

    engine_config = {"TIMEFRAMES": TIMEFRAMES, "MIN_TOOLS_MATCH": MIN_TOOLS_MATCH,
                     "CALIBRATION_TABLE_FILE": "__no_such_calibration_file__.json"}
    engine = AnalysisEngine(engine_config)

    all_records: List[Dict] = []
    for symbol in symbols:
        try:
            records = run_backtest_for_symbol(engine, client, symbol, args.months)
            all_records.extend(records)
        except Exception as e:
            logger.error(f"{symbol}: backtest failed: {e}")
            continue

    if not all_records:
        logger.error("No samples collected - nothing to report. Check API access/logs above.")
        return

    summary = summarize(all_records)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "symbols_used": symbols,
        "take_profit_percent": TAKE_PROFIT_PERCENT,
        "stop_loss_percent": STOP_LOSS_PERCENT,
        "summary": summary,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("=" * 65)
    logger.info(f"Dynamic TP/SL backtest report written to {args.output}")
    logger.info(f"Total setups evaluated: {len(all_records)}")
    logger.info("-" * 65)
    fx = summary["fixed_percent_tp_sl"]
    dy = summary["dynamic_analysis_tp_sl"]
    logger.info(f"FIXED  %  TP/SL -> win-rate: {fx['win_rate_pct']}%  |  "
                f"avg PnL/trade: {fx['avg_pnl_per_trade_pct']}%  |  samples: {fx['samples']}")
    logger.info(f"DYNAMIC   TP/SL -> win-rate: {dy['win_rate_pct']}%  |  "
                f"avg PnL/trade: {dy['avg_pnl_per_trade_pct']}%  |  samples: {dy['samples']} "
                f"({dy['coverage_pct']}% of setups had a usable analysis level)")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
