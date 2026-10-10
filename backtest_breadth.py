#!/usr/bin/env python3
"""graviton/backtest_breadth.py — Historische Breadth-Auswertung (Baustein 3)

Fragestellung (log-only, kein Filter):
  1. Lagen die SL-Verluste der LIVE-Trades gegen das damalige Markt-Sentiment?
  2. Wären die Gewinner durch einen Breadth-Filter rausgeflogen?

Methode (ohne Lookahead-Bias):
  - LIVE-Entries aus data/trade_log.jsonl (mode=[LIVE]) seit 27.09.
  - OHLCV für das Universum (BTC/ETH/SOL + aktuelle Top-20-Perps) wird
    EINMAL geholt (seit dem ersten Trade-Zeitpunkt minus Puffer), dann pro
    Trade-Zeitpunkt mit closed_ohlcv(now_ms=entry_ts) auf die damals bereits
    geschlossenen Kerzen gefiltert — identische Logik wie breadth.py.
  - Bias des Trades vs. Market-Sentiment zum Entry-Zeitpunkt.

Report-Ausgabe (stdout): Tabelle pro Trade + Zusammenfassung:
  - Verluste gegen Sentiment
  - Gewinner, die ein Filter entfernt hätte (Opportunitätskosten)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from breadth import (
    TIMEFRAMES,
    ANCHORS,
    TOP_N,
    tf_direction,
    aggregate_sentiment,
)
from candle_utils import closed_ohlcv

DATA_DIR = Path(__file__).parent / "data"
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"
LIVE_SINCE = "2026-09-27T00:00:00+00:00"

# OHLCV-Limits pro TF: großzügig, damit für den ältesten Trade (27.09.)
# genügend geschlossene Kerzen vor dem Entry existieren (tf_direction
# braucht ≥25). closed_ohlcv(now_ms=entry) entfernt danach alles, was
# erst NACH dem Entry kam (kein since-Parameter nötig → Exchange-agnostisch).
FETCH_LIMITS = {"1h": 500, "4h": 250, "1d": 80}


# ─── Trade-Liste aus dem Log ───────────────────────────────────────

def load_live_trades(path: Path = TRADE_LOG_FILE) -> list[dict]:
    """LIVE-Entries (seit 27.09.) + gewichteter Exit-PnL aus den Exits.

    Ein Trade = Entry-Event. Gewichteter PnL = Σ(close_pct/100 × pnl_pct)
    über alle zugehörigen Exit-Events (50% @ +1%, 50% @ trailing usw.).
    """
    entries: list[dict] = []
    exit_by_symbol: dict[str, list[dict]] = {}
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event["event"] == "entry" and event.get("mode") == "[LIVE]":
                ts = event["timestamp"]
                if ts >= LIVE_SINCE:
                    entries.append(event)
            elif event["event"] == "exit":
                base = event.get("base") or event["symbol"].split("/")[0]
                exit_by_symbol.setdefault(base, []).append(event)

    trades = []
    for entry in entries:
        base = entry.get("base") or entry["symbol"].split("/")[0]
        entry_ts = datetime.fromisoformat(entry["timestamp"])
        entry_ms = int(entry_ts.timestamp() * 1000)

        related_exits = [
            x for x in exit_by_symbol.get(base, [])
            if datetime.fromisoformat(x["timestamp"]) >= entry_ts
        ]
        total_pnl = 0.0
        for x in related_exits:
            pct = float(x.get("close_pct", 100) or 100) / 100.0
            pnl = float(x.get("pnl_pct", 0.0) or 0.0)
            total_pnl += pct * pnl

        trades.append({
            "symbol": entry["symbol"],
            "base": base,
            "bias": entry.get("bias", "?"),
            "entry_ts": entry_ts,
            "entry_ms": entry_ms,
            "entries_exits": len(related_exits),
            "pnl": round(total_pnl, 3),
        })
    return trades


# ─── OHLCV-Beschaffung (einmal, seit FETCH_SINCE) ───────────────────

def fetch_once(
    exchange, symbols: list[str], timeframes: tuple[str, ...] = TIMEFRAMES,
) -> dict[str, dict[str, np.ndarray]]:
    """Holt OHLCV für alle Coins/TFs einmal. Struktur {symbol: {tf: array}}.

    Kein since-Parameter: wir holen die letzten FETCH_LIMITS Kerzen pro TF
    und überlassen closed_ohlcv(now_ms=entry_ms) das Zeitfenster-Filtern.
    Fail-soft: ein Coin/TF-Fehler überspringt nur diesen Eintrag.
    """
    result: dict[str, dict[str, np.ndarray]] = {}
    for symbol in symbols:
        result[symbol] = {}
        for tf in timeframes:
            limit = FETCH_LIMITS.get(tf, 100)
            try:
                raw = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
                result[symbol][tf] = np.asarray(raw, dtype=float)
            except Exception:
                result[symbol][tf] = np.empty((0, 6), dtype=float)
    return result


# ─── Per-Trade- Sentiment (nur geschlossene Kerzen zum Entry) ──────

def sentiment_at(
    ohlcv: dict[str, dict[str, np.ndarray]],
    entry_ms: int,
    timeframes: tuple[str, ...] = TIMEFRAMES,
) -> dict:
    """Berechnet das Breadth-Sentiment zum Entry-Zeitpunkt.

    Nutzt closed_ohlcv(now_ms=entry_ms) → nur Kerzen, die vor dem Entry
    bereits geschlossen waren. Identische Aggregation wie breadth.py.
    """
    directions: dict[str, dict[str, str]] = {}
    for symbol, tf_map in ohlcv.items():
        tf_dir: dict[str, str] = {}
        for tf in timeframes:
            data = tf_map.get(tf)
            if data is None or len(data) == 0:
                tf_dir[tf] = "NEUTRAL"
                continue
            closed = closed_ohlcv(data, tf, now_ms=entry_ms)
            tf_dir[tf] = tf_direction(closed[:, 4] if len(closed) else np.array([]))
        directions[symbol] = tf_dir
    return aggregate_sentiment(directions, timeframes=timeframes)


# ─── Universum ─────────────────────────────────────────────────────

def build_universe(exchange) -> list[str]:
    """BTC/ETH/SOL + aktuelle Top-20-Perps nach Volumen (stabil genug).

    Verwendet scan-artige Filter (linear, USD-settle, swap, kein xStocks).
    """
    import ccxt
    if exchange is None:
        exchange = ccxt.krakenfutures({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        exchange.load_markets()
    markets = [
        market for symbol, market in exchange.markets.items()
        if market.get("linear") and market.get("settle") == "USD"
        and market.get("type") == "swap" and market.get("active", True)
        and market.get("info", {}).get("category") != "xStocks"
    ]
    try:
        tickers = exchange.fetch_tickers([m["symbol"] for m in markets])
    except Exception:
        tickers = {}
    volumes = []
    for market in markets:
        symbol = market["symbol"]
        ticker = tickers.get(symbol) or {}
        vol = float(ticker.get("quoteVolume", 0) or 0)
        if vol <= 0:
            vol = float(ticker.get("baseVolume", 0) or 0) * float(ticker.get("last", 0) or 0)
        volumes.append((symbol, vol))
    volumes.sort(key=lambda item: item[1], reverse=True)
    symbols = list(ANCHORS)
    for symbol, _vol in volumes[:TOP_N]:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


# ─── Report ────────────────────────────────────────────────────────

def build_report(
    trades: list[dict], universe: list[str],
    timeframes: tuple[str, ...] | None = None,
) -> str:
    """Baut den Text-Report aus den fertigen Trades (mit sentiment=True)."""
    lines = []
    lines.append("═" * 78)
    lines.append("  Graviton Baustein 3 — Breadth-Auswertung LIVE-Trades (seit 27.09.)")
    lines.append("═" * 78)
    lines.append(f"  Universum: {len(universe)} Coins (BTC/ETH/SOL + Top-20)")
    if timeframes:
        lines.append(f"  Timeframes: {', '.join(timeframes)}")
    lines.append("")

    header = (f"  {'Datum':<12}{'Coin':<10}{'Bias':<6}{'Sent':<7}"
              f"{'Match':<7}{'PnL':>8}   Verlauf")
    lines.append(header)
    lines.append("  " + "-" * 72)

    against_losses = 0
    would_have_removed_wins = 0
    total_losses = 0
    total_wins = 0
    filtered_win_pnl = 0.0
    saved_loss_pnl = 0.0

    for trade in trades:
        day = trade["entry_ts"].strftime("%d.%m.")
        sentiment = trade.get("sentiment", "?")
        match = trade.get("match", "?")
        pnl = trade["pnl"]
        pnl_s = f"{pnl:+.2f}%"

        if match == "GEGEN":
            if pnl < 0:
                against_losses += 1
                saved_loss_pnl += abs(pnl)
            else:
                would_have_removed_wins += 1
                filtered_win_pnl += pnl
        if pnl < 0:
            total_losses += 1
        else:
            total_wins += 1

        verlauf = trade.get("detail", "")
        lines.append(
            f"  {day:<12}{trade['base']:<10}{trade['bias']:<6}"
            f"{sentiment:<7}{match:<7}{pnl_s:>8}   {verlauf}"
        )

    lines.append("")
    lines.append("─" * 78)
    loss_line = (
        f"  Verluste GEGEN das Sentiment: {against_losses}/{total_losses} "
        f"(gerettet: +{saved_loss_pnl:.2f}% auf {against_losses} Trade(s))"
    )
    win_line = (
        f"  Gewinner, die ein Filter entfernt hätte: {would_have_removed_wins}/{total_wins} "
        f"(entgangen: {filtered_win_pnl:+.2f}%)"
    )
    netto = saved_loss_pnl - filtered_win_pnl
    detail = (
        f"  Netto-Wirkung eines GEGEN-Filters: {netto:+.2f}% über {len(trades)} Trades"
    )
    if netto > 0:
        verdict = "  → Filter hätte netto geholfen (positive Bilanz)"
    elif netto < 0:
        verdict = "  → Filter hätte netto geschadet (Gewinner wichtiger)"
    else:
        verdict = "  → Filter wäre wirkungslos gewesen"
    lines += [loss_line, win_line, detail, verdict]
    lines.append("─" * 78)
    lines.append("  Hinweis: log-only Auswertung, kein aktiver Filter.")
    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────

def main(max_trades: int | None = None, verbose: bool = False,
         timeframes: tuple[str, ...] = TIMEFRAMES):
    import ccxt
    trades = load_live_trades()
    if max_trades:
        trades = trades[:max_trades]
    if not trades:
        print("Keine LIVE-Trades gefunden.")
        return

    exchange = ccxt.krakenfutures({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    exchange.load_markets()
    universe = build_universe(exchange)
    ohlcv = fetch_once(exchange, universe, timeframes=timeframes)

    for trade in trades:
        sent = sentiment_at(ohlcv, trade["entry_ms"], timeframes=timeframes)
        trade["sentiment"] = sent["sentiment"]
        # Match: Bias == Sentiment-Polarität
        if sent["sentiment"] == "CHOPPY" or sent["sentiment"] == "UNKNOWN":
            trade["match"] = "~"
        elif (trade["bias"] == "LONG" and sent["sentiment"] == "LONG") or \
             (trade["bias"] == "SHORT" and sent["sentiment"] == "SHORT"):
            trade["match"] = "PASST"
        else:
            trade["match"] = "GEGEN"
        trade["detail"] = sent["detail"]
        if verbose:
            print(f"  {trade['base']:8s} {trade['bias']:5s} → "
                  f"{sent['sentiment']:7s} {sent['detail']}")

    print(build_report(trades, universe, timeframes=timeframes))
    print()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    limit = None
    verbose = False
    tfs = TIMEFRAMES
    if "--verbose" in args or "-v" in args:
        verbose = True
    for a in args:
        if a.startswith("--limit="):
            limit = int(a.split("=")[1])
        if a.startswith("--tfs="):
            tfs = tuple(t.strip().lower() for t in a.split("=")[1].split(","))
    main(max_trades=limit, verbose=verbose, timeframes=tfs)