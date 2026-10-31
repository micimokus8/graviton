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

DRY_RUN = True  # DRY_RUN aktiviert 25.10 auf User-Wunsch (Live-Pause)

# Zwei Modi, EIN Bot, EIN Cron. Umgeschaltet wird ueber die Umgebungsvariable
# GRAVITON_MODE (die Cron-Skripte lesen sie aus der Datei MODE).
#   crypto — wie bisher: 24/7-faehig, 5m-Entry, enger SL
#   stocks — tokenisierte Aktien-Perps (AAPLX, SPCXX, NVDAX, ...):
#            15m-Entry, weiterer SL, eigenes Datenverzeichnis, nur Mo-Fr
# Reihenfolge: Umgebungsvariable (Cron) -> MODE-Datei -> crypto.
# Die Datei ist die Quelle der Wahrheit, damit auch direkte Python-Laeufe
# (Debug, Tests, manuelle Skripte) denselben Modus sehen wie der Cron.
_MODE_FILE = Path(__file__).parent / "MODE"
MODE = _env("GRAVITON_MODE", "").strip().lower()
if not MODE and _MODE_FILE.exists():
    try:
        MODE = _MODE_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        MODE = ""
if MODE not in {"crypto", "stocks"}:
    MODE = "crypto"

SESSIONS = {
    "ny":   {"scan": "13:00", "open": "13:30", "close": "17:00"},
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
    # Abstand zur EMA20 wird in ATR-Einheiten gemessen, nicht in Prozent:
    # Prozent ist auf ruhigen Coins viel zu weit und auf volatilen zu eng.
    # Messung 28.10. (41 Trades): Abstand <0.7 ATR5 -> WR 65%, +19.5% Summe;
    # >=0.7 ATR -> WR 39%, -13.0% Summe. Das war der eigentliche Verlustbringer,
    # nicht der Stop. 0 = Gate aus.
    # Timeframe fuer EMA20 + ATR + Rejection, getrennt nach Instrument.
    # Backtest (scripts/tf_backtest.py, 4000 Bars je Symbol):
    #   15m Aktien-Perps: +12.4% / 177 Trades / WR 51% / 21 von 40 Tagen gruen
    #   5m  Aktien-Perps:  +1.7% /  82 Trades / WR 39%
    #   15m Krypto:       -21.0% / 155 Trades / WR 39%   <- 15m schadet hier
    #   5m  Krypto:        +3.4% /  56 Trades / WR 46%
    # Aktien sind traeger, 15m glaettet dort das Rauschen; Krypto laeuft auf
    # 5m besser. Deshalb pro Symbolklasse.
    "entry_tf":            "5m",     # Krypto / Default
    "entry_tf_stock":      "15m",    # tokenisierte Aktien-Perps (AAPLX, SPCXX, ...)
    # Aktien-Universum: nur xStocks (14: AAPLX, SPCXX, NVDAX, TSLAX, ...).
    # Die Kategorie "Equities" (AMD, INTC, PLTR, MU, SOXL, SAMSUNG) sind zwar
    # ebenfalls Perpetuals ohne Verfall, bleiben aber aussen vor (User-Entscheid
    # 30.10.: nur X-Suffix handeln). Pre-IPO/Rohstoffe/Forex/Indizes ebenso.
    "stock_categories": ["xStocks"],
    "ema_distance_max_atr": 0.70,
    "ema_distance_max":   2.00,    # harter Prozent-Cap als Sicherheitsnetz
    "overextension_max_long": 6.0,  # LONG-Block wenn Preis > +6% über EMA20(1h) — Parabel-Schutz
    # Mindest-SL; effektiver SL = min(max(1.0x 1H-ATR, Floor), Cap).
    # Messung 28.10. (41 Trades, echte 5m-Kerzen bis Session-Close):
    #   Floor 0.9% -> +2.0%  |  1.5% -> +19.9%  |  2.0% -> +16.1%
    #   3.0% -> +8.9%        |  5.0% -> +1.9%   | 10.0% -> +3.5%  | kein SL -> +10.0%
    # Der 1.4%-Stop war kein Rauschen, sondern zu eng fuer die 0.9er-Untergrenze.
    "sl_offset_pct":      1.50,
    "sl_cap_pct":         5.0,     # Maximal-SL; 5% gibt Krypto-Intraday genug Luft (vorher 2.5% zu eng)
    "max_stair_steps":    1,       # Start: nur erster Pullback
    "max_parallel_coins": 1,
    # Rejection-Kerze muss echte Beteiligung zeigen (Volumen vs 20-Bar-Median).
    # DASH 27.10.: Signal-Bar 1.50x, Entry-Bar real 0.44x — Volumen wurde
    # bisher nur im Scan/Bias geprueft. 0 = Filter aus.
    "rejection_min_vol_ratio": 1.0,
    # Entry-Bar darf nicht am Extrem schliessen (LONG: untere Haelfte = Absturz).
    "entry_bar_min_close_pos": 0.35,   # <35% = Absturzbar (am Tief geschlossen)
}

