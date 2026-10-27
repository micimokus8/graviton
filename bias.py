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
import time

from candle_utils import closed_ohlcv
from volume_metrics import _session_volume_ratio
from feature_snapshot import build_timeframe_features, relative_strength
from regime import directional, atr_pct as regime_atr_pct, volatility_expansion, classify_regime


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
    features: dict = field(default_factory=dict)
    regime: dict = field(default_factory=dict)


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
        """Fetch OHLCV; 1 Retry bei leerem Resultat (Rate-Limit-Blips nach Scan-Burst)."""
        ex = self._get_exchange()
        for attempt in range(2):
            try:
                raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                data = closed_ohlcv(raw, timeframe)
                if len(data) > 0 or attempt == 1:
                    return data
            except Exception:
                if attempt == 1:
                    return np.array([])
            time.sleep(2)  # kurz warten, dann erneut versuchen
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
            return {"signal": "ERROR", "detail": f"zu wenig geschlossene Daten ({len(data)} < 25)"}

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

        # ── Regime-Metriken (ADX/DI/ATR) für jeden Frame ────────
        # ADX hat keine Richtung: +DI/-DI werden immer mitgeliefert,
        # damit Aufwärts- und Abwärtstrend unterscheidbar bleiben.
        dir_stats = directional(data)
        regime_meta = {
            "adx": dir_stats["adx"],
            "plus_di": dir_stats["plus_di"],
            "minus_di": dir_stats["minus_di"],
            "atr_pct": regime_atr_pct(data),
            "expansion": volatility_expansion(data),
        }

        # ── 4H Trend-Frische-Check ──────────────────────────────
        # Erschöpfte Trends (≥16h alt, Umkehrsignale) zählen nicht
        if timeframe == "4h" and signal in ("BULLISH", "BEARISH"):
            exhausted, ex_reason = self._check_4h_exhaustion(data)
            if exhausted:
                return {"signal": "NEUTRAL", "detail": f"{signal} → STALE ({ex_reason})", **regime_meta}

        return {"signal": signal, "detail": detail, **regime_meta}

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

    def _build_feature_snapshot(self, result: BiasResult) -> BiasResult:
        """Attach analysis data without changing the deterministic bias."""
        error_features = build_timeframe_features(np.empty((0, 6)))
        snapshot = {
            "status": "OK",
            "timeframes": {tf: dict(error_features) for tf in ("15m", "30m", "1h")},
            "btc": {tf: dict(error_features) for tf in ("30m", "1h")},
            "relative_strength_30m_pct": None,
            "relative_strength_1h_pct": None,
        }
        try:
            coin_data = {}
            for timeframe in ("15m", "30m", "1h"):
                coin_data[timeframe] = self._fetch_ohlcv(result.symbol, timeframe, limit=60)
                snapshot["timeframes"][timeframe] = build_timeframe_features(coin_data[timeframe])
            btc_data = {}
            for timeframe in ("30m", "1h"):
                btc_data[timeframe] = self._fetch_ohlcv("BTC/USD:USD", timeframe, limit=60)
                snapshot["btc"][timeframe] = build_timeframe_features(btc_data[timeframe])
            for timeframe, key in (("30m", "relative_strength_30m_pct"), ("1h", "relative_strength_1h_pct")):
                coin, btc = coin_data[timeframe], btc_data[timeframe]
                if len(coin) and len(btc):
                    snapshot[key] = relative_strength(
                        float(coin[-1, 4]), float(btc[-1, 4]),
                        float(coin[0, 4]), float(btc[0, 4]))
        except Exception as exc:
            snapshot["status"] = "ERROR"
            snapshot["error"] = type(exc).__name__
        result.features = snapshot
        return result

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

        # Volumen-Info: getrennte Median-Berechnung aus volume_metrics
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        _, _, session_vol_ratio = _session_volume_ratio(
            data, session_open_ts, now_ms
        )
        # Bottom-Limit bei 0 (Rausch) und Top-Limit bei 5 (irrelevante übermäßige Aktivität).
        session_vol_ratio = max(0.0, min(5.0, session_vol_ratio))

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

        # Fehlende Timeframe-Daten sind kein neutrales Marktsignal. Ein 2/3-
        # Trade darf nicht dadurch entstehen, dass der dritte TF ausgefallen ist.
        tf_results = {"4H": tf_4h, "1H": tf_1h, "15m": tf_15m}
        tf_errors = [
            f"{name}: {result['detail']}"
            for name, result in tf_results.items()
            if result["signal"] == "ERROR"
        ]
        if tf_errors:
            return BiasResult(
                symbol=symbol, bias="NOISE",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=0,
                reason=f"NOISE (Datenfehler: {'; '.join(tf_errors)}) | {tf_detail}",
            )

        # Volumen-Info: nur anzeigen, wenn mind. eine geschlossene
        # Session-Candle ausgewertet wurde, sonst fehlt jede Auswertungsgrundlage.
        if n > 0:
            vol_note = f"(Vol {session_vol_ratio:.1f}x)"
        else:
            vol_note = ""

        # Session-Change nur anzeigen, wenn eine echte Session-Candle
        # zur Berechnung beigetragen hat. Bei n=0 wäre die Differenz 0,0%
        # und inhaltlich irreführend.
        chg_note = f"Session {session_chg_pct:+.1f}% " if n > 0 else ""

        signal_count = max(bullish, bearish)  # 3 = 3/3, 2 = 2/3

        # ── Regime-Gate (ADX 1H + Richtung) ──────────────────────
        # ADX ohne Richtung ist wertlos: classify_regime() prueft +DI/-DI
        # gegen den Trade-Bias. Veto bei starkem Gegentrend (ADX>=25) und
        # bei trendlosem Markt (ADX<20) — beides empirisch die schlechtesten
        # Kategorien (41 Trades: ADX<20 avg -1.27%/Trade).
        regime_cfg = self._cfg.regime if hasattr(self._cfg, "regime") else {}
        regime_enabled = bool(regime_cfg.get("enabled", True))
        regime_log_only = bool(regime_cfg.get("log_only", False))
        role_mode = bool(regime_cfg.get("role_mode", True))

        if role_mode:
            # ── Rollen-Trennung: 4H = Veto, 1H = Bias, 15m = Trigger ──
            # 1H muss gerichtet sein (NEUTRAL = kein Trade). 4H stimmt nicht
            # mehr mit ab, sondern kann nur blocken — und nur dann, wenn er
            # einen echten Gegentrend mit ADX >= four_h_veto_adx zeigt.
            h1_sig = tf_1h["signal"]
            base_bias = "LONG" if h1_sig == "BULLISH" else "SHORT" if h1_sig == "BEARISH" else "NOISE"
            if base_bias == "NOISE":
                return BiasResult(
                    symbol=symbol, bias="NOISE",
                    session_open_price=session_open_price,
                    current_price=current_price,
                    session_chg_pct=session_chg_pct,
                    session_vol_ratio=session_vol_ratio,
                    candles_analyzed=n,
                    green_candles=green, red_candles=red,
                    signal_count=0,
                    reason=f"NOISE (Role-Mode: 1H {h1_sig} — kein gerichteter Bias) {tf_detail} | {chg_note}{vol_note}",
                    regime={"regime": "ROLE_1H_NEUTRAL", "veto": True},
                )
            four_h_veto = bool(regime_cfg.get("four_h_veto", True))
            if four_h_veto and tf_4h["signal"] in ("BULLISH", "BEARISH") and tf_4h["signal"] != h1_sig:
                adx4 = float(tf_4h.get("adx") or 0.0)
                if adx4 >= float(regime_cfg.get("four_h_veto_adx", 25.0)):
                    return BiasResult(
                        symbol=symbol, bias="NOISE",
                        session_open_price=session_open_price,
                        current_price=current_price,
                        session_chg_pct=session_chg_pct,
                        session_vol_ratio=session_vol_ratio,
                        candles_analyzed=n,
                        green_candles=green, red_candles=red,
                        signal_count=signal_count,
                        reason=(f"NOISE (4H-Veto: {tf_4h['signal']} ADX {adx4:.1f} gegen "
                                f"1H {h1_sig}) {tf_detail} | {chg_note}{vol_note}"),
                        regime={"regime": "VETO_4H", "adx": adx4, "veto": True},
                    )
        else:
            base_bias = "LONG" if bullish >= 2 else "SHORT" if bearish >= 2 else "NOISE"

        regime_info = {}
        if base_bias != "NOISE":
            regime_info = classify_regime(
                tf_1h, base_bias,
                trend_threshold=float(regime_cfg.get("trend_threshold", 25.0)),
                chop_threshold=float(regime_cfg.get("chop_threshold", 20.0)),
            )
            if regime_info.get("veto") and regime_enabled and not regime_log_only:
                return BiasResult(
                    symbol=symbol, bias="NOISE",
                    session_open_price=session_open_price,
                    current_price=current_price,
                    session_chg_pct=session_chg_pct,
                    session_vol_ratio=session_vol_ratio,
                    candles_analyzed=n,
                    green_candles=green, red_candles=red,
                    signal_count=signal_count,
                    reason=f"NOISE (Regime-Veto: {regime_info['reason']}) {tf_detail} | {chg_note}{vol_note}",
                    regime=regime_info,
                )

        # ── Volumen-Filter: Session muss mind. so aktiv sein wie normal ──
        # Verhindert Drift-Trades auf toten Coins (Vol 0.0x–0.1x)
        # ONDO/BCH/ADA hatten alle Vol 0.0x und kamen nie über +1%
        MIN_SESSION_VOL_RATIO = self._cfg.bias.get("min_session_vol_ratio", 1.0) \
            if hasattr(self._cfg, 'bias') else 1.0
        if session_vol_ratio < MIN_SESSION_VOL_RATIO:
            return BiasResult(
                symbol=symbol, bias="NOISE",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=0,
                reason=f"NOISE (Vol {session_vol_ratio:.2f}x < {MIN_SESSION_VOL_RATIO:.1f}x) — keine Session-Aktivität | {tf_detail}"
            )

        # ── Ergebnis: Role-Mode (1H gerichtet) ODER 2/3-Vote ─────
        # Im Role-Mode bestimmt 1H die Richtung; 15m liefert nur noch
        # Bestaetigung (Trigger liegt ohnehin im 5m-Pullback). signal_count
        # wird so kodiert, dass das Ranking in session.py weiter funktioniert:
        #   3 = 1H + 15m gleichgerichtet  (volles Role-Setup)
        #   2 = 1H gerichtet, 15m neutral/gegenlaeufig
        if role_mode:
            signal_count = 3 if tf_15m["signal"] == h1_sig else 2
            long_reason = f"LONG (Role: 1H {h1_sig}, 15m {tf_15m['signal']}) {tf_detail} | {chg_note}{vol_note}"
            short_reason = f"SHORT (Role: 1H {h1_sig}, 15m {tf_15m['signal']}) {tf_detail} | {chg_note}{vol_note}"
        else:
            long_reason = f"LONG ({bullish}/3) {tf_detail} | {chg_note}{vol_note}"
            short_reason = f"SHORT ({bearish}/3) {tf_detail} | {chg_note}{vol_note}"

        if base_bias == "LONG":
            return BiasResult(
                symbol=symbol, bias="LONG",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=signal_count,
                reason=long_reason,
                regime=regime_info,
            )

        if base_bias == "SHORT":
            return BiasResult(
                symbol=symbol, bias="SHORT",
                session_open_price=session_open_price,
                current_price=current_price,
                session_chg_pct=session_chg_pct,
                session_vol_ratio=session_vol_ratio,
                candles_analyzed=n,
                green_candles=green, red_candles=red,
                signal_count=signal_count,
                reason=short_reason,
                regime=regime_info,
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
            reason=f"NOISE ({bullish}B/{bearish}S/{neutral}N) {tf_detail} | {chg_note}{vol_note}"
        )


# ─── analyze_watchlist (Interface für Cron) ─────────────────────────

def analyze_watchlist(
    symbols: List[str],
    session_open_ts: int,
    include_features: bool = False,
) -> List[BiasResult]:
    """Analysiert Bias für eine Liste von Coins (ohne Print — Cron macht Output)."""
    analyzer = BiasAnalyzer()
    results = []
    for sym in symbols:
        try:
            result = analyzer.analyze(sym, session_open_ts)
            if include_features:
                result = analyzer._build_feature_snapshot(result)
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