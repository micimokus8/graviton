#!/usr/bin/env python3
"""Tests für breadth.py — Market Breadth Sentiment (log-only).

Deckt ab: Richtungslogik (EMA9/EMA20), TF-Entscheidung, Aggregation,
Kandidaten-Universum (Anker + Top-N Volumen) und Datei-Persistenz.
Wichtig: breadth.py darf niemals filtern — der "log-only"-Vertrag
wird hier explizit geprüft.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import breadth


# ─── tf_direction ─────────────────────────────────────────────────

def _closes(values):
    return np.asarray(values, dtype=float)


def test_direction_bullish_when_price_above_emas():
    # Ansteigende Serie: Preis > EMA9 > EMA20
    closes = _closes([100 + i * 0.5 for i in range(40)])
    assert breadth.tf_direction(closes) == "BULLISH"


def test_direction_bearish_when_price_below_emas():
    closes = _closes([100 - i * 0.5 for i in range(40)])
    assert breadth.tf_direction(closes) == "BEARISH"


def test_direction_neutral_for_choppy():
    closes = _closes([100 + 2 * np.sin(i / 3) for i in range(40)])
    assert breadth.tf_direction(closes) == "NEUTRAL"


def test_direction_neutral_for_too_few_candles():
    closes = _closes([100 + i for i in range(10)])
    assert breadth.tf_direction(closes) == "NEUTRAL"


def test_direction_handles_empty():
    assert breadth.tf_direction(_closes([])) == "NEUTRAL"


# ─── _tf_decision ─────────────────────────────────────────────────

def test_tf_decision_long_majority():
    decision, detail = breadth._tf_decision(15, 3, 2)
    assert decision == "LONG"
    assert "15B/3S" in detail


def test_tf_decision_short_majority():
    decision, _ = breadth._tf_decision(2, 16, 2)
    assert decision == "SHORT"


def test_tf_decision_mixed_is_neutral():
    decision, _ = breadth._tf_decision(9, 11, 0)
    assert decision == "NEUTRAL"


def test_tf_decision_too_few_coins_unknown():
    decision, _ = breadth._tf_decision(4, 1, 0)
    assert decision == "UNKNOWN"


def test_tf_decision_no_data_unknown():
    decision, _ = breadth._tf_decision(0, 0, 0)
    assert decision == "UNKNOWN"


def test_tf_decision_mostly_neutral_is_neutral_not_unknown():
    """20 Coins Daten, davon 14 neutral — das ist NEUTRAL, kein UNKNOWN.
    UNKNOWN ist echten Datenlücken vorbehalten."""
    decision, detail = breadth._tf_decision(3, 3, 14)
    assert decision == "NEUTRAL"
    assert "gemischt" in detail


def test_tf_decision_all_neutral_is_neutral():
    decision, detail = breadth._tf_decision(0, 0, 20)
    assert decision == "NEUTRAL"
    assert "alle neutral" in detail


def test_tf_decision_few_active_coins_neutral():
    """Zu wenige aktive Richtungen trotz genug Daten → NEUTRAL."""
    decision, detail = breadth._tf_decision(2, 1, 17)
    assert decision == "NEUTRAL"
    assert "keine Mehrheit" in detail


# ─── aggregate_sentiment ──────────────────────────────────────────

def _directions(uni_n, bulls_per_tf):
    """Universum aus uni_n Symbolen; pro TF sind die ersten n_bull BULLISH,
    der Rest BEARISH. Vermeidet Überlappung zwischen TFs."""
    syms = [f"S{i}" for i in range(uni_n)]
    tfs = tuple(bulls_per_tf)
    out = {}
    for idx, sym in enumerate(syms):
        out[sym] = {}
        for tf in tfs:
            out[sym][tf] = "BULLISH" if idx < bulls_per_tf[tf] else "BEARISH"
    return out


def test_sentiment_long_when_two_tfs_long():
    directions = _directions(20, {"1h": 15, "4h": 14})
    result = breadth.aggregate_sentiment(directions)
    assert result["sentiment"] == "LONG"


def test_sentiment_short_when_two_tfs_short():
    directions = _directions(20, {"1h": 3, "4h": 4})
    result = breadth.aggregate_sentiment(directions)
    assert result["sentiment"] == "SHORT"


def test_sentiment_choppy_when_mixed():
    directions = _directions(20, {"1h": 15, "4h": 4})
    result = breadth.aggregate_sentiment(directions)
    assert result["sentiment"] == "CHOPPY"


def test_aggregate_output_shape():
    directions = _directions(20, {"1h": 15, "4h": 14})
    result = breadth.aggregate_sentiment(directions)
    assert result["universe"] > 0
    assert set(result["tfs"].keys()) == {"1h", "4h"}
    assert "log-only" in result["note"]


def test_log_only_contract_no_filter_fields():
    """Breadth-Output enthält keine Filter-/Block-Entscheidung."""
    directions = _directions(20, {"1h": 15, "4h": 14})
    result = breadth.aggregate_sentiment(directions)
    assert "allow" not in result and "block" not in result
    assert not result.get("filter_active", False)


# ─── candidate_symbols ────────────────────────────────────────────

def test_candidate_symbols_anchors_plus_top_volume():
    markets = [
        {"symbol": "AAA/USD:USD", "info": {"category": ""}},
        {"symbol": "BBB/USD:USD", "info": {"category": ""}},
        {"symbol": "STK/USD:USD", "info": {"category": "xStocks"}},
    ]
    tickers = {
        "AAA/USD:USD": {"quoteVolume": 1000.0},
        "BBB/USD:USD": {"quoteVolume": 100.0},
        "STK/USD:USD": {"quoteVolume": 999999.0},
    }
    symbols = breadth.candidate_symbols(markets, tickers, top_n=1)
    # BTC/ETH/SOL Anker + AAA (höchstes Volumen), aber NICHT STK (xStocks)
    assert "AAA/USD:USD" in symbols
    assert "STK/USD:USD" not in symbols
    assert "BTC/USD:USD" in symbols


def test_candidate_symbols_deduplicates():
    markets = [{"symbol": "BTC/USD:USD", "info": {"category": ""}}]
    tickers = {"BTC/USD:USD": {"quoteVolume": 99999.0}}
    symbols = breadth.candidate_symbols(markets, tickers, top_n=5)
    assert symbols.count("BTC/USD:USD") == 1


# ─── save_sentiment ───────────────────────────────────────────────

def test_save_and_load(tmp_path):
    sentiment = {"sentiment": "LONG", "universe": 20, "detail": "1H:12B/4S"}
    path = breadth.save_sentiment(sentiment, tmp_path / "ms.json")
    loaded = json.loads(path.read_text())
    assert loaded["sentiment"] == "LONG"
    assert loaded["universe"] == 20


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))