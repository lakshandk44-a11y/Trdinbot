"""
HackerAI Auto Trading Bot - Analysis Engine
ICT/SMC + FVG + Order Blocks + Liquidity + Market Structure
Timeframes: 4h, 1h, 15m
"""

import pandas as pd
import numpy as np
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

class AnalysisEngine:
    """Multi-timeframe analysis engine with 5 ICT/SMC tools"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.timeframes = config["TIMEFRAMES"]
        self.news_api_key = config.get("NEWS_API_KEY", "")

        # CALIBRATION: if backtest_calibration.py has been run on real
        # historical data, it writes a score -> actual-win-rate table to
        # this file. Loaded once here and used (read-only) by
        # _calculate_profit_chance(). If the file doesn't exist, everything
        # behaves exactly as before - no behavior change.
        self.calibration_table_file = config.get("CALIBRATION_TABLE_FILE", "calibration_table.json")
        self._calibration_table = self._load_calibration_table()
        
    def calculate_all_indicators(self, ohlc: pd.DataFrame) -> Dict:
        """Run all 5 analysis tools on a single timeframe"""
        results = {}
        
        results["ict_smc"] = self._ict_smc_analysis(ohlc)
        results["fvg"] = self._detect_fvg(ohlc)
        results["order_block"] = self._detect_order_blocks(ohlc)
        results["liquidity"] = self._detect_liquidity(ohlc)
        results["market_structure"] = self._market_structure(ohlc)
        
        # Count how many tools agree on direction
        results["bullish_tools"] = 0
        results["bearish_tools"] = 0
        results["tools_agreeing"] = 0
        results["total_active_tools"] = 5
        
        # Tool 1
        if results["ict_smc"].get("bullish"):
            results["bullish_tools"] += 1
        elif results["ict_smc"].get("bearish"):
            results["bearish_tools"] += 1
        
        # Tool 2
        if results["fvg"].get("bullish_fvg") and not results["fvg"].get("mitigated"):
            results["bullish_tools"] += 1
        elif results["fvg"].get("bearish_fvg") and not results["fvg"].get("mitigated"):
            results["bearish_tools"] += 1
        
        # Tool 3
        if results["order_block"].get("bullish_ob"):
            results["bullish_tools"] += 1
        elif results["order_block"].get("bearish_ob"):
            results["bearish_tools"] += 1
        
        # Tool 4
        liq = results["liquidity"]
        if liq.get("recent_sweep") == "sellside":
            results["bullish_tools"] += 1
        elif liq.get("recent_sweep") == "buyside":
            results["bearish_tools"] += 1
        
        # Tool 5
        ms = results["market_structure"]
        if ms.get("trend") == "bullish" or ms.get("structure_broken") == "bullish":
            results["bullish_tools"] += 1
        elif ms.get("trend") == "bearish" or ms.get("structure_broken") == "bearish":
            results["bearish_tools"] += 1
        
        # Tools agreeing (maximum between bullish/bearish)
        results["tools_agreeing"] = max(results["bullish_tools"], results["bearish_tools"])
        
        # Signal based on tools count
        results["signal"] = self._generate_signal(results)
        results["confidence"] = self._calculate_confidence(results)
        results["profit_chance"] = self._calculate_profit_chance(results)
        
        return results
    
    def _ict_smc_analysis(self, ohlc: pd.DataFrame) -> Dict:
        """
        Tool 1: ICT / Smart Money Concepts Analysis

        Implements the full set of ICT/SMC concepts:
          - Swing High / Low: validated pivot points used as structural reference levels.
          - BOS (Break of Structure): price breaks through a prior swing high/low
            in the direction of the existing trend, confirming continuation.
          - CHoCH (Change of Character): price breaks through a prior swing high/low
            AGAINST the existing trend — the first signal of a potential reversal.
          - MSS (Market Structure Shift): a displacement candle that decisively
            breaks structure after a CHoCH, confirming the reversal with momentum.
          - Displacement: an expansion candle/range that is significantly
            larger than the recent average range (real momentum, not just a
            big body).
          - Premium / Discount (PD) array: where price currently sits inside
            its recent trading range (top half = premium / sell zone,
            bottom half = discount / buy zone).
          - Optimal Trade Entry (OTE): the classic 61.8%-79% retracement
            zone of the most recent swing, where ICT-style entries are
            favored.
          - Volume-confirmed displacement, used as an approximation for
            inducement (a liquidity grab followed by a strong reversal move).
        """
        result = {
            "bullish": False, "bearish": False, "strength": 0,
            "displacement": False, "pd_zone": None, "ote": False,
            # ---- New fields ----
            "swing_highs": [],    # list of recent swing-high price levels
            "swing_lows": [],     # list of recent swing-low price levels
            "last_swing_high": None,
            "last_swing_low": None,
            "bos": False,         # Break of Structure occurred
            "bos_direction": None,  # "bullish" | "bearish"
            "choch": False,       # Change of Character occurred
            "choch_direction": None,  # "bullish" | "bearish"
            "mss": False,         # Market Structure Shift confirmed
            "mss_direction": None,  # "bullish" | "bearish"
        }

        if len(ohlc) < 50:
            return result

        close = ohlc["close"].values
        high = ohlc["high"].values
        low = ohlc["low"].values
        open_p = ohlc["open"].values
        volume = ohlc["volume"].values if "volume" in ohlc.columns else None

        # ================================================================
        # 1. SWING HIGH / LOW DETECTION
        #    A swing high: candle[i].high is the highest of (i-2,i-1,i,i+1,i+2).
        #    A swing low:  candle[i].low  is the lowest  of (i-2,i-1,i,i+1,i+2).
        #    We scan the last 60 candles (excluding the live/forming candle).
        # ================================================================
        lookback = min(60, len(ohlc) - 1)  # leave -1 for the live candle
        pivot_n = 2                          # bars on each side required

        swing_high_levels = []
        swing_low_levels = []

        for i in range(pivot_n, lookback - pivot_n):
            idx = -(lookback - i)            # negative index into the array

            # Swing High: idx.high strictly greater than surrounding bars
            is_sh = all(
                high[idx] > high[idx - k] for k in range(1, pivot_n + 1)
            ) and all(
                high[idx] > high[idx + k] for k in range(1, pivot_n + 1)
            )
            if is_sh:
                swing_high_levels.append(float(high[idx]))

            # Swing Low: idx.low strictly less than surrounding bars
            is_sl = all(
                low[idx] < low[idx - k] for k in range(1, pivot_n + 1)
            ) and all(
                low[idx] < low[idx + k] for k in range(1, pivot_n + 1)
            )
            if is_sl:
                swing_low_levels.append(float(low[idx]))

        result["swing_highs"] = swing_high_levels
        result["swing_lows"] = swing_low_levels
        result["last_swing_high"] = swing_high_levels[-1] if swing_high_levels else None
        result["last_swing_low"] = swing_low_levels[-1] if swing_low_levels else None

        # ================================================================
        # 2. TREND CONTEXT (needed to distinguish BOS vs CHoCH)
        #    Simple: compare average of recent 10 closes vs recent 30 closes.
        # ================================================================
        ema_short = np.mean(close[-10:])
        ema_long = np.mean(close[-30:]) if len(close) >= 30 else np.mean(close)
        trend_bullish = ema_short > ema_long * 1.003
        trend_bearish = ema_short < ema_long * 0.997

        # ================================================================
        # 3. BOS — Break of Structure
        #    Bullish BOS: close breaks above a prior swing high IN a bullish trend.
        #    Bearish BOS: close breaks below a prior swing low IN a bearish trend.
        # ================================================================
        if swing_high_levels and trend_bullish:
            # Use the most recent swing high as the reference level
            ref_sh = swing_high_levels[-1]
            if close[-1] > ref_sh:
                result["bos"] = True
                result["bos_direction"] = "bullish"
                result["bullish"] = True
                result["strength"] += 2

        if swing_low_levels and trend_bearish and not result["bos"]:
            ref_sl = swing_low_levels[-1]
            if close[-1] < ref_sl:
                result["bos"] = True
                result["bos_direction"] = "bearish"
                result["bearish"] = True
                result["strength"] -= 2

        # ================================================================
        # 4. CHoCH — Change of Character
        #    Bullish CHoCH: price breaks above a swing high while trend is bearish
        #                   (counter-trend break → potential reversal to bullish).
        #    Bearish CHoCH: price breaks below a swing low while trend is bullish
        #                   (counter-trend break → potential reversal to bearish).
        # ================================================================
        if swing_high_levels and trend_bearish:
            ref_sh = swing_high_levels[-1]
            if close[-1] > ref_sh:
                result["choch"] = True
                result["choch_direction"] = "bullish"
                result["bullish"] = True
                result["strength"] += 2

        if swing_low_levels and trend_bullish and not result["choch"]:
            ref_sl = swing_low_levels[-1]
            if close[-1] < ref_sl:
                result["choch"] = True
                result["choch_direction"] = "bearish"
                result["bearish"] = True
                result["strength"] -= 2

        # ================================================================
        # 5. MSS — Market Structure Shift
        #    Confirmation of CHoCH: a displacement candle (strong body, above-
        #    average range) occurs immediately after the CHoCH signal and closes
        #    clearly beyond the broken structural level.  This is the "shift"
        #    that separates a genuine reversal from a fake-out.
        # ================================================================
        recent_ranges = high[-31:-1] - low[-31:-1]
        avg_range = np.mean(recent_ranges) if len(recent_ranges) > 0 else 0.0
        last_range = high[-1] - low[-1]
        last_body = abs(close[-1] - open_p[-1])
        is_displacement_candle = bool(
            avg_range > 0
            and last_range > avg_range * 1.5
            and last_body > last_range * 0.6
        )

        if result["choch"] and is_displacement_candle:
            result["mss"] = True
            result["mss_direction"] = result["choch_direction"]
            # MSS adds extra conviction — bump strength an additional point
            if result["choch_direction"] == "bullish":
                result["strength"] += 1
            elif result["choch_direction"] == "bearish":
                result["strength"] -= 1

        # ================================================================
        # 6. DISPLACEMENT (standalone — already used above for MSS check)
        # ================================================================
        result["displacement"] = is_displacement_candle

        # ================================================================
        # 7. PREMIUM / DISCOUNT ARRAY
        # ================================================================
        swing_high_pd = np.max(high[-40:])
        swing_low_pd = np.min(low[-40:])
        range_size = swing_high_pd - swing_low_pd

        pd_zone = None
        ote = False
        if range_size > 0:
            position_in_range = (close[-1] - swing_low_pd) / range_size
            pd_zone = "discount" if position_in_range <= 0.5 else "premium"

            # Optimal Trade Entry: retracement sitting in the classic 61.8%-79% zone,
            # measured from whichever side of the range price retraced from.
            retracement_from_high = (swing_high_pd - close[-1]) / range_size
            retracement_from_low = (close[-1] - swing_low_pd) / range_size
            if 0.618 <= retracement_from_high <= 0.79 or 0.618 <= retracement_from_low <= 0.79:
                ote = True

        result["pd_zone"] = pd_zone
        result["ote"] = ote

        # ---- Directional bias: displacement aligned with PD array location ----
        if is_displacement_candle and close[-1] > open_p[-1] and pd_zone == "discount":
            result["bullish"] = True
            result["strength"] += 2
        elif is_displacement_candle and close[-1] < open_p[-1] and pd_zone == "premium":
            result["bearish"] = True
            result["strength"] -= 2

        # OTE confluence: buying/selling from the classic retracement zone adds strength
        if ote and pd_zone == "discount" and close[-1] > open_p[-1]:
            result["bullish"] = True
            result["strength"] += 1
        elif ote and pd_zone == "premium" and close[-1] < open_p[-1]:
            result["bearish"] = True
            result["strength"] -= 1

        # ================================================================
        # 8. VOLUME-CONFIRMED DISPLACEMENT
        #    (approximates inducement -> reversal displacement)
        # ================================================================
        if volume is not None and len(volume) >= 31:
            avg_vol = np.mean(volume[-31:-1])
            if avg_vol > 0 and volume[-1] > avg_vol * 1.5 and is_displacement_candle:
                if close[-1] > open_p[-1]:
                    result["bullish"] = True
                    result["strength"] += 2
                elif close[-1] < open_p[-1]:
                    result["bearish"] = True
                    result["strength"] -= 2

        return result
    
    def _detect_fvg(self, ohlc: pd.DataFrame) -> Dict:
        """
        Tool 2: Fair Value Gap (FVG) Detection

        Implements the full FVG concept set:
          - Real FVG: classic 3-candle imbalance where candle[i-1].high <
            candle[i+1].low (bullish) or candle[i-1].low > candle[i+1].high
            (bearish).  Only gaps larger than the ATR filter threshold are kept.
          - ATR Filter: gaps smaller than 0.25 * ATR(14) are noise — ignored.
            This ensures only institutionally-relevant imbalances are flagged.
          - Mitigation: a gap is mitigated once price fully trades back into it
            (close inside the gap zone).
          - IFVG (Inverse FVG): a previously mitigated FVG that price later
            reverses from.  The gap that was once support / resistance flips
            polarity — a mitigated bullish FVG becomes a bearish IFVG level
            (resistance), and vice versa.  This is the ICT "inversion" concept.
        """
        result = {
            "bullish_fvg": False,
            "bearish_fvg": False,
            "fvg_levels": [],
            "mitigated": False,
            # ---- New fields ----
            "ifvg_bullish": False,   # inverted (was bearish FVG, now acts as support)
            "ifvg_bearish": False,   # inverted (was bullish FVG, now acts as resistance)
            "ifvg_levels": [],       # list of IFVG zones with polarity
            "atr": None,             # ATR(14) value used for filtering
        }

        if len(ohlc) < 20:
            return result

        high  = ohlc["high"].values
        low   = ohlc["low"].values
        close = ohlc["close"].values

        # ================================================================
        # ATR(14) — used as size filter and stored for caller reference
        # ================================================================
        atr_period = 14
        if len(ohlc) >= atr_period + 1:
            tr_vals = np.maximum(
                high[1:] - low[1:],
                np.maximum(
                    np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:]  - close[:-1])
                )
            )
            atr = float(np.mean(tr_vals[-atr_period:]))
        else:
            atr = float(np.mean(high - low))
        result["atr"] = atr
        min_gap_size = atr * 0.25   # gaps smaller than this are filtered out

        # ================================================================
        # REAL FVG SCAN (last 40 candles, leave live candle out)
        # ================================================================
        scan_end = min(40, len(ohlc) - 1)   # -1: skip the live/forming candle

        for i in range(2, scan_end):
            # --- Bullish FVG: candle[i-1].high < candle[i+1].low ---
            # In negative indexing: candle at -(i+1), pivot -(i), candle -(i-1)
            gap_low  = high[-(i + 1)]
            gap_high = low[-(i - 1)]

            if gap_high > gap_low:
                gap_size = gap_high - gap_low
                if gap_size >= min_gap_size:
                    # Mitigation: any subsequent close inside the gap zone
                    mitigated = any(
                        gap_low <= close[-(j)] <= gap_high
                        for j in range(1, i)          # candles after the gap formed
                    )
                    entry = {
                        "type": "bullish",
                        "high": float(gap_high),
                        "low":  float(gap_low),
                        "size": float(gap_size),
                        "mitigated": mitigated,
                    }
                    result["fvg_levels"].append(entry)
                    if not mitigated:
                        result["bullish_fvg"] = True

                    # IFVG: mitigated bullish FVG → now acts as bearish resistance
                    if mitigated:
                        # Confirm inversion: price must have bounced down FROM the gap
                        # after mitigation (close below gap_low in a candle after entry)
                        inverted = any(
                            close[-(j)] < gap_low
                            for j in range(1, i)
                        )
                        if inverted:
                            result["ifvg_bearish"] = True
                            result["ifvg_levels"].append({
                                "type": "ifvg_bearish",
                                "high": float(gap_high),
                                "low":  float(gap_low),
                                "size": float(gap_size),
                            })

            # --- Bearish FVG: candle[i-1].low > candle[i+1].high ---
            gap_high2 = low[-(i + 1)]
            gap_low2  = high[-(i - 1)]

            if gap_high2 > gap_low2:
                gap_size2 = gap_high2 - gap_low2
                if gap_size2 >= min_gap_size:
                    mitigated2 = any(
                        gap_low2 <= close[-(j)] <= gap_high2
                        for j in range(1, i)
                    )
                    entry2 = {
                        "type": "bearish",
                        "high": float(gap_high2),
                        "low":  float(gap_low2),
                        "size": float(gap_size2),
                        "mitigated": mitigated2,
                    }
                    result["fvg_levels"].append(entry2)
                    if not mitigated2:
                        result["bearish_fvg"] = True

                    # IFVG: mitigated bearish FVG → now acts as bullish support
                    if mitigated2:
                        inverted2 = any(
                            close[-(j)] > gap_high2
                            for j in range(1, i)
                        )
                        if inverted2:
                            result["ifvg_bullish"] = True
                            result["ifvg_levels"].append({
                                "type": "ifvg_bullish",
                                "high": float(gap_high2),
                                "low":  float(gap_low2),
                                "size": float(gap_size2),
                            })

        # Overall mitigated flag: True only if ALL found gaps are mitigated
        if result["fvg_levels"]:
            result["mitigated"] = all(f["mitigated"] for f in result["fvg_levels"])

        return result
    
    def _detect_order_blocks(self, ohlc: pd.DataFrame) -> Dict:
        """
        Tool 3: Order Block Detection

        Implements the full OB concept set:
          - Bullish OB: the last bearish candle before a significant bullish
            impulse move. Price is expected to return to this zone as support.
          - Bearish OB: the last bullish candle before a significant bearish
            impulse move. Price is expected to return to this zone as resistance.
          - Breaker Block: an OB that price has fully traded through (violating
            it as support/resistance). Once broken, it FLIPS polarity —
            a bullish OB that gets broken becomes bearish resistance (bearish
            breaker), and vice versa. Breaker blocks are high-probability
            re-entry zones.
          - Retest: price has returned to touch an unmitigated OB zone after
            the initial move away from it. A retest of a valid OB is the
            preferred entry trigger in ICT methodology.
        """
        result = {
            "bullish_ob": None,
            "bearish_ob": None,
            "ob_levels": [],
            # ---- New fields ----
            "breaker_bullish": None,   # bearish OB that got broken -> now support
            "breaker_bearish": None,   # bullish OB that got broken -> now resistance
            "breaker_levels": [],
            "retest_bullish": False,   # price is currently retesting a bullish OB
            "retest_bearish": False,   # price is currently retesting a bearish OB
        }

        if len(ohlc) < 10:
            return result

        open_p = ohlc["open"].values
        close  = ohlc["close"].values
        high   = ohlc["high"].values
        low    = ohlc["low"].values

        # ================================================================
        # STEP 1 -- Identify Order Blocks (last 40 candles)
        # An OB is the candle BEFORE a strong impulse in the opposite direction.
        # The impulse candle must: have a strong body ratio (>0.55), and close
        # decisively beyond the OB candle's body.
        # ================================================================
        scan_range = min(40, len(ohlc) - 3)

        for i in range(3, scan_range):
            ob_idx   = -(i + 1)   # the OB candle
            move_idx = -i          # first impulse candle after the OB

            if ob_idx < -len(ohlc) or move_idx < -len(ohlc):
                continue

            move_body  = abs(close[move_idx] - open_p[move_idx])
            move_range = high[move_idx] - low[move_idx]
            if move_range == 0:
                continue
            move_body_ratio = move_body / move_range

            # Bullish OB: OB candle is bearish, followed by strong bullish impulse
            if (open_p[ob_idx] > close[ob_idx]             # OB candle bearish
                    and close[move_idx] > open_p[move_idx] # impulse bullish
                    and move_body_ratio > 0.55              # strong body
                    and close[move_idx] > high[ob_idx]):    # closes above OB high
                ob_candle = {
                    "type":  "bullish",
                    "high":  float(high[ob_idx]),
                    "low":   float(low[ob_idx]),
                    "open":  float(open_p[ob_idx]),
                    "close": float(close[ob_idx]),
                    "level": float((high[ob_idx] + low[ob_idx]) / 2),
                }
                if result["bullish_ob"] is None:
                    result["bullish_ob"] = ob_candle
                result["ob_levels"].append(ob_candle)

            # Bearish OB: OB candle is bullish, followed by strong bearish impulse
            elif (open_p[ob_idx] < close[ob_idx]            # OB candle bullish
                    and close[move_idx] < open_p[move_idx]  # impulse bearish
                    and move_body_ratio > 0.55               # strong body
                    and close[move_idx] < low[ob_idx]):      # closes below OB low
                ob_candle = {
                    "type":  "bearish",
                    "high":  float(high[ob_idx]),
                    "low":   float(low[ob_idx]),
                    "open":  float(open_p[ob_idx]),
                    "close": float(close[ob_idx]),
                    "level": float((high[ob_idx] + low[ob_idx]) / 2),
                }
                if result["bearish_ob"] is None:
                    result["bearish_ob"] = ob_candle
                result["ob_levels"].append(ob_candle)

        # ================================================================
        # STEP 2 -- Breaker Block detection
        # Bullish OB becomes Bearish Breaker if price later closes BELOW OB low.
        # Bearish OB becomes Bullish Breaker if price later closes ABOVE OB high.
        # Checked against the most recent 10 closes.
        # ================================================================
        recent_closes = close[-10:]

        for ob in result["ob_levels"]:
            if ob["type"] == "bullish":
                broken = any(c < ob["low"] for c in recent_closes)
                if broken:
                    breaker = {
                        "type":        "breaker_bearish",
                        "origin_type": "bullish_ob",
                        "high":        ob["high"],
                        "low":         ob["low"],
                        "level":       ob["level"],
                    }
                    if result["breaker_bearish"] is None:
                        result["breaker_bearish"] = breaker
                    result["breaker_levels"].append(breaker)

            elif ob["type"] == "bearish":
                broken = any(c > ob["high"] for c in recent_closes)
                if broken:
                    breaker = {
                        "type":        "breaker_bullish",
                        "origin_type": "bearish_ob",
                        "high":        ob["high"],
                        "low":         ob["low"],
                        "level":       ob["level"],
                    }
                    if result["breaker_bullish"] is None:
                        result["breaker_bullish"] = breaker
                    result["breaker_levels"].append(breaker)

        # ================================================================
        # STEP 3 -- Retest detection
        # Price is retesting a bullish OB if the current candle's low dips into
        # the OB zone but the close holds above OB low (touched, not broken).
        # Mirror logic for bearish OB. Only unbroken OBs are checked.
        # ================================================================
        breaker_keys = {(b["high"], b["low"]) for b in result["breaker_levels"]}

        for ob in result["ob_levels"]:
            if (ob["high"], ob["low"]) in breaker_keys:
                continue   # already a breaker -- skip

            if ob["type"] == "bullish":
                if low[-1] <= ob["high"] and close[-1] >= ob["low"]:
                    result["retest_bullish"] = True

            elif ob["type"] == "bearish":
                if high[-1] >= ob["low"] and close[-1] <= ob["high"]:
                    result["retest_bearish"] = True

        return result

    def _detect_liquidity(self, ohlc: pd.DataFrame) -> Dict:
        """
        Tool 4: Liquidity Detection & Sweep Analysis

        Implements the full liquidity concept set:
          - EQH / EQL (Equal Highs / Equal Lows): two or more swing highs (or
            lows) sitting within a tight band (0.15% of price) of each other.
            These clusters signal resting buy-side (above EQH) or sell-side
            (below EQL) liquidity that the market is likely to hunt.
          - Liquidity Sweep: price momentarily trades through a swing high/low
            level but closes back on the opposite side -- the classic "stop hunt"
            or "liquidity grab" that precedes a reversal move.
          - External Liquidity: the major swing highs/lows of the broader recent
            range (last 50-60 candles) -- the "obvious" levels retail traders
            place stops at. Smart money targets these first.
          - Internal Liquidity: shorter-term swing highs/lows within the current
            leg (last 15-20 candles). These get swept on pullbacks / corrections
            before the next leg in the primary direction continues.
        """
        result = {
            "buyside_liquidity":  None,
            "sellside_liquidity": None,
            "swept":              False,
            "recent_sweep":       None,
            # ---- New fields ----
            "eqh": [],                   # Equal High levels (buy-side liquidity pools)
            "eql": [],                   # Equal Low levels  (sell-side liquidity pools)
            "eqh_detected": False,
            "eql_detected": False,
            "external_liquidity_high": None,  # major swing high (external)
            "external_liquidity_low":  None,  # major swing low  (external)
            "internal_liquidity_high": None,  # recent leg swing high (internal)
            "internal_liquidity_low":  None,  # recent leg swing low  (internal)
            "internal_swept": False,     # internal level got swept
            "external_swept": False,     # external level got swept
        }

        if len(ohlc) < 30:
            return result

        high  = ohlc["high"].values
        low   = ohlc["low"].values
        close = ohlc["close"].values

        # ================================================================
        # STEP 1 -- Detect swing highs & lows (3-bar pivot, last 60 candles)
        # ================================================================
        ext_lookback = min(60, len(ohlc) - 1)
        int_lookback = min(20, len(ohlc) - 1)
        pivot_n = 3

        ext_swing_highs = []
        ext_swing_lows  = []

        for i in range(pivot_n, ext_lookback - pivot_n):
            idx = -(ext_lookback - i)
            is_sh = (all(high[idx] > high[idx - k] for k in range(1, pivot_n + 1))
                     and all(high[idx] > high[idx + k] for k in range(1, pivot_n + 1)))
            if is_sh:
                ext_swing_highs.append(float(high[idx]))

            is_sl = (all(low[idx] < low[idx - k] for k in range(1, pivot_n + 1))
                     and all(low[idx] < low[idx + k] for k in range(1, pivot_n + 1)))
            if is_sl:
                ext_swing_lows.append(float(low[idx]))

        # Internal: last 20 candles only (2-bar pivot)
        int_swing_highs = []
        int_swing_lows  = []
        int_pivot = 2

        for i in range(int_pivot, int_lookback - int_pivot):
            idx = -(int_lookback - i)
            is_sh = (all(high[idx] > high[idx - k] for k in range(1, int_pivot + 1))
                     and all(high[idx] > high[idx + k] for k in range(1, int_pivot + 1)))
            if is_sh:
                int_swing_highs.append(float(high[idx]))

            is_sl = (all(low[idx] < low[idx - k] for k in range(1, int_pivot + 1))
                     and all(low[idx] < low[idx + k] for k in range(1, int_pivot + 1)))
            if is_sl:
                int_swing_lows.append(float(low[idx]))

        # ================================================================
        # STEP 2 -- EQH / EQL (Equal Highs / Equal Lows)
        # Two swing highs within 0.15% of each other = EQH (buy-side pool).
        # Two swing lows within 0.15% of each other  = EQL (sell-side pool).
        # ================================================================
        eq_tolerance = 0.0015   # 0.15%

        eqh_levels = []
        for j in range(len(ext_swing_highs)):
            for k in range(j + 1, len(ext_swing_highs)):
                lvl_j, lvl_k = ext_swing_highs[j], ext_swing_highs[k]
                if lvl_j > 0 and abs(lvl_j - lvl_k) / lvl_j <= eq_tolerance:
                    mid = (lvl_j + lvl_k) / 2
                    if not any(abs(mid - e) / mid <= eq_tolerance for e in eqh_levels):
                        eqh_levels.append(mid)

        eql_levels = []
        for j in range(len(ext_swing_lows)):
            for k in range(j + 1, len(ext_swing_lows)):
                lvl_j, lvl_k = ext_swing_lows[j], ext_swing_lows[k]
                if lvl_j > 0 and abs(lvl_j - lvl_k) / lvl_j <= eq_tolerance:
                    mid = (lvl_j + lvl_k) / 2
                    if not any(abs(mid - e) / mid <= eq_tolerance for e in eql_levels):
                        eql_levels.append(mid)

        result["eqh"] = eqh_levels
        result["eql"] = eql_levels
        result["eqh_detected"] = len(eqh_levels) > 0
        result["eql_detected"] = len(eql_levels) > 0

        # ================================================================
        # STEP 3 -- External Liquidity (major swing high/low of broad range)
        # ================================================================
        if ext_swing_highs:
            result["external_liquidity_high"] = max(ext_swing_highs)
        if ext_swing_lows:
            result["external_liquidity_low"] = min(ext_swing_lows)

        # ================================================================
        # STEP 4 -- Internal Liquidity (recent leg swing high/low)
        # ================================================================
        if int_swing_highs:
            result["internal_liquidity_high"] = max(int_swing_highs)
        if int_swing_lows:
            result["internal_liquidity_low"] = min(int_swing_lows)

        # ================================================================
        # STEP 5 -- Liquidity Sweep detection
        # Classic sweep: high[-1] pokes above a swing high but close[-1] is
        # back below it (stop hunt above the level, reversal follows).
        # Mirror for sell-side.
        # ================================================================
        # -- External sweeps --
        ext_h = result["external_liquidity_high"]
        ext_l = result["external_liquidity_low"]

        if ext_h is not None and high[-1] >= ext_h and close[-1] < ext_h:
            result["buyside_liquidity"]  = ext_h
            result["swept"]              = True
            result["recent_sweep"]       = "buyside"
            result["external_swept"]     = True

        if ext_l is not None and low[-1] <= ext_l and close[-1] > ext_l:
            result["sellside_liquidity"] = ext_l
            result["swept"]              = True
            result["recent_sweep"]       = "sellside"
            result["external_swept"]     = True

        # -- Internal sweeps (only if no external sweep already confirmed) --
        int_h = result["internal_liquidity_high"]
        int_l = result["internal_liquidity_low"]

        if not result["swept"]:
            if int_h is not None and high[-1] >= int_h and close[-1] < int_h:
                result["buyside_liquidity"] = int_h
                result["swept"]             = True
                result["recent_sweep"]      = "buyside"
                result["internal_swept"]    = True

            if int_l is not None and low[-1] <= int_l and close[-1] > int_l:
                result["sellside_liquidity"] = int_l
                result["swept"]              = True
                result["recent_sweep"]       = "sellside"
                result["internal_swept"]     = True

        # -- EQH / EQL sweeps (also count as buy/sell-side sweeps) --
        for eqh_lvl in eqh_levels:
            if high[-1] >= eqh_lvl and close[-1] < eqh_lvl:
                result["buyside_liquidity"] = eqh_lvl
                result["swept"]             = True
                result["recent_sweep"]      = "buyside"
                break

        for eql_lvl in eql_levels:
            if low[-1] <= eql_lvl and close[-1] > eql_lvl:
                result["sellside_liquidity"] = eql_lvl
                result["swept"]              = True
                result["recent_sweep"]       = "sellside"
                break

        return result

    def _market_structure(self, ohlc: pd.DataFrame) -> Dict:
        """
        Tool 5: Market Structure Analysis

        Implements the full market structure concept set:
          - Trend: overall directional bias derived from a swing-based higher
            highs / higher lows (bullish) or lower highs / lower lows (bearish)
            count over the recent 50 candles, weighted with EMA alignment.
          - BOS (Break of Structure): close beyond a previous swing high/low
            in the SAME direction as the trend -- confirms continuation.
          - CHoCH (Change of Character): close beyond a previous swing high/low
            AGAINST the trend -- early warning of a potential reversal.
          - Confidence: a 0-100 score that combines trend clarity, BOS/CHoCH
            quality, and how many recent swing pivots confirm the structure.
            Exposed on the result dict so callers and the profit-chance
            formula can use it directly.
        """
        result = {
            "trend":             "neutral",
            "bos":               False,
            "choch":             False,
            "structure_broken":  None,
            "last_bos_direction": None,
            # ---- New field ----
            "confidence":        0.0,   # 0.0 - 1.0 structural confidence score
        }

        if len(ohlc) < 20:
            return result

        close = ohlc["close"].values
        high  = ohlc["high"].values
        low   = ohlc["low"].values

        # ================================================================
        # STEP 1 -- Trend via swing structure (HH/HL or LH/LL count)
        # Detect pivot highs and lows over last 50 candles (2-bar pivot).
        # ================================================================
        lookback  = min(50, len(ohlc) - 1)
        pivot_n   = 2
        p_highs   = []
        p_lows    = []

        for i in range(pivot_n, lookback - pivot_n):
            idx = -(lookback - i)
            if (all(high[idx] > high[idx - k] for k in range(1, pivot_n + 1))
                    and all(high[idx] > high[idx + k] for k in range(1, pivot_n + 1))):
                p_highs.append(float(high[idx]))
            if (all(low[idx] < low[idx - k] for k in range(1, pivot_n + 1))
                    and all(low[idx] < low[idx + k] for k in range(1, pivot_n + 1))):
                p_lows.append(float(low[idx]))

        # Count HH/HL vs LH/LL sequences
        hh_count = sum(1 for j in range(1, len(p_highs)) if p_highs[j] > p_highs[j-1])
        hl_count = sum(1 for j in range(1, len(p_lows))  if p_lows[j]  > p_lows[j-1])
        lh_count = sum(1 for j in range(1, len(p_highs)) if p_highs[j] < p_highs[j-1])
        ll_count = sum(1 for j in range(1, len(p_lows))  if p_lows[j]  < p_lows[j-1])

        bullish_score = hh_count + hl_count
        bearish_score = lh_count + ll_count

        # EMA alignment (secondary confirmation)
        ema_short = np.mean(close[-10:])
        ema_long  = np.mean(close[-30:]) if len(close) >= 30 else np.mean(close)
        ema_bullish = ema_short > ema_long * 1.003
        ema_bearish = ema_short < ema_long * 0.997

        if bullish_score > bearish_score and ema_bullish:
            result["trend"] = "bullish"
        elif bearish_score > bullish_score and ema_bearish:
            result["trend"] = "bearish"
        elif bullish_score > bearish_score:
            result["trend"] = "bullish"   # swing structure overrides EMA if clear
        elif bearish_score > bullish_score:
            result["trend"] = "bearish"

        # ================================================================
        # STEP 2 -- BOS (Break of Structure)
        # Price closes beyond the most recent swing high/low in the trend dir.
        # ================================================================
        ref_high = p_highs[-1] if p_highs else (max(high[-15:-5]) if len(high) >= 15 else max(high))
        ref_low  = p_lows[-1]  if p_lows  else (min(low[-15:-5])  if len(low)  >= 15 else min(low))

        if close[-1] > ref_high and result["trend"] == "bullish":
            result["bos"]               = True
            result["structure_broken"]  = "bullish"
            result["last_bos_direction"] = "up"

        if close[-1] < ref_low and result["trend"] == "bearish":
            result["bos"]               = True
            result["structure_broken"]  = "bearish"
            result["last_bos_direction"] = "down"

        # ================================================================
        # STEP 3 -- CHoCH (Change of Character)
        # Price closes against the trend beyond a swing high/low.
        # ================================================================
        if close[-1] > ref_high and result["trend"] == "bearish":
            result["choch"]              = True
            result["structure_broken"]   = "bullish_choch"

        if close[-1] < ref_low and result["trend"] == "bullish":
            result["choch"]              = True
            result["structure_broken"]   = "bearish_choch"

        # ================================================================
        # STEP 4 -- Confidence score (0.0 - 1.0)
        # Components:
        #   - Swing structure clarity: ratio of dominant direction swings
        #   - EMA alignment with swing direction
        #   - BOS presence (+0.20) / CHoCH presence (+0.10)
        #   - How many swing pivots were found (more = clearer structure)
        # ================================================================
        total_swings = bullish_score + bearish_score
        structure_clarity = (
            (max(bullish_score, bearish_score) / total_swings)
            if total_swings > 0 else 0.5
        )

        ema_aligned = (
            (ema_bullish and result["trend"] == "bullish")
            or (ema_bearish and result["trend"] == "bearish")
        )

        pivot_coverage = min(len(p_highs) + len(p_lows), 10) / 10.0

        conf = (
            structure_clarity * 0.45
            + (0.20 if ema_aligned else 0.0)
            + (0.20 if result["bos"]   else 0.0)
            + (0.10 if result["choch"] else 0.0)
            + pivot_coverage * 0.05
        )
        result["confidence"] = round(min(conf, 1.0), 3)

        return result

    
    def _generate_signal(self, results: Dict) -> int:
        """Generate signal: 1=BUY, -1=SELL, 0=HOLD"""
        # Use the tools_agreeing count from calculate_all_indicators
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
        
        if results["bullish_tools"] >= min_tools and results["bullish_tools"] > results["bearish_tools"]:
            return 1
        elif results["bearish_tools"] >= min_tools and results["bearish_tools"] > results["bullish_tools"]:
            return -1
        else:
            return 0
    
    def _calculate_confidence(self, results: Dict) -> float:
        """Calculate confidence 0.0 to 1.0"""
        total = results.get("total_active_tools", 5)
        agreeing = results.get("tools_agreeing", 0)
        if total > 0:
            return agreeing / total
        return 0.0
    
    def _calculate_profit_chance(self, results: Dict) -> float:
        """
        Calculate an estimated profit-chance score based on confluence strength.

        FIX (Fake Math bug): the old formula was `50 + agreeing_tools * 8`.
        Since MIN_TOOLS_MATCH defaults to 3, that meant 3 agreeing tools
        ALWAYS produced 50 + 24 = 74%, which is already above the 65%
        MIN_PROFIT_CHANCE filter — so the "profit chance" gate was never
        actually able to reject a trade that already passed the tools-match
        gate. It wasn't a real independent statistical estimate.

        This version scores confluence out of 100 using components that are
        NOT guaranteed to be satisfied just because MIN_TOOLS_MATCH is met,
        so the profit-chance filter can genuinely reject weak setups even
        when 3+ tools agree. This is still a heuristic score (not a
        backtested statistical probability) — it should be tuned/validated
        against real trade history, not treated as a guarantee.
        """
        total_tools = results.get("total_active_tools", 5) or 5
        agreeing = results.get("tools_agreeing", 0)
        tool_ratio = agreeing / total_tools

        # Tool agreement contributes up to 35 points (not enough alone to pass 65%)
        score = tool_ratio * 35

        # ICT/SMC confluence: all Tool-1 signals, up to ~28 points
        ict = results.get("ict_smc", {})
        ict_strength = abs(ict.get("strength", 0))
        score += min(ict_strength, 5) * 4
        if ict.get("displacement"):
            score += 3
        if ict.get("ote"):
            score += 3
        if ict.get("bos"):
            score += 5   # trend continuation confirmed
        if ict.get("choch"):
            score += 4   # potential reversal early signal
        if ict.get("mss"):
            score += 6   # reversal confirmed with displacement — strongest ICT signal
        if ict.get("last_swing_high") is not None or ict.get("last_swing_low") is not None:
            score += 2   # structural reference points are present

        # Tool 2 -- FVG confluence (up to ~18 points)
        fvg = results.get("fvg", {})
        if (fvg.get("bullish_fvg") or fvg.get("bearish_fvg")) and not fvg.get("mitigated"):
            score += 8    # fresh, unmitigated FVG
        if fvg.get("ifvg_bullish") or fvg.get("ifvg_bearish"):
            score += 6    # IFVG present -- flipped gap, high-probability zone
        if fvg.get("atr") is not None:
            score += 4    # ATR filter was active (gaps are institutionally significant)

        # Tool 3 -- Order Block confluence (up to ~18 points)
        ob = results.get("order_block", {})
        if ob.get("bullish_ob") or ob.get("bearish_ob"):
            score += 6    # valid OB identified
        if ob.get("retest_bullish") or ob.get("retest_bearish"):
            score += 7    # price is actively retesting the OB -- entry zone
        if ob.get("breaker_bullish") or ob.get("breaker_bearish"):
            score += 5    # breaker block present -- flipped OB re-entry zone

        # Tool 4 -- Liquidity confluence (up to ~18 points)
        liq = results.get("liquidity", {})
        if liq.get("swept"):
            score += 7    # any liquidity sweep (stop hunt confirmed)
        if liq.get("external_swept"):
            score += 4    # external (major) liquidity swept -- stronger signal
        if liq.get("eqh_detected") or liq.get("eql_detected"):
            score += 4    # equal highs/lows pool identified (likely target)
        if liq.get("internal_swept") and not liq.get("external_swept"):
            score += 3    # internal sweep only -- weaker but still valid

        # Tool 5 -- Market Structure confluence (up to ~18 points)
        ms = results.get("market_structure", {})
        if ms.get("bos"):
            score += 10   # BOS confirms trend continuation
        if ms.get("choch"):
            score += 6    # CHoCH signals early reversal
        ms_conf = ms.get("confidence", 0.0)
        score += ms_conf * 8   # structural confidence bonus (0-8 points)

        # Conflicting tools reduce confidence
        bullish_tools = results.get("bullish_tools", 0)
        bearish_tools = results.get("bearish_tools", 0)
        if bullish_tools > 0 and bearish_tools > 0:
            conflicting = min(bullish_tools, bearish_tools)
            score -= conflicting * 6

        # News sentiment: small nudge, not a dominant factor
        news_sentiment = self._get_news_sentiment()
        score += news_sentiment * 8

        # Clamp between 0-100
        raw_score = max(0.0, min(100.0, score))

        # CALIBRATION: if backtest_calibration.py has produced a real
        # historical calibration table, translate this raw heuristic score
        # into the ACTUAL win-rate observed in history for setups that
        # scored in this same range. If no table is loaded yet, or the
        # matching bucket doesn't have enough backtested samples, this
        # falls straight through to the original heuristic score below -
        # nothing changes unless a real backtest has been run.
        calibrated_score = self._get_calibrated_profit_chance(raw_score)
        if calibrated_score is not None:
            return calibrated_score

        return raw_score

    def _load_calibration_table(self) -> Optional[Dict]:
        """
        Load the score -> actual-win-rate calibration table produced by the
        offline backtest script (backtest_calibration.py), if one exists.

        Expected file format (written by backtest_calibration.py):
        {
            "generated_at": "...",
            "buckets": {
                "0-10":  {"win_rate": 8.3,  "samples": 41},
                "10-20": {"win_rate": 15.1, "samples": 63},
                ...
                "90-100":{"win_rate": 71.4, "samples": 22}
            }
        }
        """
        try:
            if os.path.exists(self.calibration_table_file):
                with open(self.calibration_table_file, "r") as f:
                    data = json.load(f)
                buckets = data.get("buckets", {})
                if buckets:
                    logger.info(f"Loaded profit-chance calibration table from {self.calibration_table_file}")
                    return buckets
        except Exception as e:
            logger.warning(f"Could not load calibration table ({self.calibration_table_file}): {e}")
        return None

    def _get_calibrated_profit_chance(self, raw_score: float) -> Optional[float]:
        """
        Look up the actual historical win-rate for the bucket the raw
        heuristic score falls into. Returns None (meaning: fall back to the
        raw heuristic score untouched) if there is no calibration table, or
        the matching bucket doesn't have enough backtested samples yet.
        """
        if not self._calibration_table:
            return None

        bucket_floor = int(raw_score // 10) * 10
        bucket_floor = min(bucket_floor, 90)  # clamp top bucket to "90-100"
        bucket_key = f"{bucket_floor}-{bucket_floor + 10}"

        bucket = self._calibration_table.get(bucket_key)
        if not bucket:
            return None

        min_samples = self.config.get("CALIBRATION_MIN_SAMPLES", 20)
        if bucket.get("samples", 0) < min_samples:
            return None

        return float(bucket.get("win_rate"))
    
    def is_trade_worth(self, results: Dict) -> Tuple[bool, str]:
        """
        Check if trade meets criteria:
        - At least MIN_TOOLS_MATCH tools agreeing
        - Profit chance >= MIN_PROFIT_CHANCE
        Returns (should_trade, reason)
        """
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
        min_chance = self.config.get("MIN_PROFIT_CHANCE", 65.0)
        
        signal = results.get("signal", 0)
        agreeing_tools = results.get("tools_agreeing", 0)
        profit_chance = results.get("profit_chance", 0.0)
        direction = "BUY" if signal == 1 else "SELL" if signal == -1 else "HOLD"
        
        if signal == 0:
            return False, f"No clear signal (HOLD)"
        
        if agreeing_tools < min_tools:
            return False, f"Only {agreeing_tools}/{min_tools} tools agree for {direction}"
        
        if profit_chance < min_chance:
            return False, f"Profit chance {profit_chance:.1f}% < {min_chance}% minimum"
        
        return True, f"{direction} signal | {agreeing_tools}/5 tools | {profit_chance:.1f}% profit chance"
    
    def multi_timeframe_analysis(self, ohlc_data: Dict[str, pd.DataFrame]) -> Dict:
        """Analyze all 3 timeframes with weighted decision"""
        results = {}
        
        for tf_name in ["higher", "medium", "lower"]:
            if tf_name in ohlc_data and ohlc_data[tf_name] is not None:
                df = ohlc_data[tf_name]
                if len(df) >= 50:
                    results[tf_name] = self.calculate_all_indicators(df)
                else:
                    results[tf_name] = {"signal": 0, "confidence": 0, "profit_chance": 0}
            else:
                results[tf_name] = {"signal": 0, "confidence": 0, "profit_chance": 0}
        
        final_signal = self._weighted_mtf_decision(results)
        results["final_signal"] = final_signal
        
        return results
    
    def _weighted_mtf_decision(self, mtf_results: Dict) -> Dict:
        """
        Weighted decision across timeframes:
        - Higher TF (4h): 50% weight
        - Medium TF (1h): 30% weight  
        - Lower TF (15m): 20% weight
        
        FIX (tools-agreeing bug): the "5/3 rule" is meant per-timeframe -
        each of the 3 timeframes (4h/1h/15m) should independently have at
        least MIN_TOOLS_MATCH of its own 5 tools agreeing, in the SAME
        direction. The previous version summed each timeframe's 0-5 count
        together (max possible 15) and compared that SUM to MIN_TOOLS_MATCH
        (3) - so e.g. 4h=1, 1h=1, 15m=1 (weak on every single timeframe)
        summed to 3 and passed, even though not one timeframe actually hit
        the intended 3-out-of-5 bar. In practice this meant the tools
        check almost never blocked anything (real coins were showing
        summed values of 8-11 against a threshold of 3).
        Now: ALL 3 timeframes must independently have >= MIN_TOOLS_MATCH
        bullish_tools (for BUY) or bearish_tools (for SELL). tools_agreeing
        reports the weakest (minimum) of the 3 timeframes' counts, since
        that's what actually determines pass/fail - directly comparable to
        MIN_TOOLS_MATCH on the intended 0-5 scale.
        """
        weights = {
            "higher": 0.50,
            "medium": 0.30,
            "lower": 0.20
        }
        
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
        
        weighted_signal = 0
        total_bullish = 0
        total_bearish = 0
        
        for tf_name, weight in weights.items():
            if tf_name in mtf_results:
                sig = mtf_results[tf_name].get("signal", 0)
                conf = mtf_results[tf_name].get("confidence", 0)
                profit = mtf_results[tf_name].get("profit_chance", 0)
                bullish_t = mtf_results[tf_name].get("bullish_tools", 0)
                bearish_t = mtf_results[tf_name].get("bearish_tools", 0)
                
                total_bullish += bullish_t
                total_bearish += bearish_t
                
                weighted_signal += sig * weight * (conf * 0.4 + profit/100 * 0.6)
        
        # FIX (tools-agreeing bug): each timeframe must independently clear
        # min_tools in the same direction - the weakest timeframe is the
        # binding constraint, not the sum of all three.
        tf_names = ["higher", "medium", "lower"]
        min_bullish_across_tf = min(mtf_results.get(tf, {}).get("bullish_tools", 0) for tf in tf_names)
        min_bearish_across_tf = min(mtf_results.get(tf, {}).get("bearish_tools", 0) for tf in tf_names)
        all_tf_bullish_ok = min_bullish_across_tf >= min_tools
        all_tf_bearish_ok = min_bearish_across_tf >= min_tools
        
        # Calculate overall profit chance
        avg_profit_chance = np.mean([
            mtf_results[tf].get("profit_chance", 50) 
            for tf in ["higher", "medium", "lower"] 
            if tf in mtf_results
        ]) if any(tf in mtf_results for tf in ["higher", "medium", "lower"]) else 50
        
        news_sentiment = self._get_news_sentiment()
        if news_sentiment != 0:
            weighted_signal += news_sentiment * 0.1
        
        if all_tf_bullish_ok and weighted_signal >= 0.3:
            decision = "BUY"
            overall_agreeing = min_bullish_across_tf
        elif all_tf_bearish_ok and weighted_signal <= -0.3:
            decision = "SELL"
            overall_agreeing = min_bearish_across_tf
        else:
            decision = "HOLD"
            # Still report the more-relevant of the two so the diagnostic
            # log shows how close a HOLD was to qualifying either way.
            overall_agreeing = max(min_bullish_across_tf, min_bearish_across_tf)
        
        return {
            "decision": decision,
            "score": weighted_signal,
            "tools_agreeing": overall_agreeing,
            "total_bullish_tools": total_bullish,
            "total_bearish_tools": total_bearish,
            "confidence": min(abs(weighted_signal), 1.0),
            "profit_chance": avg_profit_chance,
            "direction": 1 if decision == "BUY" else (-1 if decision == "SELL" else 0)
        }
    
    def _get_news_sentiment(self) -> float:
        """Get crypto news sentiment (-1.0 to 1.0)"""
        if not self.news_api_key or self.news_api_key == "YOUR_NEWS_API_KEY_HERE":
            return 0.0
            
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": "cryptocurrency bitcoin crypto market",
                "apiKey": self.news_api_key,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 10
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                articles = response.json().get("articles", [])
                positive_words = ["bullish", "surge", "rally", "gain", "up", "high", "growth", "positive"]
                negative_words = ["bearish", "crash", "drop", "loss", "down", "low", "decline", "negative", "ban", "hack"]
                
                sentiment = 0
                for article in articles:
                    text = (article.get("title", "") + " " + article.get("description", "")).lower()
                    for word in positive_words:
                        if word in text:
                            sentiment += 1
                    for word in negative_words:
                        if word in text:
                            sentiment -= 1
                
                max_possible = len(articles) * 2
                if max_possible > 0:
                    normalized = sentiment / max_possible
                    return max(min(normalized, 1.0), -1.0)
                    
        except Exception as e:
            logger.error(f"News API error: {e}")
        
        return 0.0
