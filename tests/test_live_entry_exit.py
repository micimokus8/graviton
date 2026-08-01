import unittest
from types import SimpleNamespace

from session import _execute_live_entry, _execute_live_partial_exit


class _FakeTrader:
    def __init__(self, *, sl_success=True, partial_success=True, open_success=True):
        self.sl_success = sl_success
        self.partial_success = partial_success
        self.open_success = open_success
        self.close_calls = []
        self.stop_calls = []

    def open_position(self, symbol, side):
        if self.open_success:
            return SimpleNamespace(success=True, price=200.0, size=2.0, message="ok")
        return SimpleNamespace(success=False, price=0, size=0, message="no fill")

    def set_stop_loss(self, symbol, side, stop_price, amount=None):
        self.stop_calls.append((symbol, side, stop_price, amount))
        return self.sl_success

    def close_position(self, symbol, side, close_pct=1.0):
        self.close_calls.append(close_pct)
        if close_pct == 0.5:
            return SimpleNamespace(success=self.partial_success, message="partial")
        return SimpleNamespace(success=True, message="full")


class LiveEntryExitIntegrationTests(unittest.TestCase):
    def test_failed_entry_does_not_create_stop_or_close(self):
        trader = _FakeTrader(open_success=False)

        from session import _execute_live_entry
        fill, live_stop, error = _execute_live_entry(
            trader, "X/USD:USD", "LONG", SimpleNamespace(entry_price=100.0, stop_loss=99.0)
        )

        self.assertIsNone(fill)
        self.assertEqual(trader.stop_calls, [])
        self.assertEqual(trader.close_calls, [])
        self.assertIn("Entry fehlgeschlagen", error)

    def test_partial_exit_failure_keeps_old_stop_active(self):
        trader = _FakeTrader(partial_success=False)

        from session import _execute_live_partial_exit
        status, error = _execute_live_partial_exit(
            trader, "X/USD:USD", "LONG", 100.0
        )

        self.assertEqual(status, "unchanged")
        self.assertEqual(trader.close_calls, [0.5])
        self.assertEqual(trader.stop_calls, [])


if __name__ == "__main__":
    unittest.main()
