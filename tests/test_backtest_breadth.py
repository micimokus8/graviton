#!/usr/bin/env python3
"""Tests für backtest_breadth.py — Trade-Extraktion + Sentiment-Kernlogik.

Nutzt synthetische OHLCV-Daten und ein temp trade_log, damit nichts
gegen die echte API oder die Produktionsdaten läuft.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest_breadth as bb


# ─── Trade-Extraktion ──────────────────────────────────────────────

def _entry_ts(day: str, time: str = "12:50"):
    return f"2026-{day}T{time}:00+00:00"


def _write_log(path, events):
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_load_live_trades_filters_dry_run_and_matches_exits(tmp_path):
    events = [
        {"timestamp": "2026-09-25T12:50:00+00:00", "event": "entry",
         "symbol": "OLD/USD:USD", "base": "OLD", "bias": "LONG", "mode": "[DRY RUN]"},
        {"timestamp": "2026-09-28T12:50:00+00:00", "event": "entry",
         "symbol": "AAA/USD:USD", "base": "AAA", "bias": "LONG", "mode": "[LIVE]"},
        # Exit: nur 50% des Trades
        {"timestamp": "2026-09-28T13:10:00+00:00", "event": "exit",
         "symbol": "AAA/USD:USD", "base": "AAA", "reason": "profit_lock",
         "close_pct": 50, "pnl_pct": 1.0},
        # Exit: Rest 50% mit Verlust
        {"timestamp": "2026-09-28T14:00:00+00:00", "event": "exit",
         "symbol": "AAA/USD:USD", "base": "AAA", "reason": "stop_loss",
         "close_pct": 100, "pnl_pct": -0.5},
    ]
    path = tmp_path / "trade_log.jsonl"
    _write_log(path, events)
    trades = bb.load_live_trades(path)

    assert len(trades) == 1, f"DRY_RUN darf nicht zählen: {trades}"
    trade = trades[0]
    assert trade["base"] == "AAA"
    assert trade["bias"] == "LONG"
    # Gewichteter PnL: 50% × 1.0 + 100% × (-0.5) = 0.5 - 0.5 = 0.0
    assert abs(trade["pnl"] - 0.0) < 0.001, f"pnl={trade['pnl']}"
    assert trade["entries_exits"] == 2


def test_load_live_trades_requires_live_mode(tmp_path):
    events = [
        {"timestamp": "2026-09-28T12:50:00+00:00", "event": "entry",
         "symbol": "X/USD:USD", "base": "X", "bias": "SHORT", "mode": "[DRY RUN]"},
    ]
    path = tmp_path / "trade_log.jsonl"
    _write_log(path, events)
    assert bb.load_live_trades(path) == []


# ─── Sentiment-Zeitfenster (Lookahead-Schutz) ──────────────────────

def _ohlcv_array(start_ms, step_ms, n, direction=1):
    """Erzeugt n OHLCV-Kerzen: steigend (direction=1) oder fallend (-1)."""
    rows = []
    price = 100.0
    for i in range(n):
        ts = start_ms + i * step_ms
        o = price
        c = price + direction * 0.5
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        rows.append([ts, o, h, l, c, 1000.0])
        price = c
    return np.asarray(rows, dtype=float)


def test_fetch_once_structure():
    class FakeEx:
        def fetch_ohlcv(self, symbol, timeframe="", since=None, limit=None):
            return [[1, 2, 3, 4, 5, 6]]

    result = bb.fetch_once(FakeEx(), ["BTC/USD:USD"])
    assert "BTC/USD:USD" in result
    assert set(result["BTC/USD:USD"].keys()) == {"1h", "4h"}


def test_sentiment_at_uses_only_closed_candles():
    """Sentiment zum Entry muss die bis dahin geschlossenen Kerzen nutzen."""
    # 1h-Kerzen: 24 fallende (ab 30h vor Entry, alle geschlossen) + 3
    # steigende, deren Open-Zeit NACH dem Entry liegt (dürfen nicht zählen).
    entry_ms = int(datetime(2026, 9, 27, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
    start = entry_ms - 30 * 60 * 60 * 1000  # 30 Stunden vor Entry
    step = 60 * 60 * 1000
    falls = _ohlcv_array(start, step, 28, direction=-1)
    rises_after_entry = _ohlcv_array(entry_ms + step, step, 3, direction=1)
    data = np.concatenate([falls, rises_after_entry])

    # Direkt über die Breadth-Funktion prüfen (nicht den Fake-Sentiment-Pfad)
    closed = bb.closed_ohlcv(data, "1h", now_ms=entry_ms)
    # Nur die 28 fallenden Kerzen dürfen übrig sein (letzte offene ignoriert)
    assert len(closed) == 28, f"closed={len(closed)} (erwartet 28)"

    direction = bb.tf_direction(closed[:, 4])
    assert direction == "BEARISH", f"closed-Kerzen fallend → BEARISH, war {direction}"


def test_sentiment_at_aggregates_across_universe():
    """Synthetisches Universum: 15 BULLISH + 5 BEARISH auf 1h+4h → LONG."""
    ohlcv = {}
    entry_ms = int(datetime(2026, 9, 28, 12, 50, tzinfo=timezone.utc).timestamp() * 1000)
    start = entry_ms - 30 * 24 * 60 * 60 * 1000  # 30 Tage vorher

    for i in range(15):
        ohlcv[f"BULL{i}/USD:USD"] = {
            "1h": _ohlcv_array(start, 60 * 60 * 1000, 60, direction=1),
            "4h": _ohlcv_array(start, 4 * 60 * 60 * 1000, 60, direction=1),
        }
    for i in range(5):
        ohlcv[f"BEAR{i}/USD:USD"] = {
            "1h": _ohlcv_array(start, 60 * 60 * 1000, 60, direction=-1),
            "4h": _ohlcv_array(start, 4 * 60 * 60 * 1000, 60, direction=-1),
        }
    sent = bb.sentiment_at(ohlcv, entry_ms)
    assert sent["sentiment"] == "LONG", f"sent={sent['sentiment']}"


def test_build_report_shape():
    trades = [
        {"entry_ts": datetime(2026, 10, 5, 12, 50, tzinfo=timezone.utc),
         "base": "FART", "bias": "LONG", "pnl": -1.61,
         "sentiment": "SHORT", "match": "GEGEN", "detail": "1H:4B/12S"},
        {"entry_ts": datetime(2026, 10, 4, 12, 50, tzinfo=timezone.utc),
         "base": "SAND", "bias": "LONG", "pnl": +1.94,
         "sentiment": "LONG", "match": "PASST", "detail": "1H:15B/2S"},
        {"entry_ts": datetime(2026, 10, 6, 12, 50, tzinfo=timezone.utc),
         "base": "WIN", "bias": "LONG", "pnl": +2.5,
         "sentiment": "SHORT", "match": "GEGEN", "detail": "1H:3B/10S"},
    ]
    report = bb.build_report(trades, ["BTC/USD:USD", "ETH/USD:USD"])
    assert "GEGEN" in report
    assert "PASST" in report
    # 1 Verlust GEGEN (-1.61 gerettet), 1 Gewinner GEGEN (+2.5 entgangen)
    assert "Verluste GEGEN das Sentiment: 1/1" in report
    assert "gerettet: +1.61%" in report
    assert "Gewinner, die ein Filter entfernt hätte: 1/2" in report
    assert "entgangen: +2.50%" in report
    # Netto: +1.61 - 2.50 = -0.89
    assert "Netto-Wirkung eines GEGEN-Filters: -0.89%" in report
    assert "Filter hätte netto geschadet" in report


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))