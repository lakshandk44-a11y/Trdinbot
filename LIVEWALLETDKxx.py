from flask import Flask, jsonify
from flask_cors import CORS
import requests
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app) # බ්‍රවුසර් එකේ CORS Error එක මඟහැරීමට

# -------------------------------------------------------------
# 🛠️ මෙතනට ඔයාගේ BINANCE API KEYS ඇතුලත් කරන්න
API_KEY = "GoVLfuUF8lLeA1zDmXfnEzLdrJ1IH49o5u42q3huPJIUE9ibASb2j9JlIAZc88P5"
SECRET_KEY = "ea8hje3mgr8wwYHaLzp95aEFo4btmg2MkKYbs9F2BNqTDedECTsTxpuZ6NHvfKWb"

# 🌐 REAL එකට දාද්දි පල්ලෙහා BASE_URL එක මාරු කරන්න:
# ටෙස්ට්නෙට් එකට: "https://testnet.binancefuture.com"
# රියල් එකවුන්ට් එකට: "https://fapi.binance.com"
BASE_URL = "https://testnet.binancefuture.com" 
# -------------------------------------------------------------

def generate_signature(params):
    query_string = urllib.parse.urlencode(params)
    return hmac.new(SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

@app.route('/api/bot_data')
def get_bot_data():
    timestamp = int(time.time() * 1000)
    headers = {'X-MBX-APIKEY': API_KEY}
    
    # 1. ලබාගැනීම: WALLET BALANCE & OPEN POSITIONS
    account_params = {'timestamp': timestamp}
    account_params['signature'] = generate_signature(account_params)
    
    try:
        acc_res = requests.get(f"{BASE_URL}/fapi/v2/account", headers=headers, params=account_params).json()
        
        # Wallet Balance එක වෙන් කර ගැනීම
        balance = 0.0
        if 'assets' in acc_res:
            for asset in acc_res['assets']:
                if asset['asset'] == 'USDT':
                    balance = float(asset['walletBalance'])
                    break
                    
        # දැනට ඔපන් වී ඇති ට්‍රේඩ්ස් (Active Positions) වෙන් කර ගැනීම
        open_trades = []
        if 'positions' in acc_res:
            for pos in acc_res['positions']:
                amt = float(pos['positionAmt'])
                if amt != 0: # 0 නොවන ඒවා පමණක් සක්‍රීය ට්‍රේඩ් වේ
                    open_trades.append({
                        'symbol': pos['symbol'],
                        'type': 'LONG' if amt > 0 else 'SHORT',
                        'entryPrice': float(pos['entryPrice']),
                        'pnl': float(pos['unrealizedProfit']),
                        'qty': abs(amt)
                    })
        
        # 2. ලබාගැනීම: අද දවසේ සිදුවූ CLOSED TRADES (Realized PnL)
        # UTC ලෝක වේලාවෙන් අද දවසේ ආරම්භක මිලිසෙකන්ඩ් ගණන (Midnight Reset එක සඳහා)
        start_of_today = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        
        income_params = {
            'incomeType': 'REALIZED_PNL',
            'startTime': start_of_today,
            'timestamp': timestamp,
            'limit': 1000
        }
        income_params['signature'] = generate_signature(income_params)
        income_res = requests.get(f"{BASE_URL}/fapi/v1/income", headers=headers, params=income_params).json()
        
        profit_trades = []
        loss_trades = []
        
        if isinstance(income_res, list):
            for inc in income_res:
                pnl_val = float(inc['income'])
                # කොමිස් ගැස්සීම් හැර සැබෑ ට්‍රේඩ් ප්‍රොෆිට්/ලොස් පමණක් ගැනීම
                if pnl_val != 0: 
                    trade_info = {
                        'symbol': inc['symbol'],
                        'amount': abs(pnl_val)
                    }
                    if pnl_val > 0:
                        profit_trades.append(trade_info)
                    else:
                        loss_trades.append(trade_info)
                        
        return jsonify({
            'balance': balance,
            'openTrades': open_trades,
            'profitTrades': profit_trades,
            'lossTrades': loss_trades
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("[DK-BOT] Proxy Server Running on http://localhost:5000")
    app.run(port=5000, debug=False)
