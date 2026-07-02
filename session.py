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
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"


def _log_trade(event: str, **kwargs):
    """Trade-Event in JSONL loggen."""
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    with open(TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")



def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ts_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


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
        candles = ex.fetch_ohlcv("BTC/USD:USD", timeframe="1m", limit=10)
        if not candles or len(candles) < 6:
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
    # Früherer Bias: 16 Min statt 31 Min (1 × 15m Kerze + 1 Min Puffer)
    # So haben wir 15 Minuten mehr Zeit für Entry-Polling
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
    # Candidate-Rotation: äußere Loop iteriert Kandidaten
    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    entered = False
    entry_price = 0.0
    stop_loss = 0.0
    candidate_idx = 0
    while candidate_idx < len(candidates) and not entered:
        cand = candidates[candidate_idx]
        blocked, reason, sr = check_sr_for_entry(cand["symbol"], cand["price"], cand["bias"])
        if blocked:
            print(f"🚫 {cand["base"]}: S/R-Block — {reason}")
            tg(f"🚫 [{name}] {cand["base"]}: S/R-Block — {reason}")
            candidate_idx += 1
            continue

        symbol = cand["symbol"]
        bias = cand["bias"]
        base = cand["base"]

        msg = f"👁 [{name}] Entry-Polling {base} ({bias})"
        print(msg)  # tg silenced — kein Spam pro Kandidat

        # Fast Mode: erste 30 Min nach Session-Open mit weiterer Entry-Distanz + schnellerem Polling
        fast_mode = datetime.now(timezone.utc) < open_dt + timedelta(minutes=30)

        last_ema_msg_time = 0
        last_far_msg_time = 0
        btc_block_count = 0
        poll_count = 0

        while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
            try:
                # Primo-Minuten: schneller poll (15s) für Sofort-Entry bei Börsenöffnung
                poll_count += 1
                signal = entry_engine.check_entry(symbol, bias, current_step=1, fast_mode=fast_mode)
                poll_interval = 15 if fast_mode else 30

                if signal.state == EntryState.ENTERED:
                    # ── BTC-Korrelations-Check mit Candidate-Rotation ──
                    btc_trend = _check_btc_1m_trend()
                    btc_blocked = (bias == "LONG" and btc_trend == "down") or \
                                  (bias == "SHORT" and btc_trend == "up")
                    if btc_blocked:
                        btc_block_count += 1
                        direction = "bearish" if bias == "LONG" else "bullish"
                        print(f"  [{_ts_str()}] {base}: BTC 1m {direction} (×{btc_block_count}/3)")
                        if btc_block_count >= 3:
                            tg(f"🔄 [{name}] {base}: 3× BTC-Block — Candidate-Rotation")
                            break
                        # silent — nur print
                        time.sleep(30)
                        continue
                    btc_block_count = 0

                    # ── Trade Log: ENTRY ──
                    _log_trade("entry", symbol=symbol, base=base, bias=bias,
                               price=signal.entry_price, ema20=signal.ema20,
                               stop_loss=signal.stop_loss, mode=mode, session=name)
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
                        # tg silenced — nur print
                        last_ema_msg_time = now_sec

                elif signal.state in (EntryState.WAITING, EntryState.APPROACHING):
                    now_sec = time.time()
                    if now_sec - last_far_msg_time > 600:  # alle 10 Min
                        state_label = "weit entfernt" if signal.state == EntryState.WAITING else "nähert sich"
                        print(f"  [{_ts_str()}] {base}: {signal.distance_pct:.2f}% von EMA ({state_label})...")
                        # tg silenced — nur print
                        last_far_msg_time = now_sec

            except Exception as e:
                print(f"  Entry-Fehler: {e}")

            time.sleep(poll_interval)
        candidate_idx += 1

    # ─── Nach Entry-Polling ─────────────────────────────────────

    if not entered:
        msg = f"⏱ [{name}] {base}: Kein Entry-Signal bis Session-Ende"
        print(msg); tg(msg)

    # ─── Phase 3: Watcher (mit Trailing-Stop) ────────────────────

    if not entered:
        msg = f"⚠️ [{name}] Kein Entry-Signal — Session beendet."
        print(msg); tg(msg)
        return

    print(f"[{_ts_str()}] Watcher aktiv — überwache {base} {bias}...")

    from watcher import Watcher, TrackedPosition
    watcher = Watcher()
    tracked = TrackedPosition(
        symbol=symbol, side=bias.lower(),
        entry_price=entry_price, stop_loss=stop_loss,
        size=0,  # Dry-Run: 0; Live: wird mit fill.size aus trader.open_position() befüllt
        steps=1, trailing_active=False, trailing_price=stop_loss,
        entry_time=datetime.now(timezone.utc).isoformat(),
        pattern_exit_done=False,
    )
    watcher.add_position(tracked)

    # Mutable state — updated during watcher loop
    current_step = 1

    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
        try:
            sig = exit_engine.check(
                symbol=symbol, side=bias.lower(),
                entry_price=entry_price, stop_loss=tracked.stop_loss,
                current_step=current_step,
                trailing_active=tracked.trailing_active,
                trailing_price=tracked.trailing_price,
            )

            if sig.reason != ExitReason.NONE:
                pct = int(sig.close_pct * 100)
                pnl = ((sig.price - entry_price) / entry_price * 100) if bias == "LONG" \
                      else ((entry_price - sig.price) / entry_price * 100)
                # ── Trade Log: EXIT ──
                _log_trade("exit", symbol=symbol, base=base, bias=bias,
                           reason=sig.reason.value, close_pct=pct, pnl_pct=round(pnl, 2),
                           price=sig.price, rsi=sig.rsi, session=name)

                # Stufe 1: Pattern ODER Profit Lock (50% close)
                if sig.reason in (ExitReason.PATTERN, ExitReason.PROFIT_LOCK):
                    level = "1/3"
                    exit_msg = (
                        f"📤 {mode} EXIT {pct}% {bias} {base}\n"
                        f"   Level:  {level} — {sig.reason.value}\n"
                        f"   Preis:  {sig.price:.6f}\n"
                        f"   PnL:    {pnl:+.2f}% | RSI: {sig.rsi}\n"
                        f"   Info:   {sig.message}\n"
                        f"   → Rest läuft mit SL auf Break-Even + Trailing"
                    )
                    print(exit_msg); tg(exit_msg)

                    # Update state — SL auf Break-Even, Trailing aktivieren, nächste Stufe
                    tracked.stop_loss = entry_price
                    tracked.trailing_active = True
                    tracked.trailing_price = entry_price  # Starttrail = Break-Even
                    tracked.pattern_exit_done = True
                    current_step = 2
                    watcher.add_position(tracked)  # Update im Watcher

                    if not DRY_RUN:
                        from trader import KrakenTrader
                        trader2 = KrakenTrader()
                        trader2.close_position(symbol, bias.lower(), close_pct=0.5)
                        trader2.set_stop_loss(symbol, bias.lower(), entry_price)
                        print(f"  → 50% geschlossen + SL auf Break-Even + Trailing aktiv: {entry_price:.6f}")
                    else:
                        print(f"  DRY RUN: 50% Close + SL auf Break-Even + Trailing ({entry_price:.6f})")

                # Stufe 2: Strukturell (100% close) — inkl. Trailing-Hit
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
                    watcher.remove_position(symbol)
                    break

            else:
                # Kein Exit → Trailing Stop updaten
                if tracked.trailing_active:
                    watcher.update_trailing(symbol, sig.price)
                    pos = watcher.get_position(symbol)
                    if pos:
                        tracked.trailing_price = pos.trailing_price
                    # Check ob Trailing getriggert
                    if watcher.check_trailing_hit(symbol, sig.price):
                        pnl = ((sig.price - entry_price) / entry_price * 100) if bias == "LONG" \
                              else ((entry_price - sig.price) / entry_price * 100)
                        trail_msg = (
                            f"📤 {mode} EXIT 100% {bias} {base}\n"
                            f"   Level:  2/3 — Trailing Stop\n"
                            f"   Preis:  {sig.price:.6f}\n"
                            f"   PnL:    {pnl:+.2f}%\n"
                            f"   Info:   Trailing getriggert @ {tracked.trailing_price:.6f}"
                        )
                        print(trail_msg); tg(trail_msg)
                        if not DRY_RUN:
                            from trader import KrakenTrader
                            trader2 = KrakenTrader()
                            trader2.close_position(symbol, bias.lower())
                        entered = False
                        watcher.remove_position(symbol)
                        break

        except Exception as e:
            print(f"  Watcher-Fehler: {e}")

        time.sleep(30)

    # ─── Phase 4: Session End ────────────────────────────────────

    end_msg = f"📤 {mode} EXIT 100% {bias} {base}\n   Level:  3/3 — session_end\n   Info:   Session-Ende → Zwangsschluss"

    if entered:
        _log_trade("exit", symbol=symbol, base=base, bias=bias,
                   reason="session_end", close_pct=100, pnl_pct=0,
                   price=0, rsi=0, session=name)
        print(end_msg); tg(end_msg)
        # Use Watcher's session-end close (mit Cleanup-Logging)
        results = watcher.close_all_session_end()
        if not results and not DRY_RUN:
            # Fallback wenn Watcher keine Position hat (z.B. Trader nicht gesetzt)
            from trader import KrakenTrader
            trader = KrakenTrader()
            trader.close_position(symbol, bias.lower())

    print(f"✅ [{name}] Session Ende — {_ts_str()}" + (" (1 Signal, dry run)" if entered else ""))
    tg(f"✅ [{name}] Session Ende — {_ts_str()}")

    ENTRY_STATE_FILE.unlink(missing_ok=True)
    entry_engine.reset_all()


if __name__ == "__main__":
    main()