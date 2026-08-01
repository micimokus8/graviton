import unittest

import numpy as np

from candle_utils import closed_ohlcv


def _rows():
    # 5m candles starting at 00:00, 00:05, 00:10 UTC.
    return np.array([
        [0, 100, 101, 99, 100.5, 10],
        [300_000, 100.5, 102, 100, 101.5, 20],
        [600_000, 101.5, 103, 101, 102.5, 30],
    ], dtype=float)


class CandleUtilsTests(unittest.TestCase):
    def test_closed_ohlcv_excludes_current_candle(self):
        result = closed_ohlcv(_rows(), "5m", now_ms=750_000)
        self.assertEqual(result[:, 0].tolist(), [0.0, 300_000.0])

    def test_closed_ohlcv_keeps_candle_at_exact_close_boundary(self):
        result = closed_ohlcv(_rows(), "5m", now_ms=900_000)
        self.assertEqual(result[:, 0].tolist(), [0.0, 300_000.0, 600_000.0])

    def test_closed_ohlcv_returns_empty_with_no_closed_rows(self):
        result = closed_ohlcv(_rows(), "5m", now_ms=100_000)
        self.assertEqual(result.shape, (0, 6))


if __name__ == "__main__":
    unittest.main()
