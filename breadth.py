#!/usr/bin/env python3
"""graviton/breadth.py — Market Breadth Sentiment (log-only)

Berechnet ein marktweites Sentiment über alle relevanten Kraken Perps
(1H / 4H / 1D) und schreibt es als Kontext in data/market_sentiment.json.

WICHTIG: Reines Beobachtungs-/Logging-Modul. Es filtert, blockt oder
verändert KEINE Trades. Die Session hängt das Ergebnis nur als zusätzliches
Feld an Trade-Log-Einträge (market_sentiment) — der Filter wird erst nach
einer historischen Auswertung (Baustein 3) scharf geschaltet.

Logik:  pro Coin pro Timeframe wird die EMA9/EMA20-Richtung bestimmt
        (identische Regel wie bias._tf_signal). Pro Timeframe wird die
        Mehrheit über das Universum gezählt. Sentiment:
          - LONG  wenn ≥2 von 3 TFs bullish_ratio ≥ 0.65 (und bull ≥ min)
          - SHORT wenn ≥2 von 3 TFs bearish_ratio ≥ 0.65 (und bear ≥ min)
          - CHOPPY wenn kein klares Bild
          - UNKNOWN wenn zu wenig Daten (kein sicheres Signal)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from atomic_json import atomic_write_json

# ─── Konfiguration ──────────────────────────────────────────────────

# Nur 1H + 4H: Der 1D ist für die 3,5h-NY-Session zu träge (ändert sich
# 1×/Tag, war in 11/14 Backtest-Tagen "alles bullish" → keine Trennschärfe,
# verwässert das Signal). Backtest 1H+4H war netto besser als 1H+4H+1D
# (+4.41% vs. +2.58%): gleiche Verlust-Rettung, ein Gewinner mehr behalten.
TIMEFRAMES = ("1h", "4h")
ANCHORS = ("BTC/USD:USD", "ETH/USD:USD", "SOL/USD:USD")
TOP_N = 20                # zusätzliche volumenstärkste Perps
MIN_COINS_TF = 8          # unter dieser Mindestzahl gilt ein TF als UNKNOWN
MIN_BIAS_COUNT = 5        # Mindestanzahl für eine "starke" Richtung pro TF
BULLISH_RATIO = 0.65      # Mehrheitsschwelle pro TF
DATA_FILE = Path(__file__).parent / "data" / "market_sentiment.json"


# ─── Pure Logik (testbar ohne API) ─────────────────────────────────

def _ema(closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) < period:
        return np.full_like(closes, np.nan)
    alpha = 2.0 / (period + 1)
    ema = np.full_like(closes, np.nan)
    ema[period - 1] = float(np.mean(closes[:period]))
    for i in range(period, len(closes)):
        ema[i] = alpha * float(closes[i]) + (1 - alpha) * ema[i - 1]
    return ema


def tf_direction(closes: np.ndarray) -> str:
    """Richtung eines Timeframes: BULLISH / BEARISH / NEUTRAL.

    Identische EMA9/EMA20-Regel wie bias.py — Preis über EMA9 über EMA20
    = bullisch, darunter = bärisch, sonst neutral. Weniger als 25 Kerzen
    = NEUTRAL (kein Signal, aber kein Datenfehler).
    """
    if closes is None or len(closes) < 25:
        return "NEUTRAL"
    price = float(closes[-1])
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    if np.isnan(ema9[-1]) or np.isnan(ema20[-1]):
        return "NEUTRAL"
    if price > ema9[-1] > ema20[-1]:
        return "BULLISH"
    if price < ema9[-1] < ema20[-1]:
        return "BEARISH"
    return "NEUTRAL"


def _tf_decision(bulls: int, bears: int, neutrals: int) -> tuple[str, str]:
    """Entscheidung für einen Timeframe + kurze Begründung.

    Gibt (decision, detail) zurück: "LONG"/"SHORT"/"NEUTRAL"/"UNKNOWN".
    UNKNOWN ist ausschließlich echten Datenlücken vorbehalten (zu wenige
    Coins insgesamt). Viele NEUTRAL-Coins bei genug Daten sind dagegen
    eine echte Information → NEUTRAL (kein klares Signal).
    """
    total = bulls + bears + neutrals
    if total == 0:
        return "UNKNOWN", "keine Daten"
    if total < MIN_COINS_TF:
        return "UNKNOWN", f"nur {total} Coins (<{MIN_COINS_TF})"
    if bulls + bears == 0:
        return "NEUTRAL", f"{total} Coins alle neutral"
    active = bulls + bears
    if active < MIN_BIAS_COUNT:
        return "NEUTRAL", f"{bulls}B/{bears}S ({active} aktiv, keine Mehrheit)"
    bullish_ratio = bulls / active
    if bullish_ratio >= BULLISH_RATIO:
        return "LONG", f"{bulls}B/{bears}S ({bullish_ratio:.0%})"
    if bullish_ratio <= 1 - BULLISH_RATIO:
        return "SHORT", f"{bulls}B/{bears}S ({bullish_ratio:.0%})"
    return "NEUTRAL", f"{bulls}B/{bears}S/{neutrals}N gemischt"


def aggregate_sentiment(
    directions: dict[str, dict[str, str]],
    timeframes: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """Aggregiert {symbol: {tf: direction}} zu Market-Sentiment.

    Gibt ein JSON-sicheres dict mit sentiment + tfs-Details zurück.
    `timeframes` erlaubt eine Teilmenge (z.B. nur 1h+4h für Daytrading);
    Standard sind alle TIMEFRAMES. Leere/ohne Daten → CHOPPY (kein Signal).
    """
    tfs: tuple[str, ...] = tuple(timeframes) if timeframes else TIMEFRAMES

    tf_stats: dict[str, dict[str, int]] = {}
    for tf in tfs:
        tf_stats[tf] = {"bullish": 0, "bearish": 0, "neutral": 0}
    for symbol, tf_map in directions.items():
        for tf, direction in tf_map.items():
            if tf not in tf_stats:
                continue
            key = direction.lower() if direction.lower() in tf_stats[tf] else "neutral"
            tf_stats[tf][key] += 1

    tf_decisions = {}
    long_tfs = short_tfs = 0
    for tf in tfs:
        stats = tf_stats[tf]
        decision, detail = _tf_decision(
            stats["bullish"], stats["bearish"], stats["neutral"]
        )
        tf_decisions[tf] = {
            "decision": decision, "detail": detail, **stats,
        }
        if decision == "LONG":
            long_tfs += 1
        elif decision == "SHORT":
            short_tfs += 1

    # Mindest-Konsens: Mehrheit der gewerteten Timeframes (≥2 von 3, bzw.
    # beide bei 2 TFs). Ohne klare Mehrheit → CHOPPY.
    required = max(2, (len(tfs) + 1) // 2)
    if long_tfs >= min(required, len(tfs)):
        sentiment = "LONG"
    elif short_tfs >= min(required, len(tfs)):
        sentiment = "SHORT"
    else:
        sentiment = "CHOPPY"

    detail_str = " ".join(
        f"{tf.upper()}:{tf_decisions[tf]['detail']}" for tf in tfs
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sentiment": sentiment,
        "universe": len(directions),
        "long_tfs": long_tfs,
        "short_tfs": short_tfs,
        "tfs": tf_decisions,
        "detail": detail_str,
        "note": "log-only beobachtend — kein Trade-Filter aktiv",
    }


def candidate_symbols(
    markets: list[dict],
    tickers: dict,
    anchors: tuple[str, ...] = ANCHORS,
    top_n: int = TOP_N,
) -> list[str]:
    """Aus Perp-Markets + Tickern das Scan-Universum bestimmen.

    Anker (BTC/ETH/SOL) immer, plus die top_n volumenstärksten liquiden
    Perps nach 24h-quoteVolume. Gibt deduplizierte Symbol-Liste zurück.
    """
    volumes: list[tuple[str, float]] = []
    for market in markets:
        symbol = market.get("symbol", "")
        if not symbol:
            continue
        if market.get("info", {}).get("category") == "xStocks":
            continue
        ticker = tickers.get(symbol) or {}
        vol = float(ticker.get("quoteVolume", 0) or 0)
        if vol <= 0:
            base_vol = float(ticker.get("baseVolume", 0) or 0)
            last = float(ticker.get("last", 0) or 0)
            vol = base_vol * last
        volumes.append((symbol, vol))
    volumes.sort(key=lambda item: item[1], reverse=True)
    symbols = list(anchors)
    for symbol, _vol in volumes[:top_n]:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


# ─── API-Logik (IO) ────────────────────────────────────────────────

def scan_sentiment(exchange=None) -> dict:
    """Holt OHLCV fürs Universum und liefert das Sentiment-dict.

    Fehlerhafte Coins werden übersprungen (fail-soft); UNKNOWN entsteht
    nur, wenn insgesamt zu wenig Daten vorliegen.
    """
    if exchange is None:
        import ccxt
        exchange = ccxt.krakenfutures({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        exchange.load_markets()

    markets = [
        market for symbol, market in exchange.markets.items()
        if market.get("linear") and market.get("settle") == "USD"
        and market.get("type") == "swap" and market.get("active", True)
    ]
    try:
        tickers = exchange.fetch_tickers([m["symbol"] for m in markets])
    except Exception:
        tickers = {}

    symbols = candidate_symbols(markets, tickers)

    directions: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        tf_map: dict[str, str] = {}
        for tf in TIMEFRAMES:
            try:
                raw = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
                data = np.asarray(raw, dtype=float)
                closes = data[:, 4]
                tf_map[tf] = tf_direction(closes)
            except Exception:
                tf_map[tf] = "NEUTRAL"
        directions[symbol] = tf_map

    return aggregate_sentiment(directions)


def save_sentiment(sentiment: dict, path: Path | str = DATA_FILE) -> Path:
    """Atomar schreiben — Trade-Logs lesen diese Datei als Kontext."""
    atomic_write_json(path, sentiment)
    return Path(path)


# ─── CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        result = scan_sentiment()
        save_sentiment(result)
        print(
            f"[{datetime.now(timezone.utc).strftime('%H:%M UTC')}] "
            f"MARKET_SENTIMENT: {result['sentiment']} "
            f"(universe {result['universe']}) — {result['detail']}"
        )
    except Exception as exc:
        print(f"Breadth-Scan fehlgeschlagen: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)