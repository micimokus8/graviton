#!/usr/bin/env python3
"""
graviton/patterns.py — Candlestick Pattern Detection
======================================================
Erkennt Exit-Signale via ta-lib (61 Patterns built-in).

Exit-Patterns:
  LONG  → Bearish Reversal: Shooting Star, Bearish Engulfing,
           Evening Star, Dark Cloud Cover
  SHORT → Bullish Reversal: Hammer, Bullish Engulfing,
           Morning Star, Piercing Line

Falls ta-lib nicht installiert ist → Fallback auf Pure-Python
Pattern Detection (simplifiziert).
"""

from __future__ import annotations
import numpy as np
from typing import Optional

# ─── ta-lib (optional) ─────────────────────────────────────────────

try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False


# ═══════════════════════════════════════════════════════════════════
# ta-lib Path
# ═══════════════════════════════════════════════════════════════════

def detect_exit_pattern_talib(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    bias: str,
) -> bool:
    """
    Erkennt Exit-Pattern via ta-lib.
    opens/highs/lows/closes: float numpy arrays
    bias: "LONG" oder "SHORT"
    Returns: True wenn Exit-Signal erkannt
    """
    o = np.asarray(opens, dtype=float)
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)

    if bias == "LONG":
        # Bearish Reversal → 50% raus
        shooting_star     = talib.CDLSHOOTINGSTAR(o, h, l, c)[-1]
        bearish_engulfing = talib.CDLENGULFING(o, h, l, c)[-1]
        evening_star      = talib.CDLEVENINGSTAR(o, h, l, c)[-1]
        dark_cloud        = talib.CDLDARKCLOUDCOVER(o, h, l, c)[-1]
        return any(x < 0 for x in [shooting_star, bearish_engulfing,
                                     evening_star, dark_cloud])

    elif bias == "SHORT":
        # Bullish Reversal → 50% raus
        hammer            = talib.CDLHAMMER(o, h, l, c)[-1]
        bullish_engulfing = talib.CDLENGULFING(o, h, l, c)[-1]
        morning_star      = talib.CDLMORNINGSTAR(o, h, l, c)[-1]
        piercing          = talib.CDLPIERCING(o, h, l, c)[-1]
        return any(x > 0 for x in [hammer, bullish_engulfing,
                                     morning_star, piercing])

    return False


# ═══════════════════════════════════════════════════════════════════
# Pure Python Fallback
# ═══════════════════════════════════════════════════════════════════

def detect_exit_pattern_pure(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    bias: str,
) -> bool:
    """
    Fallback Pattern Detection ohne ta-lib.
    Nutzt einfache Heuristiken für die letzten 3 Kerzen.
    """
    o = np.asarray(opens, dtype=float)
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)

    if len(o) < 3:
        return False

    last_o, last_h, last_l, last_c = o[-1], h[-1], l[-1], c[-1]
    body = abs(last_c - last_o)
    upper_wick = last_h - max(last_o, last_c)
    lower_wick = min(last_o, last_c) - last_l
    total_range = last_h - last_l

    if total_range == 0:
        return False

    if bias == "LONG":
        # Shooting Star: braucht Kontext — vorheriger Aufwärtsimpuls
        # Body muss oben liegen (Close nahe Low), mindestens 2 Kerzen Kontext
        is_shooting_star = (
            upper_wick > body * 2 and
            lower_wick < body * 0.5 and
            body > 0  # kein Doji
        )
        if is_shooting_star and len(c) >= 3:
            # Kontext-Check: vorherige Kerze sollte bullisch sein (Aufwärtsimpuls)
            if c[-2] > c[-3] and c[-2] > o[-2]:
                # Close nahe Low = Body liegt oben → Shooting Star bestätigt
                if last_c < (last_o + last_h) / 2:
                    return True
        # Bearish Engulfing: red body > previous green body
        if len(o) >= 2:
            prev_body = c[-2] - o[-2]
            if prev_body > 0 and last_c < last_o and abs(body) > abs(prev_body):
                return True
        # Dark Cloud Cover
        if len(o) >= 2:
            if c[-2] > o[-2] and last_o > c[-2] and last_c < o[-2]:
                return True

    elif bias == "SHORT":
        # Hammer: small body, long lower wick, small upper wick
        if lower_wick > body * 2 and upper_wick < body * 0.5:
            return True
        # Bullish Engulfing
        if len(o) >= 2:
            prev_body = o[-2] - c[-2]
            if prev_body > 0 and last_c > last_o and abs(body) > abs(prev_body):
                return True
        # Piercing Line
        if len(o) >= 2:
            if c[-2] < o[-2] and last_o < c[-2] and last_c > (o[-2] + c[-2]) / 2:
                return True

    return False


# ═══════════════════════════════════════════════════════════════════
# Unified Interface
# ═══════════════════════════════════════════════════════════════════

def detect_exit_pattern(
    opens,
    highs,
    lows,
    closes,
    bias: str,
) -> bool:
    """
    Erkennt Exit-Pattern. Nutzt ta-lib wenn verfügbar, sonst Pure Python.
    """
    if HAS_TALIB:
        return detect_exit_pattern_talib(opens, highs, lows, closes, bias)
    return detect_exit_pattern_pure(opens, highs, lows, closes, bias)