#!/usr/bin/env python3
"""
ML Filter - trainer. Run this on the VPS (NOT inside the bot process).

What it does
  1. COLLECT: pages Binance history (public candles, no API key needed), replays
     the bot's own analysis engine over it exactly like backtest_calibration.py,
     and for every BUY/SELL setup stores (a) the feature numbers the live bot
     will also see, and (b) the outcome (TP hit before SL = win) using the bot's
     own TAKE_PROFIT_PERCENT / STOP_LOSS_PERCENT. Saved per coin to a CSV, so a
     dropped SSH session / reboot loses nothing (use --resume).
  2. TRAIN: splits the samples by TIME (oldest 60% train, next 20% validation,
     newest 20% test - with a gap so no outcome window overlaps), trains a few
     models (logistic regression, gradient boosting, small neural network),
     picks the best on validation, chooses the win-probability threshold, and
     measures on the untouched test period whether filtering really helps.
  3. SAVES ml_model.pkl (atomic) - the running bot hot-reloads it, no restart.
     If the test period shows no real improvement the model is saved flagged
     "unvalidated": it advises on Telegram but never blocks trades.

Usage
  python3 ml_train.py                       # collect (or reuse dataset) + train
  python3 ml_train.py --collect-only
  python3 ml_train.py --train-only          # re-train from the saved dataset
  python3 ml_train.py --resume              # continue an interrupted collection
  python3 ml_train.py --symbols BTCUSDT,ETHUSDT --months 6
  python3 ml_train.py --use-live            # also learn from the bot's own closed trades
  python3 ml_train.py --model gbm|mlp|logreg|auto

Run it in the background so closing your phone SSH app is harmless:
  nohup python3 ml_train.py --resume > ml_train.log 2>&1 &
  tail -f ml_train.log
or  pm2 start ml_train.py --interpreter python3 --no-autorestart --name ml-train

Needs: pip install scikit-learn joblib   (numpy/pandas already installed for the bot)

Why near-signals (--near-lean, default 4): the bot's rules are strict (all 3 timeframes
need MIN_TOOLS_MATCH tools, each with MIN_SUBCONCEPTS_PER_TOOL sub-concepts), so real
trading setups are only a handful per coin per 9 months - too few to learn from. The model
therefore also learns from setups where the timeframes lean clearly one way, and the
"gated" rows (what the live bot really would take) are reported separately.

Honest limits (also printed in the report)
  * Labels come from a fixed TP/SL simulation, while the live bot uses dynamic
    TP/SL, TP1->TP2 extension and trailing - so history is an approximation.
    --use-live blends in real outcomes once enough live trades exist.
  * Funding-rate / open-interest confluence and news sentiment are not in the
    history replay (they only exist live) - they are left out of the features.
  * Hourly setups on the same coin overlap heavily, so the effective number of
    independent samples is smaller than the row count.
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config as _cfg
from ml_filter import (FEATURE_NAMES, extract_features, read_records, calibration_signature, NNEnsemble)

logger = logging.getLogger("ml_train")

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATASET = os.path.join(_HERE, "ml_dataset.csv")
DEFAULT_MODEL = os.path.join(_HERE, "ml_model.pkl")
DEFAULT_REPORT = os.path.join(_HERE, "ml_model_report.json")
META_COLS = ["ts", "symbol", "side", "won", "gated", "source"]


# ======================================================================
# 1. COLLECTION
# ======================================================================
def collect_samples_for_symbol(engine, symbol: str, higher_df, medium_df, lower_df, daily_df,
                               corr_hist: Optional[Dict], stride: int, cfg: Dict,
                               simulate_outcome, slice_up_to, lookback_limit: Dict,
                               progress_every: int = 500,
                               near_lean: int = 0, near_every: int = 3) -> List[Dict]:
    """Pure function (no network): replays the engine over the given candles.
    Mirrors backtest_calibration.run_calibration_for_symbol step for step.

    near_lean > 0 ALSO records "near-signal" setups: moments the bot would NOT
    trade (decision HOLD) but where the 3 timeframes lean clearly one way
    (|bullish tools - bearish tools| summed over 4h/1h/15m >= near_lean).
    With the bot's strict rules (all 3 timeframes need MIN_TOOLS_MATCH tools,
    each with MIN_SUBCONCEPTS_PER_TOOL sub-concepts) real trading setups are very
    rare - a few per coin per 9 months - far too few for any model to learn from.
    Near-signals give thousands of rows; every row is still labelled by what
    price really did afterwards. Rows the live bot would genuinely have taken are
    flagged gated=1 so they can be reported separately. Near-signal rows are
    sampled every `near_every`-th step only (neighbouring hours are near copies)."""
    tp_pct = cfg["TAKE_PROFIT_PERCENT"] / 100.0
    sl_pct = cfg["STOP_LOSS_PERCENT"] / 100.0
    min_tools = cfg["MIN_TOOLS_MATCH"]
    min_pc = cfg["MIN_PROFIT_CHANCE"]
    warmup = 60
    rows: List[Dict] = []
    total_steps = max(1, (len(medium_df) - warmup) // max(1, stride))
    t0 = time.time()
    done = 0

    for i in range(warmup, len(medium_df), stride):
        done += 1
        if progress_every and done % progress_every == 0:
            el = time.time() - t0
            eta = el / done * (total_steps - done)
            logger.info(f"  {symbol}: {done}/{total_steps} steps, {len(rows)} samples, ETA {eta/60:.1f} min")

        current_time = int(medium_df["timestamp"].iloc[i])
        higher_slice = slice_up_to(higher_df, current_time, lookback_limit["higher"])
        medium_slice = slice_up_to(medium_df, current_time, lookback_limit["medium"])
        lower_slice = slice_up_to(lower_df, current_time, lookback_limit["lower"])
        if len(higher_slice) < 50 or len(medium_slice) < 50 or len(lower_slice) < 50:
            continue

        try:
            mtf_input = {"higher": higher_slice, "medium": medium_slice, "lower": lower_slice}
            if corr_hist:
                mtf_input["correlated"] = {
                    "higher": slice_up_to(corr_hist["higher"], current_time, lookback_limit["higher"]),
                    "medium": slice_up_to(corr_hist["medium"], current_time, lookback_limit["medium"]),
                    "lower": slice_up_to(corr_hist["lower"], current_time, lookback_limit["lower"]),
                }
            if daily_df is not None:
                mtf_input["daily"] = slice_up_to(daily_df, current_time, cfg["DAILY_HISTORY_CANDLES"])
            result = engine.multi_timeframe_analysis(mtf_input)
        except Exception as e:
            logger.debug(f"{symbol}: analysis error at {current_time}: {e}")
            continue

        final = result.get("final_signal", {})
        decision = final.get("decision", "HOLD")
        eligible = decision in ("BUY", "SELL") and final.get("tools_agreeing", 0) >= min_tools
        if eligible:
            side = decision
        elif near_lean > 0 and (done % max(1, near_every) == 0):
            lean = float(final.get("total_bullish_tools", 0)) - float(final.get("total_bearish_tools", 0))
            if lean >= near_lean:
                side = "BUY"
            elif lean <= -near_lean:
                side = "SELL"
            else:
                continue
        else:
            continue

        lower_full_idx = lower_df[lower_df["timestamp"] < current_time].index
        if len(lower_full_idx) == 0:
            continue
        entry_idx = int(lower_full_idx[-1])
        entry_price = float(lower_df["close"].iloc[entry_idx])
        if side == "BUY":
            tp_price, sl_price = entry_price * (1 + tp_pct), entry_price * (1 - sl_pct)
        else:
            tp_price, sl_price = entry_price * (1 - tp_pct), entry_price * (1 + sl_pct)

        won = simulate_outcome(lower_df, entry_idx, side, tp_price, sl_price)
        if won is None:
            continue  # unresolved - discarded, never guessed

        feats = extract_features(result, {"higher": higher_slice, "medium": medium_slice, "lower": lower_slice},
                                 side, ts_ms=current_time, drop_forming=False)
        if feats is None:
            continue
        row = {"ts": current_time, "symbol": symbol, "side": side, "won": int(bool(won)),
               "gated": int(eligible and float(final.get("profit_chance", 0.0)) >= min_pc),
               "source": "backtest"}
        row.update(feats)
        rows.append(row)
    return rows


def _append_dataset(path: str, rows: List[Dict]):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=META_COLS + FEATURE_NAMES)
    header = not os.path.exists(path)
    df.to_csv(path, mode="a", header=header, index=False)


def collect(args) -> int:
    from backtest_calibration import (fetch_full_history, slice_up_to, simulate_outcome,
                                      get_smt_correlated_symbol, LOOKBACK_LIMIT, STRIDE,
                                      REQUEST_PACING_SECONDS)
    from bot_core import BinanceFuturesClient
    from analysis_engine import AnalysisEngine

    cfg = {k: v for k, v in vars(_cfg).items() if k.isupper()}
    symbols = args.symbols.split(",") if args.symbols else list(cfg["TOP_N_COINS"])
    stride = args.stride or STRIDE

    if args.refresh and os.path.exists(args.dataset):
        os.remove(args.dataset)
        logger.info("Old dataset removed (--refresh).")

    done_symbols = set()
    if args.resume and os.path.exists(args.dataset):
        try:
            done_symbols = set(pd.read_csv(args.dataset, usecols=["symbol"])["symbol"].unique())
            logger.info(f"Resuming: {len(done_symbols)} coins already in {args.dataset}")
        except Exception as e:
            logger.warning(f"Could not read existing dataset for --resume ({e}) - collecting everything.")
    elif os.path.exists(args.dataset):
        logger.info(f"{args.dataset} already exists - new coins will be APPENDED. "
                    f"Use --refresh to start over or --resume to skip coins already collected.")

    if args.source == "config":
        client = BinanceFuturesClient(cfg["BINANCE_API_KEY"], cfg["BINANCE_API_SECRET"],
                                      testnet=cfg.get("BINANCE_TESTNET", False))
    else:  # mainnet public candles: deep, realistic history; works for testnet trading too
        client = BinanceFuturesClient("", "", testnet=False)

    # Same engine settings the live bot uses (so features match live) EXCEPT the
    # news key: with a real key the engine would call NewsAPI once per replayed
    # step (thousands of requests) - history has no news anyway.
    engine_cfg = dict(cfg)
    engine_cfg["NEWS_API_KEY"] = ""
    engine = AnalysisEngine(engine_cfg)

    todo = [s for s in symbols if s not in done_symbols]
    if not todo:
        logger.info("Every requested coin is already in the dataset - nothing to collect.")
        return 0

    corr_histories: Dict[str, Dict] = {}
    if cfg.get("SMT_DIVERGENCE_ENABLED"):
        needed = {get_smt_correlated_symbol(s) for s in todo}
        for cs in sorted(needed):
            logger.info(f"Pre-fetching correlated-symbol history: {cs}")
            h = fetch_full_history(client, cs, cfg["TIMEFRAMES"]["higher"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, cs, cfg["TIMEFRAMES"]["medium"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, cs, cfg["TIMEFRAMES"]["lower"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            if h is not None and m is not None and l is not None:
                corr_histories[cs] = {"higher": h, "medium": m, "lower": l}

    total_new = 0
    for n, symbol in enumerate(symbols, 1):
        if symbol in done_symbols:
            logger.info(f"[{n}/{len(symbols)}] {symbol}: already collected - skipping")
            continue
        try:
            logger.info(f"[{n}/{len(symbols)}] {symbol}: fetching history ({args.months} months)...")
            h = fetch_full_history(client, symbol, cfg["TIMEFRAMES"]["higher"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, symbol, cfg["TIMEFRAMES"]["medium"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, symbol, cfg["TIMEFRAMES"]["lower"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            if h is None or m is None or l is None:
                logger.warning(f"{symbol}: incomplete history - skipped")
                continue
            d = fetch_full_history(client, symbol, "1d", args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            rows = collect_samples_for_symbol(
                engine, symbol, h, m, l, d, corr_histories.get(get_smt_correlated_symbol(symbol)),
                stride, cfg, simulate_outcome, slice_up_to, LOOKBACK_LIMIT,
                near_lean=args.near_lean, near_every=args.near_every)
            _append_dataset(args.dataset, rows)
            total_new += len(rows)
            logger.info(f"[{n}/{len(symbols)}] {symbol}: {len(rows)} samples saved")
        except Exception as e:
            logger.error(f"{symbol}: collection failed ({e}) - continuing with the next coin")
            continue
    logger.info(f"Collection finished: {total_new} new samples -> {args.dataset}")
    return total_new


# ======================================================================
# 2. DATA LOADING (+ optional live trades)
# ======================================================================
def load_live_rows(cfg: Dict) -> pd.DataFrame:
    """Opened-trade feature rows joined with their real closed outcome."""
    from ml_filter import MLFilter
    log_path = cfg.get("ML_DECISION_LOG_FILE") or os.path.join(_HERE, "ml_decisions.jsonl")
    state_path = cfg.get("TRADE_STATE_FILE")
    try:  # make sure outcomes of trades closed by Binance's own SL/TP are in the log
        if state_path and os.path.exists(state_path):
            with open(state_path, "r") as fh:
                hist = json.load(fh).get("trade_history", [])
            f = MLFilter.__new__(MLFilter)
            import threading
            f.config, f.log_path, f._lock = cfg, log_path, threading.Lock()
            f._closed_ids = None
            f.sync_outcomes(hist)
    except Exception as e:
        logger.warning(f"Could not sync live outcomes from trade state: {e}")

    recs = read_records(log_path)
    opened = {r["id"]: r for r in recs if r.get("type") == "opened" and r.get("features")}
    rows = []
    for r in recs:
        if r.get("type") != "closed" or r.get("id") not in opened:
            continue
        o = opened[r["id"]]
        try:
            ts = int(datetime.fromisoformat(o["ts"]).timestamp() * 1000)
        except Exception:
            continue
        row = {"ts": ts, "symbol": o.get("symbol"), "side": o.get("side"), "won": int(bool(r.get("win"))),
               "gated": 1, "source": "live"}
        for k in FEATURE_NAMES:
            v = o["features"].get(k)
            row[k] = np.nan if v is None else v
        rows.append(row)
    return pd.DataFrame(rows, columns=META_COLS + FEATURE_NAMES)


def load_dataset(args, cfg: Dict) -> pd.DataFrame:
    if not os.path.exists(args.dataset):
        raise SystemExit(f"No dataset at {args.dataset}. Run without --train-only first (collection).")
    df = pd.read_csv(args.dataset)
    for c in FEATURE_NAMES:
        if c not in df.columns:
            df[c] = np.nan
    df = df.dropna(subset=["ts", "won"]).drop_duplicates(subset=["ts", "symbol", "side"])
    if args.use_live:
        live = load_live_rows(cfg)
        logger.info(f"Live trades with verdict+outcome available: {len(live)}")
        if len(live):
            df = pd.concat([df, live], ignore_index=True)
    df["ts"] = df["ts"].astype("int64")
    df["won"] = df["won"].astype(int)
    df["gated"] = df["gated"].astype(int)
    return df.sort_values("ts").reset_index(drop=True)


# ======================================================================
# 3. TRAINING / VALIDATION
# ======================================================================
def make_models(n_members: int = 7):
    from sklearn.pipeline import Pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    return {
        "logreg": Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler()),
                            ("m", LogisticRegression(C=0.5, max_iter=2000))]),
        "gbm": HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=120,
                                              l2_regularization=1.0, min_samples_leaf=40, random_state=42),
        # real neural network: ensemble of small MLPs, early-stopped on time-ordered validation data
        "nn": NNEnsemble(n_members=n_members),
    }


def economics(cfg: Dict):
    tp, sl = float(cfg["TAKE_PROFIT_PERCENT"]), float(cfg["STOP_LOSS_PERCENT"])
    fee = 2.0 * float(cfg.get("TRADING_FEE_PERCENT", 0.05))
    ev_win, ev_loss = tp - fee, -(sl + fee)
    breakeven = (sl + fee) / (tp + sl)
    return ev_win, ev_loss, breakeven


def ev_per_trade(y: np.ndarray, ev_win: float, ev_loss: float) -> float:
    return float(np.mean(np.where(y == 1, ev_win, ev_loss))) if len(y) else float("nan")


def auc(y, p) -> Optional[float]:
    from sklearn.metrics import roc_auc_score
    if len(y) < 20 or len(set(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def choose_threshold(p: np.ndarray, y: np.ndarray, ev_win: float, ev_loss: float, min_keep_frac: float = 0.15):
    """Threshold maximising total expected profit on validation, while keeping
    at least min_keep_frac of trades (stops it picking a tiny lucky subset)."""
    n = len(p)
    if n < 40:
        return None
    best_thr, best_total = None, -1e18
    floor = max(30, int(n * min_keep_frac))
    for thr in np.arange(0.20, 0.86, 0.01):
        keep = p >= thr
        k = int(keep.sum())
        if k < floor:
            continue
        total = float(np.sum(np.where(y[keep] == 1, ev_win, ev_loss)))
        if total > best_total + 1e-9:
            best_total, best_thr = total, float(round(thr, 2))
    return best_thr


def z_two_prop(w1, n1, w2, n2) -> float:
    """one-sided z for 'group1 win rate > group2 win rate'"""
    if n1 < 1 or n2 < 1:
        return float("nan")
    p1, p2 = w1 / n1, w2 / n2
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 1e-12))
    return (p1 - p2) / se


def temporal_split(df: pd.DataFrame, embargo_ms: int):
    ts = df["ts"].to_numpy()
    t1 = np.quantile(ts, 0.60)
    t2 = np.quantile(ts, 0.80)
    train = df[ts < t1 - embargo_ms]
    val = df[(ts >= t1) & (ts < t2 - embargo_ms)]
    test = df[ts >= t2]
    return train, val, test, t1, t2


NN_GRID = [((32, 16), 1e-2), ((32, 16), 1e-1), ((64, 32), 1e-2), ((64, 32), 1e-1)]


def tune_nn(Xtr, ytr, Xes, yes, Xva, yva, seed: int = 42):
    """Pick the network size / weight-decay with the lowest validation log-loss.
    Small grid on purpose (a big search would just overfit the validation period)."""
    from sklearn.metrics import log_loss
    rows = []
    for hidden, alpha in NN_GRID:
        t0 = time.time()
        m = NNEnsemble(n_members=3, hidden=hidden, alpha=alpha, seed=seed).fit(Xtr, ytr, Xes, yes)
        ll = float(log_loss(yva, m.predict_proba(Xva)[:, 1], labels=[0, 1]))
        rows.append({"hidden": list(hidden), "alpha": alpha, "val_logloss": round(ll, 5)})
        logger.info(f"   tuning: layers {hidden} weight-decay {alpha:g} -> validation log-loss {ll:.4f} ({time.time()-t0:.0f}s)")
    best = min(rows, key=lambda r: r["val_logloss"])
    return tuple(best["hidden"]), best["alpha"], rows


def walk_forward(df: pd.DataFrame, use_features: List[str], embargo_ms: int, nn_params: Dict,
                 n_folds: int, seed: int = 42) -> List[Dict]:
    """Does the network keep working in DIFFERENT periods? For each later time block, a fresh
    network is trained ONLY on the data before it (with an embargo gap) and scored on that block.
    One lucky test period can fake a good result; this checks several."""
    d = df.sort_values("ts").reset_index(drop=True)
    ts = d["ts"].to_numpy()
    edges = np.linspace(0.40, 1.0, n_folds + 1)
    out: List[Dict] = []
    Xall = d[use_features].to_numpy(dtype=float)
    yall = d["won"].to_numpy()
    for i in range(n_folds):
        lo = np.quantile(ts, edges[i])
        hi = np.quantile(ts, edges[i + 1]) if i < n_folds - 1 else ts.max() + 1
        te_m = (ts >= lo) & (ts < hi)
        pre = ts < lo - embargo_ms
        if pre.sum() < 400 or te_m.sum() < 100:
            continue
        tsa = ts[pre]
        cut = np.quantile(tsa, 0.85)
        fit_m = pre & (ts < cut - embargo_ms)
        es_m = pre & (ts >= cut)
        if fit_m.sum() < 300 or es_m.sum() < 50:
            continue
        t0 = time.time()
        m = NNEnsemble(n_members=3, seed=seed, **nn_params).fit(Xall[fit_m], yall[fit_m], Xall[es_m], yall[es_m])
        p = m.predict_proba(Xall[te_m])[:, 1]
        yt = yall[te_m]
        top = p >= np.median(p)
        a = auc(yt, p)
        rec = {"fold": len(out) + 1, "test_rows": int(te_m.sum()), "auc": None if a is None else round(a, 4),
               "top_half_winrate": round(float(yt[top].mean() * 100), 1),
               "bottom_half_winrate": round(float(yt[~top].mean() * 100), 1)}
        out.append(rec)
        logger.info(f"   walk-forward period {rec['fold']}: AUC {rec['auc']}  top-half win {rec['top_half_winrate']}%  "
                    f"bottom-half win {rec['bottom_half_winrate']}%  ({rec['test_rows']} setups, {time.time()-t0:.0f}s)")
    return out


def permutation_importance(model, X: np.ndarray, y: np.ndarray, names: List[str], repeats: int = 3,
                           max_rows: int = 4000, seed: int = 0):
    """How much AUC is lost when one feature is shuffled = how much the model relies on it."""
    rng = np.random.RandomState(seed)
    if len(X) > max_rows:
        idx = rng.choice(len(X), max_rows, replace=False)
        X, y = X[idx], y[idx]
    ci = list(model.classes_).index(1)
    base = auc(y, model.predict_proba(X)[:, ci])
    if base is None:
        return None, []
    out = []
    for j, name in enumerate(names):
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[:, j] = rng.permutation(Xp[:, j])
            a = auc(y, model.predict_proba(Xp)[:, ci])
            drops.append(base - (a if a is not None else base))
        out.append((name, float(np.mean(drops))))
    out.sort(key=lambda t: -t[1])
    return base, out


def train(args, cfg: Dict) -> Optional[Dict]:
    import joblib
    import sklearn
    from backtest_calibration import FORWARD_LOOKAHEAD_CANDLES

    df = load_dataset(args, cfg)
    logger.info(f"Dataset: {len(df)} samples, {df['symbol'].nunique()} coins, "
                f"win rate {df['won'].mean()*100:.1f}%, gated (live-eligible) {int(df['gated'].sum())}")
    if len(df) < args.min_samples:
        logger.error(f"Only {len(df)} samples (< {args.min_samples}). Collect more history first "
                     f"(more --months / coins). Nothing trained.")
        return None

    ev_win, ev_loss, breakeven = economics(cfg)
    embargo_ms = FORWARD_LOOKAHEAD_CANDLES * 15 * 60 * 1000  # label window of one setup
    tr, va, te, t1, t2 = temporal_split(df, embargo_ms)
    logger.info(f"Split by time -> train {len(tr)} | validation {len(va)} | test {len(te)} "
                f"(embargo {embargo_ms/3.6e6:.0f}h between sets)")
    if min(len(tr), len(va), len(te)) < 30:
        logger.error("A split is too small to validate anything reliably. Collect more data.")
        return None

    # Funding-rate / open-interest / news adjustments exist ONLY live: they shift the
    # profit-chance ("pc") scores the bot sees live, but history replay has none of them.
    # So by default the model does not use the 4 score features (tool counts, ADX,
    # regime, momentum, volatility, time of day remain). Live-eligible trades all sit in
    # a narrow high-score band anyway, so little is lost. --keep-score-features restores them.
    SCORE_FEATURES = ["higher_pc", "medium_pc", "lower_pc", "final_pc"]
    use_features = list(FEATURE_NAMES) if args.keep_score_features else [f for f in FEATURE_NAMES if f not in SCORE_FEATURES]
    logger.info(f"Features used: {len(use_features)} of {len(FEATURE_NAMES)}"
                + ("" if args.keep_score_features else " (profit-chance score features excluded: live-only inputs differ from history)"))
    # The validation period is cut in two (in time order): the FIRST half is only used by the
    # neural networks to decide when to stop training; the SECOND half chooses the winning
    # model and the threshold for EVERY model. So no model gets an unfair look at the data
    # it is later compared on, and the test period is never touched until the final report.
    half = len(va) // 2
    va_es, va = va.iloc[:half], va.iloc[half:]
    if min(len(va_es), len(va)) < 30:
        logger.error("Validation period too small to split. Collect more data.")
        return None
    X = lambda d: d[use_features].to_numpy(dtype=float)
    Xes, yes = X(va_es), va_es["won"].to_numpy()
    Xtr, ytr, Xva, yva, Xte, yte = X(tr), tr["won"].to_numpy(), X(va), va["won"].to_numpy(), X(te), te["won"].to_numpy()
    gva, gte = va["gated"].to_numpy() == 1, te["gated"].to_numpy() == 1

    # The NEURAL NETWORK is the model that runs. logistic regression and gradient boosting are
    # trained too, purely as yardsticks so you can see whether the network really adds something.
    fitted, val_auc, test_auc = {}, {}, {}
    nn_info: Dict = {}

    def _score(name, mdl, t0):
        ci_ = list(mdl.classes_).index(1)
        # models are compared on ALL validation rows (live-eligible setups alone are far too few)
        val_auc[name] = auc(yva, mdl.predict_proba(Xva)[:, ci_])
        test_auc[name] = auc(yte, mdl.predict_proba(Xte)[:, ci_])
        logger.info(f"  {name:7s} validation AUC={val_auc[name]}  test AUC={test_auc[name]}  ({time.time()-t0:.0f}s)")

    for name, mdl in make_models(args.nn_members).items():
        if name == "nn":
            continue
        t0 = time.time()
        try:
            mdl.fit(Xtr, ytr)
            fitted[name] = mdl
            _score(name, mdl, t0)
        except Exception as e:
            logger.warning(f"{name}: training failed ({e}) - skipped")

    t0 = time.time()
    try:
        hidden, alpha = (32, 16), 1e-2
        if not args.no_tune:
            logger.info("  tuning the neural network (size / weight-decay)...")
            hidden, alpha, tune_rows = tune_nn(Xtr, ytr, Xes, yes, Xva, yva)
            nn_info["tuning"] = tune_rows
        nn_info["hidden"], nn_info["alpha"] = list(hidden), alpha
        logger.info(f"  training the final neural network: {args.nn_members} networks, layers {hidden}, weight-decay {alpha:g}")
        nn = NNEnsemble(n_members=args.nn_members, hidden=hidden, alpha=alpha)
        nn.fit(Xtr, ytr, Xes, yes, log=logger.info)
        nn.fit_calibration(Xva, yva)
        fitted["nn"] = nn
        _score("nn", nn, t0)
    except Exception as e:
        logger.error(f"neural network training failed: {e}")
    if not fitted:
        logger.error("No model could be trained.")
        return None

    want = "nn" if args.model == "mlp" else args.model  # "mlp" kept as an alias of "nn"
    if want != "auto":
        if want not in fitted:
            logger.error(f"Requested model '{want}' could not be trained.")
            return None
        best = want
    else:
        best = max(fitted, key=lambda k: (val_auc[k] if val_auc[k] is not None else -1))
    logger.info("MODEL COMPARISON (higher AUC = better ranking of good vs bad setups):")
    for k in fitted:
        logger.info(f"   {'->' if k == best else '  '} {k:7s} validation AUC {val_auc[k]}   test AUC {test_auc[k]}")
    mdl = fitted[best]
    ci = list(mdl.classes_).index(1)
    pva = mdl.predict_proba(Xva)[:, ci]
    pte = mdl.predict_proba(Xte)[:, ci]

    # threshold chosen on validation, everything judged on the untouched test period.
    # Evaluation uses ALL rows (live-eligible + near-signal); the small subset the live bot
    # would really have traded (gated) is reported separately below.
    thr = choose_threshold(pva, yva, ev_win, ev_loss)
    if thr is None:
        thr = 0.5
        logger.warning("Could not choose a threshold on validation (too few rows) - using 0.50.")

    yt, pt_ = yte, pte
    keep = pt_ >= thr
    n_keep, n_drop = int(keep.sum()), int((~keep).sum())
    base_wr = float(yt.mean() * 100) if len(yt) else float("nan")
    kept_wr = float(yt[keep].mean() * 100) if n_keep else float("nan")
    drop_wr = float(yt[~keep].mean() * 100) if n_drop else float("nan")
    base_ev = ev_per_trade(yt, ev_win, ev_loss)
    kept_ev = ev_per_trade(yt[keep], ev_win, ev_loss) if n_keep else float("nan")
    z = z_two_prop(int(yt[keep].sum()), n_keep, int(yt[~keep].sum()), n_drop) if n_keep and n_drop else float("nan")
    tauc = auc(yt, pt_)
    vauc = val_auc[best]

    # the subset the live bot would genuinely have traded (strict rules + profit-chance gate)
    g_n = int(gte.sum())
    g_base = float(yte[gte].mean() * 100) if g_n else float("nan")
    g_keep = gte & keep
    g_kept_n = int(g_keep.sum())
    g_kept = float(yte[g_keep].mean() * 100) if g_kept_n else float("nan")

    checks = {
        "test AUC > 0.52": tauc is not None and tauc > 0.52,
        "validation AUC > 0.52": vauc is not None and vauc > 0.52,
        "enough approved test setups (>= 100)": n_keep >= 100,
        "enough rejected test setups (>= 50)": n_drop >= 50,
        "approved win rate beats all setups by >= 1 point": not math.isnan(kept_wr) and kept_wr >= base_wr + 1.0,
        "approved expectancy beats all setups": not math.isnan(kept_ev) and kept_ev > base_ev,
        "approved vs rejected difference significant (z >= 1.28)": not math.isnan(z) and z >= 1.28,
    }
    if g_n >= 30 and g_kept_n >= 10:  # only when the live-eligible subset is big enough to say anything
        checks["live-eligible subset: approved not worse than all of them"] = (
            not math.isnan(g_kept) and g_kept >= g_base)
    wf: List[Dict] = []
    if best == "nn" and args.wf_folds > 0:
        logger.info("Walk-forward stability test (a fresh network per later period, trained only on earlier data)...")
        try:
            wf = walk_forward(df, use_features, embargo_ms,
                              {"hidden": tuple(nn_info.get("hidden", (32, 16))), "alpha": nn_info.get("alpha", 1e-2)},
                              args.wf_folds)
        except Exception as e:
            logger.warning(f"walk-forward test failed ({e}) - skipped")
    wf_valid = [f for f in wf if f["auc"] is not None]
    wf_pos = sum(1 for f in wf_valid if f["auc"] > 0.52)
    if len(wf_valid) >= 3:  # only when there is enough history to cut into several periods
        need = max(2, math.ceil(0.75 * len(wf_valid)))
        checks[f"stable over time: AUC > 0.52 in at least {need} of {len(wf_valid)} walk-forward periods"] = wf_pos >= need
    usable = all(checks.values())

    imp_base, imp = permutation_importance(mdl, Xte, yte, use_features)

    metrics = {
        "val_auc": None if vauc is None else round(vauc, 4),
        "test_auc": None if tauc is None else round(tauc, 4),
        "test_rows": int(len(yt)),
        "test_live_eligible_rows": g_n,
        "test_live_eligible_winrate": None if math.isnan(g_base) else round(g_base, 1),
        "test_live_eligible_approved": g_kept_n,
        "test_live_eligible_approved_winrate": None if math.isnan(g_kept) else round(g_kept, 1),
        "test_base_winrate": round(base_wr, 1), "test_kept_winrate": None if math.isnan(kept_wr) else round(kept_wr, 1),
        "test_dropped_winrate": None if math.isnan(drop_wr) else round(drop_wr, 1),
        "test_kept_trades": n_keep, "test_dropped_trades": n_drop,
        "test_base_ev_pct_per_trade": round(base_ev, 3),
        "test_kept_ev_pct_per_trade": None if math.isnan(kept_ev) else round(kept_ev, 3),
        "breakeven_winrate_pct": round(breakeven * 100, 1),
        "z": None if math.isnan(z) else round(z, 2),
        "all_model_val_auc": val_auc, "all_model_test_auc": test_auc,
        "walk_forward": wf, "wf_periods": len(wf_valid), "wf_positive": wf_pos,
        "top_features": [{"feature": n, "auc_drop": round(d, 4)} for n, d in imp[:12]],
        "nn_settings": nn_info,
    }

    bundle = {
        "model": mdl, "model_name": best, "model_desc": (mdl.describe() if hasattr(mdl, "describe") else best),
        "feature_names": list(use_features), "threshold": float(thr),
        "usable": bool(usable), "metrics": metrics, "checks": {k: bool(v) for k, v in checks.items()},
        "trained_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sklearn_version": sklearn.__version__, "n_samples": int(len(df)), "n_train": int(len(tr)),
        "tp_percent": cfg["TAKE_PROFIT_PERCENT"], "sl_percent": cfg["STOP_LOSS_PERCENT"],
        "calibration_sig": calibration_signature(cfg.get("CALIBRATION_TABLE_FILE")),
        "used_live_rows": bool(args.use_live),
    }
    # Never replace a model that validated with one that did not (important for automatic
    # re-training: a bad month of data must not silently downgrade a good model).
    saved = True
    if not args.overwrite and os.path.exists(args.out) and not bundle["usable"]:
        try:
            old = joblib.load(args.out)
            if isinstance(old, dict) and old.get("usable") and old.get("model_name") == bundle["model_name"]:
                saved = False
        except Exception:
            pass  # unreadable old file -> replace it
    report = {k: v for k, v in bundle.items() if k != "model"}
    report["saved_as_active_model"] = saved
    if saved:
        tmp = args.out + ".tmp"
        joblib.dump(bundle, tmp)
        os.replace(tmp, args.out)  # atomic: the running bot never sees a half-written model
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    else:
        with open(args.report + ".rejected.json", "w") as fh:
            json.dump(report, fh, indent=2, default=str)
        logger.warning("New model did NOT validate while the current model of the same kind DID - keeping the current model "
                       "(use --overwrite to replace it anyway).")
    bundle["saved"] = saved

    print_report(bundle, base_wr, kept_wr, drop_wr, base_ev, kept_ev, breakeven, args)
    return bundle


def print_report(b, base_wr, kept_wr, drop_wr, base_ev, kept_ev, breakeven, args):
    m = b["metrics"]
    L = logger.info
    L("=" * 66)
    L(f"MODEL: {b.get('model_desc') or b['model_name']}   threshold: approve if win chance >= {b['threshold']*100:.0f}%")
    L(f"Break-even win rate for TP {b['tp_percent']}% / SL {b['sl_percent']}% + fees: {breakeven*100:.1f}%")
    L(f"UNSEEN TEST PERIOD ({m['test_rows']} setups: strict live-style + near-signals):")
    L(f"   all setups      : win {base_wr:.1f}%   expectancy {base_ev:+.2f}% per trade")
    L(f"   ML approved ({m['test_kept_trades']:>4}): win {kept_wr:.1f}%   expectancy {kept_ev:+.2f}% per trade")
    L(f"   ML rejected ({m['test_dropped_trades']:>4}): win {drop_wr:.1f}%")
    L(f"   of which setups the live bot would REALLY take: {m['test_live_eligible_rows']} "
      f"(win {m['test_live_eligible_winrate']}%), ML approved {m['test_live_eligible_approved']} "
      f"(win {m['test_live_eligible_approved_winrate']}%)")
    if m['test_live_eligible_rows'] < 100:
        L("   NOTE: too few real-style setups in the test period to confirm on them alone - "
          "watch the OFF-mode verdicts on your live trades (/ml) before switching ON.")
    wf = m.get("walk_forward") or []
    if wf:
        L("WALK-FORWARD (each later period predicted by a network trained only on earlier data):")
        for f in wf:
            L(f"   period {f['fold']}: AUC {f['auc']}   top-half win {f['top_half_winrate']}%  vs  "
              f"bottom-half win {f['bottom_half_winrate']}%   ({f['test_rows']} setups)")
    tf = m.get("top_features") or []
    if tf:
        L("FEATURES THE MODEL RELIES ON MOST (AUC lost when shuffled): "
          + ", ".join(f"{x['feature']} {x['auc_drop']:+.3f}" for x in tf[:8]))
    L("CHECKS:")
    for k, v in b["checks"].items():
        L(f"   [{'PASS' if v else 'FAIL'}] {k}")
    if b["usable"]:
        L("RESULT: VALIDATED - with ML Filter ON it will block trades it rates NOT GOOD.")
    else:
        L("RESULT: NOT VALIDATED - it only ADVISES on Telegram and never blocks. "
          "(Normal outcome when the signals carry little extra information; do not force it.)")
    if not b.get("saved", True):
        L("NOT SAVED as the active model: the current model validated and this one did not.")
    L("Reminder: history replay uses fixed TP/SL; live trades use dynamic TP/SL + trailing.")
    L("=" * 66)


def telegram_summary(b: Optional[Dict]):
    try:
        from telegram_notify import send_telegram
        if b is None:
            send_telegram("🤖 *ML training finished* - no model produced (see ml train log).")
            return
        m = b["metrics"]
        send_telegram(
            "🤖 *ML training finished*\n"
            f"Model: {b.get('model_desc') or b['model_name']}  samples: {b['n_samples']}\n"
            f"Test AUC: {m['test_auc']}  threshold: {b['threshold']*100:.0f}%\n"
            f"Test win rate: all setups {m['test_base_winrate']}% -> ML approved {m['test_kept_winrate']}% "
            f"(real-style setups in test: {m['test_live_eligible_rows']})\n"
            + (f"Stable over time: {m['wf_positive']}/{m['wf_periods']} periods\n" if m.get("wf_periods") else "")
            + f"Validated: {'YES - ON mode will block' if b['usable'] else 'NO - advisory only'}\n"
            + ("" if b.get("saved", True) else "⚠️ NOT activated: the current validated model was kept."))
    except Exception as e:
        logger.debug(f"telegram summary failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Collect history + train the optional ML trade filter")
    ap.add_argument("--symbols", type=str, default=None, help="comma list (default: TOP_N_COINS)")
    ap.add_argument("--months", type=int, default=9)
    ap.add_argument("--stride", type=int, default=0, help="replay every Nth 1h candle (default: backtest STRIDE)")
    ap.add_argument("--source", choices=["mainnet", "config"], default="mainnet",
                    help="history source: Binance mainnet public data (default) or whatever BINANCE_TESTNET says")
    ap.add_argument("--near-lean", type=int, default=4,
                    help="also learn from near-signal setups (|bull-bear tools| >= N over the 3 timeframes); "
                         "0 = only setups the bot would really take (very few)")
    ap.add_argument("--near-every", type=int, default=3,
                    help="record a near-signal setup every Nth 1h step only (default 3)")
    ap.add_argument("--keep-score-features", action="store_true",
                    help="also feed the profit-chance score features to the model (they contain live-only "
                         "funding/OI inputs that history does not have)")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--out", default=DEFAULT_MODEL)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--train-only", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip coins already in the dataset")
    ap.add_argument("--refresh", action="store_true", help="delete the dataset and collect from scratch")
    ap.add_argument("--model", choices=["nn", "auto", "gbm", "logreg", "mlp"], default="nn",
                    help="model that runs in the bot: nn = neural network (default). gbm / logreg / auto exist "
                         "only for comparison experiments (logreg and gbm are always trained as yardsticks)")
    ap.add_argument("--no-tune", action="store_true", help="skip the neural-network size / weight-decay search")
    ap.add_argument("--wf-folds", type=int, default=4,
                    help="walk-forward stability test periods (0 = skip). Needs enough history; ignored otherwise")
    ap.add_argument("--nn-members", type=int, default=7, help="networks in the neural-network ensemble")
    ap.add_argument("--overwrite", action="store_true",
                    help="activate the new model even if it did not validate and the current one did")
    ap.add_argument("--threads", type=int, default=2, help="max CPU threads for training (keeps the bot responsive)")
    ap.add_argument("--nice", type=int, default=10, help="lower CPU priority of training (0 = normal)")
    ap.add_argument("--use-live", action="store_true", help="also learn from this bot's own closed trades")
    ap.add_argument("--min-samples", type=int, default=300)
    ap.add_argument("--no-notify", action="store_true", help="do not send the Telegram summary")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    try:
        import sklearn, joblib  # noqa: F401
    except ImportError:
        raise SystemExit("scikit-learn / joblib not installed. Run:  pip install scikit-learn joblib")
    cfg = {k: v for k, v in vars(_cfg).items() if k.isupper()}

    if not args.train_only:
        collect(args)
    if args.collect_only:
        return
    try:
        os.nice(max(0, args.nice))  # training must never starve the trading bot of CPU
    except Exception:
        pass
    try:
        from threadpoolctl import threadpool_limits
        with threadpool_limits(limits=max(1, args.threads)):
            bundle = train(args, cfg)
    except ImportError:
        bundle = train(args, cfg)
    if not args.no_notify:
        telegram_summary(bundle)


if __name__ == "__main__":
    main()
