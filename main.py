import time
from config import *
from trader import *
from strategy import *
from news import news_score
from risk import can_trade, add_trade
from controller import STATE

def analyze(symbol):

    score = 0

    for tf in TIMEFRAMES:
        df = get_data(symbol, tf)

        score += smc(df)
        score += trend(df)

    score += news_score(symbol)

    return score


def run():

    send("🔥 ULTRA BOT STARTED")

    while True:

        if not STATE["run"]:
            time.sleep(3)
            continue

        if not can_trade():
            time.sleep(10)
            continue

        best = None
        best_score = -999

        for c in COINS:

            score = analyze(c)

            if score > best_score:
                best_score = score
                best = c

        df = get_data(best, "15m")
        price = df.iloc[-1]["c"]

        if best_score >= 5:
            trade(best, "buy", STATE["trade_size"])
            add_trade()

        elif best_score <= -4:
            trade(best, "sell", STATE["trade_size"])
            add_trade()

        time.sleep(40)


if __name__ == "__main__":
    run()
