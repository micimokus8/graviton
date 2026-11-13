"""Swing-Backtest: misst eine Richtungs-Engine im gleichen Harness.

`stance_fn(ind, close, i)` liefert 'LONG', 'SHORT' oder None. Entry am Open der
Folgetag-Kerze, Exit über Richtungsverlust (None/Gegenrichtung), TP/SL in
ATR-Einheiten oder Max-Hold. Noch kein 4H-Timing und kein S/R-Filter.
"""
from __future__ import annotations

import numpy as np

from .strategy import MIN_BARS, indicators


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["net_pct"] > 0]
    losses = [t for t in trades if t["net_pct"] <= 0]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for t in trades:
        equity *= (1 + t["net_pct"] / 100)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "avg_win_pct": round(sum(t["net_pct"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t["net_pct"] for t in losses) / len(losses), 2) if losses else 0.0,
        "expectancy_pct": round(sum(t["net_pct"] for t in trades) / len(trades), 2),
        "total_pct": round((equity - 1) * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "avg_hold_days": round(sum(t["hold"] for t in trades) / len(trades), 1),
    }


def replay(candles: list[list], stance_fn, *, cost_bps: float = 10.0,
           tp_atr: float = 2.0, sl_atr: float = 1.5, max_hold: int = 10) -> dict:
    close = np.array([c[4] for c in candles], dtype=float)
    ind = indicators([c[2] for c in candles], [c[3] for c in candles], close)
    fee = cost_bps / 10_000
    trades: list[dict] = []
    pos = None

    for i in range(MIN_BARS, len(candles) - 1):
        stance = stance_fn(ind, close, i)

        if pos is None:
            if stance in ("LONG", "SHORT"):
                atr = ind["atr"][i]
                if not np.isfinite(atr) or atr <= 0:
                    continue
                entry_i = i + 1
                entry = candles[entry_i][1]
                stop = entry - sl_atr * atr if stance == "LONG" else entry + sl_atr * atr
                tp = entry + tp_atr * atr if stance == "LONG" else entry - tp_atr * atr
                pos = {"side": stance, "entry": entry, "entry_i": entry_i,
                       "stop": stop, "tp": tp}
            continue

        if i <= pos["entry_i"]:
            continue
        h, l, c = candles[i][2], candles[i][3], candles[i][4]
        exit_price = reason = None
        if pos["side"] == "LONG":
            if l <= pos["stop"]:
                exit_price, reason = pos["stop"], "STOP"
            elif h >= pos["tp"]:
                exit_price, reason = pos["tp"], "TP"
        else:
            if h >= pos["stop"]:
                exit_price, reason = pos["stop"], "STOP"
            elif l <= pos["tp"]:
                exit_price, reason = pos["tp"], "TP"

        if exit_price is None:
            if stance != pos["side"]:
                exit_price, reason = c, "TREND_BREAK"
            elif (i - pos["entry_i"]) >= max_hold:
                exit_price, reason = c, "MAX_HOLD"

        if exit_price is not None:
            sign = 1 if pos["side"] == "LONG" else -1
            gross = sign * (exit_price / pos["entry"] - 1)
            trades.append({
                "side": pos["side"],
                "entry_i": pos["entry_i"], "exit_i": i,
                "hold": i - pos["entry_i"],
                "entry": round(pos["entry"], 4), "exit": round(exit_price, 4),
                "gross_pct": round(gross * 100, 2),
                "net_pct": round((gross - 2 * fee) * 100, 2),
                "reason": reason,
            })
            pos = None

    return {"trades": trades, "stats": _stats(trades)}
