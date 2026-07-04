"""
HackerAI Auto Trading Bot - Core Bot Logic
24/7 Scanning | Balance Crash Protection | 65% Profit Filter
"""

import logging
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

from binance.client import Client
from binance.exceptions import BinanceAPIException
from decimal import Decimal, ROUND_DOWN

from config import *
from analysis_engine import AnalysisEngine
from trade_manager import TradeManager

logger = logging.getLogger(__name__)

class HackerAIBot:
    """
    Main bot class - 24/7 operation
    - Balance crash handling (waits for funds)
    - 5/3 tools rule
    - 65%+ profit chance filter
    - Unlimited concurrent trades
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.running = False
        self.paused = False
        self.waiting_for_balance = False  # Balance crash state
        
        # Initialize Binance client
        self.client = Client(
            config["BINANCE_API_KEY"],
            config["BINANCE_API_SECRET"],
            testnet=config.get("BINANCE_TESTNET", False)
        )
        
        # Initialize engines
        self.analysis_engine = AnalysisEngine(config)
        self.trade_manager = TradeManager(config, self.client)
        
        # State
        self.balance = 0.0
        self.account_info = {}
        self.scanned_coins = set()
        self.analysis_cache = {}
        self.scan_count = 0
        self.consecutive_balance_errors = 0
        self.last_trade_time = {}
        
        logger.info("🤖 HackerAI Bot initialized (24/7 Mode)")
    
    def start(self):
        """Start the bot (24/7)"""
        if self.running:
            logger.warning("⚠️ Bot is already running")
            return
            
        self.running = True
        self.paused = False
        
        logger.info("🚀 HackerAI Bot STARTING in 24/7 mode...")
        
        # Start trade monitoring
        self.trade_manager.start_monitoring()
        
        # Run main loop
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
                
                # Step 1: Update account info with crash protection
                if not self._update_account_info_with_retry():
                    # Balance is 0 or error - wait and retry
                    time.sleep(self.config.get("BALANCE_CHECK_INTERVAL", 60))
                    continue
                
                # Step 2: Get top 40 coins
                coins_to_scan = self._get_top_coins()
                
                # Step 3: Scan all coins 24/7
                self._scan_coins_247(coins_to_scan)
                
                # Step 4: Update and manage open trades
                self._update_open_trades()
                
                # Step 5: Check reversals
                self._check_trade_reversals()
                
                # Log status
                if self.scan_count % 10 == 0:
                    self._log_status()
                
                # 24/7 scanning - short interval
                scan_interval = self.config.get("SCAN_INTERVAL_SECONDS", 30)
                time.sleep(scan_interval)
                
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(60)
    
    def _update_account_info_with_retry(self) -> bool:
        """
        Update account with balance crash protection.
        If balance is 0, don't crash - wait for funds.
        Returns True if balance OK, False if no balance.
        """
        try:
            account = self.client.futures_account()
            self.account_info = account
            
            for asset in account.get("assets", []):
                if asset["asset"] == "USDT":
                    self.balance = float(asset["walletBalance"])
                    break
            
            # Reset error counter on success
            self.consecutive_balance_errors = 0
            self.waiting_for_balance = False
            
            if self.balance <= 0:
                logger.warning("⚠️ Balance is 0 or negative! Waiting for funds...")
                self.waiting_for_balance = True
                return False
            
            if self.waiting_for_balance:
                logger.info(f"✅ Funds detected! Balance: {self.balance:.2f} USDT. Resuming trading.")
                self.waiting_for_balance = False
            
            return True
            
        except BinanceAPIException as e:
            self.consecutive_balance_errors += 1
            
            if self.consecutive_balance_errors >= 3:
                logger.warning(f"⚠️ Balance fetch failed {self.consecutive_balance_errors}x. Waiting...")
                self.waiting_for_balance = True
            
            logger.error(f"Binance API error (account): {e}")
            return False
            
        except Exception as e:
            logger.error(f"Account update error: {e}")
            return False
    
    def _get_top_coins(self) -> List[str]:
        """Get top 40 coins from Binance"""
        coins = self.config.get("TOP_40_COINS", TOP_40_COINS)
        
        try:
            tickers = self.client.futures_ticker()
            usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
            sorted_by_volume = sorted(usdt_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
            top_40 = [t["symbol"] for t in sorted_by_volume[:40]]
            
            if top_40:
                coins = top_40
        except:
            pass
        
        return coins
    
    def _scan_coins_247(self, coins: List[str]):
        """
        24/7 scanning of top 40 coins on 3 timeframes
        Trade entry if: 5/3 tools agree AND profit chance >= 65%
        """
        self.scan_count += 1
        
        if self.scan_count % 5 == 0:
            logger.info(f"🔍 [24/7] Scan #{self.scan_count}: {len(coins)} coins, {len(TIMEFRAMES)} TFs")
        
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
                
                # Cache for reversal checking
                self.analysis_cache[symbol] = {
                    "analysis": analysis,
                    "timestamp": datetime.now()
                }
                
                # If already in trade, just update price
                if already_in_trade:
                    lower_tf = ohlc_data.get("lower")
                    if lower_tf is not None and len(lower_tf) > 0:
                        current_price = lower_tf["close"].iloc[-1]
                        self.trade_manager.update_trade_price(symbol, current_price)
                    continue
                
                # Check if trade criteria met
                final = analysis.get("final_signal", {})
                decision = final.get("decision", "HOLD")
                
                if decision in ["BUY", "SELL"]:
                    # Check 5/3 tools rule AND profit chance >= 65%
                    tools_agreeing = final.get("tools_agreeing", 0)
                    min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
                    profit_chance = final.get("profit_chance", 0.0)
                    min_chance = self.config.get("MIN_PROFIT_CHANCE", 65.0)
                    
                    if tools_agreeing >= min_tools and profit_chance >= min_chance:
                        self._execute_trade(symbol, decision, final, ohlc_data)
                    else:
                        if self.scan_count % 20 == 0:
                            logger.debug(f"⏭️ {symbol}: {decision} skipped "
                                       f"(tools: {tools_agreeing}/{min_tools}, "
                                       f"profit: {profit_chance:.1f}%/{min_chance}%)")
                    
            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")
                continue
    
    def _fetch_multi_timeframe(self, symbol: str) -> Optional[Dict[str, pd.DataFrame]]:
        """Fetch OHLCV for 3 timeframes"""
        result = {}
        
        try:
            for tf_name, tf_interval in TIMEFRAMES.items():
                if tf_interval == "4h":
                    limit = 100
                elif tf_interval == "1h":
                    limit = 150
                else:
                    limit = 200
                
                try:
                    klines = self.client.futures_klines(
                        symbol=symbol, interval=tf_interval, limit=limit
                    )
                except BinanceAPIException:
                    klines = self.client.get_klines(
                        symbol=symbol, interval=tf_interval, limit=limit
                    )
                
                if klines:
                    df = pd.DataFrame(klines, columns=[
                        "timestamp", "open", "high", "low", "close", "volume",
                        "close_time", "quote_asset_volume", "trades",
                        "taker_buy_base", "taker_buy_quote", "ignore"
                    ])
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    result[tf_name] = df
                else:
                    result[tf_name] = None
                    
        except Exception as e:
            logger.error(f"Failed to fetch data for {symbol}: {e}")
            return None
        
        if "higher" not in result or result["higher"] is None:
            return None
            
        return result
    
def _execute_trade(self, symbol, decision, signal, ohlc_data):
        """
        🔥 Auto-Decision: Checks coin's min trade, leverage, and balance.
        If it can trade with exactly 5% balance at available leverage → TRADES.
        Otherwise → SKIPS cleanly. Never crashes.
        """
        direction = signal.get("direction", 0)
        if direction == 0:
            return

        # ─── 1. Get coin-specific exchange info (minNotional, maxLeverage) ───
        coin_min_notional = 10.0   # Fallback
        coin_max_leverage = 20     # Fallback
        try:
            info = self.client.futures_exchange_info()
            for sym in info.get("symbols", []):
                if sym["symbol"] == symbol:
                    for f in sym.get("filters", []):
                        if f["filterType"] == "MIN_NOTIONAL":
                            coin_min_notional = float(f.get("minNotional", f.get("notional", 10.0)))
                        if f["filterType"] == "LOT_SIZE":
                            coin_max_leverage = int(sym.get("leverageBrackets", [{}])[0].get("initialLeverage", 20))
                            if not sym.get("leverageBrackets"):
                                # Direct from symbol filters
                                pass
                    # Read leverage brackets
                    brackets = sym.get("leverageBrackets", [])
                    if brackets:
                        coin_max_leverage = int(brackets[0].get("initialLeverage", 20))
                    break
        except Exception as e:
            logger.debug(f"Could not fetch exchange info for {symbol}: {e}")

        # ─── 2. Calculate exact margin (5% of balance) ───
        balance_pct = self.config.get("BALANCE_PERCENTAGE", 5) / 100.0
        margin = self.balance * balance_pct

        # ─── 3. Get config max leverage ───
        config_max_lev = self.config.get("MAX_LEVERAGE", 5)
        # Actual max leverage the bot will consider
        effective_max_lev = min(coin_max_leverage, config_max_lev)

        # ─── 4. Calculate position value at max possible leverage ───
        max_position = margin * effective_max_lev

        # ─── 5. AUTO DECISION ───
        logger.info(f"🔍 {symbol}: margin=${margin:.4f}, "
                    f"minNotional=${coin_min_notional:.2f}, "
                    f"maxLev={coin_max_leverage}x, "
                    f"configLev={config_max_lev}x, "
                    f"effectiveLev={effective_max_lev}x, "
                    f"maxPos=${max_position:.2f}")

        if max_position < coin_min_notional:
            logger.info(f"⏭️ {symbol}: Even {effective_max_lev}x gives ${max_position:.2f} < ${coin_min_notional:.2f} min. "
                        f"Skipping. Need ~${coin_min_notional / (effective_max_lev or 1):.2f} margin (5% of ${(coin_min_notional / (effective_max_lev or 1)) / balance_pct:.2f} balance).")
            return  # ✅ Clean skip, NOT a crash

        # ─── 6. Calculate the EXACT leverage needed ───
        # We want: margin * leverage = coin_min_notional (or slightly above)
        optimal_leverage = coin_min_notional / margin
        optimal_leverage = int(optimal_leverage) + (1 if optimal_leverage % 1 > 0 else 0)
        # Cap at effective max
        trade_leverage = min(optimal_leverage, effective_max_lev)

        # Also consider volatility safety
        volatility = self._calculate_volatility(ohlc_data)
        vol_leverage = self.trade_manager.calculate_dynamic_leverage(symbol, volatility)
        # Final leverage: lowest of all
        final_leverage = min(trade_leverage, vol_leverage)
        final_leverage = max(1, final_leverage)  # At least 1x

        # Recalculate final position
        final_position = margin * final_leverage

        logger.info(f"✅ {symbol}: TRADE POSSIBLE! "
                    f"Margin=${margin:.2f}, Leverage={final_leverage}x, "
                    f"Position=${final_position:.2f} (min=${coin_min_notional:.2f})")

        # ─── 7. Get current price ───
        lower_tf = ohlc_data.get("lower") or ohlc_data.get("medium") or ohlc_data.get("higher")
        if lower_tf is None or len(lower_tf) == 0:
            return
        current_price = float(lower_tf["close"].iloc[-1])

        # ─── 8. Set leverage on Binance ───
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=final_leverage)
            logger.info(f"⚙️ Leverage set: {symbol} = {final_leverage}x")
        except Exception as e:
            logger.warning(f"Leverage change warning for {symbol}: {e}")

        # ─── 9. Calculate position size ───
        sl_percent = self.config.get("STOP_LOSS_PERCENT", 1.0) / 100.0
        sl_price = current_price * (1 - sl_percent) if decision == "BUY" else current_price * (1 + sl_percent)

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

        # ─── 10. Rate limit ───
        now = time.time()
        if symbol in self.last_trade_time and now - self.last_trade_time[symbol] < 5:
            return
        self.last_trade_time[symbol] = now

        # ─── 11. PLACE ORDER ───
        try:
            side = "BUY" if decision == "BUY" else "SELL"
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity,
                reduceOnly=False
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

        except BinanceAPIException as e:
            logger.error(f"❌ Order failed for {symbol}: {e.message}")
        except Exception as e:
            logger.error(f"❌ Order error for {symbol}: {e}")
    
    def _calculate_volatility(self, ohlc_data: Dict) -> float:
        """Calculate volatility from higher timeframe"""
        try:
            higher_tf = ohlc_data.get("higher")
            if higher_tf is not None and len(higher_tf) > 10:
                closes = higher_tf["close"].values[-20:]
                returns = np.diff(closes) / closes[:-1]
                volatility = np.std(returns)
                return abs(volatility)
        except:
            pass
        return 0.02
    
    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity to exchange step size"""
        try:
            info = self.client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s.get("filters", []):
                        if f["filterType"] == "LOT_SIZE":
                            step_size = float(f["stepSize"])
                            precision = len(str(step_size).split(".")[-1]) if "." in str(step_size) else 8
                            from decimal import Decimal, ROUND_DOWN
                            quantity = float(Decimal(str(quantity)).quantize(
                                Decimal(str(step_size)), rounding=ROUND_DOWN
                            ))
                            return quantity
        except:
            pass
        return quantity
    
    def _update_open_trades(self):
        """Update prices for open trades"""
        open_trades = self.trade_manager.get_open_trades()
        for symbol in open_trades:
            try:
                ticker = self.client.futures_symbol_ticker(symbol=symbol)
                current_price = float(ticker["price"])
                self.trade_manager.update_trade_price(symbol, current_price)
            except Exception as e:
                logger.debug(f"Price update error for {symbol}: {e}")
    
    def _check_trade_reversals(self):
        """Check if any open trade should close due to reversal"""
        open_trades = self.trade_manager.get_open_trades()
        
        for symbol, trade in open_trades.items():
            cached = self.analysis_cache.get(symbol)
            if not cached:
                continue
            
            analysis = cached.get("analysis", {})
            final = analysis.get("final_signal", {})
            current_direction = final.get("direction", 0)
            
            if trade["side"] == "BUY" and current_direction == -1:
                tools = final.get("tools_agreeing", 0)
                min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
                if tools >= min_tools:
                    logger.info(f"🔄 Reversal: Closing BUY {symbol} (SELL signal, {tools}/{min_tools} tools)")
                    self.trade_manager.close_trade_manually(symbol, "REVERSAL_SIGNAL")
            
            elif trade["side"] == "SELL" and current_direction == 1:
                tools = final.get("tools_agreeing", 0)
                min_tools = self.config.get("MIN_TOOLS_MATCH", 3)
                if tools >= min_tools:
                    logger.info(f"🔄 Reversal: Closing SELL {symbol} (BUY signal, {tools}/{min_tools} tools)")
                    self.trade_manager.close_trade_manually(symbol, "REVERSAL_SIGNAL")
    
    def _log_status(self):
        """Log bot status"""
        open_trades = self.trade_manager.get_open_trades()
        total_pnl = self.trade_manager.get_total_pnl()
        history = self.trade_manager.get_trade_history(5)
        
        closed_count = len(self.trade_manager.trade_history)
        wins = sum(1 for t in self.trade_manager.trade_history if t.get("pnl_percent", 0) > 0)
        win_rate = (wins / closed_count * 100) if closed_count > 0 else 0
        
        logger.info(f"═══════════════════════════════════════")
        logger.info(f"📊 [24/7] Balance: ${self.balance:.2f} | "
                   f"Trades: {len(open_trades)} open | "
                   f"Total PnL: ${total_pnl:.2f} | "
                   f"Win Rate: {win_rate:.1f}% ({wins}/{closed_count})")
        
        if self.waiting_for_balance:
            logger.info(f"⏳ Waiting for funds to be deposited...")
        
        if open_trades:
            for sym, trade in open_trades.items():
                logger.info(f"   {sym}: {trade['side']} | "
                          f"PnL: {trade['pnl_percent']:.2f}% | "
                          f"Entry: {trade['entry_price']:.8f}")
        
        if history:
            recent = history[-3:]
            for t in recent:
                logger.info(f"   📋 Closed: {t['symbol']} {t['side']} | "
                          f"PnL: {t['pnl_percent']:.2f}% | "
                          f"Reason: {t.get('close_reason', 'N/A')}")
        
        logger.info(f"═══════════════════════════════════════")
    
    def _log_trade(self, trade_info: Dict):
        """Log trade to CSV"""
        try:
            import csv
            file_exists = False
            try:
                with open("trades_log.csv", "r") as f:
                    file_exists = True
            except:
                pass
            
            with open("trades_log.csv", "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["timestamp", "type", "symbol", "side", "price", 
                                   "quantity", "leverage", "tools_agreeing", "profit_chance"])
                
                writer.writerow([
                    datetime.now().isoformat(),
                    trade_info.get("type"),
                    trade_info.get("symbol"),
                    trade_info.get("side"),
                    trade_info.get("price"),
                    trade_info.get("quantity"),
                    trade_info.get("leverage"),
                    trade_info.get("tools_agreeing", 0),
                    trade_info.get("profit_chance", 0)
                ])
        except Exception as e:
            logger.debug(f"CSV log error: {e}")
