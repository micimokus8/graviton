#!/usr/bin/env python3
"""Filter-Statistik: welche Entry-Bedingungen trennen Gewinner von Verlierern?

Liest data/entry_forensics.json, rechnet MFE/MAE ueber ein fixes 6h-Fenster
(5m-Kerzen ab Entry) neu und bucketed ADX/Geometrie/Distanz/Volumen/ATR.
Keine neue Logik im Bot — reine Auswertung der 41 geloggten Trades.
"""
import json
from datetime import datetime
from pathlib import Path

import ccxt
import numpy as np

BASE = Path("/root/.hermes/workspace/graviton")
F = BASE / "data" / "entry_forensics.json"
TF5 = 300_000


def stats(recs):
    if not recs:
        return "n=0"
    p = [r["pnl"] for r in recs]
    w = sum(1 for x in p if x > 0)
    return (f"n={len(recs):2d}  WR={w/len(p)*100:4.0f}%  "
            f"avgPnL={np.mean(p):+6.2f}%  sum={np.sum(p):+7.2f}%")


def main():
    d = json.load(open(F))
    recs = list(d.values())
    ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})

    for r in recs:
        t0 = datetime.fromisoformat(r["ts"][:19] + "+00:00")
        t0_ms = int(t0.timestamp() * 1000)
        try:
            raw = ex.fetch_ohlcv(r["symbol"] if "symbol" in r else r["base"] + "/USD:USD",
                                 "5m", since=t0_ms, limit=200)
            fwd = np.array([[float(v) for v in row[:6]] for row in raw
                            if row[0] + TF5 <= t0_ms + 6 * 3_600_000])
        except Exception as err:
            print("ERR", r["base"], err)
            continue
        if not len(fwd):
            continue
        # atr5 aus dem Cache-Feld rekonstruieren ist unnoetig: MFE in % reicht
        if r["bias"] == "LONG":
            r["mfe_pct"] = round((fwd[:, 2].max() / fwd[0][1] - 1) * 100, 2)
            r["mae_pct"] = round((fwd[:, 3].min() / fwd[0][1] - 1) * 100, 2)
        else:
            r["mfe_pct"] = round((1 - fwd[:, 3].min() / fwd[0][1]) * 100, 2)
            r["mae_pct"] = round((1 - fwd[:, 2].max() / fwd[0][1]) * 100, 2)
    json.dump(d, open(F, "w"), indent=1)

    print(f"=== ALLE {len(recs)} ===")
    print(stats(recs))

    print("\n=== ADX(1H) Regime-Gate ===")
    for lo, hi, lab in ((-1, 20, "ADX<20 (kein Trend)"), (20, 25, "ADX 20-25 (totes Band)"), (25, 999, "ADX>=25 (Trend)")):
        print(f"{lab:24s}", stats([r for r in recs if lo <= r["adx_1h"] < hi]))

    print("\n=== Entry-Bar Geometrie (Close-Position in Range) ===")
    for lo, hi, lab in ((-.01, .2, "0-20% (am Tief)"), (.2, .5, "20-50%"), (.5, 1.01, "50-100% (oben)")):
        print(f"{lab:20s}", stats([r for r in recs if lo <= r["bar_close_pos"] < hi]))

    print("\n=== Volumen Entry-Bar (x Median) ===")
    for lo, hi, lab in ((-.01, .5, "<0.5x (tot)"), (.5, 1.0, "0.5-1.0x"), (1.0, 999, ">=1.0x")):
        print(f"{lab:16s}", stats([r for r in recs if lo <= r["bar_vol_x"] < hi]))

    print("\n=== EMA-Distanz in ATR(5m) ===")
    for lo, hi, lab in ((-.01, .5, "<0.5 ATR (Rauschen)"), (.5, 1.0, "0.5-1.0 ATR"), (1.0, 999, ">1.0 ATR")):
        print(f"{lab:22s}", stats([r for r in recs if lo <= r["dist_atr5"] < hi]))

    print("\n=== ATR-Expansion (ATR vs 20-Bar-Median) ===")
    for lo, hi, lab in ((-.01, .9, "<0.9 (kontrahiert)"), (.9, 1.1, "0.9-1.1 (flach)"), (1.1, 999, ">1.1 (expandiert)")):
        print(f"{lab:22s}", stats([r for r in recs if lo <= r["atr5_expansion"] < hi]))

    print("\n=== MFE (6h-Fenster, %) — Edge vorhanden? ===")
    print(f"  avg MFE LONG  = {np.mean([r.get('mfe_pct',0) for r in recs if r['bias']=='LONG']):+.2f}%")
    print(f"  avg MFE SHORT = {np.mean([r.get('mfe_pct',0) for r in recs if r['bias']=='SHORT']):+.2f}%")
    for lo, lab in ((0.5, "MFE<0.5% (kein Edge)"), (2.0, "MFE>=2%")):
        sel = [r for r in recs if r.get("mfe_pct", 0) >= lo]
        print(f"  {lab:20s}", stats(sel))

    print("\n=== Gewinner/Verlierer Vergleich ===")
    win = [r for r in recs if r["pnl"] > 0]
    loss = [r for r in recs if r["pnl"] <= 0]
    for name, g in (("WIN", win), ("LOSS", loss)):
        print(f"{name:5s} n={len(g):2d} "
              f"avgADX={np.mean([r['adx_1h'] for r in g]):5.1f} "
              f"avgPos={np.mean([r['bar_close_pos'] for r in g]):4.2f} "
              f"avgVol={np.mean([r['bar_vol_x'] for r in g]):5.2f}x "
              f"avgDist={np.mean([r['dist_atr5'] for r in g]):4.2f}ATR "
              f"avgMFE={np.mean([r.get('mfe_pct',0) for r in g]):+5.2f}%")


if __name__ == "__main__":
    main()
