import pandas_ta as ta
import pandas as pd

class TradingStrategy:
    @staticmethod
    def analyze(df, news_sentiment):
        df['RSI'] = ta.rsi(df['close'], length=14)
        df['EMA_200'] = ta.ema(df['close'], length=200)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        
        if df['close'].iloc[-1] > df['EMA_200'].iloc[-1] and news_sentiment > 0.5:
            return "BUY"
        elif df['close'].iloc[-1] < df['EMA_200'].iloc[-1] and news_sentiment < -0.5:
            return "SELL"
        return "WAIT"

    @staticmethod
    def get_risk_params(df, balance):
        atr = df['ATR'].iloc[-1]
        size = (balance * 0.05) / atr
        return size
