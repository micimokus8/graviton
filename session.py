#!/usr/bin/env python3
"""
graviton/session.py — Complete Session Runner
===============================================
Orchestriert Bias → Entry-Loop → Watcher → Session-Close.
Sendet Live-Updates via Telegram bei jedem Schlüsselereignis.

Usage:
  python3 session.py ny      # NY Session
  python3 session.py asia    # Asia Session
"""

import fcntl
import os
import json, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import SESSIONS, DRY_RUN, EXIT, profit_lock_pct_for
from entry import EntryEngine, EntryState
from exit import ExitEngine, ExitReason
from sr_levels import check_sr_for_entry
from telegram_sender import send as tg
from candle_utils import closed_ohlcv
from atomic_json import atomic_write_json
from candidate_ranking import deterministic_rank

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
BIAS_RESULT_FILE = DATA_DIR / "bias_result.json"
ENTRY_STATE_FILE = DATA_DIR / "entry_state.json"
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"
DEBUG_LOG_FILE = DATA_DIR / "session_debug.jsonl"  # pro Polling-Cycle: Coin, Status, Grund
WATCHLIST_MAX_AGE_SECONDS = 45 * 60
BIAS_MAX_AGE_SECONDS = 15 * 60
# Entry-Fenster: harte Obergrenze gegen veraltete Bias-Snapshots.
# Messung 27.10.: Bias 12:46 UTC -> Entry 12:50 UTC = 4 Min (nicht 20 —
# die KI hatte Scan->Entry gemessen). 15 Min = 1 Bias-Bar Cap, laesst aber
# genug Polling-Zeit nach dem 5-Min-Cron-Versatz (Bias 14:45, Session 14:50).
ENTRY_WINDOW_SECONDS = 15 * 60
MARKET_SENTIMENT_FILE = DATA_DIR / "market_sentiment.json"
MARKET_SENTIMENT_MAX_AGE_SECONDS = 6 * 60 * 60  # eine Session lang gültig (log-only Kontext)



def _base(cand: dict) -> str:
    """Extrahiert Base aus Symbol ('CRV/USD:USD' → 'CRV')."""
    return cand.get("base") or cand["symbol"].split("/")[0]


def _state_file_fresh(
    path: Path,
    max_age_seconds: float,
    *,
    now_ts: float | None = None,
) -> bool:
    """Return True only for an existing state file from the current run window."""
    if not path.exists():
        return False
    reference = time.time() if now_ts is None else now_ts
    age = reference - path.stat().st_mtime
    return -5 <= age <= max_age_seconds


def _load_market_sentiment() -> dict | None:
    """Load market_sentiment.json (fresh) as log-only trade context.

    Never filters or blocks a trade — the field is only attached to
    trade_log entries for later (Baustein 3) evaluation. Returns None
    when the file is missing, stale, or unreadable (fail-soft).
    """
    if not _state_file_fresh(MARKET_SENTIMENT_FILE, MARKET_SENTIMENT_MAX_AGE_SECONDS):
        return None
    try:
        with open(MARKET_SENTIMENT_FILE) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


# Entry-Kontext für Exit-Logs: wird beim Entry gesetzt und an jeden
# Exit-Eintrag angehängt (signal_count, EMA-Distanz), damit Trade-Logs
# die nachträgliche Auswertung (Baustein 3) ermöglichen.
_entry_meta: dict = {}


def _entry_deadline(bias_file: Path, close_dt: datetime) -> datetime:
    """Hard deadline for opening a new position from this bias snapshot."""
    bias_written_at = datetime.fromtimestamp(bias_file.stat().st_mtime, tz=timezone.utc)
    return min(close_dt, bias_written_at + timedelta(seconds=ENTRY_WINDOW_SECONDS))


def _state_symbols_match(watchlist: list[dict], bias_results: list[dict]) -> bool:
    """Bias output must represent exactly the watchlist it was built from."""
    watchlist_symbols = {item.get("symbol") for item in watchlist}
    bias_symbols = {item.get("symbol") for item in bias_results}
    return None not in watchlist_symbols | bias_symbols and watchlist_symbols == bias_symbols


def _partition_sr_candidates(candidates: list[dict], checker=check_sr_for_entry):
    """Split candidates by S/R without ever re-adding a blocked fallback."""
    active = []
    blocked = []
    for candidate in candidates:
        is_blocked, reason, _ = checker(
            candidate["symbol"], candidate["price"], candidate["bias"]
        )
        if is_blocked:
            blocked.append((candidate, reason))
        else:
            active.append(candidate)
    return active, blocked


class SessionLock:
    """Non-blocking process lock for one Graviton session key."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _rebase_stop_loss(
    signal_entry: float,
    signal_stop: float,
    fill_price: float,
    bias: str,
) -> float:
    """Preserve the signal's risk percentage around the actual live fill."""
    if min(signal_entry, signal_stop, fill_price) <= 0:
        raise ValueError("Entry-, Fill- und Stop-Preis müssen positiv sein")
    risk_fraction = abs(signal_entry - signal_stop) / signal_entry
    if bias == "LONG":
        return round(fill_price * (1 - risk_fraction), 8)
    return round(fill_price * (1 + risk_fraction), 8)


def _execute_live_entry(trader, symbol: str, bias: str, signal):
    """Open a live position and require confirmed exchange protection."""
    fill = trader.open_position(symbol, bias.lower())
    if not fill.success:
        return None, signal.stop_loss, f"Entry fehlgeschlagen: {fill.message}"

    live_stop = _rebase_stop_loss(
        signal.entry_price, signal.stop_loss, fill.price, bias
    )
    if trader.set_stop_loss(symbol, bias.lower(), live_stop, fill.size):
        return fill, live_stop, ""

    emergency = trader.close_position(symbol, bias.lower(), close_pct=1.0)
    if emergency.success:
        return None, live_stop, (
            "Stop-Loss fehlgeschlagen; Entry per Emergency-Close geschlossen"
        )
    return fill, live_stop, (
        "KRITISCH: Stop-Loss und Emergency-Close fehlgeschlagen — "
        f"{emergency.message}"
    )


