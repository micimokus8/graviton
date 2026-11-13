"""Robustheit von Engine B auf der langen Aktien-Historie."""
from __future__ import annotations

import json

from swing.backtest import replay
from swing.strategy import trend_stance_at


def agg(trades):
    """Immer ein Tupel (n, wr, exp, hold) — bei leeren Trades (0, 0, 0, 0)."""
    if not trades:
        return (0, 0.0, 0.0, 0.0)
    wins = [t for t in trades if t["net_pct"] > 0]
    return (len(trades), round(100 * len(wins) / len(trades), 1),
            round(sum(t["net_pct"] for t in trades) / len(trades), 2),
            round(sum(t["hold"] for t in trades) / len(trades), 1))


def main() -> int:
    data = json.load(open("data/stock_daily.json"))
    print(f"{'sl':>5} {'hold':>5} | {'n':>5} {'WR':>6} {'exp':>7} {'hold_d':>7}")
    for sl in (1.5, 2.5, 4.0):
        for mh in (10, 20, 40):
            trades = []
            for c in data.values():
                trades += replay(c, trend_stance_at, tp_atr=99.0, sl_atr=sl,
                                 max_hold=mh)["trades"]
            n, wr, exp, hold = agg(trades)  # trades nie leer im Sweep
            print(f"{sl:5.1f} {mh:5d} | {n:5d} {wr:6.1f} {exp:+7.2f} {hold:7.1f}")

    print("\nPer-Symbol (sl=2.5, hold=20), Engine B:")
    for sym, c in data.items():
        t = replay(c, trend_stance_at, tp_atr=99.0, sl_atr=2.5, max_hold=20)["trades"]
        n, wr, exp, hold = agg(t)
        if n == 0:
            print(f"  {sym:6s} keine Trades")
            continue
        print(f"  {sym:6s} n={n:4d} WR={wr:5.1f}% exp={exp:+6.2f}% hold={hold:4.1f}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
