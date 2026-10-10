#!/usr/bin/env python3
"""Integrationstest: session._log_trade hängt Markt-Sentiment an Trade-Logs.

Verwendet tmp-Verzeichnisse für trade_log und market_sentiment, damit die
Produktionsdaten (data/trade_log.jsonl) unangetastet bleiben. Prüft:
  - Sentiment-Felder werden angedockt, wenn frische Datei existiert
  - Fail-soft: ohne Datei kein Crash, keine Felder
  - Exit-Einträge tragen signal_count/dist_pct vom Entry
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TRADE_LOG_FILE", tmp_path / "trade_log.jsonl")
    monkeypatch.setattr(session, "MARKET_SENTIMENT_FILE", tmp_path / "market_sentiment.json")
    monkeypatch.setattr(session, "_entry_meta", {})
    return tmp_path


def test_no_sentiment_file_no_crash(isolated):
    session._log_trade("entry", symbol="X/USD:USD", base="X", bias="LONG")
    lines = isolated.joinpath("trade_log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "market_sentiment" not in entry  # fail-soft: kein Feld


def test_stale_sentiment_file_ignored(isolated):
    sent = {"sentiment": "LONG", "detail": "1H:15B/2S"}
    path = isolated / "market_sentiment.json"
    path.write_text(json.dumps(sent))
    # Datei uralt machen (älter als 6h)
    old = 7 * 60 * 60
    os.utime(path, (old, old))
    session._log_trade("entry", symbol="X/USD:USD", base="X", bias="LONG")
    entry = json.loads(isolated.joinpath("trade_log.jsonl").read_text().splitlines()[0])
    assert "market_sentiment" not in entry


def test_fresh_sentiment_attached(isolated):
    sent = {"sentiment": "SHORT", "detail": "1D:4B/12S"}
    isolated.joinpath("market_sentiment.json").write_text(json.dumps(sent))
    session._log_trade("entry", symbol="X/USD:USD", base="X", bias="SHORT")
    entry = json.loads(isolated.joinpath("trade_log.jsonl").read_text().splitlines()[0])
    assert entry["market_sentiment"] == "SHORT"
    assert entry["market_breadth"] == "1D:4B/12S"


def test_exit_carries_entry_context(isolated):
    session._entry_meta = {"signal_count": 3, "dist_pct": 0.42}
    session._log_trade("exit", symbol="X/USD:USD", base="X", bias="LONG",
                       reason="stop_loss", close_pct=100, pnl_pct=-1.5,
                       price=1.0, rsi=0, session="NY")
    entry = json.loads(isolated.joinpath("trade_log.jsonl").read_text().splitlines()[0])
    assert entry["signal_count"] == 3
    assert entry["dist_pct"] == 0.42


def test_entry_logs_signal_count_and_dist(isolated):
    session._log_trade("entry", symbol="X/USD:USD", base="X", bias="LONG",
                       price=1.0, ema20=1.0, stop_loss=0.99, mode="[LIVE]",
                       session="NY", vol_ratio=1.2, signal_count=2, dist_pct=0.3)
    entry = json.loads(isolated.joinpath("trade_log.jsonl").read_text().splitlines()[0])
    assert entry["signal_count"] == 2
    assert entry["dist_pct"] == 0.3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))