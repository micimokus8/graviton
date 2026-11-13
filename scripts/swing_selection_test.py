"""Phase 2: Auswahl-Test. Ranking innerhalb der 27 Graviton-Perps (Underlyings).

Frage: Bringt es etwas, aus allen verfügbaren Titeln den STÄRKSTEN Trend zu
wählen, statt irgendeinen? Kontrolle = dieselbe Regel ohne Ranking (alphabetisch
erstbestes) — damit ist die Netto-Wirkung der Selektion isoliert.
"""
from __future__ import annotations

import sys

import numpy as np

from swing import twelvedata
from swing.strategy import indicators, trend_stance_at

UNDERLYINGS = [
    "AAPL", "AMZN", "COIN", "CRCL", "GLD", "GOOGL", "HOOD", "META", "MSTR",
    "NVDA", "QQQ", "SPY", "TSLA",                    # xStocks
    "AMD", "EWY", "EWZ", "INTC", "MRVL", "MU", "NBIS", "PLTR", "SNDK",
    "SOXL", "SOXS",                                  # Equities
]
COST_BPS = 10.0
SL_ATR = 2.5
MAX_HOLD = 20
MOM_LOOKBACK = 126


def prep(candles_by_sym):
    pre = {}
    for s, c in candles_by_sym.items():
        if len(c) < 230:
            continue
        close = np.array([x[4] for x in c], dtype=float)
        ind = indicators([x[2] for x in c], [x[3] for x in c], close)
        pre[s] = {"c": c, "close": close, "ind": ind,
                  "didx": {x[0][:10]: i for i, x in enumerate(c)}}
    return pre


def score_mom(pre_s, i):
    if i < MOM_LOOKBACK:
        return None
    return pre_s["close"][i] / pre_s["close"][i - MOM_LOOKBACK] - 1


def score_adx(pre_s, i):
    v = pre_s["ind"]["adx"][i]
    return float(v) if np.isfinite(v) else None


def run(pre, scorer, *, top_n=1, control=False, stance_fn=trend_stance_at):
    all_dates = sorted({d for p in pre.values() for d in p["didx"]})
    fee = COST_BPS / 10_000
    trades, pos = [], None

    for d in all_dates:
        if pos is not None:
            p = pre[pos["sym"]]
            i = p["didx"].get(d)
            if i is not None and i > pos["entry_i"]:
                h, l, c = p["c"][i][2], p["c"][i][3], p["c"][i][4]
                ex = reason = None
                if pos["side"] == "LONG":
                    if l <= pos["stop"]:
                        ex, reason = pos["stop"], "STOP"
                    elif h >= pos["tp"]:
                        ex, reason = pos["tp"], "TP"
                if ex is None:
                    st = stance_fn(p["ind"], p["close"], i)
                    if st != pos["side"]:
                        ex, reason = c, "TREND_BREAK"
                    elif i - pos["entry_i"] >= MAX_HOLD:
                        ex, reason = c, "MAX_HOLD"
                pos["mfe"] = max(pos.get("mfe", pos["entry"]), h)
                if ex is not None:
                    sign = 1 if pos["side"] == "LONG" else -1
                    gross = sign * (ex / pos["entry"] - 1)
                    trades.append({"sym": pos["sym"], "net_pct": round((gross - 2 * fee) * 100, 2),
                                   "hold": i - pos["entry_i"], "reason": reason,
                                   "mfe_atr": (pos["mfe"] - pos["entry"]) / pos["atr"]})
                    pos = None

        if pos is not None:
            continue
        cands = []
        for s, p in pre.items():
            i = p["didx"].get(d)
            if i is None or i + 1 >= len(p["c"]):
                continue
            if stance_fn(p["ind"], p["close"], i) != "LONG":
                continue
            sc = scorer(p, i)
            if sc is not None:
                cands.append((sc, s, i))
        if not cands:
            continue
        if control:
            cands.sort(key=lambda x: x[1], reverse=True)   # alphabetisch, ohne Skill
        else:
            cands.sort(reverse=True)                       # nach Score
        sc, s, i = cands[0]
        p = pre[s]
        atr = p["ind"]["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            continue
        entry_i = i + 1
        entry = p["c"][entry_i][1]
        pos = {"sym": s, "side": "LONG", "entry": entry, "entry_i": entry_i,
               "stop": entry - SL_ATR * atr, "tp": entry + 99 * atr, "atr": atr,
               "mfe": entry}

    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["net_pct"] > 0]
    return {
        "n": len(trades),
        "wr": round(100 * len(wins) / len(trades), 1),
        "exp": round(sum(t["net_pct"] for t in trades) / len(trades), 2),
        "hold": round(sum(t["hold"] for t in trades) / len(trades), 1),
        "trades": trades,
        "symbols": dict((s, sum(1 for t in trades if t["sym"] == s))
                        for s in {t["sym"] for t in trades}),
    }


def main() -> int:
    data = twelvedata.load(UNDERLYINGS, "data/stock_daily_27.json")
    pre = prep(data)
    print(f"{len(pre)} Titel mit ausreichend Historie\n")
    no_ema = lambda ind, close, i: trend_stance_at(ind, close, i, use_ema100=False)
    variants = (
        ("Kontrolle, EMA100", score_mom, True, trend_stance_at),
        ("ADX, EMA100", score_adx, False, trend_stance_at),
        ("ADX, ohne EMA", score_adx, False, no_ema),
        ("Momentum, ohne EMA", score_mom, False, no_ema),
    )
    for label, sc, ctrl, stance_fn in variants:
        r = run(pre, sc, control=ctrl, stance_fn=stance_fn)
        if r.get("n"):
            print(f"[{label:22s}] n={r['n']:4d} WR={r['wr']:5.1f}% exp={r['exp']:+.2f}% "
                  f"hold={r['hold']:4.1f}d")
            trades = r.get("trades") or []
            if trades:
                hits = " | ".join(
                    f"+{k:g}xATR {100*sum(1 for t in trades if t['mfe_atr'] >= k)/len(trades):4.1f}%"
                    for k in (1.0, 1.5, 2.0, 2.5))
                print(f"     Zielerreichung: {hits}")
        else:
            print(f"[{label}] keine Trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
