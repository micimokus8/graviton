#!/usr/bin/env python3
"""
graviton/pipeline_bias.py — Bias Phase (Dry Run)
=================================================
Liest letzte Watchlist aus data/watchlist.json,
führt Bias-Analyse durch (15m nach Session-Open),
gibt Ergebnisse als Telegram-formatierte Nachricht aus.
"""

import json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, ACTIVE_SESSIONS, DRY_RUN
from bias import BiasAnalyzer

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

WATCHLIST_FILE = DATA_DIR / "watchlist.json"
BIAS_RESULT_FILE = DATA_DIR / "bias_result.json"


def get_session_open_ts(session_key: str) -> int:
    session = SESSIONS[session_key]
    h, m = map(int, session["open"].split(":"))
    now = datetime.now(timezone.utc)
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if dt > now:
        dt -= timedelta(days=1)
    return int(dt.timestamp() * 1000)


def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"

    if session_key not in ACTIVE_SESSIONS:
        print(f"[Bias] Session {session_key} nicht aktiv. Skip.")
        return

    if not WATCHLIST_FILE.exists():
        print(f"[Bias] Keine Watchlist gefunden ({WATCHLIST_FILE}). Skip.")
        return

    with open(WATCHLIST_FILE) as f:
        watchlist = json.load(f)

    if not watchlist:
        print("[Bias] Watchlist leer. Skip.")
        return

    symbols = [w["symbol"] for w in watchlist]
    print(f"[Bias] Analysiere {len(symbols)} Coins...")

    session_open_ts = get_session_open_ts(session_key)
    analyzer = BiasAnalyzer()
    results = []

    for sym in symbols:
        try:
            r = analyzer.analyze(sym, session_open_ts)
            results.append({
                "symbol": r.symbol,
                "bias": r.bias,
                "green": r.green_candles,
                "red": r.red_candles,
                "rsi": r.rsi_15m,
                "price": r.session_open_price,
                "reason": r.reason,
            })
        except Exception as e:
            results.append({"symbol": sym, "bias": "ERROR", "reason": str(e)})

    # Save results
    with open(BIAS_RESULT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    # Format output
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    lines = [f"{mode} Graviton Bias — {session_key.upper()} Session",
             f"{'═'*45}"]

    candidates = [r for r in results if r["bias"] in ("LONG", "SHORT")]
    skipped = [r for r in results if r["bias"] not in ("LONG", "SHORT")]

    for r in candidates:
        arrow = "🟢" if r["bias"] == "LONG" else "🔴"
        lines.append(f"{arrow} {r['symbol']}: {r['bias']} | RSI {r['rsi']} | {r['reason']}")

    for r in skipped:
        lines.append(f"⚪ {r['symbol']}: {r['bias']} — {r['reason']}")

    lines.append(f"{'═'*45}")
    lines.append(f"{len(candidates)} Kandidaten, {len(skipped)} verworfen")

    print("\n".join(lines))

    if not candidates:
        print("[Bias] Keine Kandidaten mit klarem Bias. Session-Ende.")


if __name__ == "__main__":
    main()