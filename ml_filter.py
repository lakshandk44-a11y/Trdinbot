"""
ML Filter (optional add-on module) - second opinion on every trade the bot is
about to open.

HOW IT FITS (nothing else in the bot changes its own logic):
  * The normal Tool-5 vote, MIN_PROFIT_CHANCE gate, trading-hours filter, Smart
    Hours / Coin Performance / Volatility / Daily-Loss guards ALL run exactly as
    before. This module is consulted last, right before any exchange action
    (bot_core._execute_trade), and it can only ever SAY NO to a trade that
    everything else already approved. It never creates a trade.

TWO MODES (one Telegram toggle: ML_FILTER_ENABLED):
  * OFF  (default) -> "notify only". The trade opens exactly as it always did.
                      The moment it opens, a Telegram message says whether the
                      ML model thinks it is a GOOD or NOT-GOOD trade.
  * ON             -> the trade is blocked when the model says NOT GOOD.
                      A (rate-limited) Telegram message says what was blocked.

FAIL-OPEN EVERYWHERE: no model file, unreadable model, missing features, any
exception -> the trade proceeds untouched. ML can never crash or stall the bot.
If ML_MODULE_ENABLED = False in config.py (or this file is deleted) the bot
behaves byte-for-byte as before.

A model that did NOT validate well on unseen, newer data (see ml_train.py) is
flagged "unvalidated": it still posts verdicts (clearly labelled), but it never
blocks anything, even with the toggle ON, unless ML_BLOCK_EVEN_IF_UNVALIDATED.

FILES (all next to this file):
  ml_model.pkl         written by ml_train.py (hot-reloaded, no restart needed)
  ml_decisions.jsonl   one line per blocked / opened trade + closed outcomes
"""
import hashlib
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger("ml_filter")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TFS = ("higher", "medium", "lower")

# ----------------------------------------------------------------------
# Feature definition - SHARED by ml_train.py (history) and the live bot, so
# a model is always fed exactly the same kind of numbers it was trained on.
# Everything direction-dependent is oriented to the trade (positive = in
# favour of the trade), so one model serves both BUY and SELL.
# ----------------------------------------------------------------------
_TF_KEYS = ("agree", "oppose", "sagree", "soppose", "conf", "pc", "adx", "regime")
FEATURE_NAMES: List[str] = (
    ["dir"]
    + [f"{tf}_{k}" for tf in TFS for k in _TF_KEYS]
    + ["final_pc", "final_tools", "final_conf", "strong_path", "tot_agree", "tot_oppose",
       "atr_lower", "atr_higher",
       "rsi_lower", "rsi_medium",
       "ret_lower_4", "ret_lower_16", "ret_medium_6", "ret_higher_6",
       "dist_ema50_medium", "vol_ratio_lower", "range_pos_lower",
       "hour_sin", "hour_cos", "dow_sin", "dow_cos"]
)

_NAN = float("nan")


def _num(x, default=_NAN) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _arrays(df, drop_last: bool):
    """high/low/close/volume numpy arrays from a candle DataFrame, or None.
    drop_last=True removes the final row - used LIVE, where the last kline is
    the still-forming candle (backtest slices only contain closed candles)."""
    try:
        if df is None or len(df) == 0:
            return None
        if drop_last and len(df) > 1:
            df = df.iloc[:-1]
        return {
            "h": df["high"].to_numpy(dtype=float),
            "l": df["low"].to_numpy(dtype=float),
            "c": df["close"].to_numpy(dtype=float),
            "v": df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None,
        }
    except Exception:
        return None


def _atr_pct(a, period: int = 14) -> float:
    if a is None or len(a["c"]) < period + 1:
        return _NAN
    h, l, c = a["h"], a["l"], a["c"]
    pc = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    atr = float(tr[-period:].mean())
    return atr / c[-1] * 100.0 if c[-1] > 0 else _NAN


