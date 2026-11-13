"""Tests der Swing-Engine (reine Funktionen, ohne Netz)."""
import unittest
from datetime import datetime, timezone

from swing import engine, sr
from swing.state import new_state

DAY = 86_400_000
H4 = 14_400_000
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def trend_candles(n=320, start=100.0, drift=0.004):
    """Stetiger Aufwärtstrend mit leichter Wellenbewegung."""
    rows, p = [], start
    for i in range(n):
        o = p
        c = p * (1 + drift + 0.0025 * ((i % 7) - 3) / 3)
        rows.append([START + i * DAY, o, max(o, c) * 1.004, min(o, c) * 0.996, c, 1000.0])
        p = c
    return rows


class TestClosed(unittest.TestCase):
    def test_running_bar_is_dropped(self):
        rows = [[START, 1, 1, 1, 1, 0], [START + DAY, 2, 2, 2, 2, 0]]
        # 12h nach Start der zweiten Kerze -> zweite ist noch offen
        now = START + DAY + DAY // 2
        self.assertEqual(len(engine.closed(rows, now, DAY)), 1)
        # 24h später -> beide geschlossen
        self.assertEqual(len(engine.closed(rows, START + 2 * DAY, DAY)), 2)


class TestEntryPlan(unittest.TestCase):
    def view(self, **kw):
        base = {"price": 100.0, "atr": 2.0, "stance": "LONG", "swing_high": 120.0,
                "swing_low": 97.0, "resistance": 106.0, "support": 90.0}
        base.update(kw)
        return base

    def test_valid_setup_has_rr(self):
        plan = engine.entry_plan(self.view(), {"ok": True})
        self.assertTrue(plan["ok"])
        self.assertGreaterEqual(plan["rr"], engine.MIN_RR)

    def test_rejects_when_too_close_to_last_high(self):
        plan = engine.entry_plan(self.view(swing_high=100.5), {"ok": True})
        self.assertFalse(plan["ok"])
        self.assertIn("Hoch", plan["reason"])

    def test_rejects_low_risk_reward(self):
        plan = engine.entry_plan(self.view(resistance=103.0, swing_low=97.0), {"ok": True})
        self.assertFalse(plan["ok"])
        self.assertIn("R:R", plan["reason"])

    def test_rejects_without_h4_trigger(self):
        plan = engine.entry_plan(self.view(), {"ok": False, "reason": "kein 4H"})
        self.assertFalse(plan["ok"])


