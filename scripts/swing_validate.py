"""Richtungs-Engines auf beliebiger Symbol-Liste validieren (TwelveData-Proxy).

Aufruf: PYTHONPATH=. .venv/bin/python scripts/swing_validate.py CACHE SYM1 SYM2 ...
"""
from __future__ import annotations

import sys

from swing import twelvedata
from swing.backtest import replay
from swing.strategy import direction_row_at, trend_stance_at


def stance_a(ind, close, i):
    row = direction_row_at(ind, close, i)
    return row["stance"] if row and row["stance"] in ("LONG", "SHORT") else None


def agg(trades):
    if not trades:
        return (0, 0.0, 0.0, 0.0)
    wins = [t for t in trades if t["net_pct"] > 0]
    return (len(trades), round(100 * len(wins) / len(trades), 1),
            round(sum(t["net_pct"] for t in trades) / len(trades), 2),
            round(sum(t["hold"] for t in trades) / len(trades), 1))


def main() -> int:
    cache, symbols = sys.argv[1], sys.argv[2:]
    data = twelvedata.load(symbols, cache)
    print(f"\n{len(data)} Symbole\n")

    for name, fn in (("A_meanrev", stance_a), ("B_trend", trend_stance_at)):
        trades = []
        for c in data.values():
            trades += replay(c, fn, tp_atr=99.0, sl_atr=2.5, max_hold=20)["trades"]
        n, wr, exp, hold = agg(trades)
        longs = [t for t in trades if t["side"] == "LONG"]
        shorts = [t for t in trades if t["side"] == "SHORT"]
        le = sum(t["net_pct"] for t in longs) / len(longs) if longs else 0
        se = sum(t["net_pct"] for t in shorts) / len(shorts) if shorts else 0
        print(f"[{name:9s}] n={n:5d} WR={wr:5.1f}% exp={exp:+.2f}% hold={hold:4.1f}d "
              f"| LONG n={len(longs):4d} exp={le:+.2f}% | SHORT n={len(shorts):4d} exp={se:+.2f}%")

    print("\nPer-Symbol (Engine B, sl=2.5, hold=20):")
    pos = 0
    for sym, c in data.items():
        t = replay(c, trend_stance_at, tp_atr=99.0, sl_atr=2.5, max_hold=20)["trades"]
        n, wr, exp, hold = agg(t)
        pos += 1 if exp > 0 else 0
        print(f"  {sym:6s} n={n:4d} WR={wr:5.1f}% exp={exp:+6.2f}% hold={hold:4.1f}d")
    print(f"\n  -> {pos}/{len(data)} Symbole mit positivem Erwartungswert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