def _rsi(c: np.ndarray, period: int = 14) -> float:
    if len(c) < period + 1:
        return _NAN
    d = np.diff(c)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    for i in range(period, len(d)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _ema_last(c: np.ndarray, span: int) -> float:
    if len(c) < span:
        return _NAN
    alpha = 2.0 / (span + 1.0)
    e = float(c[:span].mean())
    for x in c[span:]:
        e = alpha * float(x) + (1.0 - alpha) * e
    return e


def _ret(c: np.ndarray, n: int, d: int) -> float:
    if len(c) <= n or c[-1 - n] <= 0:
        return _NAN
    return (float(c[-1]) / float(c[-1 - n]) - 1.0) * 100.0 * d


def extract_features(analysis: Dict, ohlc: Dict, decision: str,
                     ts_ms: Optional[int] = None, drop_forming: bool = False) -> Optional[Dict[str, float]]:
    """
    analysis: AnalysisEngine.multi_timeframe_analysis() output (has "higher",
              "medium", "lower", "final_signal").
    ohlc:     {"higher": df, "medium": df, "lower": df} the analysis was run on.
    decision: "BUY" / "SELL".
    ts_ms:    decision time (UTC ms). None = now.
    drop_forming: True live (last candle still forming), False for backtests.
    Returns {feature_name: float} (NaN where unavailable) or None.
    """
    try:
        if decision not in ("BUY", "SELL") or not isinstance(analysis, dict):
            return None
        d = 1 if decision == "BUY" else -1
        f: Dict[str, float] = {n: _NAN for n in FEATURE_NAMES}
        f["dir"] = float(d)

        for tf in TFS:
            r = analysis.get(tf) or {}
            bull = _num(r.get("bullish_tools"), 0.0)
            bear = _num(r.get("bearish_tools"), 0.0)
            sbull = _num(r.get("strong_bullish_tools"), 0.0)
            sbear = _num(r.get("strong_bearish_tools"), 0.0)
            f[f"{tf}_agree"] = bull if d > 0 else bear
            f[f"{tf}_oppose"] = bear if d > 0 else bull
            f[f"{tf}_sagree"] = sbull if d > 0 else sbear
            f[f"{tf}_soppose"] = sbear if d > 0 else sbull
            f[f"{tf}_conf"] = _num(r.get("confidence"), 0.0)
            f[f"{tf}_pc"] = _num(r.get("profit_chance"))
            mr = r.get("market_regime") or {}
            f[f"{tf}_adx"] = _num(mr.get("adx"))
            reg = mr.get("regime")
            f[f"{tf}_regime"] = 1.0 if reg == "trending" else (-1.0 if reg == "ranging" else 0.0)

        fs = analysis.get("final_signal") or {}
        f["final_pc"] = _num(fs.get("profit_chance"))
        f["final_tools"] = _num(fs.get("tools_agreeing"), 0.0)
        f["final_conf"] = _num(fs.get("confidence"), 0.0)
        f["strong_path"] = 1.0 if fs.get("entry_path") == "single_tf_strong" else 0.0
        tb = _num(fs.get("total_bullish_tools"), 0.0)
        tr_ = _num(fs.get("total_bearish_tools"), 0.0)
        f["tot_agree"] = tb if d > 0 else tr_
        f["tot_oppose"] = tr_ if d > 0 else tb

        ohlc = ohlc or {}
        lo = _arrays(ohlc.get("lower"), drop_forming)
        me = _arrays(ohlc.get("medium"), drop_forming)
        hi = _arrays(ohlc.get("higher"), drop_forming)

        f["atr_lower"] = _atr_pct(lo)
        f["atr_higher"] = _atr_pct(hi)
        if lo is not None:
            f["rsi_lower"] = (_rsi(lo["c"]) - 50.0) * d
            f["ret_lower_4"] = _ret(lo["c"], 4, d)
            f["ret_lower_16"] = _ret(lo["c"], 16, d)
            if lo["v"] is not None and len(lo["v"]) >= 24:
                base = float(lo["v"][-24:].mean())
                if base > 0:
                    f["vol_ratio_lower"] = float(lo["v"][-4:].mean()) / base
            if len(lo["c"]) >= 30:
                hh = float(lo["h"][-96:].max())
                ll = float(lo["l"][-96:].min())
                if hh > ll:
                    pos = (float(lo["c"][-1]) - ll) / (hh - ll)
                    f["range_pos_lower"] = pos if d > 0 else 1.0 - pos
        if me is not None:
            f["rsi_medium"] = (_rsi(me["c"]) - 50.0) * d
            f["ret_medium_6"] = _ret(me["c"], 6, d)
            e50 = _ema_last(me["c"], 50)
            if not math.isnan(e50) and e50 > 0:
                f["dist_ema50_medium"] = (float(me["c"][-1]) / e50 - 1.0) * 100.0 * d
        if hi is not None:
            f["ret_higher_6"] = _ret(hi["c"], 6, d)

        t = datetime.fromtimestamp((ts_ms / 1000.0) if ts_ms else time.time(), tz=timezone.utc)
        hour = t.hour + t.minute / 60.0
        f["hour_sin"] = math.sin(2 * math.pi * hour / 24.0)
        f["hour_cos"] = math.cos(2 * math.pi * hour / 24.0)
        f["dow_sin"] = math.sin(2 * math.pi * t.weekday() / 7.0)
        f["dow_cos"] = math.cos(2 * math.pi * t.weekday() / 7.0)
        return f
    except Exception as e:
        logger.debug(f"extract_features failed: {e}")
        return None


# ----------------------------------------------------------------------
# JSONL decision log helpers (module-level: ml_train.py uses them too)
# ----------------------------------------------------------------------
def log_files(base_path: str) -> List[str]:
    """Oldest -> newest: <log>.2, <log>.1, <log>."""
    return [p for p in (base_path + ".2", base_path + ".1", base_path) if os.path.exists(p)]


def read_records(base_path: str) -> List[Dict]:
    out: List[Dict] = []
    for p in log_files(base_path):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue  # a half-written / corrupt line never matters
        except Exception as e:
            logger.debug(f"could not read {p}: {e}")
    return out


def _md_safe(text) -> str:
    """Telegram legacy Markdown cannot escape _ * ` [ - strip them from dynamic text."""
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "")


