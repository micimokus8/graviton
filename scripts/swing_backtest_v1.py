"""Phase 1: Drei Richtungs-Engines im selben Backtest vergleichen."""
from __future__ import annotations

import json

from swing import data
from swing.backtest import replay
from swing.strategy import (MIN_BARS, direction_row_at, supertrend,
                            trend_stance_at, trend_supertrend_stance_at)

MIN_TRADE_BARS = MIN_BARS + 20


def stance_a(ind, close, i):
    row = direction_row_at(ind, close, i)
    return row["stance"] if row and row["stance"] in ("LONG", "SHORT") else None


def pooled(trades):
    if not trades:
        return "keine Trades"
    wins = [t for t in trades if t["net_pct"] > 0]
    exp = sum(t["net_pct"] for t in trades) / len(trades)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    parts = [f"n={len(trades):3d} WR={100*len(wins)/len(trades):5.1f}% exp={exp:+.2f}% "
             f"hold={sum(t['hold'] for t in trades)/len(trades):4.1f}d {reasons}"]
    for label, side in (("LONG", longs), ("SHORT", shorts)):
        if side:
            w = [t for t in side if t["net_pct"] > 0]
            parts.append(f"    {label:5s} n={len(side):3d} WR={100*len(w)/len(side):5.1f}% "
                         f"exp={sum(t['net_pct'] for t in side)/len(side):+.2f}%")
    return "\n  ".join(parts)


def main() -> int:
    ex = data.exchange()
    per_symbol = {}
    for p in data.stock_perps(ex):
        try:
            candles = data.daily_candles(ex, p["symbol"], limit=1000)
        except Exception:
            continue
        if len(candles) < MIN_TRADE_BARS:
            continue
        close = [c[4] for c in candles]
        st = supertrend([c[2] for c in candles], [c[3] for c in candles], close)
        per_symbol[p["base"]] = {
            "candles": candles,
            "engines": {
                "A_meanrev": replay(candles, stance_a),
                "B_trend": replay(candles, trend_stance_at),
                "C_trend_st": replay(
                    candles, lambda ind, cl, i: trend_supertrend_stance_at(ind, cl, i, st)),
            },
        }

    print(f"Symbole mit genug Historie: {len(per_symbol)} -> {sorted(per_symbol)}\n")
    for eng in ("A_meanrev", "B_trend", "C_trend_st"):
        trades = [t for s in per_symbol.values() for t in s["engines"][eng]["trades"]]
        print(f"[{eng}]")
        print("  " + pooled(trades))
        print()

    out = {b: {e: s["engines"][e]["stats"] for e in s["engines"]}
           for b, s in per_symbol.items()}
    with open("data/swing_backtest_v1.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("-> data/swing_backtest_v1.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
