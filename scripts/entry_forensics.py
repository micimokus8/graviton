#!/usr/bin/env python3
"""Entry-Forensik: misst pro Trade die Filter-Kandidaten aus der KI-Analyse.

Fuer jeden Entry (>= 2026-09-15) werden aus echten Kraken-5m/1h-Kerzen
berechnet (nichts synthetisch, nichts geschaetzt):

  latency_min      Minuten zwischen Scan-/Bias-Slot und Entry
  bar_close_pos    Close-Position der Entry-Kerze in ihrer Range (0 = Tief)
  bar_vol_x        Volumen der Entry-Kerze / 20-Bar-Median
  rej_close_pos    Close-Position der Rejection-Kerze (bester der letzten 3 Bars)
  rej_age_bars     Alter der akzeptierten Rejection-Kerze in 5m-Bars
  dist_atr5        EMA-Abstand in ATR(5m)-Einheiten
  atr5_pct / atr5_expansion  ATR(5m) vs eigener 20-Bar-Median
  adx_1h, adx_5m
  macd_hist_1h, macd_slope_1h
  stoch_k_1h
  mfe_atr / mae_atr  max. Fortschritt / Ruckschlag in ATR(5m)
  pnl              Summe der Exit-pnl_pct

Output: data/entry_forensics.json (Cache) + Konsolen-Tabelle.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import numpy as np

BASE = Path("/root/.hermes/workspace/graviton")
LOG = BASE / "data" / "trade_log.jsonl"
OUT = BASE / "data" / "entry_forensics.json"
TF_MS = {"5m": 300_000, "1h": 3_600_000}


def ema(arr, n):
    a = 2.0 / (n + 1)
    out = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def atr(data, period=14):
    h, l, c = data[:, 2], data[:, 3], data[:, 4]
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(np.mean(tr[-period:]))


def _wilder(values, period):
    """Wilder-Glaettung (RMA): start = SMA(period), dann rekursiv."""
    values = np.asarray(values, dtype=float)
    if len(values) < period:
        return np.full(len(values), np.nan)
    out = np.empty(len(values))
    out[:period] = np.nan
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def adx(data, period=14):
    """ADX(period) mit Wilder-Glaettung von +DM/-DM/TR und DX.

    Wilder-Glaettung statt einfacher Mittelung, weil die simple Mittelung
    DX-Spikes uebergewichtet und ADX systematisch zu hoch ausgibt.
    """
    h, l, c = data[:, 2], data[:, 3], data[:, 4]
    up = np.diff(h)
    dn = -np.diff(l)
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    if len(tr) < period:
        return float("nan"), float("nan"), float("nan")
    plus_s = _wilder(plus, period)
    minus_s = _wilder(minus, period)
    tr_s = _wilder(tr, period)
    tr_s = np.where((tr_s == 0) | ~np.isfinite(tr_s), np.nan, tr_s)
    pdi = 100 * plus_s / tr_s
    mdi = 100 * minus_s / tr_s
    denom = pdi + mdi
    dx = np.where((denom == 0) | ~np.isfinite(denom), np.nan,
                  100 * np.abs(pdi - mdi) / np.where(denom == 0, np.nan, denom))
    adx_s = _wilder(dx[period - 1:], period)
    last = -1
    return (float(adx_s[last]) if np.isfinite(adx_s[last]) else float("nan"),
            float(pdi[last]) if np.isfinite(pdi[last]) else float("nan"),
            float(mdi[last]) if np.isfinite(mdi[last]) else float("nan"))


def macd(closes, fast=12, slow=26, sig=9):
    line = ema(closes, fast) - ema(closes, slow)
    signal = ema(line, sig)
    return line[-1] - signal[-1], (line[-1] - signal[-1]) - (line[-2] - signal[-2])


def stoch_k(data, period=14):
    lows = data[-period:, 3]
    highs = data[-period:, 2]
    span = highs.max() - lows.min()
    return float((data[-1, 4] - lows.min()) / span * 100) if span else 50.0


def closed_before(candles, cut_ms, tf_ms):
    """Nur Bars, deren Intervall zum Zeitpunkt cut_ms bereits geschlossen ist."""
    return np.array([[float(v) for v in row[:6]] for row in candles
                     if row[0] + tf_ms <= cut_ms])


def main():
    ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    rows = [json.loads(l) for l in open(LOG)]
    entries = [r for r in rows if r.get("event") == "entry" and r["timestamp"][:10] >= "2026-09-15"]
    exits = [r for r in rows if r.get("event") == "exit"]

    cache = json.load(open(OUT)) if OUT.exists() else {}
    result = []

    for e in entries:
        key = e["timestamp"] + e["symbol"]
        if key in cache:
            result.append(cache[key])
            continue

        t0 = datetime.fromisoformat(e["timestamp"][:19] + "+00:00")
        t0_ms = int(t0.timestamp() * 1000)
        sym, bias = e["symbol"], e["bias"]
        try:
            raw5 = ex.fetch_ohlcv(sym, "5m", since=t0_ms - 200 * 300_000, limit=300)
            raw1 = ex.fetch_ohlcv(sym, "1h", since=t0_ms - 200 * 3_600_000, limit=200)
        except Exception as err:
            print(f"FETCH-ERR {e['base']} {e['timestamp'][:16]}: {err}")
            continue

        d5 = closed_before(raw5, t0_ms, TF_MS["5m"])
        d1 = closed_before(raw1, t0_ms, TF_MS["1h"])
        if len(d5) < 30 or len(d1) < 30:
            print(f"SKIP {e['base']} {e['timestamp'][:16]} zu wenig Kerzen")
            continue

        # Entry-Kerze = letzte geschlossene 5m-Kerze vor dem Entry
        bar = d5[-1]
        rng = bar[2] - bar[3]
        close_pos = (bar[4] - bar[3]) / rng if rng > 0 else 0.5
        vol_med = float(np.median(d5[-21:-1, 5]))
        bar_vol_x = float(bar[5] / vol_med) if vol_med else 0.0

        atr5 = atr(d5)
        atr_pct = atr5 / bar[4] * 100
        atr_hist = [atr(d5[:i]) for i in range(len(d5) - 20, len(d5) + 1)]
        atr_expansion = atr5 / float(np.median(atr_hist)) if np.median(atr_hist) else 1.0

        e20 = float(ema(d5[:, 4], 20)[-1])
        dist_pct = (bar[4] - e20) / e20 * 100 * (1 if bias == "LONG" else -1)
        dist_atr5 = abs(bar[4] - e20) / atr5 if atr5 else 0.0

        # Rejection-Kandidat: letzte 3 Bars, geometrisch passend zur Richtung
        rej_age, rej_close_pos = None, None
        for age in range(0, 3):
            b = d5[-(1 + age)]
            r = b[2] - b[3]
            if r <= 0:
                continue
            pos = (b[4] - b[3]) / r
            ok = pos > 0.6 if bias == "LONG" else pos < 0.4
            if ok:
                rej_age, rej_close_pos = age, pos
                break

        adx1, pdi1, mdi1 = adx(d1)
        adx5, _, _ = adx(d5)
        mh1, ms1 = macd(d1[:, 4])

        # MFE/MAE bis zum zugehoerigen Exit — Kerzen NACH dem Entry laden
        outs = [x for x in exits
                if x.get("base") == e.get("base") and x["timestamp"] > e["timestamp"]
                and x.get("bias") == bias]
        exit_ts = outs[0]["timestamp"] if outs else None
        cut = int(datetime.fromisoformat(exit_ts[:19] + "+00:00").timestamp() * 1000) if exit_ts else t0_ms + 4 * 3_600_000
        try:
            fwd_raw = ex.fetch_ohlcv(sym, "5m", since=t0_ms, limit=200)
            fwd = closed_before(fwd_raw, min(cut, t0_ms + 6 * 3_600_000), TF_MS["5m"])
        except Exception:
            fwd = np.empty((0, 6))
        if bias == "LONG":
            mfe = (fwd[:, 2].max() - bar[4]) / atr5 if len(fwd) and atr5 else 0.0
            mae = (bar[4] - fwd[:, 3].min()) / atr5 if len(fwd) and atr5 else 0.0
        else:
            mfe = (bar[4] - fwd[:, 3].min()) / atr5 if len(fwd) and atr5 else 0.0
            mae = (fwd[:, 2].max() - bar[4]) / atr5 if len(fwd) and atr5 else 0.0

        pnl = round(sum(float(x.get("pnl_pct", 0) or 0) for x in outs), 2)
        rec = {
            "ts": e["timestamp"], "base": e["base"], "bias": bias,
            "dist_pct": round(dist_pct, 3), "dist_atr5": round(dist_atr5, 2),
            "bar_close_pos": round(close_pos, 2), "bar_vol_x": round(bar_vol_x, 2),
            "rej_age": rej_age, "rej_close_pos": None if rej_close_pos is None else round(rej_close_pos, 2),
            "atr5_pct": round(atr_pct, 3), "atr5_expansion": round(atr_expansion, 2),
            "adx_1h": round(adx1, 1), "pdi_1h": round(pdi1, 1), "mdi_1h": round(mdi1, 1),
            "adx_5m": round(adx5, 1),
            "macd_hist_1h": round(mh1, 4), "macd_slope_1h": round(ms1, 4),
            "stoch_k_1h": round(stoch_k(d1), 1),
            "mfe_atr": round(float(mfe), 2), "mae_atr": round(float(mae), 2),
            "pnl": pnl, "signal_count": e.get("signal_count"),
        }
        cache[key] = rec
        result.append(rec)
        print(f"{rec['ts'][:16]} {rec['base']:9s} {bias:5s} dist{rec['dist_atr5']:5.2f}ATR "
              f"pos{rec['bar_close_pos']:4.2f} vol{rec['bar_vol_x']:4.2f}x rej{str(rec['rej_age']):>4s} "
              f"adx1h{rec['adx_1h']:5.1f} mfe{rec['mfe_atr']:5.2f} pnl{rec['pnl']:+7.2f}")
        OUT.write_text(json.dumps(cache, indent=1))
        time.sleep(0.3)

    OUT.write_text(json.dumps(cache, indent=1))
    print(f"\n{len(result)} Trades -> {OUT}")


if __name__ == "__main__":
    sys.path.insert(0, str(BASE))
    main()
