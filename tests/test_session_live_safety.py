import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from session import (
    SessionLock,
    _execute_live_entry,
    _execute_live_partial_exit,
    _rebase_stop_loss,
)


class _FakeTrader:
    def __init__(self, *, sl_success=True, partial_success=True):
        self.sl_success = sl_success
        self.partial_success = partial_success
        self.close_calls = []
        self.stop_calls = []

    def open_position(self, symbol, side):
        return SimpleNamespace(success=True, price=200.0, size=2.0, message="ok")

    def set_stop_loss(self, symbol, side, stop_price, amount=None):
        self.stop_calls.append((symbol, side, stop_price, amount))
        return self.sl_success

    def close_position(self, symbol, side, close_pct=1.0):
        self.close_calls.append(close_pct)
        success = self.partial_success if close_pct == 0.5 else True
        return SimpleNamespace(success=success, message="ok" if success else "close failed")


class SessionLiveSafetyTests(unittest.TestCase):
    def test_stop_is_rebased_to_actual_fill_price(self):
        self.assertEqual(_rebase_stop_loss(100.0, 99.0, 200.0, "LONG"), 198.0)
        self.assertEqual(_rebase_stop_loss(100.0, 101.0, 200.0, "SHORT"), 202.0)

    def test_failed_stop_installation_emergency_closes_entry(self):
        trader = _FakeTrader(sl_success=False)
        signal = SimpleNamespace(entry_price=100.0, stop_loss=99.0)

        fill, live_stop, error = _execute_live_entry(
            trader, "X/USD:USD", "LONG", signal
        )

        self.assertIsNone(fill)
        self.assertEqual(live_stop, 198.0)
        self.assertIn("Stop-Loss", error)
        self.assertEqual(trader.close_calls, [1.0])

    def test_partial_exit_failure_keeps_existing_protection(self):
        trader = _FakeTrader(partial_success=False)

        status, error = _execute_live_partial_exit(
            trader, "X/USD:USD", "LONG", 100.0
        )

        self.assertEqual(status, "unchanged")
        self.assertIn("Partial-Close", error)
        self.assertEqual(trader.stop_calls, [])

    def test_failed_replacement_stop_closes_remaining_position(self):
        trader = _FakeTrader(sl_success=False)

        status, error = _execute_live_partial_exit(
            trader, "X/USD:USD", "LONG", 100.0
        )

        self.assertEqual(status, "closed")
        self.assertIn("Break-Even-SL", error)
        self.assertEqual(trader.close_calls, [0.5, 1.0])

    def test_session_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.lock"
            first = SessionLock(path)
            second = SessionLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
