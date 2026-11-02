#!/usr/bin/env python3
"""
graviton/scripts/bias_backtest.py — Trifft der 1H-Bias die Session-Richtung?

Fuer jeden Handelstag wird der Bias so berechnet, wie ihn der Cron um 13:25 UTC
sieht (nur Kerzen, die zu diesem Zeitpunkt geschlossen sind), und mit der
tatsaechlichen Bewegung von 13:30 bis 20:00 UTC verglichen.

    python3 scripts/bias_backtest.py                 # alle Aktien-Perps
    python3 scripts/bias_backtest.py --group xstocks
"""

from __future__ import annotations

import argparse
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

import ccxt
import numpy as np

XSTOCKS = ["SPCXX", "AAPLX", "NVDAX", "TSLAX", "SPYX", "QQQX", "AMZNX", "METAX",
           "GOOGLX", "CRCLX", "COINX", "MSTRX", "HOODX", "GLDX"]
EQUITIES = ["AMD", "INTC", "PLTR", "MU", "SOXL", "SOXS", "MRVL", "NBIS",
            "SNDK", "SAMSUNG", "SKHY", "EWY", "EWZ"]
CRYPTO = ["BTC", "ETH", "SOL", "XRP"]

BIAS_AT_UTC = 13 * 60 + 25      # 13:25 UTC — Zeitpunkt des Bias-Cron
SESSION_OPEN_UTC = 13 * 60 + 30
SESSION_CLOSE_UTC = 20 * 60


def ema(values: np.ndarray, period: int) -> np.ndarray:
    a = 2.0 / (period + 1)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = a * values[i] + (1 - a) * out[i - 1]
    return out


def bias_slope(closes: np.ndarray) -> str:
    """Alternative: EMA20-Steigung (5 Bars) statt EMA9/20-Kreuz."""
    e20 = ema(closes, 20)
    return "BULLISH" if e20[-1] > e20[-5] else "BEARISH"


def bias_momentum(closes: np.ndarray) -> str:
    """Alternative: reine 6-Bar-Momentum-Richtung."""
    return "BULLISH" if closes[-1] > closes[-6] else "BEARISH"


def bias_at(closes: np.ndarray) -> str:
    """Die 1H-Regel aus bias._tf_signal, auf geschlossenen Kerzen."""
    e9, e20 = ema(closes, 9)[-1], ema(closes, 20)[-1]
    price = closes[-1]
    last3 = closes[-3:]
    rising = bool(all(last3[i] < last3[i + 1] for i in range(2)))
    falling = bool(all(last3[i] > last3[i + 1] for i in range(2)))
    if price > e9 and e9 > e20:
        return "BULLISH"
    if e9 > e20 and rising:
        return "BULLISH"
    if price < e9 and e9 < e20:
        return "BEARISH"
    if e9 < e20 and falling:
        return "BEARISH"
    return "NEUTRAL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["stocks", "xstocks", "equities", "crypto"],
                    default="stocks")
    ap.add_argument("--rule", choices=["ema9_20", "slope", "momentum"], default="ema9_20")
    args = ap.parse_args()
    bases = {"stocks": XSTOCKS + EQUITIES, "xstocks": XSTOCKS,
             "equities": EQUITIES, "crypto": CRYPTO}[args.group]

    ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    hits = defaultdict(lambda: {"hit": 0, "n": 0, "long": 0, "short": 0})
    rows = []

    for base in bases:
        sym = f"{base}/USD:USD"
        try:
            bars = ex.fetch_ohlcv(sym, "1h", limit=1000)
        except Exception as exc:                       # noqa: BLE001
            print(f"{base:10s} FETCH-ERR {type(exc).__name__}")
            continue
        if len(bars) < 100:
            continue
        # Pro Tag: alle Kerzen BIS 12:00 (die schliesst um 13:00, also vor dem
        # Bias-Lauf um 13:25) und die 20:00-Kerze fuer den Session-Schluss.
        by_day = defaultdict(dict)
        for b in bars:
            dt = datetime.fromtimestamp(b[0] / 1000, tz=timezone.utc)
            by_day[dt.date()][dt.hour] = b
        ordered = sorted(bars, key=lambda b: b[0])
        for day in sorted(by_day):
            d = by_day[day]
            if 20 not in d:
                continue
            cut = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=13)
            cut_ms = cut.timestamp() * 1000
            upto = [b for b in ordered if b[0] + 3_600_000 <= cut_ms]
            if len(upto) < 25:
                continue
            closes = np.array([b[4] for b in upto], dtype=float)
            signal = {"ema9_20": bias_at, "slope": bias_slope,
                      "momentum": bias_momentum}[args.rule](closes)
            if signal == "NEUTRAL":
                continue
            entry = float(d[13][1]) if 13 in d else float(upto[-1][4])
            exit_ = float(d[20][1])      # Open der 20:00-Kerze = Session-Ende 20:00
            move = (exit_ - entry) / entry * 100
            ok = (signal == "BULLISH" and move > 0) or (signal == "BEARISH" and move < 0)
            hits[base]["n"] += 1
            hits[base]["hit"] += int(ok)
            hits[base]["long" if signal == "BULLISH" else "short"] += 1
            rows.append((base, str(day), signal, move, ok))

    if not rows:
        print("Keine auswertbaren Tage.")
        return 0

    print(f"{'Symbol':10s} {'n':>3} {'Treffer':>8} {'Quote':>6}  {'LONG':>4} {'SHORT':>5}")
    for base, h in sorted(hits.items(), key=lambda kv: -(kv[1]["hit"] / max(1, kv[1]["n"]))):
        if not h["n"]:
            continue
        print(f"{base:10s} {h['n']:>3} {h['hit']:>8} {h['hit']/h['n']*100:>5.0f}%  "
              f"{h['long']:>4} {h['short']:>5}")

    total_n = sum(h["n"] for h in hits.values())
    total_hit = sum(h["hit"] for h in hits.values())
    moves = [r[3] for r in rows]
    print(f"\nGesamt: {total_hit}/{total_n} = {total_hit/total_n*100:.1f}% Trefferquote")
    print(f"Ø Session-Bewegung: {st.mean(moves):+.2f}% | Median {st.median(moves):+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
