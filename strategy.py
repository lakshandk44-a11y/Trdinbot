class Strategy:

    def evaluate(self, scan_result):
        score = scan_result["score"]

        if score >= 8:
            return "STRONG BUY"
        elif score >= 6:
            return "BUY"
        elif score <= 3:
            return "SELL"
        else:
            return "NO TRADE"