def _clean_features(feats: Dict[str, float]) -> Dict[str, Optional[float]]:
    return {k: (None if (v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))) else round(float(v), 6))
            for k, v in feats.items()}


class NNEnsemble:
    """
    Bagged ensemble of small feed-forward NEURAL NETWORKS (scikit-learn MLPs).

    Lives in THIS module (not in ml_train.py) on purpose: the trained object is
    pickled, and the running bot can only unpickle classes it can import. Keeping
    it here means ml_model.pkl loads inside the bot exactly as it does in the trainer.

    Why an ensemble: one network trained on noisy market data is unstable (a
    different random start gives a different answer). Averaging several networks
    - each started from different random weights and fed a different random 80%
    of the training rows - cancels most of that noise and gives smoother, better
    calibrated probabilities.

    Training details (done properly, not scikit-learn's default early stopping):
      * median-imputation + standardisation fitted on training rows only
      * every epoch is scored on a TIME-ORDERED validation slice (the newest data
        the network has not trained on); training stops after `patience` epochs
        without improvement and the best epoch's weights are restored
      * L2 weight decay (alpha) + small layers keep it from memorising noise
    """

    def __init__(self, n_members: int = 7, hidden=(48, 24), alpha: float = 3e-2, lr: float = 1e-3,
                 batch_size: int = 256, max_epochs: int = 150, patience: int = 12,
                 subsample: float = 0.8, seed: int = 42):
        self.n_members = int(n_members)
        self.hidden = tuple(hidden)
        self.alpha = float(alpha)
        self.lr = float(lr)
        self.batch_size = int(batch_size)
        self.max_epochs = int(max_epochs)
        self.patience = int(patience)
        self.subsample = float(subsample)
        self.seed = int(seed)
        self.classes_ = np.array([0, 1])
        self.imputer_ = None
        self.scaler_ = None
        self.members_: List = []
        self.history_: List[Dict] = []
        self.cal_a_: Optional[float] = None   # Platt calibration (fitted by fit_calibration)
        self.cal_b_: float = 0.0

    def fit(self, X, y, X_val, y_val, log=None):
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        from sklearn.neural_network import MLPClassifier
        from sklearn.metrics import log_loss

        X = np.asarray(X, dtype=float)
        X_val = np.asarray(X_val, dtype=float)
        y = np.asarray(y).astype(int)
        y_val = np.asarray(y_val).astype(int)
        self.imputer_ = SimpleImputer(strategy="median").fit(X)
        self.scaler_ = StandardScaler().fit(self.imputer_.transform(X))
        Xs = self.scaler_.transform(self.imputer_.transform(X))
        Xvs = self.scaler_.transform(self.imputer_.transform(X_val))

        rng = np.random.RandomState(self.seed)
        n = len(Xs)
        self.members_, self.history_ = [], []
        for m in range(self.n_members):
            idx = rng.choice(n, size=max(50, int(n * self.subsample)), replace=False)
            mlp = MLPClassifier(hidden_layer_sizes=self.hidden, alpha=self.alpha, batch_size=self.batch_size,
                                learning_rate_init=self.lr, solver="adam", random_state=self.seed + m)
            best_loss, best_w, best_ep, since = float("inf"), None, 0, 0
            for ep in range(1, self.max_epochs + 1):
                mlp.partial_fit(Xs[idx], y[idx], classes=[0, 1])  # one pass over the data
                vloss = log_loss(y_val, mlp.predict_proba(Xvs)[:, 1], labels=[0, 1])
                if vloss < best_loss - 1e-4:
                    best_loss, best_ep, since = vloss, ep, 0
                    best_w = ([c.copy() for c in mlp.coefs_], [b.copy() for b in mlp.intercepts_])
                else:
                    since += 1
                    if since >= self.patience:
                        break
            if best_w is not None:
                mlp.coefs_, mlp.intercepts_ = best_w
            self.members_.append(mlp)
            self.history_.append({"member": m, "best_epoch": best_ep, "val_logloss": round(float(best_loss), 5)})
            if log:
                log(f"    network {m + 1}/{self.n_members}: best epoch {best_ep}, validation log-loss {best_loss:.4f}")
        return self

    def _raw_p1(self, X) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        Xs = self.scaler_.transform(self.imputer_.transform(X))
        return np.mean([mm.predict_proba(Xs)[:, 1] for mm in self.members_], axis=0)

    def fit_calibration(self, X, y):
        """Platt scaling on held-out data so a printed 'win chance 55%' really means ~55%
        of such setups won (raw network outputs are usually over/under-confident). It is a
        monotone re-labelling: it never changes which setup ranks above which."""
        from sklearn.linear_model import LogisticRegression
        y = np.asarray(y).astype(int)
        if len(set(y.tolist())) < 2:
            return self
        p = np.clip(self._raw_p1(X), 1e-4, 1 - 1e-4)
        z = np.log(p / (1.0 - p)).reshape(-1, 1)
        lr = LogisticRegression(C=1e6, max_iter=1000).fit(z, y)
        a, b = float(lr.coef_[0][0]), float(lr.intercept_[0])
        if a > 0.05:  # a flat/negative slope means the network has no signal - leave it uncalibrated
            self.cal_a_, self.cal_b_ = a, b
        return self

    def predict_proba(self, X):
        p1 = self._raw_p1(X)
        a = getattr(self, "cal_a_", None)
        if a is not None:
            pc = np.clip(p1, 1e-4, 1 - 1e-4)
            p1 = 1.0 / (1.0 + np.exp(-(a * np.log(pc / (1.0 - pc)) + getattr(self, "cal_b_", 0.0))))
        return np.column_stack([1.0 - p1, p1])

    def describe(self) -> str:
        cal = ", calibrated" if getattr(self, "cal_a_", None) is not None else ""
        return f"{self.n_members} neural networks ({'-'.join(str(h) for h in self.hidden)}), averaged{cal}"


