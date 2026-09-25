#!/usr/bin/env python3
"""
HackerAI Auto Trading Bot - Profit-Chance Calibration Backtest
========================================================================

මොකද කරන්නෙ:
  analysis_engine.py එකේ _calculate_profit_chance() එකෙන් දෙන raw heuristic
  score එක (0-100), ඇත්තටම historical data එකේදී කොච්චර % ට්‍රේඩ් ජයග්‍රහණය
  කරනවද කියලා walk-forward විදියට Binance real historical candles උඩ
  test කරලා, score එක bucket 10ක් (0-10, 10-20, ... 90-100) වලට කඩලා,
  bucket එකකට තියෙන ඇත්ත win-rate එක calculate කරලා
  calibration_table.json ගොනුවට ලියනවා.

  analysis_engine.py (_get_calibrated_profit_chance) මේ ගොනුව load කරලා,
  raw heuristic score එකක් ආවම ඒ score එකේ bucket එකට ගැලපෙන ඇත්ත
  historical win-rate එක return කරනවා (ප්‍රමාණවත් samples තියෙනවනම්) -
  score එක ම නිකම් "heuristic" එකක් නෙවෙයි, ඇත්තටම backtest කරපු
  win-rate එකක් වෙනවා.

  මේකෙන් bot_core.py, trade_manager.py, config.py, analysis_engine.py -
  කිසිම existing file එකකට වෙනසක් වෙන්නෙ නෑ (bot_core.klines() එකට
  optional startTime/endTime pagination params 2ක් විතරයි add කරලා
  තියෙන්නෙ, පරණ callers කිසිවක්වත් break කරන්නෙ නෑ). මේක සම්පූර්ණයෙන්ම
  වෙනම, read-only analysis script එකක්.

වැදගත්:
  මේක ඔයාගෙම server එකේ (Binance API access තියෙන තැන) run කරන්න ඕන -
  Claude සිටින sandbox එකේ internet access නැති නිසා මෙතන test කරන්න බැහැ.

Usage:
  python3 backtest_calibration.py
  python3 backtest_calibration.py --symbols BTCUSDT,ETHUSDT --months 9
"""

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    TOP_N_COINS, TIMEFRAMES, MIN_TOOLS_MATCH, MIN_SUBCONCEPTS_PER_TOOL,
    TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, TRADING_FEE_PERCENT,
    TRADING_HOURS_OVERRIDE_FILE, PATTERN_CALIBRATION_FILE,
    SMT_DIVERGENCE_ENABLED, SMT_CORRELATED_MAP, DAILY_HISTORY_CANDLES,
    OLD_HIGH_LOW_MIN_DAYS, OLD_HIGH_LOW_MAX_DAYS,
)
import pattern_engine
from bot_core import BinanceFuturesClient
from analysis_engine import AnalysisEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s"
)
logger = logging.getLogger("backtest_calibration")

OUTPUT_FILE = "calibration_table.json"

# Same lookback windows the live bot fetches per timeframe (see
# bot_core._fetch_multi_timeframe), so every walk-forward "snapshot" the
# engine sees here has the same amount of context the live bot would have.
LOOKBACK_LIMIT = {"higher": 100, "medium": 150, "lower": 200}
FORWARD_LOOKAHEAD_CANDLES = 200  # 200 * 15m = ~50 hours to resolve TP/SL
STRIDE = 1  # FIX: was 3 (every 3rd medium-timeframe candle) to save runtime,
# but that meant the hour-of-day breakdown only ever sampled hours at a
# fixed 3-hour offset (2,5,8,11,14,17,20,23 UTC), leaving the other 16
# hours completely unsampled (0 setups) - not "bad", just never checked.
# STRIDE=1 evaluates every medium-timeframe (1h) candle, giving full
# 24-hour coverage. Runtime will be ~3x longer.

# ADDED (user request - calibrate PATTERN_MIN_CONFIDENCE against real
# outcomes instead of a random-noise false-positive test). Pattern Engine
# only ever looks at the LOWER timeframe (15m, see bot_core.
# _try_pattern_engine_entry: pattern_engine.detect_best_pattern(lower_tf,
# ...)), independently of the higher/medium/daily timeframes the main
# Tool-5 path above needs - so this reuses the SAME lower_df already
# fetched per symbol below rather than fetching anything new.
PATTERN_STRIDE = 4  # every 4th 15m candle (~hourly) - patterns form over
# many candles, so consecutive 15m candles are highly correlated/
# overlapping samples of the same forming pattern; every-candle would
# mostly multiply runtime without adding much genuinely new information.
PATTERN_MIN_BUCKET_SAMPLES = 30  # classical chart patterns are naturally
# much rarer events than "every candle has a Tool-5 score" - don't trust a
# confidence bucket's win-rate/expectancy with fewer real matches than
# this (same "not enough data, stay out" convention as MIN_HOUR_SAMPLES
# above, just a lower floor since the underlying event is rarer).

# Binance kline interval -> milliseconds, used to paginate klines() by
# startTime/endTime instead of only ever getting the most recent `limit`.
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

KLINE_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore"
]

# FIX (IP-ban cascade bug): the script used to have no delay between
# symbols/timeframes (only a 0.25s sleep between *pages* of the SAME
# symbol+timeframe), so 40 coins x 3 timeframes x several pages each fired
# far faster than Binance's rate limit tolerates, tripping a -1003 IP ban.
# Once banned, every request for the rest of the run just got the same
# "banned until <ms>" error back instantly - the script never waited it
# out, so it burned through most of the coin list getting nothing.
# This constant adds a small pause between every individual request
# (page, timeframe, or symbol) so the run stays under the limit and
# shouldn't get banned in the first place.
REQUEST_PACING_SECONDS = 1.0

