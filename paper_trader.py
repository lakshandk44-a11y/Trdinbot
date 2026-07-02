from config import INITIAL_BALANCE
import random

class PaperTrader:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.trades = []

    def open_trade(self, coin, direction, amount):
        entry = random.uniform(100, 500)

        trade = {
            "coin": coin,
            "direction": direction,
            "entry": entry,
            "amount": amount,
            "status": "OPEN"
        }

        self.trades.append(trade)
        return trade

    def close_trade(self, trade):
        exit_price = trade["entry"] * random.uniform(0.97, 1.05)

        profit = (exit_price - trade["entry"]) * trade["amount"]

        if trade["direction"] == "SELL":
            profit *= -1

        self.balance += profit
        trade["status"] = "CLOSED"
        trade["profit"] = profit

        return trade
