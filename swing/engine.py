"""Swing-Engine: ein Scan pro Tag, max. 1 Position, nur LONG.

Ablauf: 1D-Richtung (Trend) → Ranking nach ADX → 4H-Pullback-Trigger → S/R-Ziel
und R:R-Filter → Entry. Exits: Trendbruch, Not-SL, TP, Freitags-Close, Max-Hold.

`scan_from_data` ist rein (testbar ohne Netz); `load_data` holt die Kerzen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import talib

from . import sr
from .strategy import MIN_BARS, indicators, trend_stance_at

DAY_MS = 86_400_000
H4_MS = 14_400_000
MIN_BARS_4H = 60
RISK_ATR_CAP = 1.5       # Not-SL höchstens 1,5 ATR (sonst passt das R:R nie)
MIN_RR = 1.5             # Ziel/Risiko-Mindestmaß
NOT_AT_TOP_ATR = 0.5     # mind. 0,5 ATR Luft zum letzten Swing-Hoch
TARGET_MIN_ATR = 1.5     # Widerstand näher = kein Platz -> kein Trade (~50% Trefferquote)
TARGET_MAX_ATR = 4.0     # weiter weg = nicht realistisch -> kein Trade
MAX_HOLD_DAYS = 20
# Der Job läuft EINMAL täglich (15:35). "Freitag" ist daher der Freitags-Lauf
# selbst — kein separates Zeitfenster, das nie erreicht würde.
# "protect": Freitag nur schließen, wenn die Position im Minus ist (kein
# Wochenend-Gap im Verlierer); Gewinner laufen weiter, bis der Trend bricht.
# "always": jeden Freitag zwangsweise flat. "off": kein Freitags-Eingriff.
FRIDAY_MODE = "protect"
# Der 4H-Pullback-Trigger ist NICHT backgetestet (der Backtest war tagesbasiert)
# und blockierte den einzigen Kandidaten. Bis er separat gemessen ist, bleibt er
# aus: Entry erfolgt auf das Tagessignal. Auf True setzen, sobald belegt.
USE_H4_TRIGGER = False


def closed(candles: list[list], now_ms: int, tf_ms: int) -> list[list]:
    """Nur abgeschlossene Kerzen (die laufende fällt raus)."""
    return [c for c in candles if c[0] + tf_ms <= now_ms]


def _friday_close_window(now: datetime) -> bool:
    """Freitags-Lauf = Wochenend-Risikocheck (der Job läuft nur 1×/Tag)."""
    return now.astimezone(ZoneInfo("Europe/Berlin")).weekday() == 4


def daily_view(candles: list[list], now_ms: int,
               live_price: float | None = None) -> dict | None:
    """Tagesbild. `live_price` ist der aktuelle Kurs (der letzte Tages-Close ist
    beim Lauf um 15:35 bereits ~14 h alt) — Levels und Entscheidungen rechnen
    damit, die Indikatoren bleiben auf den abgeschlossenen Tageskerzen."""
    rows = closed(candles, now_ms, DAY_MS)
    if len(rows) < MIN_BARS:
        return None
    close = np.array([r[4] for r in rows], dtype=float)
    ind = indicators([r[2] for r in rows], [r[3] for r in rows], close)
    i = len(rows) - 1
    if not np.isfinite(ind["atr"][i]) or not np.isfinite(ind["adx"][i]):
        return None
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    sh, sl = sr.swing_highs(highs), sr.swing_lows(lows)
    price = float(live_price) if live_price else float(close[i])
    return {
        "date": datetime.fromtimestamp(rows[i][0] / 1000, timezone.utc).date().isoformat(),
        "price": price,
        "stance": trend_stance_at(ind, close, i),
        "adx": float(ind["adx"][i]),
        "atr": float(ind["atr"][i]),
        "swing_high": sr.last_swing_high(sh, price),
        "swing_low": sr.last_swing_low(sl, price),
        "resistance": sr.next_resistance(price, sh),
        "support": sr.next_support(price, sl),
    }


def h4_trigger(candles: list[list], now_ms: int) -> dict:
    rows = closed(candles, now_ms, H4_MS)
    if len(rows) < MIN_BARS_4H:
        return {"ok": False, "reason": "zu wenig 4H-Historie"}
    close = np.array([r[4] for r in rows], dtype=float)
    ema20 = talib.EMA(close, timeperiod=20)
    i = len(rows) - 1
    if not np.isfinite(ema20[i]):
        return {"ok": False, "reason": "4H-EMA nicht bereit"}
    lows = [r[3] for r in rows]
    pulled_back = min(lows[i - 5:i + 1]) < ema20[i]
    resumed = close[i] > rows[i - 1][2] and close[i] > ema20[i]
    if pulled_back and resumed:
        return {"ok": True, "reason": "4H-Pullback + Resume"}
    return {"ok": False, "reason": "4H: kein Pullback/Resume"}


def entry_plan(view: dict, trig: dict) -> dict:
    """S/R-Ziel, Stop und R:R für einen LONG-Kandidaten."""
    if view["stance"] != "LONG":
        return {"ok": False, "reason": "kein 1D-LONG"}
    if not trig["ok"]:
        return {"ok": False, "reason": trig["reason"]}
    price, atr = view["price"], view["atr"]
    if view["swing_high"] and (view["swing_high"] - price) < NOT_AT_TOP_ATR * atr:
        return {"ok": False, "reason": "zu nah am letzten Hoch (kein Nachkaufen)"}
    stop_struct = view["swing_low"]
    stop = max(stop_struct, price - RISK_ATR_CAP * atr) if stop_struct \
        else price - RISK_ATR_CAP * atr
    if stop >= price:
        return {"ok": False, "reason": "Stop über Entry"}
    far = view["resistance"]
    if far is None:
        target = price + 2.5 * atr
    else:
        dist_atr = (far - price) / atr
        if dist_atr < TARGET_MIN_ATR:
            return {"ok": False, "reason": "Widerstand zu nah (kein Platz)"}
        if dist_atr > TARGET_MAX_ATR:
            return {"ok": False, "reason": "Widerstand zu weit (unrealistisch)"}
        target = far
    rr = sr.risk_reward(price, stop, target)
    if rr is None or rr < MIN_RR:
        return {"ok": False, "reason": f"R:R {rr:.2f} < {MIN_RR}" if rr else "kein R:R"}
    return {"ok": True, "reason": "setup", "entry": price, "stop": stop,
            "target": target, "rr": round(rr, 2)}


def position_decision(pos: dict, view: dict, now: datetime) -> dict:
    """Exit-Entscheidung für eine offene Position.

    Wird sowohl vom Tages-Scan als auch vom Exit-Wächter (alle 30 min) genutzt,
    damit Stop/TP nicht erst am nächsten Tag auffallen.
    """
    held = (now.date() - datetime.fromisoformat(pos["opened_at"]).date()).days
    losing = view["price"] <= pos["entry"]
    if _friday_close_window(now) and (
            FRIDAY_MODE == "always" or (FRIDAY_MODE == "protect" and losing)):
        reason = "Freitags-Close (Verlierer)" if losing else "Freitags-Close"
        return {"action": "CLOSE", "reason": reason, "price": view["price"]}
    if view["stance"] != "LONG":
        return {"action": "CLOSE", "reason": "Trendbruch (1D)", "price": view["price"]}
    if view["price"] <= pos["stop"]:
        return {"action": "CLOSE", "reason": "Not-SL", "price": pos["stop"]}
    if view["price"] >= pos["tp"] and not pos.get("partial_done"):
        return {"action": "PARTIAL", "reason": "TP1 erreicht: 50% raus",
                "price": pos["tp"], "remaining_pct": 50.0}
    if held >= MAX_HOLD_DAYS:
        return {"action": "CLOSE", "reason": "Max-Hold", "price": view["price"]}
    return {"action": "HOLD", "reason": "", "price": view["price"]}


def scan_from_data(data: dict, now: datetime, state: dict) -> dict:
    """data: {base: {'d1': [...], 'h4': [...]}}. Rein, ohne Netz."""
    now_ms = int(now.timestamp() * 1000)
    pos = state.get("position")
    out = {"as_of": now.isoformat(), "position": pos, "action": "HOLD",
           "candidates": [], "rejects": {}, "reason": ""}

    # 1) offene Position verwalten
    if pos:
        base = pos["base"]
        v = daily_view(data[base]["d1"], now_ms,
                       data[base].get("last")) if base in data else None
        if v is None:
            out.update(action="NONE", reason="keine Daten für offene Position")
            return out
        out.update(position_decision(pos, v, now))
        return out

    if state.get("last_entry_date") == now.date().isoformat():
        out.update(action="NONE", reason="heute schon ein Entry")
        return out

    # 2) Kandidaten ranken
    cands = []
    funnel = {"total": len(data), "long": 0, "trigger": 0, "rr_ok": 0, "long_bases": []}
    for base, frames in data.items():
        v = daily_view(frames["d1"], now_ms, frames.get("last"))
        if v is None:
            out["rejects"][base] = "zu wenig 1D-Historie"
            continue
        if v["stance"] != "LONG":
            out["rejects"][base] = ("Trend abwärts" if v["stance"] == "SHORT"
                                    else "kein Trend (seitwärts)")
            continue
        funnel["long"] += 1
        funnel["long_bases"].append(base)
        if USE_H4_TRIGGER:
            trig = h4_trigger(frames["h4"], now_ms)
            if not trig["ok"]:
                out["rejects"][base] = trig["reason"]
                continue
            funnel["trigger"] += 1
        else:
            trig = {"ok": True, "reason": "kein 4H-Filter"}
            funnel["trigger"] += 1
        plan = entry_plan(v, trig)
        if not plan["ok"]:
            out["rejects"][base] = plan["reason"]
            continue
        funnel["rr_ok"] += 1
        cands.append({"base": base, "symbol": frames.get("symbol", base),
                      "adx": round(v["adx"], 1), **{k: plan[k] for k in
                                                    ("entry", "stop", "target", "rr")}})
    out["funnel"] = funnel
    cands.sort(key=lambda c: -c["adx"])
    out["candidates"] = cands
    if not cands:
        out.update(action="NONE", reason="kein Kandidat mit Setup + R:R")
        return out
    best = cands[0]
    out.update(action="ENTRY", reason="bestes Setup (ADX-Ranking)", pick=best)
    return out


def _rescale(underlying: list[list], perp: list[list]) -> list[list]:
    """Underlying-Kerzen auf das Preisniveau des Perps bringen.

    Faktor = Perp-Schluss / Underlying-Schluss am letzten gemeinsamen Tag.
    Damit stimmen Entry/Stop/Ziel in Perp-Preisen, auch wenn der Perp jung ist.
    """
    if not perp or not underlying:
        return underlying
    p_row = perp[-1]
    p_date = datetime.fromtimestamp(p_row[0] / 1000, timezone.utc).date()
    same = [r for r in underlying
            if datetime.fromtimestamp(r[0] / 1000, timezone.utc).date() == p_date]
    ref = same[-1] if same else underlying[-1]
    if not ref[4]:
        return underlying
    f = p_row[4] / ref[4]
    return [[r[0], r[1] * f, r[2] * f, r[3] * f, r[4] * f, r[5]] for r in underlying]


def load_data(ex, perps: list[dict], now: datetime, fetcher=None) -> dict:
    """1D- und 4H-Kerzen je Perp.

    1D aus dem Perp, wenn genug Historie; sonst aus dem Underlying (20 Jahre)
    und auf Perp-Preise skaliert. 4H kommt immer vom Perp.
    """
    from . import twelvedata
    from .data import underlying_for
    fetcher = fetcher or twelvedata.daily_ms
    now_ms = int(now.timestamp() * 1000)
    data = {}
    for p in perps:
        try:
            d1 = ex.fetch_ohlcv(p["symbol"], "1d", limit=300)
        except Exception:
            continue
        try:
            h4 = ex.fetch_ohlcv(p["symbol"], "4h", limit=200)
        except Exception:
            h4 = []
        if len(closed(d1, now_ms, DAY_MS)) < MIN_BARS:
            und = underlying_for(p["base"])
            rows = fetcher(und) if und else None
            if rows:
                d1 = _rescale(rows, d1)
        data[p["base"]] = {"d1": d1, "h4": h4, "symbol": p["symbol"],
                           "last": (h4[-1][4] if h4 else None)}
    return data