_BAN_MSG_RE = re.compile(r"banned until (\d+)")


def _extract_ban_until_ms(payload) -> Optional[int]:
    """Pull the 'banned until <epoch_ms>' timestamp out of a Binance -1003
    error payload/message, if present."""
    msg = ""
    if isinstance(payload, dict):
        msg = str(payload.get("msg", ""))
    else:
        msg = str(payload)
    match = _BAN_MSG_RE.search(msg)
    return int(match.group(1)) if match else None


def _wait_out_ban(ban_until_ms: int, context: str):
    """FIX (IP-ban cascade bug): if Binance has banned this IP, sleep until
    the ban actually lifts (plus a small safety buffer) instead of
    continuing to fire requests into the ban window - repeated requests
    during an active ban don't get more data, they just risk the ban being
    extended and waste the whole rest of the run."""
    now_ms = int(time.time() * 1000)
    wait_seconds = max(0.0, (ban_until_ms - now_ms) / 1000.0) + 2.0
    logger.warning(f"⏳ {context}: IP is rate-limit banned by Binance. "
                    f"Waiting {wait_seconds:.0f}s for the ban to lift before continuing...")
    time.sleep(wait_seconds)


def klines_with_ban_handling(client: BinanceFuturesClient, context: str, **kwargs):
    """Wraps client.klines() so a -1003 IP ban pauses and retries instead of
    being treated as a permanent per-symbol failure."""
    max_ban_retries = 5
    for attempt in range(max_ban_retries):
        batch = client.klines(**kwargs)
        if isinstance(batch, dict) and batch.get("code") == -1003:
            ban_until = _extract_ban_until_ms(batch)
            if ban_until:
                _wait_out_ban(ban_until, context)
                continue  # retry the same request now that the ban should be lifted
        return batch
    return batch  # give up after max_ban_retries, let the caller's normal error handling deal with it


