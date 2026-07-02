import ccxt
import pandas as pd
import requests
from config import *

exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
    "options": {"defaultType": "future"}
})

def send(msg):
    requests.post(DISCORD_WEBHOOK, json={"content": msg})


def get_data(symbol, tf):
    data = exchange.fetch_ohlcv(symbol, tf, limit=100)
    return pd.DataFrame(data, columns=["t","o","h","l","c","v"])


def trade(symbol, side, size):
    return exchange.create_market_order(symbol, side.lower(), size)
