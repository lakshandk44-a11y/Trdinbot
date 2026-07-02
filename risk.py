import time
from config import MAX_TRADES_PER_HOUR

trade_log = []
hour_start = time.time()

def can_trade():
    global trade_log, hour_start

    if time.time() - hour_start > 3600:
        trade_log = []
        hour_start = time.time()

    return len(trade_log) < MAX_TRADES_PER_HOUR


def add_trade():
    trade_log.append(time.time())
