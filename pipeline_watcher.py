#!/usr/bin/env python3
"""
graviton/pipeline_watcher.py — Watcher (Dry Run, Single-Pass)
===============================================================
Prüft EINMAL auf Exit-Signale für aktive Position.
Wird alle 60s via Cron aufgerufen.
Exit-Ergebnis wird in data/exit_state.json gespeichert.
"""

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, DRY_RUN
from exit import ExitEngine, ExitReason

DATA_DIR = Path(__file__).parent / "data"
ENTRY_STATE_FILE = DATA_DIR / "entry_state.json"
EXIT_STATE_FILE = DATA_DIR / "exit_state.json"


def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"

    # Already exited?
    if EXIT_STATE_FILE.exists():
        return  # silent — already closed

    if not ENTRY_STATE_FILE.exists():
        return  # no entry yet

    with open(ENTRY_STATE_FILE) as f:
        entry = json.load(f)

    if not entry.get("entered"):
        return

    symbol = entry["symbol"]
    side = entry["bias"].lower()
    entry_price = entry["entry"]
    stop_loss = entry.get("sl", 0)

    # Check session end
    session = SESSIONS[session_key]
    close_h, close_m = map(int, session["close"].split(":"))
    now = datetime.now(timezone.utc)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0)
    if now >= close_dt:
        msg = (f"{mode} SESSION END → 100% geschlossen\n"
               f"{'═'*35}\n"
               f"{symbol}: Position geschlossen (Session-Ende)")
        print(msg)
        with open(EXIT_STATE_FILE, "w") as f:
            json.dump({"reason": "session_end", "time": now.isoformat()}, f)
        return

    exit_engine = ExitEngine()
    try:
        sig = exit_engine.check(
            symbol=symbol, side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            current_step=1,
        )

        if sig.reason != ExitReason.NONE:
            pct = int(sig.close_pct * 100)
            msg = (f"{mode} EXIT {side.upper()} {symbol}\n"
                   f"{'═'*35}\n"
                   f"Grund:  {sig.reason.value}\n"
                   f"Close:  {pct}%\n"
                   f"Preis:  {sig.price:.6f}\n"
                   f"EMA20:  {sig.ema20:.6f}\n"
                   f"RSI:    {sig.rsi}\n"
                   f"Info:   {sig.message}")
            print(msg)

            with open(EXIT_STATE_FILE, "w") as f:
                json.dump({
                    "reason": sig.reason.value,
                    "close_pct": pct,
                    "price": sig.price,
                    "time": now.isoformat(),
                }, f)

    except Exception as e:
        print(f"[Watcher] Fehler {symbol}: {e}")


if __name__ == "__main__":
    main()