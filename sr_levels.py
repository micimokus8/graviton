#!/usr/bin/env python3
"""
graviton/sr_levels.py — Support/Resistance Levels
==================================================
Berechnet Weekly und Daily S/R-Level aus CCXT OHLCV.

Quellen:
  - Weekly High/Low (letzte 2 Wochen)
  - Daily High/Low (Vortag)

Regel:
  Ist nächster S/R-Level < 0.5% vom Entry entfernt → KEIN Entry.
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional, List, Dict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from config import CFG


@dataclass
class SRLevels:
    """S/R Levels für einen Coin."""
    symbol: str
    weekly_high: float   # letzte 2 Wochen
    weekly_low: float
    daily_high: float    # Vortag
    daily_low: float

    def nearest_resistance(self, price: float) -> Optional[float]:
        """Nächste Resistance über dem Preis."""
        levels = [self.weekly_high, self.daily_high]
        above = [l for l in levels if l > price]
        return min(above) if above else None

    def nearest_support(self, price: float) -> Optional[float]:
        """Nächster Support unter dem Preis."""
        levels = [self.weekly_low, self.daily_low]
        below = [l for l in levels if l < price]
        return max(below) if below else None

    def is_sr_too_close(self, price: float, bias: str) -> tuple[bool, str]:
        """
        Prüft ob nächster S/R zu nah (< 0.5%) für Entry.

        Returns (blocked, reason_string)
        """
        min_dist = CFG.sr["min_distance_pct"]

        if bias == "LONG":
            res = self.nearest_resistance(price)
            if res:
                dist_pct = (res - price) / price * 100
                if dist_pct < min_dist:
                    return True, f"Resistance {res:.4f} nur {dist_pct:.2f}% entfernt (< {min_dist}%)"
        elif bias == "SHORT":
            sup = self.nearest_support(price)
            if sup:
                dist_pct = (price - sup) / price * 100
                if dist_pct < min_dist:
                    return True, f"Support {sup:.4f} nur {dist_pct:.2f}% entfernt (< {min_dist}%)"

        return False, "S/R Abstand OK"


# ═══════════════════════════════════════════════════════════════════
# SR Calculator
# ═══════════════════════════════════════════════════════════════════

class SRCalculator:
    """Berechnet S/R-Level via CCXT OHLCV."""

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
        return self._exchange

    def calculate(self, symbol: str) -> SRLevels:
        """
        Berechnet S/R für einen Coin.

        Weekly: Letzte 2 volle Wochen (high/low)
        Daily:  Vortag (high/low)
        """
        ex = self._get_exchange()

        # Weekly candles (2 Wochen = 2 candles)
        try:
            weekly = ex.fetch_ohlcv(symbol, timeframe="1w", limit=3)
            if weekly and len(weekly) >= 2:
                weekly_high = max(float(c[2]) for c in weekly[-2:])
                weekly_low  = min(float(c[3]) for c in weekly[-2:])
            else:
                weekly_high = weekly_low = 0.0
        except Exception:
            weekly_high = weekly_low = 0.0

        # Daily candles (Vortag)
        try:
            daily = ex.fetch_ohlcv(symbol, timeframe="1d", limit=3)
            if daily and len(daily) >= 2:
                prev_day = daily[-2]
                daily_high = float(prev_day[2])
                daily_low  = float(prev_day[3])
            else:
                daily_high = daily_low = 0.0
        except Exception:
            daily_high = daily_low = 0.0

        return SRLevels(
            symbol=symbol,
            weekly_high=round(weekly_high, 6),
            weekly_low=round(weekly_low, 6),
            daily_high=round(daily_high, 6),
            daily_low=round(daily_low, 6),
        )


# ═══════════════════════════════════════════════════════════════════
# Convenience
# ═══════════════════════════════════════════════════════════════════

def check_sr_for_entry(
    symbol: str,
    price: float,
    bias: str,
) -> tuple[bool, str, SRLevels]:
    """
    Convenience: Prüft ob S/R den Entry blockiert.
    Returns (blocked, reason, sr_levels)
    """
    calc = SRCalculator()
    sr = calc.calculate(symbol)
    blocked, reason = sr.is_sr_too_close(price, bias)
    return blocked, reason, sr