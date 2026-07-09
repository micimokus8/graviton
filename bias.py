#!/usr/bin/env python3
"""
graviton/bias.py — Graviton Bias v2
====================================
Session-Momenton basiert. Keine Compound-Checks.

Alt vs Neu:
  • 3 Kerzen → 6 Kerzen (1.5h statt 45min)
  • Compound-Checks raus (highs_rising, lows_falling, green_candles)
  • Bias nur aus Session Net Move (0.5% Schwelle)
  • Volumen: Block unter 0.5x, Info über 0.5x
  • RSI im Bias entfernt (bleibt nur im Entry-Check auf 1m)
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CFG


@dataclass
class BiasResult:
    """Ergebnis der Bias-Analyse für einen Coin."""
    symbol: str
    bias: str                # "LONG", "SHORT", "NOISE"
    session_open_price: float
    current_price: float
    session_chg_pct: float
    session_vol_ratio: float
    candles_analyzed: int
    green_candles: int
    red_candles: int
    reason: str = ""


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _rsi(close: np.ndarray, period: int = 14) -> float:
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


# ═══════════════════════════════════════════════════════════════════
# Bias Engine v2
# ═══════════════════════════════════════════════════════════════════

class BiasAnalyzer:
    """
    Session-Momenton Bias Engine.

    Regeln (mittlere Variante):
      1. session_chg >  +0.5% + vol > 0.5x → LONG
      2. session_chg <  −0.5% + vol > 0.5x → SHORT
      3. Sonst → NOISE
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
        return self._exchange

    def _fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 40) -> np.ndarray:
        """Fetch OHLCV als numpy array [timestamp, open, high, low, close, volume]."""
        ex = self._get_exchange()
        try:
            candles = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not candles:
                raise ValueError("Keine Kerzen erhalten")
            return np.array(candles, dtype=float)
        except Exception as e:
            raise RuntimeError(f"OHLCV fetch failed for {symbol}: {e}")

    def analyze(self, symbol: str, session_open_ts: int) -> BiasResult:
        """
        Hauptanalyse für einen Coin.

        Args:
            symbol: CCXT Symbol (z.B. "BTC/USD:USD")
            session_open_ts: Unix-Timestamp des Session-Open (UTC, ms)

        Returns:
            BiasResult mit bias = LONG/SHORT/NOISE
        """
        cfg = CFG.bias
        min_candles = 4  # fester Wert: 4 Kerzen = 1h Daten

        # Fetch 15m candles (genug für 6 Session-Kerzen + Baseline)
        data = self._fetch_ohlcv(symbol, timeframe="15m", limit=40)

        timestamps = data[:, 0].astype(int)
        opens   = data[:, 1]
        highs   = data[:, 2]
        lows    = data[:, 3]
        closes  = data[:, 4]
        volumes = data[:, 5]

        # ── Session-Kerzen identifizieren ───────────────────────
        session_candles_mask = timestamps >= (session_open_ts - 1000)
        session_indices = np.where(session_candles_mask)[0]

        if len(session_indices) < min_candles:
            first_open = float(opens[session_indices[0]]) if len(session_indices) > 0 else 0.0
            return BiasResult(
                symbol=symbol,
                bias="NOISE",
                session_open_price=first_open,
                current_price=first_open,
                session_chg_pct=0.0,
                session_vol_ratio=0.0,
                candles_analyzed=len(session_indices),
                green_candles=0, red_candles=0,
                reason=f"Zu wenig Kerzen ({len(session_indices)} < {min_candles})"
            )

        # ── Erste 6 Kerzen nach Open ────────────────────────────
        n = min(6, len(session_indices))
        idx = session_indices[:n]

        session_open_price = float(opens[idx[0]])
        session_closes = closes[idx]

        # Grün/Rot zählen (Info only — kein Bias-Filter mehr)
        green = sum(1 for i in range(n) if session_closes[i] > opens[idx[i]])
        red = n - green

        current_price = float(closes[idx[-1]])

        # ── Session Momentum ────────────────────────────────────
        session_chg_pct = (current_price - session_open_price) / session_open_price * 100

        # ── Volumen-Ratio ───────────────────────────────────────
        avg_session_vol = float(np.mean(volumes[idx]))
        avg_baseline_vol = max(float(np.mean(volumes[-20:])), 0.001)
        session_vol_ratio = avg_session_vol / avg_baseline_vol

        # ── Volumen-Block (unter 0.5x) ─────────────────────────
        if session_vol_ratio < 0.5:
            return BiasResult(
                symbol=symbol,
                bias="NOISE",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                reason=f"Session {session_chg_pct:+.1f}%, Vol {session_vol_ratio:.1f}x — zu dünn"
            )

        # ── Bias-Logik (reine Session-Entscheidung) ────────────
        vol_note = f"(Vol {session_vol_ratio:.1f}x, stark)" if session_vol_ratio > 1.5 else f"(Vol {session_vol_ratio:.1f}x)"

        if session_chg_pct > 0.5:
            return BiasResult(
                symbol=symbol, bias="LONG",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                reason=f"Session +{session_chg_pct:.1f}% {vol_note} | {green}/{n} grün"
            )

        elif session_chg_pct < -0.5:
            return BiasResult(
                symbol=symbol, bias="SHORT",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                reason=f"Session {session_chg_pct:.1f}% {vol_note} | {red}/{n} rot"
            )

        else:
            return BiasResult(
                symbol=symbol, bias="NOISE",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                reason=f"Session {session_chg_pct:+.1f}% — zu flach (< 0.5%)"
            )


# ═══════════════════════════════════════════════════════════════════
# Convenience
# ═══════════════════════════════════════════════════════════════════

def analyze_watchlist(
    symbols: List[str],
    session_open_ts: int,
) -> List[BiasResult]:
    """Analysiert Bias für eine Liste von Coins."""
    analyzer = BiasAnalyzer()
    results = []
    for sym in symbols:
        try:
            result = analyzer.analyze(sym, session_open_ts)
            results.append(result)
            print(f"  {sym}: {result.bias} — {result.reason}")
        except Exception as e:
            print(f"  {sym}: ERROR — {e}")
    return results