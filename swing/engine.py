"""Swing-Engine: ein Scan pro Tag, max. 1 Position, Long und Short (Papier).

Ablauf: 1D-Richtung (Trend) → Ranking nach ADX → Pullback statt Kauf am Hoch
→ Ziel aus jungen Pivots und R:R-Filter → Entry. Exits: Trendbruch, Not-SL,
TP, Freitags-Close, Max-Hold in Tageskerzen.

`scan_from_data` ist rein (testbar ohne Netz); `load_data` holt die Kerzen.
Alle Symbole liegen auf Handelstagen (Mo–Fr), nicht auf 7-Tage-Perp-Kerzen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import talib

from . import config, sr
from .strategy import MIN_BARS, indicators, trend_stance_at

DAY_MS = 86_400_000
H4_MS = 14_400_000
MIN_BARS_4H = 60
RISK_ATR_CAP = 1.5       # Not-SL höchstens 1,5 ATR (sonst passt das R:R nie)
MIN_STOP_ATR = 0.8       # enger als das ist kein Stop, sondern Rauschen (bläht R:R)
STOP_BUFFER_ATR = 0.25   # Puffer hinter dem Swing, damit ein Docht nicht ausstoppt
MIN_RR = 1.5             # Ziel/Risiko-Mindestmaß
NOT_AT_TOP_ATR = 0.5     # mind. 0,5 ATR Luft zum letzten Swing-Extrem (Pullback)
TARGET_MIN_ATR = 1.5     # Niveau näher = kein Platz -> kein Trade
TARGET_MAX_ATR = 4.0     # weiter weg = nicht das Ziel; dann gemessenes ATR-Ziel
MEASURED_TARGET_ATR = 2.5
MAX_HOLD_BARS = 20       # Haltedauer in geschlossenen Tageskerzen, nicht Kalendertagen
MAX_HOLD_DAYS = MAX_HOLD_BARS  # alter Name, gleiche Bedeutung
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


def trading_day_candles(candles: list[list]) -> list[list]:
    """Samstag/Sonntag (UTC) streichen.

    Kraken-Perps liefern 7-Tage-Kerzen, TwelveData liefert Börsentage.
    Beides auf Mo–Fr zu bringen ist die gemeinsame Tagesbasis. Feiertage
    bleiben drin, wenn der Perp gehandelt hat — das ist näher an einem
    Handelstag als ein Wochenend-Balken.
    """
    out = []
    for c in candles:
        if not c:
            continue
        wd = datetime.fromtimestamp(c[0] / 1000, timezone.utc).weekday()
        if wd < 5:
            out.append(c)
    return out


def bars_held(candles: list[list], opened_at: str, now_ms: int) -> int:
    """Geschlossene Tageskerzen nach dem Entry-Tag.

    Wochenenden zählen nicht mit, sobald die Serie Handelstage ist.
    Der Entry-Tag selbst zählt nicht: der Lauf ist mitten in dieser Kerze.
    """
    opened = datetime.fromisoformat(opened_at)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    open_day = opened.astimezone(timezone.utc).date()
    n = 0
    for c in closed(candles, now_ms, DAY_MS):
        day = datetime.fromtimestamp(c[0] / 1000, timezone.utc).date()
        if day > open_day:
            n += 1
    return n


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
    atr = float(ind["atr"][i])
    price = float(live_price) if live_price else float(close[i])
    mh = sr.meaningful_swings(highs, kind="high", atr=atr)
    ml = sr.meaningful_swings(lows, kind="low", atr=atr)
    return {
        "date": datetime.fromtimestamp(rows[i][0] / 1000, timezone.utc).date().isoformat(),
        "price": price,
        "stance": trend_stance_at(ind, close, i),
        "adx": float(ind["adx"][i]),
        "atr": atr,
        # Letztes bestätigtes Hoch, auch wenn der Kurs schon darüber ist.
        # Liegt es unter dem Kurs, ist das ein frisches Hoch — kein Pullback.
        "swing_high": sr.latest_level(mh),
        # Nächstes deutliches Tief unter dem Kurs: Anker für den Long-Stop.
        "swing_low": sr.next_support(price, ml),
        "impulse_low": sr.latest_level(ml),
        "stop_high": sr.next_resistance(price, mh),
        "resistance": sr.target_level(
            price, atr, mh, side="LONG",
            min_atr=TARGET_MIN_ATR, max_atr=TARGET_MAX_ATR),
        "support": sr.target_level(
            price, atr, ml, side="SHORT",
            min_atr=TARGET_MIN_ATR, max_atr=TARGET_MAX_ATR),
    }


def h4_trigger(candles: list[list], now_ms: int, side: str = "LONG") -> dict:
    rows = closed(candles, now_ms, H4_MS)
    if len(rows) < MIN_BARS_4H:
        return {"ok": False, "reason": "zu wenig 4H-Historie"}
    close = np.array([r[4] for r in rows], dtype=float)
    ema20 = talib.EMA(close, timeperiod=20)
    i = len(rows) - 1
    if not np.isfinite(ema20[i]):
        return {"ok": False, "reason": "4H-EMA nicht bereit"}
    if side == "SHORT":
        highs = [r[2] for r in rows]
        rallied = max(highs[i - 5:i + 1]) > ema20[i]
        resumed = close[i] < rows[i - 1][3] and close[i] < ema20[i]
        if rallied and resumed:
            return {"ok": True, "reason": "4H-Rally + Resume"}
        return {"ok": False, "reason": "4H: kein Rally/Resume"}
    lows = [r[3] for r in rows]
    pulled_back = min(lows[i - 5:i + 1]) < ema20[i]
    resumed = close[i] > rows[i - 1][2] and close[i] > ema20[i]
    if pulled_back and resumed:
        return {"ok": True, "reason": "4H-Pullback + Resume"}
    return {"ok": False, "reason": "4H: kein Pullback/Resume"}


def _clamp_stop(price: float, atr: float, raw: float, side: str) -> float:
    """Stop in das Band [Mindestabstand, ATR-Deckel] ziehen."""
    if side == "LONG":
        furthest = price - RISK_ATR_CAP * atr
        nearest = price - MIN_STOP_ATR * atr
        return min(max(raw, furthest), nearest)
    furthest = price + RISK_ATR_CAP * atr
    nearest = price + MIN_STOP_ATR * atr
    return max(min(raw, furthest), nearest)


def _plan_side(view: dict, side: str) -> dict:
    """Pullback-Plan. Long kauft die Rücksetzer, Short die Rally in den Widerstand."""
    if side == "LONG" and not config.ALLOW_LONG:
        return {"ok": False, "reason": "Long ausgeschaltet"}
    if side == "SHORT" and not config.ALLOW_SHORT:
        return {"ok": False, "reason": "Short ausgeschaltet"}
    price, atr = view["price"], view["atr"]
    if not atr or atr <= 0 or not price:
        return {"ok": False, "reason": "kein ATR"}

    if side == "LONG":
        anchor = view.get("swing_high")
        gap = None if anchor is None else (anchor - price)
        near = "zu nah am letzten Hoch (kein Nachkaufen)"
        far_level = view.get("resistance")
        level_word = "Widerstand"
        ref = view.get("swing_low")
        if ref is not None and ref < price:
            raw = ref - STOP_BUFFER_ATR * atr
        else:
            raw = price - atr
        stop_bad = "Stop über Entry"
    else:
        anchor = view.get("impulse_low", view.get("swing_low"))
        gap = None if anchor is None else (price - anchor)
        near = "zu nah am letzten Tief (kein Short am Tief)"
        far_level = view.get("support")
        level_word = "Unterstützung"
        ref = view.get("stop_high")
        if ref is None:
            maybe = view.get("swing_high")
            if maybe is not None and maybe > price:
                ref = maybe
        if ref is not None and ref > price:
            raw = ref + STOP_BUFFER_ATR * atr
        else:
            raw = price + atr
        stop_bad = "Stop unter Entry"

    # Frisches Extrem: kein bestätigter Pivot auf der richtigen Seite, oder
    # der letzte Pivot liegt schon hinter dem Kurs. Beides ist kein Pullback.
    if gap is None or gap < NOT_AT_TOP_ATR * atr:
        return {"ok": False, "reason": near}

    stop = _clamp_stop(price, atr, raw, side)
    if side == "LONG" and stop >= price:
        return {"ok": False, "reason": stop_bad}
    if side == "SHORT" and stop <= price:
        return {"ok": False, "reason": stop_bad}

    sign = 1.0 if side == "LONG" else -1.0
    if far_level is None:
        target = price + sign * MEASURED_TARGET_ATR * atr
    else:
        dist_atr = sign * (far_level - price) / atr
        if dist_atr < TARGET_MIN_ATR:
            return {"ok": False, "reason": f"{level_word} zu nah (kein Platz)"}
        if dist_atr > TARGET_MAX_ATR:
            return {"ok": False, "reason": f"{level_word} zu weit (unrealistisch)"}
        target = far_level
    rr = sr.risk_reward(price, stop, target, side)
    if rr is None or rr < MIN_RR:
        return {"ok": False, "reason": f"R:R {rr:.2f} < {MIN_RR}" if rr else "kein R:R"}
    return {"ok": True, "reason": "setup", "side": side, "entry": price,
            "stop": stop, "target": target, "rr": round(rr, 2)}


def entry_plan(view: dict, trig: dict) -> dict:
    """S/R-Ziel, Stop und R:R. Long und Short, je nach `view['stance']`."""
    if not trig["ok"]:
        return {"ok": False, "reason": trig["reason"]}
    side = view.get("stance")
    if side == "LONG":
        return _plan_side(view, "LONG")
    if side == "SHORT":
        return _plan_side(view, "SHORT")
    return {"ok": False, "reason": "kein 1D-LONG" if side is None else "kein 1D-Trend"}


def _losing(pos: dict, price: float) -> bool:
    side = (pos.get("side") or "LONG").upper()
    if side == "SHORT":
        return price >= pos["entry"]
    return price <= pos["entry"]


def position_decision(pos: dict, view: dict, now: datetime) -> dict:
    """Exit-Entscheidung für eine offene Position.

    Wird sowohl vom Tages-Scan als auch vom Exit-Wächter (alle 30 min) genutzt,
    damit Stop/TP nicht erst am nächsten Tag auffallen.
    `view['bars_held']` ist die Haltedauer in Tageskerzen. Fehlt sie (alte
    Aufrufer), zählen Kalendertage — der Scan und der Wächter setzen sie.
    """
    side = (pos.get("side") or "LONG").upper()
    if "bars_held" in view:
        held = view["bars_held"]
    else:
        held = (now.date() - datetime.fromisoformat(pos["opened_at"]).date()).days
    losing = _losing(pos, view["price"])
    if _friday_close_window(now) and (
            FRIDAY_MODE == "always" or (FRIDAY_MODE == "protect" and losing)):
        reason = "Freitags-Close (Verlierer)" if losing else "Freitags-Close"
        return {"action": "CLOSE", "reason": reason, "price": view["price"]}
    if view["stance"] != side:
        return {"action": "CLOSE", "reason": "Trendbruch (1D)", "price": view["price"]}
    if side == "LONG":
        if view["price"] <= pos["stop"]:
            return {"action": "CLOSE", "reason": "Not-SL", "price": pos["stop"]}
        if view["price"] >= pos["tp"] and not pos.get("partial_done"):
            return {"action": "PARTIAL", "reason": "TP1 erreicht: 50% raus",
                    "price": pos["tp"], "remaining_pct": 50.0}
    else:
        if view["price"] >= pos["stop"]:
            return {"action": "CLOSE", "reason": "Not-SL", "price": pos["stop"]}
        if view["price"] <= pos["tp"] and not pos.get("partial_done"):
            return {"action": "PARTIAL", "reason": "TP1 erreicht: 50% raus",
                    "price": pos["tp"], "remaining_pct": 50.0}
    if held >= MAX_HOLD_BARS:
        return {"action": "CLOSE", "reason": "Max-Hold", "price": view["price"]}
    return {"action": "HOLD", "reason": "", "price": view["price"]}


def _empty_funnel(n: int) -> dict:
    return {"total": n, "long": 0, "short": 0, "trigger": 0, "rr_ok": 0,
            "long_bases": [], "short_bases": []}


def scan_from_data(data: dict, now: datetime, state: dict) -> dict:
    """data: {base: {'d1': [...], 'h4': [...], optional 'error'}}. Rein, ohne Netz."""
    now_ms = int(now.timestamp() * 1000)
    pos = state.get("position")
    out = {"as_of": now.isoformat(), "position": pos, "action": "HOLD",
           "candidates": [], "rejects": {}, "data_errors": {}, "reason": ""}

    # 1) offene Position verwalten
    if pos:
        base = pos["base"]
        frames = data.get(base)
        if not frames or frames.get("error"):
            msg = (frames or {}).get("error") or "keine Daten für offene Position"
            if frames and frames.get("error"):
                out["data_errors"][base] = frames["error"]
            out.update(action="NONE", reason=msg)
            return out
        v = daily_view(frames["d1"], now_ms, frames.get("last"))
        if v is None:
            out.update(action="NONE", reason="zu wenig 1D-Historie für offene Position")
            return out
        v["bars_held"] = bars_held(frames["d1"], pos["opened_at"], now_ms)
        out.update(position_decision(pos, v, now))
        return out

    if state.get("last_entry_date") == now.date().isoformat():
        out.update(action="NONE", reason="heute schon ein Entry")
        return out

    # 2) Kandidaten ranken. Datenfehler sind kein Setup und kein „zu wenig Historie“.
    tradable = {}
    for base, frames in data.items():
        err = frames.get("error")
        if err:
            out["data_errors"][base] = err
            continue
        tradable[base] = frames

    funnel = _empty_funnel(len(tradable))
    for base, frames in tradable.items():
        v = daily_view(frames["d1"], now_ms, frames.get("last"))
        if v is None:
            out["rejects"][base] = "zu wenig 1D-Historie"
            continue
        stance = v["stance"]
        if stance == "LONG":
            if not config.ALLOW_LONG:
                out["rejects"][base] = "Long ausgeschaltet"
                continue
            funnel["long"] += 1
            funnel["long_bases"].append(base)
        elif stance == "SHORT":
            if not config.ALLOW_SHORT:
                out["rejects"][base] = "Short ausgeschaltet"
                continue
            funnel["short"] += 1
            funnel["short_bases"].append(base)
        else:
            out["rejects"][base] = "kein Trend (seitwärts)"
            continue
        if USE_H4_TRIGGER:
            trig = h4_trigger(frames.get("h4") or [], now_ms, side=stance)
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
        out["candidates"].append({
            "base": base, "symbol": frames.get("symbol", base),
            "side": plan.get("side", stance), "adx": round(v["adx"], 1),
            **{k: plan[k] for k in ("entry", "stop", "target", "rr")},
        })
    out["funnel"] = funnel
    cands = out["candidates"]
    cands.sort(key=lambda c: -c["adx"])
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


def _data_error(where: str, exc: Exception) -> str:
    msg = str(exc).strip().replace("\n", " ")
    if msg.lower().startswith("datenfehler"):
        return msg[:180]
    if msg.startswith("TwelveData"):
        return f"Datenfehler: {msg[:160]}"
    detail = msg[:120] if msg else exc.__class__.__name__
    return f"Datenfehler: {where} ({detail})"[:180]


def load_data(ex, perps: list[dict], now: datetime, fetcher=None) -> dict:
    """1D- und 4H-Kerzen je Perp. Jedes Symbol kommt zurück, auch bei Fehler.

    1D ist immer eine Handelstagsserie: Kraken-Wochenenden fallen raus. Reicht
    das nicht für den Warmup, wird das Underlying (TwelveData, ebenfalls
    Handelstage) geholt und auf den Perp skaliert. Schlägt der Abruf fehl,
    steht der Grund in `error` — das Symbol wird nicht still übersprungen.
    4H kommt vom Perp und ist nur der optionale Trigger, kein Tages-Ersatz.
    """
    from . import twelvedata
    from .data import underlying_for
    fetcher = fetcher or twelvedata.load_daily_ms
    now_ms = int(now.timestamp() * 1000)
    data = {}
    for p in perps:
        base, symbol = p["base"], p["symbol"]
        slot = {"d1": [], "h4": [], "symbol": symbol, "last": None, "error": None}
        try:
            raw = ex.fetch_ohlcv(symbol, "1d", limit=300)
        except Exception as exc:
            slot["error"] = _data_error("Kraken-Kerzen nicht ladbar", exc)
            data[base] = slot
            continue
        if not raw:
            slot["error"] = "Datenfehler: Kraken lieferte keine Tageskerzen"
            data[base] = slot
            continue
        try:
            h4 = ex.fetch_ohlcv(symbol, "4h", limit=200)
        except Exception:
            h4 = []
        d1 = trading_day_candles(raw)
        if len(closed(d1, now_ms, DAY_MS)) < MIN_BARS:
            und = underlying_for(base)
            if und:
                try:
                    rows = fetcher(und)
                except Exception as exc:
                    slot["error"] = _data_error(f"TwelveData {und}", exc)
                    slot["h4"] = h4
                    slot["last"] = h4[-1][4] if h4 else None
                    data[base] = slot
                    continue
                if not rows:
                    slot["error"] = f"Datenfehler: TwelveData {und} lieferte nichts"
                    slot["h4"] = h4
                    slot["last"] = h4[-1][4] if h4 else None
                    data[base] = slot
                    continue
                d1 = _rescale(trading_day_candles(rows), raw)
        slot["d1"] = d1
        slot["h4"] = h4
        slot["last"] = h4[-1][4] if h4 else None
        data[base] = slot
    return data
