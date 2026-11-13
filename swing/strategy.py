"""1D-Richtungs-Engine für Swing (Kronos-Logik, auf Tageskerzen adaptiert).

Richtung entsteht NUR auf 1D. Trigger ist ein Stochastik-Extrem, bestätigt durch
die Familien MACD, RSI, EMA-Struktur und DI (>=3 von 4). Die EMA200 wirkt als
grober Regime-Rahmen (Long nur über, Short nur unter der 200er) — kein Cross-Signal,
kein Distance-Gate.
"""
from __future__ import annotations

import numpy as np
import talib

STOCH_TOP = 70.0
STOCH_BOTTOM = 30.0
VOTE_THRESHOLD = 3
RSI_TOP = 70.0
RSI_BOTTOM = 30.0
MIN_BARS = 110  # EMA100-Warmup (EMA200 brauchte 210 — zu lang für junge Perps)


def indicators(high, low, close) -> dict[str, np.ndarray]:
    """Alle für die Richtung nötigen Serien (TA-Lib, Wilder-Semantik)."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    slowk, slowd = talib.STOCH(h, l, c, fastk_period=14, slowk_period=3,
                               slowk_matype=0, slowd_period=3, slowd_matype=0)
    macd, signal, hist = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
    return {
        "k": slowk, "d": slowd,
        "macd": macd, "signal": signal, "hist": hist,
        "rsi": talib.RSI(c, timeperiod=14),
        "ema20": talib.EMA(c, timeperiod=20),
        "ema50": talib.EMA(c, timeperiod=50),
        "ema100": talib.EMA(c, timeperiod=100),
        "atr": talib.ATR(h, l, c, timeperiod=14),
        "adx": talib.ADX(h, l, c, timeperiod=14),
        "plus_di": talib.PLUS_DI(h, l, c, timeperiod=14),
        "minus_di": talib.MINUS_DI(h, l, c, timeperiod=14),
    }


def direction_at(ind: dict, close: np.ndarray, i: int) -> dict:
    """Richtungs-Entscheid für die GESCHLOSSENE Kerze i."""
    k, d = ind["k"][i], ind["d"][i]
    d_prev = ind["d"][i - 1]
    top_ev = min(k, d) >= STOCH_TOP or (d_prev >= STOCH_TOP and d < d_prev)
    bottom_ev = max(k, d) <= STOCH_BOTTOM or (d_prev <= STOCH_BOTTOM and d > d_prev)

    votes: dict[str, int] = {}
    hist, hist_prev = ind["hist"][i], ind["hist"][i - 1]
    votes["macd"] = 1 if hist > hist_prev else -1 if hist < hist_prev else 0

    rsi, rsi_prev = ind["rsi"][i], ind["rsi"][i - 1]
    if rsi >= RSI_TOP:
        votes["rsi"] = -1
    elif rsi <= RSI_BOTTOM:
        votes["rsi"] = 1
    else:
        votes["rsi"] = 1 if rsi > rsi_prev else -1 if rsi < rsi_prev else 0

    c, ema20, ema50, ema100 = close[i], ind["ema20"][i], ind["ema50"][i], ind["ema100"][i]
    slope = (ema20 - ind["ema20"][i - 1]) / ind["ema20"][i - 1]
    if c > ema50 and slope > 0:
        votes["ema"] = 1
    elif c < ema50 and slope < 0:
        votes["ema"] = -1
    elif ema20 > ema50 > ema100:
        votes["ema"] = 1
    elif ema20 < ema50 < ema100:
        votes["ema"] = -1
    else:
        votes["ema"] = 0

    plus, minus = ind["plus_di"][i], ind["minus_di"][i]
    votes["di"] = 1 if plus > minus else -1 if plus < minus else 0

    long_v = sum(1 for v in votes.values() if v > 0)
    short_v = sum(1 for v in votes.values() if v < 0)
    stance, reason = "WAIT", "INSUFFICIENT_CONFIRMATION"
    if top_ev and short_v >= VOTE_THRESHOLD:
        stance, reason = "SHORT", "STOCH_TOP_WITH_CONFIRMATION"
    elif bottom_ev and long_v >= VOTE_THRESHOLD:
        stance, reason = "LONG", "STOCH_BOTTOM_WITH_CONFIRMATION"

    # Regime-Rahmen (reine Referenz): Long nur über, Short nur unter der EMA100.
    regime_ok = True
    if np.isfinite(ema100):
        if stance == "LONG" and c < ema100:
            regime_ok = False
        if stance == "SHORT" and c > ema100:
            regime_ok = False

    return {
        "stance": stance if regime_ok else "WAIT",
        "raw_stance": stance,
        "regime_ok": regime_ok,
        "reason": reason,
        "stoch_evidence": "TOP" if top_ev else "BOTTOM" if bottom_ev else "MID",
        "votes": votes,
        "vote_counts": {"long": long_v, "short": short_v},
        "k": float(k), "d": float(d), "rsi": float(rsi),
    }


def direction_row_at(ind: dict, close: np.ndarray, i: int) -> dict | None:
    """Richtung für Kerze i; None wenn Warmup/NaN."""
    if i < MIN_BARS or i >= len(close):
        return None
    need = ("k", "d", "hist", "rsi", "ema20", "ema50", "ema100", "plus_di", "minus_di")
    if any(not np.isfinite(ind[name][i]) or not np.isfinite(ind[name][i - 1])
           for name in need):
        return None
    return direction_at(ind, close, i)


# ── Engine B: persistente Trend-Richtung ─────────────────────────────────────
ADX_MIN = 20.0


def trend_stance_at(ind: dict, close: np.ndarray, i: int,
                    use_ema100: bool = True) -> str | None:
    """Persistenter Trend-Zustand (kein Moment-Trigger).

    LONG, wenn EMA-Struktur aufwärts, Kurs über EMA50, ADX >= 20 und +DI>-DI.
    `use_ema100=False` lässt die EMA100-Referenz weg (nur EMA20/EMA50 + ADX).
    Die Bedingung ist stabil und hält über viele Tage — genau das, was ein
    Swing-Exit braucht.
    """
    if i < MIN_BARS or i >= len(close):
        return None
    need = ("ema20", "ema50", "adx", "plus_di", "minus_di")
    if any(not np.isfinite(ind[name][i]) for name in need):
        return None
    c = close[i]
    ema20, ema50 = ind["ema20"][i], ind["ema50"][i]
    adx, plus, minus = ind["adx"][i], ind["plus_di"][i], ind["minus_di"][i]
    if use_ema100:
        ema100 = ind["ema100"][i]
        if not np.isfinite(ema100):
            return None
        up_long = ema50 > ema100
        down_short = ema50 < ema100
    else:
        up_long = down_short = True
    up = c > ema50 and ema20 > ema50 and up_long and adx >= ADX_MIN and plus > minus
    down = c < ema50 and ema20 < ema50 and down_short and adx >= ADX_MIN and minus > plus
    if up:
        return "LONG"
    if down:
        return "SHORT"
    return None


# ── Engine C: Trend + SuperTrend-Bestätigung (Kontrolle) ─────────────────────
def supertrend(high, low, close, period: int = 10, multiplier: float = 3.0):
    """Einfacher Wilder-SuperTrend (nur Richtung, für den Regime-Filter)."""
    h, l, c = map(lambda x: np.asarray(x, dtype=float), (high, low, close))
    atr = talib.ATR(h, l, c, timeperiod=period)
    n = len(c)
    upper = (h + l) / 2 + multiplier * atr
    lower = (h + l) / 2 - multiplier * atr
    direction = np.full(n, np.nan)
    f_up, f_lo = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(1, n):
        if not np.isfinite(atr[i]):
            continue
        f_up[i] = min(upper[i], f_up[i - 1]) if (np.isfinite(f_up[i - 1]) and c[i - 1] <= f_up[i - 1]) else upper[i]
        f_lo[i] = max(lower[i], f_lo[i - 1]) if (np.isfinite(f_lo[i - 1]) and c[i - 1] >= f_lo[i - 1]) else lower[i]
        prev = direction[i - 1]
        if not np.isfinite(prev):
            direction[i] = 1.0 if c[i] > f_up[i] else -1.0
        elif prev > 0:
            direction[i] = -1.0 if c[i] < f_lo[i] else 1.0
        else:
            direction[i] = 1.0 if c[i] > f_up[i] else -1.0
    return direction


def trend_supertrend_stance_at(ind: dict, close: np.ndarray, i: int,
                               st_dir: np.ndarray | None = None) -> str | None:
    """Trend-Zustand wie B, zusätzlich nur in SuperTrend-Richtung."""
    stance = trend_stance_at(ind, close, i)
    if stance is None or st_dir is None or not np.isfinite(st_dir[i]):
        return stance
    if stance == "LONG" and st_dir[i] < 0:
        return None
    if stance == "SHORT" and st_dir[i] > 0:
        return None
    return stance
