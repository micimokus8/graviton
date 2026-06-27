#!/usr/bin/env python3
"""
graviton/session.py — Complete Session Runner
===============================================
Orchestriert Bias → Entry-Loop → Watcher → Session-Close.

Wird vom Cron um 13:30 UTC (NY) bzw 00:00 UTC (Asia) gestartet.
Läuft bis Session-Ende (max 2.5h).

Usage:
  python3 session.py ny      # NY Session
  python3 session.py asia    # Asia Session
"""

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, DRY_RUN
from scanner import KrakenScanner
from bias import BiasAnalyzer
from entry import EntryEngine, EntryState
from exit import ExitEngine, ExitReason
from sr_levels import check_sr_for_entry

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
ENTRY_STATE_FILE = DATA_DIR / "entry_state.json"


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ts_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"
    session = SESSIONS[session_key]
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"

    # Session times
    open_h, open_m = map(int, session["open"].split(":"))
    close_h, close_m = map(int, session["close"].split(":"))

    now = datetime.now(timezone.utc)
    open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    # If we're running before open, wait until open
    if now < open_dt:
        wait_s = (open_dt - now).total_seconds()
        print(f"{mode} Graviton {session_key.upper()} — Warte bis Session-Open ({wait_s/60:.0f} Min)...")
        time.sleep(min(wait_s, 300))  # max 5 min sleep at a time

    print(f"{mode} Graviton {session_key.upper()} Session gestartet — {_ts_str()}")
    print(f"{'═'*50}")

    # ─── Load Watchlist ──────────────────────────────────────────

    if not WATCHLIST_FILE.exists():
        print(f"[Session] Keine Watchlist ({WATCHLIST_FILE}). Skip.")
        return

    with open(WATCHLIST_FILE) as f:
        watchlist = json.load(f)

    if not watchlist:
        print(f"[Session] Watchlist leer. Session skip.")
        return

    symbols = [w["symbol"] for w in watchlist]
    print(f"[{_ts_str()}] Watchlist: {len(symbols)} Coins — {', '.join(w['base'] for w in watchlist)}")

    # ─── Phase 1: Bias (wait 15 min for candles) ─────────────────

    session_open_ts = int(open_dt.timestamp() * 1000)
    bias_time = open_dt + timedelta(minutes=16)  # 15 min + 1 min buffer

    wait_for_bias = (bias_time - datetime.now(timezone.utc)).total_seconds()
    if wait_for_bias > 0:
        print(f"[{_ts_str()}] Warte {wait_for_bias/60:.0f} Min auf 15m-Kerzen für Bias...")
        time.sleep(wait_for_bias)

    print(f"[{_ts_str()}] Bias-Analyse...")
    analyzer = BiasAnalyzer()
    bias_results = []

    for w in watchlist:
        try:
            r = analyzer.analyze(w["symbol"], session_open_ts)
            bias_results.append({
                "symbol": r.symbol, "base": w["base"],
                "bias": r.bias, "price": r.session_open_price,
                "rsi": r.rsi_15m, "reason": r.reason,
                "green": r.green_candles, "red": r.red_candles,
            })
        except Exception as e:
            print(f"  ✗ {w['base']}: Bias-Fehler — {e}")

    candidates = [r for r in bias_results if r["bias"] in ("LONG", "SHORT")]

    # Bias Output
    for r in bias_results:
        icon = {"LONG": "🟢", "SHORT": "🔴", "NOISE": "⚪", "ERROR": "⚠️"}.get(r["bias"], "⚪")
        print(f"  {icon} {r['base']}: {r['bias']} | RSI {r['rsi']} | {r['reason']}")

    print(f"{'─'*50}")
    print(f"[{_ts_str()}] {len(candidates)} Kandidaten, {len(bias_results) - len(candidates)} verworfen")

    if not candidates:
        print(f"[Session] Keine Bias-Kandidaten. Session beendet.")
        return

    # ─── Phase 2: Entry Polling ──────────────────────────────────

    candidate = candidates[0]  # max_parallel_coins=1
    symbol = candidate["symbol"]
    bias = candidate["bias"]
    base = candidate["base"]

    # S/R Check
    blocked, reason, sr = check_sr_for_entry(symbol, candidate["price"], bias)
    if blocked:
        print(f"[{_ts_str()}] ✗ {base}: S/R-Block — {reason}")
        return
    print(f"[{_ts_str()}] S/R OK für {base}")

    print(f"[{_ts_str()}] Entry-Polling {base} ({bias}) — alle 30s...")
    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    entered = False
    entry_price = 0.0
    stop_loss = 0.0

    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:  # 30s before close
        try:
            signal = entry_engine.check_entry(symbol, bias, current_step=1)

            if signal.state == EntryState.ENTERED:
                print(f"{'═'*50}")
                print(f"{mode} ENTRY {bias} {base}")
                print(f"  Price:  {signal.entry_price:.6f}")
                print(f"  EMA20:  {signal.ema20:.6f}")
                print(f"  Dist:   {signal.distance_pct:.2f}%")
                print(f"  SL:     {signal.stop_loss:.6f}")
                print(f"{'═'*50}")

                entered = True
                entry_price = signal.entry_price
                stop_loss = signal.stop_loss

                # Save entry state
                with open(ENTRY_STATE_FILE, "w") as f:
                    json.dump({
                        "entered": True, "symbol": symbol, "bias": bias,
                        "entry": entry_price, "sl": stop_loss,
                        "time": datetime.now(timezone.utc).isoformat(),
                    }, f, indent=2)
                break  # Exit entry polling, enter watcher phase

            elif signal.state == EntryState.AT_EMA:
                # Only print every 2 minutes to avoid spam
                pass  # silent — too much output during polling

        except Exception as e:
            print(f"  Entry-Fehler: {e}")

        time.sleep(30)

    # ─── Phase 3: Watcher ────────────────────────────────────────

    if not entered:
        print(f"[{_ts_str()}] Kein Entry-Signal in dieser Session.")
        return

    print(f"[{_ts_str()}] Watcher aktiv — überwache {base} {bias}...")

    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
        try:
            sig = exit_engine.check(
                symbol=symbol, side=bias.lower(),
                entry_price=entry_price, stop_loss=stop_loss,
                current_step=1,
            )

            if sig.reason != ExitReason.NONE:
                pct = int(sig.close_pct * 100)
                print(f"{'═'*50}")
                print(f"{mode} EXIT {bias} {base}")
                print(f"  Grund:  {sig.reason.value}")
                print(f"  Close:  {pct}%")
                print(f"  Preis:  {sig.price:.6f}")
                print(f"  RSI:    {sig.rsi}")
                print(f"  Info:   {sig.message}")
                print(f"{'═'*50}")

                if pct >= 100:
                    break  # Fully closed

        except Exception as e:
            print(f"  Watcher-Fehler: {e}")

        time.sleep(30)

    # ─── Phase 4: Session End ────────────────────────────────────

    print(f"{'═'*50}")
    print(f"{mode} {session_key.upper()} Session Ende — {_ts_str()}")
    if entered:
        print(f"  Position {base}: geschlossen (Session-Ende)")

    # Cleanup state
    ENTRY_STATE_FILE.unlink(missing_ok=True)
    print(f"{'═'*50}")


if __name__ == "__main__":
    main()