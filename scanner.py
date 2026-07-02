import random
from config import COINS, TIMEFRAMES, MIN_SCORE

class Scanner:

    def __init__(self):
        pass

    def get_market_data(self, coin, timeframe):
        # fake market data (paper system)
        return {
            "coin": coin,
            "timeframe": timeframe,
            "trend": random.choice(["UP", "DOWN", "SIDE"]),
            "volume": random.uniform(1, 100),
            "volatility": random.uniform(0.1, 5)
        }

    def analyze_coin(self, coin):
        score = 0
        signals = []

        for tf in TIMEFRAMES:
            data = self.get_market_data(coin, tf)

            if data["trend"] == "UP":
                score += 2
                signals.append(f"{tf}: Bullish")
            elif data["trend"] == "DOWN":
                score += 2
                signals.append(f"{tf}: Bearish")

            if data["volume"] > 50:
                score += 1

        return {
            "coin": coin,
            "score": score,
            "signals": signals
        }

    def scan_all(self):
        results = []

        for coin in COINS:
            result = self.analyze_coin(coin)

            if result["score"] >= MIN_SCORE:
                results.append(result)

        return results
