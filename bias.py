#!/usr/bin/env python3
"""
graviton/bias.py — Graviton Bias v3 (Multi-Timeframe)
=====================================================
Bewertet pro Coin 4H / 1H / 15m:
  - EMA-Position (Preis vs EMA9 vs EMA20)
  - Letzte 3 Kerzen (rising/falling)
  → BULLISH / BEARISH / NEUTRAL

Signal nur wenn 2 von 3 Timeframes übereinstimmen.
Session-Change als Info, nicht als Entscheidung.

v2 → v3 Änderungen:
  • Entfernt: Session-Change als Entscheidungskriterium
  • Neu:     Multi-Timeframe-Check (2/3 Rule)
  • Neu:     4H OHLCV Fetch
  • Neu:     _ema() Methode für EMA9 + EMA20
  • Neu:     _tf_signal() für pro-Timeframe-Bewertung
  • Behalten: min_candles, Volumen-Info

Usage:
  analyzer = BiasAnalyzer()
  result = analyzer.analyze("BTC/USD:USD", session_open_ts)
  print(result.bias, result.reason)
"""

from typing import List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
import numpy as np
import ccxt


# ─── BiasResult ──────────────────────────────────────────────────────

@dataclass
class BiasResult:
    symbol: str
    bias: str  # LONG / SHORT / NOISE
    session_open_price: float = 0.0
    current_price: float = 0.0
    session_chg_pct: float = 0.0
    session_vol_ratio: float = 0.0
    candles_analyzed: int = 0
    green_candles: int = 0
    red_candles: int = 0
    signal_count: int = 0  # 3 = 3/3, 2 = 2/3, 0 = NOISE
    reason: str = ""


# ─── BiasAnalyzer ────────────────────────────────────────────────────

