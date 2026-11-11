"""
Trailing-stop math, kept in ONE place so the live bot (trade_manager) and the
backtester (backtest_exit_modes.py) can never drift apart.

Pure functions only: no network, no files, no bot state.

WHY THIS EXISTS (what was wrong with the old trailing stop)
  * Entry needs 4h + 1h + 15m to agree - a slow, big-move signal.
  * The old trailing distance was 2 x ATR of the 15m chart - a fast, small-move
    scale. Normal 15m wobble stopped the trade out early, so winners closed at
    +0.4% / +1.7% while the structure-based initial stop (much further away)
    still allowed full-size losses. Result: high win rate, tiny wins, big losses.

WHAT "ADAPTIVE" CHANGES (ADAPTIVE_EXIT_ENABLED = True)
  1. Wider, signal-scale distance: the trailing distance is the LARGER of
       - the old one:   ATR_TRAILING_MULTIPLIER x ATR(15m)
       - a new one:     TRAIL_MEDIUM_ATR_MULT   x ATR(1h)
     so it is NEVER tighter than before - only wider where the 1h chart says
     normal movement is larger.
  2. Breakeven lock: once trailing is active the stop can never sit below the
     entry (plus a small buffer that covers round-trip fees). A trade that has
     proven itself (+TRAILING_STOP_ACTIVATE %) can no longer turn into a loss.
  Entry logic, initial stop-loss and take-profit are NOT touched.

ADAPTIVE_EXIT_ENABLED = False reproduces the previous behaviour exactly.
"""
from typing import Dict, Optional


def _adaptive(cfg: Dict) -> bool:
    return bool(cfg.get("ADAPTIVE_EXIT_ENABLED", True))


def trailing_distance(entry_price: float, entry_atr_lower: Optional[float],
                      entry_atr_medium: Optional[float], cfg: Dict) -> float:
    """Distance (in price units) the stop trails behind the best price reached."""
    if entry_atr_lower:
        legacy = float(entry_atr_lower) * float(cfg.get("ATR_TRAILING_MULTIPLIER", 2.0))
    else:  # ATR was not captured for this trade: fixed-% fallback (unchanged)
        legacy = float(entry_price) * float(cfg.get("TRAILING_STOP_DISTANCE", 0.3)) / 100.0
    if not _adaptive(cfg) or not entry_atr_medium:
        return legacy
    wider = float(entry_atr_medium) * float(cfg.get("TRAIL_MEDIUM_ATR_MULT", 1.5))
    return max(legacy, wider)


def breakeven_lock_price(side: str, entry_price: float, cfg: Dict) -> Optional[float]:
    """Stop level that is worth (about) zero after fees, or None when disabled."""
    if not _adaptive(cfg) or not cfg.get("TRAIL_BREAKEVEN_LOCK", True):
        return None
    buf = float(cfg.get("BREAKEVEN_LOCK_BUFFER_PCT", 0.15)) / 100.0
    return entry_price * (1.0 + buf) if side.upper() == "BUY" else entry_price * (1.0 - buf)


def trailing_candidate_sl(side: str, entry_price: float, extreme_price: float,
                          distance: float, cfg: Dict) -> float:
    """
    The stop the trailing logic WANTS right now (the caller only ever moves a
    stop in the profitable direction, so a worse candidate is simply ignored).
    extreme_price = highest price reached (BUY) / lowest price reached (SELL).
    """
    if side.upper() == "BUY":
        cand = extreme_price - distance
        lock = breakeven_lock_price(side, entry_price, cfg)
        return max(cand, lock) if lock is not None else cand
    cand = extreme_price + distance
    lock = breakeven_lock_price(side, entry_price, cfg)
    return min(cand, lock) if lock is not None else cand
