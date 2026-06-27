#!/usr/bin/env python3
"""
graviton/exit.py — 3 Exit-Wege
================================
A) PATTERN EXIT → 50% raus, Rest Trailing
B) STRUKTURELL → 100% raus (EMA overextended, S/R erreicht, RSI extrem)
C) SESSION ENDE → 100% raus

Exit-Signale werden kontinuierlich geprüft (via watcher.py).
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from config import CFG
from patterns import detect_exit_pattern
from sr_levels import SRCalculator


class ExitReason(Enum):
    PATTERN = "pattern"              # Candlestick-Reversal
    EMA_OVEREXTENDED = "ema_overextended"
    SR_REACHED = "sr_reached"
    RSI_EXTREME = "rsi_extreme"
    SESSION_END = "session_end"
    STOP_LOSS = "stop_loss"
    NONE = "none"


@dataclass
class ExitSignal:
    """Exit-Signal für einen Trade."""
    symbol: str
    side: str              # "long" / "short"
    reason: ExitReason
    close_pct: float       # 0.5 = 50%, 1.0 = 100%
    price: float
    ema20: float
    distance_pct: float
    rsi: float
    pattern_detected: bool
    message: str


# ═══════════════════════════════════════════════════════════════════
# Exit Engine
# ═══════════════════════════════════════════════════════════════════

class ExitEngine:
    """
    Prüft alle Exit-Bedingungen und gibt Exit-Signale zurück.
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._sr_calc = SRCalculator()

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
        return self._exchange

    def _fetch_1m(self, symbol: str, limit: int = 50) -> np.ndarray:
        ex = self._get_exchange()
        candles = ex.fetch_ohlcv(symbol, timeframe="1m", limit=limit)
        return np.array(candles, dtype=float)

    def _rsi(self, close: np.ndarray, period: int = 14) -> float:
        if len(close) < period + 1:
            return 50.0
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = np.mean(gain[-period:])
        avg_loss = np.mean(loss[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def check(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        current_step: int,
        trailing_active: bool = False,
        trailing_price: float = 0.0,
    ) -> ExitSignal:
        """
        Vollständiger Exit-Check.

        Returns ExitSignal — wenn reason != NONE → exit ausführen.

        Priorität:
          1. Pattern Exit (50%)
          2. Strukturell (100%)
          3. Session Ende (100%) — handled extern
        """
        cfg_exit = CFG.exit

        try:
            data = self._fetch_1m(symbol, limit=60)
        except Exception:
            return ExitSignal(
                symbol=symbol, side=side, reason=ExitReason.NONE,
                close_pct=0, price=0, ema20=0, distance_pct=0, rsi=0,
                pattern_detected=False, message="Daten-Fehler",
            )

        closes = data[:, 4]
        highs  = data[:, 2]
        lows   = data[:, 3]
        opens  = data[:, 1]

        current_price = float(closes[-1])
        ema20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else current_price
        distance_pct = abs(current_price - ema20) / ema20 * 100
        rsi_value = self._rsi(closes)

        # ─── A) Pattern Exit (50%) ─────────────────────────────

        pattern = detect_exit_pattern(opens, highs, lows, closes, side.upper())
        if pattern and cfg_exit["pattern_exit_50"]:
            pattern_name = self._which_pattern(opens, highs, lows, closes, side.upper())
            return ExitSignal(
                symbol=symbol, side=side,
                reason=ExitReason.PATTERN,
                close_pct=0.5,
                price=current_price, ema20=ema20,
                distance_pct=round(distance_pct, 2),
                rsi=round(rsi_value, 1),
                pattern_detected=True,
                message=f"Pattern: {pattern_name} → 50% schließen",
            )

        # ─── B) Strukturell (100%) ─────────────────────────────

        # B1: EMA overextended (> 2.5%)
        if distance_pct > cfg_exit["ema_overextended_pct"]:
            return ExitSignal(
                symbol=symbol, side=side,
                reason=ExitReason.EMA_OVEREXTENDED,
                close_pct=1.0,
                price=current_price, ema20=ema20,
                distance_pct=round(distance_pct, 2),
                rsi=round(rsi_value, 1),
                pattern_detected=False,
                message=f"EMA overextended: {distance_pct:.2f}% → 100% schließen",
            )

        # B2: RSI extreme (nur bei Step 4+)
        if current_step >= 4:
            rsi_extreme_long = cfg_exit["rsi_extreme_long"]
            rsi_extreme_short = cfg_exit["rsi_extreme_short"]

            if side == "long" and rsi_value > rsi_extreme_long:
                return ExitSignal(
                    symbol=symbol, side=side,
                    reason=ExitReason.RSI_EXTREME,
                    close_pct=1.0,
                    price=current_price, ema20=ema20,
                    distance_pct=round(distance_pct, 2),
                    rsi=round(rsi_value, 1),
                    pattern_detected=False,
                    message=f"RSI {rsi_value:.1f} > {rsi_extreme_long} bei Step {current_step}",
                )
            elif side == "short" and rsi_value < rsi_extreme_short:
                return ExitSignal(
                    symbol=symbol, side=side,
                    reason=ExitReason.RSI_EXTREME,
                    close_pct=1.0,
                    price=current_price, ema20=ema20,
                    distance_pct=round(distance_pct, 2),
                    rsi=round(rsi_value, 1),
                    pattern_detected=False,
                    message=f"RSI {rsi_value:.1f} < {rsi_extreme_short} bei Step {current_step}",
                )

        # B3: S/R Level erreicht
        try:
            sr = self._sr_calc.calculate(symbol)
            if side == "long":
                res = sr.nearest_resistance(current_price)
                if res and current_price >= res * 0.998:  # within 0.2% of resistance
                    return ExitSignal(
                        symbol=symbol, side=side,
                        reason=ExitReason.SR_REACHED,
                        close_pct=1.0,
                        price=current_price, ema20=ema20,
                        distance_pct=round(distance_pct, 2),
                        rsi=round(rsi_value, 1),
                        pattern_detected=False,
                        message=f"Resistance {res:.4f} erreicht",
                    )
            else:  # short
                sup = sr.nearest_support(current_price)
                if sup and current_price <= sup * 1.002:  # within 0.2% of support
                    return ExitSignal(
                        symbol=symbol, side=side,
                        reason=ExitReason.SR_REACHED,
                        close_pct=1.0,
                        price=current_price, ema20=ema20,
                        distance_pct=round(distance_pct, 2),
                        rsi=round(rsi_value, 1),
                        pattern_detected=False,
                        message=f"Support {sup:.4f} erreicht",
                    )
        except Exception:
            pass

        # ─── Stop-Loss Check ───────────────────────────────────

        if side == "long" and current_price <= stop_loss:
            return ExitSignal(
                symbol=symbol, side=side,
                reason=ExitReason.STOP_LOSS,
                close_pct=1.0, price=current_price, ema20=ema20,
                distance_pct=round(distance_pct, 2),
                rsi=round(rsi_value, 1),
                pattern_detected=False,
                message=f"Stop-Loss getriggert @ {stop_loss:.6f}",
            )
        elif side == "short" and current_price >= stop_loss:
            return ExitSignal(
                symbol=symbol, side=side,
                reason=ExitReason.STOP_LOSS,
                close_pct=1.0, price=current_price, ema20=ema20,
                distance_pct=round(distance_pct, 2),
                rsi=round(rsi_value, 1),
                pattern_detected=False,
                message=f"Stop-Loss getriggert @ {stop_loss:.6f}",
            )

        # ─── Trailing Stop Update ──────────────────────────────

        # (Handled by watcher.py)

        return ExitSignal(
            symbol=symbol, side=side, reason=ExitReason.NONE,
            close_pct=0, price=current_price, ema20=ema20,
            distance_pct=round(distance_pct, 2),
            rsi=round(rsi_value, 1),
            pattern_detected=False, message="Kein Exit-Signal",
        )

    def _which_pattern(
        self, opens, highs, lows, closes, side: str,
    ) -> str:
        """Identifiziert welches Pattern getriggert hat."""
        from patterns import detect_exit_pattern_talib, HAS_TALIB

        if not HAS_TALIB:
            return "Pattern (pure)"

        o = np.asarray(opens, dtype=float)
        h = np.asarray(highs, dtype=float)
        l = np.asarray(lows, dtype=float)
        c = np.asarray(closes, dtype=float)

        if side == "LONG":
            patterns = {
                "Shooting Star": talib.CDLSHOOTINGSTAR(o, h, l, c)[-1],
                "Bearish Engulfing": talib.CDLENGULFING(o, h, l, c)[-1],
                "Evening Star": talib.CDLEVENINGSTAR(o, h, l, c)[-1],
                "Dark Cloud Cover": talib.CDLDARKCLOUDCOVER(o, h, l, c)[-1],
            }
        else:
            patterns = {
                "Hammer": talib.CDLHAMMER(o, h, l, c)[-1],
                "Bullish Engulfing": talib.CDLENGULFING(o, h, l, c)[-1],
                "Morning Star": talib.CDLMORNINGSTAR(o, h, l, c)[-1],
                "Piercing": talib.CDLPIERCING(o, h, l, c)[-1],
            }

        import talib  # noqa: F811
        for name, val in patterns.items():
            if (side == "LONG" and val < 0) or (side == "SHORT" and val > 0):
                return name
        return "Unknown"