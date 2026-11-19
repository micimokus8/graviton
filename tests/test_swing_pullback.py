"""Pullback-Einstieg, Short-Seite, Handelstage und Datenfehler."""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from swing import book, engine, sr
from swing.state import new_state
from swing.strategy import MIN_BARS

DAY = 86_400_000
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _view(**kw):
    base = {
        "price": 100.0, "atr": 2.0, "stance": "LONG",
        "swing_high": 104.0, "swing_low": 97.0,
        "resistance": 106.0, "support": 94.0,
        "impulse_low": 96.0, "stop_high": 104.0,
    }
    base.update(kw)
    return base


def _candles_from_closes(closes, start=START):
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append([start + i * DAY, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1000.0])
        prev = c
    return rows


def _grind(n, drift=1.003, start_px=50.0):
    closes, p = [], start_px
    for _ in range(n):
        p *= drift
        closes.append(p)
    return closes


class TestPullbackEntry(unittest.TestCase):
    def test_existing_valid_setup_still_passes(self):
        plan = engine.entry_plan(
            _view(swing_high=120.0, swing_low=97.0, resistance=106.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        self.assertGreaterEqual(plan["rr"], engine.MIN_RR)
        self.assertEqual(plan["side"], "LONG")

    def test_fresh_high_without_pivot_is_rejected(self):
        plan = engine.entry_plan(_view(swing_high=None), {"ok": True})
        self.assertFalse(plan["ok"])
        self.assertIn("Hoch", plan["reason"])

    def test_price_above_last_swing_high_is_rejected(self):
        plan = engine.entry_plan(_view(swing_high=99.0), {"ok": True})
        self.assertFalse(plan["ok"])
        self.assertIn("Hoch", plan["reason"])

    def test_shallow_pullback_uses_measured_target_not_the_high(self):
        """1 ATR unter dem Hoch: früher war das Hoch der Widerstand und zu nah."""
        plan = engine.entry_plan(
            _view(swing_high=102.0, resistance=None, swing_low=97.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        self.assertAlmostEqual(plan["target"], 100.0 + engine.MEASURED_TARGET_ATR * 2.0)
        self.assertGreater(plan["entry"], plan["stop"])

    def test_too_close_high_still_rejected(self):
        plan = engine.entry_plan(_view(swing_high=100.5), {"ok": True})
        self.assertFalse(plan["ok"])
        self.assertIn("Hoch", plan["reason"])


class TestStopDistance(unittest.TestCase):
    def test_tiny_swing_low_is_widened_to_minimum(self):
        atr = 2.0
        tiny = 100.0 - 0.13 * atr
        plan = engine.entry_plan(
            _view(swing_low=tiny, swing_high=108.0, resistance=106.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        risk = plan["entry"] - plan["stop"]
        self.assertGreaterEqual(risk, engine.MIN_STOP_ATR * atr - 1e-9)
        self.assertLessEqual(risk, engine.RISK_ATR_CAP * atr + 1e-9)
        # Ohne Mindestabstand wäre das Risiko 0,13 ATR und R:R zweistellig.
        self.assertLess(plan["rr"], 5)

    def test_buffer_sits_beyond_the_swing_low(self):
        atr = 2.0
        swing_low = 100.0 - 1.2 * atr
        plan = engine.entry_plan(
            _view(swing_low=swing_low, swing_high=108.0, resistance=106.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        raw = swing_low - engine.STOP_BUFFER_ATR * atr
        self.assertAlmostEqual(plan["stop"], raw)

    def test_stop_never_wider_than_cap(self):
        plan = engine.entry_plan(
            _view(swing_low=90.0, swing_high=108.0, resistance=106.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        self.assertAlmostEqual(plan["stop"], 100.0 - engine.RISK_ATR_CAP * 2.0)


class TestShortSide(unittest.TestCase):
    def test_rally_into_resistance_is_a_short(self):
        plan = engine.entry_plan(
            _view(stance="SHORT", impulse_low=94.0, stop_high=104.0, support=94.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["side"], "SHORT")
        self.assertGreater(plan["stop"], plan["entry"])
        self.assertLess(plan["target"], plan["entry"])
        self.assertGreaterEqual(plan["rr"], engine.MIN_RR)

    def test_fresh_low_is_rejected(self):
        for low in (None, 101.0):
            plan = engine.entry_plan(
                _view(stance="SHORT", impulse_low=low, support=90.0),
                {"ok": True})
            self.assertFalse(plan["ok"], low)
            self.assertIn("Tief", plan["reason"])

    def test_shallow_rally_does_not_use_the_low_as_a_too_close_target(self):
        plan = engine.entry_plan(
            _view(stance="SHORT", impulse_low=99.0, stop_high=103.0, support=None),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        self.assertAlmostEqual(plan["target"], 100.0 - engine.MEASURED_TARGET_ATR * 2.0)

    def test_tiny_stop_above_is_widened(self):
        atr = 2.0
        plan = engine.entry_plan(
            _view(stance="SHORT", impulse_low=94.0,
                  stop_high=100.0 + 0.13 * atr, support=94.0),
            {"ok": True})
        self.assertTrue(plan["ok"], plan)
        risk = plan["stop"] - plan["entry"]
        self.assertGreaterEqual(risk, engine.MIN_STOP_ATR * atr - 1e-9)

    def test_short_can_be_switched_off(self):
        from swing import config
        old = config.ALLOW_SHORT
        config.ALLOW_SHORT = False
        try:
            plan = engine.entry_plan(_view(stance="SHORT"), {"ok": True})
            self.assertFalse(plan["ok"])
            self.assertIn("ausgeschaltet", plan["reason"])
        finally:
            config.ALLOW_SHORT = old

    def test_risk_reward_is_symmetric(self):
        self.assertAlmostEqual(sr.risk_reward(100, 110, 80, "SHORT"), 2.0)
        self.assertIsNone(sr.risk_reward(100, 90, 80, "SHORT"))
        self.assertAlmostEqual(sr.risk_reward(100, 90, 120), 2.0)


class TestMeaningfulPivots(unittest.TestCase):
    def test_old_and_tiny_pivots_do_not_count(self):
        series = [100.0] * 200
        series[20] = 130.0     # alt, außerhalb der 60 Kerzen
        series[180] = 100.2    # Zacke, kleiner als 0,4 ATR
        series[190] = 106.0    # jung und deutlich
        pivots = sr.meaningful_swings(series, kind="high", atr=2.0)
        self.assertEqual([v for _, v in pivots], [106.0])

    def test_shallow_impulse_is_not_the_target_or_a_blocker(self):
        # Letztes Hoch 1 ATR über dem Kurs, älteres Hoch zu weit weg.
        levels = [(10, 110.0), (40, 102.0)]
        self.assertIsNone(sr.target_level(
            100.0, 2.0, levels, side="LONG", min_atr=1.5, max_atr=4.0))

    def test_other_close_pivot_still_blocks(self):
        levels = [(20, 102.5), (40, 101.0)]
        self.assertEqual(sr.target_level(
            100.0, 2.0, levels, side="LONG", min_atr=1.5, max_atr=4.0), 102.5)

    def test_latest_level_can_sit_below_price(self):
        levels = [(5, 90.0), (12, 97.0)]
        self.assertEqual(sr.latest_level(levels), 97.0)

    def test_short_target_skips_the_shallow_low(self):
        levels = [(10, 80.0), (40, 99.0)]
        self.assertIsNone(sr.target_level(
            100.0, 2.0, levels, side="SHORT", min_atr=1.5, max_atr=4.0))


class TestDailyViewPullback(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.now_ms = int(self.now.timestamp() * 1000)

    def test_monotonic_uptrend_is_not_bought_at_the_high(self):
        rows = _candles_from_closes(_grind(180))
        view = engine.daily_view(rows, self.now_ms)
        self.assertIsNotNone(view)
        self.assertEqual(view["stance"], "LONG")
        plan = engine.entry_plan(view, {"ok": True})
        self.assertFalse(plan["ok"], plan)
        self.assertIn("Hoch", plan["reason"])

    def test_pullback_in_uptrend_can_be_bought(self):
        closes = _grind(168)
        # Kleiner Rücksetzer (~1 ATR bei diesem engen Grind), plus Kerzen
        # danach, damit das Hoch bestätigt ist. Das Hoch selbst ist eindeutig,
        # sonst zählt es nicht als Pivot.
        peak = closes[-1]
        for i in range(6):
            closes.append(peak * (1 - 0.0008 * (i + 1)))
        rows = _candles_from_closes(closes)
        peak_i = 167
        rows[peak_i][2] = rows[peak_i][4] * 1.003
        for j in range(peak_i + 1, len(rows)):
            rows[j][2] = min(rows[j][2], rows[peak_i][4] * 1.001)
        view = engine.daily_view(rows, self.now_ms)
        self.assertIsNotNone(view)
        self.assertEqual(view["stance"], "LONG", view["stance"])
        self.assertIsNotNone(view["swing_high"])
        self.assertLess(view["price"], view["swing_high"])
        gap_atr = (view["swing_high"] - view["price"]) / view["atr"]
        self.assertGreaterEqual(gap_atr, engine.NOT_AT_TOP_ATR)
        self.assertLess(gap_atr, engine.TARGET_MIN_ATR)
        # Früher war dieses Hoch gleichzeitig der Widerstand und damit „zu nah“.
        self.assertNotEqual(view["resistance"], view["swing_high"])
        plan = engine.entry_plan(view, {"ok": True})
        self.assertTrue(plan["ok"], plan)

    def test_monotonic_downtrend_is_not_shorted_at_the_low(self):
        rows = _candles_from_closes(_grind(180, drift=0.997, start_px=200.0))
        view = engine.daily_view(rows, self.now_ms)
        self.assertIsNotNone(view)
        self.assertEqual(view["stance"], "SHORT")
        plan = engine.entry_plan(view, {"ok": True})
        self.assertFalse(plan["ok"], plan)
        self.assertIn("Tief", plan["reason"])


class TestShortExits(unittest.TestCase):
    def setUp(self):
        self.pos = {"base": "X", "side": "SHORT", "entry": 100.0, "stop": 105.0,
                    "tp": 90.0, "rr": 2.0, "units": 1.0, "partial_done": False,
                    "opened_at": "2026-11-10T00:00:00+00:00"}
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)

    def test_stop_fills_at_stop(self):
        d = engine.position_decision(
            self.pos, {"price": 106.0, "stance": "SHORT"}, self.now)
        self.assertEqual(d["action"], "CLOSE")
        self.assertEqual(d["reason"], "Not-SL")
        self.assertEqual(d["price"], 105.0)

    def test_take_profit_on_the_way_down(self):
        d = engine.position_decision(
            self.pos, {"price": 89.0, "stance": "SHORT"}, self.now)
        self.assertEqual(d["action"], "PARTIAL")
        self.assertEqual(d["price"], 90.0)

    def test_trend_break_closes_short(self):
        d = engine.position_decision(
            self.pos, {"price": 99.0, "stance": "LONG"}, self.now)
        self.assertEqual(d["action"], "CLOSE")
        self.assertIn("Trendbruch", d["reason"])

    def test_friday_closes_losing_short_only(self):
        friday = datetime(2026, 11, 13, 20, 45, tzinfo=timezone.utc)
        win = engine.position_decision(
            self.pos, {"price": 98.0, "stance": "SHORT"}, friday)
        self.assertEqual(win["action"], "HOLD")
        lose = engine.position_decision(
            self.pos, {"price": 101.0, "stance": "SHORT"}, friday)
        self.assertEqual(lose["action"], "CLOSE")
        self.assertIn("Freitags-Close", lose["reason"])

    def test_max_hold_uses_bars_not_calendar(self):
        view = {"price": 99.0, "stance": "SHORT", "bars_held": engine.MAX_HOLD_BARS}
        d = engine.position_decision(self.pos, view, self.now)
        self.assertEqual(d["reason"], "Max-Hold")
        view["bars_held"] = engine.MAX_HOLD_BARS - 1
        d2 = engine.position_decision(self.pos, view, self.now)
        self.assertEqual(d2["action"], "HOLD")


class TestBarsHeld(unittest.TestCase):
    def test_weekend_is_not_a_bar(self):
        friday = datetime(2026, 11, 13, tzinfo=timezone.utc)
        monday = datetime(2026, 11, 16, tzinfo=timezone.utc)
        now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        candles = [
            [int(friday.timestamp() * 1000), 1, 1, 1, 1, 0],
            [int(monday.timestamp() * 1000), 1, 1, 1, 1, 0],
        ]
        held = engine.bars_held(candles, friday.isoformat(), int(now.timestamp() * 1000))
        self.assertEqual(held, 1)
        self.assertGreater((now.date() - friday.date()).days, held)

    def test_scan_does_not_close_on_calendar_age_alone(self):
        # Kerzen enden lange vor `now`. Kalender-Haltedauer wäre >> 20 Tage,
        # es gibt aber keine Tageskerze nach dem Entry.
        rows = _candles_from_closes(_grind(180))
        last = datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc)
        now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        price = rows[-1][4]
        st = new_state()
        st["position"] = {
            "base": "X", "symbol": "X/USD:USD", "side": "LONG",
            "entry": price * 0.99, "stop": price * 0.5, "tp": price * 2,
            "rr": 2.0, "units": 1.0, "partial_done": False,
            "opened_at": last.isoformat(),
        }
        out = engine.scan_from_data(
            {"X": {"d1": rows, "h4": [], "symbol": "X/USD:USD"}}, now, st)
        self.assertEqual(out["action"], "HOLD", out)


class TestBookSide(unittest.TestCase):
    def test_short_pnl_gains_when_price_falls(self):
        now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        st = new_state()
        book.apply_decision(st, {
            "action": "ENTRY",
            "pick": {"base": "X", "symbol": "X/USD:USD", "side": "SHORT",
                     "entry": 100.0, "stop": 105.0, "target": 90.0, "rr": 2.0},
        }, now)
        self.assertEqual(st["position"]["side"], "SHORT")
        book.apply_decision(st, {"action": "PARTIAL", "price": 90.0}, now)
        book.apply_decision(st, {"action": "CLOSE", "price": 80.0, "reason": "x"}, now)
        self.assertIsNone(st["position"])
        self.assertAlmostEqual(st["closed"][-1]["pnl_pct"], 15.0)

    def test_long_pnl_unchanged(self):
        now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        st = new_state()
        book.apply_decision(st, {
            "action": "ENTRY",
            "pick": {"base": "X", "symbol": "X/USD:USD", "side": "LONG",
                     "entry": 100.0, "stop": 95.0, "target": 110.0, "rr": 2.0},
        }, now)
        book.apply_decision(st, {"action": "CLOSE", "price": 110.0, "reason": "x"}, now)
        self.assertAlmostEqual(st["closed"][-1]["pnl_pct"], 10.0)


class TestDataErrors(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_kraken_failure_is_a_data_error(self):
        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                raise RuntimeError("timeout boom")

        loaded = engine.load_data(Ex(), [{"base": "NVDAX", "symbol": "NVDAX/USD:USD"}],
                                  self.now, fetcher=lambda s: [])
        err = loaded["NVDAX"]["error"]
        self.assertIn("Datenfehler", err)
        self.assertIn("Kraken", err)
        self.assertIn("timeout boom", err)
        out = engine.scan_from_data(loaded, self.now, new_state())
        self.assertIn("NVDAX", out["data_errors"])
        self.assertNotIn("NVDAX", out["rejects"])

    def test_empty_kraken_is_a_data_error(self):
        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                return []

        loaded = engine.load_data(
            Ex(), [{"base": "SAMSUNG", "symbol": "SAMSUNG/USD:USD"}], self.now)
        self.assertIn("keine Tageskerzen", loaded["SAMSUNG"]["error"])

    def test_twelvedata_nothing_names_the_ticker(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        raw = []
        for i in range(40):
            ts = int((start + timedelta(days=i)).timestamp() * 1000)
            raw.append([ts, 1, 1, 1, 1, 1])

        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                return [] if tf == "4h" else raw

        seen = []

        def fetcher(sym):
            seen.append(sym)
            return None

        loaded = engine.load_data(
            Ex(), [{"base": "SAMSUNG", "symbol": "SAMSUNG/USD:USD"}],
            self.now, fetcher=fetcher)
        self.assertEqual(seen, ["005930.KS"])
        err = loaded["SAMSUNG"]["error"]
        self.assertEqual(err, "Datenfehler: TwelveData 005930.KS lieferte nichts")
        out = engine.scan_from_data(loaded, self.now, new_state())
        self.assertNotIn("zu wenig 1D-Historie", out["rejects"].values())
        self.assertIn("005930.KS", out["data_errors"]["SAMSUNG"])

    def test_open_position_keeps_the_data_error(self):
        st = new_state()
        st["position"] = {
            "base": "SKHY", "symbol": "SKHY/USD:USD", "side": "LONG",
            "entry": 10.0, "stop": 9.0, "tp": 12.0, "rr": 2.0,
            "opened_at": "2026-05-01T00:00:00+00:00",
        }
        loaded = {"SKHY": {
            "d1": [], "h4": [], "symbol": "SKHY/USD:USD",
            "error": "Datenfehler: TwelveData 000660.KS lieferte nichts",
        }}
        out = engine.scan_from_data(loaded, self.now, st)
        self.assertEqual(out["action"], "NONE")
        self.assertIn("000660.KS", out["reason"])
        self.assertIn("SKHY", out["data_errors"])

    def test_short_history_without_underlying_stays_a_history_reject(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        raw = []
        for i in range(30):
            ts = int((start + timedelta(days=i)).timestamp() * 1000)
            raw.append([ts, 1, 1, 1, 1, 1])

        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                return [] if tf == "4h" else raw

        loaded = engine.load_data(
            Ex(), [{"base": "SPCXX", "symbol": "SPCXX/USD:USD"}],
            self.now, fetcher=lambda s: (_ for _ in ()).throw(AssertionError("no fetch")))
        self.assertIsNone(loaded["SPCXX"]["error"])
        out = engine.scan_from_data(loaded, self.now, new_state())
        self.assertEqual(out["rejects"]["SPCXX"], "zu wenig 1D-Historie")
        self.assertNotIn("SPCXX", out["data_errors"])

    def test_weekends_are_dropped_before_the_history_check(self):
        """150 Kalendertage sind genug für den alten 7-Tage-Check, aber zu
        wenig Handelstage — dann muss das Underlying ran."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)  # Montag
        raw = []
        px = 10.0
        for i in range(150):
            ts = int((start + timedelta(days=i)).timestamp() * 1000)
            raw.append([ts, px, px, px, px, 1])
        self.assertGreaterEqual(len(raw), MIN_BARS)
        weekdays = engine.trading_day_candles(raw)
        self.assertLess(len(weekdays), MIN_BARS)

        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                if tf == "4h":
                    return [[raw[-1][0], 1, 1, 1, px, 1]]
                return raw

        def fetcher(sym):
            self.assertEqual(sym, "AAPL")
            rows = []
            for i in range(160):
                # nur Werktage, ab einem Montag
                ts = int((start + timedelta(days=i)).timestamp() * 1000)
                if datetime.fromtimestamp(ts / 1000, timezone.utc).weekday() >= 5:
                    continue
                rows.append([ts, 20, 21, 19, 20, 1])
            return rows

        loaded = engine.load_data(
            Ex(), [{"base": "AAPLX", "symbol": "AAPLX/USD:USD"}],
            datetime(2026, 1, 1, tzinfo=timezone.utc), fetcher=fetcher)
        self.assertIsNone(loaded["AAPLX"]["error"])
        for c in loaded["AAPLX"]["d1"]:
            wd = datetime.fromtimestamp(c[0] / 1000, timezone.utc).weekday()
            self.assertLess(wd, 5)
        self.assertGreaterEqual(
            len(engine.closed(loaded["AAPLX"]["d1"],
                              int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
                              DAY)),
            MIN_BARS)

    def test_enough_weekdays_do_not_call_the_provider(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        raw = []
        for i in range(220):
            ts = int((start + timedelta(days=i)).timestamp() * 1000)
            raw.append([ts, 5, 5, 5, 5, 1])

        class Ex:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                return [] if tf == "4h" else raw

        loaded = engine.load_data(
            Ex(), [{"base": "AAPLX", "symbol": "AAPLX/USD:USD"}],
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            fetcher=lambda s: (_ for _ in ()).throw(AssertionError(s)))
        self.assertIsNone(loaded["AAPLX"]["error"])
        self.assertTrue(all(
            datetime.fromtimestamp(c[0] / 1000, timezone.utc).weekday() < 5
            for c in loaded["AAPLX"]["d1"]))
        self.assertGreaterEqual(len(loaded["AAPLX"]["d1"]), MIN_BARS)


def _report_mod():
    path = Path(__file__).resolve().parents[1] / "scripts" / "swing_session.py"
    spec = importlib.util.spec_from_file_location("swing_session_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestReport(unittest.TestCase):
    def test_data_error_and_short_line(self):
        mod = _report_mod()
        now = datetime(2026, 11, 12, 14, 35, tzinfo=timezone.utc)
        text = mod.report({
            "action": "ENTRY",
            "reason": "bestes Setup (ADX-Ranking)",
            "position": None,
            "funnel": {"total": 2, "long": 1, "short": 1, "rr_ok": 1,
                       "long_bases": ["NVDAX"], "short_bases": ["TSLAX"]},
            "data_errors": {
                "SAMSUNG": "Datenfehler: TwelveData 005930.KS lieferte nichts",
            },
            "rejects": {"INTC": "zu wenig 1D-Historie"},
            "candidates": [
                {"base": "TSLAX", "side": "SHORT", "entry": 20.0, "stop": 21.0,
                 "target": 17.0, "rr": 2.1, "adx": 28.0},
            ],
            "pick": {"base": "TSLAX", "side": "SHORT", "entry": 20.0, "stop": 21.0,
                     "target": 17.0, "rr": 2.1, "adx": 28.0},
        }, now)
        self.assertIn("DATENFEHLER", text)
        self.assertIn("005930.KS", text)
        self.assertIn("Short-Kandidaten: TSLAX", text)
        self.assertIn("im Abwärtstrend", text)
        self.assertIn("Short: TSLAX", text)
        self.assertIn("ENTRY: TSLAX (SHORT)", text)


class TestScanShortToggle(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        closes = _grind(180, drift=0.997, start_px=200.0)
        self.data = {"X": {"d1": _candles_from_closes(closes), "h4": [],
                           "symbol": "X/USD:USD"}}

    def test_downtrend_is_evaluated_as_short(self):
        out = engine.scan_from_data(self.data, self.now, new_state())
        self.assertEqual(out["funnel"]["long"], 0)
        self.assertGreaterEqual(out["funnel"]["short"], 1)
        self.assertNotEqual(out["rejects"].get("X"), "Trend abwärts")

    def test_short_toggle_off_is_explicit(self):
        from swing import config
        old = config.ALLOW_SHORT
        config.ALLOW_SHORT = False
        try:
            out = engine.scan_from_data(self.data, self.now, new_state())
        finally:
            config.ALLOW_SHORT = old
        self.assertEqual(out["funnel"]["short"], 0)
        self.assertEqual(out["rejects"]["X"], "Short ausgeschaltet")


if __name__ == "__main__":
    unittest.main()
