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

DRY_RUN = True  # True = kein Live-Trading, nur Logs

SESSIONS = {
    "ny":   {"scan": "13:00", "open": "13:30", "close": "16:00"},
    "asia": {"scan": "23:30", "open": "00:00", "close": "02:00"},
}

# Week 1: only NY. Set to ["ny", "asia"] for both.
ACTIVE_SESSIONS = ["ny"]

# ─── Scan ──────────────────────────────────────────────────────────

SCAN = {
    "min_change_pct":  3.0,    # Runter von 4.0 — XMR (+3.7%) und XLM (-3.8%) sonst raus
    "max_change_pct":  99.0,   # Deaktiviert — Futures Ticker abweichend vom Spot
    "min_volume_eur":  750_000,
    "max_watchlist":   8,
}

# ─── Bias ──────────────────────────────────────────────────────────

BIAS = {
    "timeframe":      "15m",
    "min_candles":    2,          # min 2 geschlossene Kerzen nach Session-Open
    "rsi_long_max":   80,         # RSI > 80 bei LONG-Bias → skip
    "rsi_short_min":  20,         # RSI < 20 bei SHORT-Bias → skip
}

# ─── Entry ─────────────────────────────────────────────────────────

ENTRY = {
    "timeframe":          "1m",
    "ema_period":         20,
    "ema_smoothing":      9,
    "ema_distance_max":   0.50,    # Basis-Distanz; wird dynamisch via 24h-Change skaliert
    "sl_offset_pct":      0.20,    # wird vom 1H-ATR überschrieben
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
    "account_risk_pct_per_coin":  17.5,   # % der Equity pro Coin
    "max_total_exposure_pct":     17.5,   # = 1 Position
}

# Dynamische Positionsgröße (wird bei Config-Init berechnet)
_position_size_usd = EQUITY_USD * (POSITION["account_risk_pct_per_coin"] / 100)

# ─── Exit ──────────────────────────────────────────────────────────

EXIT = {
    "ema_overextended_pct":  2.50,    # Preis > 2.5% von EMA → struktureller Exit
    "trailing_pct":          0.30,    # Trailing Stop Abstand
    "pattern_exit_50":       True,    # 50% raus bei Pattern
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