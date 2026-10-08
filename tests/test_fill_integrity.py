#!/usr/bin/env python3
"""Test: Exit-Meldungen müssen echten Fill-Preis + Konto-Stand widerspiegeln.

Deckt den gefundenen Fake-Bug ab: Der Bot sendete eine erfundene
"EXIT 100% @ 1152.82 +4.34%" Meldung, obwohl die echte zweite Hälfte
längst vom Kraken-SL @ 1190.17 geschlossen war. Quelle der Wahrheit sind
die echten Kraken-Fills, nicht der Signal-Preis.
"""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session


class MockTrader:
    """Simuliert Kraken-Fills + Close-Verhalten ohne echte API."""

    def __init__(self, fills=(),
                 close_success=True, close_message="Closed 100%",
                 close_price=1190.17, account=None):
        self._fills = list(fills)
        self._close_success = close_success
        self._close_message = close_message
        self._close_price = close_price
        self._account = account or {"eur": 491.99, "usd": 0.63,
                                    "usd_total": 492.0, "rate": 1.08}
        self.closed_with = None

    def has_api_keys(self):
        return True

    def get_fills_since(self, symbol, since_ms):
        return self._fills

    def get_account_state(self):
        return dict(self._account)

    def close_position(self, symbol, side, close_pct=1.0, **kw):
        from trader import TradeResult
        self.closed_with = (symbol, side, close_pct)
        if not self._close_success:
            return TradeResult(False, symbol, side, "", 0, 0, 0,
                               self._close_message, "t")
        return TradeResult(True, symbol, side, "f", self._close_price, 0, 0,
                           "Closed 100%", "t")

    def get_position_contracts(self, symbol, side):
        return 0.0


def test_last_live_fill_uses_fill_price():
    t = MockTrader(fills=[
        {"timestamp": 100, "side": "sell", "amount": 0.04, "price": 1205.1},
        {"timestamp": 200, "side": "buy", "amount": 0.02, "price": 1187.47},
        {"timestamp": 300, "side": "buy", "amount": 0.02, "price": 1190.17},
    ])
    fill = session._last_live_fill(t, "ZEC/USD:USD", 0)
    assert fill is not None, "letzter Fill fehlt"
    assert fill["price"] == 1190.17, f"fill={fill}"
    # PnL aus echtem Fill (SHORT): (1205.10-1190.17)/1205.10 ≈ 1.24%
    pnl = session._live_fill_pnl(1205.1, fill["price"], "SHORT")
    assert abs(pnl - 1.24) < 0.05, f"PnL={pnl:.2f} (erwartet ~1.24)"
    print(f"  OK: echter Fill 1190.17 → PnL +{pnl:.2f}% (statt fake +4.34%)")


def test_emit_exit_meldet_konto_stand():
    t = MockTrader()
    session._log_trade = lambda *a, **kw: None  # nicht ins echte Log schreiben
    # Telegram sendet nicht wirklich — nur aufbauen checken
    msgs = []
    orig_tg = session.tg
    session.tg = lambda m: msgs.append(m)
    try:
        session._emit_exit(
            "[LIVE]", "ny", "trailing_stop", "2/3", 100,
            "Trailing getriggert @ 1147.609500",
            1205.1, "ZEC/USD:USD", "ZEC", "SHORT",
            1190.17, 0.0, "   Konto:  493.00 USD", t,
        )
    finally:
        session.tg = orig_tg
    assert msgs, "keine Telegram-Meldung erzeugt"
    msg = msgs[-1]
    assert "1190.17" in msg, f"Fill-Preis fehlt in Meldung: {msg}"
    assert "+1.24%" in msg, f"PNL fehlt/wrong: {msg=}"
    assert "Konto:" in msg, f"Konto-Stand fehlt: {msg=}"
    assert "1152.82" not in msg, f"Fake-Preis in Meldung: {msg=}"
    assert "+4.34%" not in msg, f"Fake-PnL in Meldung: {msg=}"
    print("  OK: Meldung = ", msg.replace("\n", " | "))


def test_confirm_and_report_keine_position_nutzt_fill():
    t = MockTrader(
        fills=[
            {"timestamp": 100, "side": "sell", "amount": 0.04, "price": 1205.1},
            {"timestamp": 200, "side": "buy", "amount": 0.02, "price": 1190.17},
        ],
        close_success=False, close_message="Keine Position",
    )
    session._log_trade = lambda *a, **kw: None
    msgs = []
    orig_tg = session.tg
    session.tg = lambda m: msgs.append(m)
    try:
        fill_price = session._confirm_and_report(
            "[LIVE]", "ny", "trailing_stop", "2/3", 100,
            "Trailing getriggert @ 1152.82",
            1205.1, "ZEC/USD:USD", "ZEC", "SHORT",
            0.0, 0, "   Konto:  493.00 USD", t,
        )
    finally:
        session.tg = orig_tg
    assert fill_price == 1190.17, f"fill_price={fill_price}"
    msg = msgs[-1]
    assert "1190.17" in msg and "+1.24%" in msg, f"Fake erkannt: {msg=}"
    print(f"  OK: 'Keine Position' → echter Fill 1190.17 (+1.24%), kein Fake signal-price")


def test_account_str_zeigt_eur_usd():
    t = MockTrader()
    s = session._account_str(t)
    assert "492.00 USD" in s and "EUR 491.99" in s, f"Konto-String: {s}"
    print(f"  OK: Konto-String = {s}")


def test_partial_exit_liefert_echten_50pct_fill():
    """Profit-Lock (50%) muss den 50%-Fill liefern, NICHT den letzten Fill
    (der könnte der SL-Close der anderen Hälfte sein)."""
    class SeqTrader(MockTrader):
        def __init__(self):
            super().__init__()
            self._close_prices = iter([1187.47])  # 1. close = 50%-Fill
            self._stop_ok = True
        def set_stop_loss(self, symbol, side, stop_price, amount=None):
            return self._stop_ok
        def close_position(self, symbol, side, close_pct=1.0, **kw):
            from trader import TradeResult
            try:
                p = next(self._close_prices)
            except StopIteration:
                p = self._close_price
            return TradeResult(True, symbol, side, "f", p, 0, 0, "Closed 50%", "t")

    t = SeqTrader()
    status, error, partial_fill = session._execute_live_partial_exit(
        t, "ZEC/USD:USD", "SHORT", 1189.04
    )
    assert status == "protected", f"status={status}"
    assert partial_fill == 1187.47, f"partial_fill={partial_fill} (erwartet 1187.47, NICHT letzter Fill 1190.17)"
    assert error == "", f"error={error}"
    # PnL des 50%-Closes aus dem echten Fill
    pnl = session._live_fill_pnl(1205.1, partial_fill, "SHORT")
    assert abs(pnl - 1.46) < 0.05, f"PnL={pnl:.2f} (erwartet ~+1.46)"
    print(f"  OK: Partial-Exit liefert echten 50%-Fill 1187.47 → PnL +{pnl:.2f}% (statt 1190.17 / +1.24%)")


if __name__ == "__main__":
    for fn in (test_last_live_fill_uses_fill_price,
               test_emit_exit_meldet_konto_stand,
               test_confirm_and_report_keine_position_nutzt_fill,
               test_account_str_zeigt_eur_usd,
               test_partial_exit_liefert_echten_50pct_fill):
        fn()
    print("\nALLE TESTS OK ✅")