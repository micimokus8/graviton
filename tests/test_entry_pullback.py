import unittest

import numpy as np

from entry import EntryEngine, EntryState


class _PullbackExchange:
    def __init__(self, candles):
        self.candles = candles

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self.candles

    def fetch_ticker(self, symbol):
        return {"percentage": 3.0}


class EntryPullbackTests(unittest.TestCase):
    def test_long_pullback_below_ema_can_reject_and_enter(self):
        # Closed 5m candles: price is slightly below EMA20, but the last
        # candle is a valid bullish rejection with elevated volume.
        candles = []
        for i in range(30):
            close = 99.8 if i < 29 else 99.7
            candles.append([i * 300_000, 99.8, 99.9, 99.7, close, 100.0])
        candles[-1] = [29 * 300_000, 99.60, 99.80, 99.55, 99.70, 150.0]

        engine = EntryEngine()
        engine._exchange = _PullbackExchange(candles)
        engine._calc_sl_pct = lambda symbol, price: 0.6

        signal = engine.check_entry("ENA/USD:USD", "LONG")

        self.assertEqual(signal.state, EntryState.ENTERED)
        self.assertIn("Rejection", signal.reasoning)


if __name__ == "__main__":
    unittest.main()
    
