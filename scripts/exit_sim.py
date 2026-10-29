#!/usr/bin/env python3
"""
graviton/scripts/exit_sim.py — Gegenprobe fuer Exit-Regeln auf echten Kerzen.

Frage: bringt ein weiter Sicherheits-SL (10%) + Close am Session-Ende mehr als
der enge ATR-SL (0.9-5%)?

Fuer jeden Entry ab 2026-09-15 werden Kraken-5m-Kerzen von Entry bis
Session-Close (17:00 UTC) geladen und drei Szenarien gerechnet:

  A  Ist-Zustand (PnL aus dem Trade-Log)
  B  10%-Sicherheits-SL, sonst Close um 17:00 UTC
  C  wie B, aber zusaetzlich TP bei +2.5% (Voll-Exit)
  D  wie C, aber TP schliesst nur 50%, Rest laeuft bis Session-Close

Aufruf: python3 scripts/exit_sim.py [--refresh]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG = ROOT / "data" / "trade_log.jsonl"
FORENSICS = ROOT / "data" / "entry_forensics.json"
CACHE = ROOT / "data" / "exit_sim_cache.json"
OUT = ROOT / "data" / "exit_sim.json"

SESSION_CLOSE_UTC = "17:00"
SAFETY_SL_PCT = 10.0
TP_PCT = 2.5


def _session_close(ts: datetime) -> datetime:
    hh, mm = (int(x) for x in SESSION_CLOSE_UTC.split(":"))
    close = ts.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if close <= ts:
        close += timedelta(days=1)
    return close


def _simulate(bars, *, bias: str, close_ms: int, safety_sl=SAFETY_SL_PCT,
              tp=None, half=False):
    """Retour (pnl_pct, reason). bars = [[ts,o,h,l,c,v], ...] ab Entry."""
    entry = bars[0][4]
    tp_done = False
    realized = 0.0
    weight = 0.0                      # bereits realisierter Anteil
    for ts, _o, h, l, c, _v in bars:
        if ts > close_ms:
            break
        if bias == "SHORT":
            adverse = (h - entry) / entry * 100 * -1     # negativ = gegen uns
            favorable = (entry - l) / entry * 100
        else:
            adverse = (l - entry) / entry * 100          # negativ = gegen uns
            favorable = (h - entry) / entry * 100
        if adverse <= -safety_sl:
            if weight:
                return realized + (1 - weight) * (-safety_sl), "safety_sl(teil)"
            return -safety_sl, "safety_sl"
        if tp is not None and not tp_done and favorable >= tp:
            tp_done = True
            if not half:
                return tp, "tp"
            realized += 0.5 * tp
            weight = 0.5
    end = [b for b in bars if b[0] <= close_ms]
    if not end:
        return None, "no_data"
    c = end[-1][4]
    pnl = ((entry - c) / entry * 100) if bias == "SHORT" else ((c - entry) / entry * 100)
    if weight:
        return realized + (1 - weight) * pnl, "tp50+close"
    return pnl, "close"


def main() -> int:
    refresh = "--refresh" in sys.argv
    cache = {}
    if CACHE.exists() and not refresh:
        cache = json.loads(CACHE.read_text())

    forensics = json.loads(FORENSICS.read_text()) if FORENSICS.exists() else {}
    by_ts = {v["ts"]: v for v in forensics.values()}

    rows = [json.loads(line) for line in LOG.open()]
    entries = [r for r in rows if r.get("event") == "entry"
               and r["timestamp"][:10] >= "2026-09-15"]

    ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    result = []
    for e in entries:
        key = e["timestamp"] + e["symbol"]
        ts = datetime.fromisoformat(e["timestamp"][:19] + "+00:00")
        close = _session_close(ts)
        if key not in cache:
            t0_ms = int(ts.timestamp() * 1000)
            try:
                raw = ex.fetch_ohlcv(e["symbol"], "5m", since=t0_ms, limit=300)
            except Exception as exc:                     # noqa: BLE001
                print(f"FETCH-ERR {e['base']}: {exc}")
                continue
            cache[key] = [[int(b[0]), b[1], b[2], b[3], b[4], b[5]] for b in raw]
        bars = cache[key]
        if not bars:
            continue
        close_ms = int(close.timestamp() * 1000)
        actual = (by_ts.get(e["timestamp"]) or {}).get("pnl")
        sim_b, why_b = _simulate(bars, bias=e["bias"], close_ms=close_ms)
        sim_c, why_c = _simulate(bars, bias=e["bias"], close_ms=close_ms, tp=TP_PCT)
        sim_d, why_d = _simulate(bars, bias=e["bias"], close_ms=close_ms, tp=TP_PCT, half=True)
        result.append({"symbol": e["symbol"], "bias": e["bias"], "ts": e["timestamp"],
                       "actual": actual, "b": sim_b, "b_reason": why_b,
                       "c": sim_c, "c_reason": why_c,
                       "d": sim_d, "d_reason": why_d})

    CACHE.write_text(json.dumps(cache))
    OUT.write_text(json.dumps(result, indent=1))

    valid = [r for r in result if r["actual"] is not None and r["b"] is not None]
    print(f"Trades: {len(valid)}/{len(result)}\n")
    for name, key in (("A Ist (ATR-SL)", "actual"), ("B 10%-SL + Close", "b"),
                      ("C B + TP 2.5%", "c"), ("D B + TP 50%", "d")):
        vals = [r[key] for r in valid if r[key] is not None]
        wins = sum(1 for v in vals if v > 0)
        total = sum(vals)
        avg = total / len(vals) if vals else 0
        print(f"{name:18s} n={len(vals):2d}  Summe {total:+7.1f}%  Ø {avg:+.2f}%  WR {wins/len(vals)*100:3.0f}%")

    # ── Varianten-Matrix: SL-Floor und TP-Abstand (TP = R-Vielfaches des SL) ──
    print("\nVarianten (Close immer Session-Ende, kein Trailing):")
    print(f"{'SL':>6} {'TP':>8}  {'n':>3}  {'Summe':>8}  {'Ø':>7}  {'WR':>4}")
    for sl in (0.9, 1.5, 2.0, 3.0, 5.0, 10.0, 99.0):
        for rr in (None, 1.5, 2.0, 3.0):
            tp = None if rr is None else sl * rr
            vals = []
            for r in valid:
                key = r["ts"] + r["symbol"]
                bars = cache.get(key) or []
                if not bars:
                    continue
                close_ms = int(_session_close(
                    datetime.fromisoformat(r["ts"][:19] + "+00:00")).timestamp() * 1000)
                pnl, _why = _simulate(bars, bias=r["bias"], close_ms=close_ms,
                                      safety_sl=sl, tp=tp)
                if pnl is not None:
                    vals.append(pnl)
            if not vals:
                continue
            wins = sum(1 for v in vals if v > 0)
            print(f"{sl:>6.1f} {('-' if tp is None else f'{tp:.1f}%'):>8}  "
                  f"{len(vals):>3}  {sum(vals):>+8.1f}%  {sum(vals)/len(vals):>+6.2f}%  "
                  f"{wins/len(vals)*100:>3.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