class BiasAnalyzer:

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._config_loaded = False
        self._load_config()

    def _load_config(self):
        """Lazy-load config (import late to avoid circular dep)."""
        if not self._config_loaded:
            from config import CFG
            self._cfg = CFG
            self._config_loaded = True

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
            })
        return self._exchange

    def _fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 40):
        """Fetch OHLCV with minimal error handling."""
        ex = self._get_exchange()
        try:
            raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not raw:
                return np.array([])
            return np.array(raw, dtype=np.float64)
        except Exception:
            return np.array([])

    @staticmethod
    def _ema(closes: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average."""
        if len(closes) < period:
            return np.full_like(closes, np.nan)
        alpha = 2.0 / (period + 1)
        ema = np.full_like(closes, np.nan)
        ema[period - 1] = float(np.mean(closes[:period]))
        for i in range(period, len(closes)):
            ema[i] = alpha * float(closes[i]) + (1 - alpha) * ema[i - 1]
        return ema

    def _tf_signal(self, symbol: str, timeframe: str) -> dict:
        """
        Bewertet einen Timeframe: BULLISH / BEARISH / NEUTRAL.
        Gibt dict mit signal + details für reason-String zurück.
        """
        data = self._fetch_ohlcv(symbol, timeframe, limit=60)
        if len(data) < 25:
            return {"signal": "NEUTRAL", "detail": "zu wenig Daten"}

        closes = data[:, 4]
        price = float(closes[-1])

        ema9_arr = self._ema(closes, 9)
        ema20_arr = self._ema(closes, 20)

        ema9 = ema9_arr[-1]
        ema20 = ema20_arr[-1]

        if np.isnan(ema9) or np.isnan(ema20):
            return {"signal": "NEUTRAL", "detail": "EMA nicht berechenbar"}

        # Letzte 3 Closes: Richtung
        last_3 = closes[-3:]
        rising = bool(all(last_3[i] < last_3[i + 1] for i in range(2)))
        falling = bool(all(last_3[i] > last_3[i + 1] for i in range(2)))

        # BULLISH: Preis über EMA9 oder EMA-Cross bullish + Momentum
        if price > ema9 and ema9 > ema20:
            signal = "BULLISH"
            detail = f"P>{ema9:.4g}>{ema20:.4g}"
        elif ema9 > ema20 and rising:
            signal = "BULLISH"
            detail = f"EMA bullish, M{(ema20):.4g}"
        # BEARISH: Preis unter EMA9 oder EMA-Cross bearish + Momentum
        elif price < ema9 and ema9 < ema20:
            signal = "BEARISH"
            detail = f"P<{ema9:.4g}<{ema20:.4g}"
        elif ema9 < ema20 and falling:
            signal = "BEARISH"
            detail = f"EMA bearish, M{(ema20):.4g}"
        else:
            signal = "NEUTRAL"
            detail = "weder bullisch noch bärisch"

        # ── 4H Trend-Frische-Check ──────────────────────────────
        # Erschöpfte Trends (≥16h alt, Umkehrsignale) zählen nicht
        if timeframe == "4h" and signal in ("BULLISH", "BEARISH"):
            exhausted, ex_reason = self._check_4h_exhaustion(data)
            if exhausted:
                return {"signal": "NEUTRAL", "detail": f"{signal} → STALE ({ex_reason})"}

        return {"signal": signal, "detail": detail}

    def _check_4h_exhaustion(self, data: np.ndarray) -> tuple[bool, str]:
        """
        Prüft ob der 4H Trend erschöpft ist (alt + Umkehrsignale).
        Returns (exhausted, reason).
        """
        if len(data) < 4:
            return False, "zu wenig Daten"

        closes = data[:, 4]
        highs = data[:, 2]
        lows = data[:, 3]

        ema9 = self._ema(closes, 9)
        ema20 = self._ema(closes, 20)

        if np.isnan(ema9[-1]) or np.isnan(ema20[-1]):
            return False, "EMA NaN"

        # Count consecutive aligned candles (rückwärts)
        aligned = 0
        direction = None
        for i in range(len(data) - 1, -1, -1):
            if np.isnan(ema9[i]) or np.isnan(ema20[i]):
                break
            p, e9, e20 = closes[i], ema9[i], ema20[i]
            if p > e9 > e20:
                d = "LONG"
            elif p < e9 < e20:
                d = "SHORT"
            else:
                break
            if direction is None:
                direction = d
            if d == direction:
                aligned += 1
            else:
                break

        # Trend jünger als 5 Kerzen (20h) → frisch genug
        if aligned < 5:
            return False, f"Trend {aligned} candles, frisch"

        if direction == "SHORT":
            # Prüfe 1: Schluss in oberer Hälfte der letzten 4h Kerze
            last = data[-1]
            rng = last[2] - last[3]
            if rng > 0:
                upper_half = last[3] + rng * 0.5
                if last[4] > upper_half:
                    return True, f"SHORT {aligned}c·Schluss obere Hälfte"
            # Prüfe 2: Letzte 2 4h Tiefs steigen (höhere Tiefs)
            if len(lows) >= 3 and lows[-1] > lows[-2]:
                return True, f"SHORT {aligned}c·höhere Tiefs"

        if direction == "LONG":
            last = data[-1]
            rng = last[2] - last[3]
            if rng > 0:
                lower_half = last[3] + rng * 0.5
                if last[4] < lower_half:
                    return True, f"LONG {aligned}c·Schluss untere Hälfte"
            if len(highs) >= 3 and highs[-1] < highs[-2]:
                return True, f"LONG {aligned}c·niedrigere Hochs"

        return False, f"Trend {aligned}c, kein Erschöpfungsmuster"

    # ────────────────────────────────────────────────────────────────

    def analyze(self, symbol: str, session_open_ts: int) -> BiasResult:
        """
        Haupt-Methode: Multi-Timeframe Bias.

        1. Fetch 15m Session-Daten (für Info: Change, Volumen, Kerzen)
        2. Bewerte 4H / 1H / 15m Timeframes
        3. 2-von-3 Regel → LONG / SHORT / NOISE
        """
        self._load_config()

        # ── 15m Session-Daten (für Session-Change-Info) ──────────
        data = self._fetch_ohlcv(symbol, timeframe="15m", limit=40)
        if len(data) < 6:
            return BiasResult(
                symbol=symbol, bias="NOISE",
                reason=f"Zu wenig Kerzen ({len(data)} < 6)"
            )

        timestamps = data[:, 0].astype(int)
        opens = data[:, 1]
        closes = data[:, 4]
        volumes = data[:, 5]

        session_indices = np.where(timestamps >= session_open_ts)[0]
        n = len(session_indices)
        idx = session_indices

        session_open_price = float(opens[idx[0]]) if n > 0 else float(closes[-1])
        current_price = float(closes[-1])
        session_chg_pct = (current_price - session_open_price) / session_open_price * 100

        # Volumen-Info
        avg_session_vol = float(np.mean(volumes[idx])) if n > 0 else 0
        avg_baseline_vol = max(float(np.mean(volumes[-20:])), 0.001)
        session_vol_ratio = avg_session_vol / avg_baseline_vol

        green = sum(1 for i in range(n) if closes[idx[i]] > opens[idx[i]])
        red = n - green

        # ── Multi-Timeframe Bewertung ────────────────────────────
        tf_15m = self._tf_signal(symbol, "15m")
        tf_1h = self._tf_signal(symbol, "1h")
        tf_4h = self._tf_signal(symbol, "4h")

        signals_list = [tf_15m["signal"], tf_1h["signal"], tf_4h["signal"]]
        bullish = signals_list.count("BULLISH")
        bearish = signals_list.count("BEARISH")
        neutral = signals_list.count("NEUTRAL")

        # Signal-Details für Reason
        tf_detail = f"4H:{tf_4h['signal']} 1H:{tf_1h['signal']} 15m:{tf_15m['signal']}"

        # Volumen-Info
        vol_note = f"(Vol {session_vol_ratio:.1f}x)" if session_vol_ratio > 0 else ""

        signal_count = max(bullish, bearish)  # 3 = 3/3, 2 = 2/3

        # ── 2-von-3 Regel ────────────────────────────────────────
        if bullish >= 2:
            return BiasResult(
                symbol=symbol, bias="LONG",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=signal_count,
                reason=f"LONG ({bullish}/3) {tf_detail} | Session {session_chg_pct:+.1f}% {vol_note}"
            )

        if bearish >= 2:
            return BiasResult(
                symbol=symbol, bias="SHORT",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=signal_count,
                reason=f"SHORT ({bearish}/3) {tf_detail} | Session {session_chg_pct:+.1f}% {vol_note}"
            )

        # Kein Konsens: NOISE
        return BiasResult(
            symbol=symbol, bias="NOISE",
            session_open_price=session_open_price,
            current_price=current_price,
            session_chg_pct=session_chg_pct,
            session_vol_ratio=session_vol_ratio,
            candles_analyzed=n,
            green_candles=green, red_candles=red,
            signal_count=0,
            reason=f"NOISE ({bullish}B/{bearish}S/{neutral}N) {tf_detail} | Session {session_chg_pct:+.1f}% {vol_note}"
        )


# ─── analyze_watchlist (Interface für Cron) ─────────────────────────

def analyze_watchlist(
    symbols: List[str],
    session_open_ts: int,
) -> List[BiasResult]:
    """Analysiert Bias für eine Liste von Coins (ohne Print — Cron macht Output)."""
    analyzer = BiasAnalyzer()
    results = []
    for sym in symbols:
        try:
            result = analyzer.analyze(sym, session_open_ts)
            results.append(result)
        except Exception as e:
            print(f"  {sym}: ERROR — {e}")
    return results


# ─── CLI-Test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Python -m bias <symbol> [session_open_ts]
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "ZEC/USD:USD"
    ts = int(sys.argv[2]) if len(sys.argv) > 2 else int(datetime.now(timezone.utc).timestamp() * 1000) - 7200_000
    r = BiasAnalyzer().analyze(sym, ts)
    print(f"{r.bias} {r.symbol} | {r.reason}")