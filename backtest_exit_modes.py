#!/usr/bin/env python3
"""
Exit-mode backtest (read-only analysis script - changes nothing in the bot).

Question it answers, on YOUR historical setups:
    "With the same entries, same initial stop-loss and same take-profit, do the new
     trailing rules (wider 1h-scale distance + breakeven lock) make more or less
     money than the old trailing stop?"

How:
  * Entries = the setups the bot would really have taken (gated = 1 rows of
    ml_dataset.csv, written by ml_train.py). Each one is re-analysed at its own
    timestamp so the initial SL/TP are exactly what the live bot would choose
    (analysis-based levels, same code as backtest_dynamic_tpsl.py).
  * Each entry is then walked forward candle by candle (15m) and exited by:
        legacy      - the old trailing stop
        be-lock     - old distance, but with the breakeven lock only
        adaptive    - the current config (wider distance + breakeven lock)
        (--sweep adds more distance multipliers)
    using the SAME functions as the live bot (exit_logic.py).
  * Results are in R-multiples (profit divided by the initial risk), which is what
    matters because the bot sizes every trade to risk a fixed % of the account.
  * A paired bootstrap tells you whether a difference is real or just noise.

Honest limits (also printed): 15m candles hide what happens inside a candle (the
stop is checked BEFORE the candle's high is credited - the cautious order, applied
equally to every variant); the TP1->TP2 extension logic is not simulated; the number
of setups is small (the bot's rules are strict), so read the confidence interval,
not just the average.

Usage:
  python3 backtest_exit_modes.py                     # needs ml_dataset.csv (from ml_train.py)
  python3 backtest_exit_modes.py --sweep             # also tries other distance multipliers
  python3 backtest_exit_modes.py --symbols BTCUSDT,ETHUSDT --months 12
Run it on the VPS in the background:
  nohup python3 backtest_exit_modes.py > exit_bt.log 2>&1 &
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config as _cfg
from exit_logic import trailing_distance, trailing_candidate_sl, reward_risk

logger = logging.getLogger("backtest_exit_modes")
_HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------
# pure simulation (no network) - unit-testable
# ----------------------------------------------------------------------
def simulate_exit(lower_df: pd.DataFrame, entry_idx: int, side: str, entry: float, tp: float, sl: float,
                  atr_lower: Optional[float], atr_medium: Optional[float], cfg: Dict,
                  max_candles: int, fee_round_trip_pct: float) -> Dict:
    """Walk forward from the entry candle. Order inside a candle (cautious, same for
    every variant): 1) was the current stop touched? 2) was the take-profit touched?
    3) only then credit this candle's high/low to the trailing logic."""
    side = side.upper()
    op = lower_df["open"].to_numpy(dtype=float)
    hi = lower_df["high"].to_numpy(dtype=float)
    lo = lower_df["low"].to_numpy(dtype=float)
    cl = lower_df["close"].to_numpy(dtype=float)
    activate = float(cfg.get("TRAILING_STOP_ACTIVATE", 0.5)) / 100.0
    cur_sl = sl
    extreme = entry
    active = False
    last = min(len(lower_df), entry_idx + 1 + max_candles)
    exit_price, reason = None, "TIMEOUT"
    for j in range(entry_idx + 1, last):
        if side == "BUY":
            if lo[j] <= cur_sl:
                exit_price = min(op[j], cur_sl) if op[j] < cur_sl else cur_sl
                reason = "TRAIL" if (active and cur_sl > sl) else "SL"
                break
            if hi[j] >= tp:
                exit_price, reason = tp, "TP"
                break
            extreme = max(extreme, hi[j])
            if not active and (extreme - entry) / entry >= activate:
                active = True
            if active:
                dist = trailing_distance(entry, atr_lower, atr_medium, cfg)
                cand = trailing_candidate_sl(side, entry, extreme, dist, cfg)
                if cand > cur_sl:
                    cur_sl = cand
        else:
            if hi[j] >= cur_sl:
                exit_price = max(op[j], cur_sl) if op[j] > cur_sl else cur_sl
                reason = "TRAIL" if (active and cur_sl < sl) else "SL"
                break
            if lo[j] <= tp:
                exit_price, reason = tp, "TP"
                break
            extreme = min(extreme, lo[j])
            if not active and (entry - extreme) / entry >= activate:
                active = True
            if active:
                dist = trailing_distance(entry, atr_lower, atr_medium, cfg)
                cand = trailing_candidate_sl(side, entry, extreme, dist, cfg)
                if cand < cur_sl:
                    cur_sl = cand
    if exit_price is None:
        exit_price = cl[last - 1]
    move = (exit_price - entry) / entry * 100.0 if side == "BUY" else (entry - exit_price) / entry * 100.0
    pnl = move - fee_round_trip_pct
    risk_pct = abs(entry - sl) / entry * 100.0
    return {"pnl_pct": pnl, "r": (pnl / risk_pct) if risk_pct > 0 else 0.0, "reason": reason}


