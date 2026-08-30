import unittest
from unittest.mock import patch

import numpy as np

from bias import BiasAnalyzer, BiasResult


class FeatureSnapshotIntegrationTests(unittest.TestCase):
    def test_feature_fetch_failure_does_not_change_deterministic_bias(self):
        analyzer = BiasAnalyzer()
        result = BiasResult(symbol="X/USD:USD", bias="SHORT")
        with patch.object(analyzer, "_fetch_ohlcv", side_effect=RuntimeError("rate limit")):
            enriched = analyzer._build_feature_snapshot(result)
        self.assertEqual(enriched.bias, "SHORT")
        self.assertEqual(enriched.features["status"], "ERROR")
        self.assertEqual(enriched.features["timeframes"]["30m"]["trend"], "ERROR")

    def test_feature_snapshot_contains_required_timeframes_and_btc_context(self):
        analyzer = BiasAnalyzer()
        candles = np.asarray([
            [i * 1_800_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000]
            for i in range(60)
        ], dtype=float)
        btc = np.asarray([
            [i * 1_800_000, 200 + i, 201 + i, 199 + i, 200.5 + i, 1000]
            for i in range(60)
        ], dtype=float)

        def fetch(symbol, timeframe, limit=60):
            return btc if symbol == "BTC/USD:USD" else candles

        with patch.object(analyzer, "_fetch_ohlcv", side_effect=fetch):
            enriched = analyzer._build_feature_snapshot(BiasResult(symbol="X/USD:USD", bias="LONG"))
        self.assertEqual(enriched.features["status"], "OK")
        self.assertEqual(set(enriched.features["timeframes"]), {"15m", "30m", "1h"})
        self.assertEqual(set(enriched.features["btc"]), {"30m", "1h"})
        self.assertIn("relative_strength_30m_pct", enriched.features)

    def test_feature_collection_is_opt_in(self):
        analyzer = BiasAnalyzer()
        with patch.object(analyzer, "analyze", return_value=BiasResult(symbol="X/USD:USD", bias="NOISE")), \
             patch.object(analyzer, "_build_feature_snapshot") as build:
            analyzer._exchange = object()
            # Exercise the public batch function without feature collection.
            with patch("bias.BiasAnalyzer", return_value=analyzer):
                from bias import analyze_watchlist
                analyze_watchlist(["X/USD:USD"], 0)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
