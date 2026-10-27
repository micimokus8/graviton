"""Regime-Bausteine: Wilder-ADX, ATR, Richtungsgeber (DI), Volatilitaets-Expansion.

Eine Quelle der Wahrheit fuer alle Pfade (bias, entry, Forensik). Vorher
existierte ADX im Bot ueberhaupt nicht, und Forensik-Skripte rechneten ihn
mit einfacher Mittelung — was ADX systematisch zu hoch ausgibt (Spikes
werden uebergewichtet).

Wichtig: ADX hat KEINE Richtung. ADX 37 heisst "starker Trend", nicht
"starker Aufwaertstrend". Deshalb liefert dieses Modul immer +DI/-DI mit,
und die Gates pruefen die Richtung gegen den Trade-Bias.
"""
from __future__ import annotations

import numpy as np


def wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder-Glaettung (RMA): Start = SMA(period), danach rekursiv."""
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _true_range(data: np.ndarray) -> np.ndarray:
    h, l, c = data[:, 2], data[:, 3], data[:, 4]
    return np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])),
    )


def atr(data: np.ndarray, period: int = 14, method: str = "wilder") -> float:
    """ATR in Preis-Einheiten. method='wilder' (Default) oder 'sma'."""
    data = np.asarray(data, dtype=float)
    if len(data) < period + 1:
        return float("nan")
    tr = _true_range(data)
    if method == "wilder":
        s = wilder(tr, period)
        return float(s[-1]) if np.isfinite(s[-1]) else float("nan")
    return float(np.mean(tr[-period:]))


def atr_pct(data: np.ndarray, period: int = 14) -> float:
    """ATR als Prozent des letzten Close."""
    a = atr(data, period)
    last = float(np.asarray(data)[-1, 4])
    return float(a / last * 100) if last and np.isfinite(a) else float("nan")


def directional(data: np.ndarray, period: int = 14) -> dict:
    """+DI, -DI, ADX mit Wilder-Glaettung.

    Returns dict: adx, plus_di, minus_di, atr. Alle Werte NaN, wenn zu
    wenig Kerzen (< 2*period+1) fuer eine belastbare ADX-Glaettung.
    """
    data = np.asarray(data, dtype=float)
    nan_result = {"adx": float("nan"), "plus_di": float("nan"),
                  "minus_di": float("nan"), "atr": float("nan")}
    if len(data) < period * 2 + 1:
        return nan_result

    h, l, c = data[:, 2], data[:, 3], data[:, 4]
    up = np.diff(h)
    dn = -np.diff(l)
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = _true_range(data)

    plus_s = wilder(plus_dm, period)
    minus_s = wilder(minus_dm, period)
    tr_s = wilder(tr, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(tr_s > 0, 100 * plus_s / tr_s, np.nan)
        minus_di = np.where(tr_s > 0, 100 * minus_s / tr_s, np.nan)
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100 * np.abs(plus_di - minus_di) / di_sum, np.nan)

    # ADX = Wilder-Glaettung der DX-Reihe, Start ab dem ersten gueltigen DX
    valid = np.isfinite(dx)
    if valid.sum() < period:
        return nan_result
    first = int(np.argmax(valid))
    adx_series = np.full(len(dx), np.nan)
    smoothed = wilder(dx[first:], period)
    adx_series[first:] = smoothed

    last = -1
    return {
        "adx": float(adx_series[last]) if np.isfinite(adx_series[last]) else float("nan"),
        "plus_di": float(plus_di[last]) if np.isfinite(plus_di[last]) else float("nan"),
        "minus_di": float(minus_di[last]) if np.isfinite(minus_di[last]) else float("nan"),
        "atr": float(tr_s[last]) if np.isfinite(tr_s[last]) else float("nan"),
    }


def volatility_expansion(data: np.ndarray, period: int = 14, lookback: int = 20) -> float:
    """ATR(period) / Median der ATR(period) der letzten `lookback` Bars.

    >1.1 = Volatilitaet expandiert, <0.9 = kontrahiert.
    """
    data = np.asarray(data, dtype=float)
    if len(data) < period + lookback + 1:
        return float("nan")
    values = []
    for end in range(len(data) - lookback, len(data) + 1):
        a = atr(data[:end], period)
        if np.isfinite(a):
            values.append(a)
    if not values:
        return float("nan")
    med = float(np.median(values))
    return float(values[-1] / med) if med > 0 else float("nan")


def classify_regime(direction: dict, bias: str,
                    trend_threshold: float = 25.0,
                    chop_threshold: float = 20.0) -> dict:
    """Uebersetzt ADX/DI in ein handelbares Regime-Urteil.

    Rueckgabe:
      regime: TREND | CHOP | NEUTRAL
      aligned: True, wenn die Trendrichtung zum Trade-Bias passt
      veto:    True, wenn der Trade gegen einen starken Trend laeuft
      reason:  Kurzbegruendung

    Regeln (bewusst konservativ):
      - ADX >= trend_threshold und DI-Richtung gegen Bias -> veto
      - ADX < chop_threshold -> CHOP (kein Trendfolge-Entry)
      - sonst NEUTRAL (weder Veto noch Freibrief)
    """
    adx = direction.get("adx", float("nan"))
    pdi = direction.get("plus_di", float("nan"))
    mdi = direction.get("minus_di", float("nan"))

    metrics = {"adx": adx, "plus_di": pdi, "minus_di": mdi}
    if not np.isfinite(adx):
        return {"regime": "UNKNOWN", "aligned": None, "veto": False,
                "reason": "ADX nicht berechenbar", **metrics}

    trend_up = pdi > mdi
    if bias == "LONG":
        aligned = trend_up
    else:
        aligned = not trend_up

    if adx >= trend_threshold:
        if not aligned:
            return {"regime": "TREND", "aligned": False, "veto": True,
                    "reason": f"ADX {adx:.1f} starker Trend, +DI {pdi:.1f} / -DI {mdi:.1f} gegen {bias}", **metrics}
        return {"regime": "TREND", "aligned": True, "veto": False,
                "reason": f"ADX {adx:.1f} Trend bestaetigt {bias} (+DI {pdi:.1f} / -DI {mdi:.1f})", **metrics}

    if adx < chop_threshold:
        return {"regime": "CHOP", "aligned": None, "veto": True,
                "reason": f"ADX {adx:.1f} < {chop_threshold:.0f} — trendlos, kein Trendfolge-Entry", **metrics}

    return {"regime": "NEUTRAL", "aligned": None, "veto": False,
            "reason": f"ADX {adx:.1f} im toten Band ({chop_threshold:.0f}-{trend_threshold:.0f})", **metrics}


__all__ = ["wilder", "atr", "atr_pct", "directional", "volatility_expansion",
           "classify_regime"]
