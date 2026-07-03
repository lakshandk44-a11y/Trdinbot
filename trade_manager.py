"""
HackerAI Auto Trading Bot - Trade Manager
Manages all open trades 24/7
"""

import logging
import threading
import time
from typing import Dict, List, Optional
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

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
    
    def _monitor_loop(self):
        """Monitor all trades every 5 seconds"""
        while self.running:
            try:
                with self.lock:
                    trades_copy = dict(self.open_trades)
                
                for symbol, trade in trades_copy.items():
                    self._evaluate_trade(trade)
                    
                time.sleep(5)
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                time.sleep(10)
    
    def calculate_position_size(self, balance: float, entry_price: float, 
                                stop_loss_price: float, leverage: int) -> float:
        """Calculate position size with risk management"""
        risk_percent = self.config.get("RISK_PER_TRADE", 0.02)
        risk_amount = balance * risk_percent
        
        price_risk = abs(entry_price - stop_loss_price)
        if price_risk == 0:
            price_risk = entry_price * 0.01
        
        position_value = risk_amount / (price_risk / entry_price)
        quantity = position_value / entry_price
        
        max_leverage = self.config.get("MAX_LEVERAGE", leverage)
        leverage = min(leverage, max_leverage)
        
        quantity_with_leverage = quantity * leverage
        
        return quantity_with_leverage
    
    def open_new_trade(self, symbol: str, side: str, entry_price: float, 
                       quantity: float, leverage: int, 
                       analysis_result: Dict) -> Optional[Dict]:
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
        
        if side.upper() == "BUY":
            take_profit = entry_price * (1 + tp_percent)
            stop_loss = entry_price * (1 - sl_percent)
        else:
            take_profit = entry_price * (1 - tp_percent)
            stop_loss = entry_price * (1 + sl_percent)
        
        trade = {
            "symbol": symbol,
            "side": side.upper(),
            "entry_price": entry_price,
            "quantity": quantity,
            "leverage": leverage,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
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
            "binance_order_id": None
        }
        
        with self.lock:
            self.open_trades[symbol] = trade
        
        logger.info(f"✅ NEW TRADE: {side} {symbol} @ {entry_price:.8f} | "
                   f"Qty: {quantity} | Lev: {leverage}x | "
                   f"TP: {take_profit:.8f} | SL: {stop_loss:.8f}")
        
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
    
    def _evaluate_trade(self, trade: Dict):
        """Evaluate trade for SL/TP/trailing"""
        if trade["status"] != "OPEN":
            return
            
        current_price = trade["current_price"]
        side = trade["side"]
        
        # Take Profit check
        if side == "BUY" and current_price >= trade["take_profit"]:
            logger.info(f"🎯 TP HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
            self._close_trade(trade["symbol"], "TAKE_PROFIT")
            return
        elif side == "SELL" and current_price <= trade["take_profit"]:
            logger.info(f"🎯 TP HIT: {trade['symbol']} @ {current_price:.8f} (PnL: {trade['pnl_percent']:.2f}%)")
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
                    trade["stop_loss"] = new_sl
        
        elif side == "SELL":
            pnl = (trade["entry_price"] - current_price) / trade["entry_price"]
            
            if pnl >= activate_pct and not trade["trailing_activated"]:
                trade["trailing_activated"] = True
                logger.info(f"🔻 Trailing ON: {trade['symbol']} @ {pnl*100:.2f}%")
            
            if trade["trailing_activated"] and trade["lowest_price"]:
                new_sl = trade["lowest_price"] * (1 + trail_dist)
                if new_sl < trade["stop_loss"]:
                    trade["stop_loss"] = new_sl
    
    def close_trade_manually(self, symbol: str, reason: str = "MANUAL_CLOSE"):
        """Close a trade (called from bot)"""
        self._close_trade(symbol, reason)
    
    def _close_trade(self, symbol: str, reason: str):
        """Close a trade and record history"""
        with self.lock:
            if symbol not in self.open_trades:
                return
                
            trade = self.open_trades.pop(symbol)
            trade["close_time"] = datetime.now()
            trade["close_price"] = trade["current_price"]
            trade["close_reason"] = reason
            trade["status"] = "CLOSED"
            
            # Calculate final PnL
            if trade["side"] == "BUY":
                final_pnl = (trade["close_price"] - trade["entry_price"]) / trade["entry_price"]
            else:
                final_pnl = (trade["entry_price"] - trade["close_price"]) / trade["entry_price"]
            trade["pnl_percent"] = final_pnl * 100 * trade.get("leverage", 1)
            
            self.trade_history.append(trade)
        
        duration = (datetime.now() - trade["entry_time"]).seconds
        logger.info(f"🔒 TRADE CLOSED: {trade['side']} {symbol} | "
                   f"PnL: {trade['pnl_percent']:.2f}% | "
                   f"Duration: {duration}s | Reason: {reason}")
    
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
