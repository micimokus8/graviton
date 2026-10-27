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
from candle_utils import closed_ohlcv
from regime import atr_pct as regime_atr_pct, volatility_expansion


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
    # Kontinuierliche Entry-Features fuer die spaetere Bucket-Auswertung.
    # Die KI-Kritik war klar: nicht raten, sondern als Variable loggen.
    bar_close_pos: float = 0.0   # Close-Position im Entry-Bar (0=Tief, 1=Hoch)
    atr_pct: float = 0.0         # ATR(5m) in % des Preises
    atr_ratio: float = 0.0       # ATR(5m) / Median der letzten 20 Bars


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

    def _fetch_1m(self, symbol: str, limit: int = 50, tf: str = "1m") -> Optional[np.ndarray]:
        """Fetch OHLCV. Default 1m, alternativ 5m. None bei API-Fehler."""
        ex = self._get_exchange()
        try:
            candles = ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        except Exception as e:
            print(f"   [entry] OHLCV-Fetch-Fehler {symbol} {tf}: {e}")
            return None
        return closed_ohlcv(candles, tf)

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

        Gelockert für Krypto-Volatilität (v2):
          - Wick-Toleranz 0.5% statt 0.3% (tiefere EMA-Penetration normal)
          - Body/Wick-Ratio ≥ 0.25 statt 0.5 (erlaubt Kerzen mit moderatem Wick)
          - NEU: Kerze muss stark in Richtung schließen (Close im oberen/
            unteren 40%-Bereich der Range) — trennt echten Bounce von
            schwacher Berührung, ohne die Pullback-Logik zu blockieren.
        """
        body = abs(close - open_)
        if body == 0:
            return False

        WICK_TOLERANCE_PCT = 0.5   # 5m-Kerze darf 0.5% durch die EMA wicken
        MIN_BODY_RATIO = 0.25       # Body muss ≥ 25% des Wicks sein

        candle_range = high - low
        if candle_range <= 0:
            return False

        if bias == "LONG":
            if close <= open_:
                return False
            # Low nahe EMA (Support)
            low_dist = (ema - low) / ema * 100
            if low_dist > WICK_TOLERANCE_PCT:
                return False
            # Close muss im oberen Bereich der Kerze liegen (starker Schluss)
            if close < low + candle_range * 0.6:
                return False
            upper_wick = high - close
            if upper_wick <= 0:
                return True  # Kein oberer Wick → klare Rejection
            return body >= upper_wick * MIN_BODY_RATIO

        elif bias == "SHORT":
            if close >= open_:
                return False
            # High nahe EMA (Resistance)
            high_dist = (high - ema) / ema * 100
            if high_dist > WICK_TOLERANCE_PCT:
                return False
            # Close muss im unteren Bereich der Kerze liegen (starker Schluss)
            if close > low + candle_range * 0.4:
                return False
            lower_wick = min(open_, close) - low
            if lower_wick <= 0:
                return True  # Kein unterer Wick → klare Rejection
            return body >= lower_wick * MIN_BODY_RATIO

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
        """Berechnet SL-Prozent: 1.0× 1H ATR, min 0.9%."""
        atr_1h_pct = 0.0
        try:
            ex = self._get_exchange()
            candles_1h = closed_ohlcv(
                ex.fetch_ohlcv(symbol, timeframe="1h", limit=20), "1h"
            )
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
        # Cap: ATR-Spikes (neue Listings, Vol-Explosionen) dürfen den SL nicht
        # auf absurde Distanzen treiben — ACE 15.08. hatte -31.8%, GPS -6.0%.
        cap = CFG.entry.get("sl_cap_pct", 3.0)
        return min(max(atr_1h_pct * 1.0, CFG.entry["sl_offset_pct"]), cap)

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
                return max(base_dist, 1.50)
            elif change_24h >= 5:
                return max(base_dist, 0.80)
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

        # Fetch 5m Daten — Guard: leere/dünne Daten = kein Entry (fail-closed).
        # Vorher: IndexError "index 0 is out of bounds" im 30s-Polling (POL 24.08.,
        # Kraken lieferte 0×5m-Kerzen) → Session ohne Entry beendet.
        data = self._fetch_1m(symbol, limit=60, tf="5m")
        if data is None or len(data) < ema_period + 1:
            return EntrySignal(
                symbol=symbol, bias=bias, state=EntryState.NO_ENTRY,
                price=0, ema20=0, distance_pct=999,
                rejection=False, rejection_high=0, rejection_low=0,
                rejection_close=0, entry_price=0, stop_loss=0,
                step=current_step, timestamp=datetime.now(timezone.utc).isoformat(),
            )
        closes  = data[:, 4]
        highs   = data[:, 2]
        lows    = data[:, 3]
        opens   = data[:, 1]
        volumes = data[:, 5]  # für Volumen-Confirmation

        # EMA20 (KEIN Smoothing auf 1m — doppelte Glättung erzeugt zu viel Lag)
        ema_raw = self._calc_ema(closes, ema_period)
        current_ema = float(ema_raw[-1])
        current_price = float(closes[-1])

        # ─── LONG Overextension-Gate ─────────────────────────────
        # Empirie (40 Trades, Sep-Okt 2026): LONG-Entries >3% über
        # EMA20(1h): 13 Trades, netto -7.5% — der klassische "Peak
        # weggekauft"-Fall (QNT 26.10.: +10.7% über EMA → SL -2.47%).
        # LONG nahe EMA20(1h): WR 62%. Gate blockiert nur LONG;
        # SHORT-Bewertung unangetastet. Threshold konfigurierbar.
        if bias == "LONG":
            gate = float(CFG.entry.get("overextension_max_long", 6.0))
            try:
                oh1h = self._fetch_1m(symbol, limit=30, tf="1h")
                if oh1h is not None and len(oh1h) >= 21:
                    ema1h = float(self._calc_ema(oh1h[:, 4], 20)[-1])
                    dist_1h = (current_price - ema1h) / ema1h * 100
                    if dist_1h > gate:
                        return EntrySignal(
                            symbol=symbol, bias=bias, state=EntryState.NO_ENTRY,
                            price=round(current_price, 6), ema20=round(ema1h, 6),
                            distance_pct=round(dist_1h, 4),
                            rejection=False, rejection_high=0, rejection_low=0,
                            rejection_close=0, entry_price=0, stop_loss=0,
                            step=current_step,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            reasoning=f"Overextension-Gate: +{dist_1h:.1f}% über EMA20(1h) (> {gate:.0f}%) — überhitzt, kein LONG",
                        )
            except Exception:
                pass  # Gate-Daten nicht ladbar → Gate offen (kein Hard-Fail)

        # Distanz zur EMA
        distance_pct = abs(current_price - current_ema) / current_ema * 100

        # ─── State Machine ──────────────────────────────────────
        # Ein Pullback darf die EMA20 kurzfristig unterschreiten (LONG)
        # bzw. überschreiten (SHORT). Die alte Seitenprüfung an dieser Stelle
        # hat genau solche Pullbacks vor der Rejection-Prüfung verworfen.
        # Die Richtung wird weiterhin durch Bias + bestätigte 5m-Rejection
        # und Volumen bestätigt; ein weiter entfernter Preis bleibt unten
        # WAITING/APPROACHING.

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

        # Fast-Entry ENTFERNT — 30-65 RSI ist praktisch immer wahr und
        # umgeht die Rejection+Volumen-Bestätigung komplett.
        # Jeder Entry MUSS jetzt über die Rejection-Kerze unten laufen.

        # Rejection auf den letzten drei *geschlossenen* 5m-Kerzen prüfen.
        # Nur die allerletzte Kerze zu akzeptieren verpasst gültige Pullbacks:
        # die Folgekerze kann bereits wieder von der EMA weg laufen.
        try:
            data_5m = data
            if len(data_5m) < 12:
                return signal

            recent_start = max(0, len(data_5m) - 3)
            for idx in range(len(data_5m) - 1, recent_start - 1, -1):
                open5, high5, low5, close5 = map(
                    float, data_5m[idx, 1:5]
                )
                if not self._is_rejection_candle(
                    open5, high5, low5, close5, current_ema, bias
                ):
                    continue

                # Volumen-Confirmation auf GENAU dieser Rejection-Kerze.
                # Vorher wurde Volumen nur im Scan/Bias geprüft — das
                # Signal-Bar (1.50x) wurde mit dem Entry-Bar verwechselt,
                # der real nur 0.44x hatte (DASH 27.10.). Eine Rejection
                # ohne Beteiligung ist keine Rejection.
                min_vol = float(cfg.get("rejection_min_vol_ratio", 1.0))
                vol_ratio = 0.0
                try:
                    hist = volumes[max(0, idx - 20):idx]
                    med = float(np.median(hist)) if len(hist) else 0.0
                    vol_ratio = float(volumes[idx] / med) if med > 0 else 0.0
                except Exception:
                    vol_ratio = 0.0
                if min_vol > 0 and vol_ratio < min_vol:
                    continue

                # Distanz-Check (nach Volumen, damit Logs den echten Grund zeigen)
                if distance_pct > max_dist:
                    continue

                # ── Entry-Bar-Geometrie (der eigentliche KI-Fix) ─────
                # Die Rejection darf 2 Bars zurückliegen — aber der Bar, zu
                # dessen Close wir einsteigen, darf kein Absturzbar sein.
                # DASH 27.10.: Rejection 2 Bars zurück akzeptiert, Entry-Bar
                # schloss auf 0% der Range (55.36 → 55.14) → Long ins fallende
                # Messer. Regel: LONG nur, wenn der Entry-Bar in der oberen
                # Hälfte schließt; SHORT spiegelbildlich.
                last_bar = data_5m[-1]
                rng_last = float(last_bar[2]) - float(last_bar[3])
                pos_last = 0.5
                if rng_last > 0:
                    pos_last = (float(last_bar[4]) - float(last_bar[3])) / rng_last
                    min_pos = float(cfg.get("entry_bar_min_close_pos", 0.35))
                    if bias == "LONG" and pos_last < min_pos:
                        continue
                    if bias == "SHORT" and pos_last > (1.0 - min_pos):
                        continue

                # Nicht zum alten Kerzenschluss kaufen: Die Rejection ist
                # nur der Trigger, ausgeführt wird zum aktuellen Marktpreis.
                signal.entry_price = current_price
                signal.bar_close_pos = round(float(pos_last), 3)
                try:
                    signal.atr_pct = round(float(regime_atr_pct(data_5m)), 3)
                    signal.atr_ratio = round(float(volatility_expansion(data_5m)), 2)
                except Exception:
                    pass
                sl_pct = self._calc_sl_pct(symbol, signal.entry_price)
                if bias == "LONG":
                    signal.stop_loss = round(signal.entry_price * (1 - sl_pct / 100), 8)
                else:
                    signal.stop_loss = round(signal.entry_price * (1 + sl_pct / 100), 8)
                signal.state = EntryState.ENTERED
                age = len(data_5m) - 1 - idx
                signal.reasoning = (
                    f"Pullback: 5m Rejection an EMA ({distance_pct:.2f}%, "
                    f"{age} Kerze(n) zurück, Vol {vol_ratio:.2f}x)"
                )
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