def make_variants(cfg: Dict, sweep: bool) -> Dict[str, Dict]:
    base = dict(cfg)
    v = {
        "legacy (old trailing stop)": dict(base, ADAPTIVE_EXIT_ENABLED=False),
        "be-lock only (old distance)": dict(base, ADAPTIVE_EXIT_ENABLED=True, TRAIL_MEDIUM_ATR_MULT=0.0, TRAIL_BREAKEVEN_LOCK=True),
        f"adaptive (config: {float(cfg.get('TRAIL_MEDIUM_ATR_MULT', 1.5)):g} x ATR1h + be-lock)":
            dict(base, ADAPTIVE_EXIT_ENABLED=True),
    }
    if sweep:
        for m in (1.0, 2.0, 3.0):
            v[f"adaptive {m:g} x ATR1h + be-lock"] = dict(base, ADAPTIVE_EXIT_ENABLED=True, TRAIL_MEDIUM_ATR_MULT=m, TRAIL_BREAKEVEN_LOCK=True)
        v["adaptive 1.5 x ATR1h, NO be-lock"] = dict(base, ADAPTIVE_EXIT_ENABLED=True, TRAIL_MEDIUM_ATR_MULT=1.5, TRAIL_BREAKEVEN_LOCK=False)
    return v


def run_variants(prepared: List[Dict], variants: Dict[str, Dict], max_candles: int, fee_pct: float) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {k: [] for k in variants}
    for e in prepared:
        for name, vcfg in variants.items():
            out[name].append(simulate_exit(e["lower_df"], e["entry_idx"], e["side"], e["entry"], e["tp"], e["sl"],
                                           e["atr_lower"], e["atr_medium"], vcfg, max_candles, fee_pct))
    return out


def summarize(rows: List[Dict]) -> Dict:
    pnl = np.array([r["pnl_pct"] for r in rows], dtype=float)
    rr = np.array([r["r"] for r in rows], dtype=float)
    w, l = pnl[pnl > 0], pnl[pnl <= 0]
    reasons: Dict[str, int] = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {
        "n": int(len(pnl)), "win_rate": float((pnl > 0).mean() * 100) if len(pnl) else float("nan"),
        "avg_win_pct": float(w.mean()) if len(w) else 0.0, "avg_loss_pct": float(l.mean()) if len(l) else 0.0,
        "payoff": float(abs(w.mean() / l.mean())) if len(w) and len(l) and l.mean() != 0 else float("nan"),
        "expectancy_pct": float(pnl.mean()) if len(pnl) else float("nan"),
        "avg_r": float(rr.mean()) if len(rr) else float("nan"), "total_r": float(rr.sum()),
        "reasons": reasons,
    }


