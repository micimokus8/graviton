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

    def test_recent_closed_rejection_is_not_missed_when_following_candle_moves_away(self):
        # The rejection is the penultimate closed candle. The latest candle
        # has already moved away, but the current price remains within the
        # EMA entry band, so the pullback is still actionable.
        candles = []
        for i in range(30):
            candles.append([i * 300_000, 99.8, 99.9, 99.7, 99.8, 100.0])
        candles[-2] = [28 * 300_000, 99.60, 99.80, 99.55, 99.70, 105.0]
        candles[-1] = [29 * 300_000, 99.85, 99.90, 99.75, 99.82, 100.0]

        engine = EntryEngine()
        engine._exchange = _PullbackExchange(candles)
        engine._calc_sl_pct = lambda symbol, price: 0.6

        signal = engine.check_entry("ENA/USD:USD", "LONG")

        self.assertEqual(signal.state, EntryState.ENTERED)
        self.assertIn("1 Kerze(n) zurück", signal.reasoning)
        self.assertAlmostEqual(signal.entry_price, 99.82)

    def test_dash_crash_entry_bar_is_not_entered(self):
        # DASH 27.10.: gueltige Rejection 2 Bars zurueck, aber der Entry-Bar
        # schloss auf 0% seiner Range (55.36 -> 55.14) — klassischer Absturz.
        # Der neue Geometrie-Filter muss den Entry verweigern.
        candles = []
        for i in range(30):
            candles.append([i * 300_000, 55.00, 55.04, 54.98, 55.00, 100.0])
        candles[-3] = [27 * 300_000, 54.95, 55.10, 54.94, 55.06, 150.0]  # Rejection
        candles[-2] = [28 * 300_000, 55.06, 55.08, 55.02, 55.05, 90.0]
        candles[-1] = [29 * 300_000, 55.06, 55.06, 54.90, 54.90, 60.0]   # Absturz, Close=Tief

        engine = EntryEngine()
        engine._exchange = _PullbackExchange(candles)
        engine._calc_sl_pct = lambda symbol, price: 1.41

        signal = engine.check_entry("DASH/USD:USD", "LONG")

        self.assertNotEqual(signal.state, EntryState.ENTERED)
        self.assertIn("AT_EMA", signal.state.name)
        # Der Blockgrund muss im Log stehen, nicht nur "keine Rejection".
        self.assertIn("Entry-Bar", signal.reasoning)

    def test_rejection_without_volume_is_not_entered(self):
        # Signal-Bar mit 0.4x Median-Volumen: keine Beteiligung = keine Rejection.
        candles = []
        for i in range(30):
            candles.append([i * 300_000, 99.8, 99.9, 99.7, 99.8, 100.0])
        candles[-1] = [29 * 300_000, 99.60, 99.80, 99.55, 99.70, 40.0]

        engine = EntryEngine()
        engine._exchange = _PullbackExchange(candles)
        engine._calc_sl_pct = lambda symbol, price: 0.6

        signal = engine.check_entry("ENA/USD:USD", "LONG")

        self.assertNotEqual(signal.state, EntryState.ENTERED)
        self.assertIn("Volumen", signal.reasoning)

    def test_deep_wick_reports_reason_and_depth(self):
        # PEPE 28.10.: Low 0.72% unter EMA20 -> Wick-Toleranz 0.5% verletzt.
        # Der Grund muss im Signal stehen, nicht nur "keine Rejection".
        candles = []
        for i in range(30):
            candles.append([i * 300_000, 100.0, 100.05, 99.95, 100.0, 100.0])
        candles[-1] = [29 * 300_000, 99.90, 100.02, 99.20, 99.98, 150.0]

        engine = EntryEngine()
        engine._exchange = _PullbackExchange(candles)
        engine._calc_sl_pct = lambda symbol, price: 1.4

        signal = engine.check_entry("PEPE/USD:USD", "LONG")

        self.assertNotEqual(signal.state, EntryState.ENTERED)
        self.assertIn("Wick", signal.reasoning)
        self.assertGreater(signal.wick_depth_pct, 0.5)


if __name__ == "__main__":
    unittest.main()
    
