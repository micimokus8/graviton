#!/usr/bin/env python3
"""
graviton/pipeline_entry.py — Entry Check (Dry Run, Single-Pass)
=================================================================
Prüft EINMAL auf Entry-Signal. Wird alle 30s via Cron aufgerufen.
State wird in data/entry_state.json gespeichert.
"""

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, DRY_RUN
from entry import EntryEngine, EntryState

DATA_DIR = Path(__file__).parent / "data"
BIAS_RESULT_FILE = DATA_DIR / "bias_result.json"
ENTRY_STATE_FILE = DATA_DIR / "entry_state.json"


def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"

    # Check if already entered this session
    if ENTRY_STATE_FILE.exists():
        with open(ENTRY_STATE_FILE) as f:
            state = json.load(f)
        if state.get("entered"):
            return  # silent — already entered

    if not BIAS_RESULT_FILE.exists():
        return  # no bias yet

    with open(BIAS_RESULT_FILE) as f:
        bias_results = json.load(f)

    candidates = [r for r in bias_results if r["bias"] in ("LONG", "SHORT")]
    if not candidates:
        return

    candidate = candidates[0]
    symbol = candidate["symbol"]
    bias = candidate["bias"]

    engine = EntryEngine()
    try:
        signal = engine.check_entry(symbol, bias, current_step=1)

        if signal.state == EntryState.ENTERED:
            msg = (f"{mode} ENTRY {bias} {symbol}\n"
                   f"{'═'*40}\n"
                   f"Price:  {signal.entry_price:.6f}\n"
                   f"EMA20:  {signal.ema20:.6f}\n"
                   f"Dist:   {signal.distance_pct:.2f}%\n"
                   f"SL:     {signal.stop_loss:.6f}\n"
                   f"Bias:   {bias}")
            print(msg)

            # Save entry state
            ENTRY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ENTRY_STATE_FILE, "w") as f:
                json.dump({
                    "entered": True,
                    "symbol": symbol, "bias": bias,
                    "entry": signal.entry_price,
                    "sl": signal.stop_loss,
                    "time": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)

        elif signal.state == EntryState.AT_EMA:
            print(f"[Entry] {symbol} an EMA ({signal.distance_pct:.2f}%), warte auf Rejection...")

    except Exception as e:
        print(f"[Entry] Fehler {symbol}: {e}")


if __name__ == "__main__":
    main()