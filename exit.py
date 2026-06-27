#!/usr/bin/env python3
"""
graviton/exit.py — 3 Exit-Stufen
==================================
Stufe 1 — PATTERN (50%): Gegenbewegungskerze → 50% schließen + SL auf Break-Even
Stufe 2 — STRUKTURELL (100%): EMA overextended, S/R erreicht, RSI extrem, Stop-Loss
Stufe 3 — SESSION ENDE (100%): Zwangsschluss bei Session-Ende
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from config import CFG
from patterns import detect_exit_pattern
from sr_levels import SRCalculator


class ExitReason(Enum):
    PATTERN = "pattern"                  # 50% close + SL → breakeven
    EMA_OVEREXTENDED = "ema_overextended"  # 100% close
    SR_REACHED = "sr_reached"            # 100% close
    RSI_EXTREME = "rsi_extreme"          # 100% close
    STOP_LOSS = "stop_loss"              # 100% close
    SESSION_END = "session_end"          # 100% close
    NONE = "none"


@dataclass
class ExitSignal:
    symbol: str
    side: str
    reason: ExitReason
    close_pct: float
    price: float
    ema20: float
    distance_pct: float
    rsi: float
    pattern_detected: bool
    move_sl_to_breakeven: bool
    message: str


class ExitEngine:

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
        """Wilder's RSI (EMA-Smoothing)."""
        if len(close) < period + 1:
            return 50.0
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gain[:period])
        avg_loss = np.mean(loss[:period])
        for i in range(period, len(gain)):
            avg_gain = (avg_gain * (period - 1) + gain[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    def _no_signal(self, symbol: str, side: str) -> ExitSignal:
        return ExitSignal(symbol, side, ExitReason.NONE, 0, 0, 0, 0, 0, False, False, "Kein Signal")

    def _sig(self, symbol, side, reason, close_pct, price, ema20, dist, rsi,
             pattern=False, move_sl=False, msg="") -> ExitSignal:
        return ExitSignal(symbol, side, reason, close_pct, price, ema20, dist, rsi,
                         pattern, move_sl, msg)

    def check(self, symbol: str, side: str, entry_price: float,
              stop_loss: float, current_step: int,
              trailing_active: bool = False, trailing_price: float = 0.0) -> ExitSignal:
        cfg_exit = CFG.exit
        ns = self._no_signal(symbol, side)
        sig = self._sig

        try:
            data = self._fetch_1m(symbol, limit=60)
        except Exception:
            return sig(symbol, side, ExitReason.NONE, 0, 0, 0, 0, 0, msg="Daten-Fehler")

        closes = data[:, 4]
        highs  = data[:, 2]
        lows   = data[:, 3]
        opens  = data[:, 1]

        price = float(closes[-1])
        # EMA(20): gewichtet neuere Werte stärker
        if len(closes) >= 20:
            alpha = 2.0 / 21
            ema = closes[0]
            for v in closes[1:]:
                ema = alpha * v + (1 - alpha) * ema
            ema20 = float(ema)
        else:
            ema20 = price
        dist = abs(price - ema20) / ema20 * 100
        rsi_val = self._rsi(closes)

        # ─── Stufe 1: Pattern (50%) ────────────────────────────

        pattern = detect_exit_pattern(opens, highs, lows, closes, side.upper())
        if pattern and cfg_exit["pattern_exit_50"]:
            pname = self._which_pattern(opens, highs, lows, closes, side.upper())
            return sig(symbol, side, ExitReason.PATTERN, 0.5, price, ema20, round(dist, 2),
                      round(rsi_val, 1), True, True,
                      f"[PATTERN] {pname} → 50% schließen + SL auf Break-Even")

        # ─── Stufe 2: Strukturell (100%) ───────────────────────

        # B1: EMA overextended
        if dist > cfg_exit["ema_overextended_pct"]:
            return sig(symbol, side, ExitReason.EMA_OVEREXTENDED, 1.0, price, ema20,
                      round(dist, 2), round(rsi_val, 1),
                      msg=f"[STRUKTURELL] EMA {dist:.1f}% entfernt → 100%")

        # B2: RSI extrem (ab Step 4)
        if current_step >= 4:
            if side == "long" and rsi_val > cfg_exit["rsi_extreme_long"]:
                return sig(symbol, side, ExitReason.RSI_EXTREME, 1.0, price, ema20,
                          round(dist, 2), round(rsi_val, 1),
                          msg=f"[STRUKTURELL] RSI {rsi_val:.0f} extrem → 100%")
            elif side == "short" and rsi_val < cfg_exit["rsi_extreme_short"]:
                return sig(symbol, side, ExitReason.RSI_EXTREME, 1.0, price, ema20,
                          round(dist, 2), round(rsi_val, 1),
                          msg=f"[STRUKTURELL] RSI {rsi_val:.0f} extrem → 100%")

        # B3: S/R erreicht
        try:
            sr = self._sr_calc.calculate(symbol)
            if side == "long":
                res = sr.nearest_resistance(price)
                if res and price >= res * 0.998:
                    return sig(symbol, side, ExitReason.SR_REACHED, 1.0, price, ema20,
                              round(dist, 2), round(rsi_val, 1),
                              msg=f"[STRUKTURELL] Resistance {res:.4f} erreicht → 100%")
            else:
                sup = sr.nearest_support(price)
                if sup and price <= sup * 1.002:
                    return sig(symbol, side, ExitReason.SR_REACHED, 1.0, price, ema20,
                              round(dist, 2), round(rsi_val, 1),
                              msg=f"[STRUKTURELL] Support {sup:.4f} erreicht → 100%")
        except Exception:
            pass

        # B4: Stop-Loss
        if side == "long" and price <= stop_loss:
            return sig(symbol, side, ExitReason.STOP_LOSS, 1.0, price, ema20,
                      round(dist, 2), round(rsi_val, 1),
                      msg=f"[STOP-LOSS] {stop_loss:.4f} getriggert → 100%")
        elif side == "short" and price >= stop_loss:
            return sig(symbol, side, ExitReason.STOP_LOSS, 1.0, price, ema20,
                      round(dist, 2), round(rsi_val, 1),
                      msg=f"[STOP-LOSS] {stop_loss:.4f} getriggert → 100%")

        # Kein Exit — price muss aktuellen Preis enthalten!
        return sig(symbol, side, ExitReason.NONE, 0, price, ema20,
                  round(dist, 2), round(rsi_val, 1), msg="Kein Signal")

    def _which_pattern(self, opens, highs, lows, closes, side: str) -> str:
        from patterns import detect_exit_pattern_talib, HAS_TALIB
        if not HAS_TALIB:
            return "Candlestick"
        import talib  # noqa — muss VOR pairs kommen, Dict wird sofort evaluiert

        o = np.asarray(opens, dtype=float)
        h = np.asarray(highs, dtype=float)
        l = np.asarray(lows, dtype=float)
        c = np.asarray(closes, dtype=float)

        pairs = {
            "LONG": [
                ("Shooting Star", talib.CDLSHOOTINGSTAR(o, h, l, c)[-1] < 0),
                ("Bearish Engulfing", talib.CDLENGULFING(o, h, l, c)[-1] < 0),
                ("Evening Star", talib.CDLEVENINGSTAR(o, h, l, c)[-1] < 0),
                ("Dark Cloud", talib.CDLDARKCLOUDCOVER(o, h, l, c)[-1] < 0),
            ],
            "SHORT": [
                ("Hammer", talib.CDLHAMMER(o, h, l, c)[-1] > 0),
                ("Bullish Engulfing", talib.CDLENGULFING(o, h, l, c)[-1] > 0),
                ("Morning Star", talib.CDLMORNINGSTAR(o, h, l, c)[-1] > 0),
                ("Piercing", talib.CDLPIERCING(o, h, l, c)[-1] > 0),
            ],
        }
        for name, hit in pairs.get(side, []):
            if hit:
                return name
        return "Candlestick"