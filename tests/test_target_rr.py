import unittest

from config import EXIT, profit_lock_pct_for


class TargetRRTests(unittest.TestCase):
    def test_target_is_rr_multiple_of_actual_stop(self):
        # DASH 27.10.: Entry 55.14, SL 54.36 -> 1.41% Stop. Ziel = 3R = 4.24%
        # (target_rr ist konfigurierbar; die Messung 28.10. sprach fuer 3R.)
        sl_pct = (55.14 - 54.36071429) / 55.14 * 100
        # Feste Krypto-Parameter, damit der Test nicht vom aktiven Modus
        # (stocks: 2R) abhaengt.
        cfg = {"target_rr": 3.0, "profit_lock_pct": 2.0, "target_max_pct": 10.0}
        target = profit_lock_pct_for(55.14, 54.36071429, cfg=cfg)
        expected = max(sl_pct * 3.0, 2.0)
        self.assertAlmostEqual(target, expected, places=2)
        self.assertAlmostEqual(target, 4.24, places=1)

    def test_target_has_minimum(self):
        # Sehr enger Stop (0.5%) darf das Ziel nicht unter die 2%-Untergrenze druecken.
        target = profit_lock_pct_for(100.0, 99.5)
        self.assertEqual(target, EXIT["profit_lock_pct"])

    def test_target_is_capped(self):
        # Breiter Stop (6%) -> 12% waeren unrealistisch, Cap greift.
        target = profit_lock_pct_for(100.0, 94.0)
        self.assertEqual(target, EXIT["target_max_pct"])

    def test_zero_entry_price_falls_back(self):
        self.assertEqual(profit_lock_pct_for(0.0, 0.0), EXIT["profit_lock_pct"])


class DryRunTargetTests(unittest.TestCase):
    def test_resolver_uses_passed_target(self):
        from session import _resolve_dry_run_candle

        # Ziel 3% statt Config-2%: High 102.5 muss dann NICHT als Lock gelten.
        event = _resolve_dry_run_candle(
            bias="LONG", entry_price=100.0, initial_stop=98.0,
            active_stop=98.0, half_closed=False,
            candle_open=100.5, candle_high=102.5, candle_low=99.5,
            profit_lock_pct=3.0,
        )
        self.assertIsNone(event)

        event = _resolve_dry_run_candle(
            bias="LONG", entry_price=100.0, initial_stop=98.0,
            active_stop=98.0, half_closed=False,
            candle_open=100.5, candle_high=103.2, candle_low=99.5,
            profit_lock_pct=3.0,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event[0], "profit_lock")
        self.assertAlmostEqual(event[1], 103.0)

    def test_default_keeps_config_target(self):
        from session import _resolve_dry_run_candle

        event = _resolve_dry_run_candle(
            bias="LONG", entry_price=100.0, initial_stop=98.0,
            active_stop=98.0, half_closed=False,
            candle_open=100.5, candle_high=103.0, candle_low=99.5,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event[0], "profit_lock")


if __name__ == "__main__":
    unittest.main()
