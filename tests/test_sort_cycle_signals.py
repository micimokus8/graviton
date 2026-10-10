#!/usr/bin/env python3
"""Test: Entry-Signale eines Zyklus werden nach Signalstärke sortiert.

Regressions-Test für den Bug vom 10.10.2026: Reine EMA-Distanz-Sortierung
ließ einen schwachen 2/3 SHORT (0.114% Distanz) einen starken 3/3 LONG
gewinnen, obwohl 5 LONG-Kandidaten existierten. Der Tie-Breaker muss
signal_count primär verwenden, EMA-Distanz nur als sekundärer Key.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session


def _cand(symbol: str, signal_count: int) -> dict:
    return {"symbol": symbol, "bias": "LONG", "signal_count": signal_count}


def _sig(distance_pct: float):
    class _S:
        pass

    s = _S()
    s.distance_pct = distance_pct
    return s


def test_3_3_beats_2_3_even_with_bigger_distance():
    """3/3 mit grösserer Distanz schlägt 2/3 mit kleinerer Distanz."""
    zro_short = (_cand("ZRO/USD:USD", 2), _sig(0.114))   # der alte Gewinner
    ada_long = (_cand("ADA/USD:USD", 2), _sig(0.183))
    near_long = (_cand("NEAR/USD:USD", 3), _sig(0.493))  # starker 3/3 LONG

    ordered = session._sort_cycle_signals([near_long, ada_long, zro_short])
    first = ordered[0][0]["symbol"]
    assert first == "NEAR/USD:USD", f"3/3 LONG muss zuerst kommen, war {first}"


def test_distance_is_tie_breaker_within_same_signal_count():
    """Innerhalb gleicher Signalstärke gewinnt der kleinere EMA-Abstand."""
    a = (_cand("A/USD:USD", 2), _sig(0.30))
    b = (_cand("B/USD:USD", 2), _sig(0.15))
    ordered = session._sort_cycle_signals([a, b])
    assert ordered[0][0]["symbol"] == "B/USD:USD"


def test_signal_count_primary_regardless_of_order():
    """Reihenfolge der Eingabe ändert das Ergebnis nicht."""
    weak_close = (_cand("WEAK/USD:USD", 2), _sig(0.05))
    strong_far = (_cand("STRONG/USD:USD", 3), _sig(1.20))
    ordered = session._sort_cycle_signals([weak_close, strong_far])
    assert ordered[0][0]["symbol"] == "STRONG/USD:USD"


def test_missing_signal_count_treated_as_zero():
    """Kandidat ohne signal_count (None/fehlend) wird als 0 behandelt."""
    no_count = ({"symbol": "X/USD:USD", "bias": "LONG"}, _sig(0.1))
    with_count = (_cand("Y/USD:USD", 3), _sig(0.9))
    ordered = session._sort_cycle_signals([no_count, with_count])
    assert ordered[0][0]["symbol"] == "Y/USD:USD"


if __name__ == "__main__":
    test_3_3_beats_2_3_even_with_bigger_distance()
    test_distance_is_tie_breaker_within_same_signal_count()
    test_signal_count_primary_regardless_of_order()
    test_missing_signal_count_treated_as_zero()
    print("alle Sortier-Tests ok")