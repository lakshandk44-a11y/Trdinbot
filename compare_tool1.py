#!/usr/bin/env python3
"""
Tool 1 de-duplication check (read-only analysis script - changes nothing in the bot).

Question it answers, on YOUR historical data:
    "When Tool 1 counts each underlying market fact once (TOOL1_DEDUPE_SUBCONCEPTS = True),
     how many setups does the bot lose, and were the lost ones better or worse than the
     ones it keeps?"

It replays the same walk-forward loop the calibration backtest uses (same entries logic,
same labels: take-profit hit before stop-loss, using TAKE_PROFIT_PERCENT/STOP_LOSS_PERCENT)
twice - once with the old counting, once with the de-duplicated counting - and compares:

    * how many setups each version takes,
    * the win rate of each version,
    * the win rate of the setups ONLY the old counting took (the ones that disappear)
      versus the ones both versions take (the ones that stay).
      If the disappearing ones win clearly LESS, the de-duplication is removing noise.
      If they win MORE, the old counting was catching something useful.

Setups are few (the bot's rules are strict), so the output also prints how many there are -
a difference of a handful of trades proves nothing.

Usage (on the VPS, in the background - one coin takes ~6 minutes):
  nohup python3 compare_tool1.py --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT > tool1.log 2>&1 &
  python3 compare_tool1.py                 # first 10 coins of TOP_N_COINS
  python3 compare_tool1.py --months 9 --stride 1
"""
import argparse
import json
import logging
import math
import os
import time
from typing import Dict, List

import numpy as np

import config as _cfg

logger = logging.getLogger("compare_tool1")
_HERE = os.path.dirname(os.path.abspath(__file__))


def winrate(rows: List[Dict]) -> float:
    return float(np.mean([r["won"] for r in rows]) * 100) if rows else float("nan")


def z_test(w1, n1, w2, n2) -> float:
    """one-sided: group 1 win rate > group 2 win rate"""
    if n1 < 1 or n2 < 1:
        return float("nan")
    p1, p2, p = w1 / n1, w2 / n2, (w1 + w2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 1e-12))
    return (p1 - p2) / se


def compare(legacy: List[Dict], dedup: List[Dict]) -> Dict:
    key = lambda r: (r["symbol"], int(r["ts"]), r["side"])
    L = {key(r): r for r in legacy}
    D = {key(r): r for r in dedup}
    both = [L[k] for k in L if k in D]
    only_legacy = [L[k] for k in L if k not in D]
    only_dedup = [D[k] for k in D if k not in L]
    out = {
        "legacy_setups": len(L), "dedup_setups": len(D),
        "kept_in_both": len(both), "only_legacy": len(only_legacy), "only_dedup": len(only_dedup),
        "winrate_legacy": winrate(list(L.values())), "winrate_dedup": winrate(list(D.values())),
        "winrate_kept": winrate(both), "winrate_dropped": winrate(only_legacy), "winrate_added": winrate(only_dedup),
    }
    out["drop_pct"] = (1 - len(D) / len(L)) * 100 if L else float("nan")
    if only_legacy and both:
        out["z_dropped_worse_than_kept"] = z_test(
            sum(r["won"] for r in both), len(both), sum(r["won"] for r in only_legacy), len(only_legacy))
    return out


def print_report(r: Dict, breakeven: float, title: str = "all setups that pass the tools rule"):
    L = logger.info
    L("=" * 74)
    L(f"TOOL 1 DE-DUPLICATION - effect on which setups qualify  [{title}]")
    L(f"   old counting          : {r['legacy_setups']:5d} setups, win rate {r['winrate_legacy']:.1f}%")
    L(f"   de-duplicated counting: {r['dedup_setups']:5d} setups, win rate {r['winrate_dedup']:.1f}%   "
      f"({r['drop_pct']:+.0f}% setups vs old)")
    L(f"   setups in both        : {r['kept_in_both']:5d}  win rate {r['winrate_kept']:.1f}%")
    L(f"   ONLY in the old count : {r['only_legacy']:5d}  win rate {r['winrate_dropped']:.1f}%   <- these disappear")
    if r["only_dedup"]:
        L(f"   ONLY in the new count : {r['only_dedup']:5d}  win rate {r['winrate_added']:.1f}%   (rare: an opposing fact is no longer double-counted)")
    L(f"   break-even win rate for the TP/SL used here: {breakeven:.1f}%")
    z = r.get("z_dropped_worse_than_kept")
    if z is not None and not math.isnan(z):
        if r["only_legacy"] < 30:
            L(f"   -> only {r['only_legacy']} setups disappear: far too few to say whether they were good or bad.")
        elif z >= 1.28:
            L("   -> the disappearing setups win clearly LESS than the ones that stay: the de-duplication removes noise.")
        elif z <= -1.28:
            L("   -> the disappearing setups win clearly MORE than the ones that stay: consider turning TOOL1_DEDUPE_SUBCONCEPTS off.")
        else:
            L("   -> no clear difference in quality between the disappearing and the staying setups.")
    L("Labels: take-profit before stop-loss at the fixed TP/SL percentages (the same simulation the calibration table uses).")
    L("=" * 74)


