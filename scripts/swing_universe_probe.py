"""Phase 0: Datenlage des Aktien-Perp-Universums (dynamisch aus Kraken-Markets).

Sammelt alle xStocks- und Equities-Perps, holt 1D-Historie und rechnet die
Kennzahlen, die für einen Swing-Backtest nötig sind (Bars, Zeitraum, ATR%).
Schreibt data/swing_universe.json.
"""
import json
import statistics
from datetime import datetime, timezone

import ccxt

CATS = ("xStocks", "Equities")


def atr_pct(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:]
    last_close = candles[-1][4]
    return 100.0 * (sum(recent) / len(recent)) / last_close if last_close else None


def main():
    ex = ccxt.krakenfutures({"enableRateLimit": True})
    markets = ex.load_markets()
    rows = []
    for sym, m in markets.items():
        cat = (m.get("info") or {}).get("category", "")
        if cat not in CATS:
            continue
        rec = {"symbol": sym, "base": m.get("base"), "category": cat}
        try:
            candles = ex.fetch_ohlcv(sym, "1d", limit=1000)
            if candles:
                first = datetime.fromtimestamp(candles[0][0] / 1000, timezone.utc)
                last = datetime.fromtimestamp(candles[-1][0] / 1000, timezone.utc)
                rec.update({
                    "bars": len(candles),
                    "from": first.date().isoformat(),
                    "to": last.date().isoformat(),
                    "last_close": candles[-1][4],
                    "atr_pct_14": round(atr_pct(candles), 2) if atr_pct(candles) else None,
                })
            else:
                rec["error"] = "keine Kerzen"
        except Exception as exc:
            rec["error"] = str(exc)[:120]
        rows.append(rec)
        print(json.dumps(rec, ensure_ascii=False))

    rows.sort(key=lambda r: (r["category"], -(r.get("bars") or 0)))
    out = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "categories": list(CATS),
        "count": len(rows),
        "symbols": rows,
    }
    with open("data/swing_universe.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\n{len(rows)} Symbole -> data/swing_universe.json")


if __name__ == "__main__":
    main()
