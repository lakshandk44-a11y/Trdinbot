import feedparser

def news_score(symbol):
    feed = feedparser.parse("https://cointelegraph.com/rss")

    score = 0

    for n in feed.entries[:10]:
        t = n.title.lower()

        if symbol.split("/")[0].lower() in t:
            if any(x in t for x in ["surge","breakout","etf","adoption"]):
                score += 2
            if any(x in t for x in ["hack","crash","ban","lawsuit"]):
                score -= 3

    return score
