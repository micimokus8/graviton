#!/usr/bin/env python3
"""
graviton/entry.py — EMA20 Pullback Entry (5m Chart)
=====================================================
Wartet auf den Pullback zur EMA20 auf dem 5m-Chart.
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
    reasoning: str = ""          # Entry-Grund (Pullback / Breakout)


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

    def _fetch_1m(self, symbol: str, limit: int = 50, tf: str = "1m") -> np.ndarray:
        """Fetch OHLCV. Default 1m, alternativ 5m."""
        ex = self._get_exchange()
        candles = ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
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
        body = abs(close - open_)
        if body == 0:
            return False

        if bias == "LONG":
            # Grüne Kerze
            if close <= open_:
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
            if close >= open_:
                return False
            # High nah an EMA (EMA diente als Resistance)
            high_dist = (high - ema) / ema * 100
            if high_dist > 0.3:
                return False
            # Body dominant vs upper wick (Rejection nach unten)
            upper_wick = high - max(open_, close)
            return body > upper_wick * 0.5

        return False

    # ─── EMA-Positions-Check: Preis muss auf richtiger Seite der EMA20 sein ──

    @staticmethod
    def _is_correct_side(price: float, ema20: float, bias: str) -> tuple[bool, str]:
        """
        Prüft ob Preis auf der richtigen EMA20-Seite ist.
        LONG: Preis > EMA20 (mindestens 0.1%)
        SHORT: Preis < EMA20 (mindestens 0.1%)
        Returns (ok, reason)
        """
        dist = (price - ema20) / ema20 * 100 if ema20 > 0 else 0
        if bias == "LONG":
            if dist <= 0:
                return False, f"Preis {dist:+.2f}% unter EMA20 — kein LONG"
            return True, ""
        else:  # SHORT
            if dist >= 0:
                return False, f"Preis {dist:+.2f}% über EMA20 — kein SHORT"
            return True, ""

    # ─── RSI (Wilder's Smoothing) ──────────────────────────────

    @staticmethod
    def _rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        delta = np.diff(closes)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = float(np.mean(gain[:period]))
        avg_loss = float(np.mean(loss[:period]))
        for i in range(period, len(gain)):
            avg_gain = (avg_gain * (period - 1) + gain[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / max(avg_loss, 1e-10)
        return 100.0 - (100.0 / (1.0 + rs))

    # ─── SL-Prozent aus 1H ATR ────────────────────────────────

    def _calc_sl_pct(self, symbol: str, price: float) -> float:
        """Berechnet SL-Prozent: 0.75× 1H ATR, min 0.6%."""
        atr_1h_pct = 0.0
        try:
            ex = self._get_exchange()
            candles_1h = ex.fetch_ohlcv(symbol, timeframe="1h", limit=20)
            if len(candles_1h) >= 3:
                trs = []
                for i in range(1, len(candles_1h)):
                    h_i, l_i = candles_1h[i][2], candles_1h[i][3]
                    c_prev = candles_1h[i-1][4]
                    tr = max(h_i - l_i, abs(h_i - c_prev), abs(l_i - c_prev))
                    trs.append(tr)
                lookback = min(14, len(trs))
                atr_1h = float(np.mean(trs[-lookback:]))
                atr_1h_pct = (atr_1h / price * 100) if atr_1h > 0 and price > 0 else 0
        except Exception:
            pass
        return max(atr_1h_pct * 0.75, 0.6)

    # ─── Hilfsfunktion: dynamische EMA-Max-Distanz ──────────────

    def _dynamic_max_dist(self, symbol: str, bias: str) -> float:
        """
        EMA-Distanz basierend auf 24h-Change des Coins.
        Je stärker der Move, desto weiter die erlaubte Distanz.

        daily_move < 5%   → 0.50% (enger Pullback)
        daily_move 5-10%  → 1.00% (moderat)
        daily_move > 10%  → 1.50% (weiter — Momentum-Tag)
        """
        base_dist = CFG.entry["ema_distance_max"]
        try:
            ex = self._get_exchange()
            ticker = ex.fetch_ticker(symbol)
            change_24h = abs(float(ticker.get("percentage", 0) or 0))
            if change_24h >= 10:
                return max(base_dist, 1.00)
            elif change_24h >= 5:
                return max(base_dist, 0.60)
        except Exception:
            pass
        return base_dist

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
        max_dist = self._dynamic_max_dist(symbol, bias)
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

        # Fetch 5m Daten
        data = self._fetch_1m(symbol, limit=60, tf="5m")
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

        # EMA-Positions-Check: Preis muss auf richtiger Seite sein
        side_ok, side_reason = self._is_correct_side(current_price, current_ema, bias)
        if not side_ok:
            signal = EntrySignal(
                symbol=symbol, bias=bias, state=EntryState.NO_ENTRY,
                price=round(current_price, 6), ema20=round(current_ema, 6),
                distance_pct=round(distance_pct, 4),
                rejection=False, rejection_high=0, rejection_low=0,
                rejection_close=0, entry_price=0, stop_loss=0,
                step=current_step, reasoning=side_reason,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            return signal

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

        # ─── Nur Pullback-Mode: Preis an EMA20 + Rejection ─────────

        # Entfernt von EMA → warten
        if distance_pct > max_dist * 3:
            signal.state = EntryState.WAITING
            return signal

        # Annähernd an EMA → APPROACHING
        if distance_pct > max_dist:
            signal.state = EntryState.APPROACHING
            return signal

        # Preis an der EMA → AT_EMA
        signal.state = EntryState.AT_EMA

        # ── Fast Entry: 5m RSI neutral → sofort Entry ──
        # 1m RSI zu verrauscht, 1H RSI blockt Momentum-Trades (DEXE +5% ignoriert)
        rsi_ok = False
        try:
            data_5m = self._fetch_1m(symbol, limit=50, tf="5m")
            closes_5m = data_5m[:, 4]
            rsi_5m = self._rsi(closes_5m)
            if bias == "LONG":
                rsi_ok = 30 <= rsi_5m <= 65
            else:
                rsi_ok = 35 <= rsi_5m <= 70
        except Exception:
            pass
        if rsi_ok:
            signal.entry_price = float(closes[-1])
            sl_pct = self._calc_sl_pct(symbol, signal.entry_price)
            if bias == "LONG":
                signal.stop_loss = round(signal.entry_price * (1 - sl_pct / 100), 8)
            else:
                signal.stop_loss = round(signal.entry_price * (1 + sl_pct / 100), 8)
            signal.state = EntryState.ENTERED
            signal.reasoning = f"Fast Entry: an EMA20 (5m RSI {rsi_5m:.0f}, Dist {distance_pct:.2f}%)"
            return signal

        # Rejection-Prüfung auf 5m Kerze (statt 1m — 1m zu verrauscht)
        try:
            data_5m = self._fetch_1m(symbol, limit=30, tf="5m")  # 5m data
            o5, h5, l5, c5 = data_5m[:, 1], data_5m[:, 2], data_5m[:, 3], data_5m[:, 4]
            v5 = data_5m[:, 5]
            last_open5, last_high5 = float(o5[-1]), float(h5[-1])
            last_low5, last_close5 = float(l5[-1]), float(c5[-1])
            last_vol5 = float(v5[-1])

            is_rejection = self._is_rejection_candle(
                last_open5, last_high5, last_low5, last_close5,
                current_ema, bias,
            )

            if is_rejection:
                if len(v5) >= 12:
                    avg_vol = float(np.mean(v5[-12:-2]))
                    if last_vol5 >= avg_vol * 1.2:
                        signal.entry_price = last_close5
                        sl_pct = self._calc_sl_pct(symbol, signal.entry_price)
                        if bias == "LONG":
                            signal.stop_loss = round(signal.entry_price * (1 - sl_pct / 100), 8)
                        else:
                            signal.stop_loss = round(signal.entry_price * (1 + sl_pct / 100), 8)
                        signal.state = EntryState.ENTERED
                        signal.reasoning = f"Pullback: 5m Rejection an EMA ({distance_pct:.2f}%)"
                        return signal
        except Exception:
            pass
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