#!/usr/bin/env python3
"""
graviton/main.py — Session Orchestrator
========================================
Steuert den gesamten Trading-Ablauf pro Session:

  1. 30 Min vor Session-Open: Scan → Watchlist (max 8 Coins)
  2. Session-Open: Bias-Analyse (15m) → LONG/SHORT/NOISE
  3. Während Session: Entry-Polling (1m EMA20 Pullback)
  4. Nach Entry: Watcher (Exit-Signale, Trailing)
  5. Session-Ende: Alle Positionen schließen

Sessions:
  NY:   13:00 Scan, 13:30 Open, 16:00 Close (UTC)
  Asia: 23:30 Scan, 00:00 Open, 02:00 Close (UTC)

Usage:
  python main.py              # Läuft kontinuierlich, wartet auf Sessions
  python main.py --once       # Einmaliger Run (aktuelle Session)
  python main.py --dry-run    # Ohne Live-Orders
"""

from __future__ import annotations

import sys
import time
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict

from config import CFG, SESSIONS, ACTIVE_SESSIONS
from scanner import KrakenScanner, ScanResult
from bias import BiasAnalyzer, BiasResult
from entry import EntryEngine, EntrySignal, EntryState, wait_for_entry
from sr_levels import check_sr_for_entry
from trader import KrakenTrader, TradeResult
from exit import ExitEngine, ExitSignal, ExitReason
from watcher import Watcher, TrackedPosition


# ─── Constants ─────────────────────────────────────────────────────

LOG_DIR = CFG.logs_dir
LOG_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str):
    print(f"[{_ts()}] {msg}")


def _parse_time(time_str: str) -> tuple[int, int]:
    """'13:30' → (13, 30)"""
    h, m = time_str.split(":")
    return int(h), int(m)


def _next_session_time(session_key: str) -> Optional[datetime]:
    """
    Berechnet den nächsten Session-Start (Scan-Zeit) für einen Session-Key.
    Returns None wenn Session nicht aktiv.
    """
    if session_key not in ACTIVE_SESSIONS:
        return None

    session = SESSIONS[session_key]
    scan_h, scan_m = _parse_time(session["scan"])
    now = _now()
    target = now.replace(hour=scan_h, minute=scan_m, second=0, microsecond=0)

    if target <= now:
        target += timedelta(days=1)

    # Weekend-Check: Samstag/Sonntag keine Session
    if target.weekday() >= 5:  # Samstag=5, Sonntag=6
        # Skip to Monday
        days_until_monday = 7 - target.weekday()
        target += timedelta(days=days_until_monday)

    return target


def _get_session_close_ts(session_key: str, scan_dt: datetime) -> int:
    """Berechnet Unix-Timestamp für Session-Close."""
    session = SESSIONS[session_key]
    close_h, close_m = _parse_time(session["close"])
    close_dt = scan_dt.replace(hour=close_h, minute=close_m, second=0)
    return int(close_dt.timestamp() * 1000)


def _get_session_open_ts(session_key: str, scan_dt: datetime) -> int:
    """Berechnet Unix-Timestamp für Session-Open."""
    session = SESSIONS[session_key]
    open_h, open_m = _parse_time(session["open"])
    open_dt = scan_dt.replace(hour=open_h, minute=open_m, second=0)
    return int(open_dt.timestamp() * 1000)


# ═══════════════════════════════════════════════════════════════════
# Session Runner
# ═══════════════════════════════════════════════════════════════════

