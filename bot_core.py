"""
HackerAI Auto Trading Bot - Core Bot Logic
24/7 Scanning | Balance Crash Protection | 65% Profit Filter
Auto Leverage | Auto Min Notional Check
"""

import logging
import time
import os
import hashlib
import hmac
import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from decimal import Decimal, ROUND_DOWN

# Optional Telegram notifications (per explicit request) - if unavailable
# for any reason, the bot must keep working exactly as before.
try:
    from telegram_notify import send_telegram, format_tp1_hit, format_tp1_extended, format_tp1_closed
    _TELEGRAM_AVAILABLE = True
except Exception:
    _TELEGRAM_AVAILABLE = False

# Optional Telegram remote-control panel (user request) - same fail-safe
# pattern: if unavailable, the bot just runs without it, unaffected.
try:
    from telegram_control import TelegramController
    _TELEGRAM_CONTROL_AVAILABLE = True
except Exception:
    _TELEGRAM_CONTROL_AVAILABLE = False

# Optional Pattern Recognition Engine (user request, Phase 1) - same fail-
# safe pattern: if unavailable or PATTERN_ENGINE_ENABLED=False, the bot's
# normal scan/decision/execute path is 100% unaffected.
try:
    import pattern_engine
    _PATTERN_ENGINE_AVAILABLE = True
except Exception:
    _PATTERN_ENGINE_AVAILABLE = False

from config import *
from analysis_engine import AnalysisEngine
from trade_manager import TradeManager

logger = logging.getLogger(__name__)