def main():
    ap = argparse.ArgumentParser(description="Compare Tool 1 old counting vs de-duplicated counting on history")
    ap.add_argument("--symbols", default=None, help="comma list (default: first 10 of TOP_N_COINS)")
    ap.add_argument("--months", type=int, default=9)
    ap.add_argument("--stride", type=int, default=0)
    ap.add_argument("--source", choices=["mainnet", "config"], default="mainnet")
    ap.add_argument("--out", default=os.path.join(_HERE, "tool1_compare_report.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    from backtest_calibration import (fetch_full_history, slice_up_to, simulate_outcome, get_smt_correlated_symbol,
                                      LOOKBACK_LIMIT, STRIDE, REQUEST_PACING_SECONDS)
    from bot_core import BinanceFuturesClient
    from analysis_engine import AnalysisEngine
    from ml_train import collect_samples_for_symbol

    cfg = {k: v for k, v in vars(_cfg).items() if k.isupper()}
    cfg["NEWS_API_KEY"] = ""  # history has no news; a real key would trigger a web request per step
    symbols = args.symbols.split(",") if args.symbols else list(cfg["TOP_N_COINS"])[:10]
    stride = args.stride or STRIDE
    client = (BinanceFuturesClient(cfg["BINANCE_API_KEY"], cfg["BINANCE_API_SECRET"], testnet=cfg.get("BINANCE_TESTNET", False))
              if args.source == "config" else BinanceFuturesClient("", "", testnet=False))
    tf = cfg["TIMEFRAMES"]
    eng_old = AnalysisEngine(dict(cfg, TOOL1_DEDUPE_SUBCONCEPTS=False))
    eng_new = AnalysisEngine(dict(cfg, TOOL1_DEDUPE_SUBCONCEPTS=True))

    corr: Dict[str, Dict] = {}
    if cfg.get("SMT_DIVERGENCE_ENABLED"):
        for cs in sorted({get_smt_correlated_symbol(s) for s in symbols}):
            logger.info(f"Pre-fetching correlated-symbol history: {cs}")
            h = fetch_full_history(client, cs, tf["higher"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, cs, tf["medium"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, cs, tf["lower"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            if h is not None and m is not None and l is not None:
                corr[cs] = {"higher": h, "medium": m, "lower": l}

    legacy_rows: List[Dict] = []
    dedup_rows: List[Dict] = []
    for n, sym in enumerate(symbols, 1):
        try:
            logger.info(f"[{n}/{len(symbols)}] {sym}: fetching history ({args.months} months)")
            h = fetch_full_history(client, sym, tf["higher"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, sym, tf["medium"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, sym, tf["lower"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            d = fetch_full_history(client, sym, "1d", args.months); time.sleep(REQUEST_PACING_SECONDS)
            if h is None or m is None or l is None:
                logger.warning(f"{sym}: incomplete history - skipped")
                continue
            c = corr.get(get_smt_correlated_symbol(sym))
            for label, eng, bucket in (("old counting", eng_old, legacy_rows), ("de-duplicated", eng_new, dedup_rows)):
                logger.info(f"   replaying {sym} with {label}...")
                rows = collect_samples_for_symbol(eng, sym, h, m, l, d, c, stride, cfg, simulate_outcome,
                                                  slice_up_to, LOOKBACK_LIMIT, near_lean=0)
                bucket.extend(rows)
            logger.info(f"   {sym}: old {sum(1 for r in legacy_rows if r['symbol'] == sym)} | "
                        f"de-duplicated {sum(1 for r in dedup_rows if r['symbol'] == sym)} setups")
        except Exception as e:
            logger.error(f"{sym}: failed ({e}) - continuing")

    if not legacy_rows:
        raise SystemExit("No setups were collected - nothing to compare.")
    rep = compare(legacy_rows, dedup_rows)
    rep_gated = compare([r for r in legacy_rows if r.get("gated")], [r for r in dedup_rows if r.get("gated")])
    tp, sl = float(cfg["TAKE_PROFIT_PERCENT"]), float(cfg["STOP_LOSS_PERCENT"])
    fee = 2.0 * float(cfg.get("TRADING_FEE_PERCENT", 0.05))
    breakeven = (sl + fee) / (tp + sl) * 100
    print_report(rep, breakeven, "all setups that pass the tools rule")
    if rep_gated["legacy_setups"]:
        print_report(rep_gated, breakeven, "setups the live bot would really take (tools rule + profit-chance gate)")
    with open(args.out, "w") as fh:
        json.dump({"all_tools_rule": rep, "live_eligible": rep_gated}, fh, indent=2, default=str)
    logger.info(f"Saved {args.out}")


if __name__ == "__main__":
    main()