class SessionRunner:
    """
    Führt eine komplette Trading-Session durch:
    Scan → Bias → Entry → Watcher → Exit
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.scanner = KrakenScanner()
        self.bias_analyzer = BiasAnalyzer()
        self.entry_engine = EntryEngine()
        self.trader = KrakenTrader() if not dry_run else None
        self.exit_engine = ExitEngine()
        self.watcher = Watcher(self.trader)

        self._running = False

    # ─── Phase 1: Scan ─────────────────────────────────────────

    def scan_phase(self) -> List[ScanResult]:
        """Scanner: 30 Min vor Open."""
        _log("Phase 1: SCAN — Suche Momentum-Coins...")
        watchlist = self.scanner.scan()

        if not watchlist:
            _log("  Keine Coins gefunden. Session wird übersprungen.")
            return []

        _log(f"  Watchlist ({len(watchlist)} Coins):")
        for coin in watchlist:
            arrow = "▲" if coin.change_24h_pct > 0 else "▼"
            _log(f"    {coin.base:<8} {arrow}{abs(coin.change_24h_pct):.1f}%  "
                 f"Vol:€{coin.volume_24h_eur:,.0f}  ${coin.price:.4f}")

        return watchlist

    # ─── Phase 2: Bias ─────────────────────────────────────────

    def bias_phase(self, watchlist: List[ScanResult], session_open_ts: int) -> List[BiasResult]:
        """Bias-Analyse: erste 15m nach Open."""
        _log("Phase 2: BIAS — Warte auf 15m-Kerzen...")

        # Warte bis genug Kerzen (ca. 17-18 Minuten nach Open)
        wait_seconds = 18 * 60
        _log(f"  Warte {wait_seconds // 60} Minuten auf 15m-Kerzen...")
        time.sleep(min(wait_seconds, 30))  # im echten Run 18min, für Tests 30s

        symbols = [r.symbol for r in watchlist]
        results = []

        for sym in symbols:
            try:
                result = self.bias_analyzer.analyze(sym, session_open_ts)
                results.append(result)
                status = "✓" if result.bias != "NOISE" else "✗"
                _log(f"  {status} {result.symbol}: {result.bias} — {result.reason}")
            except Exception as e:
                _log(f"  ✗ {sym}: ERROR — {e}")

        # Filter: nur LONG/SHORT
        candidates = [r for r in results if r.bias in ("LONG", "SHORT")]

        # Sort: höchster abs(change) zuerst (vom Scan)
        cand_symbols = {r.symbol for r in candidates}
        scan_map = {r.symbol: r for r in watchlist}
        candidates.sort(
            key=lambda r: abs(scan_map[r.symbol].change_24h_pct)
            if r.symbol in scan_map else 0,
            reverse=True,
        )

        _log(f"  Candidates: {len(candidates)} Coins mit Bias")
        return candidates

    # ─── Phase 3: Entry ────────────────────────────────────────

    def entry_phase(
        self,
        candidates: List[BiasResult],
        session_close_ts: int,
    ):
        """
        Entry-Polling: Wartet auf EMA20 Pullback + Rejection.
        Nur 1 Coin gleichzeitig (max_parallel_coins=1).
        """
        _log("Phase 3: ENTRY — Warte auf Pullback-Signale...")

        max_parallel = CFG.entry["max_parallel_coins"]
        active_count = 0

        for bias_result in candidates:
            if active_count >= max_parallel:
                _log(f"  Max parallel ({max_parallel}) erreicht, restliche Coins skipped.")
                break

            symbol = bias_result.symbol
            bias = bias_result.bias

            # S/R Check vor Entry
            scan_price = bias_result.session_open_price
            blocked, reason, sr = check_sr_for_entry(symbol, scan_price, bias)
            if blocked:
                _log(f"  ✗ {symbol}: S/R Block — {reason}")
                continue

            _log(f"  Warte auf {bias} Entry für {symbol}...")

            # Timeout = bis Session-Ende
            now_ts = int(_now().timestamp() * 1000)
            timeout = max(60, (session_close_ts - now_ts) / 1000 - 120)  # 2min vor close

            signal = wait_for_entry(
                symbol=symbol,
                bias=bias,
                timeout_seconds=int(timeout),
                poll_interval=5,
            )

            if signal is None or signal.state != EntryState.ENTERED:
                _log(f"  ✗ {symbol}: Kein Entry-Signal innerhalb Timeout")
                continue

            # Entry ausführen
            _log(f"  ▶ ENTRY {bias} {symbol} @ {signal.entry_price:.6f} "
                 f"SL:{signal.stop_loss:.6f}")

            if self.trader and not self.dry_run:
                result = self.trader.enter_position(
                    symbol=symbol,
                    side=bias.lower(),
                    step=signal.step,
                )
                if result.success:
                    self.trader.set_stop_loss(
                        symbol, bias.lower(), signal.stop_loss
                    )
                else:
                    _log(f"  ✗ Entry failed: {result.message}")
                    continue
            else:
                _log(f"  [DRY RUN] Entry {bias} {symbol} "
                     f"@ {signal.entry_price:.6f} SL:{signal.stop_loss:.6f}")

            # Position tracken
            tracked = TrackedPosition(
                symbol=symbol,
                side=bias.lower(),
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                size=0,  # wird vom trader gesetzt
                steps=signal.step,
                trailing_active=False,
                trailing_price=0,
                entry_time=signal.timestamp,
                pattern_exit_done=False,
            )
            self.watcher.add_position(tracked)
            self.entry_engine.increment_step(symbol)
            active_count += 1

    # ─── Phase 4: Watcher ──────────────────────────────────────

    def watch_phase(self, session_close_ts: int):
        """
        Pollt Exit-Signale bis Session-Ende.
        """
        _log("Phase 4: WATCH — Überwache Positionen...")

        poll_interval = 5  # Sekunden
        while _now().timestamp() * 1000 < session_close_ts - 60_000:  # 1min Puffer
            exits = self.watcher.poll()

            for symbol, signal in exits:
                _log(f"  Exit-Signal: {symbol} {signal.reason.value} → "
                     f"{int(signal.close_pct * 100)}% — {signal.message}")

                if self.trader and not self.dry_run:
                    pos = self.watcher.get_position(symbol)
                    side = pos.side if pos else "long"
                    self.trader.close_position(symbol, side, close_pct=signal.close_pct)

                    if signal.close_pct >= 0.99:
                        self.watcher.remove_position(symbol)
                        self.entry_engine.reset_coin(symbol)
                    else:
                        # Aktiviere Trailing für Rest
                        pos.trailing_active = True
                        pos.trailing_price = (
                            signal.price * (1 - CFG.exit["trailing_pct"] / 100)
                            if side == "long"
                            else signal.price * (1 + CFG.exit["trailing_pct"] / 100)
                        )
                        pos.pattern_exit_done = True

            # Summary
            if exits:
                _log(self.watcher.summary())

            time.sleep(poll_interval)

    # ─── Phase 5: Session End ──────────────────────────────────

    def session_end_phase(self):
        """Session-Ende: Alles schließen."""
        _log("Phase 5: SESSION END — Schließe alle Positionen...")
        results = self.watcher.close_all_session_end()
        for symbol, status in results:
            _log(f"  Closed {symbol}: {status}")
        self.entry_engine.reset_all()
        _log("Session beendet. ✓")

    # ─── Full Session ──────────────────────────────────────────

    def run_session(self, session_key: str):
        """
        Führt eine komplette Session durch.
        """
        session = SESSIONS[session_key]
        _log(f"{'═'*60}")
        _log(f"  SESSION START: {session_key.upper()} "
             f"({session['open']}–{session['close']} UTC)")
        _log(f"{'═'*60}")

        now = _now()
        scan_dt = now  # für Timestamp-Berechnung

        session_open_ts = _get_session_open_ts(session_key, scan_dt)
        session_close_ts = _get_session_close_ts(session_key, scan_dt)

        # Phase 1: Scan
        watchlist = self.scan_phase()
        if not watchlist:
            _log("Session übersprungen (keine Watchlist).")
            return

        # Phase 2: Bias
        candidates = self.bias_phase(watchlist, session_open_ts)
        if not candidates:
            _log("Keine Coins mit klarem Bias. Session beendet.")
            return

        # Phase 3: Entry
        self.entry_phase(candidates, session_close_ts)

        # Phase 4: Watch
        self.watch_phase(session_close_ts)

        # Phase 5: Session End
        self.session_end_phase()

    # ─── Continuous Mode ───────────────────────────────────────

    def run_continuous(self):
        """Läuft kontinuierlich und wartet auf nächste Session."""
        self._running = True
        _log("Graviton gestartet. Warte auf Sessions...")
        _log(f"  Aktive Sessions: {ACTIVE_SESSIONS}")
        _log(f"  Dry Run: {self.dry_run}")

        while self._running:
            now = _now()

            # Finde nächste Session
            next_session = None
            next_time = None
            for sk in ACTIVE_SESSIONS:
                t = _next_session_time(sk)
                if t and (next_time is None or t < next_time):
                    next_time = t
                    next_session = sk

            if next_session is None or next_time is None:
                _log("Keine aktiven Sessions. Exit.")
                break

            wait_seconds = (next_time - now).total_seconds()
            if wait_seconds > 0:
                _log(f"Nächste Session: {next_session.upper()} in "
                     f"{wait_seconds / 60:.0f} Minuten "
                     f"({next_time.strftime('%H:%M UTC')})")

                # Sleep bis Session-Start, aber checke alle 60s
                while wait_seconds > 0 and self._running:
                    sleep_time = min(60, wait_seconds)
                    time.sleep(sleep_time)
                    wait_seconds -= sleep_time

            if self._running:
                self.run_session(next_session)

    def stop(self):
        """Stoppt den Continuous-Mode."""
        self._running = False
        _log("Graviton wird gestoppt...")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    # Session-Key aus Args
    session_key = "ny"
    for sk in ("ny", "asia"):
        if f"--{sk}" in sys.argv:
            session_key = sk
            break

    runner = SessionRunner(dry_run=dry_run)

    # Signal Handler
    def _handle_signal(sig, frame):
        runner.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if once:
        _log(f"Einmaliger Run: {session_key.upper()}")
        runner.run_session(session_key)
    else:
        runner.run_continuous()


if __name__ == "__main__":
    main()