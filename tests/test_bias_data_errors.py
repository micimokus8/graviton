import unittest

import numpy as np

from bias import BiasAnalyzer


def _session_rows():
    rows = []
    for i in range(40):
        rows.append([i * 900_000, 100.0, 101.0, 99.0, 100.5, 100.0])
    return np.array(rows, dtype=float)


class BiasDataErrorTests(unittest.TestCase):
    def test_short_timeframe_data_is_error_not_neutral(self):
        analyzer = BiasAnalyzer()
        analyzer._fetch_ohlcv = lambda symbol, timeframe, limit: np.empty((0, 6))

        result = analyzer._tf_signal("X/USD:USD", "4h")

        self.assertEqual(result["signal"], "ERROR")

    def test_any_timeframe_error_forces_noise(self):
        analyzer = BiasAnalyzer()
        analyzer._fetch_ohlcv = lambda symbol, timeframe="15m", limit=40: _session_rows()
        signals = iter([
            {"signal": "BULLISH", "detail": "ok"},
            {"signal": "BULLISH", "detail": "ok"},
            {"signal": "ERROR", "detail": "fetch failed"},
        ])
        analyzer._tf_signal = lambda symbol, timeframe: next(signals)

        result = analyzer.analyze("X/USD:USD", session_open_ts=0)

        self.assertEqual(result.bias, "NOISE")
        self.assertIn("Datenfehler", result.reason)


if __name__ == "__main__":
    unittest.main()
