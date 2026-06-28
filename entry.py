#!/usr/bin/env python3
"""
graviton/entry.py — EMA20 Pullback Entry (1m Chart)
=====================================================
Wartet auf den Pullback zur EMA20 auf dem 1m-Chart.
Entry-Logik:

  LONG:
    - 15m Bias = LONG
    - Preis fällt → Pullback RUNTER zur EMA20
    - Preis < 0.3% von EMA20 entfernt
    - Rejection: grüne 1m-Kerze an der EMA20
    → ENTRY LONG

  SHORT:
    - 15m Bias = SHORT
    - Preis steigt → Pullback HOCH zur EMA20
    - Preis < 0.3% von EMA20 entfernt
    - Rejection: rote 1m-Kerze an der EMA20
    → ENTRY SHORT

  SL: 0.2% über/unter Rejection-Kerze
  Max: 4 Treppenstufen pro Coin (Pullback-Add, kein Averaging-Down)
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from config import CFG


class EntryState(Enum):
    WAITING = "waiting"           # Warte auf Pullback
    APPROACHING = "approaching"   # Preis nähert sich EMA20
    AT_EMA = "at_ema"             # Preis an der EMA20
    REJECTION = "rejection"       # Rejection-Kerze erkannt
    ENTERED = "entered"           # Trade aktiv
    NO_ENTRY = "no_entry"         # Kein valider Entry


@dataclass
class EntrySignal:
    """Entry-Signal mit allen Details."""
    symbol: str
    bias: str                    # LONG / SHORT
    state: EntryState
    price: float
    ema20: float
    distance_pct: float          # % Abstand zur EMA
    rejection: bool
    rejection_high: float
    rejection_low: float
    rejection_close: float
    entry_price: float           # Market-Preis bei Entry
    stop_loss: float
    step: int                    # Treppenstufe (1-4)
    timestamp: str


# ═══════════════════════════════════════════════════════════════════
# Entry Engine
# ═══════════════════════════════════════════════════════════════════

class EntryEngine:
    """
    Überwacht 1m-Chart und triggert Entry bei EMA20 Pullback + Rejection.
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._active_positions: Dict[str, int] = {}  # symbol -> current step

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
        return self._exchange

    def _fetch_1m(self, symbol: str, limit: int = 50) -> np.ndarray:
        """Fetch 1m OHLCV."""
        ex = self._get_exchange()
        candles = ex.fetch_ohlcv(symbol, timeframe="1m", limit=limit)
        return np.array(candles, dtype=float)

    def _calc_ema(self, closes: np.ndarray, period: int = 20) -> np.ndarray:
        """Berechnet EMA als numpy array (glatt)."""
        alpha = 2.0 / (period + 1)
        ema = np.zeros_like(closes)
        ema[0] = closes[0]
        for i in range(1, len(closes)):
            ema[i] = alpha * closes[i] + (1 - alpha) * ema[i - 1]
        return ema

    def _smooth_ema(self, ema: np.ndarray, smoothing: int = 9) -> np.ndarray:
        """SMA-Glättung der EMA für weniger Noise."""
        if len(ema) < smoothing:
            return ema
        smoothed = np.zeros_like(ema)
        for i in range(len(ema)):
            start = max(0, i - smoothing + 1)
            smoothed[i] = np.mean(ema[start:i + 1])
        return smoothed

    def _is_rejection_candle(
        self,
        open_: float, high: float, low: float, close: float,
        ema: float, bias: str,
    ) -> bool:
        """
        Prüft ob die letzte Kerze eine Rejection ist.

        LONG Rejection:
          - Grüne Kerze (close > open)
          - Low nahe EMA (weniger als 0.15% drunter)
          - Body > obere Wick

        SHORT Rejection:
          - Rote Kerze (close < open)
          - High nahe EMA (weniger als 0.15% drüber)
          - Body > untere Wick
        """
        body = abs(close - open)
        if body == 0:
            return False

        if bias == "LONG":
            # Grüne Kerze
            if close <= open:
                return False
            # Low sollte nah an EMA sein (EMA diente als Support)
            low_dist = (ema - low) / ema * 100
            if low_dist > 0.3:
                return False
            # Body sollte signifikant sein vs obere Wick
            upper_wick = high - close
            return body > upper_wick * 0.5

        elif bias == "SHORT":
            # Rote Kerze
            if close >= open:
                return False
            # High nah an EMA (EMA diente als Resistance)
            high_dist = (high - ema) / ema * 100
            if high_dist > 0.3:
                return False
            # Body dominant vs upper wick (Rejection nach unten)
            upper_wick = high - max(open, close)
            return body > upper_wick * 0.5

        return False

    # ─── Main Entry Check ──────────────────────────────────────

    def check_entry(
        self,
        symbol: str,
        bias: str,
        current_step: int = 1,
    ) -> EntrySignal:
        """
        Prüft ob ein Entry-Signal vorliegt.

        Args:
            symbol: CCXT Symbol
            bias: "LONG" oder "SHORT" (vom BiasAnalyzer)
            current_step: aktuelle Treppenstufe (1-4)

        Returns:
            EntrySignal mit state und details
        """
        cfg = CFG.entry
        ema_period = cfg["ema_period"]
        smoothing = cfg["ema_smoothing"]
        max_dist = cfg["ema_distance_max"]
        sl_offset = cfg["sl_offset_pct"]
        max_steps = cfg["max_stair_steps"]

        if current_step > max_steps:
            return EntrySignal(
                symbol=symbol, bias=bias, state=EntryState.NO_ENTRY,
                price=0, ema20=0, distance_pct=999,
                rejection=False, rejection_high=0, rejection_low=0,
                rejection_close=0, entry_price=0, stop_loss=0,
                step=current_step, timestamp="",
            )

        # Fetch 1m Daten
        data = self._fetch_1m(symbol, limit=60)
        closes  = data[:, 4]
        highs   = data[:, 2]
        lows    = data[:, 3]
        opens   = data[:, 1]
        volumes = data[:, 5]  # für Volumen-Confirmation

        # EMA20 (KEIN Smoothing auf 1m — doppelte Glättung erzeugt zu viel Lag)
        ema_raw = self._calc_ema(closes, ema_period)
        current_ema = float(ema_raw[-1])
        current_price = float(closes[-1])

        # Distanz zur EMA
        distance_pct = abs(current_price - current_ema) / current_ema * 100

        # ─── State Machine ─────────────────────────────────────

        signal = EntrySignal(
            symbol=symbol,
            bias=bias,
            state=EntryState.WAITING,
            price=round(current_price, 6),
            ema20=round(current_ema, 6),
            distance_pct=round(distance_pct, 4),
            rejection=False,
            rejection_high=0, rejection_low=0, rejection_close=0,
            entry_price=0, stop_loss=0,
            step=current_step,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Entfernt von EMA → warten
        if distance_pct > max_dist * 3:
            signal.state = EntryState.WAITING
            return signal

        # Annähernd an EMA → APPROACHING
        if distance_pct > max_dist:
            signal.state = EntryState.APPROACHING
            return signal

        # Preis an der EMA → AT_EMA + Rejection-Prüfung
        signal.state = EntryState.AT_EMA

        # Rejection-Prüfung auf letzter Kerze
        last_open  = float(opens[-1])
        last_high  = float(highs[-1])
        last_low   = float(lows[-1])
        last_close = float(closes[-1])

        is_rejection = self._is_rejection_candle(
            last_open, last_high, last_low, last_close,
            current_ema, bias,
        )

        if is_rejection:
            # ── Volumen-Confirmation: Rejection ohne Volumen = False Signal ──
            if len(volumes) < 12:
                # Zu wenig Daten (Session-Start) → warten statt blind entry
                signal.state = EntryState.AT_EMA
                return signal
            avg_vol = float(np.mean(volumes[-12:-2]))  # letzte 10 Kerzen vor Rejection
            rejection_vol = float(volumes[-2])  # letzte GESCHLOSSENE Kerze
            if rejection_vol < avg_vol * 1.2:  # mind. 20% über Durchschnitt
                # Rejection ohne Volumen → zurück auf AT_EMA, warten
                signal.state = EntryState.AT_EMA
                return signal

            signal.state = EntryState.REJECTION
            signal.rejection = True
            signal.rejection_high = last_high
            signal.rejection_low = last_low
            signal.rejection_close = last_close

            # Entry-Preis = Market (aktueller Close)
            signal.entry_price = last_close

            # Stop Loss
            if bias == "LONG":
                # SL = Low der Rejection-Kerze - offset%
                signal.stop_loss = round(last_low * (1 - sl_offset / 100), 6)
            else:  # SHORT
                # SL = High der Rejection-Kerze + offset%
                signal.stop_loss = round(last_high * (1 + sl_offset / 100), 6)

            signal.state = EntryState.ENTERED

        return signal

    # ─── Active Position Tracking ──────────────────────────────

    def get_step(self, symbol: str) -> int:
        """Aktuelle Treppenstufe für Coin."""
        return self._active_positions.get(symbol, 0)

    def increment_step(self, symbol: str):
        """Treppenstufe erhöhen."""
        current = self._active_positions.get(symbol, 0)
        self._active_positions[symbol] = current + 1

    def reset_coin(self, symbol: str):
        """Coin-Tracking zurücksetzen (Session-Ende)."""
        self._active_positions.pop(symbol, None)

    def reset_all(self):
        """Alle Coins zurücksetzen."""
        self._active_positions.clear()