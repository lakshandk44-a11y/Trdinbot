"""
Trade-level math, kept in ONE place so the live bot (bot_core / trade_manager) and the
backtester (backtest_exit_modes.py) can never drift apart:
  * trailing stop (trailing_distance / trailing_candidate_sl / breakeven_lock_price)
  * reward/risk of a planned trade (reward_risk) - used by the R:R guard

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


_LOCK_SAFETY_MARGIN = 0.0005  # the lock must sit at least 0.05% on the safe side of the live price


def trailing_candidate_sl(side: str, entry_price: float, extreme_price: float,
                          distance: float, cfg: Dict, current_price: Optional[float] = None) -> float:
    """
    The stop the trailing logic WANTS right now (the caller only ever moves a
    stop in the profitable direction, so a worse candidate is simply ignored).
    extreme_price = highest price reached (BUY) / lowest price reached (SELL).

    current_price (live use only): the breakeven lock is skipped while price is not yet
    safely beyond it. That matters for trades that were ALREADY trailing when this logic
    was introduced (or that pulled back below the lock before it could be applied): a lock
    sitting above (BUY) / below (SELL) the live price would close the trade at once instead
    of protecting it. For a normal new trade the lock is applied the instant trailing
    activates, when price is well beyond it, so this guard never changes anything there.
    """
    side = side.upper()
    lock = breakeven_lock_price(side, entry_price, cfg)
    if lock is not None and current_price:
        if side == "BUY" and lock >= current_price * (1.0 - _LOCK_SAFETY_MARGIN):
            lock = None
        elif side == "SELL" and lock <= current_price * (1.0 + _LOCK_SAFETY_MARGIN):
            lock = None
    if side == "BUY":
        cand = extreme_price - distance
        return max(cand, lock) if lock is not None else cand
    cand = extreme_price + distance
    return min(cand, lock) if lock is not None else cand


def reward_risk(side: str, entry_price: float, dynamic_tp: Optional[float],
                dynamic_sl: Optional[float], cfg: Dict) -> Dict:
    """
    Reward/risk of a PLANNED trade, using the SAME level choice trade_manager makes when it
    creates the trade: the analysis-based level when it lies on the correct side of entry,
    otherwise the fixed TAKE_PROFIT_PERCENT / STOP_LOSS_PERCENT.

    rr = distance to the first take-profit / distance to the stop-loss.
    A trade with rr = 0.4 must win about 71% of the time just to break even (1 / (1 + rr));
    rr = 0.05 needs 95%. The first take-profit (TP1) is used on purpose: it is the target
    that is actually the base case; any extension to TP2/TP3 is conditional on a fresh analysis.
    """
    side = side.upper()
    entry = float(entry_price)
    tp_pct = float(cfg.get("TAKE_PROFIT_PERCENT", 2.0)) / 100.0
    sl_pct = float(cfg.get("STOP_LOSS_PERCENT", 1.0)) / 100.0
    if side == "BUY":
        tp = dynamic_tp if (dynamic_tp is not None and dynamic_tp > entry) else entry * (1 + tp_pct)
        sl = dynamic_sl if (dynamic_sl is not None and dynamic_sl < entry) else entry * (1 - sl_pct)
    else:
        tp = dynamic_tp if (dynamic_tp is not None and dynamic_tp < entry) else entry * (1 - tp_pct)
        sl = dynamic_sl if (dynamic_sl is not None and dynamic_sl > entry) else entry * (1 + sl_pct)
    tp_dist, sl_dist = abs(tp - entry), abs(entry - sl)
    return {
        "rr": (tp_dist / sl_dist) if sl_dist > 0 else float("inf"),  # unusable stop: never block here
        "tp": tp, "sl": sl,
        "tp_pct": tp_dist / entry * 100.0 if entry else 0.0,
        "sl_pct": sl_dist / entry * 100.0 if entry else 0.0,
    }