# ─── Regime-Gate (ADX 1H + Richtung) ────────────────────────────────
# Empirie 41 Trades: ADX<20 avg -1.27%/Trade, ADX>=25 avg +0.29%/Trade.
# ADX hat KEINE Richtung — classify_regime() prueft +DI/-DI gegen den Bias.
REGIME = {
    "enabled":            True,   # Gate aktiv (bias.py → NOISE bei Veto)
    "trend_threshold":    25.0,   # ADX >= 25 = starker Trend
    "chop_threshold":     20.0,   # ADX < 20 = trendlos, kein Trendfolge-Entry
    "log_only":           False,  # True = nur loggen, nicht blocken (Shadow-Modus)
    # Rollen statt Stimmen (KI-Empfehlung): 4H = Veto, 1H = Bias, 15m = Trigger.
    # False = alte 2-von-3-Demokratie (Fallback / A-B-Test).
    "role_mode":          True,
    "four_h_veto":        True,
    "four_h_veto_adx":    25.0,   # 4H-Gegentrend zaehlt nur als Veto ab ADX 25
    # ADX<20 als Eintritts-Block ist NICHT belegt (n=4) und mechanisch
    # fragwuerdig: der Edge lebt in flachen Pullbacks/kontrahierter Vola,
    # also eher NIEDRIGEM ADX. Default: nur loggen, nicht blocken.
    "chop_gate_enabled":  False,
}


def profit_lock_pct_for(entry_price: float, stop_loss: float,
                        cfg: dict | None = None) -> float:
    """Ziel-Prozent fuer den 50%-Profit-Lock: target_rr x SL-Distanz.

    Fixe 2% waren bei 1.41% SL nur 1.4R. Hier wird das Ziel an den
    tatsaechlichen Stop gekoppelt (Mindest-R), mit Untergrenze und Cap.
    """
    exit_cfg = EXIT if cfg is None else cfg
    if not entry_price:
        return float(exit_cfg["profit_lock_pct"])
    sl_pct = abs(entry_price - stop_loss) / entry_price * 100
    target = max(sl_pct * float(exit_cfg.get("target_rr", 2.0)),
                 float(exit_cfg["profit_lock_pct"]))
    return min(target, float(exit_cfg.get("target_max_pct", 10.0)))

# ─── S/R Kontext ───────────────────────────────────────────────────

SR = {
    "lookback_weeks":   2,
    "min_distance_pct": 0.50,         # kein Entry wenn S/R < 0.5% entfernt
}

# ─── Position Sizing ───────────────────────────────────────────────

EQUITY_USD = _env_float("EQUITY_USD", 200.0)

POSITION = {
    # Fixer Einsatz pro Trade (User-Vorgabe 28.10.): 100 USDC, unabhaengig von
    # der Equity. 0 = stattdessen account_risk_pct_per_coin nutzen.
    "fixed_usd":                  100.0,
    "account_risk_pct_per_coin":  8.75,
    "max_total_exposure_pct":     17.5,   # = 1 Position
}

# Dynamische Positionsgröße (wird bei Config-Init berechnet)
_position_size_usd = EQUITY_USD * (POSITION["account_risk_pct_per_coin"] / 100)

# ─── Exit ──────────────────────────────────────────────────────────

