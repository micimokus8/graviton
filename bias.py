#!/usr/bin/env python3
"""
graviton/bias.py — 15m Directional Bias
========================================
Bestimmt LONG/SHORT/NOISE aus den ersten 2–3 geschlossenen
15m-Kerzen nach Session-Open.

Logik:
  LONG:  Preis über Session-Open, 2+ grüne Kerzen, steigende Highs
  SHORT: Preis unter Session-Open, 2+ rote Kerzen, fallende Lows
  NOISE: Gemischt → Coin ignorieren

Zusätzlich: RSI-Check (RSI > 80 LONG → skip, RSI < 20 SHORT → skip)
"""

from __future__ import annotations
import ccxt
import numpy as np
from typing import Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CFG


@dataclass
class BiasResult:
    """Ergebnis der Bias-Analyse für einen Coin."""
    symbol: str
    bias: str                # "LONG", "SHORT", "NOISE"
    session_open_price: float
    candles_analyzed: int
    green_candles: int
    red_candles: int
    highs_rising: bool
    lows_falling: bool
    rsi_15m: float
    rsi_blocked: bool        # True wenn RSI zu extrem
    reason: str = ""


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _rsi(close: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI (EMA-Smoothing, nicht simple average)."""
    if len(close) < period + 1:
        return 50.0
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Erste avg = simple mean über ersten 'period' Bars
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    # Wilder's smoothing für den Rest
    for i in range(period, len(gain)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


# ═══════════════════════════════════════════════════════════════════
# Bias Analyzer
# ═══════════════════════════════════════════════════════════════════

class BiasAnalyzer:
    """
    Analysiert den 15m-Bias für einen Coin zum Session-Open.

    Schema:
      1. Hole 15m Kerzen (genug für RSI + Session-Open Kontext)
      2. Finde erste vollständige Kerze NACH session_open
      3. Prüfe 2–3 Kerzen auf Richtungskonsistenz
      4. RSI-Check
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

    def _fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 30) -> np.ndarray:
        """
        Fetch OHLCV als numpy array [timestamp, open, high, low, close, volume].
        Returns structured arrays o, h, l, c.
        """
        ex = self._get_exchange()
        try:
            candles = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not candles:
                raise ValueError("Keine Kerzen erhalten")
            data = np.array(candles, dtype=float)
            return data
        except Exception as e:
            raise RuntimeError(f"OHLCV fetch failed for {symbol}: {e}")

    def analyze(self, symbol: str, session_open_ts: int) -> BiasResult:
        """
        Hauptanalyse für einen Coin.

        Args:
            symbol: CCXT Symbol (z.B. "BTC/USD:USD")
            session_open_ts: Unix-Timestamp des Session-Open (UTC)

        Returns:
            BiasResult mit bias = LONG/SHORT/NOISE
        """
        cfg = CFG.bias
        min_candles = cfg["min_candles"]
        rsi_long_max = cfg["rsi_long_max"]
        rsi_short_min = cfg["rsi_short_min"]

        # Fetch 15m candles
        data = self._fetch_ohlcv(symbol, timeframe="15m", limit=60)

        timestamps = data[:, 0].astype(int)
        opens   = data[:, 1]
        highs   = data[:, 2]
        lows    = data[:, 3]
        closes  = data[:, 4]

        # Finde Kerzen NACH session_open (mit 1s Toleranz gegen Float-Präzision)
        # Kraken OHLCV-Timestamps sind int Millisekunden, aber float64 → int cast
        # kann bei Präzisionsverlust 1ms daneben liegen
        session_candles_mask = timestamps >= (session_open_ts - 1000)
        session_indices = np.where(session_candles_mask)[0]

        if len(session_indices) < min_candles:
            return BiasResult(
                symbol=symbol,
                bias="NOISE",
                session_open_price=float(opens[session_indices[0]]) if len(session_indices) > 0 else 0,
                candles_analyzed=len(session_indices),
                green_candles=0, red_candles=0,
                highs_rising=False, lows_falling=False,
                rsi_15m=50.0, rsi_blocked=False,
                reason=f"Zu wenige Kerzen ({len(session_indices)} < {min_candles})"
            )

        # Nimm die ersten n Kerzen nach Open
        n = min(3, len(session_indices))
        idx = session_indices[:n]

        session_open_price = float(opens[idx[0]])
        session_opens  = opens[idx]
        session_closes = closes[idx]
        session_highs  = highs[idx]
        session_lows   = lows[idx]

        # Zähle grüne/rote Kerzen
        green = sum(1 for i in range(n) if session_closes[i] > session_opens[i])
        red   = sum(1 for i in range(n) if session_closes[i] < session_opens[i])

        # Highs rising / Lows falling
        highs_rising = all(session_highs[i] >= session_highs[i-1] for i in range(1, n))
        lows_falling = all(session_lows[i] <= session_lows[i-1] for i in range(1, n))

        # Preis vs Session-Open
        current_price = float(closes[idx[-1]])
        price_above_open = current_price > session_open_price
        price_below_open = current_price < session_open_price

        # RSI auf 15m (nutze alle available closes vor + inkl. Session)
        all_closes_for_rsi = closes[:idx[-1] + 1] if len(closes) >= 14 else closes
        rsi_value = _rsi(all_closes_for_rsi)

        # ── Daily Trend Context ──────────────────────────────────
        # Coin der +6% auf 24h aber -0.3% in Session = LONG-Pullback, kein SHORT
        daily_trend = "NEUTRAL"
        daily_chg_pct = 0.0
        try:
            daily_data = self._fetch_ohlcv(symbol, timeframe="1d", limit=5)
            if len(daily_data) >= 3:
                d_closes = daily_data[:, 4]
                d_close_now = float(d_closes[-1])
                d_close_1d = float(d_closes[-2])
                d_close_2d = float(d_closes[-3])
                daily_chg_pct = (d_close_now - d_close_1d) / d_close_1d * 100
                two_day_chg = (d_close_now - d_close_2d) / d_close_2d * 100
                if daily_chg_pct > 2.0 or two_day_chg > 4.0:
                    daily_trend = "STRONG_UP"
                elif daily_chg_pct < -2.0 or two_day_chg < -4.0:
                    daily_trend = "STRONG_DOWN"
        except Exception:
            pass

        # ─── Bias-Logik (Daily-Trend-aware) ──────────────────────
        bias = "NOISE"
        reason = ""
        rsi_blocked = False

        session_chg_pct = (current_price - session_open_price) / session_open_price * 100

        # Daily-Kontext: starker Trend + kleiner Session-Pullback = Continuation
        if daily_trend == "STRONG_UP":
            if rsi_value > rsi_long_max:
                bias = "NOISE"; reason = f"Daily UP aber RSI {rsi_value:.0f} > {rsi_long_max} (überkauft)"
            elif session_chg_pct < -2.0:
                bias = "NOISE"; reason = f"Daily UP aber Session {session_chg_pct:.1f}% (Trendbruch?)"
            else:
                bias = "LONG"; reason = f"Daily UP +{daily_chg_pct:.1f}%, Session {session_chg_pct:+.1f}% → LONG"

        elif daily_trend == "STRONG_DOWN":
            if rsi_value < rsi_short_min:
                bias = "NOISE"; reason = f"Daily DOWN aber RSI {rsi_value:.0f} < {rsi_short_min} (überverkauft)"
            elif session_chg_pct > 2.0:
                bias = "NOISE"; reason = f"Daily DOWN aber Session {session_chg_pct:.1f}% (Trendbruch?)"
            else:
                bias = "SHORT"; reason = f"Daily DOWN {daily_chg_pct:.1f}%, Session {session_chg_pct:+.1f}% → SHORT"

        else:  # NEUTRAL daily — nur starke Session-Signale
            if price_above_open and green >= min_candles and highs_rising:
                if rsi_value > rsi_long_max:
                    rsi_blocked = True; reason = f"LONG-Setup aber RSI {rsi_value:.1f} > {rsi_long_max}"
                else:
                    bias = "LONG"; reason = f"Session LONG: {green} grüne Kerzen, Highs↑, RSI {rsi_value:.1f}"
            elif price_below_open and red >= min_candles and lows_falling:
                if rsi_value < rsi_short_min:
                    rsi_blocked = True; reason = f"SHORT-Setup aber RSI {rsi_value:.1f} < {rsi_short_min}"
                else:
                    bias = "SHORT"; reason = f"Session SHORT: {red} rote Kerzen, Lows↓, RSI {rsi_value:.1f}"
            else:
                reason = f"NEUTRAL: {'über' if price_above_open else 'unter'} Open, {green}grün/{red}rot"

        return BiasResult(
            symbol=symbol,
            bias=bias,
            session_open_price=session_open_price,
            candles_analyzed=n,
            green_candles=green,
            red_candles=red,
            highs_rising=highs_rising,
            lows_falling=lows_falling,
            rsi_15m=round(rsi_value, 1),
            rsi_blocked=rsi_blocked,
            reason=reason,
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