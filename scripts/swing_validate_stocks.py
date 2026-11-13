"""Richtungs-Engine auf langer Aktien-Historie validieren (TwelveData, Proxy).

Die Kraken-Perps sind zu jung (nach EMA200-Warmup nur 2-3 Monate handelbar).
Die Regel wird daher an den Underlying-Aktien über viele Jahre geprüft.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from swing.backtest import replay
from swing.strategy import MIN_BARS, direction_row_at, trend_stance_at

CACHE = "data/stock_daily.json"
SYMBOLS = ["NVDA", "AAPL", "GOOGL", "MSTR", "TSLA", "HOOD", "COIN", "AMD"]
KEY_FILE = "/root/.hermes/workspace/earnings-monitor/TwelveData Key.txt"


def fetch(symbol: str) -> list[list]:
    key = open(KEY_FILE).read().strip()
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=1day"
           f"&outputsize=5000&apikey={key}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    vals = data.get("values") or []
    if not vals:
        raise RuntimeError(data.get("message", "keine Daten"))
    rows = []
    for v in reversed(vals):  # TwelveData: neueste zuerst -> aufsteigend
        rows.append([v["datetime"], float(v["open"]), float(v["high"]),
                     float(v["low"]), float(v["close"]), float(v.get("volume") or 0)])
    return rows


def load(ex) -> dict[str, list[list]]:
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    out = {}
    for i, s in enumerate(SYMBOLS):
        try:
            out[s] = fetch(s)
            print(f"  {s}: {len(out[s])} Bars ({out[s][0][0]}..{out[s][-1][0]})")
        except Exception as exc:
            print(f"  {s}: FEHLER {str(exc)[:80]}")
        if i < len(SYMBOLS) - 1:
            time.sleep(8)  # TwelveData: 8 Credits/Minute
    json.dump(out, open(CACHE, "w"))
    return out


def stance_a(ind, close, i):
    row = direction_row_at(ind, close, i)
    return row["stance"] if row and row["stance"] in ("LONG", "SHORT") else None


def agg(trades):
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["net_pct"] > 0]
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    return {
        "n": len(trades),
        "wr": round(100 * len(wins) / len(trades), 1),
        "exp": round(sum(t["net_pct"] for t in trades) / len(trades), 2),
        "hold": round(sum(t["hold"] for t in trades) / len(trades), 1),
        "long_n": len(longs), "long_exp": round(sum(t["net_pct"] for t in longs) / len(longs), 2) if longs else 0,
        "short_n": len(shorts), "short_exp": round(sum(t["net_pct"] for t in shorts) / len(shorts), 2) if shorts else 0,
    }


def main() -> int:
    from swing.data import exchange
    data = load(exchange())
    print(f"\n{len(data)} Symbole, je >{MIN_BARS} Bars\n")
    for name, fn in (("A_meanrev", stance_a), ("B_trend", trend_stance_at)):
        trades = []
        for sym, candles in data.items():
            trades += replay(candles, fn, tp_atr=99.0, sl_atr=2.5, max_hold=20)["trades"]
        a = agg(trades)
        print(f"[{name}] n={a['n']} WR={a['wr']}% exp={a['exp']:+.2f}% hold={a['hold']}d | "
              f"LONG n={a['long_n']} exp={a['long_exp']:+.2f}% | "
              f"SHORT n={a['short_n']} exp={a['short_exp']:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
