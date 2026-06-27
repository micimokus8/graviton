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

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, DRY_RUN
from bias import BiasAnalyzer
from entry import EntryEngine, EntryState
from exit import ExitEngine, ExitReason
from sr_levels import check_sr_for_entry
from telegram_sender import send as tg

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
    name = session_key.upper()

    # Session times
    open_h, open_m = map(int, session["open"].split(":"))
    close_h, close_m = map(int, session["close"].split(":"))

    now = datetime.now(timezone.utc)
    open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

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

    with open(WATCHLIST_FILE) as f:
        watchlist = json.load(f)

    if not watchlist:
        msg = f"⚠️ [{name}] Watchlist leer — Session skip"
        print(msg); tg(msg)
        return

    bases = [w["base"] for w in watchlist]
    print(f"[{_ts_str()}] Watchlist: {len(watchlist)} Coins — {', '.join(bases)}")

    # ─── Phase 1: Bias ───────────────────────────────────────────

    session_open_ts = int(open_dt.timestamp() * 1000)
    bias_time = open_dt + timedelta(minutes=16)

    wait_s = (bias_time - datetime.now(timezone.utc)).total_seconds()
    if wait_s > 0:
        time.sleep(wait_s)

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

    # Bias Telegram
    bias_lines = [f"🧠 [{name}] Bias:"]
    for r in bias_results:
        icon = {"LONG": "🟢", "SHORT": "🔴", "NOISE": "⚪", "ERROR": "⚠️"}.get(r["bias"], "⚪")
        bias_lines.append(f"  {icon} {r['base']}: {r['bias']} | RSI {r['rsi']}")
    bias_lines.append(f"  → {len(candidates)} Kandidaten, {len(bias_results) - len(candidates)} verworfen")
    bias_msg = "\n".join(bias_lines)
    print(bias_msg); tg(bias_msg)

    if not candidates:
        msg = f"⚠️ [{name}] Keine Bias-Kandidaten — Session beendet."
        print(msg); tg(msg)
        return

    # ─── Phase 2: Entry Polling ──────────────────────────────────

    # Iteriere Kandidaten, nimm ersten nicht-geblockten
    entry_candidate = None
    for cand in candidates:
        blocked, reason, sr = check_sr_for_entry(cand["symbol"], cand["price"], cand["bias"])
        if not blocked:
            entry_candidate = cand
            break
        else:
            print(f"🚫 {cand['base']}: S/R-Block — {reason}")
            tg(f"🚫 [{name}] {cand['base']}: S/R-Block — {reason}")

    if entry_candidate is None:
        msg = f"⚠️ [{name}] Alle Kandidaten durch S/R geblockt — Session beendet."
        print(msg); tg(msg)
        return

    symbol = entry_candidate["symbol"]
    bias = entry_candidate["bias"]
    base = entry_candidate["base"]

    msg = f"👁 [{name}] Entry-Polling {base} ({bias}) — alle 30s"
    print(msg); tg(msg)

    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    entered = False
    entry_price = 0.0
    stop_loss = 0.0
    last_ema_msg_time = 0
    last_far_msg_time = 0   # throttle "weit von EMA" Meldung

    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
        try:
            signal = entry_engine.check_entry(symbol, bias, current_step=1)

            if signal.state == EntryState.ENTERED:
                entry_msg = (
                    f"🎯 {mode} ENTRY {bias} {base}\n"
                    f"   Price:  {signal.entry_price:.6f}\n"
                    f"   EMA20:  {signal.ema20:.6f}\n"
                    f"   Dist:   {signal.distance_pct:.2f}%\n"
                    f"   SL:     {signal.stop_loss:.6f}"
                )
                print(entry_msg); tg(entry_msg)

                entered = True
                entry_price = signal.entry_price
                stop_loss = signal.stop_loss

                # Track step in engine
                entry_engine.increment_step(symbol)

                # LIVE: execute trade
                if not DRY_RUN:
                    from trader import KrakenTrader
                    trader = KrakenTrader()
                    fill = trader.open_position(symbol, bias.lower())
                    if fill.success:
                        trader.set_stop_loss(symbol, bias.lower(), stop_loss, fill.size)
                        entry_price = fill.price
                        print(f"  → LIVE {bias} {base} @ {fill.price:.6f} | SL: {stop_loss:.6f}")
                    else:
                        print(f"  → LIVE ERROR: {fill.message}")
                        entered = False

                with open(ENTRY_STATE_FILE, "w") as f:
                    json.dump({
                        "entered": entered, "symbol": symbol, "bias": bias,
                        "entry": entry_price, "sl": stop_loss,
                        "time": datetime.now(timezone.utc).isoformat(),
                    }, f, indent=2)
                break

            elif signal.state == EntryState.AT_EMA:
                now_sec = time.time()
                if now_sec - last_ema_msg_time > 300:
                    print(f"  [{_ts_str()}] {base} an EMA ({signal.distance_pct:.2f}%)...")
                    tg(f"⏳ [{name}] {base} an EMA20 ({signal.distance_pct:.2f}%) — warte auf Pullback")
                    last_ema_msg_time = now_sec

            elif signal.state in (EntryState.WAITING, EntryState.APPROACHING):
                now_sec = time.time()
                if now_sec - last_far_msg_time > 600:  # alle 10 Min
                    state_label = "weit entfernt" if signal.state == EntryState.WAITING else "nähert sich"
                    print(f"  [{_ts_str()}] {base}: {signal.distance_pct:.2f}% von EMA ({state_label})...")
                    tg(f"⏳ [{name}] {base}: {signal.distance_pct:.2f}% von EMA20 — {state_label}")
                    last_far_msg_time = now_sec

        except Exception as e:
            print(f"  Entry-Fehler: {e}")

        time.sleep(30)

    # Entry-Loop beendet (Session-Ende ohne Signal)
    if not entered:
        msg = f"⏱ [{name}] {base}: Kein Entry-Signal bis Session-Ende"
        print(msg); tg(msg)

    # ─── Phase 3: Watcher ────────────────────────────────────────

    if not entered:
        msg = f"⚠️ [{name}] Kein Entry-Signal — Session beendet."
        print(msg); tg(msg)
        return

    print(f"[{_ts_str()}] Watcher aktiv — überwache {base} {bias}...")

    # Mutable state — updated during watcher loop
    current_step = 1
    stop_loss_active = stop_loss

    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
        try:
            sig = exit_engine.check(
                symbol=symbol, side=bias.lower(),
                entry_price=entry_price, stop_loss=stop_loss_active,
                current_step=current_step,
            )

            if sig.reason != ExitReason.NONE:
                pct = int(sig.close_pct * 100)
                pnl = ((sig.price - entry_price) / entry_price * 100) if bias == "LONG" \
                      else ((entry_price - sig.price) / entry_price * 100)

                # Stufe 1: Pattern (50% close)
                if sig.reason == ExitReason.PATTERN:
                    level = "1/3"
                    exit_msg = (
                        f"📤 {mode} EXIT {pct}% {bias} {base}\n"
                        f"   Level:  {level} — {sig.reason.value}\n"
                        f"   Preis:  {sig.price:.6f}\n"
                        f"   PnL:    {pnl:+.2f}% | RSI: {sig.rsi}\n"
                        f"   Info:   {sig.message}\n"
                        f"   → Rest läuft mit SL auf Break-Even"
                    )
                    print(exit_msg); tg(exit_msg)

                    # Update state — SL auf Break-Even, nächste Stufe
                    stop_loss_active = entry_price
                    current_step = 2

                    if not DRY_RUN:
                        from trader import KrakenTrader
                        trader2 = KrakenTrader()
                        trader2.close_position(symbol, bias.lower(), close_pct=0.5)
                        trader2.set_stop_loss(symbol, bias.lower(), entry_price)
                        print(f"  → 50% geschlossen + SL auf Break-Even: {entry_price:.6f}")
                    else:
                        print(f"  DRY RUN: 50% Close + SL auf Break-Even ({entry_price:.6f})")

                # Stufe 2: Strukturell (100% close)
                elif sig.reason in (ExitReason.EMA_OVEREXTENDED, ExitReason.SR_REACHED,
                                     ExitReason.RSI_EXTREME, ExitReason.STOP_LOSS):
                    level = "2/3"
                    exit_msg = (
                        f"📤 {mode} EXIT {pct}% {bias} {base}\n"
                        f"   Level:  {level} — {sig.reason.value}\n"
                        f"   Preis:  {sig.price:.6f}\n"
                        f"   PnL:    {pnl:+.2f}% | RSI: {sig.rsi}\n"
                        f"   Info:   {sig.message}"
                    )
                    print(exit_msg); tg(exit_msg)

                    # LIVE close
                    if not DRY_RUN:
                        from trader import KrakenTrader
                        trader2 = KrakenTrader()
                        trader2.close_position(symbol, bias.lower())
                    entered = False  # Position komplett zu
                    break

        except Exception as e:
            print(f"  Watcher-Fehler: {e}")

        time.sleep(30)

    # ─── Phase 4: Session End ────────────────────────────────────

    end_msg = f"📤 {mode} EXIT 100% {bias} {base}\n   Level:  3/3 — session_end\n   Info:   Session-Ende → Zwangsschluss"

    if entered:
        print(end_msg); tg(end_msg)
        if not DRY_RUN:
            from trader import KrakenTrader
            trader = KrakenTrader()
            trader.close_position(symbol, bias.lower())

    print(f"✅ [{name}] Session Ende — {_ts_str()}" + (" (1 Signal, dry run)" if entered else ""))
    tg(f"✅ [{name}] Session Ende — {_ts_str()}")

    ENTRY_STATE_FILE.unlink(missing_ok=True)
    entry_engine.reset_all()


if __name__ == "__main__":
    main()