def paired_bootstrap(a: np.ndarray, b: np.ndarray, n_boot: int = 4000, seed: int = 7):
    """mean(b - a) with a 95% bootstrap interval (paired by setup)."""
    d = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    if len(d) < 5:
        return float(d.mean()) if len(d) else float("nan"), None, None
    rng = np.random.RandomState(seed)
    means = np.array([d[rng.randint(0, len(d), len(d))].mean() for _ in range(n_boot)])
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def report(results: Dict[str, List[Dict]]) -> Dict:
    names = list(results)
    summ = {k: summarize(v) for k, v in results.items()}
    L = logger.info
    L("=" * 78)
    L(f"EXIT-MODE BACKTEST - {summ[names[0]]['n']} setups, same entries / same initial SL & TP for every row")
    L(f"{'variant':52s} win%   avgWin  avgLoss payoff  avgR   totalR")
    for k in names:
        s = summ[k]
        L(f"{k:52s} {s['win_rate']:4.0f}  {s['avg_win_pct']:+6.2f}% {s['avg_loss_pct']:+6.2f}%  {s['payoff']:5.2f} {s['avg_r']:+6.3f} {s['total_r']:+7.1f}")
    L("exit reasons: " + " | ".join(f"{k[:14]}: " + ", ".join(f"{a} {b}" for a, b in summ[k]['reasons'].items()) for k in names))
    base = np.array([r["r"] for r in results[names[0]]])
    diffs = {}
    L("-" * 78)
    L(f"DIFFERENCE vs '{names[0]}' in average R per trade (paired bootstrap, 95% interval):")
    for k in names[1:]:
        m, lo, hi = paired_bootstrap(base, np.array([r["r"] for r in results[k]]))
        diffs[k] = {"mean_r_diff": m, "ci_low": lo, "ci_high": hi}
        if lo is None:
            verdict = "too few setups to judge"
        elif lo > 0:
            verdict = "BETTER (interval above zero)"
        elif hi < 0:
            verdict = "WORSE (interval below zero)"
        else:
            verdict = "no clear difference (interval includes zero)"
        L(f"   {k:52s} {m:+.3f} R  [{('%.3f' % lo) if lo is not None else 'n/a'} .. {('%.3f' % hi) if hi is not None else 'n/a'}]  -> {verdict}")
    L("-" * 78)
    L("How to read: R = profit divided by the trade's initial risk (the bot risks a fixed % of the account per trade, so")
    L("+0.10 R more per trade is +0.10 x that % of the account more per trade). 'no clear difference' is a normal outcome")
    L("with a small number of setups - then keep the simpler / safer variant and re-run when more setups exist.")
    L("Limits: 15m candles hide intra-candle order (stop checked first - cautious, equal for all); TP1->TP2 extension is")
    L("not simulated; trades that never resolve within --max-candles are closed at the last close.")
    L("=" * 78)
    return {"summary": summ, "differences": diffs}


def rr_preview(prepared: List[Dict], results: Dict[str, List[Dict]], cfg: Dict,
               thresholds=(0.2, 0.3, 0.4, 0.5, 0.7, 1.0)) -> Dict:
    """What would the reward/risk guard (MIN_REWARD_RISK) have done to THESE setups?
    For every threshold: how many setups are kept / skipped and how the skipped ones performed
    (average R under the variant named in the heading). Uses the same reward_risk() as the live bot."""
    names = list(results)
    name = names[2] if len(names) > 2 else names[-1]  # the config's adaptive variant (3rd; sweep variants come after it)
    rr = np.array([reward_risk(e["side"], e["entry"], e["tp"], e["sl"], cfg)["rr"] for e in prepared], dtype=float)
    r = np.array([x["r"] for x in results[name]], dtype=float)
    L = logger.info
    L("=" * 78)
    L(f"REWARD/RISK GUARD PREVIEW  (R:R = distance to first take-profit / distance to stop-loss; results: '{name}')")
    q = np.percentile(rr[np.isfinite(rr)], [10, 25, 50, 75, 90]) if np.isfinite(rr).any() else [float('nan')] * 5
    L(f"   R:R of these {len(rr)} setups  -  10%: {q[0]:.2f}  25%: {q[1]:.2f}  median: {q[2]:.2f}  75%: {q[3]:.2f}  90%: {q[4]:.2f}")
    L(f"   {'min R:R':8s} {'kept':>5s} {'skipped':>8s}   avg R of kept   avg R of skipped")
    rows = []
    for t in thresholds:
        keep = rr >= t
        kept_r = float(r[keep].mean()) if keep.any() else float("nan")
        skip_r = float(r[~keep].mean()) if (~keep).any() else float("nan")
        rows.append({"min_rr": t, "kept": int(keep.sum()), "skipped": int((~keep).sum()), "avg_r_kept": kept_r, "avg_r_skipped": skip_r})
        L(f"   {t:<8.2f} {int(keep.sum()):5d} {int((~keep).sum()):8d}   {kept_r:+13.3f}   {skip_r:+16.3f}")
    L("   Reading it: if the SKIPPED group has a clearly negative average R, skipping them helps. If a threshold removes most")
    L("   setups, it is too strict for your bot - pick the highest value that still keeps enough trades. A handful of setups")
    L("   proves nothing; the live bot's own results (Telegram) are the final judge.")
    L("=" * 78)
    return {"variant": name, "rr_quantiles": [float(x) for x in q], "thresholds": rows}