def _execute_live_partial_exit(
    trader,
    symbol: str,
    bias: str,
    new_stop: float,
) -> tuple[str, str, float]:
    """Reduce first under existing protection, then replace the stop.

    Returns (status, error, partial_fill_price). partial_fill_price ist der
    echte Fill des 50%-Closes (0.0 wenn keiner bestätigt) — Grundlage für
    die Exit-Meldung, NICHT der letzte Fill (der könnte schon der SL-Close
    der anderen Hälfte sein).
    """
    partial = trader.close_position(symbol, bias.lower(), close_pct=0.5)
    if not partial.success:
        return "unchanged", f"Partial-Close fehlgeschlagen: {partial.message}", 0.0
    partial_fill = float(partial.price or 0)
    if trader.set_stop_loss(symbol, bias.lower(), new_stop):
        return "protected", "", partial_fill
    emergency = trader.close_position(symbol, bias.lower(), close_pct=1.0)
    if emergency.success:
        return "closed", "Break-Even-SL fehlgeschlagen; Rest per Emergency-Close geschlossen", partial_fill
    return "critical", (
        "KRITISCH: Break-Even-SL und Emergency-Close fehlgeschlagen — "
        f"{emergency.message}"
    ), partial_fill


def _last_live_fill(trader, symbol: str, entry_ts_ms: float) -> dict | None:
    """Letzter echter Kraken-Fill seit Entry — Grundlage für Fill-Preis."""
    fills = trader.get_fills_since(symbol, entry_ts_ms)
    if not fills:
        return None
    last = fills[-1]
    return {
        "time": last.get("timestamp"),
        "side": last.get("side"),
        "amount": float(last.get("amount", 0) or 0),
        "price": float(last.get("price", 0) or 0),
    }


def _live_fill_pnl(entry_price: float, fill_price: float, bias: str) -> float:
    """PnL% auf Basis des echten Fill-Preises (statt Signal-Preis)."""
    if entry_price <= 0 or fill_price <= 0:
        return 0.0
    if bias == "LONG":
        return (fill_price - entry_price) / entry_price * 100
    return (entry_price - fill_price) / entry_price * 100


def _live_account_line(trader) -> str:
    """Aktueller Konto-Stand als Telegram-Zeile (nach Exit)."""
    if trader is None:
        return "n/a"
    return _account_str(trader)


def _emit_exit(
    mode: str, name: str, reason: str, level: str, close_pct: int, info: str,
    entry_price: float, symbol: str, base: str, bias: str,
    fill_price: float, rsi: float, account_before: str | None, trader,
) -> float:
    """Baut Telegram-Meldung + Log mit echtem Fill-Preis + Konto-Stand."""
    pnl = _live_fill_pnl(entry_price, fill_price, bias)
    pnl_icon = "🟢" if pnl >= 0 else "🔴"
    account_after = _live_account_line(trader)
    msg = (
        f"📤 {mode} EXIT {close_pct}% {bias} {base}\n"
        f"   Level:  {level} — {reason}\n"
        f"   Preis:  {fill_price:.6f}\n"
        f"   PnL:    {pnl_icon} {pnl:+.2f}%"
        + (f" | RSI: {rsi}" if rsi else "")
        + "\n"
        f"   Info:   {info}\n"
        f"{account_after}"
    )
    print(msg); tg(msg)
    _log_trade("exit", symbol=symbol, base=base, bias=bias,
               reason=reason, close_pct=close_pct,
               pnl_pct=round(pnl, 2), price=fill_price,
               rsi=rsi, session=name,
               account_before=account_before, account_after=account_after)
    return fill_price


def _confirm_and_report(
    mode: str, name: str, reason: str, level: str, close_pct: int, info: str,
    entry_price: float, symbol: str, base: str, bias: str,
    rsi: float, entry_ts_ms: float, account_before: str | None, trader,
) -> float | None:
    """Führt den echten Close aus und meldet NUR mit bestätigtem Fill.

    Holt nach dem Close die echten Kraken-Fills, verwendet den letzten
    echten Fill-Preis für PnL und hängt den Konto-Stand an. Gibt den
    echten Fill-Preis zurück oder None wenn kein Fill bestätigt wurde.
    """
    if trader is None:
        return None
    result = trader.close_position(symbol, bias.lower())
    if not result.success:
        if "Keine Position" in str(result.message):
            # Position war schon weg (z.B. externer SL) — echten Fill holen.
            fill = _last_live_fill(trader, symbol, entry_ts_ms)
            if fill and fill["price"] > 0:
                return _emit_exit(
                    mode, name, reason, level, close_pct, info, entry_price,
                    symbol, base, bias, fill["price"], rsi,
                    account_before, trader,
                )
            return None
        err_msg = f"🚨 [{name}] {base}: {reason} - Close fehlgeschlagen — {result.message}"
        print(err_msg); tg(err_msg)
        return None
    # Echter Fill bestätigt
    fill_price = float(result.price or 0)
    if fill_price <= 0:
        fill = _last_live_fill(trader, symbol, entry_ts_ms)
        if fill and fill["price"] > 0:
            fill_price = fill["price"]
        else:
            return None
    return _emit_exit(
        mode, name, reason, level, close_pct, info, entry_price,
        symbol, base, bias, fill_price, rsi,
        account_before, trader,
    )


