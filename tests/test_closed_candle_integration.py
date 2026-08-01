import time
import unittest

from bias import BiasAnalyzer
from entry import EntryEngine
from exit import ExitEngine


class _FakeExchange:
    def __init__(self, interval_ms):
        self.interval_ms = interval_ms

    def fetch_ohlcv(self, symbol, timeframe, limit):
        now = int(time.time() * 1000)
        return [
            [now - self.interval_ms * 2, 100, 101, 99, 100.5, 10],
            [now - self.interval_ms // 2, 100.5, 102, 100, 101.5, 20],
        ]


class ClosedCandleIntegrationTests(unittest.TestCase):
    def test_entry_fetch_excludes_open_5m_candle(self):
        engine = EntryEngine()
        engine._exchange = _FakeExchange(5 * 60_000)
        rows = engine._fetch_1m("X/USD:USD", tf="5m")
        self.assertEqual(len(rows), 1)

    def test_bias_fetch_excludes_open_15m_candle(self):
        analyzer = BiasAnalyzer()
        analyzer._exchange = _FakeExchange(15 * 60_000)
        rows = analyzer._fetch_ohlcv("X/USD:USD", timeframe="15m")
        self.assertEqual(len(rows), 1)

    def test_exit_fetch_excludes_open_1m_candle(self):
        engine = ExitEngine()
        engine._exchange = _FakeExchange(60_000)
        rows = engine._fetch_1m("X/USD:USD")
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