EXIT = {
    "ema_overextended_pct":  2.50,    # Preis > 2.5% von EMA → struktureller Exit (inaktiv, siehe structural_exits)
    "trailing_pct":          0.50,    # Trailing Stop Abstand für die Rest-Hälfte
    # Ziel = R-Vielfaches des tatsaechlichen SL, nicht fixe Prozent.
    # Fixe 2% bei 1.41% SL waren nur 1.4R (KI-Befund). target_rr=2.0 setzt
    # das Ziel bei 2x SL; profit_lock_pct bleibt Untergrenze, target_max_pct
    # deckelt breite Stops (SL-Cap 5% -> sonst 10%+ Ziel, das nie laeuft).
    "profit_lock_pct":        2.0,    # Untergrenze fuer das 50%-Lock
    # 3R statt 2R: bei 1.5% SL liegt das Teilziel bei 4.5%. Messung: TP bei
    # 1.5R = +1.6%, 2R = +5.4%, 3R = +19.4% (ohne TP +19.9%) — fruehe TPs
    # schneiden genau die Ausreisser ab, die den Bot tragen.
    "target_rr":              3.0,
    "target_max_pct":        10.0,    # Cap fuer das Ziel in Prozent
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
    regime: dict = field(default_factory=lambda: dict(REGIME))
    sr: dict = field(default_factory=lambda: dict(SR))
    position: dict = field(default_factory=lambda: dict(POSITION))
    exit: dict = field(default_factory=lambda: dict(EXIT))
    exchange: dict = field(default_factory=lambda: dict(EXCHANGE))
    base_dir: Path = BASE_DIR
    logs_dir: Path = LOGS_DIR
    mode: str = MODE
    # default_factory: die Modus-Profile werden erst NACH der Klasse berechnet
    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    weekdays_only: bool = field(default_factory=lambda: WEEKDAYS_ONLY)
    universe: str = field(default_factory=lambda: UNIVERSE)

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


# ─── Mode-Profile ──────────────────────────────────────────────────
# Werte werden VOR Config() in die Modul-Dicts gemischt, damit alle Module
# (Scanner, Session, Entry, Exit) unveraendert ueber CFG.* lesen koennen.

from datetime import datetime as _dt, time as _time, timezone as _tz  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402

_NY = _ZoneInfo("America/New_York")


def _ny_to_utc(hhmm: str) -> str:
    """NYSE-Uhrzeit (ET) -> UTC. Beruecksichtigt die US-Sommerzeit automatisch."""
    today = _dt.now(_NY).date()
    t = _time(*[int(x) for x in hhmm.split(":")])
    return _dt.combine(today, t, tzinfo=_NY).astimezone(_tz.utc).strftime("%H:%M")


def _ny_window(open_et: str = "09:30", close_et: str = "16:00", scan_lead_min: int = 10) -> dict:
    o, c = _ny_to_utc(open_et), _ny_to_utc(close_et)
    oh, om = (int(x) for x in o.split(":"))
    total = (oh * 60 + om - scan_lead_min) % (24 * 60)
    return {"scan": f"{total // 60:02d}:{total % 60:02d}", "open": o, "close": c}


MODE_PROFILES = {
    "crypto": {
        "sessions": {"ny": {"scan": "13:00", "open": "13:30", "close": "17:00"},
                     "asia": {"scan": "23:30", "open": "00:00", "close": "02:00"}},
        "active_sessions": ["ny"],
        "scan": {"min_change_pct": 3.0, "min_volume_eur": 500_000, "max_watchlist": 8},
        "entry": {"entry_tf": "5m", "sl_offset_pct": 1.50, "sl_cap_pct": 5.0},
        "exit": {"target_rr": 3.0, "target_max_pct": 10.0, "profit_lock_pct": 2.0},
        "data_dir": None,            # = data/
        "weekdays_only": False,
        "universe": "crypto",
    },
    "stocks": {
        # NYSE 9:30-16:00 ET. Die UTC-Zeiten werden beim Import berechnet,
        # damit die US-Zeitumstellung (1.11.) kein manuelles Nachziehen braucht.
        "sessions": {"ny": _ny_window("09:30", "16:00", 10)},
        "active_sessions": ["ny"],
        # Aktien bewegen 1-3%/Tag — der Krypto-Filter (3%) wuerde alles aussortieren.
        # 27 Aktien-Perps insgesamt — nicht vorne weg aussortieren.
        "scan": {"min_change_pct": 0.8, "min_volume_eur": 20_000, "max_watchlist": 8},
        # 15m (Backtest: +12.4% vs +1.7% auf 5m), SL 2.5% statt 1.5%: ATR15m
        # auf Aktien ist ~0.3%, ein 1.5-ATR-Stop waere 0.45% und würde im
        # normalen Tagesrauschen liegen.
        # entry_tf bleibt 5m als Fallback; Aktien laufen ueber entry_tf_stock
        # (die Markt-Kategorie entscheidet, nicht der Modus).
        # ATR-Band 1.5 statt 0.7: 0.7 war auf 5m-Krypto kalibriert und blockte
        # auf 15m fast alles (30.10.: 7 von 8 Coins bei 1.7-6.6 ATR).
        # Messung xStocks 15m: 0.7 -> +12.0% (178 Trades), 1.5 -> +17.1% (244),
        # 2.5 -> +13.6% (264). 1.5 hat die beste Kombination aus Edge und Trades.
        "entry": {"sl_offset_pct": 2.50, "sl_cap_pct": 6.0, "ema_distance_max_atr": 1.50},
        # TP 2R statt 3R: bei 2.5% SL sind 3R = 7.5% Tagesziel, das praktisch
        # nie erreicht wird (Backtest: 4 TP-Treffer in 177 Trades).
        "exit": {"target_rr": 2.0, "target_max_pct": 12.0, "profit_lock_pct": 2.5},
        "data_dir": "data/stocks",
        "weekdays_only": True,
        "universe": "xstocks",
    },
}

_profile = MODE_PROFILES[MODE]
SESSIONS = _profile["sessions"]
ACTIVE_SESSIONS = _profile["active_sessions"]
SCAN.update(_profile["scan"])
ENTRY.update(_profile["entry"])
EXIT.update(_profile["exit"])
DATA_DIR = BASE_DIR / _profile["data_dir"] if _profile["data_dir"] else BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WEEKDAYS_ONLY = _profile["weekdays_only"]
UNIVERSE = _profile["universe"]

# LLM-Shadow-Bias ist komplett AUS (29.10., User-Entscheid).
# Gruende: der Prompt ist krypto-spezifisch (BTC-Kontext, Krypto-Indikatoren),
# xStocks scheiterten 8/8 am Symbol-Parser, und das Ergebnis hat nie eine
# Order beeinflusst (effective_bias = deterministisch). Kosten: ~30-40s pro
# Bias-Lauf. Wiedereinschalten: LLM_BIAS_ENABLED=true in .env UND hier True.
LLM_BIAS_ACTIVE = False


# Singleton
CFG = Config()