def _log_trade(event: str, **kwargs):
    """Trade-Event in JSONL loggen — inkl. Markt-Sentiment-Kontext.

    Hängt das (log-only) Market-Sentiment aus market_sentiment.json an
    jeden Trade-Eintrag an. Exit-Einträge tragen zusätzlich den Entry-
    Kontext (signal_count, EMA-Distanz) für die spätere Auswertung.
    Ist keine frische Sentiment-Datei vorhanden, wird der Kontext einfach
    weggelassen (fail-soft, kein Filter).
    """
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    sentiment = _load_market_sentiment()
    if sentiment:
        entry["market_sentiment"] = sentiment.get("sentiment", "UNKNOWN")
        entry["market_breadth"] = sentiment.get("detail", "")
    if event == "exit" and _entry_meta:
        entry.setdefault("signal_count", _entry_meta.get("signal_count"))
        entry.setdefault("dist_pct", _entry_meta.get("dist_pct"))
        entry.setdefault("regime", _entry_meta.get("regime") or {})
        entry.setdefault("entry_vol_ratio", _entry_meta.get("vol_ratio"))
    with open(TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _log_debug(cycle: int, base: str, bias: str, state: str, reason: str, **kwargs):
    """Pro Polling-Cycle: was hat der Coin gemacht, warum kein Entry.
    Kein Telegram — nur File-Log für Post-Mortem-Analyse."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle": cycle, "base": base, "bias": bias,
        "state": state, "reason": reason,
        **kwargs,
    }
    with open(DEBUG_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _account_str(trader) -> str:
    """Konto-Stand als Telegram-Zeile: USD-Äquivalent (EUR+USD) + EUR."""
    if trader is None or not trader.has_api_keys():
        return "   Konto:  n/a"
    state = trader.get_account_state()
    if state is None:
        return "   Konto:  Abruf-Fehler"
    return (
        f"   Konto:  {state['usd_total']:.2f} USD "
        f"(EUR {state['eur']:.2f} / USD {state['usd']:.2f})"
    )



def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ts_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def _pnl_pct(entry_price: float, price: float, bias: str) -> float:
    """Return directional PnL percentage for a LONG/SHORT trade."""
    if entry_price <= 0 or price <= 0:
        raise ValueError("Entry- und Exit-Preis müssen positiv sein")
    if bias == "LONG":
        return (price - entry_price) / entry_price * 100
    return (entry_price - price) / entry_price * 100


def _stop_fill_price(
    bias: str,
    stop_price: float,
    candle_open: float,
) -> float:
    """Use stop price, or the worse candle open for a gap through the stop."""
    if bias == "LONG":
        return min(stop_price, candle_open)
    return max(stop_price, candle_open)


def _profit_lock_price(
    bias: str,
    target_price: float,
    candle_open: float,
) -> float:
    """Use target price, or the favorable candle open for a gap through target."""
    if bias == "LONG":
        return max(target_price, candle_open)
    return min(target_price, candle_open)


def _resolve_dry_run_candle(
    *,
    bias: str,
    entry_price: float,
    initial_stop: float,
    active_stop: float,
    half_closed: bool,
    candle_open: float,
    candle_high: float,
    candle_low: float,
    profit_lock_pct: float | None = None,
) -> tuple[str, float] | None:
    """Resolve one closed candle without using its close as an execution fill.

    With OHLCV alone the intrabar path is unknowable. Before the profit lock,
    an initial stop therefore wins over a same-candle target hit (conservative
    and deterministic). Once half the position is closed, only the active stop
    is relevant.
    """
    lock_pct = EXIT["profit_lock_pct"] if profit_lock_pct is None else profit_lock_pct
    if bias == "LONG":
        stop_hit = candle_low <= active_stop
        target_price = entry_price * (1 + lock_pct / 100)
        target_hit = candle_high >= target_price
    else:
        stop_hit = candle_high >= active_stop
        target_price = entry_price * (1 - lock_pct / 100)
        target_hit = candle_low <= target_price

    if not half_closed and stop_hit:
        return "stop_loss", _stop_fill_price(bias, initial_stop, candle_open)
    if not half_closed and target_hit:
        return "profit_lock", _profit_lock_price(bias, target_price, candle_open)
    if half_closed and stop_hit:
        # Label: Break-Even-Level (entry ± 0.1%) vs. getrailter Stop
        be_level = entry_price * 1.001 if bias == "LONG" else entry_price * 0.999
        trailed = abs(active_stop - be_level) / be_level > 0.0005
        return ("trailing_stop" if trailed else "breakeven_stop",
                _stop_fill_price(bias, active_stop, candle_open))
    return None


def _check_btc_1m_trend() -> str:
    """
    BTC 1m Mikrotrend für Entry-Filter.
    Nutzt EMA5 auf letzten 5 1m-Kerzen von BTC/USD:USD.
    Returns "up" (steigend), "down" (fallend), oder "neutral".
    """
    import ccxt
    import numpy as np
    try:
        ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        candles = closed_ohlcv(
            ex.fetch_ohlcv("BTC/USD:USD", timeframe="1m", limit=10), "1m"
        )
        if len(candles) < 6:
            return "neutral"
        closes = np.array([c[4] for c in candles], dtype=float)
        # EMA5 — konsistente Serie über alle 10 Bars
        alpha = 2.0 / 6.0
        emas = [closes[0]]
        for v in closes[1:]:
            emas.append(alpha * v + (1 - alpha) * emas[-1])
        if emas[-1] > emas[-2] * 1.001:
            return "up"
        elif emas[-1] < emas[-2] * 0.999:
            return "down"
        return "neutral"
    except Exception:
        return "neutral"


def _sort_cycle_signals(cycle_signals: list) -> list:
    """Sortiert Entry-Signale eines Zyklus: Signalstärke zuerst, dann EMA-Distanz.

    Vorher sortierte reine EMA-Distanz (nähester Pullback gewann) und
    überschrieb damit die 3/3-vor-2/3-Bevorzugung aus der Kandidatenliste —
    so konnte ein schwacher 2/3 SHORT mit 0.1% Distanz einen starken 3/3 LONG
    mit 0.5% Distanz schlagen (belegt: ZRO SHORT 10.10 obwohl 5 LONGs).
    Jetzt: −signal_count primär (3/3 vor 2/3), EMA-Distanz nur Tie-Breaker.
    """
    return sorted(
        cycle_signals,
        key=lambda item: (-int(item[0].get("signal_count", 0) or 0),
                          float(item[1].distance_pct)),
    )


def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"
    lock = SessionLock(DATA_DIR / f"session_{session_key}.lock")
    if not lock.acquire():
        msg = f"⚠️ [{session_key.upper()}] Session läuft bereits — zweiter Start abgebrochen"
        print(msg); tg(msg)
        return
    try:
        _run_session(session_key)
    finally:
        lock.release()


def _run_session(session_key: str):
    session = SESSIONS[session_key]
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    name = session_key.upper()

    # Session times
    open_h, open_m = map(int, session["open"].split(":"))
    close_h, close_m = map(int, session["close"].split(":"))

    now = datetime.now(timezone.utc)
    open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    if close_dt <= open_dt:
        close_dt += timedelta(days=1)  # Midnight-Überlauf

    # ─── Wait for Open ──────────────────────────────────────────

    if now < open_dt:
        wait_m = int((open_dt - now).total_seconds() / 60)
        print(f"⏳ Warte {wait_m} Min bis Session-Open...")
        time.sleep((open_dt - now).total_seconds())

    print(f"⏳ [{name}] Session Start — warte 15 Min auf Bias...")

    # ─── Load Watchlist ──────────────────────────────────────────

    if not WATCHLIST_FILE.exists():
        msg = f"⚠️ [{name}] Keine Watchlist — Session skip"
        print(msg); tg(msg)
        return

    if not _state_file_fresh(WATCHLIST_FILE, WATCHLIST_MAX_AGE_SECONDS):
        msg = f"⚠️ [{name}] Watchlist veraltet (>45 Min) — Session skip"
        print(msg); tg(msg)
        return

    try:
        with open(WATCHLIST_FILE) as f:
            watchlist = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"⚠️ [{name}] Watchlist ungültig — {exc}"
        print(msg); tg(msg)
        return

    if not watchlist:
        msg = f"⚠️ [{name}] Watchlist leer — Session skip"
        print(msg); tg(msg)
        return

    bases = [w["base"] for w in watchlist]
    print(f"[{_ts_str()}] Watchlist: {len(watchlist)} Coins — {', '.join(bases)}")

    # ─── Phase 1: Bias (aus Cron-Ergebnis, KEINE Doppelberechnung) ───

    bias_file = BIAS_RESULT_FILE
    if not bias_file.exists():
        msg = f"⚠️ [{name}] Kein Bias-File — Cron lief nicht?"
        print(msg); tg(msg)
        return

    if not _state_file_fresh(bias_file, BIAS_MAX_AGE_SECONDS):
        msg = f"⚠️ [{name}] Bias-File veraltet (>15 Min) — Session skip"
        print(msg); tg(msg)
        return

    try:
        with open(bias_file) as f:
            bias_results = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"⚠️ [{name}] Bias-File ungültig — {exc}"
        print(msg); tg(msg)
        return

    if not _state_symbols_match(watchlist, bias_results):
        msg = f"⚠️ [{name}] Bias/Watchlist gehören nicht zum selben Run — Session skip"
        print(msg); tg(msg)
        return

    bias_written_ts = bias_file.stat().st_mtime
    entry_deadline = _entry_deadline(bias_file, close_dt)
    print(
        f"⏱ [{name}] Entry-Fenster bis {entry_deadline.strftime('%H:%M UTC')} "
        f"(max. {ENTRY_WINDOW_SECONDS // 60} Min nach Bias)"
    )

    candidates = [r for r in bias_results if r["bias"] in ("LONG", "SHORT")]

    # Bias Telegram
    bias_lines = [f"🧠 [{name}] Bias:"]
    for r in bias_results:
        icon = {"LONG": "🟢", "SHORT": "🔴", "NOISE": "⚪", "ERROR": "⚠️"}.get(r["bias"], "⚪")
        bias_lines.append(f"  {icon} {r['symbol'].split('/')[0]}: {r['bias']} | {r['reason']}")
    bias_lines.append(f"  → {len(candidates)} Kandidaten, {len(bias_results) - len(candidates)} verworfen")
    bias_msg = "\n".join(bias_lines)
    print(bias_msg)  # nur für Log — Bias-Cron sendet separat

    if not candidates:
        msg = f"⚠️ [{name}] Keine Bias-Kandidaten — Session beendet."
        print(msg); tg(msg)
        return

    # ─── Phase 2: Entry Polling ──────────────────────────────────

    # Candidate-Rotation: alle Kandidaten rotierend prüfen, nicht nur ersten
    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    trader = None
    if not DRY_RUN:
        from trader import KrakenTrader
        trader = KrakenTrader()
        try:
            open_positions = trader.get_open_positions(strict=True)
        except RuntimeError as exc:
            msg = f"⚠️ [{name}] Positionsprüfung fehlgeschlagen — {exc}"
            print(msg); tg(msg)
            return
        active_positions = [
            position for position in open_positions
            if float(position.get("contracts", 0) or 0) > 0
        ]
        if active_positions:
            msg = f"⚠️ [{name}] Bereits offene Kraken-Position — kein neuer Entry"
            print(msg); tg(msg)
            return
    entered = False
    entry_price = 0.0
    stop_loss = 0.0
    last_status_msg = 0

    # S/R vorab hart prüfen. Blockierte Kandidaten werden nie als Fallback
    # zurück in die Entry-Rotation aufgenommen.
    active_candidates, blocked_candidates = _partition_sr_candidates(candidates)
    for blocked_candidate, block_reason in blocked_candidates:
        print(f"🚫 {_base(blocked_candidate)}: S/R-Block — {block_reason}")

    # Priorität: 3/3 Signale zuerst, 2/3 als Fallback
    if active_candidates:
        priority = [c for c in active_candidates if c.get('signal_count', 0) >= 3]
        fallback = [c for c in active_candidates if c.get('signal_count', 0) == 2]
        if priority:
            print(f"   Priorität ({len(priority)}× 3/3): {', '.join(_base(c) for c in priority)}")
            print(f"   Fallback ({len(fallback)}× 2/3): {', '.join(_base(c) for c in fallback)}")
            active_candidates = priority + fallback  # 3/3 zuerst, dann 2/3
        else:
            print(f"   Kein 3/3 — alle {len(fallback)}× 2/3 aktiv")

    if not active_candidates:
        msg = f"⚠️ [{name}] Alle Kandidaten S/R-geblockt — Session beendet."
        print(msg); tg(msg)
        return

    # Transparente Bias-Rangfolge. Die laufende Entry-Prüfung bleibt dynamisch:
    # bei gleichzeitig gültigen Signalen entscheidet weiterhin die EMA-Distanz.
    active_candidates = deterministic_rank(active_candidates)
    print("   Deterministisches Bias-Ranking: " + " > ".join(
        f"{_base(c)} ({c['rank_score']:.2f}; {', '.join(c['rank_reasons'])})"
        for c in active_candidates
    ))

    bases = [_base(c) for c in active_candidates]
    print(f"👁 [{name}] Entry-Polling — {len(active_candidates)} Kandidaten: {', '.join(bases)}")
    print(f"   Rotiere alle 30s — erster mit Pullback gewinnt")

    cycle = 0
    while _now_ts() < int(entry_deadline.timestamp() * 1000) - 30_000:
        cycle += 1
        # Alle Kandidaten dieses Zyklus zuerst prüfen. Danach gewinnt der
        # aktuell EMA-nächste gültige Kandidat — die Bias-Stärke bleibt nur
        # über die stabile Kandidatenliste als Tie-Breaker erhalten.
        cycle_signals = []
        for cand in active_candidates:
            symbol = cand["symbol"]
            bias = cand["bias"]
            base = _base(cand)

            try:
                signal = entry_engine.check_entry(symbol, bias, current_step=1)

                # Debug-Log pro Coin pro Cycle (nur File, kein Telegram)
                state_name = signal.state.value if hasattr(signal.state, 'value') else str(signal.state)
                reason = ""
                if signal.state == EntryState.NO_ENTRY:
                    reason = signal.reasoning if signal.reasoning else "EMA-Position falsch"
                elif signal.state == EntryState.WAITING:
                    reason = f"EMA-Distanz {signal.distance_pct:.2f}% > 1.5% (3× Basis)"
                elif signal.state == EntryState.APPROACHING:
                    reason = f"EMA-Distanz {signal.distance_pct:.2f}% > dynamische Distanz"
                elif signal.state == EntryState.AT_EMA:
                    reason = f"An EMA ({signal.distance_pct:.2f}%) — warte auf Rejection"
                _log_debug(cycle, base, bias, state_name, reason,
                           price=round(signal.price, 6), ema20=round(signal.ema20, 6),
                           dist=round(signal.distance_pct, 3))
                cycle_signals.append((cand, signal))

                if signal.state == EntryState.AT_EMA:
                    now_sec = time.time()
                    if now_sec - last_status_msg > 300:
                        print(f"  [{_ts_str()}] {base} an EMA ({signal.distance_pct:.2f}%)...")
                        last_status_msg = now_sec
            except Exception as e:
                print(f"  {base}: Entry-Fehler — {e}")

        # Signalstärke zuerst (3/3 vor 2/3), EMA-Distanz nur als Tie-Breaker.
        # Vorher: reine Distanz-Sortierung ließ schwache Pullbacks starke
        # Signale schlagen (ZRO SHORT 10.10 bei 5 verfügbaren LONGs).
        cycle_signals = _sort_cycle_signals(cycle_signals)
        for cand, signal in cycle_signals:
            symbol = cand["symbol"]
            bias = cand["bias"]
            base = _base(cand)

            if signal.state == EntryState.ENTERED:
                # S/R kann sich seit dem Bias-Snapshot verschoben haben.
                # Deshalb direkt am tatsächlichen Entry-Preis erneut prüfen.
                entry_sr_blocked, entry_sr_reason, _ = check_sr_for_entry(
                    symbol, signal.entry_price, bias
                )
                if entry_sr_blocked:
                    print(f"  [{_ts_str()}] {base}: S/R am Entry blockiert — {entry_sr_reason}")
                    _log_debug(
                        cycle, base, bias, "sr_blocked_at_entry", entry_sr_reason,
                        price=signal.entry_price,
                    )
                    continue

                # ── BTC-Korrelations-Check ──
                btc_trend = _check_btc_1m_trend()
                btc_blocked = (bias == "LONG" and btc_trend == "down") or \
                              (bias == "SHORT" and btc_trend == "up")
                if btc_blocked:
                    print(f"  [{_ts_str()}] {base}: BTC 1m {'bearish' if bias == 'LONG' else 'bullish'} — skip")
                    continue

                vol_ratio = cand.get("session_vol_ratio", 0)
                entered = True
                entry_price = signal.entry_price
                stop_loss = signal.stop_loss

                if not DRY_RUN:
                    fill, live_stop, live_error = _execute_live_entry(
                        trader, symbol, bias, signal
                    )
                    if live_error:
                        error_msg = f"🚨 [{name}] {base}: {live_error}"
                        print(error_msg); tg(error_msg)
                    if fill is None:
                        entered = False
                        atomic_write_json(ENTRY_STATE_FILE, {
                            "entered": False, "symbol": symbol, "bias": bias,
                            "entry": 0, "sl": live_stop,
                            "time": datetime.now(timezone.utc).isoformat(),
                        })
                        continue
                    entry_price = fill.price
                    stop_loss = live_stop
                    if live_error:
                        # Emergency-Close ist fehlgeschlagen: keine zweite
                        # Position riskieren, State für manuelle Recovery halten.
                        atomic_write_json(ENTRY_STATE_FILE, {
                            "entered": True, "symbol": symbol, "bias": bias,
                            "entry": entry_price, "sl": stop_loss,
                            "time": datetime.now(timezone.utc).isoformat(),
                            "error": live_error,
                        })
                        return

                # Ziel aus echtem SL ableiten: 2R statt fixer 2%.
                lock_pct = profit_lock_pct_for(entry_price, stop_loss)
                sl_pct_actual = abs(entry_price - stop_loss) / entry_price * 100

                # Erst nach bestätigtem DRY_RUN-Entry bzw. geschütztem LIVE-Fill loggen.
                global _entry_meta
                _entry_meta = {
                    "signal_count": int(cand.get("signal_count", 0) or 0),
                    "dist_pct": round(float(signal.distance_pct), 4),
                    "regime": cand.get("regime") or {},
                    "vol_ratio": round(float(vol_ratio), 2),
                    "sl_pct": round(sl_pct_actual, 2),
                    "target_pct": round(lock_pct, 2),
                    "rr": round(lock_pct / sl_pct_actual, 2) if sl_pct_actual else None,
                    "bias_age_sec": round(time.time() - bias_written_ts),
                    "bar_close_pos": getattr(signal, "bar_close_pos", None),
                    "atr_pct": getattr(signal, "atr_pct", None),
                    "atr_ratio": getattr(signal, "atr_ratio", None),
                }
                _log_trade("entry", symbol=symbol, base=base, bias=bias,
                           price=entry_price, ema20=signal.ema20,
                           stop_loss=stop_loss, mode=mode, session=name,
                           vol_ratio=round(vol_ratio, 2),
                           signal_count=_entry_meta["signal_count"],
                           dist_pct=_entry_meta["dist_pct"],
                           regime=_entry_meta["regime"],
                           sl_pct=_entry_meta["sl_pct"],
                           target_pct=_entry_meta["target_pct"],
                           rr=_entry_meta["rr"],
                           bias_age_sec=_entry_meta["bias_age_sec"],
                           bar_close_pos=_entry_meta["bar_close_pos"],
                           atr_pct=_entry_meta["atr_pct"],
                           atr_ratio=_entry_meta["atr_ratio"])
                # Konto-Stand vor dem Trade + Entry-Zeitmarke (für echte Fills).
                account_before = _account_str(trader) if (trader is not None and not DRY_RUN) else None
                entry_ts_ms = _now_ts()
                entry_account_line = f"\n{account_before}" if account_before else ""
                entry_msg = (
                    f"🎯 {mode} ENTRY {bias} {base}\n"
                    f"   Price:  {entry_price:.8f}\n"
                    f"   EMA20:  {signal.ema20:.8f}\n"
                    f"   Dist:   {signal.distance_pct:.2f}%\n"
                    f"   Vol:    {vol_ratio:.2f}x\n"
                    f"   SL:     {stop_loss:.8f}  ({_entry_meta['sl_pct']:.2f}%)\n"
                    f"   Ziel:   {lock_pct:.2f}%  ({_entry_meta['rr']}R) → 50% sichern\n"
                    f"   Grund:  {signal.reasoning}"
                    f"{entry_account_line}"
                )
                print(entry_msg); tg(entry_msg)
                entry_engine.increment_step(symbol)

                atomic_write_json(ENTRY_STATE_FILE, {
                    "entered": entered, "symbol": symbol, "bias": bias,
                    "entry": entry_price, "sl": stop_loss,
                    "time": datetime.now(timezone.utc).isoformat(),
                })
                break  # inner loop (candidates)

        if entered:
            break

        time.sleep(30)

    # ─── Nach Entry-Polling ─────────────────────────────────────

    if not entered:
        # Summary-Log: letzter Status jedes Kandidaten
        summary_reasons = []
        for cand in active_candidates:
            try:
                sig = entry_engine.check_entry(cand["symbol"], cand["bias"])
                if sig.state == EntryState.WAITING:
                    summary_reasons.append(f"{_base(cand)}: {sig.distance_pct:.1f}% von EMA (zu weit)")
                elif sig.state == EntryState.APPROACHING:
                    summary_reasons.append(f"{_base(cand)}: {sig.distance_pct:.1f}% von EMA (nähert sich)")
                elif sig.state == EntryState.AT_EMA:
                    summary_reasons.append(f"{_base(cand)}: an EMA — keine Rejection")
                else:
                    summary_reasons.append(f"{_base(cand)}: {sig.state}")
            except Exception as e:
                summary_reasons.append(f"{_base(cand)}: Fehler — {e}")
        msg = f"⏱ [{name}] Kein Entry im Bias-Fenster — {len(active_candidates)} Coins geprüft, {cycle} Cycles\n"
        for r in summary_reasons:
            msg += f"   {r}\n"
        print(msg); tg(msg)
        _log_debug(cycle, "SESSION", "END", "NO_ENTRY", f"{len(active_candidates)} Coins, {cycle} Cycles",
                   candidates=[_base(c) for c in active_candidates])

    # ─── Phase 3: Watcher (mit Trailing-Stop) ────────────────────

    if not entered:
        msg = f"⚠️ [{name}] Kein Entry-Signal — Session beendet."
        print(msg); tg(msg)
        return

    print(f"[{_ts_str()}] Watcher aktiv — überwache {base} {bias}...")

    if DRY_RUN:
        # DRY RUN: simulierter 2-Stufen-Exit — identische Werte wie LIVE
        # (Profit-Lock EXIT['profit_lock_pct'], Trailing EXIT['trailing_pct'],
        #  Entry-SL unverändert. Kein Break-Even mehr — Trailing startet am Lock.)
        print(f"   DRY RUN: 2-Stufen-Exit simuliert — 50% bei +{lock_pct:.2f}% (2R-Lock), Rest Trailing {EXIT['trailing_pct']}% ab Lock")
        print(f"   Nächste Session: ~{close_dt.strftime('%H:%M')} UTC (Session-Ende)")
        last_pnl_msg = 0
        best_pnl = 0.0
        half_closed = False          # Stage 1: 50% bei +1% PnL
        remaining_stop = stop_loss   # Stop für den (verbleibenden) Teil
        total_pnl = 0.0              # gewichteter Gesamt-PnL über beide Teile
        last_processed_candle_ts = None  # jedes Candle-Event genau einmal

        while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
            try:
                price_data = entry_engine._fetch_1m(symbol, limit=3)
                if len(price_data) > 0:
                    candle = price_data[-1]
                    candle_ts = float(candle[0])
                    candle_open = float(candle[1])
                    candle_high = float(candle[2])
                    candle_low = float(candle[3])
                    current_px = float(candle[4])
                    pnl = _pnl_pct(entry_price, current_px, bias)
                    _log_debug(0, base, bias, "DRY_RUN", f"PnL {pnl:+.2f}% @ {current_px:.4f}",
                               price=current_px, pnl=round(pnl, 2))

                    # PnL-Update alle 5 Min
                    if pnl > best_pnl:
                        best_pnl = pnl
                    if time.time() - last_pnl_msg > 300:
                        print(f"   [{_ts_str()}] {base}: PnL {pnl:+.2f}% (best: {best_pnl:+.2f}%)")
                        last_pnl_msg = time.time()

                    # Jede geschlossene Candle nur einmal als Event auswerten,
                    # auch wenn der 20s-Poller sie mehrfach sieht.
                    is_new_candle = candle_ts != last_processed_candle_ts
                    if is_new_candle:
                        last_processed_candle_ts = candle_ts
                        event = _resolve_dry_run_candle(
                            bias=bias,
                            entry_price=entry_price,
                            initial_stop=stop_loss,
                            active_stop=remaining_stop if half_closed else stop_loss,
                            half_closed=half_closed,
                            candle_open=candle_open,
                            candle_high=candle_high,
                            candle_low=candle_low,
                            profit_lock_pct=lock_pct,
                        )
                        # Trailing für Rest-Hälfte ab Profit-Lock (der Live-Zweig
                        # macht das längst — die Sim hat es nie nachgebaut). Stop
                        # läuft dem Hoch (LONG) bzw. Tief (SHORT) je geschlossener
                        # Kerze hinterher; konservativ: Ratchet erst NACH der
                        # Stop-Prüfung derselben Kerze.
                        if half_closed and not event:
                            trail = EXIT["trailing_pct"] / 100
                            if bias == "LONG":
                                cand = candle_high * (1 - trail)
                                if cand > remaining_stop:
                                    remaining_stop = cand
                            else:
                                cand = candle_low * (1 + trail)
                                if cand < remaining_stop:
                                    remaining_stop = cand
                    else:
                        event = None

                    if event and event[0] == "profit_lock":
                        _, fill_price = event
                        half_closed = True
                        # Profit-Lock-Rest: kein Break-Even. Der Trailing-Stop
                        # startet direkt 0.5% hinter dem tatsächlichen Lock-Fill.
                        trail = EXIT["trailing_pct"] / 100
                        remaining_stop = (
                            fill_price * (1 - trail)
                            if bias == "LONG"
                            else fill_price * (1 + trail)
                        )
                        lock_pnl = _pnl_pct(entry_price, fill_price, bias)
                        total_pnl += lock_pnl * 0.5
                        lock_msg = (
                            f"📤 {mode} EXIT 50% {bias} {base}\n"
                            f"   Level:  1/3 — profit_lock (simuliert)\n"
                            f"   Entry:  ${entry_price:.8f}\n"
                            f"   Exit:   ${fill_price:.8f}\n"
                            f"   PnL:    🟢 {lock_pnl:+.2f}%\n"
                            f"   Ziel:   {lock_pct:.2f}% ({_entry_meta['rr']}R) erreicht\n"
                            f"   Info:   50% gesichert, Rest → Trailing ab Lock-Preis ({EXIT['trailing_pct']}%)"
                        )
                        print(lock_msg); tg(lock_msg)
                        _log_trade("exit", symbol=symbol, base=base, bias=bias,
                                   reason="profit_lock", close_pct=50,
                                   pnl_pct=round(lock_pnl, 2), price=fill_price,
                                   rsi=0, session=name,
                                   mfe_pct=round(best_pnl, 2))

                    elif event:
                        reason_label, fill_price = event
                        rest_pct = 50 if half_closed else 100
                        rest_pnl = _pnl_pct(entry_price, fill_price, bias)
                        total_pnl += rest_pnl * (rest_pct / 100)
                        pnl_icon = "🟢" if rest_pnl >= 0 else "🔴"
                        sl_msg = (
                            f"📤 {mode} EXIT {rest_pct}% {bias} {base}\n"
                            f"   Level:  2/3 — {reason_label} (simuliert)\n"
                            f"   Entry:  ${entry_price:.8f}\n"
                            f"   Exit:   ${fill_price:.8f}\n"
                            f"   PnL:    {pnl_icon} {rest_pnl:+.2f}% (Rest-Anteil)\n"
                            f"   Gesamt-PnL Trade: {total_pnl:+.2f}%\n"
                            f"   Info:   DRY RUN {('Trailing-Stop' if half_closed else 'SL')} getriggert"
                        )
                        print(sl_msg); tg(sl_msg)
                        _log_trade("exit", symbol=symbol, base=base, bias=bias,
                                   reason=reason_label, close_pct=rest_pct,
                                   pnl_pct=round(rest_pnl, 2), price=fill_price,
                                   rsi=0, session=name,
                                   mfe_pct=round(best_pnl, 2))
                        entered = False
                        break
            except Exception as exc:
                print(f"   DRY RUN Watcher-Fehler: {exc}")
            time.sleep(20)  # 20s statt 60s — fängt Break-Even/SL-Level vor großem Durchbruch

        # Session-End: falls (Teil-)Position noch offen
        if entered:
            try:
                price_data = entry_engine._fetch_1m(symbol, limit=3)
                if len(price_data) > 0:
                    exit_price_actual = float(price_data[-1][4])
                    pnl = _pnl_pct(entry_price, exit_price_actual, bias)
                else:
                    raise RuntimeError("keine geschlossene 1m-Candle verfügbar")
            except Exception as exc:
                print(f"   DRY RUN Session-End-Preis fehlgeschlagen: {exc}")
                pnl = 0.0
                exit_price_actual = entry_price

            rest_pct = 50 if half_closed else 100
            total_pnl += pnl * (rest_pct / 100)
            pnl_icon = "🟢" if pnl >= 0 else "🔴"
            exit_msg = (
                f"📤 {mode} EXIT {rest_pct}% {bias} {base}\n"
                f"   Level:  3/3 — session_end\n"
                f"   Entry:  ${entry_price:.8f}\n"
                f"   Exit:   ${exit_price_actual:.8f}\n"
                f"   PnL:    {pnl_icon} {pnl:+.2f}% (Rest-Anteil)\n"
                f"   Gesamt-PnL Trade: {total_pnl:+.2f}%\n"
                f"   Info:   Session-Ende (DRY RUN)"
            )
            print(exit_msg); tg(exit_msg)
            _log_trade("exit", symbol=symbol, base=base, bias=bias,
                       reason="session_end", close_pct=rest_pct, pnl_pct=round(pnl, 2),
                       price=exit_price_actual, rsi=0, session=name,
                       mfe_pct=round(best_pnl, 2))
            entered = False
    else:
        # LIVE: echter Watcher mit Exit-Engine
        from watcher import Watcher, TrackedPosition
        watcher = Watcher(trader)
        tracked = TrackedPosition(
            symbol=symbol, side=bias.lower(),
            entry_price=entry_price, stop_loss=stop_loss,
            size=0,
            steps=1, trailing_active=False, trailing_price=stop_loss,
            entry_time=datetime.now(timezone.utc).isoformat(),
            pattern_exit_done=False,
            profit_lock_pct=lock_pct,
        )
        watcher.add_position(tracked)

        # Mutable state — updated during watcher loop
        current_step = 1
        # Konto-Stand vor dem Trade (für Exit-Meldung)
        account_before = _account_str(trader) if (trader is not None and not DRY_RUN) else None

        while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
            try:
                # ── Echte Position prüfen: extern geschlossen (SL) erkennen ──
                if trader is not None and not DRY_RUN:
                    live_contracts = trader.get_position_contracts(symbol, bias.lower())
                    if live_contracts < 0:
                        # Abfrage fehlgeschlagen — konservativ weitermachen, kein Fake.
                        pass
                    elif live_contracts == 0 and tracked.pattern_exit_done:
                        # Position weg, obwohl wir noch Rest tracken → externer SL-Close.
                        fill = _last_live_fill(trader, symbol, entry_ts_ms)
                        if fill and fill["price"] > 0:
                            _emit_exit(
                                mode, name,
                                "stop_loss", "2/3", 100,
                                "Position extern geschlossen (SL) — Rest real gefillt",
                                entry_price, symbol, base, bias,
                                fill["price"], 0.0, account_before, trader,
                            )
                        entered = False
                        watcher.remove_position(symbol)
                        break

                sig = exit_engine.check(
                    symbol=symbol, side=bias.lower(),
                    entry_price=entry_price, stop_loss=tracked.stop_loss,
                    current_step=current_step,
                    trailing_active=tracked.trailing_active,
                    trailing_price=tracked.trailing_price,
                )

                if sig.reason != ExitReason.NONE:
                    pct = int(sig.close_pct * 100)

                    if sig.reason in (ExitReason.PATTERN, ExitReason.PROFIT_LOCK):
                        level = "1/3"
                        if sig.reason == ExitReason.PATTERN:
                            remaining_stop = entry_price
                            trailing_start = "Break-Even"
                        else:
                            trail = EXIT["trailing_pct"] / 100
                            # Anker: Live-Marktpreis statt veraltetem 1m-Candle-Close.
                            # Verhindert, dass der Lock-Stop unter/über dem Markt
                            # landet und sofort feuert (Guard in set_stop_loss fängt
                            # den Rest ab, aber richtiger Anker vermeidet die Korrektur).
                            live_price = sig.price
                            if trader is not None:
                                try:
                                    ticker = trader._get_exchange().fetch_ticker(symbol)
                                    mark = float(ticker.get("info", {}).get("markPrice", 0) or 0)
                                    if mark > 0:
                                        live_price = mark
                                except Exception:
                                    pass
                            remaining_stop = (
                                live_price * (1 - trail)
                                if bias == "LONG"
                                else live_price * (1 + trail)
                            )
                            trailing_start = f"Lock-Preis mit Trailing {EXIT['trailing_pct']}%"

                        # 50%-Close erst ausführen (unter bestehendem SL-Schutz),
                        # dann den ECHTEN 50%-Fill verwenden für die Meldung.
                        if trader is not None and not DRY_RUN:
                            status, error, partial_fill = _execute_live_partial_exit(
                                trader, symbol, bias, remaining_stop
                            )
                            if error:
                                error_msg = f"🚨 [{name}] {base}: {error}"
                                print(error_msg); tg(error_msg)
                            if status not in {"protected", "closed"}:
                                entered = False
                                watcher.remove_position(symbol)
                                break
                            # Der echte Fill des 50%-Closes (nicht der letzte Fill,
                            # der könnte schon der SL-Close der anderen Hälfte sein).
                            fill_price = partial_fill if partial_fill > 0 else sig.price
                            _emit_exit(
                                mode, name,
                                sig.reason.value, level, pct, sig.message,
                                entry_price, symbol, base, bias,
                                fill_price, sig.rsi, account_before, trader,
                            )
                        else:
                            # DRY_RUN / kein Trader: Signal-Preis als Anzeige.
                            _emit_exit(
                                mode, name,
                                sig.reason.value, level, pct, sig.message,
                                entry_price, symbol, base, bias,
                                sig.price, sig.rsi, account_before, trader,
                            )

                        tracked.stop_loss = remaining_stop
                        tracked.trailing_active = True
                        tracked.trailing_price = remaining_stop
                        tracked.pattern_exit_done = True
                        current_step = 2
                        watcher.add_position(tracked)
                        print(f"  → 50% geschlossen + {trailing_start} aktiv: {remaining_stop:.6f}")

                    elif sig.reason in (ExitReason.EMA_OVEREXTENDED, ExitReason.SR_REACHED,
                                         ExitReason.RSI_EXTREME, ExitReason.STOP_LOSS):
                        level = "2/3"
                        fill_price = _confirm_and_report(
                            mode, name,
                            sig.reason.value, level, pct, sig.message,
                            entry_price, symbol, base, bias, sig.rsi, entry_ts_ms,
                            account_before, trader,
                        )
                        entered = False
                        watcher.remove_position(symbol)
                        break

                else:
                    if tracked.trailing_active:
                        watcher.update_trailing(symbol, sig.price)
                        pos = watcher.get_position(symbol)
                        if pos:
                            tracked.trailing_price = pos.trailing_price
                        if watcher.check_trailing_hit(symbol, sig.price):
                            fill_price = _confirm_and_report(
                                mode, name,
                                "trailing_stop", "2/3", 100,
                                f"Trailing getriggert @ {tracked.trailing_price:.6f}",
                                entry_price, symbol, base, bias, 0.0, entry_ts_ms,
                                account_before, trader,
                            )
                            entered = False
                            watcher.remove_position(symbol)
                            break

            except Exception as e:
                print(f"  Watcher-Fehler: {e}")

            time.sleep(30)

        # LIVE Session-Ende
        if entered and trader is not None:
            results = watcher.close_all_session_end()
            # Rest-Position wurde geschlossen — als echten Exit mit Fill melden.
            fill = _last_live_fill(trader, symbol, entry_ts_ms)
            if fill and fill["price"] > 0:
                _emit_exit(
                    mode, name,
                    "session_end", "3/3", 100,
                    "Session-Ende — Position geschlossen",
                    entry_price, symbol, base, bias,
                    fill["price"], 0.0, account_before, trader,
                )
            entered = False

    # ─── Session End (beide Modi) ────────────────────────────────

    dry_label = " (1 Signal, dry run)" if entered and DRY_RUN else ""
    print(f"✅ [{name}] Session Ende — {_ts_str()}{dry_label}")
    tg(f"✅ [{name}] Session Ende — {_ts_str()}")

    ENTRY_STATE_FILE.unlink(missing_ok=True)
    entry_engine.reset_all()


if __name__ == "__main__":
    main()