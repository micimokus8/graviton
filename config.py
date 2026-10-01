#!/usr/bin/env python3
"""
graviton/config.py — Central Configuration
===========================================
Alle Parameter für den Graviton EMA20 Momentum Pullback Bot.
Kein LLM. Pure Python. Deterministic signals.
"""

from __future__ import annotations
import os
from pathlib import Path
from dataclasses import dataclass, field

# ─── .env laden ────────────────────────────────────────────────────

ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# ─── Mode ──────────────────────────────────────────────────────────

DRY_RUN = False  # False = LIVE-Trading (am 27.09.2026 freigeschaltet, halbe Größe 8.75%)

SESSIONS = {
    "ny":   {"scan": "12:00", "open": "12:30", "close": "16:00"},
    "asia": {"scan": "23:30", "open": "00:00", "close": "02:00"},
}

# Week 1: only NY. Set to ["ny", "asia"] for both.
ACTIVE_SESSIONS = ["ny"]

# ─── Scan ──────────────────────────────────────────────────────────

SCAN = {
    "min_change_pct":  3.0,    # Runter von 4.0 — XMR (+3.7%) und XLM (-3.8%) sonst raus
    "max_change_pct":  99.0,   # Deaktiviert — Futures Ticker abweichend vom Spot
    "min_volume_eur":  500_000,
    "max_watchlist":   8,
}

# ─── Bias ──────────────────────────────────────────────────────────

LLM_BIAS_ENABLED = _env("LLM_BIAS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
LLM_BIAS_API_KEY = _env("LLM_BIAS_API_KEY", "")
LLM_BIAS_BASE_URL = _env("LLM_BIAS_BASE_URL", "https://openrouter.ai/api/v1")
LLM_BIAS_MODEL = _env("LLM_BIAS_MODEL", "")
LLM_BIAS_MIN_CONFIDENCE = _env_float("LLM_BIAS_MIN_CONFIDENCE", 0.65)

BIAS = {
    "timeframe":              "15m",
    "min_candles":           4,           # 4 Kerzen = 1h Daten (v2)
    "rsi_long_max":          80,          # wird nur noch vom Entry-Check genutzt
    "rsi_short_min":         20,
    "min_session_vol_ratio": 0.7,         # 0.7× statt 1.0×, Median-Vergleich
}

# ─── Entry ─────────────────────────────────────────────────────────

ENTRY = {
    "timeframe":          "1m",
    "ema_period":         20,
    "ema_smoothing":      9,
    "ema_distance_max":   0.50,    # Basis-Distanz; wird dynamisch via 24h-Change skaliert
    "sl_offset_pct":      0.90,    # Mindest-SL; effektiver SL = min(max(1.0× 1H-ATR, 0.9%), Cap)
    "sl_cap_pct":         1.50,     # Maximal-SL; auf 1.5% reduziert (vorher 3.0% — MON -3.28% war zu weit)
    "max_stair_steps":    1,       # Start: nur erster Pullback
    "max_parallel_coins": 1,
}

# ─── S/R Kontext ───────────────────────────────────────────────────

SR = {
    "lookback_weeks":   2,
    "min_distance_pct": 0.50,         # kein Entry wenn S/R < 0.5% entfernt
}

# ─── Position Sizing ───────────────────────────────────────────────

EQUITY_USD = _env_float("EQUITY_USD", 200.0)

POSITION = {
    "account_risk_pct_per_coin":  8.75,   # ~$50 Position bei ~$570 Equity (halbiert für Live-Start)
    "max_total_exposure_pct":     17.5,   # = 1 Position
}

# Dynamische Positionsgröße (wird bei Config-Init berechnet)
_position_size_usd = EQUITY_USD * (POSITION["account_risk_pct_per_coin"] / 100)

# ─── Exit ──────────────────────────────────────────────────────────

EXIT = {
    "ema_overextended_pct":  2.50,    # Preis > 2.5% von EMA → struktureller Exit (inaktiv, siehe structural_exits)
    "trailing_pct":          0.50,    # Trailing Stop Abstand für die Rest-Hälfte
    "profit_lock_pct":        1.05,    # 50% sichern, danach Rest trailing
    "pattern_exit_50":       False,   # Pattern aus — Live = DRY RUN (1m-Umkehr vs 5m-Pullback-Konflikt)
    "structural_exits":      False,   # EMA/RSI/S/R aus — Live = DRY RUN (nur SL + Profit-Lock + Trailing)
    "rsi_extreme_long":      78,
    "rsi_extreme_short":     22,
}

# ─── Exchange ──────────────────────────────────────────────────────

EXCHANGE = {
    "name":      "krakenfutures",     # CCXT exchange ID
    "leverage":  1,
    "margin":    "isolated",
}

# ─── Paths ─────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)


@dataclass
class Config:
    """Typed configuration (compatible with dict access)."""
    sessions: dict = field(default_factory=lambda: dict(SESSIONS))
    active_sessions: list = field(default_factory=lambda: list(ACTIVE_SESSIONS))
    scan: dict = field(default_factory=lambda: dict(SCAN))
    bias: dict = field(default_factory=lambda: dict(BIAS))
    entry: dict = field(default_factory=lambda: dict(ENTRY))
    sr: dict = field(default_factory=lambda: dict(SR))
    position: dict = field(default_factory=lambda: dict(POSITION))
    exit: dict = field(default_factory=lambda: dict(EXIT))
    exchange: dict = field(default_factory=lambda: dict(EXCHANGE))
    base_dir: Path = BASE_DIR
    logs_dir: Path = LOGS_DIR

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


# Singleton
CFG = Config()