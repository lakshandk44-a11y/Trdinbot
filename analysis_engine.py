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
        
    def calculate_all_indicators(self, ohlc: pd.DataFrame,
                                  correlated_ohlc: Optional[pd.DataFrame] = None,
                                  daily_ohlc: Optional[pd.DataFrame] = None,
                                  market_data: Optional[Dict] = None) -> Dict:
        """
        Run all 5 analysis tools on a single timeframe.

        correlated_ohlc / daily_ohlc are OPTIONAL extra context used only by
        Tool 1's extended concepts (SMT Divergence needs a correlated
        symbol's candles; Macro Structure / Old Highs-Lows need daily
        candles). If either is not provided, those specific sub-features
        simply don't trigger - nothing else changes, exactly like before.

        market_data (ADDED, user request) is an OPTIONAL dict of live
        market data (currently: funding_rate_pct) used only by the new
        Volume Profile / Funding Rate confluence checks inside
        _calculate_profit_chance. Same pattern as correlated_ohlc/
        daily_ohlc: if not provided, those specific checks just no-op.
        Neither this nor Volume Profile touches the 5-tool
        bullish_tools/bearish_tools vote below in any way - both are
        purely additive inputs to the separate profit_chance score.
        """
        results = {}

        # Tools 2-4 (and 5) are computed FIRST and handed to Tool 1 as
        # read-only input. This is a pure re-ordering - none of Tools 2-5's
        # own detection logic is touched at all - it just lets Tool 1 spot
        # confluences that span multiple tools (e.g. the Unicorn Model =
        # FVG + Order Block + Liquidity Sweep all in one zone) without
        # duplicating their detection code.
        fvg_result = self._detect_fvg(ohlc)
        ob_result = self._detect_order_blocks(ohlc, fvg=fvg_result)
        liquidity_result = self._detect_liquidity(ohlc)
        ms_result = self._market_structure(ohlc)

        results["ict_smc"] = self._ict_smc_analysis(
            ohlc,
            fvg=fvg_result,
            ob=ob_result,
            liquidity=liquidity_result,
            correlated_ohlc=correlated_ohlc,
            daily_ohlc=daily_ohlc,
        )
        results["fvg"] = fvg_result
        results["order_block"] = ob_result
        results["liquidity"] = liquidity_result
        results["market_structure"] = ms_result

        # ADDED (user request): Volume Profile (VPVR) and the passed-through
        # live market_data (funding rate). Both are stored as plain extra
        # result keys, computed/attached here alongside the 5 tools above,
        # but are NOT tools themselves - see the "TOOLS AGREEMENT" block
        # right below, which is completely unchanged and has no knowledge
        # of either of these keys. They are read only inside
        # _calculate_profit_chance, as additional (non-gating) score inputs.
        results["volume_profile"] = self._volume_profile_analysis(ohlc)
        results["market_data"] = market_data or {}
        # ADDED (user request): Market Regime Detection (ADX). Same pattern
        # as volume_profile immediately above - a plain extra result key,
        # NOT a tool, no involvement in the bullish_tools/bearish_tools
        # vote block right below. Read only inside _calculate_profit_chance.
        results["market_regime"] = self._market_regime_analysis(ohlc)
        
        # ================================================================
        # TOOLS AGREEMENT (bullish_tools / bearish_tools, out of 5)
        #
        # Each of the 5 tools above independently detects several distinct
        # named sub-concepts (Tool 1: BOS, CHoCH, MSS, SMT Divergence, Macro
        # Break, Unicorn Model, Inverse Fairy Tale, Old High/Low reaction,
        # Wyckoff breakout; Tool 2: fresh FVG, CE-zone entry, FVG stacking,
        # IFVG; Tool 3: fresh OB, Breaker Block, Retest, Rejection Block,
        # Volume-confirmed OB; Tool 4: external sweep, internal sweep, EQH/
        # EQL sweep; Tool 5: swing-structure trend, BOS/CHoCH, EMA alignment).
        # A tool only counts toward bullish_tools/bearish_tools if AT LEAST
        # MIN_SUBCONCEPTS_PER_TOOL of ITS OWN sub-concepts agree on the same
        # direction at the same time - one lone sub-signal firing inside a
        # tool is no longer enough, on its own, to swing that tool's vote.
        # None of the detection thresholds/logic inside the 5 tools were
        # changed by this - only how many of a tool's own concepts must
        # agree before that tool's single vote counts in the outer 5-tool
        # vote (which itself is still governed by MIN_TOOLS_MATCH, unchanged).
        # ================================================================
        min_sub = self.config.get("MIN_SUBCONCEPTS_PER_TOOL", 2)

        results["bullish_tools"] = 0
        results["bearish_tools"] = 0
        results["tools_agreeing"] = 0
        results["total_active_tools"] = 5

        # ---- Tool 1: ICT/SMC ----
        ict = results["ict_smc"]
        ict_bull = sum([
            ict.get("bos_direction") == "bullish",
            ict.get("choch_direction") == "bullish",
            ict.get("mss_direction") == "bullish",
            bool(ict.get("smt_bullish_divergence")),
            bool(ict.get("macro_break_bullish")),
            bool(ict.get("unicorn_bullish")),
            bool(ict.get("inverse_fairy_tale_bullish")),
            ict.get("old_level_support") is not None,
            bool(ict.get("wyckoff_breakout_bullish")),
            bool(ict.get("displacement_pd_bullish")),
            bool(ict.get("ote_confluence_bullish")),
            bool(ict.get("volume_displacement_bullish")),
        ])
        ict_bear = sum([
            ict.get("bos_direction") == "bearish",
            ict.get("choch_direction") == "bearish",
            ict.get("mss_direction") == "bearish",
            bool(ict.get("smt_bearish_divergence")),
            bool(ict.get("macro_break_bearish")),
            bool(ict.get("unicorn_bearish")),
            bool(ict.get("inverse_fairy_tale_bearish")),
            ict.get("old_level_resistance") is not None,
            bool(ict.get("wyckoff_breakout_bearish")),
            bool(ict.get("displacement_pd_bearish")),
            bool(ict.get("ote_confluence_bearish")),
            bool(ict.get("volume_displacement_bearish")),
        ])
        ict["bullish_subconcepts"], ict["bearish_subconcepts"] = ict_bull, ict_bear
        if ict_bull >= min_sub and ict_bull > ict_bear:
            results["bullish_tools"] += 1
        elif ict_bear >= min_sub and ict_bear > ict_bull:
            results["bearish_tools"] += 1

        # ---- Tool 2: FVG ----
        fvg = results["fvg"]
        fvg_bull = sum([
            bool(fvg.get("bullish_fvg")) and not fvg.get("mitigated"),
            bool(fvg.get("ce_entry_bullish")),
            bool(fvg.get("stacked_bullish")),
            bool(fvg.get("ifvg_bullish")),
        ])
        fvg_bear = sum([
            bool(fvg.get("bearish_fvg")) and not fvg.get("mitigated"),
            bool(fvg.get("ce_entry_bearish")),
            bool(fvg.get("stacked_bearish")),
            bool(fvg.get("ifvg_bearish")),
        ])
        fvg["bullish_subconcepts"], fvg["bearish_subconcepts"] = fvg_bull, fvg_bear
        if fvg_bull >= min_sub and fvg_bull > fvg_bear:
            results["bullish_tools"] += 1
        elif fvg_bear >= min_sub and fvg_bear > fvg_bull:
            results["bearish_tools"] += 1

        # ---- Tool 3: Order Block ----
        ob = results["order_block"]
        ob_bull = sum([
            ob.get("bullish_ob") is not None,
            ob.get("breaker_bullish") is not None,
            bool(ob.get("retest_bullish")),
            ob.get("rejection_block_bullish") is not None,
            bool(ob.get("volume_confirmed_bullish")),
        ])
        ob_bear = sum([
            ob.get("bearish_ob") is not None,
            ob.get("breaker_bearish") is not None,
            bool(ob.get("retest_bearish")),
            ob.get("rejection_block_bearish") is not None,
            bool(ob.get("volume_confirmed_bearish")),
        ])
        ob["bullish_subconcepts"], ob["bearish_subconcepts"] = ob_bull, ob_bear
        if ob_bull >= min_sub and ob_bull > ob_bear:
            results["bullish_tools"] += 1
        elif ob_bear >= min_sub and ob_bear > ob_bull:
            results["bearish_tools"] += 1

        # ---- Tool 4: Liquidity ----
        # NOTE: a SELLSIDE sweep (lows swept) is the bullish signal (stop-hunt
        # of shorts before reversal up) and a BUYSIDE sweep (highs swept) is
        # the bearish signal - same convention the original code used.
        liq = results["liquidity"]
        liq_bull = sum([
            bool(liq.get("ext_sellside_swept")),
            bool(liq.get("int_sellside_swept")),
            bool(liq.get("eql_swept")),
        ])
        liq_bear = sum([
            bool(liq.get("ext_buyside_swept")),
            bool(liq.get("int_buyside_swept")),
            bool(liq.get("eqh_swept")),
        ])
        liq["bullish_subconcepts"], liq["bearish_subconcepts"] = liq_bull, liq_bear
        if liq_bull >= min_sub and liq_bull > liq_bear:
            results["bullish_tools"] += 1
        elif liq_bear >= min_sub and liq_bear > liq_bull:
            results["bearish_tools"] += 1

        # ---- Tool 5: Market Structure ----
        # FIX (regression): EMA Alignment was added as a 3rd competing
        # sub-concept alongside trend/structure_broken. The ORIGINAL code
        # only ever checked trend/structure_broken (trend ALONE was always
        # enough) - EMA was never part of this vote before. Adding it as an
        # equal, independently-counted concept meant a trend-only signal
        # (trend=bullish, no EMA data either way) could get TIED and
        # CANCELLED by a contradicting ema_bearish, blocking setups that
        # used to count on trend alone. ema_bullish/ema_bearish are still
        # tracked on the result dict (informational), just no longer part
        # of this tally.
        ms = results["market_structure"]
        ms_bull = sum([
            ms.get("trend") == "bullish",
            ms.get("structure_broken") == "bullish",
        ])
        ms_bear = sum([
            ms.get("trend") == "bearish",
            ms.get("structure_broken") == "bearish",
        ])
        ms["bullish_subconcepts"], ms["bearish_subconcepts"] = ms_bull, ms_bear
        if ms_bull >= min_sub and ms_bull > ms_bear:
            results["bullish_tools"] += 1
        elif ms_bear >= min_sub and ms_bear > ms_bull:
            results["bearish_tools"] += 1

        # ================================================================
        # STRONG TOOLS TALLY (user request): a SECOND, independent tally
        # using a stricter per-tool bar (STRONG_SUBCONCEPTS_PER_TOOL, e.g.
        # 2+) than the normal bullish_tools/bearish_tools above (which uses
        # MIN_SUBCONCEPTS_PER_TOOL, e.g. 1+). Reuses the exact same
        # already-computed per-tool counts (ict_bull, fvg_bull, etc.) - just
        # a different, stricter threshold applied to them, computing a
        # completely separate 0-5 count. This does NOT replace or change
        # bullish_tools/bearish_tools at all - it's used by
        # _weighted_mtf_decision as an ADDITIONAL, alternate way for a
        # single very-strong timeframe to justify a trade on its own,
        # alongside (not instead of) the existing all-3-timeframes rule.
        # ================================================================
        strong_min_sub = self.config.get("STRONG_SUBCONCEPTS_PER_TOOL", 2)
        results["strong_bullish_tools"] = sum([
            ict_bull >= strong_min_sub and ict_bull > ict_bear,
            fvg_bull >= strong_min_sub and fvg_bull > fvg_bear,
            ob_bull >= strong_min_sub and ob_bull > ob_bear,
            liq_bull >= strong_min_sub and liq_bull > liq_bear,
            ms_bull >= strong_min_sub and ms_bull > ms_bear,
        ])
        results["strong_bearish_tools"] = sum([
            ict_bear >= strong_min_sub and ict_bear > ict_bull,
            fvg_bear >= strong_min_sub and fvg_bear > fvg_bull,
            ob_bear >= strong_min_sub and ob_bear > ob_bull,
            liq_bear >= strong_min_sub and liq_bear > liq_bull,
            ms_bear >= strong_min_sub and ms_bear > ms_bull,
        ])

        # Tools agreeing (maximum between bullish/bearish)
        results["tools_agreeing"] = max(results["bullish_tools"], results["bearish_tools"])
        
        # Signal based on tools count
        results["signal"] = self._generate_signal(results)
        results["confidence"] = self._calculate_confidence(results)
        results["profit_chance"] = self._calculate_profit_chance(results)
        
        return results
    
    def _ict_smc_analysis(self, ohlc: pd.DataFrame,
                           fvg: Optional[Dict] = None,
                           ob: Optional[Dict] = None,
                           liquidity: Optional[Dict] = None,
                           correlated_ohlc: Optional[pd.DataFrame] = None,
                           daily_ohlc: Optional[pd.DataFrame] = None) -> Dict:
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
          - SMT Divergence: correlated-pair (e.g. BTC/ETH) divergence between
            swing highs/lows - the highest-conviction reversal signal.
          - Kill Zones: London / New York / Silver Bullet session windows.
          - Macro Structure: Previous Week/Day High-Low and Opening Range.
          - Unicorn Model: FVG + Order Block + Liquidity Sweep confluence.
          - Inverse Fairy Tale: a swept strong high/low that closes back
            beyond it - trend-continuation signal.
          - Old Highs/Lows: swing levels from ~1-6 months back.
          - Accumulation/Distribution (Wyckoff): range-bound volume profile.

        fvg / ob / liquidity are the ALREADY-COMPUTED outputs of Tools 2, 3
        and 4 (read-only) - used here only to detect the Unicorn Model
        confluence. correlated_ohlc / daily_ohlc are optional extra data;
        when not supplied the corresponding sub-features simply don't fire.
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
            # ---- Extended ICT/SMC concepts ----
            "smt_bullish_divergence": False,
            "smt_bearish_divergence": False,
            "kill_zone": None,            # "london" | "new_york" | "silver_bullet" | None
            "in_kill_zone": False,
            "pdh": None, "pdl": None,       # Previous Day High/Low
            "pwh": None, "pwl": None,       # Previous Week High/Low
            "opening_range_high": None, "opening_range_low": None,
            "macro_break_bullish": False,
            "macro_break_bearish": False,
            "unicorn_bullish": False,
            "unicorn_bearish": False,
            "inverse_fairy_tale_bullish": False,
            "inverse_fairy_tale_bearish": False,
            # FIX (regression): these 3 concepts already existed and set
            # result["bullish"]/["bearish"] directly (see below), but had no
            # own named field, so they were left OUT of the sub-concept
            # tally in calculate_all_indicators - meaning setups that used
            # to make Tool 1 "bullish" via ONE of these 3 concepts alone no
            # longer counted at all, even at MIN_SUBCONCEPTS_PER_TOOL=1.
            "displacement_pd_bullish": False,
            "displacement_pd_bearish": False,
            "ote_confluence_bullish": False,
            "ote_confluence_bearish": False,
            "volume_displacement_bullish": False,
            "volume_displacement_bearish": False,
            "old_highs": [],
            "old_lows": [],
            "old_level_support": None,
            "old_level_resistance": None,
            "wyckoff_phase": None,         # "accumulation" | "distribution" | None
            "wyckoff_range_high": None,
            "wyckoff_range_low": None,
            "wyckoff_breakout_bullish": False,
            "wyckoff_breakout_bearish": False,
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
            result["displacement_pd_bullish"] = True
            result["strength"] += 2
        elif is_displacement_candle and close[-1] < open_p[-1] and pd_zone == "premium":
            result["bearish"] = True
            result["displacement_pd_bearish"] = True
            result["strength"] -= 2

        # OTE confluence: buying/selling from the classic retracement zone adds strength
        if ote and pd_zone == "discount" and close[-1] > open_p[-1]:
            result["bullish"] = True
            result["ote_confluence_bullish"] = True
            result["strength"] += 1
        elif ote and pd_zone == "premium" and close[-1] < open_p[-1]:
            result["bearish"] = True
            result["ote_confluence_bearish"] = True
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
                    result["volume_displacement_bullish"] = True
                    result["strength"] += 2
                elif close[-1] < open_p[-1]:
                    result["bearish"] = True
                    result["volume_displacement_bearish"] = True
                    result["strength"] -= 2

        # ================================================================
        # 9. SMT (SMART MONEY TECHNIQUE) DIVERGENCE
        #    Compares this asset's most recent two swing lows/highs against
        #    a correlated pair's (e.g. BTC/ETH). If THIS asset makes a lower
        #    low while the correlated asset makes a higher low -> bullish
        #    SMT divergence (top-tier reversal signal). Mirror for highs.
        #    Requires correlated_ohlc to be supplied - no-ops otherwise.
        # ================================================================
        if correlated_ohlc is not None and len(correlated_ohlc) >= 30 and len(ohlc) >= 30:
            try:
                c_high = correlated_ohlc["high"].values
                c_low = correlated_ohlc["low"].values
                n_cmp = min(30, len(high), len(c_high), len(low), len(c_low))

                def _last_two_pivot_lows(arr):
                    pivots = []
                    for i in range(2, len(arr) - 2):
                        if (arr[i] < arr[i - 1] and arr[i] < arr[i - 2]
                                and arr[i] < arr[i + 1] and arr[i] < arr[i + 2]):
                            pivots.append(arr[i])
                    return pivots[-2:] if len(pivots) >= 2 else None

                def _last_two_pivot_highs(arr):
                    pivots = []
                    for i in range(2, len(arr) - 2):
                        if (arr[i] > arr[i - 1] and arr[i] > arr[i - 2]
                                and arr[i] > arr[i + 1] and arr[i] > arr[i + 2]):
                            pivots.append(arr[i])
                    return pivots[-2:] if len(pivots) >= 2 else None

                own_lows_pv = _last_two_pivot_lows(low[-n_cmp:])
                corr_lows_pv = _last_two_pivot_lows(c_low[-n_cmp:])
                if own_lows_pv and corr_lows_pv:
                    if own_lows_pv[1] < own_lows_pv[0] and corr_lows_pv[1] > corr_lows_pv[0]:
                        result["smt_bullish_divergence"] = True
                        result["bullish"] = True
                        result["strength"] += 3

                own_highs_pv = _last_two_pivot_highs(high[-n_cmp:])
                corr_highs_pv = _last_two_pivot_highs(c_high[-n_cmp:])
                if own_highs_pv and corr_highs_pv:
                    if own_highs_pv[1] > own_highs_pv[0] and corr_highs_pv[1] < corr_highs_pv[0]:
                        result["smt_bearish_divergence"] = True
                        result["bearish"] = True
                        result["strength"] -= 3
            except Exception as e:
                logger.debug(f"SMT divergence check skipped: {e}")

        # ================================================================
        # 10. KILL ZONES (session timing)
        #     London 02:00-05:00 UTC, New York 08:00-11:00 UTC,
        #     Silver Bullet 09:50-10:10 UTC (highest-priority, narrowest).
        #     Uses the candle's OWN timestamp (not wall-clock) so this is
        #     equally correct live and when replayed in a backtest.
        #     Not directional by itself - adds conviction to whatever
        #     directional bias already exists during these windows.
        # ================================================================
        try:
            if "timestamp" in ohlc.columns and len(ohlc) > 0:
                last_ts = pd.to_numeric(pd.Series(ohlc["timestamp"].iloc[-1]), errors="coerce").iloc[0]
                candle_dt = (datetime.utcfromtimestamp(last_ts / 1000.0)
                             if pd.notna(last_ts) else datetime.utcnow())
            else:
                candle_dt = datetime.utcnow()
        except Exception:
            candle_dt = datetime.utcnow()

        hh, mm = candle_dt.hour, candle_dt.minute
        kill_zone = None
        if 2 <= hh < 5:
            kill_zone = "london"
        if 8 <= hh < 11:
            kill_zone = "new_york"
        if (hh == 9 and mm >= 50) or (hh == 10 and mm <= 10):
            kill_zone = "silver_bullet"   # narrowest/most specific window wins

        result["kill_zone"] = kill_zone
        result["in_kill_zone"] = kill_zone is not None

        if kill_zone is not None:
            if result["bullish"] and not result["bearish"]:
                result["strength"] += 1
            elif result["bearish"] and not result["bullish"]:
                result["strength"] -= 1

        # ================================================================
        # 11. MACRO STRUCTURE (Weekly/Daily levels)
        #     Previous Day High/Low, Previous Week High/Low, and today's
        #     Opening Range - from the supplied daily candles. A close
        #     beyond PDH/PWH or PDL/PWL is treated as a macro break.
        #     Requires daily_ohlc to be supplied - no-ops otherwise.
        # ================================================================
        if daily_ohlc is not None and len(daily_ohlc) >= 3 and "timestamp" in daily_ohlc.columns:
            try:
                d = daily_ohlc.copy()
                d["timestamp"] = pd.to_numeric(d["timestamp"], errors="coerce")
                d = d.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                d["_dt"] = pd.to_datetime(d["timestamp"], unit="ms", utc=True)

                if len(d) >= 2:
                    prev_day = d.iloc[-2]
                    result["pdh"] = float(prev_day["high"])
                    result["pdl"] = float(prev_day["low"])

                iso = d["_dt"].dt.isocalendar()
                d["_iso_year"] = iso["year"]
                d["_iso_week"] = iso["week"]
                weekly = d.groupby(["_iso_year", "_iso_week"]).agg(
                    wh=("high", "max"), wl=("low", "min")
                ).reset_index()
                if len(weekly) >= 2:
                    prev_week = weekly.iloc[-2]
                    result["pwh"] = float(prev_week["wh"])
                    result["pwl"] = float(prev_week["wl"])

                today = d.iloc[-1]
                result["opening_range_high"] = float(today["high"])
                result["opening_range_low"] = float(today["low"])

                c_last = close[-1]
                if result["pdh"] is not None and c_last > result["pdh"]:
                    result["macro_break_bullish"] = True
                if result["pwh"] is not None and c_last > result["pwh"]:
                    result["macro_break_bullish"] = True
                if result["pdl"] is not None and c_last < result["pdl"]:
                    result["macro_break_bearish"] = True
                if result["pwl"] is not None and c_last < result["pwl"]:
                    result["macro_break_bearish"] = True

                if result["macro_break_bullish"] and not result["macro_break_bearish"]:
                    result["bullish"] = True
                    result["strength"] += 2
                elif result["macro_break_bearish"] and not result["macro_break_bullish"]:
                    result["bearish"] = True
                    result["strength"] -= 2
            except Exception as e:
                logger.debug(f"Macro structure check skipped: {e}")

        # ================================================================
        # 12. UNICORN MODEL
        #     FVG + Order Block + Liquidity Sweep all overlapping in the
        #     same price zone in the same direction - the strongest single
        #     ICT confluence pattern. Uses Tools 2/3/4's own already-computed
        #     results (read-only) - their detection logic is untouched.
        # ================================================================
        if fvg and ob and liquidity:
            try:
                swept_dir = liquidity.get("recent_sweep")

                if swept_dir == "sellside":
                    bull_ob = ob.get("bullish_ob")
                    if bull_ob:
                        for f in fvg.get("fvg_levels", []):
                            if f.get("type") == "bullish" and not f.get("mitigated"):
                                if max(bull_ob["low"], f["low"]) <= min(bull_ob["high"], f["high"]):
                                    result["unicorn_bullish"] = True
                                    result["bullish"] = True
                                    result["strength"] += 3
                                    break

                if swept_dir == "buyside":
                    bear_ob = ob.get("bearish_ob")
                    if bear_ob:
                        for f in fvg.get("fvg_levels", []):
                            if f.get("type") == "bearish" and not f.get("mitigated"):
                                if max(bear_ob["low"], f["low"]) <= min(bear_ob["high"], f["high"]):
                                    result["unicorn_bearish"] = True
                                    result["bearish"] = True
                                    result["strength"] -= 3
                                    break
            except Exception as e:
                logger.debug(f"Unicorn Model check skipped: {e}")

        # ================================================================
        # 13. INVERSE FAIRY TALE
        #     A strong high/low gets swept (pierced) but price closes back
        #     beyond it within a few candles - a failed break that signals
        #     trend CONTINUATION in the original direction.
        # ================================================================
        try:
            lookback_ift = min(6, len(ohlc) - 1)
            if swing_low_levels and lookback_ift >= 2:
                ref_low = swing_low_levels[-1]
                pierced = any(low[-k] < ref_low for k in range(2, lookback_ift + 1))
                if pierced and close[-1] > ref_low:
                    result["inverse_fairy_tale_bullish"] = True
                    result["bullish"] = True
                    result["strength"] += 2

            if swing_high_levels and lookback_ift >= 2:
                ref_high = swing_high_levels[-1]
                pierced_h = any(high[-k] > ref_high for k in range(2, lookback_ift + 1))
                if pierced_h and close[-1] < ref_high:
                    result["inverse_fairy_tale_bearish"] = True
                    result["bearish"] = True
                    result["strength"] -= 2
        except Exception as e:
            logger.debug(f"Inverse Fairy Tale check skipped: {e}")

        # ================================================================
        # 14. OLD HIGHS / LOWS (OHL/OLH)
        #     Swing highs/lows from the supplied daily candles that are
        #     roughly 1-6 months old. Price currently sitting near one of
        #     these levels is treated as a reaction-zone confluence.
        #     Requires daily_ohlc to be supplied - no-ops otherwise.
        # ================================================================
        if daily_ohlc is not None and len(daily_ohlc) >= 40 and "timestamp" in daily_ohlc.columns:
            try:
                d2 = daily_ohlc.copy()
                d2["timestamp"] = pd.to_numeric(d2["timestamp"], errors="coerce")
                d2 = d2.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                d_high = d2["high"].values
                d_low = d2["low"].values
                d_ts = d2["timestamp"].values
                now_ms = d_ts[-1]   # dataset's own "now" -> correct live AND in backtests
                day_ms = 24 * 60 * 60 * 1000

                min_days = self.config.get("OLD_HIGH_LOW_MIN_DAYS", 30)
                max_days = self.config.get("OLD_HIGH_LOW_MAX_DAYS", 180)
                pivot_n2 = 5

                old_highs, old_lows = [], []
                for i in range(pivot_n2, len(d2) - pivot_n2):
                    age_days = (now_ms - d_ts[i]) / day_ms
                    if not (min_days <= age_days <= max_days):
                        continue
                    if (all(d_high[i] > d_high[i - k] for k in range(1, pivot_n2 + 1))
                            and all(d_high[i] > d_high[i + k] for k in range(1, pivot_n2 + 1))):
                        old_highs.append(float(d_high[i]))
                    if (all(d_low[i] < d_low[i - k] for k in range(1, pivot_n2 + 1))
                            and all(d_low[i] < d_low[i + k] for k in range(1, pivot_n2 + 1))):
                        old_lows.append(float(d_low[i]))

                result["old_highs"] = old_highs
                result["old_lows"] = old_lows

                tol = 0.005   # 0.5% proximity tolerance
                c_last = close[-1]
                for lvl in old_lows:
                    if lvl > 0 and abs(c_last - lvl) / lvl <= tol and c_last >= lvl:
                        result["old_level_support"] = lvl
                        result["bullish"] = True
                        result["strength"] += 1
                        break
                for lvl in old_highs:
                    if lvl > 0 and abs(c_last - lvl) / lvl <= tol and c_last <= lvl:
                        result["old_level_resistance"] = lvl
                        result["bearish"] = True
                        result["strength"] -= 1
                        break
            except Exception as e:
                logger.debug(f"Old Highs/Lows check skipped: {e}")

        # ================================================================
        # 15. ACCUMULATION / DISTRIBUTION (Wyckoff)
        #     A tight trading range (span small relative to average candle
        #     range) with a volume profile skewed to up-candles = likely
        #     Accumulation (bullish bias); skewed to down-candles = likely
        #     Distribution (bearish bias). A close breaking out of the range
        #     in the phase's implied direction confirms it.
        # ================================================================
        try:
            wyckoff_window = min(25, len(ohlc) - 1)
            if wyckoff_window >= 15:
                w_high = high[-wyckoff_window:]
                w_low = low[-wyckoff_window:]
                w_close = close[-wyckoff_window:]
                w_open = open_p[-wyckoff_window:]
                range_high = float(np.max(w_high))
                range_low = float(np.min(w_low))
                range_span = range_high - range_low

                recent_ranges2 = (high[-31:-1] - low[-31:-1]) if len(high) >= 31 else (high - low)
                avg_range2 = float(np.mean(recent_ranges2)) if len(recent_ranges2) > 0 else 0.0
                is_ranging = bool(avg_range2 > 0 and range_span < avg_range2 * 4)

                if is_ranging:
                    up_vol = 0.0
                    down_vol = 0.0
                    if volume is not None and len(volume) >= wyckoff_window:
                        w_vol = volume[-wyckoff_window:]
                        up_mask = w_close > w_open
                        down_mask = w_close < w_open
                        up_vol = float(np.sum(w_vol[up_mask]))
                        down_vol = float(np.sum(w_vol[down_mask]))

                    result["wyckoff_range_high"] = range_high
                    result["wyckoff_range_low"] = range_low

                    if up_vol > down_vol * 1.2:
                        result["wyckoff_phase"] = "accumulation"
                    elif down_vol > up_vol * 1.2:
                        result["wyckoff_phase"] = "distribution"

                    if result["wyckoff_phase"] == "accumulation" and close[-1] > range_high:
                        result["wyckoff_breakout_bullish"] = True
                        result["bullish"] = True
                        result["strength"] += 2
                    elif result["wyckoff_phase"] == "distribution" and close[-1] < range_low:
                        result["wyckoff_breakout_bearish"] = True
                        result["bearish"] = True
                        result["strength"] -= 2
        except Exception as e:
            logger.debug(f"Wyckoff Accumulation/Distribution check skipped: {e}")

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
          - Mitigation (by Close AND by Wick): a gap is mitigated once price
            trades back into it. This is tracked as mitigated_by_close (a
            candle CLOSED inside the gap) and mitigated_by_wick (a candle's
            high/low TOUCHED the gap even if the close didn't) separately -
            the overall "mitigated" flag (used everywhere else) is true if
            EITHER happened, since a wick tap into the zone already means
            that liquidity/imbalance has been traded through.
          - Consequent Encroachment (CE): the 50% midpoint of an FVG - the
            classic ICT entry level within the gap. Each gap entry carries
            its own "ce" price, and an active CE-zone touch (price currently
            trading at/near the CE of a fresh, unmitigated gap) is flagged.
          - FVG Stacking: 2 or 3 unmitigated FVGs of the same direction
            formed close together in time (the same impulsive leg) - an
            Order Flow Imbalance, weighted higher than a single lone gap.
          - Gap FVG (raw/weekend gaps): a genuine 2-candle price gap (no
            overlap at all between consecutive candles, distinct from the
            3-candle FVG definition) - specifically flagged when it spans a
            traditional-market weekend close (Friday evening -> Sat/Sun/Mon).
          - Mitigated vs Unmitigated tracking: every fvg_levels entry keeps
            its own mitigated flag so once used it is not treated as a fresh
            zone again by any caller (bullish_fvg/bearish_fvg only turn on
            for UNMITIGATED gaps, same as before).
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
            # ---- Extended FVG concepts ----
            "ce_entry_bullish": False,   # price currently trading at the CE (50%) of a fresh bullish FVG
            "ce_entry_bearish": False,   # price currently trading at the CE (50%) of a fresh bearish FVG
            "stacked_bullish": False,
            "stacked_bearish": False,
            "stack_count_bullish": 0,
            "stack_count_bearish": 0,
            "weekend_gaps": [],          # raw 2-candle price gaps (separate from the 3-candle FVG definition)
            "has_weekend_gap": False,
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
                    # Mitigation by CLOSE: a subsequent candle closed inside the gap zone
                    mitigated_by_close = any(
                        gap_low <= close[-(j)] <= gap_high
                        for j in range(1, i)          # candles after the gap formed
                    )
                    # Mitigation by WICK (touch): a subsequent candle's high/low
                    # range overlaps the gap at all, even without closing inside it.
                    mitigated_by_wick = any(
                        low[-(j)] <= gap_high and high[-(j)] >= gap_low
                        for j in range(1, i)
                    )
                    mitigated = mitigated_by_close or mitigated_by_wick
                    ce = float((gap_high + gap_low) / 2)   # Consequent Encroachment
                    entry = {
                        "type": "bullish",
                        "high": float(gap_high),
                        "low":  float(gap_low),
                        "size": float(gap_size),
                        "ce": ce,
                        "mitigated": mitigated,
                        "mitigated_by_close": mitigated_by_close,
                        "mitigated_by_wick": mitigated_by_wick,
                        "i_pos": i,   # recency marker (smaller = more recent) - used for stacking
                    }
                    result["fvg_levels"].append(entry)
                    if not mitigated:
                        result["bullish_fvg"] = True
                        # CE entry: price is currently trading at/near this fresh
                        # gap's 50% midpoint (classic ICT entry trigger).
                        ce_tolerance = max(atr * 0.15, gap_size * 0.15)
                        if gap_low <= close[-1] <= gap_high and abs(close[-1] - ce) <= ce_tolerance:
                            result["ce_entry_bullish"] = True

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
                    mitigated_by_close2 = any(
                        gap_low2 <= close[-(j)] <= gap_high2
                        for j in range(1, i)
                    )
                    mitigated_by_wick2 = any(
                        low[-(j)] <= gap_high2 and high[-(j)] >= gap_low2
                        for j in range(1, i)
                    )
                    mitigated2 = mitigated_by_close2 or mitigated_by_wick2
                    ce2 = float((gap_high2 + gap_low2) / 2)
                    entry2 = {
                        "type": "bearish",
                        "high": float(gap_high2),
                        "low":  float(gap_low2),
                        "size": float(gap_size2),
                        "ce": ce2,
                        "mitigated": mitigated2,
                        "mitigated_by_close": mitigated_by_close2,
                        "mitigated_by_wick": mitigated_by_wick2,
                        "i_pos": i,
                    }
                    result["fvg_levels"].append(entry2)
                    if not mitigated2:
                        result["bearish_fvg"] = True
                        ce_tolerance2 = max(atr * 0.15, gap_size2 * 0.15)
                        if gap_low2 <= close[-1] <= gap_high2 and abs(close[-1] - ce2) <= ce_tolerance2:
                            result["ce_entry_bearish"] = True

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

        # ================================================================
        # FVG STACKING (Order Flow Imbalance)
        # 2 or 3 unmitigated FVGs of the SAME direction, formed close
        # together in time (within a handful of candles of each other),
        # count as a "stack" - repeated one-directional imbalance instead
        # of a single isolated gap.
        # ================================================================
        def _count_stack(entries):
            entries_sorted = sorted(entries, key=lambda f: f["i_pos"])
            if len(entries_sorted) < 2:
                return 1 if entries_sorted else 0
            best = 1
            run = 1
            for a, b in zip(entries_sorted, entries_sorted[1:]):
                if abs(b["i_pos"] - a["i_pos"]) <= 8:   # close together in time
                    run += 1
                    best = max(best, run)
                else:
                    run = 1
            return best

        bullish_unmit = [f for f in result["fvg_levels"] if f["type"] == "bullish" and not f["mitigated"]]
        bearish_unmit = [f for f in result["fvg_levels"] if f["type"] == "bearish" and not f["mitigated"]]

        result["stack_count_bullish"] = _count_stack(bullish_unmit)
        result["stack_count_bearish"] = _count_stack(bearish_unmit)
        result["stacked_bullish"] = result["stack_count_bullish"] >= 2
        result["stacked_bearish"] = result["stack_count_bearish"] >= 2

        # ================================================================
        # GAP FVG (raw/weekend gaps) - a genuine 2-candle price gap where
        # there is NO overlap at all between consecutive candles (distinct
        # from the 3-candle FVG imbalance definition above). Flagged
        # specifically as a "weekend gap" when it spans a traditional
        # Friday-evening -> Saturday/Sunday/Monday market close, which is
        # when this concept classically occurs.
        # ================================================================
        if "timestamp" in ohlc.columns:
            try:
                ts_vals = pd.to_numeric(ohlc["timestamp"], errors="coerce").values
                gap_scan_end = min(40, len(ohlc) - 1)
                for k in range(1, gap_scan_end):
                    i_now, i_prev = -k, -(k + 1)
                    gap_up = low[i_now] > high[i_prev]
                    gap_down = high[i_now] < low[i_prev]
                    if not (gap_up or gap_down):
                        continue

                    prev_ts, now_ts = ts_vals[i_prev], ts_vals[i_now]
                    if pd.isna(prev_ts) or pd.isna(now_ts):
                        continue

                    prev_dt = datetime.utcfromtimestamp(float(prev_ts) / 1000.0)
                    now_dt = datetime.utcfromtimestamp(float(now_ts) / 1000.0)
                    # Mon=0 ... Sun=6. Traditional markets close Friday evening
                    # and reopen Monday - a gap spanning that window is a
                    # classic "weekend gap".
                    is_weekend_span = (
                        prev_dt.weekday() == 4 and prev_dt.hour >= 20
                        and now_dt.weekday() in (5, 6, 0)
                    )

                    if gap_up:
                        g_low, g_high = float(high[i_prev]), float(low[i_now])
                    else:
                        g_low, g_high = float(high[i_now]), float(low[i_prev])

                    result["weekend_gaps"].append({
                        "type": "bullish" if gap_up else "bearish",
                        "low": g_low,
                        "high": g_high,
                        "is_weekend": bool(is_weekend_span),
                    })
                    if is_weekend_span:
                        result["has_weekend_gap"] = True
            except Exception as e:
                logger.debug(f"Weekend/raw gap check skipped: {e}")

        return result
    
    def _detect_order_blocks(self, ohlc: pd.DataFrame, fvg: Optional[Dict] = None) -> Dict:
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
          - Mitigation Flagging (Used OB): once price re-enters an OB zone
            (by close OR by wick) in the candles after the impulse move, that
            OB is marked mitigated=True and excluded from active entry signals.
            Only fresh, unmitigated OBs are set as bullish_ob / bearish_ob.
            Mitigated OBs are collected in mitigated_obs for reference.
          - OB + FVG Confluence (Overlap): the % of the OB zone that overlaps
            with any unmitigated FVG zone. Higher overlap = higher probability
            setup. Requires the already-computed FVG result (fvg parameter).
            Stored per-OB as ob_fvg_overlap_pct; the maximum across all active
            OBs is stored in ob_fvg_confluence_pct.
          - Rejection Block: an OB candle whose body is tiny relative to its
            total range (body_ratio < 30%) — dominated by wicks — signalling a
            strong rejection / pin-bar at the OB zone. Stored separately as
            rejection_block_bullish / rejection_block_bearish.
          - Order Flow Imbalance (Volume): the OB candle's volume relative to
            the local average of surrounding candles. A ratio >= 1.5 means the
            OB formed on above-average institutional activity and is therefore
            considered volume-confirmed. Stored per-OB as volume_ratio /
            volume_confirmed, and rolled up to top-level flags.
          - OB Quality Score: the OB candle's body-to-range ratio expressed
            as a 0-100 score (score > 60 = strong). Stored per-OB as
            quality_score and body_ratio.
        """
        result = {
            "bullish_ob": None,
            "bearish_ob": None,
            "ob_levels": [],
            # ---- Existing fields ----
            "breaker_bullish": None,          # bearish OB that got broken -> now support
            "breaker_bearish": None,          # bullish OB that got broken -> now resistance
            "breaker_levels": [],
            "retest_bullish": False,          # price currently retesting a bullish OB
            "retest_bearish": False,          # price currently retesting a bearish OB
            # ---- New fields ----
            "rejection_block_bullish": None,  # first active bullish OB that is also a rejection block
            "rejection_block_bearish": None,  # first active bearish OB that is also a rejection block
            "rejection_block_levels": [],     # all active rejection blocks found
            "ob_fvg_confluence_pct": 0.0,     # highest OB+FVG zone overlap % among active OBs
            "has_ob_fvg_confluence": False,   # True if any active OB overlaps an unmitigated FVG
            "volume_confirmed_bullish": False, # any active bullish OB was high-volume at formation
            "volume_confirmed_bearish": False, # any active bearish OB was high-volume at formation
            "mitigated_obs": [],              # list of OBs that have been used/mitigated
        }

        if len(ohlc) < 10:
            return result

        open_p = ohlc["open"].values
        close  = ohlc["close"].values
        high   = ohlc["high"].values
        low    = ohlc["low"].values
        volume = ohlc["volume"].values if "volume" in ohlc.columns else None

        # Global average volume (fallback when local window is unavailable)
        avg_volume_global = float(np.mean(volume)) if volume is not None and len(volume) > 0 else 0.0

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

            ob_high  = float(high[ob_idx])
            ob_low   = float(low[ob_idx])
            ob_range = ob_high - ob_low

            # ---- OB Quality Score: body / total-range ratio ----
            ob_body       = abs(close[ob_idx] - open_p[ob_idx])
            body_ratio    = (ob_body / ob_range) if ob_range > 0 else 0.0
            quality_score = round(body_ratio * 100, 1)   # 0–100; >60 = strong body
            # Rejection Block: body < 30% of range → large wicks dominate (pin-bar style)
            is_rejection  = body_ratio < 0.30

            # ---- Order Flow Imbalance (Volume) ----
            # Compare OB candle's volume to local ±5-candle average (excluding OB itself).
            if volume is not None:
                ob_abs_idx   = len(ohlc) + ob_idx           # convert to 0-based index
                ctx_start    = max(0, ob_abs_idx - 5)
                ctx_end      = min(len(ohlc), ob_abs_idx + 6)
                ctx_vols     = [volume[k] for k in range(ctx_start, ctx_end)
                                if k != ob_abs_idx]
                local_avg_vol = float(np.mean(ctx_vols)) if ctx_vols else avg_volume_global
                ob_vol        = float(volume[ob_idx])
                volume_ratio  = (ob_vol / local_avg_vol) if local_avg_vol > 0 else 1.0
                volume_confirmed = volume_ratio >= 1.5    # >= 1.5× local avg = institutional
            else:
                ob_vol           = 0.0
                volume_ratio     = 1.0
                volume_confirmed = False

            # ---- OB + FVG Confluence: % overlap of OB zone with unmitigated FVG zones ----
            ob_fvg_overlap_pct = 0.0
            if fvg and ob_range > 0:
                for fvg_entry in fvg.get("fvg_levels", []):
                    if fvg_entry.get("mitigated"):
                        continue
                    fvg_h     = fvg_entry["high"]
                    fvg_l     = fvg_entry["low"]
                    overlap_h = min(ob_high, fvg_h)
                    overlap_l = max(ob_low,  fvg_l)
                    if overlap_h > overlap_l:
                        pct = round(((overlap_h - overlap_l) / ob_range) * 100, 1)
                        if pct > ob_fvg_overlap_pct:
                            ob_fvg_overlap_pct = min(pct, 100.0)

            # ---- Mitigation Flagging (Used OB) ----
            # Post-impulse candles: indices -(i-1) through -1 (after the first
            # impulse candle at move_idx=-i). range(1, i) gives j=1..i-1.
            post_closes = [close[-(j)] for j in range(1, i)]
            post_lows   = [low[-(j)]   for j in range(1, i)]
            post_highs  = [high[-(j)]  for j in range(1, i)]

            # Mitigated by close: a post-impulse candle closed inside the OB zone
            mitigated_by_close = any(ob_low <= c <= ob_high for c in post_closes)
            # Mitigated by wick: a post-impulse candle's range overlapped the OB zone
            mitigated_by_wick  = any(
                lo <= ob_high and hi >= ob_low
                for lo, hi in zip(post_lows, post_highs)
            )
            mitigated = mitigated_by_close or mitigated_by_wick

            # ================================================================
            # BUILD THE OB ENTRY DICT (shared base for both directions)
            # ================================================================
            ob_candle_base = {
                "high":               ob_high,
                "low":                ob_low,
                "open":               float(open_p[ob_idx]),
                "close":              float(close[ob_idx]),
                "level":              float((ob_high + ob_low) / 2),
                # --- Mitigation ---
                "mitigated":          mitigated,
                "mitigated_by_close": mitigated_by_close,
                "mitigated_by_wick":  mitigated_by_wick,
                # --- Quality Score ---
                "quality_score":      quality_score,
                "body_ratio":         round(body_ratio, 3),
                # --- Rejection Block ---
                "is_rejection_block": is_rejection,
                # --- Volume OFI ---
                "volume_ratio":       round(volume_ratio, 3),
                "volume_confirmed":   volume_confirmed,
                # --- OB+FVG Confluence ---
                "ob_fvg_overlap_pct": ob_fvg_overlap_pct,
            }

            # Bullish OB: OB candle is bearish, followed by strong bullish impulse
            if (open_p[ob_idx] > close[ob_idx]             # OB candle bearish
                    and close[move_idx] > open_p[move_idx] # impulse bullish
                    and move_body_ratio > 0.55              # strong body
                    and close[move_idx] > high[ob_idx]):    # closes above OB high
                ob_candle = {**ob_candle_base, "type": "bullish"}
                result["ob_levels"].append(ob_candle)

                if mitigated:
                    result["mitigated_obs"].append(ob_candle)
                else:
                    # First (most recent) non-mitigated bullish OB is the primary entry zone
                    if result["bullish_ob"] is None:
                        result["bullish_ob"] = ob_candle
                    # Rejection block (first non-mitigated bullish rejection block)
                    if is_rejection and result["rejection_block_bullish"] is None:
                        result["rejection_block_bullish"] = ob_candle
                        result["rejection_block_levels"].append(ob_candle)
                    # Volume confirmation roll-up
                    if volume_confirmed:
                        result["volume_confirmed_bullish"] = True
                    # OB+FVG confluence roll-up (track highest overlap found)
                    if ob_fvg_overlap_pct > result["ob_fvg_confluence_pct"]:
                        result["ob_fvg_confluence_pct"]  = ob_fvg_overlap_pct
                        result["has_ob_fvg_confluence"]  = ob_fvg_overlap_pct > 0.0

            # Bearish OB: OB candle is bullish, followed by strong bearish impulse
            elif (open_p[ob_idx] < close[ob_idx]            # OB candle bullish
                    and close[move_idx] < open_p[move_idx]  # impulse bearish
                    and move_body_ratio > 0.55               # strong body
                    and close[move_idx] < low[ob_idx]):      # closes below OB low
                ob_candle = {**ob_candle_base, "type": "bearish"}
                result["ob_levels"].append(ob_candle)

                if mitigated:
                    result["mitigated_obs"].append(ob_candle)
                else:
                    # First (most recent) non-mitigated bearish OB is the primary entry zone
                    if result["bearish_ob"] is None:
                        result["bearish_ob"] = ob_candle
                    # Rejection block (first non-mitigated bearish rejection block)
                    if is_rejection and result["rejection_block_bearish"] is None:
                        result["rejection_block_bearish"] = ob_candle
                        result["rejection_block_levels"].append(ob_candle)
                    # Volume confirmation roll-up
                    if volume_confirmed:
                        result["volume_confirmed_bearish"] = True
                    # OB+FVG confluence roll-up
                    if ob_fvg_overlap_pct > result["ob_fvg_confluence_pct"]:
                        result["ob_fvg_confluence_pct"] = ob_fvg_overlap_pct
                        result["has_ob_fvg_confluence"] = ob_fvg_overlap_pct > 0.0

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
        # Mirror logic for bearish OB.
        # Only non-broken AND non-mitigated OBs qualify (mitigated OBs are used
        # zones — retesting them doesn't count as a fresh setup).
        # ================================================================
        breaker_keys = {(b["high"], b["low"]) for b in result["breaker_levels"]}

        for ob in result["ob_levels"]:
            if (ob["high"], ob["low"]) in breaker_keys:
                continue   # already a breaker — skip
            if ob.get("mitigated"):
                continue   # used/mitigated OB — skip

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
            # ---- Added for sub-concept voting only (see calculate_all_indicators) ----
            # These record the SAME conditions already checked below, just
            # without the "only if no external sweep already fired" early-exit
            # that the original external_swept/internal_swept/recent_sweep
            # fields use - so more than one of Tool 4's own sweep concepts can
            # be counted at once when several genuinely co-occur. They do NOT
            # change swept/recent_sweep/buyside_liquidity/sellside_liquidity/
            # external_swept/internal_swept, which everything else (bot_core's
            # TP/SL level picks, Tool 1's Unicorn Model) keeps using unchanged.
            "ext_buyside_swept": False,
            "ext_sellside_swept": False,
            "int_buyside_swept": False,
            "int_sellside_swept": False,
            "eqh_swept": False,
            "eql_swept": False,
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
            result["ext_buyside_swept"]  = True

        if ext_l is not None and low[-1] <= ext_l and close[-1] > ext_l:
            result["sellside_liquidity"] = ext_l
            result["swept"]              = True
            result["recent_sweep"]       = "sellside"
            result["external_swept"]     = True
            result["ext_sellside_swept"] = True

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

        # ext_*_swept/int_*_swept for the sub-concept tally: same conditions
        # as above, computed unconditionally (not gated by "if not swept")
        # so internal and external sweeps can both be counted if they both
        # genuinely happened, even though only one becomes the "official"
        # recent_sweep above.
        if int_h is not None and high[-1] >= int_h and close[-1] < int_h:
            result["int_buyside_swept"] = True
        if int_l is not None and low[-1] <= int_l and close[-1] > int_l:
            result["int_sellside_swept"] = True

        # -- EQH / EQL sweeps (also count as buy/sell-side sweeps) --
        for eqh_lvl in eqh_levels:
            if high[-1] >= eqh_lvl and close[-1] < eqh_lvl:
                result["buyside_liquidity"] = eqh_lvl
                result["swept"]             = True
                result["recent_sweep"]      = "buyside"
                result["eqh_swept"]         = True
                break

        for eql_lvl in eql_levels:
            if low[-1] <= eql_lvl and close[-1] > eql_lvl:
                result["sellside_liquidity"] = eql_lvl
                result["swept"]              = True
                result["recent_sweep"]       = "sellside"
                result["eql_swept"]          = True
                break

        return result

    def _market_regime_analysis(self, ohlc: pd.DataFrame, period: int = 14) -> Dict:
        """
        ADDED (user request): Market Regime Detection via ADX (Average
        Directional Index, Wilder's standard method) - classifies this
        timeframe's own recent price action as "trending" or "ranging".
        This is genuinely NEW information none of the 5 existing tools
        capture: they detect STRUCTURE (order blocks, FVGs, liquidity,
        BOS/CHoCH) but never whether the broader environment actually
        favors a breakout/trend-following read (what ICT/SMC concepts are
        built for) versus a choppy, mean-reverting one (where those same
        signals are classically less reliable) - exactly what a
        professional trader reads the regime for before trusting a
        breakout signal.

        Purely a NEW, read-only result key - does not participate in the
        bullish_tools/bearish_tools vote at all (that block never reads
        this key, same as volume_profile above). Only consumed by
        _calculate_profit_chance below as an additional, non-gating score
        input - exactly the same pattern as Volume Profile/Funding Rate,
        so it can only ever nudge the existing score, never block/reject
        a trade that already passed the 5-tool vote.
        """
        result = {"adx": None, "regime": "unknown"}
        if ohlc is None or len(ohlc) < period * 2 + 1:
            return result

        high = ohlc["high"].values
        low = ohlc["low"].values
        close = ohlc["close"].values
        n = len(ohlc)

        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        tr = np.zeros(n)
        for i in range(1, n):
            up_move = high[i] - high[i - 1]
            down_move = low[i - 1] - low[i]
            plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

        # Wilder's smoothing (same technique as a standard ATR/RSI smooth).
        def wilder_smooth(arr, period):
            smoothed = np.zeros(len(arr))
            smoothed[period] = arr[1:period + 1].sum()
            for i in range(period + 1, len(arr)):
                smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / period) + arr[i]
            return smoothed

        if n <= period + 1:
            return result

        tr_smooth = wilder_smooth(tr, period)
        plus_dm_smooth = wilder_smooth(plus_dm, period)
        minus_dm_smooth = wilder_smooth(minus_dm, period)

        with np.errstate(divide="ignore", invalid="ignore"):
            plus_di = np.where(tr_smooth != 0, 100 * plus_dm_smooth / tr_smooth, 0.0)
            minus_di = np.where(tr_smooth != 0, 100 * minus_dm_smooth / tr_smooth, 0.0)
            di_sum = plus_di + minus_di
            dx = np.where(di_sum != 0, 100 * np.abs(plus_di - minus_di) / di_sum, 0.0)

        # ADX = Wilder-smoothed average of DX over the last `period` bars
        # with valid data (starting right after the initial DI/TR warm-up).
        dx_valid = dx[period:]
        if len(dx_valid) < period:
            return result
        adx = float(np.mean(dx_valid[-period:]))

        if not np.isfinite(adx):
            return result

        result["adx"] = round(adx, 1)
        if adx >= 25:
            result["regime"] = "trending"
        elif adx < 20:
            result["regime"] = "ranging"
        else:
            result["regime"] = "transitional"

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
            "choch_direction":   None,  # "bullish_choch"/"bearish_choch" for debugging/display - the counting logic uses structure_broken (plain "bullish"/"bearish") instead
            "last_bos_direction": None,
            # ---- New field ----
            "confidence":        0.0,   # 0.0 - 1.0 structural confidence score
            # ---- Added for sub-concept voting only (see calculate_all_indicators) ----
            "ema_bullish": False,
            "ema_bearish": False,
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
        result["ema_bullish"] = bool(ema_bullish)
        result["ema_bearish"] = bool(ema_bearish)

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
            result["structure_broken"]   = "bullish"
            result["choch_direction"]    = "bullish_choch"

        if close[-1] < ref_low and result["trend"] == "bullish":
            result["choch"]              = True
            result["structure_broken"]   = "bearish"
            result["choch_direction"]    = "bearish_choch"

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

    def _volume_profile_analysis(self, ohlc: pd.DataFrame, num_bins: int = 24) -> Dict:
        """
        ADDED (user request): Volume Profile (VPVR) - bins the visible
        price range (of this same timeframe's candles) into `num_bins`
        buckets and sums traded volume in each, to find the Point of
        Control (POC - the single highest-volume price level, the
        strongest support/resistance in the visible range) and the Value
        Area (the tightest contiguous band of bins holding ~68% of total
        volume - the "fair value" zone). This is a well-established,
        independent (volume-based, not price-structure-based) confluence
        signal that complements the existing 5 price-structure tools
        without duplicating them.

        Purely a NEW, read-only result key - does not participate in the
        bullish_tools/bearish_tools vote at all (that block never reads
        this key). Only consumed by _calculate_profit_chance below as an
        additional, non-gating score input.
        """
        result = {
            "poc_price": None,
            "value_area_high": None,
            "value_area_low": None,
            "in_value_area": False,
            "near_poc": False,
        }
        if ohlc is None or len(ohlc) < 20 or "volume" not in ohlc.columns:
            return result

        high = ohlc["high"].values
        low = ohlc["low"].values
        close = ohlc["close"].values
        volume = ohlc["volume"].values

        price_min = float(np.min(low))
        price_max = float(np.max(high))
        if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
            return result

        bin_edges = np.linspace(price_min, price_max, num_bins + 1)
        bin_volume = np.zeros(num_bins)

        # Distribute each candle's volume across the bins its high-low
        # range spans, proportional to how much of the bin the candle's
        # range overlaps - a standard, simple VPVR approximation.
        for i in range(len(ohlc)):
            c_low, c_high, c_vol = low[i], high[i], volume[i]
            if not np.isfinite(c_low) or not np.isfinite(c_high) or c_high <= c_low or c_vol <= 0:
                continue
            for b in range(num_bins):
                b_lo, b_hi = bin_edges[b], bin_edges[b + 1]
                overlap = min(c_high, b_hi) - max(c_low, b_lo)
                if overlap > 0:
                    bin_volume[b] += c_vol * (overlap / (c_high - c_low))

        total_vol = float(bin_volume.sum())
        if total_vol <= 0:
            return result

        poc_idx = int(np.argmax(bin_volume))
        poc_price = float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2)

        # Value Area: grow outward from the POC bin until >=68% of total
        # volume is included (standard VPVR definition), always adding
        # whichever neighboring bin (above or below the current band) has
        # more volume next.
        target = total_vol * 0.68
        running = bin_volume[poc_idx]
        lo_i, hi_i = poc_idx, poc_idx
        while running < target and (lo_i > 0 or hi_i < num_bins - 1):
            next_lo_vol = bin_volume[lo_i - 1] if lo_i > 0 else -1.0
            next_hi_vol = bin_volume[hi_i + 1] if hi_i < num_bins - 1 else -1.0
            if next_hi_vol >= next_lo_vol:
                hi_i += 1
                running += bin_volume[hi_i]
            else:
                lo_i -= 1
                running += bin_volume[lo_i]

        va_low = float(bin_edges[lo_i])
        va_high = float(bin_edges[hi_i + 1])
        current_price = float(close[-1])
        bin_width = (price_max - price_min) / num_bins

        result["poc_price"] = poc_price
        result["value_area_high"] = va_high
        result["value_area_low"] = va_low
        result["in_value_area"] = va_low <= current_price <= va_high
        result["near_poc"] = abs(current_price - poc_price) <= bin_width

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

        # Extended ICT/SMC confluence (new sub-concepts added to Tool 1).
        # NOTE: since these add new points on top of the existing formula,
        # the score distribution has shifted vs. whatever calibration_table.json
        # was built from - re-run backtest_calibration.py to refresh it.
        if ict.get("smt_bullish_divergence") or ict.get("smt_bearish_divergence"):
            score += 6    # SMT divergence -- top-tier reversal confluence
        if ict.get("in_kill_zone"):
            score += 2    # inside a historically higher-probability session window
        if ict.get("macro_break_bullish") or ict.get("macro_break_bearish"):
            score += 5    # daily/weekly macro level broken
        if ict.get("unicorn_bullish") or ict.get("unicorn_bearish"):
            score += 6    # Unicorn Model -- FVG + OB + sweep confluence
        if ict.get("inverse_fairy_tale_bullish") or ict.get("inverse_fairy_tale_bearish"):
            score += 3    # swept level closed back beyond it -- continuation
        if ict.get("old_level_support") is not None or ict.get("old_level_resistance") is not None:
            score += 2    # reacting off an old (1-6 month) high/low
        if ict.get("wyckoff_breakout_bullish") or ict.get("wyckoff_breakout_bearish"):
            score += 4    # accumulation/distribution range breakout confirmed

        # Tool 2 -- FVG confluence (up to ~18 points)
        fvg = results.get("fvg", {})
        if (fvg.get("bullish_fvg") or fvg.get("bearish_fvg")) and not fvg.get("mitigated"):
            score += 8    # fresh, unmitigated FVG
        if fvg.get("ifvg_bullish") or fvg.get("ifvg_bearish"):
            score += 6    # IFVG present -- flipped gap, high-probability zone
        if fvg.get("atr") is not None:
            score += 4    # ATR filter was active (gaps are institutionally significant)

        # Extended FVG concepts (new sub-concepts added to Tool 2)
        if fvg.get("ce_entry_bullish") or fvg.get("ce_entry_bearish"):
            score += 4    # price trading at the Consequent Encroachment (50%) of a fresh FVG
        if fvg.get("stacked_bullish") or fvg.get("stacked_bearish"):
            score += 5    # FVG stacking -- repeated same-direction Order Flow Imbalance
        if fvg.get("has_weekend_gap"):
            score += 2    # a genuine weekend/raw gap is present as an extra reference level

        # Tool 3 -- Order Block confluence (up to ~30 points)
        ob = results.get("order_block", {})
        if ob.get("bullish_ob") or ob.get("bearish_ob"):
            score += 6    # valid, unmitigated OB identified (mitigated OBs excluded)
        if ob.get("retest_bullish") or ob.get("retest_bearish"):
            score += 7    # price is actively retesting the OB -- entry zone
        if ob.get("breaker_bullish") or ob.get("breaker_bearish"):
            score += 5    # breaker block present -- flipped OB re-entry zone

        # New OB sub-concepts (Tool 3 additions)
        if ob.get("rejection_block_bullish") or ob.get("rejection_block_bearish"):
            score += 3    # Rejection Block: large-wick OB candle -- strong reversal signal
        if ob.get("has_ob_fvg_confluence"):
            # OB + FVG Overlap: scale +0 to +4 pts based on % overlap (100% overlap = +4)
            overlap_pct = ob.get("ob_fvg_confluence_pct", 0.0)
            score += min(overlap_pct / 25.0, 4.0)
        if ob.get("volume_confirmed_bullish") or ob.get("volume_confirmed_bearish"):
            score += 3    # Order Flow Imbalance: OB formed on above-avg volume (institutional)
        # OB Quality Score: active OB with body > 60% of range = strong (clean impulse origin)
        _active_ob = ob.get("bullish_ob") or ob.get("bearish_ob")
        if _active_ob and _active_ob.get("quality_score", 0) > 60:
            score += 2    # strong-body OB (quality_score > 60)

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

        # ================================================================
        # ADDED (user request): Volume Profile + Funding Rate confluence.
        # Both are purely additive/subtractive nudges on this SAME existing
        # score, exactly like every component above (ICT/SMC, FVG, OB,
        # Liquidity, Market Structure, news sentiment) - neither one gates
        # or replaces anything, and neither touches bullish_tools/
        # bearish_tools/tools_agreeing/total_active_tools (the 5-tool vote)
        # at all. They only change whether the FINAL profit_chance number
        # clears the separate, already-existing MIN_PROFIT_CHANCE gate.
        # ================================================================

        # Volume Profile: current price at/near the Point of Control (the
        # single highest-volume level in the visible range) is a strong,
        # independent (volume-based, not price-structure-based) reaction
        # zone. Inside the Value Area (the ~68%-of-volume "fair value"
        # band) is a moderate positive. Outside both is a low-volume "air
        # pocket" - historically thinner, less reliable price action.
        vp = results.get("volume_profile", {})
        if vp.get("near_poc"):
            score += 4
        elif vp.get("in_value_area"):
            score += 2
        elif vp.get("poc_price") is not None:
            score -= 2

        # Funding Rate: crypto-futures-specific positioning signal,
        # independent of price structure. An extreme funding rate in the
        # SAME direction as this timeframe's own tool-vote lean signals
        # crowded positioning (everyone already long/short) - a classic
        # contrarian warning that a squeeze/reversal is more likely, so
        # the setup is penalized. Funding skewed the OTHER way (less
        # crowded) is a mild positive. Uses this timeframe's own
        # bullish_tools/bearish_tools as the direction proxy - the same
        # signal already used a few lines above for the conflicting-tools
        # penalty, so this is consistent with how this function already
        # reasons about direction.
        md = results.get("market_data", {})
        funding_rate_pct = md.get("funding_rate_pct")
        if funding_rate_pct is not None:
            direction = 1 if bullish_tools >= bearish_tools else -1
            if (direction > 0 and funding_rate_pct > 0.05) or (direction < 0 and funding_rate_pct < -0.05):
                score -= 4
            elif (direction > 0 and funding_rate_pct < -0.02) or (direction < 0 and funding_rate_pct > 0.02):
                score += 2

        # Market Regime (ADX) - ADDED (user request): a professional
        # trader reads the broader regime (trending vs choppy/ranging)
        # before trusting a breakout-style signal - ICT/SMC concepts
        # (BOS/CHoCH, order blocks, liquidity sweeps) are classically
        # breakout/trend-following reads, so they deserve more confidence
        # in a confirmed trend and less in a genuinely range-bound market.
        # Same non-gating, purely additive pattern as every component
        # above - "unknown"/insufficient-data regime contributes nothing.
        regime = results.get("market_regime", {}).get("regime")
        if regime == "trending":
            score += 3
        elif regime == "ranging":
            score -= 3

        # Clamp between 0-100
        raw_score = max(0.0, min(100.0, score))

        # ADDED (user request): DEBUG-level score breakdown, log-only - no
        # logic changes. Shows exactly how much each new component (Volume
        # Profile, Funding Rate, Market Regime) contributed to this
        # timeframe's raw score, alongside the final calibrated result, so
        # it can be independently verified straight from pm2 logs without
        # having to trust anything else. Only visible if the logger is set
        # to DEBUG level; at the normal INFO level this line never prints
        # and has zero effect on anything.
        _vp_pts = (4 if vp.get("near_poc") else (2 if vp.get("in_value_area") else
                   (-2 if vp.get("poc_price") is not None else 0)))
        _funding_pts = 0
        if funding_rate_pct is not None:
            _direction = 1 if bullish_tools >= bearish_tools else -1
            if (_direction > 0 and funding_rate_pct > 0.05) or (_direction < 0 and funding_rate_pct < -0.05):
                _funding_pts = -4
            elif (_direction > 0 and funding_rate_pct < -0.02) or (_direction < 0 and funding_rate_pct > 0.02):
                _funding_pts = 2
        _regime_pts = 3 if regime == "trending" else (-3 if regime == "ranging" else 0)
        logger.debug(f"Score breakdown: raw={raw_score:.1f} | "
                     f"volume_profile={_vp_pts:+d} | funding={_funding_pts:+d} | "
                     f"regime={_regime_pts:+d} (regime={regime})")

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
        """
        Analyze all 3 timeframes with weighted decision.

        ohlc_data may OPTIONALLY also contain:
          - "correlated": {"higher": df, "medium": df, "lower": df} - the
            SMT-divergence reference symbol's candles on matching timeframes.
          - "daily": df - daily candles used for Macro Structure / Old
            Highs-Lows.
          - "market_data": {"funding_rate_pct": ...} (ADDED, user request) -
            live market data, currently just funding rate, passed through
            unchanged to all 3 timeframes (it isn't timeframe-specific).
        All three are purely additive. Callers that only pass "higher"/
        "medium"/"lower" (e.g. the existing backtest scripts) are
        completely unaffected - those extra sub-features just don't trigger.
        """
        results = {}

        correlated_data = ohlc_data.get("correlated") if isinstance(ohlc_data, dict) else None
        daily_ohlc = ohlc_data.get("daily") if isinstance(ohlc_data, dict) else None
        market_data = ohlc_data.get("market_data") if isinstance(ohlc_data, dict) else None

        for tf_name in ["higher", "medium", "lower"]:
            if tf_name in ohlc_data and ohlc_data[tf_name] is not None:
                df = ohlc_data[tf_name]
                if len(df) >= 50:
                    corr_df = None
                    if isinstance(correlated_data, dict):
                        corr_df = correlated_data.get(tf_name)
                    results[tf_name] = self.calculate_all_indicators(
                        df, correlated_ohlc=corr_df, daily_ohlc=daily_ohlc, market_data=market_data
                    )
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

        # ================================================================
        # PATH A (user request): a SECOND, independent way to justify a
        # trade, ALONGSIDE the "all 3 timeframes independently qualify"
        # rule below (Path B - completely unchanged). If ANY ONE of the 3
        # timeframes on its own has >= STRONG_TOOLS_MATCH tools where each
        # of those tools has >= STRONG_SUBCONCEPTS_PER_TOOL of its own sub-
        # concepts agreeing (a single very-strong timeframe), that alone is
        # enough - the other 2 timeframes don't need to confirm. If both
        # directions hit this on different timeframes at the same time
        # (genuinely conflicting signals), that's treated as ambiguous and
        # Path A is skipped in favor of Path B below, same as always.
        # ================================================================
        strong_min_tools = self.config.get("STRONG_TOOLS_MATCH", 4)
        strong_path_bullish_tf = []
        strong_path_bearish_tf = []
        if self.config.get("ENABLE_SINGLE_TF_STRONG_ENTRY", False):
            strong_path_bullish_tf = [
                tf for tf in tf_names
                if mtf_results.get(tf, {}).get("strong_bullish_tools", 0) >= strong_min_tools
            ]
            strong_path_bearish_tf = [
                tf for tf in tf_names
                if mtf_results.get(tf, {}).get("strong_bearish_tools", 0) >= strong_min_tools
            ]

        # Calculate overall profit chance
        avg_profit_chance = np.mean([
            mtf_results[tf].get("profit_chance", 50) 
            for tf in ["higher", "medium", "lower"] 
            if tf in mtf_results
        ]) if any(tf in mtf_results for tf in ["higher", "medium", "lower"]) else 50
        
        news_sentiment = self._get_news_sentiment()
        if news_sentiment != 0:
            weighted_signal += news_sentiment * 0.1

        entry_path = "none"
        if strong_path_bullish_tf and not strong_path_bearish_tf:
            decision = "BUY"
            overall_agreeing = max(mtf_results.get(tf, {}).get("strong_bullish_tools", 0) for tf in strong_path_bullish_tf)
            entry_path = "single_tf_strong"
        elif strong_path_bearish_tf and not strong_path_bullish_tf:
            decision = "SELL"
            overall_agreeing = max(mtf_results.get(tf, {}).get("strong_bearish_tools", 0) for tf in strong_path_bearish_tf)
            entry_path = "single_tf_strong"
        elif all_tf_bullish_ok and weighted_signal >= 0.3:
            decision = "BUY"
            overall_agreeing = min_bullish_across_tf
            entry_path = "all_tf_confirmed"
        elif all_tf_bearish_ok and weighted_signal <= -0.3:
            decision = "SELL"
            overall_agreeing = min_bearish_across_tf
            entry_path = "all_tf_confirmed"
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
            "direction": 1 if decision == "BUY" else (-1 if decision == "SELL" else 0),
            "entry_path": entry_path,  # "single_tf_strong" or "all_tf_confirmed" or "none" (HOLD)
        }
    
    def describe_agreement(self, results: Dict, direction: int) -> List[Dict]:
        """
        FIX (user request): human-readable summary of WHICH of the 5 tools
        agreed with a given direction on one timeframe's already-computed
        `results` (the output of calculate_all_indicators), and WHICH named
        sub-concepts inside each of those tools fired - for display in the
        "Trade Opened" Telegram message. Purely a read-only summary of
        fields that already exist on `results` (the exact same fields used
        for the bullish_tools/bearish_tools vote) - changes no detection or
        decision logic at all.

        direction: 1 for bullish/BUY, -1 for bearish/SELL.
        Returns a list of {"tool": <display name>, "subconcepts": [names]}
        for only the tools that agreed with `direction` (using the same
        MIN_SUBCONCEPTS_PER_TOOL-gated agreement as the real vote).
        """
        want_bull = direction == 1
        out = []

        ict = results.get("ict_smc", {})
        ict_names = {
            "BOS": ict.get("bos_direction") == ("bullish" if want_bull else "bearish"),
            "CHoCH": ict.get("choch_direction") == ("bullish" if want_bull else "bearish"),
            "MSS": ict.get("mss_direction") == ("bullish" if want_bull else "bearish"),
            "SMT Divergence": bool(ict.get("smt_bullish_divergence" if want_bull else "smt_bearish_divergence")),
            "Macro Break": bool(ict.get("macro_break_bullish" if want_bull else "macro_break_bearish")),
            "Unicorn Model": bool(ict.get("unicorn_bullish" if want_bull else "unicorn_bearish")),
            "Inverse Fairy Tale": bool(ict.get("inverse_fairy_tale_bullish" if want_bull else "inverse_fairy_tale_bearish")),
            "Old " + ("Low Support" if want_bull else "High Resistance"):
                ict.get("old_level_support" if want_bull else "old_level_resistance") is not None,
            "Wyckoff Breakout": bool(ict.get("wyckoff_breakout_bullish" if want_bull else "wyckoff_breakout_bearish")),
            "Displacement+PD": bool(ict.get("displacement_pd_bullish" if want_bull else "displacement_pd_bearish")),
            "OTE Confluence": bool(ict.get("ote_confluence_bullish" if want_bull else "ote_confluence_bearish")),
            "Volume Displacement": bool(ict.get("volume_displacement_bullish" if want_bull else "volume_displacement_bearish")),
        }
        ict_hits = [name for name, fired in ict_names.items() if fired]
        min_sub = self.config.get("MIN_SUBCONCEPTS_PER_TOOL", 2)
        if len(ict_hits) >= min_sub:
            out.append({"tool": "ICT/SMC", "subconcepts": ict_hits})

        fvg = results.get("fvg", {})
        fvg_names = {
            "Fresh FVG": bool(fvg.get("bullish_fvg" if want_bull else "bearish_fvg")) and not fvg.get("mitigated"),
            "CE Entry": bool(fvg.get("ce_entry_bullish" if want_bull else "ce_entry_bearish")),
            "FVG Stacking": bool(fvg.get("stacked_bullish" if want_bull else "stacked_bearish")),
            "IFVG": bool(fvg.get("ifvg_bullish" if want_bull else "ifvg_bearish")),
        }
        fvg_hits = [name for name, fired in fvg_names.items() if fired]
        if len(fvg_hits) >= min_sub:
            out.append({"tool": "FVG", "subconcepts": fvg_hits})

        ob = results.get("order_block", {})
        ob_names = {
            "Fresh OB": ob.get("bullish_ob" if want_bull else "bearish_ob") is not None,
            "Breaker Block": ob.get("breaker_bullish" if want_bull else "breaker_bearish") is not None,
            "Retest": bool(ob.get("retest_bullish" if want_bull else "retest_bearish")),
            "Rejection Block": ob.get("rejection_block_bullish" if want_bull else "rejection_block_bearish") is not None,
            "Volume Confirmed": bool(ob.get("volume_confirmed_bullish" if want_bull else "volume_confirmed_bearish")),
        }
        ob_hits = [name for name, fired in ob_names.items() if fired]
        if len(ob_hits) >= min_sub:
            out.append({"tool": "Order Block", "subconcepts": ob_hits})

        liq = results.get("liquidity", {})
        liq_names = {
            "External Sweep": bool(liq.get("ext_sellside_swept" if want_bull else "ext_buyside_swept")),
            "Internal Sweep": bool(liq.get("int_sellside_swept" if want_bull else "int_buyside_swept")),
            "EQH/EQL Sweep": bool(liq.get("eql_swept" if want_bull else "eqh_swept")),
        }
        liq_hits = [name for name, fired in liq_names.items() if fired]
        if len(liq_hits) >= min_sub:
            out.append({"tool": "Liquidity", "subconcepts": liq_hits})

        ms = results.get("market_structure", {})
        ms_names = {
            "Swing Trend": ms.get("trend") == ("bullish" if want_bull else "bearish"),
            "BOS/CHoCH": ms.get("structure_broken") == ("bullish" if want_bull else "bearish"),
        }
        ms_hits = [name for name, fired in ms_names.items() if fired]
        if len(ms_hits) >= min_sub:
            out.append({"tool": "Market Structure", "subconcepts": ms_hits})

        return out

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
