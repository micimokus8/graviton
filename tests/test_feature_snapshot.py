import unittest

import numpy as np

from feature_snapshot import build_timeframe_features, relative_strength


class FeatureSnapshotTests(unittest.TestCase):
    def test_timeframe_features_are_numeric_and_use_closed_rows(self):
        rows = []
        for i in range(60):
            close = 100.0 + i * 0.2
            rows.append([i * 60_000, close - 0.1, close + 0.4, close - 0.3, close, 100.0 + i])
        features = build_timeframe_features(np.asarray(rows, dtype=float))
        self.assertEqual(features["candles"], 60)
        self.assertEqual(features["trend"], "BULLISH")
        self.assertGreater(features["ema20_slope_pct"], 0.0)
        self.assertTrue(0.0 <= features["rsi"] <= 100.0)
        self.assertIn(features["volatility_regime"], {"LOW", "NORMAL", "HIGH"})

    def test_empty_or_thin_data_returns_error_without_inventing_values(self):
        features = build_timeframe_features(np.empty((0, 6)))
        self.assertEqual(features["trend"], "ERROR")
        self.assertIsNone(features["rsi"])
        self.assertEqual(features["candles"], 0)

    def test_relative_strength_compares_same_period_returns(self):
        self.assertAlmostEqual(relative_strength(105.0, 102.0, 100.0, 100.0), 5.0 - 2.0)
        self.assertIsNone(relative_strength(100.0, 0.0, 102.0, 100.0))


if __name__ == "__main__":
    unittest.main()
