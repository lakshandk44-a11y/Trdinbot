"""
HackerAI Auto Trading Bot - Core Bot Logic
24/7 Scanning | Balance Crash Protection | 65% Profit Filter
Auto Leverage | Auto Min Notional Check
"""

import logging
import time
import hashlib
import hmac
import requests
import json
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from decimal import Decimal, ROUND_DOWN

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

    def klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
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

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
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
                        order_type: str = "STOP_MARKET", reduce_only: bool = True) -> dict:
        """
        Place a real resting STOP_MARKET or TAKE_PROFIT_MARKET order on the
        exchange. Unlike the bot's own polling-based SL/TP check (which only
        protects the position while this process is running), this order
        lives on Binance's servers and will trigger even if the bot/VPS
        goes offline.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "stopPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
            "workingType": "MARK_PRICE"
        }
        return self._post("/fapi/v1/order", params)

    def get_order(self, symbol: str, orderId: int = None, origClientOrderId: str = None) -> dict:
        params = {"symbol": symbol}
        if orderId:
            params["orderId"] = orderId
        if origClientOrderId:
            params["origClientOrderId"] = origClientOrderId
        return self._get("/fapi/v1/order", params)

    def cancel_order(self, symbol: str, orderId: int = None) -> dict:
        params = {"symbol": symbol}
        if orderId:
            params["orderId"] = orderId
        return self._delete("/fapi/v1/order", params)


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

        # State
        self.balance = 0.0
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

    def start(self):
        """Start the bot"""
        if self.running:
            logger.warning("⚠️ Bot already running")
            return

        self.running = True
        self.paused = False
        logger.info("🚀 HackerAI Bot STARTING in 24/7 mode...")

        # FIX (Persistence Bug): cross-check any trades restored from disk
        # against what's actually open on Binance right now, in case
        # positions were closed (manually, or by an exchange-side SL/TP
        # order) while the bot was offline.
        self.trade_manager.reconcile_with_exchange()

        self.trade_manager.start_monitoring()

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

                # Get top 40 coins
                coins_to_scan = self._get_top_coins()

                # Scan all coins 24/7
                self._scan_coins_247(coins_to_scan)

                # Update open trades
                self._update_open_trades()

                # Check reversals
                self._check_trade_reversals()

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
        """Get top 40 coins from Binance by volume"""
        coins = self.config.get("TOP_40_COINS", TOP_40_COINS)
        try:
            tickers = self.client.ticker_24hr()
            usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
            sorted_by_volume = sorted(usdt_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
            top_40 = [t["symbol"] for t in sorted_by_volume[:40]]
            if top_40:
                coins = top_40
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

                    if tools_agreeing >= min_tools and profit_chance >= min_chance:
                        self._execute_trade(symbol, decision, final, ohlc_data)

            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")
                continue

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
        return result

    def _execute_trade(self, symbol: str, decision: str, signal: Dict, ohlc_data: Dict):
        """
        Execute trade on Binance Futures
        Auto coin min notional, max leverage, and balance check
        """
        direction = signal.get("direction", 0)
        if direction == 0:
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
                    brackets = s.get("leverageBrackets", [])
                    if brackets:
                        coin_max_leverage = int(brackets[0].get("initialLeverage", 20))
                    break
        except Exception as e:
            logger.debug(f"Exchange info fetch error for {symbol}: {e}")

        # Calculate margin (exactly 5% of balance)
        balance_pct = self.config.get("BALANCE_PERCENTAGE", 5) / 100.0
        margin = self.balance * balance_pct

        if margin < 0.001:
            logger.warning(f"⚠️ Margin too small: ${margin:.4f}. Cannot trade {symbol}")
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

        # Calculate optimal leverage
        optimal_leverage = coin_min_notional / margin
        optimal_leverage = int(optimal_leverage) + (1 if optimal_leverage % 1 > 0 else 0)
        trade_leverage = min(optimal_leverage, effective_max_lev)

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

        # Set leverage on Binance
        try:
            self.client.change_leverage(symbol=symbol, leverage=final_leverage)
            logger.info(f"⚙️ Leverage set: {symbol} = {final_leverage}x")
        except Exception as e:
            logger.warning(f"Leverage change warning for {symbol}: {e}")

        # Calculate position size
        sl_percent = self.config.get("STOP_LOSS_PERCENT", 1.0) / 100.0
        if decision == "BUY":
            sl_price = current_price * (1 - sl_percent)
        else:
            sl_price = current_price * (1 + sl_percent)

        quantity = self.trade_manager.calculate_position_size(
            margin * final_leverage,
            current_price,
            sl_price,
            final_leverage
        )
        quantity = self._round_quantity(symbol, quantity)

        if quantity <= 0:
            logger.warning(f"⚠️ Invalid quantity for {symbol}: {quantity}")
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

            trade = self.trade_manager.open_new_trade(
                symbol=symbol,
                side=side,
                entry_price=current_price,
                quantity=quantity,
                leverage=final_leverage,
                analysis_result={
                    "signal": signal,
                    "volatility": volatility,
                    "tools_agreeing": signal.get("tools_agreeing", 0),
                    "profit_chance": signal.get("profit_chance", 0),
                    "margin_used": margin,
                    "position_value": final_position,
                    "min_notional": coin_min_notional,
                }
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
        """Round quantity to exchange step size"""
        try:
            info = self._get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    # FIX (Precision bug): all orders this bot places are
                    # MARKET orders, and Binance validates MARKET order
                    # quantity against the "MARKET_LOT_SIZE" filter, which
                    # can have a different (usually coarser) stepSize than
                    # "LOT_SIZE". Rounding to LOT_SIZE alone can still be
                    # too precise for a MARKET order and trigger
                    # "Precision is over the maximum defined for this asset."
                    # Prefer MARKET_LOT_SIZE; fall back to LOT_SIZE if absent.
                    step_size = None
                    for f in s.get("filters", []):
                        if f["filterType"] == "MARKET_LOT_SIZE":
                            step_size = float(f["stepSize"])
                            break
                    if step_size is None:
                        for f in s.get("filters", []):
                            if f["filterType"] == "LOT_SIZE":
                                step_size = float(f["stepSize"])
                                break
                    if step_size:
                        rounded = float(Decimal(str(quantity)).quantize(
                            Decimal(str(step_size)), rounding=ROUND_DOWN
                        ))
                        logger.debug(f"🔧 {symbol}: qty {quantity!r} -> step {step_size!r} "
                                     f"-> rounded {rounded!r}")
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

    def _check_trade_reversals(self):
        """Check if open trades should close due to reversal"""
        open_trades = self.trade_manager.get_open_trades()
        min_tools = self.config.get("MIN_TOOLS_MATCH", 3)

        for symbol, trade in open_trades.items():
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

        if self.waiting_for_balance:
            logger.info("⏳ Waiting for funds to be deposited...")

        for sym, trade in open_trades.items():
            logger.info(f"   {sym}: {trade['side']} | "
                        f"PnL: {trade['pnl_percent']:.2f}% | "
                        f"Entry: {trade['entry_price']:.8f}")

        if self.trade_manager.trade_history:
            recent = self.trade_manager.trade_history[-3:]
            for t in recent:
                logger.info(f"   📋 Closed: {t['symbol']} {t['side']} | "
                            f"PnL: {t['pnl_percent']:.2f}% | "
                            f"Reason: {t.get('close_reason', 'N/A')}")

        logger.info(f"═══════════════════════════════════════")
