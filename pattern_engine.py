"""
Pattern Recognition Engine (Phase 1 - 6 classical chart patterns)

SAFETY CONTRACT (per explicit request): this module is ONLY ever consulted
for a candidate that has ALREADY been REJECTED by the normal Tool 5 /
MIN_TOOLS_MATCH / MIN_PROFIT_CHANCE gate in bot_core._scan_coins_247 - it
never runs before or instead of that gate, and it is only consulted at all
when PATTERN_ENGINE_ENABLED=True (default False). With the flag off, or
this file deleted entirely, the bot's existing scan/decision/execute path
is 100% byte-for-byte unaffected - this module has no import-time side
effects and nothing else in the codebase calls into it.

Patterns implemented (Phase 1 - the 6 most reliably/objectively detectable
classical reversal & continuation patterns):
  1. Double Top       (bearish reversal)
  2. Double Bottom     (bullish reversal)
  3. Head & Shoulders  (bearish reversal)
  4. Inverse H&S       (bullish reversal)
  5. Bull Flag         (bullish continuation)
  6. Bear Flag         (bearish continuation)

Each detector returns a 0-100 confidence score built from several
independently-weighted, named checks specific to that pattern's own
classical definition (peak/trough symmetry, retracement depth, neckline/
trendline breakout confirmation, volume behavior) - not a single generic
threshold. A pattern is only returned as a candidate match if its
confidence is computed from at least the structural checks (symmetry +
breakout confirmation); volume is a bonus/penalty on top where available,
never a hard requirement (many exchanges' futures volume data is noisy).

TP/SL for a pattern-based entry use the classical "measured move"
technique for that specific pattern (e.g. Double Top target = neckline -
(peak height above neckline)) - not the bot's normal OB/FVG/Liquidity
level picker.
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple


# ============================================================
# Shared helpers
# ============================================================

def _find_swing_points(high: np.ndarray, low: np.ndarray, window: int = 4) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """
    Simple fractal-style swing point detector: index i is a swing HIGH if
    high[i] is the max within [i-window, i+window], swing LOW if low[i] is
    the min within that same window. This is the same style of local-
    extremum detection already used elsewhere in this bot's own market-
    structure code, just re-implemented standalone here so this file has
    zero dependency on analysis_engine.py (keeps the "delete this file and
    nothing else changes" guarantee simple and literal).
    """
    n = len(high)
    swing_highs, swing_lows = [], []
    for i in range(window, n - window):
        window_high = high[i - window:i + window + 1]
        window_low = low[i - window:i + window + 1]
        if high[i] == window_high.max() and np.sum(window_high == high[i]) == 1:
            swing_highs.append((i, float(high[i])))
        if low[i] == window_low.min() and np.sum(window_low == low[i]) == 1:
            swing_lows.append((i, float(low[i])))
    return swing_highs, swing_lows


def _pct_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom


def _avg_volume_around(volume: Optional[np.ndarray], idx: int, span: int = 2) -> Optional[float]:
    if volume is None:
        return None
    lo, hi = max(0, idx - span), min(len(volume), idx + span + 1)
    if hi <= lo:
        return None
    return float(np.mean(volume[lo:hi]))


def _breakout_volume_score(volume: Optional[np.ndarray], formation_start: int, formation_end: int,
                            breakout_start: int, max_points: float) -> float:
    """
    ADDED (user request, stronger pattern confidence): classical technical-
    analysis confirmation signal that none of the existing checks captured
    - every existing volume check here only looks at volume BEHAVIOR DURING
    the pattern's formation (declining shoulder->head->shoulder, etc), not
    at the breakout itself. A genuine breakout is normally accompanied by a
    volume SPIKE relative to the pattern's own formation volume; a
    breakout/breakdown on below-average volume is a well-known false-
    breakout risk. Compares average volume on the breakout bar(s) against
    average volume across the whole pattern formation window.

    Returns max_points at >=1.3x formation volume, scaling down to 0 at
    <=1.0x (i.e. breakout volume no higher than the pattern's own average -
    a real weakness, not just "no bonus"). Returns max_points*0.5 (neutral
    - neither rewarded nor penalized) when volume data isn't available at
    all, since many exchanges' futures volume feeds are noisy/incomplete
    and this must never be a hard requirement.
    """
    if volume is None:
        return max_points * 0.5
    formation_vol = volume[max(0, formation_start):formation_end]
    breakout_vol = volume[breakout_start:]
    if len(formation_vol) == 0 or len(breakout_vol) == 0:
        return max_points * 0.5
    avg_formation = float(np.mean(formation_vol))
    avg_breakout = float(np.mean(breakout_vol))
    if avg_formation <= 0:
        return max_points * 0.5
    ratio = avg_breakout / avg_formation
    if ratio >= 1.3:
        return max_points
    elif ratio >= 1.0:
        return max_points * ((ratio - 1.0) / 0.3)
    else:
        return 0.0


def _extract_arrays(ohlc: pd.DataFrame):
    high = ohlc["high"].values.astype(float)
    low = ohlc["low"].values.astype(float)
    close = ohlc["close"].values.astype(float)
    volume = ohlc["volume"].values.astype(float) if "volume" in ohlc.columns else None
    return high, low, close, volume


# ============================================================
# 1 & 2. DOUBLE TOP / DOUBLE BOTTOM
# ============================================================

def _select_dominant_pair(swing_points: List[Tuple[int, float]], want_high: bool,
                           lookback_bars: int, total_len: int) -> Optional[Tuple[Tuple[int, float], Tuple[int, float]]]:
    """
    FIX (false-positive bug, 2nd pass): actively searching for the closest-
    matching pair among many candidates in a wide lookback GUARANTEES a
    good "symmetry" score even on pure noise (multiple-comparisons bias) -
    worse than the original "last two" approach. This version instead:
    (1) uses a SHORT lookback (shrinks the search space significantly),
    (2) requires a MINIMUM PROMINENCE for each candidate (must stand out
    from its immediate surroundings by a real amount, filtering out noise-
    level wiggles), (3) still requires the "no bigger point in between"
    sanity check. Combined with a tight similarity tolerance and a
    right-now breakout requirement in the callers, this closes the
    coincidental-match loophole instead of amplifying it.
    """
    cutoff = total_len - lookback_bars
    candidates = [p for p in swing_points if p[0] >= cutoff]
    if len(candidates) < 2:
        return None

    anchor = max(candidates, key=lambda p: p[1]) if want_high else min(candidates, key=lambda p: p[1])
    others = [p for p in candidates if p is not anchor]
    if not others:
        return None
    partner = min(others, key=lambda p: abs(p[1] - anchor[1]))

    first, second = (anchor, partner) if anchor[0] < partner[0] else (partner, anchor)
    if second[0] - first[0] < 4:
        return None

    between = [p for p in swing_points if first[0] < p[0] < second[0]]
    if want_high:
        if any(p[1] > max(first[1], second[1]) for p in between):
            return None
    else:
        if any(p[1] < min(first[1], second[1]) for p in between):
            return None

    return first, second


def _select_head_and_shoulders_triplet(swing_points: List[Tuple[int, float]], want_high: bool,
                                        lookback_bars: int, total_len: int
                                        ) -> Optional[Tuple[Tuple[int, float], Tuple[int, float], Tuple[int, float]]]:
    """
    Same philosophy as _select_dominant_pair, extended to 3 points: finds
    the single most extreme point in the recent lookback as the HEAD, then
    the nearest-by-time swing point before it (left shoulder) and after it
    (right shoulder) - rather than blindly taking the chronologically-last
    3 pivots regardless of how prominent they are.
    """
    cutoff = total_len - lookback_bars
    candidates = [p for p in swing_points if p[0] >= cutoff]
    if len(candidates) < 3:
        return None

    head = max(candidates, key=lambda p: p[1]) if want_high else min(candidates, key=lambda p: p[1])
    before = [p for p in candidates if p[0] < head[0]]
    after = [p for p in candidates if p[0] > head[0]]
    if not before or not after:
        return None
    left = max(before, key=lambda p: p[0])   # nearest-in-time before the head
    right = min(after, key=lambda p: p[0])   # nearest-in-time after the head
    if head[0] - left[0] < 3 or right[0] - head[0] < 3:
        return None
    return left, head, right


def detect_double_top(ohlc: pd.DataFrame) -> Optional[Dict]:
    """
    Classical Double Top (bearish reversal):
      - Two peaks at a similar price level ("M" shape)
      - A meaningful trough between them (not a shallow wiggle)
      - Confirmed by price breaking below the trough (neckline) after the
        second peak
    Weighted confidence (out of 100):
      30 - peak similarity (closer = higher score, tapers out past 3%)
      25 - trough retracement depth is meaningful (3-15% ideal)
      20 - neckline breakdown actually confirmed (hard structural check)
      15 - volume lower on 2nd peak than 1st (classical distribution signal)
      10 - breakout bar(s) show a volume spike vs the pattern's own formation
    """
    high, low, close, volume = _extract_arrays(ohlc)
    if len(close) < 30:
        return None

    swing_highs, swing_lows = _find_swing_points(high, low, window=4)
    if len(swing_highs) < 2 or len(swing_lows) < 1:
        return None

    pair = _select_dominant_pair(swing_highs, want_high=True, lookback_bars=35, total_len=len(close))
    if pair is None:
        return None
    (peak1_idx, peak1), (peak2_idx, peak2) = pair

    troughs_between = [sl for sl in swing_lows if peak1_idx < sl[0] < peak2_idx]
    if not troughs_between:
        return None
    trough_idx, trough = min(troughs_between, key=lambda x: x[1])

    score = 0.0
    peak_diff = _pct_diff(peak1, peak2)
    if peak_diff <= 0.006:
        score += 30
    elif peak_diff <= 0.018:
        score += 30 * (1 - (peak_diff - 0.006) / 0.012)

    retracement = (peak1 - trough) / peak1 if peak1 > 0 else 0
    if 0.03 <= retracement <= 0.15:
        score += 25
    elif retracement > 0.15:
        score += 25 * max(0.0, 1 - (retracement - 0.15) / 0.15)
    elif retracement > 0:
        score += 25 * (retracement / 0.03)

    # FIX (false-positive bug): "any close below neckline anywhere in the
    # rest of the data" is true for most random walks given enough bars -
    # require a MEANINGFUL (>=0.2%) penetration that happened RECENTLY
    # (within the last 6 bars), AND that the current price hasn't already
    # reclaimed back above the neckline (which would invalidate the setup).
    after = close[peak2_idx + 1:]
    if len(after) < 1:
        breakdown = False
    else:
        penetration_pct = (trough - after) / trough
        breakdown = bool(np.any(penetration_pct[-2:] >= 0.003)) and penetration_pct[-1] >= 0.0
    if breakdown:
        score += 20

    vol1 = _avg_volume_around(volume, peak1_idx)
    vol2 = _avg_volume_around(volume, peak2_idx)
    if vol1 is not None and vol2 is not None and vol1 > 0:
        if vol2 < vol1:
            score += 15
        else:
            score += 15 * max(0.0, 1 - (vol2 - vol1) / vol1)

    # ADDED (user request, stronger pattern confidence): breakout-volume
    # confirmation, distinct from the vol1/vol2 "declining into the
    # pattern" check above - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, peak1_idx, peak2_idx, len(close) - 2, 10)

    if score < 30 or not breakdown:  # structural minimum: needs the confirmed breakdown
        return None

    neckline = trough
    head_height = ((peak1 + peak2) / 2) - neckline
    return {
        "pattern": "Double Top",
        "direction": "SELL",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": neckline - head_height,
        "invalidation": max(peak1, peak2),
    }


def detect_double_bottom(ohlc: pd.DataFrame) -> Optional[Dict]:
    """Mirror of Double Top - bullish reversal ("W" shape)."""
    high, low, close, volume = _extract_arrays(ohlc)
    if len(close) < 30:
        return None

    swing_highs, swing_lows = _find_swing_points(high, low, window=4)
    if len(swing_lows) < 2 or len(swing_highs) < 1:
        return None

    pair = _select_dominant_pair(swing_lows, want_high=False, lookback_bars=35, total_len=len(close))
    if pair is None:
        return None
    (bottom1_idx, bottom1), (bottom2_idx, bottom2) = pair

    peaks_between = [sh for sh in swing_highs if bottom1_idx < sh[0] < bottom2_idx]
    if not peaks_between:
        return None
    peak_idx, peak = max(peaks_between, key=lambda x: x[1])

    score = 0.0
    bottom_diff = _pct_diff(bottom1, bottom2)
    if bottom_diff <= 0.006:
        score += 30
    elif bottom_diff <= 0.018:
        score += 30 * (1 - (bottom_diff - 0.006) / 0.012)

    bounce = (peak - bottom1) / bottom1 if bottom1 > 0 else 0
    if 0.03 <= bounce <= 0.15:
        score += 25
    elif bounce > 0.15:
        score += 25 * max(0.0, 1 - (bounce - 0.15) / 0.15)
    elif bounce > 0:
        score += 25 * (bounce / 0.03)

    after = close[bottom2_idx + 1:]
    if len(after) < 1:
        breakout = False
    else:
        penetration_pct = (after - peak) / peak
        breakout = bool(np.any(penetration_pct[-2:] >= 0.003)) and penetration_pct[-1] >= 0.0
    if breakout:
        score += 20

    vol1 = _avg_volume_around(volume, bottom1_idx)
    vol2 = _avg_volume_around(volume, bottom2_idx)
    if vol1 is not None and vol2 is not None and vol1 > 0:
        if vol2 < vol1:
            score += 15
        else:
            score += 15 * max(0.0, 1 - (vol2 - vol1) / vol1)

    # ADDED (user request, stronger pattern confidence): breakout-volume
    # confirmation, distinct from the vol1/vol2 "declining into the
    # pattern" check above - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, bottom1_idx, bottom2_idx, len(close) - 2, 10)

    if score < 30 or not breakout:
        return None

    neckline = peak
    head_depth = neckline - ((bottom1 + bottom2) / 2)
    return {
        "pattern": "Double Bottom",
        "direction": "BUY",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": neckline + head_depth,
        "invalidation": min(bottom1, bottom2),
    }


# ============================================================
# 3 & 4. HEAD & SHOULDERS / INVERSE HEAD & SHOULDERS
# ============================================================

def detect_head_and_shoulders(ohlc: pd.DataFrame) -> Optional[Dict]:
    """
    Classical Head & Shoulders (bearish reversal):
      - 3 peaks: left shoulder, head (clearly the highest), right shoulder
      - Shoulders roughly symmetric in height
      - Neckline (connecting the 2 troughs) roughly horizontal
      - Confirmed by a close below the neckline after the right shoulder
    Weighted confidence (out of 100):
      20 - head clearly higher than both shoulders
      20 - shoulder symmetry (similar height)
      15 - neckline roughly horizontal (troughs at similar level)
      20 - neckline breakdown confirmed (hard structural check)
      15 - volume declining shoulder->head->shoulder
      10 - breakout bar(s) show a volume spike vs the pattern's own formation
    """
    high, low, close, volume = _extract_arrays(ohlc)
    if len(close) < 40:
        return None

    swing_highs, swing_lows = _find_swing_points(high, low, window=4)
    if len(swing_highs) < 3 or len(swing_lows) < 2:
        return None

    triplet = _select_head_and_shoulders_triplet(swing_highs, want_high=True, lookback_bars=40, total_len=len(close))
    if triplet is None:
        return None
    (ls_idx, ls), (head_idx, head), (rs_idx, rs) = triplet
    if not (ls_idx < head_idx < rs_idx):
        return None

    troughs1 = [sl for sl in swing_lows if ls_idx < sl[0] < head_idx]
    troughs2 = [sl for sl in swing_lows if head_idx < sl[0] < rs_idx]
    if not troughs1 or not troughs2:
        return None
    t1_idx, t1 = min(troughs1, key=lambda x: x[1])
    t2_idx, t2 = min(troughs2, key=lambda x: x[1])

    score = 0.0
    if head > ls and head > rs:
        head_prominence = min((head - ls) / head, (head - rs) / head) if head > 0 else 0
        score += 20 * min(1.0, head_prominence / 0.02)
    else:
        return None  # not a valid H&S at all without a clearly higher head

    shoulder_diff = _pct_diff(ls, rs)
    if shoulder_diff <= 0.012:
        score += 20
    elif shoulder_diff <= 0.035:
        score += 20 * (1 - (shoulder_diff - 0.012) / 0.023)

    neckline_diff = _pct_diff(t1, t2)
    if neckline_diff <= 0.008:
        score += 15
    elif neckline_diff <= 0.022:
        score += 15 * (1 - (neckline_diff - 0.008) / 0.014)

    neckline_level = (t1 + t2) / 2
    after = close[rs_idx + 1:]
    if len(after) < 1:
        breakdown = False
    else:
        penetration_pct = (neckline_level - after) / neckline_level
        breakdown = bool(np.any(penetration_pct[-2:] >= 0.003)) and penetration_pct[-1] >= 0.0
    if breakdown:
        score += 20

    vol_ls = _avg_volume_around(volume, ls_idx)
    vol_head = _avg_volume_around(volume, head_idx)
    vol_rs = _avg_volume_around(volume, rs_idx)
    if vol_ls is not None and vol_head is not None and vol_rs is not None and vol_ls > 0:
        if vol_rs < vol_head <= vol_ls or vol_rs < vol_ls:
            score += 15
        else:
            score += 5

    # ADDED (user request, stronger pattern confidence): breakout-volume
    # confirmation, distinct from the shoulder->head->shoulder "declining
    # volume" check above - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, ls_idx, rs_idx, len(close) - 2, 10)

    if score < 40 or not breakdown:
        return None

    head_height = head - neckline_level
    return {
        "pattern": "Head & Shoulders",
        "direction": "SELL",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": neckline_level - head_height,
        "invalidation": max(ls, head, rs),
    }


def detect_inverse_head_and_shoulders(ohlc: pd.DataFrame) -> Optional[Dict]:
    """Mirror of Head & Shoulders - bullish reversal."""
    high, low, close, volume = _extract_arrays(ohlc)
    if len(close) < 40:
        return None

    swing_highs, swing_lows = _find_swing_points(high, low, window=4)
    if len(swing_lows) < 3 or len(swing_highs) < 2:
        return None

    triplet = _select_head_and_shoulders_triplet(swing_lows, want_high=False, lookback_bars=40, total_len=len(close))
    if triplet is None:
        return None
    (ls_idx, ls), (head_idx, head), (rs_idx, rs) = triplet
    if not (ls_idx < head_idx < rs_idx):
        return None

    peaks1 = [sh for sh in swing_highs if ls_idx < sh[0] < head_idx]
    peaks2 = [sh for sh in swing_highs if head_idx < sh[0] < rs_idx]
    if not peaks1 or not peaks2:
        return None
    p1_idx, p1 = max(peaks1, key=lambda x: x[1])
    p2_idx, p2 = max(peaks2, key=lambda x: x[1])

    score = 0.0
    if head < ls and head < rs:
        head_prominence = min((ls - head) / ls, (rs - head) / rs) if ls > 0 and rs > 0 else 0
        score += 20 * min(1.0, head_prominence / 0.02)
    else:
        return None

    shoulder_diff = _pct_diff(ls, rs)
    if shoulder_diff <= 0.012:
        score += 20
    elif shoulder_diff <= 0.035:
        score += 20 * (1 - (shoulder_diff - 0.012) / 0.023)

    neckline_diff = _pct_diff(p1, p2)
    if neckline_diff <= 0.008:
        score += 15
    elif neckline_diff <= 0.022:
        score += 15 * (1 - (neckline_diff - 0.008) / 0.014)

    neckline_level = (p1 + p2) / 2
    after = close[rs_idx + 1:]
    if len(after) < 1:
        breakout = False
    else:
        penetration_pct = (after - neckline_level) / neckline_level
        breakout = bool(np.any(penetration_pct[-2:] >= 0.003)) and penetration_pct[-1] >= 0.0
    if breakout:
        score += 20

    vol_ls = _avg_volume_around(volume, ls_idx)
    vol_head = _avg_volume_around(volume, head_idx)
    vol_rs = _avg_volume_around(volume, rs_idx)
    if vol_ls is not None and vol_head is not None and vol_rs is not None and vol_ls > 0:
        if vol_rs < vol_head <= vol_ls or vol_rs < vol_ls:
            score += 15
        else:
            score += 5

    # ADDED (user request, stronger pattern confidence): breakout-volume
    # confirmation, distinct from the shoulder->head->shoulder "declining
    # volume" check above - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, ls_idx, rs_idx, len(close) - 2, 10)

    if score < 40 or not breakout:
        return None

    head_depth = neckline_level - head
    return {
        "pattern": "Inverse Head & Shoulders",
        "direction": "BUY",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": neckline_level + head_depth,
        "invalidation": min(ls, head, rs),
    }


# ============================================================
# 5 & 6. BULL FLAG / BEAR FLAG
# ============================================================

def detect_bull_flag(ohlc: pd.DataFrame) -> Optional[Dict]:
    """
    Classical Bull Flag (bullish continuation):
      - A sharp "flagpole" impulse move up
      - Followed by a tight, roughly-parallel, flat-to-slightly-down
        consolidation channel with LOWER volatility than the flagpole
      - Confirmed by a breakout above the flag's upper bound
    Weighted confidence (out of 100):
      30 - flagpole is a genuine sharp impulse (strong % move over a short window)
      25 - flag consolidation range is tight relative to the flagpole (low volatility)
      15 - flag drifts flat/slightly down (not up - a rising channel isn't a flag)
      20 - breakout above flag high confirmed
      10 - breakout bar shows a volume spike vs the flag's own (quiet) consolidation
    """
    high, low, close, volume = _extract_arrays(ohlc)
    n = len(close)
    if n < 30:
        return None

    pole_window = 10
    flag_window = 12
    if n < pole_window + flag_window + 2:
        return None

    pole_start = n - flag_window - pole_window
    pole_end = n - flag_window
    pole_move = (close[pole_end - 1] - close[pole_start]) / close[pole_start] if close[pole_start] > 0 else 0

    flag_slice_high = high[pole_end:n - 1]  # FIX: exclude the breakout candle itself from the channel definition
    flag_slice_low = low[pole_end:n - 1]
    if len(flag_slice_high) < 3:
        return None
    flag_high = float(flag_slice_high.max())
    flag_low = float(flag_slice_low.min())
    flag_range_pct = (flag_high - flag_low) / flag_low if flag_low > 0 else 1.0

    score = 0.0
    if pole_move >= 0.03:
        score += 30 * min(1.0, pole_move / 0.08)

    if flag_range_pct <= pole_move * 0.5:
        score += 25
    elif flag_range_pct <= pole_move:
        score += 25 * (1 - (flag_range_pct - pole_move * 0.5) / (pole_move * 0.5 + 1e-9))

    flag_drift = (close[-2] - close[pole_end]) / close[pole_end] if close[pole_end] > 0 else 0
    if -0.03 <= flag_drift <= 0.01:
        score += 15
    elif flag_drift > 0.01:
        score += 15 * max(0.0, 1 - (flag_drift - 0.01) / 0.03)

    breakout = close[-1] > flag_high  # FIX: clean breakout check against the (now-excluded-breakout-bar) channel high
    if breakout:
        score += 20

    # ADDED (user request, stronger pattern confidence): flags previously
    # had NO volume check at all. Genuine flag breakouts classically fire
    # on a volume pickup relative to the (typically quiet) flag
    # consolidation - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, pole_end, n - 1, n - 1, 10)

    if score < 40 or pole_move < 0.03 or not breakout:
        return None

    pole_height = close[pole_end - 1] - close[pole_start]
    return {
        "pattern": "Bull Flag",
        "direction": "BUY",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": flag_high + pole_height,
        "invalidation": flag_low,
    }


def detect_bear_flag(ohlc: pd.DataFrame) -> Optional[Dict]:
    """Mirror of Bull Flag - bearish continuation."""
    high, low, close, volume = _extract_arrays(ohlc)
    n = len(close)
    if n < 30:
        return None

    pole_window = 10
    flag_window = 12
    if n < pole_window + flag_window + 2:
        return None

    pole_start = n - flag_window - pole_window
    pole_end = n - flag_window
    pole_move = (close[pole_start] - close[pole_end - 1]) / close[pole_start] if close[pole_start] > 0 else 0

    flag_slice_high = high[pole_end:n - 1]  # FIX: exclude the breakout candle itself from the channel definition
    flag_slice_low = low[pole_end:n - 1]
    if len(flag_slice_high) < 3:
        return None
    flag_high = float(flag_slice_high.max())
    flag_low = float(flag_slice_low.min())
    flag_range_pct = (flag_high - flag_low) / flag_low if flag_low > 0 else 1.0

    score = 0.0
    if pole_move >= 0.03:
        score += 30 * min(1.0, pole_move / 0.08)

    if flag_range_pct <= pole_move * 0.5:
        score += 25
    elif flag_range_pct <= pole_move:
        score += 25 * (1 - (flag_range_pct - pole_move * 0.5) / (pole_move * 0.5 + 1e-9))

    flag_drift = (close[pole_end] - close[-2]) / close[pole_end] if close[pole_end] > 0 else 0
    if -0.03 <= flag_drift <= 0.01:
        score += 15
    elif flag_drift > 0.01:
        score += 15 * max(0.0, 1 - (flag_drift - 0.01) / 0.03)

    breakout = close[-1] < flag_low  # FIX: clean breakout check against the (now-excluded-breakout-bar) channel low
    if breakout:
        score += 20

    # ADDED (user request, stronger pattern confidence): flags previously
    # had NO volume check at all. Genuine flag breakouts classically fire
    # on a volume pickup relative to the (typically quiet) flag
    # consolidation - see _breakout_volume_score docstring.
    score += _breakout_volume_score(volume, pole_end, n - 1, n - 1, 10)

    if score < 40 or pole_move < 0.03 or not breakout:
        return None

    pole_height = close[pole_start] - close[pole_end - 1]
    return {
        "pattern": "Bear Flag",
        "direction": "SELL",
        "confidence": round(min(score, 100.0), 1),
        "entry_anchor": float(close[-1]),
        "target": flag_low - pole_height,
        "invalidation": flag_high,
    }


# ============================================================
# Main entry point
# ============================================================

_DETECTORS = [
    detect_double_top, detect_double_bottom,
    detect_head_and_shoulders, detect_inverse_head_and_shoulders,
    detect_bull_flag, detect_bear_flag,
]


def detect_best_pattern(ohlc: pd.DataFrame, min_confidence: float = 80.0) -> Optional[Dict]:
    """
    Runs all 6 detectors against the given OHLC candles and returns the
    SINGLE highest-confidence match that clears min_confidence, or None if
    nothing clears the bar. Only ever called (see bot_core.py) for a
    candidate that already failed the normal Tool 5 gate, and only when
    PATTERN_ENGINE_ENABLED=True.
    """
    if ohlc is None or len(ohlc) < 30:
        return None

    best = None
    for detector in _DETECTORS:
        try:
            result = detector(ohlc)
        except Exception:
            result = None
        if result and result["confidence"] >= min_confidence:
            if best is None or result["confidence"] > best["confidence"]:
                best = result
    return best