class TestScan(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)  # Donnerstag

    def test_no_long_gives_none_with_funnel(self):
        flat = trend_candles()[:]  # wird unten als Fallback benutzt
        # fallende Serie -> kein LONG
        rows, p = [], 200.0
        for i in range(320):
            o = p
            c = p * 0.996
            rows.append([START + i * DAY, o, max(o, c) * 1.004, min(o, c) * 0.996, c, 1000.0])
            p = c
        data = {"X": {"d1": rows, "h4": flat[-100:], "symbol": "X/USD:USD"}}
        out = engine.scan_from_data(data, self.now, new_state())
        self.assertEqual(out["action"], "NONE")
        self.assertEqual(out["funnel"]["long"], 0)

    def test_trend_break_closes_position(self):
        rows, p = [], 200.0
        for i in range(320):
            o = p
            c = p * 0.996
            rows.append([START + i * DAY, o, max(o, c) * 1.004, min(o, c) * 0.996, c, 1000.0])
            p = c
        st = new_state()
        st["position"] = {"base": "X", "symbol": "X/USD:USD", "side": "LONG",
                          "entry": 100.0, "stop": 95.0, "tp": 120.0, "rr": 2.0,
                          "opened_at": "2026-11-01T00:00:00+00:00"}
        out = engine.scan_from_data({"X": {"d1": rows, "h4": [], "symbol": "X/USD:USD"}},
                                    self.now, st)
        self.assertEqual(out["action"], "CLOSE")
        self.assertIn("Trendbruch", out["reason"])


    def test_take_profit_books_half(self):
        rows = trend_candles(320)
        price = rows[-1][4]
        st = new_state()
        st["position"] = {"base": "X", "symbol": "X/USD:USD", "side": "LONG",
                          "entry": price * 0.9, "stop": price * 0.85,
                          "tp": price * 0.95, "rr": 2.0, "units": 1.0,
                          "partial_done": False,
                          "opened_at": "2026-11-10T00:00:00+00:00"}
        out = engine.scan_from_data({"X": {"d1": rows, "h4": [], "symbol": "X/USD:USD"}},
                                    self.now, st)
        self.assertEqual(out["action"], "PARTIAL")
        self.assertEqual(out["remaining_pct"], 50.0)


    def test_friday_closes_only_losers(self):
        rows = trend_candles(320)
        price = rows[-1][4]
        friday = datetime(2026, 11, 13, 20, 45, tzinfo=timezone.utc)  # Fr 21:45 Berlin
        data = {"X": {"d1": rows, "h4": [], "symbol": "X/USD:USD"}}

        winning = {"base": "X", "symbol": "X/USD:USD", "side": "LONG",
                   "entry": price * 0.95, "stop": price * 0.90, "tp": price * 1.05,
                   "rr": 2.0, "units": 1.0, "partial_done": False,
                   "opened_at": "2026-11-10T00:00:00+00:00"}
        st = new_state(); st["position"] = winning
        self.assertEqual(engine.scan_from_data(data, friday, st)["action"], "HOLD")

        losing = dict(winning, entry=price * 1.05)
        st = new_state(); st["position"] = losing
        out = engine.scan_from_data(data, friday, st)
        self.assertEqual(out["action"], "CLOSE")
        self.assertIn("Freitags-Close", out["reason"])


class TestPositionDecision(unittest.TestCase):
    def setUp(self):
        self.pos = {"base": "X", "side": "LONG", "entry": 100.0, "stop": 95.0,
                    "tp": 120.0, "rr": 2.0, "units": 1.0, "partial_done": False,
                    "opened_at": "2026-11-10T00:00:00+00:00"}
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)  # Donnerstag

    def test_stop_fills_at_stop_price(self):
        d = engine.position_decision(self.pos, {"price": 94.0, "stance": "LONG"}, self.now)
        self.assertEqual(d["action"], "CLOSE")
        self.assertEqual(d["reason"], "Not-SL")
        self.assertEqual(d["price"], 95.0)   # Fill am Stop, nicht am Kurseinbruch

    def test_partial_only_once(self):
        d = engine.position_decision(self.pos, {"price": 121.0, "stance": "LONG"}, self.now)
        self.assertEqual(d["action"], "PARTIAL")
        done = dict(self.pos, partial_done=True, stop=100.0)
        d2 = engine.position_decision(done, {"price": 121.0, "stance": "LONG"}, self.now)
        self.assertEqual(d2["action"], "HOLD")

    def test_trend_break_closes(self):
        d = engine.position_decision(self.pos, {"price": 101.0, "stance": None}, self.now)
        self.assertEqual(d["action"], "CLOSE")
        self.assertIn("Trendbruch", d["reason"])


class TestSR(unittest.TestCase):
    def test_next_resistance_and_support(self):
        highs = [(3, 110.0), (8, 125.0)]
        lows = [(5, 90.0), (9, 95.0)]
        self.assertEqual(sr.next_resistance(100.0, highs), 110.0)
        self.assertEqual(sr.next_support(100.0, lows), 95.0)

    def test_risk_reward(self):
        rr = sr.risk_reward(100, 90, 120)
        assert rr is not None
        self.assertAlmostEqual(rr, 2.0)
        self.assertIsNone(sr.risk_reward(100, 100, 120))


if __name__ == "__main__":
    unittest.main()
