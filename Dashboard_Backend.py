"""
Live Trade Dashboard - Backend

FIX (rate-limit-free dashboard): the original version of this file called
Binance's REST API directly (account + income endpoints) every time the
HTML page requested data. Every request competed with the main bot for the
SAME IP-based Binance rate limit budget - exactly the kind of traffic that
caused the -1003 "Too many requests" error seen earlier.

This version makes ZERO Binance API calls. It only reads two small local
files the main bot (bot_core.py / trade_manager.py) already writes for its
own purposes:
  - trade_state.json  -> open_trades, trade_history (written by TradeManager)
  - live_status.json  -> balance, total_pnl, win_rate (written by bot_core._log_status)

Since the bot is already fetching/maintaining this data for its own
trading logic, reading it from disk costs the bot nothing extra and adds
no additional Binance API traffic at all - however often the dashboard is
refreshed.

No real Binance API keys are needed here at all.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import json
import os
import secrets
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# These must point at the SAME directory the main bot (main.py) runs from,
# since that's where it writes these files.
TRADE_STATE_FILE = "trade_state.json"
LIVE_STATUS_FILE = "live_status.json"

# FIX (public-access security): this server now binds to 0.0.0.0 so any
# phone on any network can reach it (per explicit request), not just the
# VPS itself. Exposing live balance/PnL/trade data on an open port with no
# protection at all would let anyone who finds the IP:port see everything.
# A simple shared-secret token is required on every request instead.
#
# Set this via environment variable before starting:
#   export DASHBOARD_TOKEN="something-long-and-random"
# If not set, a random one is generated at startup and printed once to the
# console (check the terminal/pm2 log immediately after starting) - the
# server refuses to run with a blank/guessable token.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
if not os.getenv("DASHBOARD_TOKEN"):
    print(f"[DK-BOT] No DASHBOARD_TOKEN set - generated one for this session:")
    print(f"[DK-BOT]   {DASHBOARD_TOKEN}")
    print(f"[DK-BOT] Add ?token={DASHBOARD_TOKEN} to the URL on your phone, or set "
          f"DASHBOARD_TOKEN as an env var to keep the same token across restarts.")


def _read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        # File could be mid-write (rare, since both writers use atomic
        # tmp+rename) - just treat as "not available yet" rather than
        # crashing the whole endpoint over one bad read.
        return None


def _is_today_utc(iso_timestamp: str) -> bool:
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()
    except Exception:
        return False


@app.route('/api/bot_data')
def get_bot_data():
    # FIX (public-access security): reject any request that doesn't
    # present the correct token, before touching any trade data.
    if request.args.get("token") != DASHBOARD_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    state = _read_json(TRADE_STATE_FILE) or {}
    status = _read_json(LIVE_STATUS_FILE) or {}

    # ---- Open trades (from trade_state.json, written by TradeManager) ----
    # Field names/shape here match the ORIGINAL Binance-direct backend's
    # output exactly (symbol/type/entryPrice/qty/pnl in USDT), so the
    # existing HTML dashboard works with zero changes to its JS beyond
    # adding ?token=... to the fetch URL.
    open_trades = []
    for symbol, t in state.get("open_trades", {}).items():
        entry_price = t.get("entry_price", 0)
        current_price = t.get("current_price", entry_price)
        quantity = t.get("quantity", 0)
        side = t.get("side", "BUY")
        # Dollar PnL = quantity * price move (leverage doesn't multiply this
        # again - see earlier conversation: leverage affects margin
        # required, not the $ P&L for a given quantity/price move).
        dollar_pnl = quantity * (current_price - entry_price) if side == "BUY" \
            else quantity * (entry_price - current_price)
        open_trades.append({
            "symbol": symbol,
            "type": "LONG" if side == "BUY" else "SHORT",
            "entryPrice": entry_price,
            "qty": quantity,
            "pnl": dollar_pnl,
        })

    # ---- Today's closed trades (from trade_history, last 200 kept) ----
    today_trades = [
        t for t in state.get("trade_history", [])
        if t.get("close_time") and _is_today_utc(t["close_time"])
    ]

    def _closed_dollar_pnl(t):
        entry_price = t.get("entry_price", 0)
        close_price = t.get("close_price", entry_price)
        quantity = t.get("quantity", 0)
        side = t.get("side", "BUY")
        # Gross $ PnL (matches quantity*price-move convention above; note
        # this is pre-fee, unlike pnl_percent which already has the
        # estimated round-trip fee subtracted - shown here as an
        # approximation of realized USDT gain/loss for display purposes).
        return quantity * (close_price - entry_price) if side == "BUY" \
            else quantity * (entry_price - close_price)

    profit_trades = [
        {"symbol": t.get("symbol"), "amount": round(_closed_dollar_pnl(t), 4)}
        for t in today_trades if t.get("pnl_percent", 0) > 0
    ]
    loss_trades = [
        {"symbol": t.get("symbol"), "amount": round(abs(_closed_dollar_pnl(t)), 4)}
        for t in today_trades if t.get("pnl_percent", 0) <= 0
    ]

    return jsonify({
        "balance": status.get("balance", 0),
        "openTrades": open_trades,
        "profitTrades": profit_trades,
        "lossTrades": loss_trades,
    })


if __name__ == '__main__':
    print("[DK-BOT] Local-file dashboard backend running on http://0.0.0.0:5000")
    print("(Reads trade_state.json + live_status.json only - no Binance API calls)")
    app.run(host='0.0.0.0', port=5000, debug=False)
