"""
HackerAI Auto Trading Bot - Analysis Engine
ICT/SMC + FVG + Order Blocks + Liquidity + Market Structure
Timeframes: 4h, 1h, 15m
"""

import pandas as pd
import numpy as np
import logging
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
        """Tool 1: ICT / Smart Money Concepts Analysis"""
        result = {"bullish": False, "bearish": False, "strength": 0}
        
        if len(ohlc) < 50:
            return result
            
        close = ohlc["close"].values
        high = ohlc["high"].values
        low = ohlc["low"].values
        open_p = ohlc["open"].values
        
        for i in range(20, min(50, len(ohlc))):
            body = abs(close[-i] - open_p[-i])
            range_total = high[-i] - low[-i]
            if range_total > 0:
                body_ratio = body / range_total
                if close[-i] > open_p[-i] and body_ratio > 0.7:
                    if i > 5 and high[-i] > max(high[-i-5:-i]):
                        result["bullish"] = True
                        result["strength"] += 1
                elif close[-i] < open_p[-i] and body_ratio > 0.7:
                    if i > 5 and low[-i] < min(low[-i-5:-i]):
                        result["bearish"] = True
                        result["strength"] -= 1
        
        volume = ohlc["volume"].values if "volume" in ohlc.columns else None
        if volume is not None:
            avg_vol = np.mean(volume[-30:])
            if avg_vol > 0 and volume[-1] > avg_vol * 1.5:
                body_l = abs(close[-1] - open_p[-1])
                if close[-1] > open_p[-1] and body_l > (high[-1] - low[-1]) * 0.6:
                    result["bullish"] = True
                    result["strength"] += 2
                elif close[-1] < open_p[-1] and body_l > (high[-1] - low[-1]) * 0.6:
                    result["bearish"] = True
                    result["strength"] -= 2
        
        return result
    
    def _detect_fvg(self, ohlc: pd.DataFrame) -> Dict:
        """Tool 2: Fair Value Gap Detection"""
        result = {"bullish_fvg": False, "bearish_fvg": False, "fvg_levels": [], "mitigated": False}
        
        if len(ohlc) < 5:
            return result
            
        high = ohlc["high"].values
        low = ohlc["low"].values
        close = ohlc["close"].values
        
        for i in range(2, min(20, len(ohlc))):
            if high[-i-1] < low[-i+1]:
                gap_high = low[-i+1]
                gap_low = high[-i-1]
                mitigated = any(
                    gap_low <= close[-j] <= gap_high 
                    for j in range(i-1, min(i+5, len(ohlc)))
                )
                result["bullish_fvg"] = True
                result["fvg_levels"].append({
                    "type": "bullish", "high": gap_high, "low": gap_low,
                    "size": gap_high - gap_low, "mitigated": mitigated
                })
            elif low[-i-1] > high[-i+1]:
                gap_high = low[-i-1]
                gap_low = high[-i+1]
                mitigated = any(
                    gap_low <= close[-j] <= gap_high 
                    for j in range(i-1, min(i+5, len(ohlc)))
                )
                result["bearish_fvg"] = True
                result["fvg_levels"].append({
                    "type": "bearish", "high": gap_high, "low": gap_low,
                    "size": gap_high - gap_low, "mitigated": mitigated
                })
        
        if result["fvg_levels"]:
            result["mitigated"] = any(f["mitigated"] for f in result["fvg_levels"])
        
        return result
    
    def _detect_order_blocks(self, ohlc: pd.DataFrame) -> Dict:
        """Tool 3: Order Block Detection"""
        result = {"bullish_ob": None, "bearish_ob": None, "ob_levels": []}
        
        if len(ohlc) < 10:
            return result
            
        open_p = ohlc["open"].values
        close = ohlc["close"].values
        high = ohlc["high"].values
        low = ohlc["low"].values
        
        for i in range(3, min(30, len(ohlc))):
            current_body = abs(close[-i] - open_p[-i])
            current_range = high[-i] - low[-i]
            if current_range == 0:
                continue
            body_ratio = current_body / current_range
            
            if body_ratio > 0.6 and close[-i] > open_p[-i] and \
               close[-i] > close[-i-1] and close[-i] > close[-i-2]:
                ob_idx = -i-1
                if ob_idx >= -len(ohlc):
                    ob_candle = {
                        "type": "bullish", "high": high[ob_idx], "low": low[ob_idx],
                        "open": open_p[ob_idx], "close": close[ob_idx],
                        "level": (high[ob_idx] + low[ob_idx]) / 2
                    }
                    if result["bullish_ob"] is None:
                        result["bullish_ob"] = ob_candle
                    result["ob_levels"].append(ob_candle)
                    
            elif body_ratio > 0.6 and close[-i] < open_p[-i] and \
                 close[-i] < close[-i-1] and close[-i] < close[-i-2]:
                ob_idx = -i-1
                if ob_idx >= -len(ohlc):
                    ob_candle = {
                        "type": "bearish", "high": high[ob_idx], "low": low[ob_idx],
                        "open": open_p[ob_idx], "close": close[ob_idx],
                        "level": (high[ob_idx] + low[ob_idx]) / 2
                    }
                    if result["bearish_ob"] is None:
                        result["bearish_ob"] = ob_candle
                    result["ob_levels"].append(ob_candle)
        
        return result
    
    def _detect_liquidity(self, ohlc: pd.DataFrame) -> Dict:
        """Tool 4: Liquidity Sweeps Detection"""
        result = {"buyside_liquidity": None, "sellside_liquidity": None, 
                  "swept": False, "recent_sweep": None}
        
        if len(ohlc) < 30:
            return result
            
        high = ohlc["high"].values
        low = ohlc["low"].values
        close = ohlc["close"].values
        
        swing_highs = []
        swing_lows = []
        
        for i in range(5, min(30, len(ohlc) - 5)):
            if all(high[-i] > high[-i-j] for j in range(1, 4)) and \
               all(high[-i] > high[-i+j] for j in range(1, 4)):
                swing_highs.append({"index": len(ohlc) - i, "level": high[-i]})
            if all(low[-i] < low[-i-j] for j in range(1, 4)) and \
               all(low[-i] < low[-i+j] for j in range(1, 4)):
                swing_lows.append({"index": len(ohlc) - i, "level": low[-i]})
        
        if swing_highs:
            nearest_sh = max(sh for sh in swing_highs if sh["level"] > close[-1] * 0.99)
            if nearest_sh and high[-1] >= nearest_sh["level"]:
                result["buyside_liquidity"] = nearest_sh["level"]
                result["swept"] = True
                result["recent_sweep"] = "buyside"
                
        if swing_lows:
            nearest_sl = min(sl for sl in swing_lows if sl["level"] < close[-1] * 1.01)
            if nearest_sl and low[-1] <= nearest_sl["level"]:
                result["sellside_liquidity"] = nearest_sl["level"]
                result["swept"] = True
                result["recent_sweep"] = "sellside"
        
        return result
    
    def _market_structure(self, ohlc: pd.DataFrame) -> Dict:
        """Tool 5: Market Structure Analysis (BOS/CHoCH)"""
        result = {"trend": "neutral", "bos": False, "choch": False, 
                  "structure_broken": None, "last_bos_direction": None}
        
        if len(ohlc) < 20:
            return result
            
        close = ohlc["close"].values
        high = ohlc["high"].values
        low = ohlc["low"].values
        
        ema_short = np.mean(close[-10:])
        ema_long = np.mean(close[-30:]) if len(close) >= 30 else np.mean(close)
        
        if ema_short > ema_long * 1.005:
            result["trend"] = "bullish"
        elif ema_short < ema_long * 0.995:
            result["trend"] = "bearish"
        
        prev_swing_high = max(high[-15:-5]) if len(high) >= 15 else max(high)
        if close[-1] > prev_swing_high and result["trend"] == "bullish":
            result["bos"] = True
            result["structure_broken"] = "bullish"
            result["last_bos_direction"] = "up"
            
        prev_swing_low = min(low[-15:-5]) if len(low) >= 15 else min(low)
        if close[-1] < prev_swing_low and result["trend"] == "bearish":
            result["bos"] = True
            result["structure_broken"] = "bearish"
            result["last_bos_direction"] = "down"
        
        if len(ohlc) >= 30:
            old_highs = max(high[-20:-10])
            old_lows = min(low[-20:-10])
            recent_high = max(high[-5:])
            recent_low = min(low[-5:])
            
            if old_lows < recent_low and recent_high < old_highs and close[-1] < close[-3]:
                result["choch"] = True
                result["structure_broken"] = "bearish_choch"
            if old_highs > recent_high and recent_low > old_lows and close[-1] > close[-3]:
                result["choch"] = True
                result["structure_broken"] = "bullish_choch"
        
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
        Calculate profit chance percentage based on:
        - Number of tools agreeing
        - Signal strength
        - Market structure alignment
        Returns 0-100%
        """
        base_chance = 50.0  # Base 50%
        
        # Each agreeing tool adds 8%
        agreeing = results.get("tools_agreeing", 0)
        base_chance += agreeing * 8
        
        # Subtract for conflicting tools
        if results["bullish_tools"] > 0 and results["bearish_tools"] > 0:
            conflicting = min(results["bullish_tools"], results["bearish_tools"])
            base_chance -= conflicting * 5
        
        # ICT strength boost
        ict = results.get("ict_smc", {})
        base_chance += ict.get("strength", 0) * 3
        
        # Market structure boost
        ms = results.get("market_structure", {})
        if ms.get("bos"):
            base_chance += 5
        if ms.get("choch"):
            base_chance += 3
        
        # FVG not mitigated = good
        fvg = results.get("fvg", {})
        if fvg.get("bullish_fvg") or fvg.get("bearish_fvg"):
            if not fvg.get("mitigated"):
                base_chance += 5
        
        # Liquidity sweep = extra confirmation
        liq = results.get("liquidity", {})
        if liq.get("swept"):
            base_chance += 3
        
        # News sentiment boost
        news_sentiment = self._get_news_sentiment()
        base_chance += news_sentiment * 10
        
        # Clamp between 0-100
        return max(0.0, min(100.0, base_chance))
    
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
        
        Tools 5/3 rule applied per timeframe AND overall
        """
        weights = {
            "higher": 0.50,
            "medium": 0.30,
            "lower": 0.20
        }
        
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
        
        weighted_signal = 0
        total_tools_count = 0
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
                total_tools_count += mtf_results[tf_name].get("tools_agreeing", 0)
                
                weighted_signal += sig * weight * (conf * 0.4 + profit/100 * 0.6)
        
        # Overall tools count
        overall_agreeing = max(total_bullish, total_bearish)
        
        # Calculate overall profit chance
        avg_profit_chance = np.mean([
            mtf_results[tf].get("profit_chance", 50) 
            for tf in ["higher", "medium", "lower"] 
            if tf in mtf_results
        ]) if any(tf in mtf_results for tf in ["higher", "medium", "lower"]) else 50
        
        news_sentiment = self._get_news_sentiment()
        if news_sentiment != 0:
            weighted_signal += news_sentiment * 0.1
        
        if overall_agreeing >= min_tools and weighted_signal >= 0.3:
            decision = "BUY"
        elif overall_agreeing >= min_tools and weighted_signal <= -0.3:
            decision = "SELL"
        else:
            decision = "HOLD"
        
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
