import unittest

import numpy as np

from regime import classify_regime, directional, wilder


def _trend_candles(n=80, step=0.5, start=100.0, volume=100.0):
    """Stetiger Aufwaertstrend -> ADX hoch, +DI > -DI."""
    rows = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        rows.append([i * 3_600_000, o, c + 0.1, o - 0.1, c, volume])
        price = c
    return np.array(rows, dtype=float)


class RegimeTests(unittest.TestCase):
    def test_wilder_matches_recursive_definition(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        out = wilder(values, 3)
        # Start = SMA(3) = 2.0, dann (2*2+x)/3
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], (2.0 * 2 + 4.0) / 3)
        self.assertAlmostEqual(out[4], (out[3] * 2 + 5.0) / 3)

    def test_uptrend_confirms_long_and_vetoes_short(self):
        data = _trend_candles()
        stats = directional(data)
        self.assertGreater(stats["adx"], 25)
        self.assertGreater(stats["plus_di"], stats["minus_di"])

        long_verdict = classify_regime(stats, "LONG")
        self.assertFalse(long_verdict["veto"])
        self.assertEqual(long_verdict["regime"], "TREND")

        short_verdict = classify_regime(stats, "SHORT")
        self.assertTrue(short_verdict["veto"])

    def test_flat_market_is_chop_and_blocks_entries(self):
        # Seitwaerts: kein gerichteter Impuls -> ADX niedrig.
        rows = []
        for i in range(80):
            base = 100.0 + (0.05 if i % 2 else -0.05)
            rows.append([i * 3_600_000, base, base + 0.08, base - 0.08, base, 100.0])
        stats = directional(np.array(rows, dtype=float))
        verdict = classify_regime(stats, "LONG")
        self.assertEqual(verdict["regime"], "CHOP")
        self.assertTrue(verdict["veto"])

    def test_unknown_adx_never_vetoes(self):
        stats = {"adx": float("nan"), "plus_di": float("nan"), "minus_di": float("nan")}
        verdict = classify_regime(stats, "LONG")
        self.assertFalse(verdict["veto"])
        self.assertEqual(verdict["regime"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
