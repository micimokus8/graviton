import unittest

from session import _resolve_dry_run_candle


class DryRunMathTests(unittest.TestCase):
    def test_long_stop_uses_stop_price_not_candle_close(self):
        event = _resolve_dry_run_candle(
            bias="LONG",
            entry_price=100.0,
            initial_stop=99.0,
            active_stop=99.0,
            half_closed=False,
            candle_open=100.5,
            candle_high=101.0,
            candle_low=98.5,
        )
        self.assertEqual(event, ("stop_loss", 99.0))

    def test_gap_through_long_stop_uses_candle_open(self):
        event = _resolve_dry_run_candle(
            bias="LONG",
            entry_price=100.0,
            initial_stop=99.0,
            active_stop=99.0,
            half_closed=False,
            candle_open=98.0,
            candle_high=100.0,
            candle_low=97.5,
        )
        self.assertEqual(event, ("stop_loss", 98.0))

    def test_profit_lock_uses_target_price(self):
        event = _resolve_dry_run_candle(
            bias="LONG",
            entry_price=100.0,
            initial_stop=99.0,
            active_stop=99.0,
            half_closed=False,
            candle_open=100.0,
            candle_high=101.5,
            candle_low=99.8,
        )
        self.assertEqual(event, ("profit_lock", 101.0))

    def test_stop_wins_when_initial_stop_and_profit_target_are_both_crossed(self):
        event = _resolve_dry_run_candle(
            bias="LONG",
            entry_price=100.0,
            initial_stop=99.0,
            active_stop=99.0,
            half_closed=False,
            candle_open=100.0,
            candle_high=101.5,
            candle_low=98.5,
        )
        self.assertEqual(event, ("stop_loss", 99.0))

    def test_short_breakeven_stop_uses_stop_price(self):
        event = _resolve_dry_run_candle(
            bias="SHORT",
            entry_price=100.0,
            initial_stop=101.0,
            active_stop=99.9,
            half_closed=True,
            candle_open=99.7,
            candle_high=100.5,
            candle_low=99.5,
        )
        self.assertEqual(event, ("breakeven_stop", 99.9))

    def test_short_initial_stop_uses_stop_price(self):
        event = _resolve_dry_run_candle(
            bias="SHORT",
            entry_price=100.0,
            initial_stop=101.0,
            active_stop=101.0,
            half_closed=False,
            candle_open=100.0,
            candle_high=101.5,
            candle_low=99.8,
        )
        self.assertEqual(event, ("stop_loss", 101.0))

    def test_short_profit_lock_uses_target_price(self):
        event = _resolve_dry_run_candle(
            bias="SHORT",
            entry_price=100.0,
            initial_stop=101.0,
            active_stop=101.0,
            half_closed=False,
            candle_open=100.0,
            candle_high=100.2,
            candle_low=98.5,
        )
        self.assertEqual(event, ("profit_lock", 99.0))

    def test_short_stop_wins_when_stop_and_target_are_both_crossed(self):
        event = _resolve_dry_run_candle(
            bias="SHORT",
            entry_price=100.0,
            initial_stop=101.0,
            active_stop=101.0,
            half_closed=False,
            candle_open=100.0,
            candle_high=101.5,
            candle_low=98.5,
        )
        self.assertEqual(event, ("stop_loss", 101.0))


if __name__ == "__main__":
    unittest.main()