class MLFilter:
    def __init__(self, config: Dict, history_provider: Optional[Callable[[], List[Dict]]] = None,
                 base_dir: Optional[str] = None):
        self.config = config
        base = base_dir or _BASE_DIR
        self.model_path = config.get("ML_MODEL_FILE") or os.path.join(base, "ml_model.pkl")
        self.log_path = config.get("ML_DECISION_LOG_FILE") or os.path.join(base, "ml_decisions.jsonl")
        self.history_provider = history_provider

        self._lock = threading.Lock()
        self._bundle: Optional[Dict] = None
        self._model_mtime: Optional[float] = None
        self._last_load_check = 0.0
        self._load_error: Optional[str] = None
        self._closed_ids: Optional[set] = None
        self._last_sync = 0.0
        self._block_notified: Dict[str, float] = {}

        self._try_load(force=True)
        if self._bundle is None:
            logger.info("🤖 ML Filter ready - no trained model yet (run: python3 ml_train.py). "
                        "Until then it does nothing and every trade proceeds normally.")

    # ------------------------------------------------------------------
    # model loading (hot-reload: retraining never needs a bot restart)
    # ------------------------------------------------------------------
    def _try_load(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_load_check < 60:
            return
        self._last_load_check = now
        try:
            mtime = os.path.getmtime(self.model_path)
        except OSError:
            return  # no file (or vanished): keep whatever is loaded, if anything
        if mtime == self._model_mtime:
            return
        self._model_mtime = mtime  # also stops a broken file being retried every minute
        try:
            import joblib
            bundle = joblib.load(self.model_path)
            if not isinstance(bundle, dict) or "model" not in bundle or "feature_names" not in bundle:
                raise ValueError("unexpected model file layout")
            try:
                import sklearn
                trained_v = bundle.get("sklearn_version")
                if trained_v and trained_v != sklearn.__version__:
                    logger.warning(f"🤖 ML model was trained with scikit-learn {trained_v} but {sklearn.__version__} "
                                   f"is installed - retrain (python3 ml_train.py --train-only) if verdicts look odd.")
            except Exception:
                pass
            with self._lock:
                self._bundle = bundle
                self._load_error = None
            logger.info(f"🤖 ML model loaded: {bundle.get('model_name', '?')} | "
                        f"trained {bundle.get('trained_at', '?')} | threshold {bundle.get('threshold', 0):.2f} | "
                        f"validated={'YES' if bundle.get('usable') else 'NO'}")
        except Exception as e:
            self._load_error = str(e)
            logger.warning(f"🤖 ML model could not be loaded ({e}) - ML Filter inactive, trades unaffected.")

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def evaluate(self, symbol: str, decision: str, analysis: Dict, ohlc_data: Dict,
                 enforce: bool = False) -> Optional[Dict]:
        """None = no opinion (no model / bad input) -> caller proceeds normally."""
        try:
            if decision not in ("BUY", "SELL"):
                return None
            self._try_load()
            self._maybe_sync_outcomes()
            bundle = self._bundle
            if bundle is None:
                return None
            feats = extract_features(analysis, ohlc_data, decision, ts_ms=None, drop_forming=True)
            if feats is None:
                return None
            names = bundle["feature_names"]
            x = np.array([[feats.get(n, _NAN) for n in names]], dtype=float)
            model = bundle["model"]
            proba = model.predict_proba(x)[0]
            idx = list(model.classes_).index(1)
            prob = float(proba[idx])
            thr = self.config.get("ML_THRESHOLD_OVERRIDE")
            thr = float(thr) if thr is not None else float(bundle.get("threshold", 0.5))
            good = prob >= thr
            trusted = bool(bundle.get("usable", False))
            allow_unvalidated = bool(self.config.get("ML_BLOCK_EVEN_IF_UNVALIDATED", False))
            block = bool(enforce and (not good) and (trusted or allow_unvalidated))
            return {
                "id": f"{symbol}-{int(time.time() * 1000)}",
                "symbol": symbol, "decision": decision,
                "prob": prob, "threshold": thr, "good": good,
                "trusted": trusted, "block": block, "enforce": bool(enforce),
                "model_name": bundle.get("model_name", "?"),
                "features": feats,
            }
        except Exception as e:
            logger.warning(f"🤖 ML evaluate failed for {symbol}: {e} - trade proceeds normally.")
            return None

    @staticmethod
    def compact(result: Dict) -> Dict:
        """Tiny summary stored inside the trade record (kept small on purpose:
        trade_state.json is rewritten often)."""
        return {
            "id": result["id"],
            "prob": round(float(result["prob"]), 4),
            "thr": round(float(result["threshold"]), 4),
            "verdict": "GOOD" if result["good"] else "BAD",
            "mode": "block" if result["enforce"] else "notify",
            "trusted": bool(result["trusted"]),
        }

    # ------------------------------------------------------------------
    # events from bot_core
    # ------------------------------------------------------------------
    def on_blocked(self, symbol: str, decision: str, result: Dict):
        """Trade was vetoed. The same signal reappears every scan cycle, so the
        log line / jsonl row / Telegram message are rate-limited per coin+side."""
        try:
            key = f"{symbol}:{decision}"
            cooldown = float(self.config.get("ML_BLOCK_NOTIFY_COOLDOWN_MIN", 60)) * 60.0
            now = time.time()
            last = self._block_notified.get(key, 0.0)
            if now - last < cooldown:
                logger.debug(f"🤖 ML Filter: {symbol} {decision} still blocked (win chance {result['prob']*100:.1f}%).")
                return
            self._block_notified[key] = now
            logger.info(f"🤖 ML Filter: BLOCKED {symbol} {decision} - win chance {result['prob']*100:.1f}% "
                        f"< {result['threshold']*100:.1f}% required.")
            self._append({"type": "blocked", "ts": datetime.utcnow().isoformat(), "id": result["id"],
                          "symbol": symbol, "side": decision, "prob": round(result["prob"], 4),
                          "thr": round(result["threshold"], 4), "features": _clean_features(result["features"])})
            self._notify(
                f"🤖 *ML FILTER BLOCKED A TRADE*\n"
                f"Coin: {_md_safe(symbol)}  ({_md_safe(decision)})\n"
                f"Win chance: {result['prob']*100:.1f}%  (needs {result['threshold']*100:.1f}%)\n"
                f"This trade was NOT opened.")
        except Exception as e:
            logger.debug(f"ML on_blocked failed: {e}")

    def on_opened(self, symbol: str, decision: str, result: Dict):
        """Trade really opened - log it and send the verdict message right away."""
        try:
            verdict = "GOOD" if result["good"] else "BAD"
            logger.info(f"🤖 ML verdict for opened trade {symbol} {decision}: {verdict} "
                        f"(win chance {result['prob']*100:.1f}%, threshold {result['threshold']*100:.1f}%, "
                        f"mode={'block' if result['enforce'] else 'notify'}, validated={result['trusted']})")
            self._append({"type": "opened", "ts": datetime.utcnow().isoformat(), "id": result["id"],
                          "symbol": symbol, "side": decision, "prob": round(result["prob"], 4),
                          "thr": round(result["threshold"], 4), "verdict": verdict,
                          "mode": "block" if result["enforce"] else "notify",
                          "trusted": bool(result["trusted"]),
                          "features": _clean_features(result["features"])})
            if self.config.get("ML_NOTIFY_ENABLED", True):
                self._notify(self._format_opened(symbol, decision, result))
            self._maybe_sync_outcomes(force=True)
        except Exception as e:
            logger.debug(f"ML on_opened failed: {e}")

    @staticmethod
    def _format_opened(symbol: str, decision: str, r: Dict) -> str:
        mode = "ON - blocking" if r["enforce"] else "OFF - notify only"
        head = f"🤖 *ML VERDICT*  (ML Filter {mode})\nCoin: {_md_safe(symbol)}  ({_md_safe(decision)})\n"
        if r["good"]:
            body = f"✅ *GOOD trade* - win chance {r['prob']*100:.1f}% (needs {r['threshold']*100:.1f}%)"
        else:
            body = f"❌ *NOT GOOD trade* - win chance {r['prob']*100:.1f}% (needs {r['threshold']*100:.1f}%)"
            if r["enforce"]:
                body += "\nNot blocked: this model did not validate well, so it only advises."
            else:
                body += "\nWould have been BLOCKED if ML Filter was ON."
        if not r["trusted"]:
            body += "\n⚠️ Model not validated on unseen data - treat as a hint only."
        return head + body

    # ------------------------------------------------------------------
    # outcomes: link every closed trade back to its ML verdict
    # ------------------------------------------------------------------
    def _load_closed_ids(self):
        if self._closed_ids is None:
            self._closed_ids = {r.get("id") for r in read_records(self.log_path) if r.get("type") == "closed"}

    def _maybe_sync_outcomes(self, force: bool = False):
        try:
            if self.history_provider is None:
                return
            now = time.time()
            if not force and now - self._last_sync < 600:
                return
            self._last_sync = now
            self.sync_outcomes(self.history_provider())
        except Exception as e:
            logger.debug(f"ML outcome sync failed: {e}")

    def sync_outcomes(self, history: List[Dict]) -> int:
        """Append a 'closed' row for every finished trade carrying an ML tag that
        has not been recorded yet. Idempotent. Works from trade_history (which
        also contains trades closed by Binance's own SL/TP orders)."""
        n = 0
        with self._lock:
            self._load_closed_ids()
            for t in history or []:
                ml = (t.get("analysis") or {}).get("ml")
                if not ml or not ml.get("id") or ml["id"] in self._closed_ids:
                    continue
                if t.get("status") != "CLOSED" and not t.get("close_time"):
                    continue
                pnl = _num(t.get("pnl_percent"))
                if math.isnan(pnl):
                    continue
                self._append_unlocked({"type": "closed", "ts": datetime.utcnow().isoformat(), "id": ml["id"],
                                       "symbol": t.get("symbol"), "side": t.get("side"),
                                       "pnl_percent": round(pnl, 4), "win": bool(pnl > 0),
                                       "close_reason": t.get("close_reason"), "verdict": ml.get("verdict"),
                                       "prob": ml.get("prob")})
                self._closed_ids.add(ml["id"])
                n += 1
        return n

    # ------------------------------------------------------------------
    # jsonl
    # ------------------------------------------------------------------
    def _append(self, rec: Dict):
        with self._lock:
            self._append_unlocked(rec)

    def _append_unlocked(self, rec: Dict):
        try:
            line = json.dumps(rec, default=str, separators=(",", ":"))
            self._rotate_if_needed()
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:
            logger.debug(f"ML log write failed: {e}")

    def _rotate_if_needed(self):
        try:
            limit = int(self.config.get("ML_LOG_MAX_BYTES", 5_000_000))
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > limit:
                p1, p2 = self.log_path + ".1", self.log_path + ".2"
                if os.path.exists(p2):
                    os.remove(p2)
                if os.path.exists(p1):
                    os.replace(p1, p2)
                os.replace(self.log_path, p1)
        except Exception as e:
            logger.debug(f"ML log rotate failed: {e}")

    # ------------------------------------------------------------------
    # telegram (background thread: a slow Telegram call can never stall scanning)
    # ------------------------------------------------------------------
    @staticmethod
    def _notify(text: str):
        def _run():
            try:
                from telegram_notify import send_telegram
                send_telegram(text)
            except Exception as e:
                logger.debug(f"ML telegram notify failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # /ml status
    # ------------------------------------------------------------------
    def status_text(self, enforce: bool) -> str:
        try:
            self._try_load(force=True)
            self._maybe_sync_outcomes(force=True)
        except Exception:
            pass
        mode = "ON - blocks NOT GOOD trades" if enforce else "OFF - notify only"
        lines = [f"🤖 *ML Filter*", f"Mode: {mode}"]
        b = self._bundle
        if b is None:
            if self._load_error:
                lines.append(f"Model: could not load ({_md_safe(self._load_error)[:80]})")
            else:
                lines.append("Model: none yet - run python3 ml_train.py on the VPS")
            return "\n".join(lines)

        m = b.get("metrics", {}) or {}
        lines.append(f"Model: {_md_safe(b.get('model_desc') or b.get('model_name', '?'))}")
        lines.append(f"Trained: {_md_safe(str(b.get('trained_at', '?'))[:16])}")
        lines.append(f"Validated on unseen data: {'YES' if b.get('usable') else 'NO'}")
        if m:
            lines.append(f"Test AUC: {m.get('test_auc', 'n/a')}  Threshold: {float(b.get('threshold', 0))*100:.0f}%")
            if m.get("test_kept_winrate") is not None:
                lines.append(f"Test win rate: all {m.get('test_base_winrate')}%  ->  ML-approved {m.get('test_kept_winrate')}%")
        try:
            cal_sig = b.get("calibration_sig")
            cur_sig = calibration_signature(self.config.get("CALIBRATION_TABLE_FILE"))
            if cal_sig and cur_sig and cal_sig != cur_sig:
                lines.append("⚠️ calibration table changed since training - retrain the model")
        except Exception:
            pass

        # live evidence from this bot's own closed trades
        stats = {"GOOD": [0, 0], "BAD": [0, 0]}
        try:
            hist = self.history_provider() if self.history_provider else []
            for t in hist:
                ml = (t.get("analysis") or {}).get("ml")
                if not ml or ml.get("verdict") not in stats:
                    continue
                if t.get("pnl_percent") is None:
                    continue
                stats[ml["verdict"]][0] += 1
                if t["pnl_percent"] > 0:
                    stats[ml["verdict"]][1] += 1
        except Exception:
            pass
        lines.append("")
        lines.append("Live results of trades that carried a verdict:")
        for k, label in (("GOOD", "✅ GOOD"), ("BAD", "❌ NOT GOOD")):
            n, w = stats[k]
            lines.append(f"  {label}: {n} closed" + (f", {w/n*100:.0f}% won" if n else ""))
        try:
            week_ago = time.time() - 7 * 86400
            blocked = 0
            for r in read_records(self.log_path):
                if r.get("type") == "blocked":
                    try:
                        ts = datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        continue
                    if ts >= week_ago:
                        blocked += 1
            lines.append(f"Blocked in the last 7 days: {blocked}")
        except Exception:
            pass
        return "\n".join(lines)


def calibration_signature(path: Optional[str]) -> Optional[str]:
    """Short hash of calibration_table.json - lets us notice if it changed after training."""
    try:
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                return hashlib.sha1(fh.read()).hexdigest()[:10]
    except Exception:
        pass
    return None
