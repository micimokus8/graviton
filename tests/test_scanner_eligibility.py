import unittest

from scanner import KrakenScanner


class _FakeExchange:
    def __init__(self, ohlcv_change_pct):
        self.ohlcv_change_pct = ohlcv_change_pct
        self.markets = {
            "X/USD:USD": {
                "symbol": "X/USD:USD",
                "id": "X",
                "base": "X",
                "quote": "USD",
                "linear": True,
                "settle": "USD",
                "type": "swap",
                "active": True,
                "info": {"category": "AI"},
            }
        }

    def fetch_tickers(self, symbols):
        return {
            "X/USD:USD": {
                "percentage": 4.0,
                "quoteVolume": 2_000_000,
                "last": 100.0,
                "high": 101.0,
                "low": 99.0,
                "bid": 99.9,
                "ask": 100.1,
            }
        }

    def fetch_ohlcv(self, symbol, timeframe, limit):
        yesterday_open = 100.0
        yesterday_close = yesterday_open * (1 + self.ohlcv_change_pct / 100)
        return [
            [0, 100, 101, 99, 100, 1],
            [1, yesterday_open, 101, 99, yesterday_close, 1],
            [2, yesterday_close, 102, 99, yesterday_close, 1],
        ]


class ScannerEligibilityTests(unittest.TestCase):
    def _scan(self, ohlcv_change_pct):
        scanner = KrakenScanner()
        scanner._exchange = _FakeExchange(ohlcv_change_pct)
        scanner._update_eur_usd = lambda: None
        return scanner.scan()

    def test_rejects_coin_when_final_ohlcv_change_is_below_threshold(self):
        self.assertEqual(self._scan(0.4), [])

    def test_keeps_coin_when_final_ohlcv_change_passes_threshold(self):
        results = self._scan(4.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].change_24h_pct, 4.0)


if __name__ == "__main__":
    unittest.main()
