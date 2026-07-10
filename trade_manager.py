"""
HackerAI Auto Trading Bot - Trade Manager
Manages all open trades 24/7
"""

import logging
import threading
import time
import json
import os
from typing import Dict, List, Optional
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

logger = logging.getLogger(__name__)

class TradeManager:
    """
    Manages all open trades:
    - SL/TP
    - Trailing stops
    - Market monitoring
    - Auto-close
    """
    
    def __init__(self, config: Dict, binance_client=None):
        self.config = config
        self.client = binance_client
        self.open_trades: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []
        self.lock = threading.Lock()
        self.running = False
        self.monitor_thread = None

        # FIX (Hedge Mode bug): whether this Binance account is in Hedge
        # (dual-side) Mode. Set by bot_core right after construction, once
        # it has queried the exchange. Defaults to False (One-way) so
        # nothing changes for accounts that don't use Hedge Mode.
        self.hedge_mode = False

        # FIX (Price precision bug): cached PRICE_FILTER lookup so SL/TP
        # prices sent to Binance are rounded to each symbol's tick size
        # instead of the raw calculated float. Mirrors bot_core's own
        # exchangeInfo cache so this doesn't add extra API calls per trade.
        self._exchange_info_cache = None
        self._exchange_info_cache_time = 0.0
        self._exchange_info_ttl = 3600  # seconds

        # FIX (Persistence Bug): open trades used to live only in memory.
        # If the bot process or the VPS restarted, the bot completely lost
        # track of any real open Binance positions and their SL/TP levels —
        # they kept existing on the exchange, unmanaged. State is now saved
        # to disk on every change and reloaded here on startup.
        self.state_file = self.config.get("TRADE_STATE_FILE", "trade_state.json")
        self._load_state()

        # FIX (TP1 -> TP2 continuation): optional callback, wired up by
        # bot_core via set_tp1_reanalysis_callback(), that re-runs fresh
        # market analysis the moment a trade's TP1 is hit. TradeManager
        # itself has no analysis engine or OHLC data — it only asks the
        # callback for a decision and acts on the result. Left as None
        # (feature inert) until bot_core registers it.
        self.tp1_reanalysis_callback = None
        
    def set_tp1_reanalysis_callback(self, callback):
        """
        FIX (TP1 -> TP2 continuation): register the function bot_core uses
        to re-analyze a symbol the instant its TP1 is hit. Expected
        signature: callback(symbol: str, trade: Dict) -> Optional[Dict].
        Return None (or a dict without "extend": True) to close at TP1 as
        before; return {"extend": True, "new_sl": <price>, "new_tp": <price>}
        to keep the trade open with those new levels instead.
        """
        self.tp1_reanalysis_callback = callback

    def start_monitoring(self):
        """Start background monitoring (24/7)"""
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("🔄 Trade Manager monitoring started (24/7)")
        
    def stop_monitoring(self):
        """Stop monitoring"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("⏹️ Trade Manager monitoring stopped")

    # ------------------------------------------------------------------
    # FIX (Persistence Bug): state save/load so open trades and their
    # SL/TP/trailing levels survive a bot or VPS restart.
    # ------------------------------------------------------------------
    _DATETIME_FIELDS = ["entry_time", "last_update", "close_time"]

    def _serialize_trade(self, trade: Dict) -> Dict:
        data = dict(trade)
        for field in self._DATETIME_FIELDS:
            if field in data and isinstance(data[field], datetime):
                data[field] = data[field].isoformat()
        return data

    def _deserialize_trade(self, data: Dict) -> Dict:
        trade = dict(data)
        for field in self._DATETIME_FIELDS:
            if field in trade and isinstance(trade[field], str):
                try:
                    trade[field] = datetime.fromisoformat(trade[field])
                except ValueError:
                    pass
        return trade

    def _save_state(self):
        """Persist open trades + recent history to disk (atomic write)."""
        try:
            with self.lock:
                data = {
                    "open_trades": {s: self._serialize_trade(t) for s, t in self.open_trades.items()},
                    "trade_history": [self._serialize_trade(t) for t in self.trade_history[-200:]]
                }
            tmp_path = f"{self.state_file}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp_path, self.state_file)
        except Exception as e:
            logger.error(f"⚠️ Failed to save trade state to {self.state_file}: {e}")

    def _load_state(self):
        """Restore open trades + history from disk, if a state file exists."""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            loaded_open = data.get("open_trades", {})
            loaded_history = data.get("trade_history", [])
            with self.lock:
                self.open_trades = {s: self._deserialize_trade(t) for s, t in loaded_open.items()}
                self.trade_history = [self._deserialize_trade(t) for t in loaded_history]
            if self.open_trades:
                logger.info(f"♻️ Restored {len(self.open_trades)} open trade(s) from "
                            f"{self.state_file}: {list(self.open_trades.keys())}")
        except Exception as e:
            logger.error(f"⚠️ Failed to load trade state from {self.state_file}: {e}")

    def reconcile_with_exchange(self):
        """
        FIX (Persistence Bug, part 2): after restoring trades from disk,
        cross-check them against Binance's REAL open positions. This
        catches cases where a position was closed while the bot was
        offline (manually, or by an exchange-side SL/TP order), and warns
        about any real exchange position the bot has no local record of.
        Call this once at startup, before start_monitoring().
        """
        if not self.client:
            return

        try:
            positions = self.client.position_risk()
        except Exception as e:
            logger.error(f"⚠️ Could not fetch exchange positions for reconciliation: {e}")
            return

        real_open = {}
        for p in positions:
            try:
                amt = float(p.get("positionAmt", 0))
            except (TypeError, ValueError):
                amt = 0.0
            if abs(amt) > 1e-10:
                real_open[p["symbol"]] = p

        with self.lock:
            local_symbols = list(self.open_trades.keys())

        # Local trades no longer actually open on the exchange
        for symbol in local_symbols:
            if symbol not in real_open:
                logger.warning(f"♻️ {symbol} was tracked locally but has no open position on "
                                f"Binance (closed while the bot was offline). Removing from "
                                f"local tracking.")
                with self.lock:
                    trade = self.open_trades.pop(symbol, None)
                if trade:
                    trade["close_time"] = datetime.now()
                    trade["close_price"] = trade.get("current_price", trade.get("entry_price"))
                    trade["close_reason"] = "RECONCILED_CLOSED_EXTERNALLY"
                    trade["status"] = "CLOSED"
                    with self.lock:
                        self.trade_history.append(trade)

        # Real exchange positions with no local record at all
        for symbol, pos in real_open.items():
            with self.lock:
                known = symbol in self.open_trades
            if not known:
                logger.warning(f"⚠️ Binance shows an OPEN position for {symbol} "
                                f"(positionAmt={pos.get('positionAmt')}) that the bot has NO "
                                f"local record of. This position is NOT being managed/protected "
                                f"by the bot — please check it manually on Binance.")

        self._save_state()

    # ------------------------------------------------------------------
    # FIX (Price precision bug): SL/TP prices were sent to Binance exactly
    # as calculated (e.g. entry_price * 0.99), with no regard for the
    # symbol's PRICE_FILTER tick size. Coins whose tick size doesn't match
    # that raw float's decimal places got their protective orders REJECTED
    # by Binance with "Invalid price precision", leaving the position with
    # no exchange-side protection at all.
    # ------------------------------------------------------------------
    def _get_exchange_info(self) -> dict:
        """Cached exchangeInfo lookup (refreshed at most once per hour)."""
        if not self.client:
            return {"symbols": []}
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

    def _round_price(self, symbol: str, price: float) -> float:
        """Round a price to the symbol's PRICE_FILTER tick size."""
        try:
            info = self._get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s.get("filters", []):
                        if f["filterType"] == "PRICE_FILTER":
                            tick_size_str = f["tickSize"]
                            if float(tick_size_str) > 0:
                                # FIX (real bug, same root cause as the
                                # quantity precision bug): Decimal.quantize()
                                # rounds to match the number of decimal
                                # PLACES of its argument, not to a multiple
                                # of its value. Binance sends tickSize
                                # strings like "0.10000000" or "1.00000000"
                                # (trailing zeros) — using tick_size = float(...)
                                # then str(tick_size) back loses those extra
                                # zeros' meaning and can leave the rounded
                                # price with more decimals than the symbol
                                # actually allows, which Binance rejects as
                                # "Precision is over the maximum defined for
                                # this asset." normalize() strips the
                                # trailing zeros so quantizing matches the
                                # real precision (e.g. "1.00000000" -> "1").
                                tick = Decimal(tick_size_str).normalize()
                                if tick == tick.to_integral_value():
                                    tick = tick.quantize(Decimal(1))
                                price = float(Decimal(str(price)).quantize(
                                    tick, rounding=ROUND_HALF_UP
                                ))
                            return price
        except Exception as e:
            logger.debug(f"Price rounding lookup failed for {symbol}: {e}")
        return price

    # ------------------------------------------------------------------
    # FIX (Missing exchange-side protection): real STOP_MARKET /
    # TAKE_PROFIT_MARKET orders on Binance so a position stays protected
    # even if the bot process or VPS goes offline.
    # ------------------------------------------------------------------
    def place_protective_orders(self, symbol: str, trade: Dict):
        """Place real exchange-side SL and TP orders right after opening a trade."""
        if not self.client:
            logger.warning(f"⚠️ No exchange client attached — {symbol} has NO exchange-side "
                            f"SL/TP protection, only the bot's software-side monitor.")
            self._save_state()
            return

        close_side = "SELL" if trade["side"] == "BUY" else "BUY"

        # FIX (Hedge Mode bug): positionSide must identify the position
        # being protected (LONG/SHORT) on a Hedge Mode account, and must be
        # omitted entirely on a One-way account.
        position_side = trade.get("position_side") if self.hedge_mode else None

        # FIX (Price precision bug): round to the symbol's tick size before
        # sending. Also update the trade dict so the stored SL/TP matches
        # exactly what's resting on the exchange.
        trade["stop_loss"] = self._round_price(symbol, trade["stop_loss"])
        trade["take_profit"] = self._round_price(symbol, trade["take_profit"])

        try:
            sl_order = self.client.new_stop_order(
                symbol=symbol, side=close_side, stop_price=trade["stop_loss"],
                quantity=trade["quantity"], order_type="STOP_MARKET",
                positionSide=position_side
            )
            trade["sl_order_id"] = sl_order.get("orderId")
            trade["_last_exchange_sl"] = trade["stop_loss"]
            logger.info(f"🛡️ Exchange STOP_MARKET placed: {symbol} @ {trade['stop_loss']:.8f}")
        except Exception as e:
            trade["sl_order_id"] = None
            logger.error(f"⚠️ Could not place exchange STOP_MARKET for {symbol}: {e}. "
                         f"Falling back to software-side monitoring only for this trade.")

        # FIX (TP1 -> TP2 continuation): a resting exchange-side
        # TAKE_PROFIT_MARKET order would fully close the position the
        # instant price touches TP1 — before the bot's own monitor loop
        # gets a chance to re-analyze the market and possibly extend to
        # TP2. So while this feature is enabled, TP is watched and acted
        # on by the bot's software-side monitor only (still every 5s, see
        # _monitor_loop); the SL above stays a real exchange order either
        # way, so the trade is never left without exchange-side downside
        # protection even if the bot/VPS goes offline.
        if self.config.get("TP1_REANALYSIS_ENABLED", False):
            trade["tp_order_id"] = None
            logger.info(f"🧠 {symbol}: TP managed by bot analysis (TP1_REANALYSIS_ENABLED) — "
                        f"no resting exchange TP order; SL stays exchange-protected.")
        else:
            try:
                tp_order = self.client.new_stop_order(
                    symbol=symbol, side=close_side, stop_price=trade["take_profit"],
                    quantity=trade["quantity"], order_type="TAKE_PROFIT_MARKET",
                    positionSide=position_side
                )
                trade["tp_order_id"] = tp_order.get("orderId")
                logger.info(f"🛡️ Exchange TAKE_PROFIT_MARKET placed: {symbol} @ {trade['take_profit']:.8f}")
            except Exception as e:
                trade["tp_order_id"] = None
                logger.error(f"⚠️ Could not place exchange TAKE_PROFIT_MARKET for {symbol}: {e}. "
                             f"Falling back to software-side monitoring only for this trade.")

        self._save_state()

    def _cancel_protective_orders(self, symbol: str, trade: Dict):
        """Cancel any still-resting exchange SL/TP orders (e.g. before closing manually)."""
        if not self.client:
            return
        for key in ("sl_order_id", "tp_order_id"):
            order_id = trade.get(key)
            if order_id:
                try:
                    self.client.cancel_order(symbol=symbol, orderId=order_id)
                except Exception as e:
                    # This is expected/harmless if that order already triggered and filled
                    logger.debug(f"Cancel {key}={order_id} for {symbol} skipped/failed "
                                 f"(may already be filled): {e}")

    def _update_trailing_stop_order(self, symbol: str, trade: Dict, new_sl_price: float):
        """
        Move the resting exchange STOP_MARKET order when the bot's trailing
        stop advances. Throttled to avoid cancel/replace on every tiny tick.

        FIX (protection gap on error -2021 "Order would immediately
        trigger"): this used to CANCEL the old resting stop order first,
        then place the new one. If price moved fast between those two
        calls, the new order could arrive at a stop price already on the
        wrong side of the current market price, and Binance rejects it
        with -2021 — leaving the position with NO exchange-side stop at
        all until the next successful trailing update (software-side
        polling was still watching it, but the whole point of the
        exchange-side order is to protect the position even if the bot/VPS
        goes down). Placing the new order FIRST and only cancelling the
        old one after that succeeds removes that gap: worst case there are
        briefly two resting reduceOnly stop orders (harmless — filling one
        flattens the position, so the other simply has nothing left to
        reduce), and if the new order is rejected the original order is
        left in place instead of being torn down for nothing.
        """
        # FIX (Price precision bug): round the new trailing SL to the
        # symbol's tick size before storing/sending it.
        new_sl_price = self._round_price(symbol, new_sl_price)
        trade["stop_loss"] = new_sl_price

        if not self.client:
            return

        last_exchange_sl = trade.get("_last_exchange_sl", trade["entry_price"])
        if last_exchange_sl and abs(new_sl_price - last_exchange_sl) / last_exchange_sl < 0.0005:
            return  # change too small to be worth an exchange round-trip yet

        close_side = "SELL" if trade["side"] == "BUY" else "BUY"
        old_order_id = trade.get("sl_order_id")

        # FIX (Hedge Mode bug): same positionSide rule as place_protective_orders.
        position_side = trade.get("position_side") if self.hedge_mode else None

        try:
            new_order = self.client.new_stop_order(
                symbol=symbol, side=close_side, stop_price=new_sl_price,
                quantity=trade["quantity"], order_type="STOP_MARKET",
                positionSide=position_side
            )
        except Exception as e:
            logger.error(f"⚠️ Could not move exchange stop for {symbol}: {e}. "
                         f"Leaving the existing exchange order in place (not cancelled) — "
                         f"software-side monitor still has the updated level.")
            return

        trade["sl_order_id"] = new_order.get("orderId")
        trade["_last_exchange_sl"] = new_sl_price
        logger.info(f"🔺 Exchange trailing stop moved: {symbol} @ {new_sl_price:.8f}")

        try:
            if old_order_id:
                self.client.cancel_order(symbol=symbol, orderId=old_order_id)
        except Exception as e:
            logger.debug(f"Could not cancel old SL order for {symbol} (may already be gone): {e}")
    
    def _monitor_loop(self):
        """Monitor all trades every 5 seconds"""
        while self.running:
            try:
                with self.lock:
                    trades_copy = dict(self.open_trades)
                
                for symbol, trade in trades_copy.items():
                    self._evaluate_trade(trade)

                self._save_state()
                time.sleep(5)
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                time.sleep(10)
    
    def calculate_position_size(self, position_value: float, entry_price: float,
                                stop_loss_price: float, leverage: int,
                                account_balance: Optional[float] = None) -> float:
        """
        Calculate order quantity (base-asset units).

        FIX (margin-insufficient regression): `position_value` here is the
        already-validated margin*leverage notional the caller computed in
        bot_core._execute_trade (logged as "TRADE POSSIBLE! ... Position=$X")
        - it has already been checked against the exchange's min/max
        notional and the actually available margin. That MUST stay the
        primary driver of order size, or the order can request a
        completely different (often far larger) notional than what was
        validated, which is exactly what caused the -2019 "Margin is
        insufficient" errors: the previous version of this function ignored
        position_value entirely and derived quantity purely from
        account_balance * RISK_PER_TRADE / stop-distance, with no ceiling
        tied to the margin actually allocated for the trade.

        account_balance (optional) is only used as a downward safety cap:
        if the validated position_value would risk more than
        RISK_PER_TRADE% of the real account balance on a stop-loss hit,
        the quantity is scaled DOWN (never up) to keep real risk within
        that budget. This also fixes the earlier double-leverage bug
        (real risk used to be risk_amount * leverage instead of
        risk_amount) without reintroducing this regression.
        """
        quantity = position_value / entry_price

        if account_balance:
            risk_percent = self.config.get("RISK_PER_TRADE", 0.02)
            risk_amount = account_balance * risk_percent
            price_risk = abs(entry_price - stop_loss_price)
            if price_risk > 0:
                actual_risk = quantity * price_risk
                if actual_risk > risk_amount:
                    quantity = risk_amount / price_risk  # scale down only

        return quantity
    
    def open_new_trade(self, symbol: str, side: str, entry_price: float, 
                       quantity: float, leverage: int, 
                       analysis_result: Dict,
                       dynamic_tp: Optional[float] = None,
                       dynamic_sl: Optional[float] = None) -> Optional[Dict]:
        """Open a new trade"""
        with self.lock:
            if len(self.open_trades) >= self.config.get("MAX_OPEN_TRADES", 15):
                logger.warning(f"⚠️ Max trades ({len(self.open_trades)}). Cannot open {symbol}")
                return None
            
            if symbol in self.open_trades:
                logger.warning(f"⚠️ Already in trade for {symbol}")
                return None
        
        tp_percent = self.config.get("TAKE_PROFIT_PERCENT", 2.0) / 100
        sl_percent = self.config.get("STOP_LOSS_PERCENT", 1.0) / 100

        # FIX (analysis-based TP/SL): use the levels the caller derived from
        # this trade's own order block / FVG / liquidity analysis, when
        # available and sane (on the correct side of entry). Otherwise fall
        # back to the original flat-percentage calculation, unchanged.
        if side.upper() == "BUY":
            take_profit = dynamic_tp if (dynamic_tp is not None and dynamic_tp > entry_price) \
                else entry_price * (1 + tp_percent)
            stop_loss = dynamic_sl if (dynamic_sl is not None and dynamic_sl < entry_price) \
                else entry_price * (1 - sl_percent)
        else:
            take_profit = dynamic_tp if (dynamic_tp is not None and dynamic_tp < entry_price) \
                else entry_price * (1 - tp_percent)
            stop_loss = dynamic_sl if (dynamic_sl is not None and dynamic_sl > entry_price) \
                else entry_price * (1 + sl_percent)

        # FIX (Price precision bug): round to the symbol's tick size right
        # away so the stored SL/TP always matches what's actually sent to
        # Binance later.
        take_profit = self._round_price(symbol, take_profit)
        stop_loss = self._round_price(symbol, stop_loss)

        # FIX (Hedge Mode bug): record which side of a Hedge Mode position
        # (LONG/SHORT) this trade belongs to, so every later order
        # (protective SL/TP, trailing stop update, close) can send the
        # correct positionSide. "BOTH" is Binance's One-way Mode value and
        # is simply never sent (see BinanceFuturesClient.new_order).
        position_side = ("LONG" if side.upper() == "BUY" else "SHORT") if self.hedge_mode else "BOTH"

        trade = {
            "symbol": symbol,
            "side": side.upper(),
            "position_side": position_side,
            "entry_price": entry_price,
            "quantity": quantity,
            "leverage": leverage,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            # FIX (TP1 -> TP2 continuation): 1 = still targeting the
            # original TP; 2 = already extended past TP1 into a TP2 run.
            # Extension is only ever attempted once per trade (stage 1->2).
            "tp_stage": 1,
            "current_price": entry_price,
            "pnl_percent": 0.0,
            "pnl_amount": 0.0,
            "highest_price": entry_price if side.upper() == "BUY" else None,
            "lowest_price": entry_price if side.upper() == "SELL" else None,
            "trailing_activated": False,
            "entry_time": datetime.now(),
            "last_update": datetime.now(),
            "analysis": analysis_result,
            "status": "OPEN",
            "binance_order_id": None,
            "sl_order_id": None,
            "tp_order_id": None,
            "_last_exchange_sl": stop_loss
        }
        
        with self.lock:
            self.open_trades[symbol] = trade
        
        logger.info(f"✅ NEW TRADE: {side} {symbol} @ {entry_price:.8f} | "
                   f"Qty: {quantity} | Lev: {leverage}x | "
                   f"TP: {take_profit:.8f} | SL: {stop_loss:.8f}")

        self._save_state()

        return trade
    
    def update_trade_price(self, symbol: str, current_price: float):
        """Update current price for a trade"""
        with self.lock:
            if symbol not in self.open_trades:
                return
            
            trade = self.open_trades[symbol]
            trade["current_price"] = current_price
            trade["last_update"] = datetime.now()
            
            if trade["side"] == "BUY":
                pnl_change = (current_price - trade["entry_price"]) / trade["entry_price"]
                trade["pnl_percent"] = pnl_change * 100 * trade["leverage"]
                trade["pnl_amount"] = pnl_change * trade["quantity"] * current_price * trade["leverage"]
                
                if trade["highest_price"] is None or current_price > trade["highest_price"]:
                    trade["highest_price"] = current_price
                    
            else:
                pnl_change = (trade["entry_price"] - current_price) / trade["entry_price"]
                trade["pnl_percent"] = pnl_change * 100 * trade["leverage"]
                trade["pnl_amount"] = pnl_change * trade["quantity"] * current_price * trade["leverage"]
                
                if trade["lowest_price"] is None or current_price < trade["lowest_price"]:
                    trade["lowest_price"] = current_price
    
    def _maybe_extend_to_tp2(self, trade: Dict) -> bool:
        """
        FIX (TP1 -> TP2 continuation): called the instant TP1 is hit, before
        the trade would otherwise be closed. Asks bot_core's registered
        callback to re-analyze the market for this symbol RIGHT NOW. If the
        fresh analysis still supports continuation, the trade's SL is moved
        up to the TP1 price (locking in that profit) and a new TP2 target is
        set — the trade stays OPEN instead of closing. Returns True if the
        trade was extended (caller must NOT close it), False if it should
        close at TP1 exactly as before (feature disabled, no callback,
        analysis unavailable, or analysis doesn't confirm continuation).

        This only ever fires once per trade: tp_stage flips from 1 to 2 as
        soon as it fires, and this function immediately no-ops for any
        trade already at stage 2 (or beyond), so a TP2 hit later always
        closes the trade normally.
        """
        if not self.config.get("TP1_REANALYSIS_ENABLED", False):
            return False
        if trade.get("tp_stage", 1) != 1:
            return False
        if not self.tp1_reanalysis_callback:
            return False

        symbol = trade["symbol"]
        try:
            decision = self.tp1_reanalysis_callback(symbol, trade)
        except Exception as e:
            logger.error(f"⚠️ TP1 re-analysis callback failed for {symbol}: {e}. "
                         f"Closing at TP1 as normal.")
            return False

        if not decision or not decision.get("extend"):
            logger.info(f"🧠 {symbol}: fresh analysis does NOT confirm continuation — "
                        f"closing at TP1 as normal.")
            return False

        new_sl = decision.get("new_sl")
        new_tp = decision.get("new_tp")
        side = trade["side"]

        # Sanity check: the new levels must actually sit on the correct
        # side of the current price, or extending would be meaningless
        # (or immediately re-trigger). If they don't check out, fall back
        # to closing at TP1 instead of trusting a bad level.
        current_price = trade["current_price"]
        if new_sl is None or new_tp is None:
            return False
        if side == "BUY" and not (new_sl < current_price < new_tp):
            logger.warning(f"⚠️ {symbol}: TP1 extension levels failed sanity check "
                            f"(SL {new_sl} / price {current_price} / TP {new_tp}) — closing at TP1.")
            return False
        if side == "SELL" and not (new_tp < current_price < new_sl):
            logger.warning(f"⚠️ {symbol}: TP1 extension levels failed sanity check "
                            f"(TP {new_tp} / price {current_price} / SL {new_sl}) — closing at TP1.")
            return False

        old_tp = trade["take_profit"]
        old_sl = trade["stop_loss"]
        trade["take_profit"] = self._round_price(symbol, new_tp)
        trade["tp_stage"] = 2

        # Move the exchange-side stop up to lock in the TP1-level profit.
        # Reuses the same cancel-safe "place new, then cancel old" trailing
        # stop mechanism already used elsewhere, so there's never a moment
        # with zero exchange-side protection.
        self._update_trailing_stop_order(symbol, trade, new_sl)

        logger.info(f"🚀 {symbol}: continuation confirmed — extending past TP1. "
                    f"SL {old_sl:.8f} -> {trade['stop_loss']:.8f} | "
                    f"TP {old_tp:.8f} -> {trade['take_profit']:.8f} (TP2)")

        self._save_state()
        return True

    def _evaluate_trade(self, trade: Dict):
        """Evaluate trade for SL/TP/trailing"""
        if trade["status"] != "OPEN":
            return
            
        current_price = trade["current_price"]
        side = trade["side"]
        
        # Take Profit check
        if side == "BUY" and current_price >= trade["take_profit"]:
            logger.info(f"🎯 TP HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
            if self._maybe_extend_to_tp2(trade):
                return
            self._close_trade(trade["symbol"], "TAKE_PROFIT")
            return
        elif side == "SELL" and current_price <= trade["take_profit"]:
            logger.info(f"🎯 TP HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
            if self._maybe_extend_to_tp2(trade):
                return
            self._close_trade(trade["symbol"], "TAKE_PROFIT")
            return
        
        # Stop Loss check
        if side == "BUY" and current_price <= trade["stop_loss"]:
            logger.info(f"🛑 SL HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
            self._close_trade(trade["symbol"], "STOP_LOSS")
            return
        elif side == "SELL" and current_price >= trade["stop_loss"]:
            logger.info(f"🛑 SL HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
            self._close_trade(trade["symbol"], "STOP_LOSS")
            return
        
        # Trailing Stop
        activate_pct = self.config.get("TRAILING_STOP_ACTIVATE", 0.5) / 100
        trail_dist = self.config.get("TRAILING_STOP_DISTANCE", 0.3) / 100
        
        if side == "BUY":
            pnl = (current_price - trade["entry_price"]) / trade["entry_price"]
            
            if pnl >= activate_pct and not trade["trailing_activated"]:
                trade["trailing_activated"] = True
                logger.info(f"🔺 Trailing ON: {trade['symbol']} @ {pnl*100:.2f}%")
            
            if trade["trailing_activated"] and trade["highest_price"]:
                new_sl = trade["highest_price"] * (1 - trail_dist)
                if new_sl > trade["stop_loss"]:
                    self._update_trailing_stop_order(trade["symbol"], trade, new_sl)
        
        elif side == "SELL":
            pnl = (trade["entry_price"] - current_price) / trade["entry_price"]
            
            if pnl >= activate_pct and not trade["trailing_activated"]:
                trade["trailing_activated"] = True
                logger.info(f"🔻 Trailing ON: {trade['symbol']} @ {pnl*100:.2f}%")
            
            if trade["trailing_activated"] and trade["lowest_price"]:
                new_sl = trade["lowest_price"] * (1 + trail_dist)
                if new_sl < trade["stop_loss"]:
                    self._update_trailing_stop_order(trade["symbol"], trade, new_sl)
    
    def close_trade_manually(self, symbol: str, reason: str = "MANUAL_CLOSE"):
        """Close a trade (called from bot)"""
        self._close_trade(symbol, reason)

    def _place_exchange_close_order(self, symbol: str, trade: Dict) -> bool:
        """
        FIX (Critical Bug): actually close the position on Binance.
        Previously the bot only removed the trade from its own memory,
        leaving the real position open on the exchange with no SL/TP protection.
        Places a reduceOnly MARKET order in the opposite direction, with retries.
        """
        if not self.client:
            logger.warning(f"⚠️ No exchange client attached — cannot place real close order for {symbol}")
            return False

        close_side = "SELL" if trade["side"] == "BUY" else "BUY"
        quantity = trade["quantity"]

        # FIX (Hedge Mode bug): Binance rejects reduceOnly + positionSide
        # together, so on a Hedge Mode account send positionSide (which
        # identifies the position being closed) and NOT reduceOnly.
        position_side = trade.get("position_side") if self.hedge_mode else None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                order = self.client.new_order(
                    symbol=symbol,
                    side=close_side,
                    type="MARKET",
                    quantity=quantity,
                    reduceOnly=True,
                    positionSide=position_side
                )
                logger.info(f"✅ Exchange position closed: {symbol} {close_side} qty={quantity} "
                            f"(order_id={order.get('orderId', 'N/A')}, attempt {attempt}/{max_attempts})")
                return True
            except Exception as e:
                logger.error(f"❌ Close order attempt {attempt}/{max_attempts} failed for {symbol}: {e}")
                if attempt < max_attempts:
                    time.sleep(2)

        logger.critical(f"🚨 CRITICAL: FAILED to close {symbol} position on Binance after "
                         f"{max_attempts} attempts! The real exchange position may still be OPEN "
                         f"and unprotected. Manual intervention required immediately!")
        return False

    def _close_position_if_needed(self, symbol: str, trade: Dict) -> bool:
        """
        Check the REAL exchange position before closing. If an exchange-side
        STOP_MARKET/TAKE_PROFIT_MARKET order already closed it (e.g. it
        triggered a moment before the bot's own software check), there's
        nothing left to close and trying anyway would just fail/duplicate.
        """
        if not self.client:
            logger.warning(f"⚠️ No exchange client attached — cannot verify/close {symbol}")
            return False

        try:
            positions = self.client.position_risk(symbol=symbol)
            # FIX (Hedge Mode bug): a Hedge Mode account can return TWO
            # entries for the same symbol (one LONG, one SHORT). Picking
            # positions[0] blindly could check the wrong side. Match on
            # this trade's actual position_side when in Hedge Mode.
            position_side = trade.get("position_side")
            amt = 0.0
            for p in positions or []:
                if self.hedge_mode and position_side and p.get("positionSide") != position_side:
                    continue
                try:
                    amt = float(p.get("positionAmt", 0))
                except (TypeError, ValueError):
                    amt = 0.0
                break
            if abs(amt) < 1e-10:
                logger.info(f"ℹ️ {symbol} is already flat on the exchange "
                            f"(closed via a protective order already).")
                return True
        except Exception as e:
            logger.debug(f"Could not verify position for {symbol} before closing: {e}")

        return self._place_exchange_close_order(symbol, trade)

    def _close_trade(self, symbol: str, reason: str):
        """Close a trade: place the real exchange close order first, then record history"""
        with self.lock:
            trade = self.open_trades.get(symbol)
            if not trade:
                return
            # Snapshot values needed for the close order before releasing the lock,
            # so the network call below doesn't hold the lock.
            trade_snapshot = dict(trade)

        # Cancel any still-resting protective order (the one that didn't
        # trigger) so it can't fire unexpectedly after we close manually.
        self._cancel_protective_orders(symbol, trade_snapshot)

        exchange_closed = self._close_position_if_needed(symbol, trade_snapshot)

        with self.lock:
            if symbol not in self.open_trades:
                return
            trade = self.open_trades.pop(symbol)
            trade["close_time"] = datetime.now()
            trade["close_price"] = trade["current_price"]
            trade["close_reason"] = reason
            trade["status"] = "CLOSED"
            trade["exchange_closed"] = exchange_closed

            # Calculate final PnL
            if trade["side"] == "BUY":
                final_pnl = (trade["close_price"] - trade["entry_price"]) / trade["entry_price"]
            else:
                final_pnl = (trade["entry_price"] - trade["close_price"]) / trade["entry_price"]
            leverage = trade.get("leverage", 1)
            gross_pnl_percent = final_pnl * 100 * leverage

            # FIX (Real win-rate bug): pnl_percent used to be the raw ideal
            # entry/exit price move only, with no trading fees deducted. A
            # trade that barely moved in profit (e.g. +0.02%) was counted
            # as a "win" even though Binance's real entry+exit taker fees
            # would have made it a net loss. Subtract an estimated
            # round-trip fee (charged on both the entry and exit notional,
            # i.e. leverage x) so pnl_percent/win-rate reflects the REAL
            # net result. The pre-fee number is kept as pnl_percent_gross
            # for anyone who wants to see the raw price move too.
            fee_percent_per_side = self.config.get("TRADING_FEE_PERCENT", 0.05)
            fee_cost_percent = 2 * fee_percent_per_side * leverage
            net_pnl_percent = gross_pnl_percent - fee_cost_percent

            trade["pnl_percent_gross"] = gross_pnl_percent
            trade["pnl_percent"] = net_pnl_percent
            trade["fee_cost_percent"] = fee_cost_percent

            self.trade_history.append(trade)

        duration = (datetime.now() - trade["entry_time"]).seconds
        status_icon = "✅" if exchange_closed else "⚠️ (exchange close FAILED)"
        logger.info(f"🔒 TRADE CLOSED {status_icon}: {trade['side']} {symbol} | "
                   f"PnL: {trade['pnl_percent']:.2f}% | "
                   f"Duration: {duration}s | Reason: {reason}")

        self._save_state()
    
    def get_open_trades(self) -> Dict[str, Dict]:
        """Get all open trades"""
        with self.lock:
            return dict(self.open_trades)
    
    def get_open_trades_count(self) -> int:
        """Get open trades count"""
        with self.lock:
            return len(self.open_trades)
    
    def get_total_pnl(self) -> float:
        """Get total PnL of open trades"""
        total = 0.0
        with self.lock:
            for trade in self.open_trades.values():
                total += trade.get("pnl_amount", 0.0)
        return total
    
    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        """Get trade history"""
        with self.lock:
            return self.trade_history[-limit:]
    
    def calculate_dynamic_leverage(self, symbol: str, volatility: float) -> int:
        """Calculate optimal leverage based on volatility"""
        base_leverage = self.config.get("MAX_LEVERAGE", 5)
        
        if volatility > 0.05:
            return max(1, base_leverage - 3)
        elif volatility > 0.03:
            return max(1, base_leverage - 1)
        elif volatility > 0.015:
            return base_leverage
        else:
            return min(10, base_leverage + 2)
