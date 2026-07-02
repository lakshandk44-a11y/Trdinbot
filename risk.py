import time
from config import TIMEFRAMES

trade_log = []
start_hour = time.time()

def can_trade():
    global trade_log, start_hour

    if time.time() - start_hour > 3600:
        trade_log = []
        start_hour = time.time()

    return len(trade_log) < 3


def add_trade():
    trade_log.append(time.time())
