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
    intraday_high: float  # Heute Session High (letzte 3 Tage)
    intraday_low: float   # Heute Session Low
    day3_high: float      # Vor-3-Tage High
    day3_low: float       # Vor-3-Tage Low

    def _all_resistances(self, price: float) -> List[float]:
        """Alle Resistance-Levels über Preis."""
        candidates = [
            self.weekly_high, self.daily_high,
            self.intraday_high, self.day3_high,
        ]
        return sorted([l for l in candidates if l > price])

    def _all_supports(self, price: float) -> List[float]:
        """Alle Support-Levels unter Preis."""
        candidates = [
            self.weekly_low, self.daily_low,
            self.intraday_low, self.day3_low,
        ]
        return sorted([l for l in candidates if l < price], reverse=True)

    def nearest_resistance(self, price: float) -> Optional[float]:
        """Nächste Resistance über dem Preis (mit Confluence-Info)."""
        above = self._all_resistances(price)
        return above[0] if above else None

    def nearest_support(self, price: float) -> Optional[float]:
        """Nächster Support unter dem Preis (mit Confluence-Info)."""
        below = self._all_supports(price)
        return below[0] if below else None

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

        # Weekly candles (limit=4: -4/-3 = vollständig, -2/-1 = aktuell/laufend)
        try:
            weekly = ex.fetch_ohlcv(symbol, timeframe="1w", limit=4)
            if weekly and len(weekly) >= 3:
                # Nur abgeschlossene Wochen: [-3] und [-2] (skip [-1] = aktuell)
                completed = weekly[-3:-1]
                weekly_high = max(float(c[2]) for c in completed)
                weekly_low  = min(float(c[3]) for c in completed)
            elif weekly and len(weekly) >= 2:
                weekly_high = float(weekly[0][2])
                weekly_low  = float(weekly[0][3])
            else:
                weekly_high = weekly_low = 0.0
        except Exception:
            weekly_high = weekly_low = 0.0

        # Daily candles (letzte 5 Tage für intraday + 3-Tage-Kontext)
        try:
            daily = ex.fetch_ohlcv(symbol, timeframe="1d", limit=6)
            if daily and len(daily) >= 5:
                prev_day = daily[-2]   # Vortag
                day3     = daily[-4]   # Vor-3-Tage
                daily_high = float(prev_day[2])
                daily_low  = float(prev_day[3])
                day3_high  = float(day3[2])
                day3_low   = float(day3[3])
                # Intraday: höchste High / tiefste Low der letzten 3 Tage
                recent = daily[-4:-1]  # letzte 3 abgeschlossene Tage
                intraday_high = max(float(c[2]) for c in recent)
                intraday_low  = min(float(c[3]) for c in recent)
            elif daily and len(daily) >= 2:
                prev_day = daily[-2]
                daily_high = float(prev_day[2])
                daily_low  = float(prev_day[3])
                day3_high = day3_low = intraday_high = intraday_low = 0.0
            else:
                daily_high = daily_low = day3_high = day3_low = intraday_high = intraday_low = 0.0
        except Exception:
            daily_high = daily_low = day3_high = day3_low = intraday_high = intraday_low = 0.0

        return SRLevels(
            symbol=symbol,
            weekly_high=round(weekly_high, 6),
            weekly_low=round(weekly_low, 6),
            daily_high=round(daily_high, 6),
            daily_low=round(daily_low, 6),
            intraday_high=round(intraday_high, 6),
            intraday_low=round(intraday_low, 6),
            day3_high=round(day3_high, 6),
            day3_low=round(day3_low, 6),
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