import unittest

import numpy as np

from volume_metrics import _session_volume_ratio

TF_MS = 15 * 60_000


def _row(ts_ms, vol):
    return [ts_ms, 100.0, 101.0, 99.0, 100.5, vol]


class VolumeRatioTests(unittest.TestCase):
    def test_first_session_candle_falls_back_to_optimistic_default(self):
        ts_pre = 0
        ts_open = 15 * TF_MS          # 15 min from epoch
        ts_now = ts_open + 16 * 60_000  # 16 min after session open
        candles = [
            _row(ts_pre, 1_000),
            _row(ts_open, 2_000),
        ]
        data = np.array(candles, dtype=float)
        baseline, session, ratio = _session_volume_ratio(
            data, session_open_ts=ts_open, now_ms=ts_now
        )

        self.assertEqual(session, [2_000])
        self.assertEqual(baseline, [1_000])
        # First closed session candle has no statistical counterpart;
        # we must not block it from the volume filter.
        self.assertGreater(ratio, 1.0)

    def test_median_of_three_session_candles_vs_pre_session(self):
        pre_tses = [i * TF_MS for i in range(20)]
        pre = [_row(ts, 2_000_000) for ts in pre_tses]
        sess_tses = [(20 + i) * TF_MS for i in range(3)]
        sess = [
            _row(sess_tses[0], 4_000_000),
            _row(sess_tses[1], 2_000_000),
            _row(sess_tses[2], 1_000_000),
        ]
        data = np.array(pre + sess, dtype=float)
        session_open_ts = sess_tses[0]
        now_ms = sess_tses[-1] + TF_MS + 16 * 60_000

        baseline, session, ratio = _session_volume_ratio(
            data, session_open_ts=session_open_ts, now_ms=now_ms
        )

        self.assertEqual(len(session), 3)
        self.assertEqual(sorted(session), [1_000_000, 2_000_000, 4_000_000])
        self.assertEqual(len(baseline), 20)
        self.assertAlmostEqual(ratio, 1.0, places=2)

    def test_two_session_candles_with_low_volume_drop_below_threshold(self):
        pre = [_row(i * TF_MS, 2_000_000) for i in range(20)]
        sess_tses = [(20) * TF_MS, (21) * TF_MS]
        sess = [
            _row(sess_tses[0], 200_000),
            _row(sess_tses[1], 200_000),
        ]
        data = np.array(pre + sess, dtype=float)
        now_ms = sess_tses[-1] + TF_MS + 16 * 60_000

        baseline, session, ratio = _session_volume_ratio(
            data, session_open_ts=sess_tses[0], now_ms=now_ms
        )

        self.assertLess(ratio, 0.5)
        self.assertEqual(sorted(session), [200_000, 200_000])
        self.assertEqual(baseline, [2_000_000] * 20)

    def test_empty_input_does_not_crash(self):
        data = np.empty((0, 6), dtype=float)
        baseline, session, ratio = _session_volume_ratio(
            data, session_open_ts=0, now_ms=1
        )
        self.assertEqual(baseline, [])
        self.assertEqual(session, [])
        self.assertGreater(ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
