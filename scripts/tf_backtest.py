#!/usr/bin/env python3
"""
graviton/scripts/tf_backtest.py — Timeframe-Vergleich der Entry-Logik.

Simuliert die EMA20-Pullback-Regel auf 5m / 15m / 1h Kerzen und vergleicht
Trefferquote und Netto-Ergebnis. Instrumente frei waehlbar (Default: die
tokenisierten Aktien-Perps von Kraken Futures + zwei Krypto-Referenzen).

Regeln (pro Bar, nur geschlossene Kerzen):
  LONG   EMA20 steigt, Preis innerhalb ATR-Band um EMA20, Rejection-Bar
         (Close in oberer Haelfte, Low unter EMA), Volumen >= Median
  SHORT  gespiegelt
  Exit   SL = SL_PCT, TP = TP_R x SL, sonst Close zur Session-Boundary
         17:00 UTC; max. 1 Trade pro Symbol und Tag.

Aufruf:
  python3 scripts/tf_backtest.py                 # 15m, Aktien-Perps
  python3 scripts/tf_backtest.py --tf 5m
  python3 scripts/tf_backtest.py --symbols BTC/USD:USD,ETH/USD:USD
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import numpy as np

SL_PCT = 1.5
TP_R = 3.0
ATR_BAND = 0.7
SESSION_CLOSE_H = 17

XSTOCKS = ["SPCXX/USD:USD", "AAPLX/USD:USD", "NVDAX/USD:USD", "TSLAX/USD:USD",
           "SPYX/USD:USD", "QQQX/USD:USD", "AMZNX/USD:USD", "METAX/USD:USD",
           "GOOGLX/USD:USD", "CRCLX/USD:USD", "COINX/USD:USD", "MSTRX/USD:USD",
           "HOODX/USD:USD", "GLDX/USD:USD"]
EQUITIES = ["AMD/USD:USD", "INTC/USD:USD", "PLTR/USD:USD", "MU/USD:USD",
            "SOXL/USD:USD", "SOXS/USD:USD", "MRVL/USD:USD", "NBIS/USD:USD",
            "SNDK/USD:USD", "SAMSUNG/USD:USD", "SKHY/USD:USD", "EWY/USD:USD",
            "EWZ/USD:USD"]
STOCKS = XSTOCKS + EQUITIES
CRYPTO = ["BTC/USD:USD", "ETH/USD:USD", "SOL/USD:USD", "XRP/USD:USD"]


def ema(values: np.ndarray, period: int = 20) -> np.ndarray:
    a = 2.0 / (period + 1)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = a * values[i] + (1 - a) * out[i - 1]
    return out


def atr(high, low, close, period: int = 14) -> np.ndarray:
    tr = np.maximum(high - low, np.maximum(np.abs(high - np.roll(close, 1)),
                                           np.abs(low - np.roll(close, 1))))
    out = np.empty_like(tr)
    out[0] = tr[0]
    a = 2.0 / (period + 1)
    for i in range(1, len(tr)):
        out[i] = a * tr[i] + (1 - a) * out[i - 1]
    return out


def fetch(ex, symbol: str, tf: str, target_bars: int = 4000):
    tf_ms = ex.parse_timeframe(tf) * 1000
    now = ex.milliseconds()
    since = now - target_bars * tf_ms
    bars = []
    while since < now and len(bars) < target_bars:
        chunk = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not chunk:
            break
        bars.extend(chunk)
        since = chunk[-1][0] + tf_ms
    seen, out = set(), []
    for b in bars:
        if b[0] not in seen:
            seen.add(b[0])
            out.append(b)
    return out


def session_close_ms(ts_ms: int, tf_ms: int) -> int:
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    close = d.replace(hour=SESSION_CLOSE_H, minute=0, second=0, microsecond=0)
    if close <= d:
        close = close.replace(day=d.day) if d.day == 28 and d.month == 2 else close
        import datetime as _dt
        close = d + _dt.timedelta(days=1)
        close = close.replace(hour=SESSION_CLOSE_H, minute=0, second=0, microsecond=0)
    return int(close.timestamp() * 1000)


def run_symbol(bars, tf_ms: int, band_atr: float = ATR_BAND,
               sl_pct: float = SL_PCT, tp_r: float = TP_R):
    if len(bars) < 80:
        return []
    arr = np.array([[b[0], b[1], b[2], b[3], b[4], b[5]] for b in bars], dtype=float)
    ts, o, h, l, c, v = (arr[:, i] for i in range(6))
    e20 = ema(c)
    a14 = atr(h, l, c)
    vol_med = np.array([np.median(v[max(0, i - 20):i]) if i >= 20 else 0 for i in range(len(v))])
    trades, last_day = [], {}

    for i in range(30, len(arr) - 1):
        if a14[i] <= 0 or vol_med[i] <= 0:
            continue
        band = band_atr * a14[i]
        dist = c[i] - e20[i]
        body = abs(c[i] - o[i]) or 1e-9
        close_pos = (c[i] - l[i]) / (h[i] - l[i]) if h[i] > l[i] else 0.5
        day = datetime.fromtimestamp(ts[i] / 1000, tz=timezone.utc).date()
        if last_day.get(day):
            continue

        long_setup = (e20[i] > e20[i - 5] and -band <= dist <= band and
                      c[i] > o[i] and close_pos >= 0.6 and l[i] <= e20[i] * 1.0005 and
                      v[i] >= vol_med[i])
        short_setup = (e20[i] < e20[i - 5] and -band <= dist <= band and
                       c[i] < o[i] and close_pos <= 0.4 and h[i] >= e20[i] * 0.9995 and
                       v[i] >= vol_med[i])
        if not (long_setup or short_setup):
            continue

        bias = "LONG" if long_setup else "SHORT"
        entry = c[i]
        sl_price = entry * (1 - sl_pct / 100) if bias == "LONG" else entry * (1 + sl_pct / 100)
        tp_price = entry * (1 + sl_pct * tp_r / 100) if bias == "LONG" else entry * (1 - sl_pct * tp_r / 100)
        close_ts = session_close_ms(int(ts[i]), tf_ms)

        pnl, reason = None, "open"
        for j in range(i + 1, len(arr)):
            if ts[j] > close_ts:
                px = c[j - 1]
                pnl = (px - entry) / entry * 100 if bias == "LONG" else (entry - px) / entry * 100
                reason = "close"
                break
            if bias == "LONG":
                if l[j] <= sl_price:
                    pnl, reason = -sl_pct, "sl"; break
                if h[j] >= tp_price:
                    pnl, reason = sl_pct * tp_r, "tp"; break
            else:
                if h[j] >= sl_price:
                    pnl, reason = -sl_pct, "sl"; break
                if l[j] <= tp_price:
                    pnl, reason = sl_pct * tp_r, "tp"; break
        if pnl is None:
            continue
        last_day[day] = True
        trades.append({"day": str(day), "bias": bias, "pnl": pnl, "reason": reason})
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--group", choices=["stocks", "xstocks", "equities", "crypto"],
                    default="stocks")
    ap.add_argument("--atr-band", type=float, default=ATR_BAND,
                    help="Max. EMA-Abstand in ATR-Einheiten (Default 0.7)")
    ap.add_argument("--sl", type=float, default=SL_PCT, help="Stop-Loss in Prozent")
    ap.add_argument("--tp-r", type=float, default=TP_R, help="Take-Profit als R-Vielfaches")
    args = ap.parse_args()

    groups = {"stocks": STOCKS, "xstocks": XSTOCKS, "equities": EQUITIES, "crypto": CRYPTO}
    syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
            or groups[args.group])
    ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    tf_ms = ex.parse_timeframe(args.tf) * 1000

    all_trades, per_symbol = [], {}
    for sym in syms:
        try:
            bars = fetch(ex, sym, args.tf)
        except Exception as exc:                     # noqa: BLE001
            print(f"{sym:16s} FETCH-ERR {type(exc).__name__}")
            continue
        if not bars:
            print(f"{sym:16s} keine Daten")
            continue
        tr = run_symbol(bars, tf_ms, args.atr_band, args.sl, args.tp_r)
        per_symbol[sym] = tr
        all_trades.extend(tr)
        p = [t["pnl"] for t in tr]
        print(f"{sym:16s} Bars {len(bars):5d}  Trades {len(tr):3d}"
              + (f"  Summe {sum(p):+7.1f}%  Ø {st.mean(p):+5.2f}%  WR {sum(1 for x in p if x>0)/len(p)*100:3.0f}%"
                 if tr else ""))

    if not all_trades:
        print("\nKeine Trades.")
        return 0
    p = [t["pnl"] for t in all_trades]
    days = defaultdict(float)
    for t in all_trades:
        days[t["day"]] += t["pnl"]
    reasons = defaultdict(int)
    for t in all_trades:
        reasons[t["reason"]] += 1
    print(f"\n=== {args.tf} | {len(all_trades)} Trades | Summe {sum(p):+.1f}% | "
          f"Ø {st.mean(p):+.2f}% | WR {sum(1 for x in p if x>0)/len(p)*100:.0f}% | "
          f"grüne Tage {sum(1 for x in days.values() if x>0)}/{len(days)}")
    print("Exits:", dict(reasons))

    out = Path(__file__).resolve().parent.parent / "data" / f"tf_backtest_{args.tf}_{args.group}_band{args.atr_band}.json"
    out.write_text(json.dumps({"tf": args.tf, "group": args.group,
                               "per_symbol": per_symbol}, indent=1))
    print("→", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