def fetch_full_history(client: BinanceFuturesClient, symbol: str, interval: str,
                        months_back: int) -> Optional[pd.DataFrame]:
    """
    Pages through Binance's klines endpoint (1500 candles per request, the
    exchange's max) using startTime, from `months_back` months ago up to
    now, and returns one combined DataFrame - same column layout/typing as
    bot_core._fetch_multi_timeframe uses live, so the analysis engine sees
    identical data shape whether it's backtesting or trading live.
    """
    interval_ms = INTERVAL_MS.get(interval)
    if interval_ms is None:
        logger.error(f"Unknown interval '{interval}' - add it to INTERVAL_MS.")
        return None

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=30 * months_back)).timestamp() * 1000)

    all_rows: List[list] = []
    cursor = start_ms
    max_requests = 200  # safety cap (200 * 1500 candles is far more than any months_back needs)

    for _ in range(max_requests):
        if cursor >= end_ms:
            break
        try:
            batch = klines_with_ban_handling(
                client, f"{symbol} {interval}",
                symbol=symbol, interval=interval, limit=1500,
                start_time=cursor, end_time=end_ms
            )
        except Exception as e:
            logger.warning(f"{symbol} {interval}: klines request failed at cursor={cursor}: {e}")
            break  # FIX (partial-history discard bug): break, don't return None -
            # whatever was already collected in all_rows is still used below.

        # FIX ('backtest failed: -1' bug, round 2): the known -1121-style
        # error dict is handled below, but ANY other unexpected response
        # shape (a differently-shaped rate-limit body, a transient
        # malformed payload, etc.) used to crash unguarded on
        # `batch[-1][0]` with a bare, opaque exception (e.g. KeyError(-1))
        # that propagated all the way up to main() as the unhelpful
        # "{symbol}: backtest failed: -1" message - with NO clue what
        # actually went wrong. Everything below is now wrapped so any such
        # surprise is caught here, logged with the actual type/content of
        # the bad response, and the symbol is skipped cleanly instead of
        # crashing.
        try:
            # FIX ('backtest failed: -1' bug): when a symbol is invalid, delisted,
            # or renamed on Binance Futures (e.g. an old TOP_N_COINS entry that
            # no longer exists as-is), the klines endpoint returns a JSON error
            # OBJECT like {"code": -1121, "msg": "Invalid symbol."} instead of a
            # list of candles. That dict is truthy and iterable, so without this
            # check it silently got extended into all_rows (as its string keys)
            # and then `batch[-1][0]` crashed with a bare KeyError(-1) deep in
            # the pagination loop - which surfaced up in main() as the
            # unhelpful "{symbol}: backtest failed: -1" message. Now it's
            # detected immediately and skipped with a clear reason instead.
            #
            # FIX (partial-history discard bug): confirmed via web search that
            # BATUSDT/AVAXUSDT/ICPUSDT are all still valid, actively-traded
            # Binance Futures symbols (not delisted/renamed) - yet they were
            # showing up as "incomplete history, skipping". The real cause:
            # if a LATER page (e.g. month 7 of 9) hit a persistent rate-limit
            # ban that outlasted klines_with_ban_handling's retries, this
            # branch fired and discarded ALL already-collected months of
            # good data via `return None`, even though the symbol/data were
            # completely fine. Now: if we already have SOME rows, we keep
            # them (break out and use what we've got) instead of throwing
            # everything away. Only a symbol that fails on its very FIRST
            # page (all_rows still empty - the real "invalid symbol" case,
            # like the old plain SHIBUSDT) still results in None.
            if isinstance(batch, dict):
                if all_rows:
                    logger.warning(f"{symbol} {interval}: Binance returned an error "
                                    f"(code={batch.get('code')}, msg={batch.get('msg')}) after already "
                                    f"collecting {len(all_rows)} candles - using partial history instead "
                                    f"of discarding it.")
                    break
                logger.warning(f"{symbol} {interval}: Binance returned an error instead of candles "
                                f"(code={batch.get('code')}, msg={batch.get('msg')}) - symbol is "
                                f"likely invalid/delisted/renamed on Futures. Skipping this symbol.")
                return None

            if not batch:
                break

            all_rows.extend(batch)

            last_open_time = int(batch[-1][0])
            next_cursor = last_open_time + interval_ms
            if next_cursor <= cursor:
                break  # exchange returned no forward progress - stop instead of looping forever
            cursor = next_cursor

            if len(batch) < 1500:
                break  # short batch means we've caught up to "now"
        except Exception as e:
            if all_rows:
                logger.warning(f"{symbol} {interval}: unexpected response shape after already "
                                f"collecting {len(all_rows)} candles ({e!r}) - using partial history "
                                f"instead of discarding it.")
                break
            logger.warning(f"{symbol} {interval}: unexpected response shape while paginating "
                            f"(type={type(batch).__name__}, sample={str(batch)[:200]!r}) - {e!r}. "
                            f"Skipping this symbol.")
            return None

        time.sleep(REQUEST_PACING_SECONDS)  # be gentle with rate limits across a multi-month pull

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=KLINE_COLUMNS)
    for col in ["timestamp", "open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def slice_up_to(df: pd.DataFrame, current_time: int, limit: int) -> pd.DataFrame:
    """Returns the last `limit` candles STRICTLY BEFORE current_time - i.e.
    exactly the window the live bot would have seen if 'now' were
    current_time (candles that have actually CLOSED by then, never the
    candle that's still forming as current_time itself).

    FIX (user request, look-ahead bias): this previously used `<=`, which
    included the candle whose OWN open timestamp equals current_time -
    but that candle is the one just STARTING at current_time, so its
    high/low/close aren't actually known yet at that moment. Using them
    anyway let the analysis "see" how that candle's price action played
    out before it happened, making backtested win-rate look better than
    what the live bot - which only ever sees already-closed candles -
    could actually achieve. Changed to `<` so only genuinely-closed
    candles are ever used for signal computation."""
    sliced = df[df["timestamp"] < current_time]
    return sliced.tail(limit).reset_index(drop=True)


def get_smt_correlated_symbol(symbol: str) -> str:
    """
    FIX (calibration/live mismatch): mirrors
    bot_core.HackerAIBot._get_smt_correlated_symbol() exactly, so the
    calibration backtest exercises Tool 1's SMT Divergence sub-feature
    against the SAME correlated reference pair the live bot uses - instead
    of never triggering it at all (see run_calibration_for_symbol).
    """
    if symbol in SMT_CORRELATED_MAP:
        return SMT_CORRELATED_MAP[symbol]
    if symbol == "BTCUSDT":
        return "ETHUSDT"
    return "BTCUSDT"


def simulate_outcome(lower_df: pd.DataFrame, entry_idx: int, direction: str,
                      tp_price: float, sl_price: float) -> Optional[bool]:
    """Walk forward on the lower timeframe candle-by-candle until TP or SL is
    hit. Returns True (win), False (loss), or None if neither resolved
    within the lookahead window (that setup is discarded - not a fixed
    "loss", genuinely unresolved so counting it either way would be wrong)."""
    end_idx = min(entry_idx + FORWARD_LOOKAHEAD_CANDLES, len(lower_df) - 1)
    for i in range(entry_idx + 1, end_idx + 1):
        high = lower_df["high"].iloc[i]
        low = lower_df["low"].iloc[i]
        if direction == "BUY":
            hit_sl = low <= sl_price
            hit_tp = high >= tp_price
        else:
            hit_sl = high >= sl_price
            hit_tp = low <= tp_price

        if hit_sl and hit_tp:
            return False  # can't tell which came first intra-candle - count as loss
        if hit_tp:
            return True
        if hit_sl:
            return False
    return None


def run_calibration_for_symbol(engine: AnalysisEngine, client: BinanceFuturesClient,
                                symbol: str, months_back: int,
                                correlated_histories: Optional[Dict[str, Dict[str, pd.DataFrame]]] = None) -> Tuple[List[Dict], Optional[pd.DataFrame]]:
    """Returns (labeled, lower_df): labeled is a list of {"score": float,
    "won": bool} setups for one symbol, using the bot's OWN current
    fixed-percent TP/SL (TAKE_PROFIT_PERCENT/STOP_LOSS_PERCENT) - this
    calibrates the score against exactly what the live bot actually trades
    with today. lower_df is the raw fetched lower-timeframe (15m) history -
    ADDED (user request) so callers can reuse it for
    run_pattern_calibration_for_symbol() below without a second fetch of
    the same data.

    FIX (calibration/live mismatch): the live bot's Tool 1 also uses SMT
    Divergence (needs a correlated symbol's candles) and Macro Structure /
    Old Highs-Lows (needs daily candles) - see bot_core._fetch_multi_timeframe.
    This used to only ever pass higher/medium/lower to the analysis engine,
    so those Tool 1 sub-features silently never triggered during
    calibration, even though they DO trigger live - meaning the calibration
    table was built against a slightly different (incomplete) version of
    Tool 1 than what's actually trading. Now daily candles are fetched per
    symbol here, and correlated-symbol candles (pre-fetched once in main()
    and passed in via correlated_histories) are sliced the same way as
    higher/medium/lower below - identical to what the live bot assembles."""
    logger.info(f"Fetching history for {symbol}...")

    higher_df = fetch_full_history(client, symbol, TIMEFRAMES["higher"], months_back)
    time.sleep(REQUEST_PACING_SECONDS)
    medium_df = fetch_full_history(client, symbol, TIMEFRAMES["medium"], months_back)
    time.sleep(REQUEST_PACING_SECONDS)
    lower_df = fetch_full_history(client, symbol, TIMEFRAMES["lower"], months_back)

    if higher_df is None or medium_df is None or lower_df is None:
        logger.warning(f"{symbol}: incomplete history, skipping")
        return [], None

    time.sleep(REQUEST_PACING_SECONDS)
    daily_df = fetch_full_history(client, symbol, "1d", months_back)
    if daily_df is None:
        logger.debug(f"{symbol}: daily candles unavailable - Macro Structure / "
                      f"Old Highs-Lows will no-op for this symbol (matches live bot's "
                      f"own no-op fallback when the daily fetch fails).")

    corr_symbol = get_smt_correlated_symbol(symbol)
    corr_hist = (correlated_histories or {}).get(corr_symbol)
    if SMT_DIVERGENCE_ENABLED and not corr_hist:
        logger.debug(f"{symbol}: no pre-fetched correlated history for {corr_symbol} - "
                      f"SMT Divergence will no-op for this symbol.")

    logger.info(f"{symbol}: {len(higher_df)} {TIMEFRAMES['higher']} / "
                f"{len(medium_df)} {TIMEFRAMES['medium']} / "
                f"{len(lower_df)} {TIMEFRAMES['lower']} candles fetched")

    tp_pct = TAKE_PROFIT_PERCENT / 100.0
    sl_pct = STOP_LOSS_PERCENT / 100.0
    warmup = 60

    labeled: List[Dict] = []

    for i in range(warmup, len(medium_df), STRIDE):
        current_time = int(medium_df["timestamp"].iloc[i])

        higher_slice = slice_up_to(higher_df, current_time, LOOKBACK_LIMIT["higher"])
        medium_slice = slice_up_to(medium_df, current_time, LOOKBACK_LIMIT["medium"])
        lower_slice = slice_up_to(lower_df, current_time, LOOKBACK_LIMIT["lower"])

        if len(higher_slice) < 50 or len(medium_slice) < 50 or len(lower_slice) < 50:
            continue

        try:
            mtf_input = {"higher": higher_slice, "medium": medium_slice, "lower": lower_slice}
            if corr_hist:
                mtf_input["correlated"] = {
                    "higher": slice_up_to(corr_hist["higher"], current_time, LOOKBACK_LIMIT["higher"]),
                    "medium": slice_up_to(corr_hist["medium"], current_time, LOOKBACK_LIMIT["medium"]),
                    "lower": slice_up_to(corr_hist["lower"], current_time, LOOKBACK_LIMIT["lower"]),
                }
            if daily_df is not None:
                mtf_input["daily"] = slice_up_to(daily_df, current_time, DAILY_HISTORY_CANDLES)
            result = engine.multi_timeframe_analysis(mtf_input)
        except Exception as e:
            logger.debug(f"{symbol}: analysis error at {current_time}: {e}")
            continue

        final = result.get("final_signal", {})
        decision = final.get("decision", "HOLD")
        tools_agreeing = final.get("tools_agreeing", 0)

        if decision not in ("BUY", "SELL") or tools_agreeing < MIN_TOOLS_MATCH:
            continue

        # FIX (user request, look-ahead bias): mirrors the slice_up_to fix
        # above - use the last lower-timeframe candle STRICTLY BEFORE
        # current_time (already fully closed), not `<=` which could
        # include a candle still forming exactly at current_time whose
        # close price wouldn't actually be known yet. This is what
        # decides both the entry price used AND (via entry_idx) exactly
        # where simulate_outcome starts walking FORWARD from - so this is
        # also what keeps the outcome-checking side strictly separated
        # from the signal-computation side, per the two-sided nature of
        # this fix (past-only for the signal, future-only for the
        # outcome).
        lower_full_idx = lower_df[lower_df["timestamp"] < current_time].index
        if len(lower_full_idx) == 0:
            continue
        entry_idx = int(lower_full_idx[-1])
        entry_price = float(lower_df["close"].iloc[entry_idx])

        if decision == "BUY":
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        won = simulate_outcome(lower_df, entry_idx, decision, tp_price, sl_price)
        if won is None:
            continue  # unresolved within lookahead - discard, don't guess

        raw_score = float(final.get("profit_chance", 0.0))
        entry_hour_utc = datetime.utcfromtimestamp(current_time / 1000).hour
        labeled.append({"score": raw_score, "won": won, "hour": entry_hour_utc})

    logger.info(f"{symbol}: {len(labeled)} labeled setups collected")
    return labeled, lower_df


def run_pattern_calibration_for_symbol(lower_df: pd.DataFrame, symbol: str,
                                        stride: int = PATTERN_STRIDE) -> List[Dict]:
    """
    ADDED (user request - calibrate PATTERN_MIN_CONFIDENCE against real
    backtested outcomes instead of a random-noise false-positive test).

    Walk-forward backtests all 6 classical chart-pattern detectors
    (pattern_engine.py) against the SAME lower-timeframe (15m) candles
    run_calibration_for_symbol() already fetched for this symbol -
    mirroring exactly how bot_core._try_pattern_engine_entry() calls
    pattern_engine.detect_best_pattern(lower_tf, ...) live, just walked
    forward through history instead of at one live moment. No extra
    Binance calls - lower_df is reused, not re-fetched.

    Uses min_confidence=0 here (not the live PATTERN_MIN_CONFIDENCE) to
    capture the FULL confidence distribution - a run using the live
    threshold would only ever see already-cherry-picked high scores and
    could never tell us whether lower confidence tiers are actually fine
    too, or genuinely bad.

    Each match's OWN target/invalidation prices (the pattern's real
    measured-move levels - NOT a fixed TAKE_PROFIT_PERCENT/
    STOP_LOSS_PERCENT like the main Tool-5 path uses) are passed straight
    into the SAME simulate_outcome() this file already uses for the main
    path, since it already takes tp_price/sl_price as absolute prices.
    reward_pct/risk_pct (the pattern's own real distances, as a % of
    entry price) are recorded per match too, since a meaningful "is this
    confidence tier actually profitable" verdict needs each bucket's real
    average reward:risk, not just its win rate - see
    build_pattern_buckets() below.
    """
    labeled: List[Dict] = []
    if lower_df is None or len(lower_df) < LOOKBACK_LIMIT["lower"] + 10:
        return labeled

    for i in range(LOOKBACK_LIMIT["lower"], len(lower_df) - 1, stride):
        window_start = max(0, i - LOOKBACK_LIMIT["lower"] + 1)
        window = lower_df.iloc[window_start:i + 1]
        try:
            match = pattern_engine.detect_best_pattern(window, min_confidence=0)
        except Exception:
            match = None
        if not match:
            continue

        direction = match.get("direction")
        target = match.get("target")
        invalidation = match.get("invalidation")
        confidence = match.get("confidence")
        if direction not in ("BUY", "SELL") or target is None or invalidation is None or confidence is None:
            continue

        entry_price = float(lower_df["close"].iloc[i])
        if entry_price <= 0:
            continue

        try:
            won = simulate_outcome(lower_df, i, direction, target, invalidation)
        except Exception:
            continue
        if won is None:
            continue  # neither TP nor SL resolved within the lookahead window - inconclusive, skip

        labeled.append({
            "score": confidence, "won": won,
            "reward_pct": abs(target - entry_price) / entry_price * 100,
            "risk_pct": abs(entry_price - invalidation) / entry_price * 100,
            "pattern": match.get("pattern", "?"),
        })

    logger.info(f"{symbol}: {len(labeled)} pattern-match setups collected")
    return labeled


def build_buckets(labeled: List[Dict]) -> Dict[str, Dict]:
    """Groups labeled setups into 10-wide score buckets (0-10 ... 90-100)
    and computes the actual win-rate observed in each bucket - this is the
    table analysis_engine._get_calibrated_profit_chance() looks up."""
    buckets: Dict[str, Dict] = {}
    for floor in range(0, 100, 10):
        buckets[f"{floor}-{floor + 10}"] = {"wins": 0, "samples": 0}

    for item in labeled:
        floor = int(item["score"] // 10) * 10
        floor = min(floor, 90)
        key = f"{floor}-{floor + 10}"
        buckets[key]["samples"] += 1
        if item["won"]:
            buckets[key]["wins"] += 1

    result: Dict[str, Dict] = {}
    for key, b in buckets.items():
        win_rate = round(b["wins"] / b["samples"] * 100, 2) if b["samples"] else None
        result[key] = {"win_rate": win_rate, "samples": b["samples"]}
    return result


def build_hour_buckets(labeled: List[Dict]) -> Dict[str, Dict]:
    """Groups labeled setups by entry hour (UTC, 0-23) and computes the
    real win-rate observed in each hour - lets us check whether trading
    session/time-of-day (e.g. Asian vs London/NY overlap) actually makes a
    measurable difference for this bot's setups, instead of assuming it
    does or doesn't based on theory alone."""
    buckets: Dict[str, Dict] = {f"{h:02d}:00-{h:02d}:59": {"wins": 0, "samples": 0} for h in range(24)}

    for item in labeled:
        key = f"{item['hour']:02d}:00-{item['hour']:02d}:59"
        buckets[key]["samples"] += 1
        if item["won"]:
            buckets[key]["wins"] += 1

    result: Dict[str, Dict] = {}
    for key, b in buckets.items():
        win_rate = round(b["wins"] / b["samples"] * 100, 2) if b["samples"] else None
        result[key] = {"win_rate": win_rate, "samples": b["samples"]}
    return result


# ADDED (user request - keep TRADING_HOURS_FILTER's best-hours list
# current without manual hand-editing of config.py every time). Same
# breakeven formula config.py's TRADING_HOURS_FILTER comment already
# documents: p = (SL + 2*fee) / (TP + SL) - only depends on this bot's
# ACTUAL configured TAKE_PROFIT_PERCENT/STOP_LOSS_PERCENT/
# TRADING_FEE_PERCENT, so it stays correct even if those are re-tuned
# later without anyone having to remember to update this too.
MIN_HOUR_SAMPLES = 200  # don't trust an hour's win-rate with fewer
# backtested setups than this - same "not enough data yet, leave it
# alone" convention SMART_HOURS_GUARD already uses live (SMART_HOURS_
# MIN_SAMPLES in config.py). The original run had ~2,500-2,900 samples
# per hour, so this floor only bites on an unusually short/thin run.
MIN_QUALIFYING_HOURS = 3  # if fewer than this many hours clear breakeven,
# treat the run as too thin/unusual to trust for something as
# consequential as the live trading window - see
# write_trading_hours_override() below for what happens instead.


def select_best_trading_hours(hour_buckets: Dict[str, Dict], breakeven_percent: float,
                               min_samples: int = MIN_HOUR_SAMPLES) -> List[int]:
    """Returns the sorted UTC hours (0-23) whose backtested win_rate clears
    `breakeven_percent`, using only hours with >= `min_samples` setups -
    a thin-sample hour is skipped entirely (neither included nor
    excluded with confidence) rather than trusted either way."""
    qualifying = []
    for key, bucket in hour_buckets.items():
        win_rate = bucket.get("win_rate")
        samples = bucket.get("samples", 0)
        if win_rate is None or samples < min_samples:
            continue
        if win_rate > breakeven_percent:
            qualifying.append(int(key.split(":")[0]))
    return sorted(qualifying)


def write_trading_hours_override(hour_buckets: Dict[str, Dict], months_backtested: int, total_samples: int):
    """
    Derives which UTC hours clear this bot's real breakeven from the
    hour_buckets this run just computed, and writes them to
    TRADING_HOURS_OVERRIDE_FILE. config.py reads this file at import time
    for ALLOWED_TRADING_HOURS_UTC and falls back to its hardcoded default
    on anything missing/invalid (see config.py) - so this is purely
    additive: re-running this script keeps the live filter's hours
    current; never running it (or hitting the safety gate below) leaves
    whatever was there completely untouched.

    SAFETY GATE: fewer than MIN_QUALIFYING_HOURS clearing breakeven means
    this run's data is too thin/unusual to trust for the live trading
    window - the override file is left exactly as it was (created or
    not), nothing is written, and a loud warning explains why. This
    exists specifically so a bad/thin calibration run can never silently
    shrink live trading down to almost nothing - the same failure mode
    that caused zero trades before, just from a different direction.
    """
    breakeven = (STOP_LOSS_PERCENT + 2 * TRADING_FEE_PERCENT) / (TAKE_PROFIT_PERCENT + STOP_LOSS_PERCENT) * 100
    best_hours = select_best_trading_hours(hour_buckets, breakeven)

    logger.info("=" * 65)
    logger.info(f"Real breakeven (TP={TAKE_PROFIT_PERCENT}%/SL={STOP_LOSS_PERCENT}%/"
                f"fee={TRADING_FEE_PERCENT}%/side): {breakeven:.2f}%")

    if len(best_hours) < MIN_QUALIFYING_HOURS:
        logger.warning(
            f"Only {len(best_hours)} hour(s) cleared breakeven with >= {MIN_HOUR_SAMPLES} "
            f"samples each - too few to trust for the live trading window. "
            f"TRADING_HOURS_OVERRIDE_FILE was left UNCHANGED (existing value, if any, "
            f"still applies). Try a longer --months window for more samples per hour."
        )
        logger.info("=" * 65)
        return

    old_hours = None
    try:
        with open(TRADING_HOURS_OVERRIDE_FILE, "r") as f:
            old_hours = json.load(f).get("allowed_hours_utc")
    except Exception:
        old_hours = None

    override = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": months_backtested,
        "total_samples": total_samples,
        "breakeven_percent": round(breakeven, 2),
        "min_samples_per_hour": MIN_HOUR_SAMPLES,
        "allowed_hours_utc": best_hours,
        "hour_buckets": hour_buckets,
    }
    with open(TRADING_HOURS_OVERRIDE_FILE, "w") as f:
        json.dump(override, f, indent=2)

    logger.info(f"Best hours (UTC) this run: {best_hours}")
    if old_hours is not None and sorted(old_hours) != best_hours:
        logger.info(f"  (was: {sorted(old_hours)} - CHANGED)")
    elif old_hours is not None:
        logger.info("  (unchanged from previous run)")
    logger.info(f"TRADING_HOURS_OVERRIDE_FILE updated: {TRADING_HOURS_OVERRIDE_FILE}")
    logger.info("Restart the bot (e.g. pm2 restart ...) for the new hours to take effect.")
    logger.info("=" * 65)


def build_pattern_buckets(labeled: List[Dict]) -> Dict[str, Dict]:
    """
    ADDED (user request - calibrate PATTERN_MIN_CONFIDENCE against real
    outcomes). Same 10-wide bucketing scheme as build_buckets() above, but
    each bucket ALSO tracks the average reward_pct/risk_pct of its own
    matches and a resulting expectancy_pct - because pattern trades use
    each pattern's own measured-move target/invalidation (see
    run_pattern_calibration_for_symbol above), a bucket's real
    profitability depends on its own actual reward:risk, not a single
    fixed breakeven percentage the way the main Tool-5 path's fixed TP/SL
    allows. expectancy_pct = win_rate*(avg_reward - fee) -
    (1-win_rate)*(avg_risk + fee), fee = 2*TRADING_FEE_PERCENT (round
    trip, both sides) - positive means that bucket's setups were
    genuinely profitable on average in this backtest, after fees;
    negative means they weren't, regardless of how high the win rate
    looks in isolation (a bucket can have a >50% win rate and still be
    unprofitable if its average loss is much bigger than its average win,
    or vice versa).
    """
    buckets: Dict[str, Dict] = {}
    for b in labeled:
        score = b["score"]
        floor = min(int(score // 10) * 10, 90)
        key = f"{floor}-{floor + 10}"
        bucket = buckets.setdefault(key, {"wins": 0, "samples": 0, "reward_sum": 0.0, "risk_sum": 0.0})
        bucket["samples"] += 1
        if b["won"]:
            bucket["wins"] += 1
        bucket["reward_sum"] += b.get("reward_pct", 0.0)
        bucket["risk_sum"] += b.get("risk_pct", 0.0)

    fee_rt = 2 * TRADING_FEE_PERCENT
    result: Dict[str, Dict] = {}
    for key, b in buckets.items():
        samples = b["samples"]
        win_rate = round(b["wins"] / samples * 100, 2) if samples else None
        avg_reward = round(b["reward_sum"] / samples, 3) if samples else None
        avg_risk = round(b["risk_sum"] / samples, 3) if samples else None
        expectancy = None
        if win_rate is not None and avg_reward is not None and avg_risk is not None:
            wr = win_rate / 100
            expectancy = round(wr * (avg_reward - fee_rt) - (1 - wr) * (avg_risk + fee_rt), 4)
        result[key] = {
            "win_rate": win_rate, "samples": samples,
            "avg_reward_pct": avg_reward, "avg_risk_pct": avg_risk,
            "expectancy_pct": expectancy,
        }
    return result


def select_pattern_min_confidence(pattern_buckets: Dict[str, Dict],
                                   min_samples: int = PATTERN_MIN_BUCKET_SAMPLES) -> Optional[int]:
    """
    Returns the lowest confidence floor F (0-100, step 10) such that EVERY
    bucket from F up to 90-100 has both enough samples AND positive
    expectancy_pct - i.e. the lowest threshold PATTERN_MIN_CONFIDENCE
    could safely be set to (as a >= cutoff, everything at/above it is
    admitted, so every one of those buckets needs to genuinely clear the
    bar, not just the single bucket at the boundary). Returns None if even
    the top 90-100 bucket doesn't qualify, or lacks samples - "nothing
    trustworthy in this run" rather than a wrong guess.
    """
    floors = list(range(90, -1, -10))
    best_floor = None
    for floor in floors:
        key = f"{floor}-{floor + 10}"
        bucket = pattern_buckets.get(key)
        if not bucket or bucket.get("samples", 0) < min_samples:
            break
        if bucket.get("expectancy_pct") is None or bucket["expectancy_pct"] <= 0:
            break
        best_floor = floor
    return best_floor


def write_pattern_calibration_override(pattern_buckets: Dict[str, Dict], months_backtested: int, total_samples: int):
    """
    Derives a recommended PATTERN_MIN_CONFIDENCE from pattern_buckets (this
    run's fresh backtest) and writes it to PATTERN_CALIBRATION_FILE.
    config.py reads this file at import time and falls back to its
    hardcoded default (90.0) on anything missing/invalid - see config.py -
    so, exactly like TRADING_HOURS_OVERRIDE_FILE above, this is purely
    additive: re-running this script keeps PATTERN_MIN_CONFIDENCE current;
    never running it (or hitting the safety gate below) leaves whatever
    was there completely untouched.

    SAFETY GATE: select_pattern_min_confidence() returning None (nothing
    trustworthy this run - too few samples anywhere, or no confidence
    tier was genuinely profitable after fees) leaves the override file
    exactly as it was, with a loud warning instead - never writes a
    guess. This is the same protective convention write_trading_hours_
    override() above already uses.
    """
    recommended = select_pattern_min_confidence(pattern_buckets)

    logger.info("=" * 65)
    logger.info("Pattern Engine calibration (all 6 chart patterns, real backtested outcomes):")
    for floor in range(90, -1, -10):
        key = f"{floor}-{floor + 10}"
        b = pattern_buckets.get(key)
        if not b:
            continue
        logger.info(f"  {key:>7}: win_rate={b['win_rate']}%  samples={b['samples']}  "
                    f"avg_reward={b['avg_reward_pct']}%  avg_risk={b['avg_risk_pct']}%  "
                    f"expectancy={b['expectancy_pct']}%")

    if recommended is None:
        logger.warning(
            f"No confidence tier both had >= {PATTERN_MIN_BUCKET_SAMPLES} samples AND positive "
            f"expectancy after fees in this run - too little/unreliable pattern-match data to "
            f"trust for PATTERN_MIN_CONFIDENCE. PATTERN_CALIBRATION_FILE was left UNCHANGED "
            f"(existing value, if any, still applies). Try a longer --months window, or Pattern "
            f"Engine's real edge (if any) may simply need more live trades to confirm."
        )
        logger.info("=" * 65)
        return

    old_value = None
    try:
        with open(PATTERN_CALIBRATION_FILE, "r") as f:
            old_value = json.load(f).get("recommended_min_confidence")
    except Exception:
        old_value = None

    override = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": months_backtested,
        "total_samples": total_samples,
        "min_samples_per_bucket": PATTERN_MIN_BUCKET_SAMPLES,
        "recommended_min_confidence": recommended,
        "pattern_buckets": pattern_buckets,
    }
    with open(PATTERN_CALIBRATION_FILE, "w") as f:
        json.dump(override, f, indent=2)

    logger.info(f"Recommended PATTERN_MIN_CONFIDENCE this run: {recommended}")
    if old_value is not None and old_value != recommended:
        logger.info(f"  (was: {old_value} - CHANGED)")
    elif old_value is not None:
        logger.info("  (unchanged from previous run)")
    logger.info(f"PATTERN_CALIBRATION_FILE updated: {PATTERN_CALIBRATION_FILE}")
    logger.info("Restart the bot (e.g. pm2 restart ...) for the new value to take effect.")
    logger.info("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Calibrate the profit-chance heuristic score against real historical win-rates")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbols (default: TOP_N_COINS from config.py)")
    parser.add_argument("--months", type=int, default=9,
                         help="Months of history to backtest (default: 9)")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE,
                         help=f"Output file path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else TOP_N_COINS

    client = BinanceFuturesClient(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)

    # FIX (calibration/live mismatch): pre-fetch each DISTINCT correlated
    # reference symbol's history ONCE (almost every symbol maps to
    # BTCUSDT/ETHUSDT - see get_smt_correlated_symbol), instead of
    # re-fetching it per-symbol, which would multiply the already-long
    # runtime by ~2x for no benefit.
    correlated_histories: Dict[str, Dict[str, pd.DataFrame]] = {}
    if SMT_DIVERGENCE_ENABLED:
        needed = {get_smt_correlated_symbol(s) for s in symbols} | set(SMT_CORRELATED_MAP.values())
        for corr_symbol in needed:
            logger.info(f"Pre-fetching correlated-symbol history for SMT Divergence: {corr_symbol}")
            h = fetch_full_history(client, corr_symbol, TIMEFRAMES["higher"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            m = fetch_full_history(client, corr_symbol, TIMEFRAMES["medium"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            l = fetch_full_history(client, corr_symbol, TIMEFRAMES["lower"], args.months)
            time.sleep(REQUEST_PACING_SECONDS)
            if h is not None and m is not None and l is not None:
                correlated_histories[corr_symbol] = {"higher": h, "medium": m, "lower": l}
            else:
                logger.warning(f"Could not fetch full history for correlated symbol {corr_symbol} - "
                                f"SMT Divergence will no-op for every symbol mapped to it (same "
                                f"safe no-op behavior the live bot falls back to on a fetch failure).")

    # CALIBRATION_TABLE_FILE points at a file that doesn't exist so the
    # engine used purely for backtesting always returns the RAW heuristic
    # score (never an already-calibrated one) - we're building the table,
    # not consuming it.
    engine_config = {"TIMEFRAMES": TIMEFRAMES, "MIN_TOOLS_MATCH": MIN_TOOLS_MATCH,
                      "MIN_SUBCONCEPTS_PER_TOOL": MIN_SUBCONCEPTS_PER_TOOL,
                      "CALIBRATION_TABLE_FILE": "__no_such_calibration_file__.json",
                      "OLD_HIGH_LOW_MIN_DAYS": OLD_HIGH_LOW_MIN_DAYS,
                      "OLD_HIGH_LOW_MAX_DAYS": OLD_HIGH_LOW_MAX_DAYS}
    engine = AnalysisEngine(engine_config)

    all_labeled: List[Dict] = []
    all_pattern_labeled: List[Dict] = []
    for symbol in symbols:
        try:
            labeled, lower_df = run_calibration_for_symbol(engine, client, symbol, args.months, correlated_histories)
            all_labeled.extend(labeled)
            # ADDED (user request - calibrate PATTERN_MIN_CONFIDENCE):
            # reuses the SAME lower_df just fetched above - zero extra
            # Binance calls for this.
            if lower_df is not None:
                pattern_labeled = run_pattern_calibration_for_symbol(lower_df, symbol)
                all_pattern_labeled.extend(pattern_labeled)
        except Exception as e:
            logger.error(f"{symbol}: calibration failed: {e}")
            continue
        time.sleep(REQUEST_PACING_SECONDS)  # pace between symbols too

    if not all_labeled:
        logger.error("No samples collected - nothing to write. Check API access/logs above.")
        return

    buckets = build_buckets(all_labeled)
    hour_buckets = build_hour_buckets(all_labeled)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "symbols_used": symbols,
        "total_samples": len(all_labeled),
        "buckets": buckets,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    # Hour-of-day breakdown is written to its own file, separate from
    # calibration_table.json. analysis_engine._get_calibrated_profit_chance()
    # only knows how to read the score-bucket schema above - keeping this
    # separate means the live bot's calibration lookup is completely
    # unaffected; this file is purely for us to inspect (e.g. "is UTC 13-16
    # genuinely better than UTC 02-05?") before deciding whether a
    # session/time filter is worth adding.
    hourly_output = {
        "generated_at": datetime.utcnow().isoformat(),
        "months_backtested": args.months,
        "total_samples": len(all_labeled),
        "note": "hour is UTC, entry candle time. Informational only - not read by the live bot.",
        "hour_buckets": hour_buckets,
    }
    hourly_path = "hourly_breakdown.json"
    with open(hourly_path, "w") as f:
        json.dump(hourly_output, f, indent=2)

    logger.info("=" * 65)
    logger.info(f"Calibration table written to {args.output}")
    logger.info(f"Total setups evaluated: {len(all_labeled)}")
    logger.info("-" * 65)
    for key, b in buckets.items():
        logger.info(f"  {key:>7}: win_rate={b['win_rate']}%  samples={b['samples']}")
    logger.info("=" * 65)
    logger.info(f"Hour-of-day (UTC) breakdown written to {hourly_path}")
    logger.info("-" * 65)
    for key, b in hour_buckets.items():
        logger.info(f"  {key}: win_rate={b['win_rate']}%  samples={b['samples']}")
    logger.info("=" * 65)

    # ADDED (user request - auto-apply the freshly backtested best hours
    # to the live TRADING_HOURS_FILTER, instead of a human reading the
    # log above and hand-editing config.py). See
    # write_trading_hours_override() for the full reasoning and its
    # safety gate.
    write_trading_hours_override(hour_buckets, args.months, len(all_labeled))

    # ADDED (user request - calibrate PATTERN_MIN_CONFIDENCE against real
    # outcomes, same run, no extra fetching). all_pattern_labeled was
    # collected above by reusing each symbol's already-fetched lower_df.
    if all_pattern_labeled:
        pattern_buckets = build_pattern_buckets(all_pattern_labeled)
        write_pattern_calibration_override(pattern_buckets, args.months, len(all_pattern_labeled))
    else:
        logger.info("=" * 65)
        logger.info("No pattern-engine matches collected this run (0 samples) - "
                     "PATTERN_CALIBRATION_FILE left unchanged.")
        logger.info("=" * 65)


if __name__ == "__main__":
    main()