class BinanceFuturesClient:
    """Custom Binance Futures client using raw requests (avoid library signing issues)"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

    def _sign(self, params: dict) -> str:
        """
        Build the exact query string that gets signed, and return it WITH
        the signature already appended.

        FIX: previously this returned a dict with params["signature"] set,
        and the caller passed that dict straight to requests' `params=`.
        requests encodes a dict in insertion order, but the signature here
        was computed over a *sorted* (alphabetical) version of the same
        params. So the string Binance received never matched the string
        that was actually signed -> "Signature for this request is not
        valid" (-1022) on every single signed call. Returning the final,
        already-ordered query string (and sending that exact string, not a
        dict) guarantees the bytes signed == the bytes sent.
        """
        query_string = "&".join([f"{k}={self._format_value(v)}" for k, v in sorted(params.items())])
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    @staticmethod
    def _format_value(v) -> str:
        """
        FIX (Precision bug): Python's default str()/f-string formatting for
        a float switches to scientific notation for very small or very
        large numbers (e.g. 0.00001234 -> "1.234e-05"). Binance's API does
        not accept exponential notation for quantity/price fields and
        rejects it as invalid precision. This formats floats as plain
        decimal strings instead, with trailing zeros/dot stripped, leaving
        all other types (str, int, bool) untouched.
        """
        if isinstance(v, float):
            s = f"{v:.8f}".rstrip("0").rstrip(".")
            return s if s else "0"
        return str(v)

    def _get(self, path: str, params: dict = None) -> dict:
        """Signed GET request"""
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query_string = self._sign(params)
        url = f"{self.base_url}{path}?{query_string}"
        resp = self.session.get(url)
        if resp.status_code != 200:
            raise Exception(f"API Error {resp.status_code}: {resp.text}")
        return resp.json()

    def _post(self, path: str, params: dict = None) -> dict:
        """Signed POST request"""
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query_string = self._sign(params)
        url = f"{self.base_url}{path}?{query_string}"
        resp = self.session.post(url)
        if resp.status_code != 200:
            raise Exception(f"API Error {resp.status_code}: {resp.text}")
        return resp.json()

    def _delete(self, path: str, params: dict = None) -> dict:
        """
        Signed DELETE request.
        FIX: cancel_order() below used to send its request via _post() (HTTP
        POST) to the order endpoint, which is Binance's "place a new order"
        method, not "cancel an order" (that's HTTP DELETE). It never
        surfaced before because cancel_order() wasn't actually called
        anywhere in the bot, but it's needed now to manage exchange-side
        SL/TP orders, so it has to send a real DELETE request.
        """
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query_string = self._sign(params)
        url = f"{self.base_url}{path}?{query_string}"
        resp = self.session.delete(url)
        if resp.status_code != 200:
            raise Exception(f"API Error {resp.status_code}: {resp.text}")
        return resp.json()

    # ---- Futures API Methods ----

    def ping(self) -> dict:
        resp = self.session.get(f"{self.base_url}/fapi/v1/ping")
        return resp.json()

    def time(self) -> dict:
        resp = self.session.get(f"{self.base_url}/fapi/v1/time")
        return resp.json()

    def account(self) -> dict:
        return self._get("/fapi/v2/account")

    def position_risk(self, symbol: str = None) -> list:
        """
        Get real current position(s) from Binance. Used to:
        - reconcile local trade-tracking state with the real exchange
          positions after a bot restart
        - verify whether a position is actually still open before trying to
          close it (e.g. an exchange-side SL/TP order may have already
          closed it)
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/fapi/v2/positionRisk", params)

    def exchange_info(self) -> dict:
        resp = self.session.get(f"{self.base_url}/fapi/v1/exchangeInfo")
        return resp.json()

    def klines(self, symbol: str, interval: str, limit: int = 100,
               start_time: int = None, end_time: int = None) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        resp = self.session.get(f"{self.base_url}/fapi/v1/klines", params=params)
        return resp.json()

    def ticker_24hr(self) -> list:
        resp = self.session.get(f"{self.base_url}/fapi/v1/ticker/24hr")
        return resp.json()

    def ticker_price(self, symbol: str = None) -> dict:
        params = {}
        if symbol:
            params["symbol"] = symbol
        resp = self.session.get(f"{self.base_url}/fapi/v1/ticker/price", params=params)
        data = resp.json()
        if symbol:
            return data
        return data

    def funding_rate(self, symbol: str) -> dict:
        """
        ADDED (user request): GET /fapi/v1/premiumIndex - public endpoint
        (same unsigned pattern as klines/ticker_price above, no signing
        needed). Returns current mark price and the current/last funding
        rate for a symbol (lastFundingRate, a fraction e.g. 0.0001 = 0.01%).
        """
        resp = self.session.get(f"{self.base_url}/fapi/v1/premiumIndex", params={"symbol": symbol})
        return resp.json()

    def income(self, symbol: str = None, income_type: str = None,
               start_time: int = None, end_time: int = None,
               limit: int = 100) -> list:
        """
        FIX (missing method): trade_manager.py calls self.client.income(...)
        to pull Binance's own REALIZED_PNL record for the Telegram
        "real PnL" notification, but this method never existed on
        BinanceFuturesClient - every call raised
        AttributeError: 'BinanceFuturesClient' object has no attribute 'income',
        caught by trade_manager's own try/except (so it never crashed the
        bot), but real_pnl_usdt was always None and the log filled with
        this warning on every trade close.
        GET /fapi/v1/income - signed, returns a list of income records.
        """
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self._get("/fapi/v1/income", params)

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })

    def leverage_bracket(self, symbol: str) -> list:
        """
        FIX (real bug found via user's ESPUSDT report): the old code tried
        to read per-symbol max leverage out of a "leverageBrackets" field
        on GET /fapi/v1/exchangeInfo - that field does not exist there
        (confirmed against Binance's docs: leverage bracket data is only
        ever returned by this separate, signed endpoint,
        GET /fapi/v1/leverageBracket). That lookup silently always came
        back empty, so every coin's max leverage was assumed to be the
        hardcoded default of 20, regardless of the coin's real cap - for
        a low-cap coin like ESPUSDT (really capped at 1x), this let the
        bot compute a position sized for far more leverage than Binance
        would actually grant.
        """
        result = self._get("/fapi/v1/leverageBracket", {"symbol": symbol})
        # Binance returns a single object when queried with a symbol, but
        # be defensive - a list wrapping the same object has also been
        # observed on some API versions/wrappers.
        if isinstance(result, list):
            return result[0].get("brackets", []) if result else []
        return result.get("brackets", [])

    def change_margin_type(self, symbol: str, margin_type: str) -> dict:
        """
        ADDED (user request, Telegram-toggleable Isolated/Cross): POST
        /fapi/v1/marginType switches a symbol between ISOLATED and CROSSED
        margin mode. margin_type must be exactly Binance's enum value:
        "ISOLATED" or "CROSSED".
        """
        return self._post("/fapi/v1/marginType", {
            "symbol": symbol,
            "marginType": margin_type
        })

    def new_order(self, symbol: str, side: str, type: str, quantity: float,
                  reduceOnly: bool = False, positionSide: str = None) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "quantity": quantity,
            "reduceOnly": "true" if reduceOnly else "false"
        }
        # FIX: accept positionSide (needed for Hedge Mode accounts). Only
        # send it when provided — omitting it keeps One-way Mode accounts
        # working exactly as before.
        if positionSide:
            params["positionSide"] = positionSide
        return self._post("/fapi/v1/order", params)

    def new_stop_order(self, symbol: str, side: str, stop_price: float, quantity: float,
                        order_type: str = "STOP_MARKET", reduce_only: bool = True,
                        positionSide: str = None) -> dict:
        """
        Place a real resting STOP_MARKET or TAKE_PROFIT_MARKET order on the
        exchange. Unlike the bot's own polling-based SL/TP check (which only
        protects the position while this process is running), this order
        lives on Binance's servers and will trigger even if the bot/VPS
        goes offline.

        FIX (Binance Algo Order migration, effective 2025-12-09): Binance
        stopped accepting STOP_MARKET/TAKE_PROFIT_MARKET on the classic
        POST /fapi/v1/order endpoint (error -4120: "Order type not
        supported for this endpoint. Please use the Algo Order API
        endpoints instead."). Conditional order types now must go through
        the dedicated Algo Order API (POST /fapi/v1/algoOrder), which also
        renames stopPrice -> triggerPrice and requires algoType=CONDITIONAL.
        That endpoint returns "algoId" instead of "orderId", so the result
        is normalized here to also carry "orderId" for callers.
        """
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": stop_price,
            "quantity": quantity,
            "workingType": "MARK_PRICE"
        }
        # FIX: accept positionSide (needed for Hedge Mode accounts), same as
        # new_order(). Binance rejects reduceOnly + positionSide together,
        # so on a Hedge Mode account send positionSide (which already
        # identifies the position being protected) and omit reduceOnly; on
        # a One-way account keep sending reduceOnly exactly as before.
        if positionSide:
            params["positionSide"] = positionSide
        else:
            params["reduceOnly"] = "true" if reduce_only else "false"
        result = self._post("/fapi/v1/algoOrder", params)
        if "algoId" in result and "orderId" not in result:
            result["orderId"] = result["algoId"]
        return result

    def get_order(self, symbol: str, orderId: int = None, origClientOrderId: str = None) -> dict:
        params = {"symbol": symbol}
        if orderId:
            params["orderId"] = orderId
        if origClientOrderId:
            params["origClientOrderId"] = origClientOrderId
        return self._get("/fapi/v1/order", params)

    def query_algo_order(self, symbol: str, algo_id: int) -> dict:
        """
        ADDED (user request, definitive auto-vs-manual close detection):
        GET /fapi/v1/algoOrder - checks a SPECIFIC conditional (algo)
        order's status by algoId, the same "orderId" new_stop_order()
        returns and trade["sl_order_id"]/["tp_order_id"] already store.
        Response includes "actualPrice" - per Binance's own docs, this is
        only ever populated once the order has actually been TRIGGERED
        and FILLED in the matching engine (stays "0.00000" otherwise,
        e.g. if still resting or cancelled unfilled) - so a non-zero
        actualPrice here is definitive, first-party proof the SL/TP order
        genuinely filled, not a guess based on price proximity.
        """
        return self._get("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def cancel_order(self, symbol: str, orderId: int = None) -> dict:
        """
        FIX (Binance Algo Order migration): this is only ever called (from
        trade_manager.py) to cancel the resting SL/TP orders placed via
        new_stop_order(), which now live in Binance's Algo Order system.
        Those must be cancelled via DELETE /fapi/v1/algoOrder using
        algoId — the same value new_stop_order() returns (and stores) as
        "orderId", so no caller changes are needed.
        """
        params = {}
        if orderId:
            params["algoId"] = orderId
        return self._delete("/fapi/v1/algoOrder", params)


class HackerAIBot:
    """Main bot class - 24/7 operation with auto trade management"""

    def __init__(self, config: Dict):
        self.config = config
        self.running = False
        self.paused = False
        self.waiting_for_balance = False

        # ====== FIXED: Custom Binance Futures Client ======
        self.client = BinanceFuturesClient(
            api_key=config["BINANCE_API_KEY"],
            api_secret=config["BINANCE_API_SECRET"],
            testnet=config.get("BINANCE_TESTNET", False)
        )

        # Test connection
        try:
            ping = self.client.ping()
            logger.info(f"✅ Binance Futures API connected (ping: {ping})")
            srv_time = self.client.time()
            logger.info(f"🕐 Server time: {srv_time.get('serverTime', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Binance Futures connection failed: {e}")

        # Initialize engines
        self.analysis_engine = AnalysisEngine(config)
        self.trade_manager = TradeManager(config, self.client)

        # FIX (TP1 -> TP2 continuation): give TradeManager a way to ask
        # "should this trade extend past TP1?" the instant TP1 is hit.
        # TradeManager has no analysis engine or OHLC access of its own,
        # so it calls back into bot_core to get a fresh answer.
        self.trade_manager.set_tp1_reanalysis_callback(self._tp1_reanalysis_decision)

        # FIX (user request, pattern-engine re-entry loop): track, per
        # symbol, the datetime until which a NEW pattern-engine trade is
        # blocked - set whenever a pattern-based trade closes (see
        # _on_trade_closed), read in _try_pattern_engine_entry. This alone
        # does not change anything about the normal Tool 5 path - only
        # pattern-engine entries are ever gated by this.
        self.pattern_cooldown_until: Dict[str, datetime] = {}
        self.trade_manager.set_on_trade_closed_callback(self._on_trade_closed)

        # State
        self.balance = 0.0
        self.available_balance = 0.0
        self.account_info = {}
        self.analysis_cache = {}
        self.scan_count = 0
        self.consecutive_balance_errors = 0
        self.last_trade_time = {}

        # FIX (Performance Bug): exchange_info() was being called via a fresh
        # API request in BOTH _execute_trade() and _round_quantity() for every
        # single trade attempt. When scanning 40+ coins this produced hundreds
        # of duplicate heavy API calls per cycle and risked hitting Binance's
        # rate limit. exchangeInfo barely changes, so it's now cached and
        # refreshed at most once per hour (see _get_exchange_info()).
        self._exchange_info_cache = None
        self._exchange_info_cache_time = 0.0
        self._exchange_info_ttl = 3600  # seconds

        logger.info("🤖 HackerAI Bot initialized (24/7 Mode)")

        # Telegram remote control (user request) - safe toggles + pause/
        # resume + status, admin-chat-only. Never blocks/crashes the bot
        # if unavailable or misconfigured.
        self.telegram_controller = None
        if _TELEGRAM_CONTROL_AVAILABLE:
            try:
                self.telegram_controller = TelegramController(self, config)
            except Exception as e:
                logger.warning(f"⚠️ Telegram control panel could not be initialized: {e}")

    def start(self):
        """Start the bot"""
        if self.running:
            logger.warning("⚠️ Bot already running")
            return

        self.running = True
        # FIX: this used to unconditionally reset self.paused = False here,
        # which silently discarded a "paused" state restored from
        # settings_override.json (via TelegramController, loaded earlier in
        # __init__) every time the bot started/restarted - so pausing via
        # Telegram, then having PM2 crash-restart the process, would
        # silently un-pause it. self.paused already defaults to False at
        # __init__ and is only ever True if a saved override explicitly set
        # it - so it's left untouched here.
        logger.info("🚀 HackerAI Bot STARTING in 24/7 mode..." +
                    (" (resuming in PAUSED state from a saved setting)" if self.paused else ""))

        # FIX (Persistence Bug): cross-check any trades restored from disk
        # against what's actually open on Binance right now, in case
        # positions were closed (manually, or by an exchange-side SL/TP
        # order) while the bot was offline.
        self.trade_manager.reconcile_with_exchange()

        self.trade_manager.start_monitoring()

        # FIX: TelegramController was being constructed in __init__ but its
        # .start() (which actually spawns the polling thread) was never
        # called - the control panel was built but silently never ran.
        if self.telegram_controller:
            self.telegram_controller.start()

        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("⏹️ Bot stopped by user")
        except Exception as e:
            logger.error(f"💥 Fatal error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stop()

    def stop(self):
        """Stop the bot"""
        self.running = False
        self.trade_manager.stop_monitoring()
        if self.telegram_controller:
            self.telegram_controller.stop()
        logger.info("⏹️ HackerAI Bot STOPPED")

    def _main_loop(self):
        """Main trading loop - runs 24/7"""
        while self.running:
            try:
                if self.paused:
                    time.sleep(5)
                    continue

                # Update balance with crash protection
                if not self._update_account_info_with_retry():
                    time.sleep(self.config.get("BALANCE_CHECK_INTERVAL", 60))
                    continue

                # Get top-N coins (TOP_N_COIN_COUNT, default 50)
                coins_to_scan = self._get_top_coins()

                # Scan all coins 24/7
                self._scan_coins_247(coins_to_scan)

                # Update open trades
                self._update_open_trades()

                # REMOVED (per explicit request): reversal-signal mid-trade
                # close. Trades now ONLY close via STOP_LOSS, TAKE_PROFIT,
                # or the trailing stop - never because a fresh analysis
                # scan flipped direction on an already-open trade. See
                # _check_trade_reversals() below, kept but no longer
                # called, in case this is ever wanted back.

                # Log status every 10 scans
                if self.scan_count % 10 == 0:
                    self._log_status()

                # Sleep between scans
                time.sleep(self.config.get("SCAN_INTERVAL_SECONDS", 30))

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(60)

    def _update_account_info_with_retry(self) -> bool:
        """Update account info with balance crash protection"""
        try:
            account = self.client.account()
            self.account_info = account

            for asset in account.get("assets", []):
                if asset["asset"] == "USDT":
                    self.balance = float(asset["walletBalance"])
                    # FIX (root cause of -2019 "Margin is insufficient" on
                    # EVERY order): walletBalance is TOTAL equity - it does
                    # NOT subtract margin already locked in currently-open
                    # positions/orders. New-trade sizing was using
                    # walletBalance directly (5% of TOTAL equity), so once
                    # even one position was open, every subsequent order
                    # tried to use margin that was already spoken for and
                    # Binance rejected it - regardless of how healthy
                    # walletBalance still looked. availableBalance is what
                    # Binance itself reports as actually free to use for a
                    # new order right now; used for margin sizing below.
                    self.available_balance = float(asset.get("availableBalance", asset["walletBalance"]))
                    break

            self.consecutive_balance_errors = 0

            if self.balance <= 0:
                if not self.waiting_for_balance:
                    logger.warning("⚠️ Balance is 0! Waiting for funds...")
                self.waiting_for_balance = True
                return False

            if self.waiting_for_balance:
                logger.info(f"✅ Funds detected! Balance: ${self.balance:.2f} USDT. Resuming trading.")
                self.waiting_for_balance = False

            return True

        except Exception as e:
            self.consecutive_balance_errors += 1
            if self.consecutive_balance_errors >= 3:
                if not self.waiting_for_balance:
                    logger.warning(f"⚠️ Balance fetch failed {self.consecutive_balance_errors}x. Waiting...")
                self.waiting_for_balance = True
            logger.error(f"Binance API error (account): {e}")
            return False

    def _get_top_coins(self) -> List[str]:
        """Get top-N coins from Binance by volume (N = TOP_N_COIN_COUNT, default 50)"""
        coins = self.config.get("TOP_N_COINS", TOP_N_COINS)
        try:
            tickers = self.client.ticker_24hr()
            usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
            sorted_by_volume = sorted(usdt_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
            top_n = self.config.get("TOP_N_COIN_COUNT", 50)
            top_coins = [t["symbol"] for t in sorted_by_volume[:top_n]]
            if top_coins:
                coins = top_coins
        except Exception:
            pass
        return coins

    def _scan_coins_247(self, coins: List[str]):
        """24/7 scanning with trade execution"""
        self.scan_count += 1

        if self.scan_count % 5 == 0:
            logger.info(f"🔍 [24/7] Scan #{self.scan_count}: {len(coins)} coins")

        for symbol in coins:
            if not self.running:
                break

            try:
                already_in_trade = symbol in self.trade_manager.open_trades

                # Fetch multi-timeframe data
                ohlc_data = self._fetch_multi_timeframe(symbol)
                if ohlc_data is None:
                    continue

                # Run analysis
                analysis = self.analysis_engine.multi_timeframe_analysis(ohlc_data)
                self.analysis_cache[symbol] = {
                    "analysis": analysis,
                    "timestamp": datetime.now()
                }

                # If already in trade, just update price
                if already_in_trade:
                    lower_tf = ohlc_data.get("lower")
                    if lower_tf is not None and len(lower_tf) > 0:
                        self.trade_manager.update_trade_price(symbol, lower_tf["close"].iloc[-1])
                    continue

                # Check if trade criteria met
                final = analysis.get("final_signal", {})
                decision = final.get("decision", "HOLD")

                if decision in ["BUY", "SELL"]:
                    tools_agreeing = final.get("tools_agreeing", 0)
                    min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
                    profit_chance = final.get("profit_chance", 0.0)
                    min_chance = self.config.get("MIN_PROFIT_CHANCE", 65.0)

                    # DIAGNOSTIC (per explicit request): log every BUY/SELL
                    # candidate's actual numbers, whether it passes or not -
                    # so a rejected coin shows exactly WHY (tools too few,
                    # score too low, or both) instead of just silently not
                    # trading with no visible reason.
                    passed = tools_agreeing >= min_tools and profit_chance >= min_chance
                    logger.info(f"📈 {symbol}: {decision} candidate | "
                                f"tools_agreeing={tools_agreeing}/{min_tools} | "
                                f"profit_chance={profit_chance:.1f}%/{min_chance:.1f}% | "
                                f"{'✅ PASSED' if passed else '❌ rejected'}")

                    if passed:
                        if self._is_within_trading_hours():
                            self._execute_trade(symbol, decision, final, ohlc_data, analysis)
                        else:
                            logger.info(f"⏱️ {symbol}: {decision} signal qualified but outside "
                                        f"ALLOWED_TRADING_HOURS_UTC - skipping entry.")
                    else:
                        # FIX (user request, Phase 1 pattern engine): the
                        # normal gate rejected this candidate - optionally
                        # (PATTERN_ENGINE_ENABLED) give it one more, fully
                        # independent check against classical chart
                        # patterns before giving up on this coin this scan.
                        self._try_pattern_engine_entry(symbol, decision, final, ohlc_data, analysis)
                else:
                    # decision == "HOLD": Tool 5 found no direction at all
                    # on this coin. Still worth a pattern-engine check
                    # (same opt-in gate) since a chart pattern is a fully
                    # independent signal from the Tool 5 vote.
                    self._try_pattern_engine_entry(symbol, decision, final, ohlc_data, analysis)

            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")
                continue

    def _is_within_trading_hours(self) -> bool:
        """
        Gate for NEW trade entries only, based on hourly_breakdown.json
        findings (see config.py comment above ALLOWED_TRADING_HOURS_UTC):
        only 12:00-16:59 UTC cleared breakeven with statistically solid
        sample sizes; every other hour was below it. Existing open trades
        are never affected by this - it only decides whether a fresh
        BUY/SELL signal is allowed to actually open a position right now.
        """
        if not self.config.get("TRADING_HOURS_FILTER_ENABLED", False):
            return True
        allowed_hours = self.config.get("ALLOWED_TRADING_HOURS_UTC", list(range(24)))
        return datetime.utcnow().hour in allowed_hours

    def _fetch_multi_timeframe(self, symbol: str) -> Optional[Dict[str, pd.DataFrame]]:
        """Fetch OHLCV data for all 3 timeframes"""
        result = {}
        try:
            for tf_name, tf_interval in TIMEFRAMES.items():
                limit = {"4h": 100, "1h": 150, "15m": 200}.get(tf_interval, 100)

                try:
                    klines = self.client.klines(symbol=symbol, interval=tf_interval, limit=limit)
                except Exception:
                    result[tf_name] = None
                    continue

                if klines:
                    df = pd.DataFrame(klines, columns=[
                        "timestamp", "open", "high", "low", "close", "volume",
                        "close_time", "quote_asset_volume", "trades",
                        "taker_buy_base", "taker_buy_quote", "ignore"
                    ])
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    result[tf_name] = df
                else:
                    result[tf_name] = None
        except Exception as e:
            logger.error(f"Data fetch error {symbol}: {e}")
            return None

        if "higher" not in result or result["higher"] is None:
            return None

        # ---- Extended Tool 1 context (SMT Divergence / Macro Structure /
        # Old Highs-Lows) -----------------------------------------------
        # This is ADDITIVE and fully isolated in its own try/except: if any
        # of it fails for any reason, the "higher"/"medium"/"lower" result
        # built above is returned exactly as before, and Tool 1's extended
        # sub-features simply don't trigger for this scan (identical to how
        # the missing calibration_table.json is handled elsewhere).
        if self.config.get("SMT_DIVERGENCE_ENABLED", True):
            try:
                corr_symbol = self._get_smt_correlated_symbol(symbol)
                correlated = {}
                for tf_name, tf_interval in TIMEFRAMES.items():
                    limit = {"4h": 100, "1h": 150, "15m": 200}.get(tf_interval, 100)
                    try:
                        c_klines = self.client.klines(symbol=corr_symbol, interval=tf_interval, limit=limit)
                    except Exception:
                        c_klines = None
                    if c_klines:
                        c_df = pd.DataFrame(c_klines, columns=[
                            "timestamp", "open", "high", "low", "close", "volume",
                            "close_time", "quote_asset_volume", "trades",
                            "taker_buy_base", "taker_buy_quote", "ignore"
                        ])
                        for col in ["open", "high", "low", "close", "volume"]:
                            c_df[col] = pd.to_numeric(c_df[col], errors="coerce")
                        correlated[tf_name] = c_df
                    else:
                        correlated[tf_name] = None
                result["correlated"] = correlated
            except Exception as e:
                logger.debug(f"SMT correlated-symbol fetch skipped for {symbol}: {e}")

        try:
            daily_limit = self.config.get("DAILY_HISTORY_CANDLES", 200)
            d_klines = self.client.klines(symbol=symbol, interval="1d", limit=daily_limit)
            if d_klines:
                daily_df = pd.DataFrame(d_klines, columns=[
                    "timestamp", "open", "high", "low", "close", "volume",
                    "close_time", "quote_asset_volume", "trades",
                    "taker_buy_base", "taker_buy_quote", "ignore"
                ])
                for col in ["open", "high", "low", "close", "volume"]:
                    daily_df[col] = pd.to_numeric(daily_df[col], errors="coerce")
                result["daily"] = daily_df
        except Exception as e:
            logger.debug(f"Daily-candle fetch skipped for {symbol} "
                         f"(macro structure / old highs-lows will no-op this scan): {e}")

        # ---- Funding Rate (ADDED, user request) -------------------------
        # Same additive, fully isolated pattern as "correlated"/"daily"
        # above: on any failure, this key is simply absent and the funding-
        # rate confluence check inside AnalysisEngine._calculate_profit_chance
        # no-ops (identical to how a missing calibration table or missing
        # correlated-symbol data already no-op elsewhere) - nothing else
        # about this scan is affected.
        if self.config.get("FUNDING_RATE_ENABLED", True):
            try:
                premium = self.client.funding_rate(symbol)
                funding_rate_pct = float(premium.get("lastFundingRate", 0)) * 100
                result["market_data"] = {"funding_rate_pct": funding_rate_pct}
            except Exception as e:
                logger.debug(f"Funding-rate fetch skipped for {symbol}: {e}")

        return result

    def _get_smt_correlated_symbol(self, symbol: str) -> str:
        """
        Pick the correlated pair used for SMT (Smart Money Technique)
        Divergence. ICT's classic reference pair is BTC vs ETH: use BTCUSDT
        as the correlated reference for every symbol, and fall back to
        ETHUSDT when the symbol being scanned IS BTCUSDT itself.
        """
        correlated_map = self.config.get("SMT_CORRELATED_MAP", {})
        if symbol in correlated_map:
            return correlated_map[symbol]
        if symbol == "BTCUSDT":
            return "ETHUSDT"
        return "BTCUSDT"

    def _get_analysis_based_tp_sl(self, symbol: str, side: str, entry_price: float,
                                   analysis: Dict) -> Tuple[Optional[float], Optional[float]]:
        """
        Derive TP/SL from the same analysis tools that triggered the trade,
        instead of a flat fixed percentage for every coin.

        For a BUY: TP targets the STRONGEST detected resistance ABOVE entry
        (a bearish order block, a bearish FVG's near edge, or an untapped
        buyside liquidity pool); SL targets the STRONGEST detected support
        BELOW entry (mirror image, bullish versions). For a SELL it's the
        reverse. This uses the same order block / FVG / liquidity data the
        5 tools already compute — no new indicators are introduced.

        CHANGE (per explicit request, round 2): candidate selection changed
        from "nearest to entry" to "strongest" — previously the single
        closest detected level was always used, which meant a minor/weak
        structure sitting very close to entry could produce an
        unreasonably tight SL that ordinary noise triggers, even though a
        more significant (and often farther) level existed. Now every
        candidate carries a strength score and the highest-strength one on
        the correct side of entry is used, regardless of which is nearest.
        Distance is still NOT clamped (previous change, unchanged) — the
        strongest level is used exactly as found, however close or far
        that turns out to be. Strength scoring:
          - Order Block: the OB candle's displacement (high - low) — a
            bigger impulse candle behind the level means more significant
            institutional footprint.
          - FVG: the gap's own size (already computed by _detect_fvg) —
            bigger imbalance = more significant.
          - Liquidity: a fixed weight reflecting ICT's own hierarchy of
            significance (external swing-range liquidity > internal/EQH-EQL
            liquidity), scaled by entry_price so it's comparable in the
            same price-distance units as OB/FVG strength above.
        Every candidate is still checked to be strictly on the correct side
        of entry_price (a "resistance" behind current price is useless).
        Returns (None, None) if no timeframe has a usable level, so the
        caller falls back to the existing fixed-percent
        (TAKE_PROFIT_PERCENT/STOP_LOSS_PERCENT) behavior exactly as before
        — that fallback is unchanged.

        ADDED (user request): Volume Profile POC / Value Area edges are now
        also added to the same candidate pool (same "strongest wins"
        selection, same "silently contributes nothing if absent" pattern
        as every other source here) - see the block right after the old
        highs/lows loop below.

        FIX (user request, 3 quality improvements to this function only -
        entry logic, tool vote, profit_chance, trading hours are ALL
        untouched, this function only runs after the trade side is already
        decided):
          1) ATR-scaled weighting: the "fixed weight" candidates (Liquidity,
             PDH/PDL, PWH/PWL, Old Highs/Lows, Volume Profile POC/VA) used
             to be a flat fraction of entry_price - comparing a coin's own
             ATR-independent constant against OB/FVG's REAL price-based
             size was an apples-to-oranges comparison, and never adapted
             to how much that specific coin actually moves. Now every
             fixed-weight candidate is scaled by this timeframe's own ATR
             (already computed by _detect_fvg) instead, preserving the
             exact same relative hierarchy between candidate types
             (PWH/PWL > POC > external liquidity > PDH/PDL = Value Area >
             Old H/L > internal liquidity) but now genuinely volatility-
             adaptive. If ATR isn't available for some reason, falls back
             to a basis that reproduces the OLD fixed-price-fraction
             weights exactly - nothing regresses in that edge case.
          2) Reward:risk-aware TP selection: SL selection is UNCHANGED
             (still the pool's single strongest candidate - the protection
             level is untouched). TP selection now prefers the strongest
             candidate that ALSO gives >= 1:1 reward:risk against the
             chosen SL, only falling back to the pool's absolute strongest
             (the OLD behavior) when no candidate clears that bar. This can
             only ever upgrade the TP choice - it never rejects/blocks the
             trade, since the old fallback path is always still there.
          3) (Same fix as #1 above - ATR-scaling is what makes this
             volatility-adaptive; no separate code path needed.)

        ADDED (user request, Confluence Stacking): after all candidates
        are gathered below, nearby levels are merged into combined
        clusters (see _cluster_candidates) before selection - so a zone
        with several independent levels overlapping can win over one
        single strong level elsewhere, the same "multiple confirmations
        in one zone" concept ICT's Unicorn Model already uses for entries.
        """
        resistance_candidates = []  # list of (level, strength)
        support_candidates = []
        atr_basis = entry_price * 0.005  # fallback if the loop below never sets it (analysis missing both timeframes)

        for tf_name in ["higher", "medium"]:
            tf = analysis.get(tf_name)
            if not tf:
                continue

            # FIX #1/#3: ATR-based weighting basis for every fixed-weight
            # candidate type below. atr_basis mirrors entry_price*0.005
            # (the OLD internal-liquidity weight) when ATR is unavailable,
            # so the multipliers below reproduce the exact OLD numbers in
            # that fallback case - only the normal (ATR available) case
            # actually changes behavior.
            atr = tf.get("fvg", {}).get("atr")
            atr_basis = atr if atr else entry_price * 0.005

            ob = tf.get("order_block", {})
            bear_ob = ob.get("bearish_ob")
            if bear_ob and bear_ob.get("level"):
                ob_strength = bear_ob.get("high", bear_ob["level"]) - bear_ob.get("low", bear_ob["level"])
                resistance_candidates.append((bear_ob["level"], ob_strength))
            bull_ob = ob.get("bullish_ob")
            if bull_ob and bull_ob.get("level"):
                ob_strength = bull_ob.get("high", bull_ob["level"]) - bull_ob.get("low", bull_ob["level"])
                support_candidates.append((bull_ob["level"], ob_strength))

            for fvg in tf.get("fvg", {}).get("fvg_levels", []):
                fvg_strength = fvg.get("size", 0)
                if fvg.get("type") == "bearish" and fvg.get("low"):
                    resistance_candidates.append((fvg["low"], fvg_strength))
                elif fvg.get("type") == "bullish" and fvg.get("high"):
                    support_candidates.append((fvg["high"], fvg_strength))

            liq = tf.get("liquidity", {})
            liq_strength = atr_basis * (2.0 if liq.get("external_swept") else 1.0)
            if liq.get("buyside_liquidity"):
                resistance_candidates.append((liq["buyside_liquidity"], liq_strength))
            if liq.get("sellside_liquidity"):
                support_candidates.append((liq["sellside_liquidity"], liq_strength))

            # Extended Tool 1 levels (Macro Structure + Old Highs/Lows).
            # Same relative hierarchy as before (weekly > daily > old
            # swing levels), now ATR-scaled instead of price-fraction.
            ict = tf.get("ict_smc", {})
            if ict.get("pdh") is not None:
                resistance_candidates.append((ict["pdh"], atr_basis * 1.6))
            if ict.get("pdl") is not None:
                support_candidates.append((ict["pdl"], atr_basis * 1.6))
            if ict.get("pwh") is not None:
                resistance_candidates.append((ict["pwh"], atr_basis * 3.0))
            if ict.get("pwl") is not None:
                support_candidates.append((ict["pwl"], atr_basis * 3.0))
            for lvl in ict.get("old_highs", []):
                resistance_candidates.append((lvl, atr_basis * 1.2))
            for lvl in ict.get("old_lows", []):
                support_candidates.append((lvl, atr_basis * 1.2))

            # ADDED (user request): Volume Profile (POC / Value Area) as
            # additional candidate levels, using the exact same "add to
            # the pool, let strength decide" pattern as every other source
            # above - not a new gate, not a new required check. If Volume
            # Profile didn't compute (insufficient candles) these keys are
            # just None and silently contribute nothing, exactly like a
            # missing OB/FVG/liquidity level already does above. Now also
            # ATR-scaled (fix #1/#3), same as the other fixed-weight
            # candidates above.
            vp = tf.get("volume_profile", {})
            poc = vp.get("poc_price")
            if poc is not None:
                weight = atr_basis * 2.4
                if poc > entry_price:
                    resistance_candidates.append((poc, weight))
                elif poc < entry_price:
                    support_candidates.append((poc, weight))
            va_high = vp.get("value_area_high")
            if va_high is not None and va_high > entry_price:
                resistance_candidates.append((va_high, atr_basis * 1.6))
            va_low = vp.get("value_area_low")
            if va_low is not None and va_low < entry_price:
                support_candidates.append((va_low, atr_basis * 1.6))

        # ADDED (user request): Confluence Stacking. A professional trader
        # weighs a zone where SEVERAL independent levels cluster together
        # (e.g. an Order Block overlapping a Fair Value Gap overlapping a
        # Volume Profile POC) as a materially stronger zone than any one of
        # those levels alone - this is exactly the "Unicorn Model" concept
        # Tool 1 already uses for entries, now applied to level SELECTION
        # too. Groups candidates within one ATR of each other and treats a
        # cluster's SUMMED strength as a single combined candidate; this
        # can only ever make an already-detected level win more often when
        # it has real confluence behind it - it never invents a new level,
        # never removes any candidate from the pool, and the exact same
        # fallback logic (pick the strongest, or pick a >=1:1 R:R one) runs
        # unchanged right after this - so if clustering finds nothing (a
        # sparse pool with no nearby levels), every candidate simply stays
        # its own single-member "cluster" and behavior is identical to
        # before.
        resistance_candidates = self._cluster_candidates(resistance_candidates, atr_basis)
        support_candidates = self._cluster_candidates(support_candidates, atr_basis)

        if side == "BUY":
            tp_pool = [(lvl, s) for lvl, s in resistance_candidates if lvl > entry_price]
            sl_pool = [(lvl, s) for lvl, s in support_candidates if lvl < entry_price]
            sl_level = max(sl_pool, key=lambda x: x[1])[0] if sl_pool else None   # strongest support below (unchanged)
            tp_level = self._pick_tp_with_rr(tp_pool, entry_price, sl_level) if tp_pool else None
        else:
            tp_pool = [(lvl, s) for lvl, s in support_candidates if lvl < entry_price]
            sl_pool = [(lvl, s) for lvl, s in resistance_candidates if lvl > entry_price]
            sl_level = max(sl_pool, key=lambda x: x[1])[0] if sl_pool else None   # strongest resistance above (unchanged)
            tp_level = self._pick_tp_with_rr(tp_pool, entry_price, sl_level) if tp_pool else None

        return tp_level, sl_level

    def _pick_tp_with_rr(self, tp_pool: List[Tuple[float, float]], entry_price: float,
                          sl_level: Optional[float]) -> float:
        """
        FIX (user request, quality improvement #2 of 3): prefer the
        strongest TP candidate that ALSO gives >= 1:1 reward:risk against
        the already-chosen SL. Falls back to the pool's absolute strongest
        candidate (the OLD, unchanged behavior) whenever no candidate
        clears that bar, or when there's no SL to measure against - so
        this can only ever upgrade the TP choice, it never blocks/rejects
        the trade (tp_pool is only ever called with a non-empty pool, by
        the "if tp_pool else None" checks at each call site).
        """
        strongest = max(tp_pool, key=lambda x: x[1])[0]
        if sl_level is None:
            return strongest

        risk = abs(entry_price - sl_level)
        if risk <= 0:
            return strongest

        qualifying = [(lvl, s) for lvl, s in tp_pool if abs(lvl - entry_price) >= risk]
        if qualifying:
            return max(qualifying, key=lambda x: x[1])[0]

        return strongest

    def _cluster_candidates(self, candidates: List[Tuple[float, float]],
                             cluster_distance: float) -> List[Tuple[float, float]]:
        """
        ADDED (user request, Confluence Stacking): groups candidate levels
        that fall within `cluster_distance` of each other into a single
        combined candidate - its strength is the SUM of every member's
        strength, its level is their strength-weighted average. A cluster
        of 3 independent, moderately-strong levels all pointing at nearly
        the same price can then legitimately outweigh one single very
        strong level elsewhere, exactly how real confluence works.

        Pure pool transformation - every input candidate is still
        represented in the output (just possibly merged), so this can
        never make the pool empty or remove information; it can only
        change which combined level ends up strongest.
        """
        if not candidates:
            return []

        sorted_candidates = sorted(candidates, key=lambda x: x[0])
        clusters = [[sorted_candidates[0]]]
        for lvl, strength in sorted_candidates[1:]:
            if lvl - clusters[-1][-1][0] <= cluster_distance:
                clusters[-1].append((lvl, strength))
            else:
                clusters.append([(lvl, strength)])

        merged = []
        for cluster in clusters:
            total_strength = sum(s for _, s in cluster)
            if total_strength > 0:
                weighted_level = sum(lvl * s for lvl, s in cluster) / total_strength
            else:
                weighted_level = cluster[0][0]
            merged.append((weighted_level, total_strength))

        return merged

    def _on_trade_closed(self, trade: Dict):
        """
        FIX (user request, pattern-engine re-entry loop): fired after ANY
        trade closes. If it was a pattern-engine trade (entry_path ==
        "pattern_match"), starts a cooldown for that symbol so a losing (or
        winning) pattern trade can't immediately re-trigger the same
        pattern again on the very next scan - this was the main mechanism
        behind repeated back-to-back losses on the same coin/pattern.
        Completely inert for normal Tool-5 trades (entry_path anything
        else) - never touches their behavior.
        """
        entry_path = (trade.get("analysis") or {}).get("entry_path")
        if entry_path != "pattern_match":
            return
        symbol = trade.get("symbol")
        if not symbol:
            return
        cooldown_minutes = self.config.get("PATTERN_COOLDOWN_MINUTES", 240)
        self.pattern_cooldown_until[symbol] = datetime.now() + timedelta(minutes=cooldown_minutes)
        logger.info(f"🔷 {symbol}: pattern-trade closed ({trade.get('close_reason')}) - "
                    f"pattern-engine entries on this symbol paused for {cooldown_minutes} min.")

    def _try_pattern_engine_entry(self, symbol: str, decision: str, final: Dict,
                                   ohlc_data: Dict, analysis: Dict):
        """
        FIX (user request, Phase 1 pattern engine): called ONLY for a
        candidate the normal Tool 5 / MIN_TOOLS_MATCH / MIN_PROFIT_CHANCE
        gate has ALREADY REJECTED (see call sites in _scan_coins_247).
        Checks the coin's 15m candles against the 6 classical chart
        patterns in pattern_engine.py; if the best match clears
        PATTERN_MIN_CONFIDENCE, opens the trade through the EXACT SAME
        _execute_trade path as every other trade (same margin sizing,
        leverage, order placement, trade_manager hookup, monitoring, TP1-2-
        3 reanalysis, Telegram notifications) - just with that pattern's
        own measured-move TP/SL instead of the normal Tool-5-based levels.

        No-ops immediately (zero overhead) if PATTERN_ENGINE_ENABLED is
        False (the default) or the module isn't importable.
        """
        if not _PATTERN_ENGINE_AVAILABLE or not self.config.get("PATTERN_ENGINE_ENABLED", False):
            return

        # FIX (user request, re-entry loop): don't open a new pattern trade
        # on a symbol that just had one close - prevents the same pattern
        # re-triggering (and re-losing) again right after a loss.
        cooldown_until = self.pattern_cooldown_until.get(symbol)
        if cooldown_until and datetime.now() < cooldown_until:
            return

        lower_tf = ohlc_data.get("lower")
        if lower_tf is None or len(lower_tf) < 30:
            return

        min_confidence = self.config.get("PATTERN_MIN_CONFIDENCE", 80.0)
        try:
            match = pattern_engine.detect_best_pattern(lower_tf, min_confidence=min_confidence)
        except Exception as e:
            logger.debug(f"pattern_engine: detection failed for {symbol}: {e}")
            return

        if not match:
            return

        pattern_decision = match["direction"]  # "BUY" or "SELL", independent of the Tool 5 decision above
        current_price = float(lower_tf["close"].iloc[-1])
        target = match["target"]
        invalidation = match["invalidation"]

        # FIX (user request, real risk management practice): a pattern is
        # only tradeable if the CURRENT price still sits between its own
        # invalidation and target - i.e. the move hasn't already happened
        # (overextended past target) and the setup hasn't already failed
        # (past invalidation). Also enforces a minimum reward:risk ratio
        # (>= 0.8:1) - a pattern whose own measured-move target is closer
        # than its own invalidation point is a poor bet even if the
        # direction is right. Both were previously unchecked here, which
        # let trade_manager's own entry-price sanity check silently swap
        # in a GENERIC fixed-percent TP/SL that had nothing to do with the
        # pattern at all whenever this happened - now we just skip the
        # trade instead of quietly trading a broken/stale setup.
        if pattern_decision == "BUY":
            valid_position = invalidation < current_price < target
            reward = target - current_price
            risk = current_price - invalidation
        else:
            valid_position = target < current_price < invalidation
            reward = current_price - target
            risk = invalidation - current_price

        if not valid_position:
            logger.info(f"🔷 {symbol}: {match['pattern']} matched but price has already moved past "
                        f"target/invalidation (stale setup) - skipping.")
            return

        if risk <= 0 or reward / risk < 0.8:
            logger.info(f"🔷 {symbol}: {match['pattern']} matched but reward:risk "
                        f"({reward/risk if risk > 0 else 0:.2f}:1) is below the 0.8:1 minimum - skipping.")
            return

        logger.info(f"🔷 {symbol}: PATTERN MATCH — {match['pattern']} ({match['confidence']}% confidence) "
                    f"→ {pattern_decision} | target={match['target']:.8f} invalidation={match['invalidation']:.8f} "
                    f"| reward:risk={reward/risk:.2f}:1")

        # Build a signal dict compatible with what _execute_trade expects,
        # tagged so downstream code (Telegram breakdown, entry_path) knows
        # this came from the pattern engine, not the normal Tool 5 vote.
        pattern_signal = {
            "direction": 1 if pattern_decision == "BUY" else -1,
            "tools_agreeing": 0,
            "profit_chance": match["confidence"],
            "entry_path": "pattern_match",
        }
        pattern_override = {
            "tp": match["target"],
            "sl": match["invalidation"],
            "pattern": match["pattern"],
            "confidence": match["confidence"],
        }

        if not self._is_within_trading_hours():
            logger.info(f"⏱️ {symbol}: pattern match qualified but outside "
                        f"ALLOWED_TRADING_HOURS_UTC - skipping entry.")
            return

        self._execute_trade(symbol, pattern_decision, pattern_signal, ohlc_data,
                             analysis=None, pattern_override=pattern_override)

    def _execute_trade(self, symbol: str, decision: str, signal: Dict, ohlc_data: Dict,
                        analysis: Optional[Dict] = None, pattern_override: Optional[Dict] = None):
        """
        Execute trade on Binance Futures
        Auto coin min notional, max leverage, and balance check

        pattern_override (user request): when provided (a dict with
        "tp"/"sl"/"pattern"/"confidence" from pattern_engine.detect_best_
        pattern), this trade's TP/SL come from that classical chart
        pattern's own measured-move levels instead of the normal Tool 5 OB/
        FVG/Liquidity level picker - used ONLY for the pattern-engine
        fallback path (see _try_pattern_engine_entry), which itself is only
        ever reached for a candidate the normal gate already rejected.
        Every other part of trade execution below (margin sizing, leverage,
        order placement, trade_manager hookup) is IDENTICAL either way -
        only where TP/SL come from differs.
        """
        direction = signal.get("direction", 0)
        if direction == 0:
            return

        # FIX (CRITICAL, user-reported): MAX_OPEN_TRADES used to only be
        # checked inside trade_manager.open_new_trade() - which is called
        # AFTER this function already sets leverage on Binance and places a
        # REAL market order (self.client.new_order below). That meant every
        # attempt beyond the cap still executed a real, live position on
        # the exchange, then got silently abandoned (open_new_trade()
        # returning None before ever placing its protective SL/TP order) -
        # a real, untracked, UNPROTECTED position with no local record and
        # no stop-loss. This is almost certainly the source of the
        # "TP/SL: --/--" orphaned positions seen on the account, and the
        # repeated pattern-engine re-attempts on the same symbol (each one
        # placing ANOTHER real order). Checking the cap here, before any
        # exchange action, makes this function a true no-op once the cap is
        # reached - exactly matching what MAX_OPEN_TRADES is supposed to do.
        max_open = self.config.get("MAX_OPEN_TRADES", 15)
        current_open = len(self.trade_manager.open_trades)
        if current_open >= max_open:
            logger.warning(f"⚠️ Max trades ({current_open}/{max_open}). Skipping {symbol} "
                            f"BEFORE any exchange action (no leverage change, no order placed).")
            return

        # ADDED (user request): daily realized-loss limit - checked here
        # for the exact same reason and at the exact same point as
        # MAX_OPEN_TRADES right above (before ANY exchange action, so a
        # blocked attempt is a true no-op - no leverage change, no order
        # placed). Covers BOTH entry paths automatically since both the
        # tool-vote path and _try_pattern_engine_entry's fallback path
        # both call this same _execute_trade() function. Never affects
        # managing/closing an ALREADY-open trade - only blocks new entries.
        if self.trade_manager.is_daily_loss_limit_reached():
            logger.warning(f"🛑 Daily loss limit reached (${self.trade_manager.daily_loss_usdt:.2f} "
                            f"lost today). Skipping {symbol} - no new trades until tomorrow.")
            return

        # Get coin-specific exchange info
        coin_min_notional = 10.0
        coin_max_leverage = 20

        try:
            info = self._get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s.get("filters", []):
                        if f["filterType"] == "MIN_NOTIONAL":
                            coin_min_notional = float(f.get("minNotional", f.get("notional", 10.0)))
                    break
        except Exception as e:
            logger.debug(f"Exchange info fetch error for {symbol}: {e}")

        # FIX (real bug found via user's ESPUSDT report, see
        # BinanceFuturesClient.leverage_bracket docstring for the root
        # cause): this now calls the correct, dedicated endpoint instead
        # of reading a field that never existed in exchangeInfo.
        try:
            brackets = self.client.leverage_bracket(symbol)
            if brackets:
                coin_max_leverage = int(brackets[0].get("initialLeverage", 20))
        except Exception as e:
            logger.debug(f"Leverage bracket fetch error for {symbol}: {e}")

        # Calculate margin (exactly 5% of balance)
        # FIX (root cause of -2019 "Margin is insufficient" on every trade):
        # use available_balance (free margin right now, after subtracting
        # what's already locked in open positions), NOT self.balance (total
        # wallet equity, which stays the same regardless of how much margin
        # other open trades are using). Sizing off total equity meant every
        # new order could try to use margin that was already spoken for.
        balance_pct = self.config.get("BALANCE_PERCENTAGE", 5) / 100.0
        margin = self.available_balance * balance_pct

        if margin < 0.001:
            logger.warning(f"⚠️ Margin too small: ${margin:.4f} (available balance: "
                            f"${self.available_balance:.2f}). Cannot trade {symbol}")
            return

        # Calculate required leverage to meet minimum notional
        config_max_lev = self.config.get("MAX_LEVERAGE", 5)
        effective_max_lev = min(coin_max_leverage, config_max_lev)
        max_position = margin * effective_max_lev

        logger.info(f"🔍 {symbol}: margin=${margin:.4f}, "
                    f"minNotional=${coin_min_notional:.2f}, "
                    f"maxLev={coin_max_leverage}x, "
                    f"configLev={config_max_lev}x, "
                    f"effectiveLev={effective_max_lev}x, "
                    f"maxPos=${max_position:.2f}")

        if max_position < coin_min_notional:
            logger.info(f"⏭️ {symbol}: Even {effective_max_lev}x gives ${max_position:.2f} < "
                        f"${coin_min_notional:.2f} min. Skipping.")
            return

        # Calculate leverage to use
        # FIX (real bug: every trade silently traded at 1x): this used to
        # compute optimal_leverage as the BARE MINIMUM leverage needed to
        # reach coin_min_notional (coin_min_notional/margin, rounded up),
        # then take min(optimal_leverage, effective_max_lev). Since margin
        # is virtually always >> coin_min_notional for any funded account,
        # that bare minimum came out to 1x almost every time - so
        # trade_leverage was ALWAYS ~1x regardless of MAX_LEVERAGE (5x/10x/
        # 25x, whatever was configured), silently wasting the leverage
        # setting on every trade. We already verified above (max_position =
        # margin * effective_max_lev >= coin_min_notional) that trading at
        # effective_max_lev is safe/sufficient, so use it directly - no
        # need to first compute some smaller "just barely enough" value.
        trade_leverage = effective_max_lev

        # Volatility safety
        volatility = self._calculate_volatility(ohlc_data)
        vol_leverage = self.trade_manager.calculate_dynamic_leverage(symbol, volatility)
        final_leverage = min(trade_leverage, vol_leverage)
        final_leverage = max(1, final_leverage)

        final_position = margin * final_leverage

        logger.info(f"✅ {symbol}: TRADE POSSIBLE! "
                    f"Margin=${margin:.2f}, Leverage={final_leverage}x, "
                    f"Position=${final_position:.2f}")

        # Get current price
        lower_tf = ohlc_data.get("lower")
        if lower_tf is None:
            lower_tf = ohlc_data.get("medium")
        if lower_tf is None:
            lower_tf = ohlc_data.get("higher")
        if lower_tf is None or len(lower_tf) == 0:
            return
        current_price = float(lower_tf["close"].iloc[-1])

        # ADDED (user request, Telegram-toggleable Isolated/Cross): sets
        # this symbol's margin mode to whatever USE_ISOLATED_MARGIN is
        # currently toggled to in Telegram, right before every trade open.
        # Binance returns -4046 ("No need to change margin type") whenever
        # the symbol is already in the requested mode - completely normal
        # on every call after the first for that symbol, not a real error.
        # Any other failure (e.g. Binance refuses to switch while an old
        # order/position lingers) is logged and swallowed - falls back to
        # whatever margin mode the symbol is already in rather than
        # blocking the trade, exactly like the leverage-change try/except
        # right below already does.
        desired_margin_type = "ISOLATED" if self.config.get("USE_ISOLATED_MARGIN", False) else "CROSSED"
        try:
            self.client.change_margin_type(symbol=symbol, margin_type=desired_margin_type)
            logger.info(f"⚙️ Margin type set: {symbol} = {desired_margin_type}")
        except Exception as e:
            if "-4046" not in str(e):
                logger.warning(f"Margin type change warning for {symbol} "
                                f"(-> {desired_margin_type}): {e}")

        # Set leverage on Binance
        # FIX (real bug found via user's ESPUSDT report): even with the
        # root-cause leverage_bracket fix above, this is kept as a second,
        # independent safety net - change_leverage()'s own response always
        # echoes back the leverage Binance ACTUALLY applied (it
        # self-corrects/clamps to whatever the account's real bracket
        # allows). Using that CONFIRMED value - instead of blindly
        # trusting the pre-computed "intended" final_leverage - for every
        # downstream position-size calculation means the order can never
        # end up sized for a leverage that isn't really active, no matter
        # the reason (stale bracket data, a bracket that changed after our
        # lookup, a hedge-mode quirk, anything). If the call fails
        # outright (no confirmed value available at all), fall back to 1x
        # (the safest possible assumption) rather than trusting an
        # unverified number - this is the same failure mode that produced
        # ESPUSDT's oversized margin, so silently keeping the old
        # (unverified) final_leverage here would not actually fix it.
        try:
            lev_resp = self.client.change_leverage(symbol=symbol, leverage=final_leverage)
            confirmed_leverage = int(lev_resp.get("leverage", final_leverage))
            if confirmed_leverage != final_leverage:
                logger.warning(f"⚠️ {symbol}: requested {final_leverage}x but Binance confirmed "
                                f"{confirmed_leverage}x - using the confirmed value for sizing.")
            final_leverage = confirmed_leverage
            logger.info(f"⚙️ Leverage set: {symbol} = {final_leverage}x")
        except Exception as e:
            logger.warning(f"Leverage change warning for {symbol}: {e} - falling back to 1x "
                            f"for safety (actual active leverage could not be confirmed).")
            final_leverage = 1

        # Recompute position value using the CONFIRMED leverage, not the
        # pre-verification estimate used for the earlier "TRADE POSSIBLE"
        # log line - this is what actually drives order size below.
        final_position = margin * final_leverage

        # Calculate position size
        # FIX (analysis-based TP/SL): derive the stop/target from the same
        # order block / FVG / liquidity levels the tools detected, instead
        # of always using a flat fixed percentage. Falls back to the
        # original fixed-percent SL/TP untouched if no timeframe has a
        # usable level for this trade.
        sl_percent = self.config.get("STOP_LOSS_PERCENT", 1.0) / 100.0
        dynamic_tp, dynamic_sl = (None, None)
        if pattern_override:
            dynamic_tp = pattern_override.get("tp")
            dynamic_sl = pattern_override.get("sl")
        elif analysis:
            dynamic_tp, dynamic_sl = self._get_analysis_based_tp_sl(
                symbol, decision, current_price, analysis
            )

        if dynamic_sl is not None:
            sl_price = dynamic_sl
        elif decision == "BUY":
            sl_price = current_price * (1 - sl_percent)
        else:
            sl_price = current_price * (1 + sl_percent)

        # FIX (Double-leverage sizing bug): pass the real account balance
        # (RISK_PER_TRADE is meant as a % of account equity), not
        # margin*leverage — that was an already-leveraged notional value
        # being treated as "balance", which combined with the old double
        # leverage multiplication inside calculate_position_size inflated
        # real risk on a stop-loss hit far beyond the intended
        # RISK_PER_TRADE percentage.
        # FIX (margin-insufficient regression): pass final_position (the
        # already-validated margin*leverage notional — "TRADE POSSIBLE!
        # ... Position=$X" above) as the primary size driver, since that's
        # the number already checked against exchange min/max notional and
        # available margin. account_balance is passed separately, used only
        # as a downward risk-cap inside calculate_position_size — it must
        # never be the sole basis for quantity (that disconnect from
        # final_position/margin is what caused "Margin is insufficient"
        # errors after the previous fix).
        quantity = self.trade_manager.calculate_position_size(
            final_position,
            current_price,
            sl_price,
            final_leverage,
            account_balance=self.balance
        )
        quantity = self._round_quantity(symbol, quantity)

        if quantity <= 0:
            logger.warning(f"⚠️ Invalid quantity for {symbol}: {quantity}")
            return

        # FIX (real bug, user-reported: Binance API Error -4164 "Order's
        # notional must be no smaller than 5"): the EARLIER "TRADE
        # POSSIBLE!" check above only validates the margin*leverage-based
        # final_position against coin_min_notional - BEFORE
        # calculate_position_size()'s risk-based downward cap (scaling
        # quantity down, never up, whenever the naive margin*leverage
        # sizing would risk more than RISK_PER_TRADE% of account_balance
        # on a stop-loss hit) runs. That cap can shrink quantity well
        # below what was validated - most aggressively on trades with a
        # wide SL distance (a small account_balance makes this worse
        # too) - with nothing afterward re-checking the POST-cap,
        # POST-rounding quantity against the exchange's real minimum
        # notional. The order actually sent could therefore fall under
        # Binance's floor and get rejected outright, even though the
        # pre-check above logged "TRADE POSSIBLE" and a solid-looking
        # position size just moments earlier.
        #
        # Re-validate here, after both the risk-cap AND rounding have
        # been applied, using the exact same coin_min_notional already
        # computed above - if the final notional is still short, skip
        # this trade cleanly with a clear log line instead of sending an
        # order Binance will only reject. Deliberately does NOT bump
        # quantity back up to clear the minimum - the risk-cap's whole
        # purpose is keeping real $ risk within RISK_PER_TRADE, and
        # forcibly enlarging the order to meet an exchange minimum would
        # undermine exactly the protection it exists to provide.
        final_notional = quantity * current_price
        if final_notional < coin_min_notional:
            logger.info(
                f"⏭️ {symbol}: skipping - after the risk-based position-size "
                f"cap, order notional (${final_notional:.2f}) fell below the "
                f"exchange minimum (${coin_min_notional:.2f}). This trade's "
                f"stop-loss distance made the RISK_PER_TRADE-safe quantity "
                f"too small for this symbol; sending it would only be "
                f"rejected by Binance."
            )
            return

        # Rate limit
        now = time.time()
        if symbol in self.last_trade_time and now - self.last_trade_time[symbol] < 5:
            return
        self.last_trade_time[symbol] = now

        # Place order
        try:
            side = "BUY" if decision == "BUY" else "SELL"
            order = self.client.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity
            )

            logger.info(f"✅ TRADE: {side} {symbol} @ {current_price:.8f} | "
                        f"Qty={quantity} | Lev={final_leverage}x | "
                        f"Pos=${final_position:.2f} | Margin=${margin:.2f}")

            # FIX (user request, real fill price): a MARKET order can fill
            # at a different price than current_price (the price seen
            # during the scan, moments before this order was actually
            # placed) - slippage, especially on lower-liquidity coins, can
            # make that gap meaningful. Binance's own order response for a
            # filled MARKET order includes avgPrice (the real average fill
            # price) and executedQty (the real filled quantity) - using
            # those instead of the pre-order estimates keeps the LOCAL
            # trade record (and everything derived from it: TP/SL
            # distances, PnL%, trailing stop math) matched to what
            # actually happened on the exchange. Falls back to the
            # pre-order estimates if the response is missing/zero for any
            # reason (e.g. an unexpected response shape) - never blocks
            # the trade from being recorded over this.
            actual_entry_price = current_price
            actual_quantity = quantity
            try:
                filled_price = float(order.get("avgPrice", 0) or 0)
                if filled_price > 0:
                    actual_entry_price = filled_price
                filled_qty = float(order.get("executedQty", 0) or 0)
                if filled_qty > 0:
                    actual_quantity = filled_qty
            except (TypeError, ValueError) as e:
                logger.warning(f"{symbol}: could not parse actual fill price/qty from order "
                                f"response ({e}) - using pre-order estimates instead.")

            # FIX (user request, full fix - not just a warning): SL/TP
            # were originally SELECTED using current_price (the pre-order
            # estimate) because calculate_position_size() above genuinely
            # needs a concrete SL price to size the quantity BEFORE this
            # order can be placed - that ordering constraint is real and
            # can't change without restructuring how quantity itself gets
            # decided (a much bigger, riskier change the user did not ask
            # for). BUT the quantity is now already fixed regardless (the
            # order is filled) - so the TP/SL levels actually recorded
            # for and used to protect this trade CAN be safely refreshed
            # here to reflect the true fill price, at zero cost to
            # anything already decided. Re-runs the exact same candidate
            # selection (ATR-scaling, Confluence Stacking, R:R-aware TP -
            # unchanged) with actual_entry_price as the entry anchor
            # instead of the pre-fill current_price. In the overwhelming
            # majority of trades (slippage far too small to change which
            # candidates even qualify) this reproduces the exact same
            # levels already chosen - only in a genuine slippage edge
            # case does anything actually differ, and only for the
            # better (levels correctly anchored to the real entry).
            # Falls back to the original pre-fill levels if analysis
            # isn't available or the recompute finds nothing (identical
            # to how the very first computation already falls back) -
            # this can only ever refine the chosen levels, never fail the
            # trade or block it from opening.
            if pattern_override:
                pass  # pattern-derived TP/SL is a fixed measured-move target, not re-derived from live analysis - nothing to refresh
            elif analysis:
                refreshed_tp, refreshed_sl = self._get_analysis_based_tp_sl(
                    symbol, decision, actual_entry_price, analysis
                )
                if refreshed_tp is not None:
                    dynamic_tp = refreshed_tp
                if refreshed_sl is not None:
                    dynamic_sl = refreshed_sl
                    sl_price = refreshed_sl
                elif dynamic_sl is None:
                    # No analysis-based SL before OR after refresh - keep
                    # the same fixed-percent fallback already computed
                    # above, just anchored to the real entry price now.
                    sl_price = (actual_entry_price * (1 - sl_percent) if decision == "BUY"
                                else actual_entry_price * (1 + sl_percent))

            # Safety net: if the (possibly just-refreshed) SL still ends
            # up on the wrong side of the real fill price - only possible
            # now from genuinely extreme slippage, not from stale-price
            # selection anymore - surface that loudly rather than let a
            # trade silently open with an already-breached stop.
            if side == "BUY" and sl_price is not None and actual_entry_price <= sl_price:
                logger.warning(f"⚠️ {symbol}: actual fill price ({actual_entry_price:.8f}) is at/past "
                                f"the selected stop_loss ({sl_price:.8f}) - this trade's SL may "
                                f"trigger almost immediately.")
            elif side == "SELL" and sl_price is not None and actual_entry_price >= sl_price:
                logger.warning(f"⚠️ {symbol}: actual fill price ({actual_entry_price:.8f}) is at/past "
                                f"the selected stop_loss ({sl_price:.8f}) - this trade's SL may "
                                f"trigger almost immediately.")

            # FIX (trailing stop redesign): capture ATR(14) at entry so the
            # trailing stop distance can adapt to THIS coin's actual
            # volatility instead of using one fixed % for all scanned coins
            # (see trade_manager._evaluate_trade for how this is used).
            entry_atr = None
            if analysis:
                entry_atr = (analysis.get("lower", {}).get("fvg", {}).get("atr")
                             or analysis.get("medium", {}).get("fvg", {}).get("atr"))

            # FIX (user request, real fill price): capture WHICH of the 5 tools agreed and
            # WHICH of their own sub-concepts fired, per timeframe, purely
            # for display in the "Trade Opened" Telegram message. Read-only
            # summary of the exact same already-computed fields the real
            # vote uses (see AnalysisEngine.describe_agreement) - changes
            # no decision/entry logic at all.
            tool_breakdown = {}
            if analysis:
                trade_direction = signal.get("direction", 1 if decision == "BUY" else -1)
                for tf_name in ("higher", "medium", "lower"):
                    tf_result = analysis.get(tf_name)
                    if tf_result:
                        tool_breakdown[tf_name] = self.analysis_engine.describe_agreement(
                            tf_result, trade_direction
                        )

            trade = self.trade_manager.open_new_trade(
                symbol=symbol,
                side=side,
                entry_price=actual_entry_price,
                quantity=actual_quantity,
                leverage=final_leverage,
                analysis_result={
                    "signal": signal,
                    "volatility": volatility,
                    "tools_agreeing": signal.get("tools_agreeing", 0),
                    "profit_chance": signal.get("profit_chance", 0),
                    "margin_used": margin,
                    "position_value": final_position,
                    "min_notional": coin_min_notional,
                    "entry_atr": entry_atr,
                    "tool_breakdown": tool_breakdown,
                    "entry_path": "pattern_match" if pattern_override else signal.get("entry_path", "unknown"),
                    "pattern_name": pattern_override.get("pattern") if pattern_override else None,
                    "pattern_confidence": pattern_override.get("confidence") if pattern_override else None,
                },
                dynamic_tp=dynamic_tp,
                dynamic_sl=dynamic_sl
            )
            if trade:
                trade["binance_order_id"] = order.get("orderId")
                # FIX (Missing exchange-side protection): previously SL/TP
                # only existed as numbers inside the bot's own memory,
                # checked by a 5s polling loop. If the bot/VPS went down,
                # the position had NO protection at all. Now real
                # STOP_MARKET / TAKE_PROFIT_MARKET orders are placed on
                # Binance itself, so protection survives even if this
                # process is not running.
                self.trade_manager.place_protective_orders(symbol, trade)

        except Exception as e:
            logger.error(f"❌ Order error for {symbol}: {e}")

    def _get_exchange_info(self) -> dict:
        """
        FIX (Performance Bug): cached exchangeInfo lookup.
        Refreshes at most once per self._exchange_info_ttl seconds instead of
        making a fresh API call every time a trade is evaluated/executed.
        """
        now = time.time()
        if (self._exchange_info_cache is None or
                (now - self._exchange_info_cache_time) > self._exchange_info_ttl):
            try:
                self._exchange_info_cache = self.client.exchange_info()
                self._exchange_info_cache_time = now
            except Exception as e:
                logger.debug(f"Exchange info fetch error: {e}")
                if self._exchange_info_cache is None:
                    return {"symbols": []}
        return self._exchange_info_cache

    def _calculate_volatility(self, ohlc_data: Dict) -> float:
        """Calculate volatility from higher timeframe"""
        try:
            higher_tf = ohlc_data.get("higher")
            if higher_tf is not None and len(higher_tf) > 10:
                closes = higher_tf["close"].values[-20:]
                returns = np.diff(closes) / closes[:-1]
                return abs(np.std(returns))
        except Exception:
            pass
        return 0.02

    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity to exchange step size, and clamp to the symbol's min/max quantity."""
        try:
            info = self._get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    # FIX (Precision bug, root cause): all orders this bot
                    # places are MARKET orders, and Binance validates MARKET
                    # order quantity against the "MARKET_LOT_SIZE" filter,
                    # which can have a different (usually coarser) stepSize
                    # than "LOT_SIZE". Prefer MARKET_LOT_SIZE; fall back to
                    # LOT_SIZE if absent.
                    step_size_str = None
                    min_qty_str = None
                    max_qty_str = None
                    for f in s.get("filters", []):
                        if f["filterType"] == "MARKET_LOT_SIZE":
                            step_size_str = f["stepSize"]
                            min_qty_str = f.get("minQty")
                            max_qty_str = f.get("maxQty")
                            break
                    if step_size_str is None:
                        for f in s.get("filters", []):
                            if f["filterType"] == "LOT_SIZE":
                                step_size_str = f["stepSize"]
                                min_qty_str = f.get("minQty")
                                max_qty_str = f.get("maxQty")
                                break
                    if step_size_str and float(step_size_str) > 0:
                        # FIX (real bug): Decimal.quantize() rounds to match
                        # the number of decimal PLACES of its argument, not
                        # to a multiple of its value. Binance sends stepSize
                        # strings like "1.00000000" (trailing zeros) — used
                        # as-is, that forces quantizing to 8 decimal places
                        # instead of to whole numbers, leaving extra
                        # decimals that Binance then rejects as over-precise.
                        # normalize() strips the trailing zeros so the
                        # quantize step actually matches the real precision
                        # (e.g. "1.00000000" -> "1", "0.00100000" -> "0.001").
                        step = Decimal(step_size_str).normalize()
                        if step == step.to_integral_value():
                            step = step.quantize(Decimal(1))
                        rounded = float(Decimal(str(quantity)).quantize(
                            step, rounding=ROUND_DOWN
                        ))
                        # FIX (error -4005 "Quantity greater than max
                        # quantity"): the bot sized every order purely from
                        # margin x leverage and never checked the result
                        # against the exchange's own per-symbol maxQty
                        # (MARKET_LOT_SIZE/LOT_SIZE). Coins with a low max
                        # order size (e.g. KAITOUSDT) could get a computed
                        # quantity above that cap, which Binance rejects
                        # outright. Clamp into [minQty, maxQty] here so the
                        # order always sent is one Binance will accept.
                        if max_qty_str:
                            max_qty = float(max_qty_str)
                            if max_qty > 0 and rounded > max_qty:
                                logger.warning(
                                    f"⚠️ {symbol}: qty {rounded!r} exceeds exchange max "
                                    f"quantity {max_qty!r}, capping to max instead."
                                )
                                rounded = float(Decimal(str(max_qty)).quantize(
                                    step, rounding=ROUND_DOWN
                                ))
                        if min_qty_str:
                            min_qty = float(min_qty_str)
                            if min_qty > 0 and 0 < rounded < min_qty:
                                logger.warning(
                                    f"⚠️ {symbol}: qty {rounded!r} is below exchange min "
                                    f"quantity {min_qty!r}, skipping this trade."
                                )
                                return 0.0
                        logger.debug(f"🔧 {symbol}: qty {quantity!r} -> step "
                                     f"{step_size_str!r} -> rounded {rounded!r}")
                        return rounded
                    logger.warning(f"⚠️ {symbol}: no LOT_SIZE/MARKET_LOT_SIZE filter found, "
                                    f"sending unrounded quantity {quantity!r}")
                    return quantity
            logger.warning(f"⚠️ {symbol}: symbol not found in exchangeInfo, "
                            f"sending unrounded quantity {quantity!r}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol}: quantity rounding failed ({e}), "
                            f"sending unrounded quantity {quantity!r}")
        return quantity

    def _update_open_trades(self):
        """Update prices for open trades"""
        open_trades = self.trade_manager.get_open_trades()
        for symbol in open_trades:
            try:
                ticker = self.client.ticker_price(symbol=symbol)
                self.trade_manager.update_trade_price(symbol, float(ticker["price"]))
            except Exception as e:
                logger.debug(f"Price update error for {symbol}: {e}")

    def _tp1_reanalysis_decision(self, symbol: str, trade: Dict) -> Optional[Dict]:
        """
        FIX (TP1 -> TP2 continuation, extended to TP2 -> TP3 per explicit
        request): called by TradeManager the instant a trade's CURRENT take
        profit is hit, BEFORE it would otherwise be closed. Re-fetches
        fresh multi-timeframe data and re-runs the exact same analysis used
        for entries and reversal checks (same tools, same MIN_TOOLS_MATCH
        rule) — nothing new is introduced here, it's the identical decision
        process, just re-run at this exact moment for this one symbol.

        This function itself was already stage-agnostic (it just reads
        trade["take_profit"], whatever it currently is, and looks for the
        next level beyond it) - the only change here is using the trade's
        own tp_stage to label the logs/Telegram messages correctly as
        TP1->TP2 or TP2->TP3 instead of always saying "TP1"/"TP2". Whether
        this fires a second time at all is decided by TradeManager's
        _maybe_extend_to_tp2 (capped at stage 3), not here.

        Returns None if the market does NOT confirm continuation, or if
        fresh data can't be fetched right now — TradeManager then closes
        the trade at the current TP exactly as it always has. Returns
        {"extend": True, "new_sl": <price>, "new_tp": <price>} if
        continuation IS confirmed, so TradeManager moves the stop up to the
        just-hit TP price (locking in that profit) and keeps the trade open
        toward the next TP level instead.
        """
        from_stage = trade.get("tp_stage", 1)
        to_stage = from_stage + 1
        try:
            ohlc_data = self._fetch_multi_timeframe(symbol)
            if ohlc_data is None:
                logger.warning(f"⚠️ {symbol}: no fresh data for TP{from_stage} re-analysis — closing at TP{from_stage}.")
                return None
            analysis = self.analysis_engine.multi_timeframe_analysis(ohlc_data)
        except Exception as e:
            logger.error(f"⚠️ {symbol}: TP{from_stage} re-analysis fetch/analysis failed ({e}) — closing at TP{from_stage}.")
            return None

        final = analysis.get("final_signal", {})
        direction = final.get("direction", 0)
        tools_agreeing = final.get("tools_agreeing", 0)
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)

        logger.info(f"🎯 [{symbol}] TP{from_stage} HIT — re-analyzing market before deciding "
                    f"whether to close here or extend toward TP{to_stage}...")
        if _TELEGRAM_AVAILABLE:
            try:
                send_telegram(format_tp1_hit(symbol, from_stage=from_stage, to_stage=to_stage))
            except Exception as e:
                logger.warning(f"Telegram notify (TP{from_stage} hit) failed: {e}")

        side = trade["side"]
        wants_continue = (side == "BUY" and direction == 1) or (side == "SELL" and direction == -1)
        if not wants_continue or tools_agreeing < min_tools:
            logger.info(f"❌ [{symbol}] Fresh analysis does NOT confirm continuation "
                        f"({tools_agreeing}/{min_tools} tools) — closing at TP{from_stage}, profit locked.")
            if _TELEGRAM_AVAILABLE:
                try:
                    send_telegram(format_tp1_closed(symbol, tools_agreeing, min_tools, at_stage=from_stage))
                except Exception as e:
                    logger.warning(f"Telegram notify (TP{from_stage} closed) failed: {e}")
            return None  # market doesn't confirm continuation -> close at current TP, unchanged

        tp1_level = trade["take_profit"]

        # Reuse the same order-block / FVG / liquidity level detection used
        # to set the original TP/SL, just anchored at the just-hit TP price
        # instead of the entry price, to find the NEXT resistance/support
        # beyond it.
        new_tp, _ = self._get_analysis_based_tp_sl(symbol, side, tp1_level, analysis)

        min_extra = abs(tp1_level - trade["entry_price"]) * 0.5
        if new_tp is None or abs(new_tp - tp1_level) < min_extra:
            tp_percent = self.config.get("TAKE_PROFIT_PERCENT", 2.0) / 100
            extra = trade["entry_price"] * tp_percent
            new_tp = tp1_level + extra if side == "BUY" else tp1_level - extra

        # FIX (TP1->TP2 zero-buffer SL bug): locking the new stop exactly at
        # the just-hit TP price left zero room before the very next tick
        # (which is already at/through that same price) - this is why the
        # trade almost always closed right back out at TP1 instead of ever
        # reaching TP2/TP3. A small buffer (half the normal ATR trailing
        # distance, or a small % fallback) gives the position room to
        # breathe while still locking in the large majority of TP1's profit.
        entry_atr = trade.get("analysis", {}).get("entry_atr")
        if entry_atr:
            sl_buffer = entry_atr * (self.config.get("ATR_TRAILING_MULTIPLIER", 2.0) * 0.5)
        else:
            sl_buffer = tp1_level * (self.config.get("TRAILING_STOP_DISTANCE", 0.3) / 100)

        new_sl = tp1_level + sl_buffer if side == "SELL" else tp1_level - sl_buffer

        logger.info(f"✅ [{symbol}] Continuation CONFIRMED ({tools_agreeing}/{min_tools} tools) — "
                    f"extending to TP{to_stage}.")
        logger.info(f"   New Stop Loss: {new_sl:.8f} (was TP{from_stage} — profit now locked, can't go back to loss)")
        logger.info(f"   New Take Profit (TP{to_stage}): {new_tp:.8f}")
        if _TELEGRAM_AVAILABLE:
            try:
                send_telegram(format_tp1_extended(symbol, tools_agreeing, min_tools, new_sl, new_tp, to_stage=to_stage))
            except Exception as e:
                logger.warning(f"Telegram notify (TP{from_stage} extended) failed: {e}")

        return {"extend": True, "new_sl": new_sl, "new_tp": new_tp}

    def _check_trade_reversals(self):
        """
        Check if open trades should close due to reversal.

        FIX (whipsaw protection): a trade used to become eligible for
        REVERSAL_SIGNAL close on the very next 30s scan after opening —
        the same min_tools=3 bar used for entry. Brief noise right after
        entry could flip that bar the other way for one scan and get the
        trade closed seconds later, even if price would have gone back in
        the original direction moments after. This does NOT touch
        STOP_LOSS or TAKE_PROFIT in any way — those still trigger exactly
        as before, at any moment, including during this window. It only
        holds off the *reversal-based* manual close until the trade has
        been open for REVERSAL_COOLDOWN_SECONDS, so a fresh trade gets a
        short grace period to ride out momentary noise before a reversal
        signal is allowed to close it.
        """
        open_trades = self.trade_manager.get_open_trades()
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
        cooldown_seconds = self.config.get("REVERSAL_COOLDOWN_SECONDS", 240)

        for symbol, trade in open_trades.items():
            entry_time = trade.get("entry_time")
            if entry_time:
                age_seconds = (datetime.now() - entry_time).total_seconds()
                if age_seconds < cooldown_seconds:
                    continue  # still in grace period — SL/TP remain fully active regardless

            cached = self.analysis_cache.get(symbol)
            if not cached:
                continue

            final = cached["analysis"].get("final_signal", {})
            current_direction = final.get("direction", 0)
            tools = final.get("tools_agreeing", 0)

            if trade["side"] == "BUY" and current_direction == -1 and tools >= min_tools:
                logger.info(f"🔄 Reversal: Closing BUY {symbol}")
                self.trade_manager.close_trade_manually(symbol, "REVERSAL_SIGNAL")
            elif trade["side"] == "SELL" and current_direction == 1 and tools >= min_tools:
                logger.info(f"🔄 Reversal: Closing SELL {symbol}")
                self.trade_manager.close_trade_manually(symbol, "REVERSAL_SIGNAL")

    def _log_status(self):
        """Log current bot status"""
        open_trades = self.trade_manager.get_open_trades()
        total_pnl = self.trade_manager.get_total_pnl()
        closed_count = len(self.trade_manager.trade_history)
        wins = sum(1 for t in self.trade_manager.trade_history if t.get("pnl_percent", 0) > 0)
        win_rate = (wins / closed_count * 100) if closed_count > 0 else 0

        logger.info(f"═══════════════════════════════════════")
        logger.info(f"📊 Balance: ${self.balance:.2f} | "
                    f"Open: {len(open_trades)} | "
                    f"Total PnL: ${total_pnl:.2f} | "
                    f"Win Rate: {win_rate:.1f}% ({wins}/{closed_count})")

        # FIX (rate-limit-free dashboard support): write the SAME status
        # this function already logs to a small local file, so a separate
        # dashboard/backend process can read live balance/PnL/win-rate from
        # disk instead of calling Binance's API itself. This adds ZERO
        # extra Binance API calls - self.balance and trade_manager's data
        # are already being fetched/maintained by the bot for its own
        # trading logic; we're just also writing them out. Atomic write
        # (tmp + rename), same pattern as trade_manager._save_state.
        try:
            status_data = {
                "updated_at": datetime.now().isoformat(),
                "balance": self.balance,
                "open_count": len(open_trades),
                "total_pnl": total_pnl,
                "win_rate": win_rate,
                "wins": wins,
                "closed_count": closed_count,
            }
            tmp_path = "live_status.json.tmp"
            with open(tmp_path, "w") as f:
                json.dump(status_data, f, indent=2, default=str)
            os.replace(tmp_path, "live_status.json")
        except Exception as e:
            logger.error(f"⚠️ Failed to write live_status.json: {e}")

        if self.waiting_for_balance:
            logger.info("⏳ Waiting for funds to be deposited...")

        for sym, trade in open_trades.items():
            pnl_icon = "🟢" if trade["pnl_percent"] >= 0 else "🔴"
            trailing_note = " (trailing active)" if trade.get("trailing_activated") else ""
            logger.info(f"   {pnl_icon} [{sym}] {trade['side']} | PnL: {trade['pnl_percent']:+.2f}% | "
                        f"Entry: {trade['entry_price']:.8f} | SL: {trade['stop_loss']:.8f} | "
                        f"TP: {trade['take_profit']:.8f}{trailing_note}")

        if self.trade_manager.trade_history:
            recent = self.trade_manager.trade_history[-3:]
            logger.info(f"   📋 Last {len(recent)} closed trades:")
            for t in recent:
                icon = "🟢" if t['pnl_percent'] >= 0 else "🔴"
                logger.info(f"      {icon} [{t['symbol']}] {t['side']} | "
                            f"PnL: {t['pnl_percent']:+.2f}% | "
                            f"Reason: {t.get('close_reason', 'N/A')}")

        logger.info(f"═══════════════════════════════════════")
        
