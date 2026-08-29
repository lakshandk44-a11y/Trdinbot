"""
HackerAI Trading Bot - Volatility Guard
=========================================
Detects when a symbol's CURRENT price volatility is abnormally elevated
relative to its own recent history, and blocks NEW trade entries for
that symbol until conditions normalize - without touching any existing
trade management, SL/TP, trailing-stop, or analysis logic in any way.

WHY THIS EXISTS
---------------
ICT/SMC (structure-based) signals rely on price respecting detected
levels (Order Blocks, FVGs, swing structure). During abnormally choppy
or news-driven conditions, price frequently violates these levels with
sharp, erratic moves ("whipsaws") that have nothing to do with the
underlying structure being wrong - the market itself is just unusually
disorderly right now. Entering during exactly these windows is where
structure-based strategies tend to take their worst, fastest losses.

HOW IT WORKS (deliberately stateless - see design note below)
----------------------------------------------------------------
For the symbol's execution-timeframe candles (the SAME OHLC data
already fetched and passed into _execute_trade() - no extra API calls):

  1. Compute a rolling ATR(14) series across the whole candle window.
  2. current_atr  = the most recent ATR reading (last CLOSED candle).
  3. baseline_atr = the average ATR over a longer recent lookback
                     (VOLATILITY_GUARD_BASELINE_CANDLES candles - the
                     symbol's own "normal" volatility recently).
  4. ratio = current_atr / baseline_atr

  If ratio >= VOLATILITY_GUARD_RATIO_THRESHOLD, the CURRENT moment is
  judged abnormally volatile FOR THIS SYMBOL relative to its own recent
  behavior, and new entries are blocked - but ONLY for this symbol,
  ONLY for this one check. Already-open trades on ANY symbol are never
  touched; SL/TP/trailing continue managing them exactly as before.

DESIGN NOTE - why this is intentionally STATELESS
----------------------------------------------------
Unlike SessionLossGuard (tracks losses over time) or SmartHoursGuard
(tracks per-hour history), VolatilityGuard needs no memory between
calls: every check is fully re-derived from the CURRENT candle data
already on hand. This is a deliberate design choice, made specifically
to avoid an entire class of bugs this bot has already hit twice in
production - a persisted-state file defaulting to a relative path,
which silently "loses" its data whenever a VPS/PM2 restart happens to
launch the process from a different working directory. A guard with
NO persisted state cannot suffer that failure mode at all: there is
nothing to save, nothing to load, and therefore nothing that can be
lost or become stale across a restart. Every restart, this guard is
immediately and correctly "born correct" from whatever live data comes
in on the very next check - by construction, not by careful bookkeeping.

This statelessness also means there is no cross-thread mutable state
to protect, so no locking is needed anywhere in this file.

FAIL-SAFE BEHAVIOR
-------------------
- Not enough candle history for this symbol yet -> fails OPEN (does
  NOT block) rather than guessing. A new/thinly-traded symbol should
  never be silently unable to trade just because it lacks history.
- Any NaN/invalid OHLC values, zero baseline, or unexpected shape in
  the data -> fails OPEN, logs a warning, never raises.
- Missing/failed import of this module elsewhere in the bot -> the
  bot continues exactly as before (see trade_manager.py's
  self.volatility_guard = None fallback and every call site's
  `if self.volatility_guard is not None` guard).
"""

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class VolatilityGuard:
    """
    Real-time, per-symbol volatility spike detector for gating NEW
    trade entries. Stateless by design (see module docstring).

    Parameters
    ----------
    config : dict
        The bot's live config dict (same one bot_core / trade_manager
        use). Read fresh on every call so Telegram-toggled changes
        take effect immediately without a restart.
    """

    def __init__(self, config: Dict):
        self.config = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_blocked(self, ohlc: Optional[pd.DataFrame], symbol: str) -> bool:
        """
        Returns True if `symbol`'s current volatility is abnormally
        elevated relative to its own recent history and a new entry
        should be skipped right now.

        Called from bot_core._execute_trade() alongside the other
        entry guards (daily loss limit, session loss guard, smart
        hours guard), before ANY exchange action. Never blocks
        managing/closing an ALREADY-open trade - only new entries.

        `ohlc` should be the SAME DataFrame already fetched for
        analysis (bot_core passes ohlc_data[VOLATILITY_GUARD_TIMEFRAME]
        straight through - no separate fetch happens here).
        """
        if not self.config.get("VOLATILITY_GUARD_ENABLED", True):
            return False

        ratio, current_atr, baseline_atr, reason = self._compute_ratio(ohlc)

        if ratio is None:
            # Insufficient/invalid data - fail OPEN, never block on
            # uncertainty. Logged at debug level since this is routine
            # for newly-listed symbols or very early in a scan cycle.
            logger.debug(f"VolatilityGuard {symbol}: {reason} - not blocking (fail-open)")
            return False

        threshold = self.config.get("VOLATILITY_GUARD_RATIO_THRESHOLD", 2.0)
        blocked = ratio >= threshold

        logger.info(
            f"📊 VolatilityGuard {symbol}: ratio={ratio:.2f}x "
            f"(threshold={threshold:.2f}x) | current_ATR={current_atr:.8f} | "
            f"baseline_ATR={baseline_atr:.8f} | "
            f"{'🔴 BLOCKED' if blocked else '✅ ok'}"
        )

        return blocked

    def get_status_text(self) -> str:
        """
        Short status string for the Telegram /status panel. Always
        safe to call; never raises.

        Unlike SessionLossGuard/SmartHoursGuard, this guard has no
        single "currently active" global state to report - it decides
        fresh, per-symbol, on every entry attempt. The status line
        therefore reports configuration (enabled + threshold), not a
        live blocked/clear state.
        """
        try:
            if not self.config.get("VOLATILITY_GUARD_ENABLED", True):
                return "🔕 Disabled"
            threshold = self.config.get("VOLATILITY_GUARD_RATIO_THRESHOLD", 2.0)
            baseline_n = self.config.get("VOLATILITY_GUARD_BASELINE_CANDLES", 100)
            return (
                f"✅ Active — blocks entries when current volatility "
                f"≥ {threshold:.1f}x the last {baseline_n}-candle average"
            )
        except Exception:
            return "⚠️ status unavailable"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_ratio(
        self, ohlc: Optional[pd.DataFrame]
    ) -> Tuple[Optional[float], float, float, str]:
        """
        Returns (ratio, current_atr, baseline_atr, reason).
        `ratio` is None if the ratio could not be computed for any
        reason - `reason` then explains why (for logging only).
        """
        atr_period = max(1, int(self.config.get("VOLATILITY_GUARD_ATR_PERIOD", 14)))
        baseline_n = max(1, int(self.config.get("VOLATILITY_GUARD_BASELINE_CANDLES", 100)))

        if ohlc is None or not isinstance(ohlc, pd.DataFrame):
            return None, 0.0, 0.0, "no OHLC data provided"

        required = atr_period + baseline_n + 1
        if len(ohlc) < required:
            return None, 0.0, 0.0, (
                f"only {len(ohlc)} candles available, need >= {required} "
                f"(atr_period={atr_period} + baseline_candles={baseline_n} + 1)"
            )

        try:
            high = ohlc["high"].to_numpy(dtype=float)
            low = ohlc["low"].to_numpy(dtype=float)
            close = ohlc["close"].to_numpy(dtype=float)
        except (KeyError, ValueError, TypeError) as exc:
            return None, 0.0, 0.0, f"malformed OHLC columns: {exc}"

        if not (np.all(np.isfinite(high)) and np.all(np.isfinite(low))
                and np.all(np.isfinite(close))):
            # Fall back to a NaN-tolerant path rather than failing
            # outright - a single bad candle shouldn't disable the
            # guard for the whole window; pandas' rolling mean already
            # skips NaNs by default within a window as long as the
            # final values used are finite.
            high = np.where(np.isfinite(high), high, np.nan)
            low = np.where(np.isfinite(low), low, np.nan)
            close = np.where(np.isfinite(close), close, np.nan)

        # True Range - same formula used throughout analysis_engine.py's
        # ATR calculation (Tool 2 / FVG), reproduced here independently
        # so this module has zero import coupling to analysis_engine.py.
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1]),
            ),
        )

        tr_series = pd.Series(tr)
        atr_series = tr_series.rolling(window=atr_period, min_periods=atr_period).mean()
        atr_series = atr_series.dropna()

        if len(atr_series) < baseline_n + 1:
            return None, 0.0, 0.0, (
                f"only {len(atr_series)} valid ATR readings after warm-up, "
                f"need >= {baseline_n + 1}"
            )

        current_atr = float(atr_series.iloc[-1])
        baseline_atr = float(atr_series.iloc[-(baseline_n + 1):-1].mean())

        if not np.isfinite(current_atr) or not np.isfinite(baseline_atr):
            return None, 0.0, 0.0, "non-finite ATR value computed"

        if baseline_atr <= 0:
            return None, 0.0, 0.0, f"baseline ATR is zero/negative ({baseline_atr})"

        ratio = current_atr / baseline_atr
        return ratio, current_atr, baseline_atr, "ok"
