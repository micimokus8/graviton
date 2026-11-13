"""Diagnose: Wie oft ist ein Symbol im 1D-LONG-Zustand? (Feuert das System?)"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np

from swing.strategy import MIN_BARS, indicators, trend_stance_at

CACHE = "data/underlying_daily.json"


def main() -> int:
    cache = json.load(open(CACHE)).get("symbols") or {}
    series = {}
    for sym, entry in cache.items():
        rows = entry.get("rows") or []
        if len(rows) < MIN_BARS + 10:
            continue
        ts = [int(datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc).timestamp() * 1000)
              for r in rows]
        c = [r for r in rows]
        close = np.array([r[4] for r in c], dtype=float)
        ind = indicators([r[2] for r in c], [r[3] for r in c], close)
        series[sym] = (ts, close, ind)

    print(f"{len(series)} Symbole mit Historie im Cache\n")
    # letzte 30 Handelstage (gemeinsame Achse über alle Symbole)
    dates = sorted({d for _, e in cache.items() for d in [r[0] for r in (e.get('rows') or [])]})
    tail = dates[-30:]
    print(f"{'Datum':12s} {'LONG':>5}  Symbole")
    for d in tail:
        longs = []
        for sym, (ts, close, ind) in series.items():
            idx = None
            for i, t in enumerate(ts):
                if datetime.fromtimestamp(t / 1000, timezone.utc).date().isoformat() == d:
                    idx = i
                    break
            if idx is None or idx < MIN_BARS:
                continue
            if trend_stance_at(ind, close, idx) == "LONG":
                longs.append(sym)
        print(f"{d:12s} {len(longs):5d}  {','.join(longs[:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
