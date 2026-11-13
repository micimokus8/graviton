"""Exit-Geometrie der Trend-Engine (B) durchmessen — Gewinner laufen lassen?"""
from __future__ import annotations

import json
import os

from swing import data
from swing.backtest import replay
from swing.strategy import MIN_BARS, trend_stance_at

CACHE = "data/swing_candles.json"
MIN_TRADE_BARS = MIN_BARS + 20


def load_candles(ex):
    if os.path.exists(CACHE):
        with open(CACHE) as fh:
            return json.load(fh)
    out = {}
    for p in data.stock_perps(ex):
        try:
            c = data.daily_candles(ex, p["symbol"], limit=1000)
        except Exception:
            continue
        if len(c) >= MIN_TRADE_BARS:
            out[p["base"]] = c
    with open(CACHE, "w") as fh:
        json.dump(out, fh)
    return out


def agg(trades):
    if not trades:
        return None
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    return {
        "n": len(trades),
        "wr": round(100 * wins / len(trades), 1),
        "exp": round(sum(t["net_pct"] for t in trades) / len(trades), 2),
        "sum": round(sum(t["net_pct"] for t in trades), 1),
        "hold": round(sum(t["hold"] for t in trades) / len(trades), 1),
    }


def main() -> int:
    candles = load_candles(data.exchange())
    print(f"{len(candles)} Symbole\n")
    print(f"{'sl_atr':>6} {'tp_atr':>7} {'hold':>5} | {'n':>3} {'WR':>6} {'exp':>7} {'sum':>7} {'hold_d':>7}")
    for sl in (1.5, 2.5, 3.5):
        for tp in (2.0, 4.0, 99.0):
            for mh in (10, 20):
                trades = []
                for base, c in candles.items():
                    trades += replay(c, trend_stance_at, tp_atr=tp, sl_atr=sl,
                                     max_hold=mh)["trades"]
                a = agg(trades)
                tag = "kein TP" if tp >= 99 else f"{tp:g}"
                print(f"{sl:6.1f} {tag:>7} {mh:5d} | {a['n']:3d} {a['wr']:6.1f} "
                      f"{a['exp']:+7.2f} {a['sum']:+7.1f} {a['hold']:7.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
