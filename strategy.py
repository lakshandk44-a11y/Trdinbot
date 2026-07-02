def smc(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0

    # Break of structure
    if last["c"] > prev["h"]:
        score += 2
    if last["c"] < prev["l"]:
        score -= 2

    # Strong candle filter (avoid weak noise)
    body = abs(last["c"] - last["o"])
    candle_range = last["h"] - last["l"]

    if candle_range > 0 and body / candle_range > 0.6:
        score += 1

    return score


def trend(df):
    if df.iloc[-1]["c"] > df.iloc[-20]["c"]:
        return 1
    return -1