# ----------------------------------------------------------------------
# data side (network)
# ----------------------------------------------------------------------
def prepare_entries(args, cfg: Dict) -> List[Dict]:
    from backtest_calibration import (fetch_full_history, slice_up_to, get_smt_correlated_symbol,
                                      LOOKBACK_LIMIT, REQUEST_PACING_SECONDS)
    from backtest_dynamic_tpsl import get_dynamic_tp_sl
    from bot_core import BinanceFuturesClient
    from analysis_engine import AnalysisEngine

    if not os.path.exists(args.dataset):
        raise SystemExit(f"{args.dataset} not found. Run ml_train.py first (it writes the list of setups this script re-uses).")
    ds = pd.read_csv(args.dataset, usecols=["ts", "symbol", "side", "gated", "source"])
    ds = ds[(ds["gated"] == 1) & (ds["source"] == "backtest")].drop_duplicates(subset=["ts", "symbol", "side"])
    if args.symbols:
        ds = ds[ds["symbol"].isin(args.symbols.split(","))]
    if ds.empty:
        raise SystemExit("No gated (live-eligible) setups in the dataset for the chosen symbols.")
    logger.info(f"{len(ds)} setups across {ds['symbol'].nunique()} coins")

    client = (BinanceFuturesClient(cfg["BINANCE_API_KEY"], cfg["BINANCE_API_SECRET"], testnet=cfg.get("BINANCE_TESTNET", False))
              if args.source == "config" else BinanceFuturesClient("", "", testnet=False))
    ecfg = dict(cfg)
    ecfg["NEWS_API_KEY"] = ""  # a real key would make the engine call NewsAPI once per replayed step
    engine = AnalysisEngine(ecfg)
    tf = cfg["TIMEFRAMES"]
    corr_cache: Dict[str, Optional[Dict]] = {}

    def corr_for(sym):
        cs = get_smt_correlated_symbol(sym)
        if not cfg.get("SMT_DIVERGENCE_ENABLED"):
            return None
        if cs not in corr_cache:
            h = fetch_full_history(client, cs, tf["higher"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, cs, tf["medium"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, cs, tf["lower"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            corr_cache[cs] = {"higher": h, "medium": m, "lower": l} if (h is not None and m is not None and l is not None) else None
        return corr_cache[cs]

    prepared: List[Dict] = []
    for n, (sym, grp) in enumerate(ds.groupby("symbol"), 1):
        try:
            logger.info(f"[{n}/{ds['symbol'].nunique()}] {sym}: fetching history ({len(grp)} setups)")
            h = fetch_full_history(client, sym, tf["higher"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, sym, tf["medium"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, sym, tf["lower"], args.months); time.sleep(REQUEST_PACING_SECONDS)
            d = fetch_full_history(client, sym, "1d", args.months); time.sleep(REQUEST_PACING_SECONDS)
            if h is None or m is None or l is None:
                logger.warning(f"{sym}: incomplete history - skipped")
                continue
            corr = corr_for(sym)
            for _, row in grp.iterrows():
                ts, side = int(row["ts"]), str(row["side"])
                hs, ms_, ls = (slice_up_to(h, ts, LOOKBACK_LIMIT["higher"]), slice_up_to(m, ts, LOOKBACK_LIMIT["medium"]),
                               slice_up_to(l, ts, LOOKBACK_LIMIT["lower"]))
                if len(hs) < 50 or len(ms_) < 50 or len(ls) < 50:
                    continue
                mtf = {"higher": hs, "medium": ms_, "lower": ls}
                if corr:
                    mtf["correlated"] = {k: slice_up_to(corr[k], ts, LOOKBACK_LIMIT[k]) for k in ("higher", "medium", "lower")}
                if d is not None:
                    mtf["daily"] = slice_up_to(d, ts, cfg["DAILY_HISTORY_CANDLES"])
                try:
                    res = engine.multi_timeframe_analysis(mtf)
                except Exception as e:
                    logger.debug(f"{sym}: analysis failed at {ts}: {e}")
                    continue
                idx = l[l["timestamp"] < ts].index
                if len(idx) == 0:
                    continue
                entry_idx = int(idx[-1])
                entry = float(l["close"].iloc[entry_idx])
                tp, sl = get_dynamic_tp_sl(side, entry, res)
                if tp is None or sl is None:  # live bot falls back to the fixed percentages too
                    tpp, slp = cfg["TAKE_PROFIT_PERCENT"] / 100.0, cfg["STOP_LOSS_PERCENT"] / 100.0
                    tp = entry * (1 + tpp) if side == "BUY" else entry * (1 - tpp)
                    sl = entry * (1 - slp) if side == "BUY" else entry * (1 + slp)
                a_l = (res.get("lower", {}).get("fvg", {}) or {}).get("atr") or (res.get("medium", {}).get("fvg", {}) or {}).get("atr")
                a_m = (res.get("medium", {}).get("fvg", {}) or {}).get("atr")
                prepared.append({"symbol": sym, "side": side, "lower_df": l, "entry_idx": entry_idx, "entry": entry,
                                 "tp": tp, "sl": sl, "atr_lower": a_l, "atr_medium": a_m})
        except Exception as e:
            logger.error(f"{sym}: failed ({e}) - continuing")
    return prepared


def main():
    ap = argparse.ArgumentParser(description="Compare the old trailing stop with the adaptive exit on historical setups")
    ap.add_argument("--dataset", default=os.path.join(_HERE, "ml_dataset.csv"))
    ap.add_argument("--symbols", default=None, help="comma list (default: every coin in the dataset)")
    ap.add_argument("--months", type=int, default=15, help="history to fetch (must cover the dataset's period)")
    ap.add_argument("--source", choices=["mainnet", "config"], default="mainnet")
    ap.add_argument("--max-candles", type=int, default=600, help="15m candles to follow a trade (600 = 6 days)")
    ap.add_argument("--sweep", action="store_true", help="also try other distance multipliers / no breakeven lock")
    ap.add_argument("--out", default=os.path.join(_HERE, "exit_mode_backtest_report.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    cfg = {k: v for k, v in vars(_cfg).items() if k.isupper()}
    prepared = prepare_entries(args, cfg)
    if len(prepared) < 5:
        raise SystemExit(f"Only {len(prepared)} usable setups - too few to compare anything.")
    fee = 2.0 * float(cfg.get("TRADING_FEE_PERCENT", 0.05))
    results = run_variants(prepared, make_variants(cfg, args.sweep), args.max_candles, fee)
    rep = report(results)
    rep["rr_preview"] = rr_preview(prepared, results, cfg)
    with open(args.out, "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    logger.info(f"Saved {args.out}")


if __name__ == "__main__":
    main()
