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

    # ─── Breakout Detection ─────────────────────────────────────

    def _check_breakout(
        self,
        highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        volumes: np.ndarray, bias: str, fast_mode: bool,
    ) -> Optional[EntrySignal]:
        """
        Prüft ob ein Breakout vorliegt: Preis bricht 15-Min-Hoch/-Tief mit Volumen.

        LONG Breakout: letzte Kerze schließt über dem höchsten High der letzten N Kerzen
        SHORT Breakout: letzte Kerze schließt unter dem tiefsten Low der letzten N Kerzen

        Volumen muss > 1.3x (normal) / 1.0x (fast mode) Durchschnitt sein.
        """
        if len(closes) < 20:
            return None

        cfg = CFG.entry
        lookback = cfg["breakout_lookback"]
        vol_mult = 1.0 if fast_mode else cfg["breakout_vol_mult"]

        last_close = float(closes[-1])
        last_high = float(highs[-1])
        last_low = float(lows[-1])
        last_vol = float(volumes[-1])
        last_open = float(closes[-1])  # wird später überschrieben

        # Durchschnittsvolumen der letzten 10 Kerzen (vor der aktuellen)
        avg_vol = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[-5:]))

        if bias == "LONG":
            # Höchstes High der letzten N Kerzen
            recent_highs = highs[-(lookback + 1):-1]  # letzte N geschlossene, exkl. aktuelle
            breakout_level = float(np.max(recent_highs)) if len(recent_highs) > 0 else last_high
            if last_close > breakout_level and last_vol >= avg_vol * vol_mult:
                # Breakout bestätigt
                entry_price = last_close
                stop_loss = round(float(np.min(lows[-5:])), 6)  # SL = 5-Min-Tief

                signal = self._build_entry(None, bias, entry_price, stop_loss, last_high, volumes, entry_price)
                signal.state = EntryState.ENTERED
                signal.reasoning = f"Breakout: über 15Min-Hoch {breakout_level:.4f} (Vol {last_vol/avg_vol:.1f}x)"
                return signal

        elif bias == "SHORT":
            # Tiefstes Low der letzten N Kerzen
            recent_lows = lows[-(lookback + 1):-1]
            breakout_level = float(np.min(recent_lows)) if len(recent_lows) > 0 else last_low
            if last_close < breakout_level and last_vol >= avg_vol * vol_mult:
                entry_price = last_close
                stop_loss = round(float(np.max(highs[-5:])), 6)  # SL = 5-Min-Hoch

                signal = self._build_entry(None, bias, entry_price, stop_loss, last_high, volumes, entry_price)
                signal.state = EntryState.ENTERED
                signal.reasoning = f"Breakout: unter 15Min-Tief {breakout_level:.4f} (Vol {last_vol/avg_vol:.1f}x)"
                return signal

        return None

    # ─── Shared Entry Builder (SL/TP) ───────────────────────────

    def _build_entry(
        self, signal, bias: str, entry_price: float,
        last_low: float, last_high: float, volumes: np.ndarray,
        last_close: float,
    ) -> EntrySignal:
        """Baut EntrySignal mit 1H-ATR-basiertem SL und Reasoning."""
        if signal is None:
            signal = EntrySignal(
                symbol="", bias=bias, state=EntryState.ENTERED,
                price=entry_price, ema20=entry_price, distance_pct=0,
                rejection=False, rejection_high=0, rejection_low=0,
                rejection_close=0, entry_price=entry_price, stop_loss=0,
                step=1, timestamp=datetime.now(timezone.utc).isoformat(),
            )

        signal.entry_price = entry_price

        # 1H ATR für SL
        atr_1h_pct = 0.0
        try:
            ex = self._get_exchange()
            candles_1h = ex.fetch_ohlcv(signal.symbol if signal.symbol else "BTC/USD:USD", timeframe="1h", limit=20)
            if len(candles_1h) >= 3:
                trs = []
                for i in range(1, len(candles_1h)):
                    h_i, l_i = candles_1h[i][2], candles_1h[i][3]
                    c_prev = candles_1h[i-1][4]
                    tr = max(h_i - l_i, abs(h_i - c_prev), abs(l_i - c_prev))
                    trs.append(tr)
                lookback = min(14, len(trs))
                atr_1h = float(np.mean(trs[-lookback:]))
                atr_1h_pct = (atr_1h / entry_price * 100) if atr_1h > 0 and entry_price > 0 else 0
        except Exception:
            pass
        sl_pct = max(atr_1h_pct * 0.3, 0.3)

        if bias == "LONG":
            signal.stop_loss = round(entry_price * (1 - sl_pct / 100), 6)
        else:
            signal.stop_loss = round(entry_price * (1 + sl_pct / 100), 6)

        return signal

    # ─── Main Entry Check ──────────────────────────────────────

    def check_entry(
        self,
        symbol: str,
        bias: str,
        current_step: int = 1,
        fast_mode: bool = False,
    ) -> EntrySignal:
        """
        Prüft ob ein Entry-Signal vorliegt.

        Args:
            symbol: CCXT Symbol
            bias: "LONG" oder "SHORT" (vom BiasAnalyzer)
            current_step: aktuelle Treppenstufe (1-4)
            fast_mode: wenn True, weitere Entry-Distanz (0.8% statt 0.5%)

        Returns:
            EntrySignal mit state und details
        """
        cfg = CFG.entry
        ema_period = cfg["ema_period"]
        smoothing = cfg["ema_smoothing"]
        max_dist = cfg["fast_entry_distance"] if fast_mode else cfg["ema_distance_max"]
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

        # ─── 1. Pullback-Mode: Preis an EMA20 + Rejection ────────────

        # Entfernt von EMA → warten
        if distance_pct > max_dist * 3:
            signal.state = EntryState.WAITING
            # Fall durch zu Breakout-Check — auch wenn weit von EMA, Breakout möglich
        else:
            # Annähernd an EMA → APPROACHING
            if distance_pct > max_dist:
                signal.state = EntryState.APPROACHING
            else:
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
                # ── Volumen-Confirmation ──
                vol_threshold = 1.0 if fast_mode else 1.2
                if len(volumes) >= 12:
                    avg_vol = float(np.mean(volumes[-12:-2]))
                    rejection_vol = float(volumes[-2])
                    if rejection_vol >= avg_vol * vol_threshold:
                        signal = self._build_entry(signal, bias, last_close, last_low, last_high, volumes, last_close)
                        signal.state = EntryState.ENTERED
                        signal.reasoning = f"Pullback: Rejection an EMA ({distance_pct:.2f}%)"
                        return signal

        # ─── 2. Breakout-Mode: Preis bricht 15-Min-Hoch/-Tief mit Volumen ───
        breakout_signal = self._check_breakout(highs, lows, closes, volumes, bias, fast_mode)
        if breakout_signal is not None:
            return breakout_signal

        # Kein Entry — Status zurückmelden
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