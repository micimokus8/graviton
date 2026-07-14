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
BIAS_RESULT_FILE = DATA_DIR / "bias_result.json"
ENTRY_STATE_FILE = DATA_DIR / "entry_state.json"
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"
DEBUG_LOG_FILE = DATA_DIR / "session_debug.jsonl"  # pro Polling-Cycle: Coin, Status, Grund




def _base(cand: dict) -> str:
    """Extrahiert Base aus Symbol ('CRV/USD:USD' → 'CRV')."""
    return cand.get("base") or cand["symbol"].split("/")[0]

def _log_trade(event: str, **kwargs):
    """Trade-Event in JSONL loggen."""
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
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

    # ─── Phase 1: Bias (aus Cron-Ergebnis, KEINE Doppelberechnung) ───

    bias_file = BIAS_RESULT_FILE
    if not bias_file.exists():
        msg = f"⚠️ [{name}] Kein Bias-File — Cron lief nicht?"
        print(msg); tg(msg)
        return

    with open(bias_file) as f:
        bias_results = json.load(f)

    candidates = [r for r in bias_results if r["bias"] in ("LONG", "SHORT")]

    # Bias Telegram
    bias_lines = [f"🧠 [{name}] Bias:"]
    for r in bias_results:
        icon = {"LONG": "🟢", "SHORT": "🔴", "NOISE": "⚪", "ERROR": "⚠️"}.get(r["bias"], "⚪")
        bias_lines.append(f"  {icon} {r['symbol'].split('/')[0]}: {r['bias']} | {r['reason']}")
    bias_lines.append(f"  → {len(candidates)} Kandidaten, {len(bias_results) - len(candidates)} verworfen")
    bias_msg = "\n".join(bias_lines)
    print(bias_msg); tg(bias_msg)

    if not candidates:
        msg = f"⚠️ [{name}] Keine Bias-Kandidaten — Session beendet."
        print(msg); tg(msg)
        return

    # ─── Phase 2: Entry Polling ──────────────────────────────────

    # Candidate-Rotation: alle Kandidaten rotierend prüfen, nicht nur ersten
    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    entered = False
    entry_price = 0.0
    stop_loss = 0.0
    last_status_msg = 0

    # S/R vorab prüfen — blockierte Kandidaten notieren, aber nicht hart ausschließen
    active_candidates = []
    blocked_candidates = []  # für Fallback wenn alle blockiert
    for cand in candidates:
        blocked, reason, sr = check_sr_for_entry(cand["symbol"], cand["price"], cand["bias"])
        if blocked:
            # Distanz zum S/R-Level berechnen für Fallback-Sortierung
            try:
                if cand["bias"] == "LONG":
                    res = sr.nearest_resistance(cand["price"])
                    sr_dist = (res - cand["price"]) / cand["price"] * 100 if res else 0
                else:
                    sup = sr.nearest_support(cand["price"])
                    sr_dist = (cand["price"] - sup) / cand["price"] * 100 if sup else 0
            except:
                sr_dist = 0
            print(f"🚫 {_base(cand)}: S/R-Block — {reason}")
            blocked_candidates.append((sr_dist, cand))
        else:
            active_candidates.append(cand)

    # Priorität: 3/3 Signale zuerst, 2/3 als Fallback
    if active_candidates:
        priority = [c for c in active_candidates if c.get('signal_count', 0) >= 3]
        fallback = [c for c in active_candidates if c.get('signal_count', 0) == 2]
        if priority:
            print(f"   Priorität ({len(priority)}× 3/3): {', '.join(c['base'] for c in priority)}")
            print(f"   Fallback ({len(fallback)}× 2/3): {', '.join(c['base'] for c in fallback)}")
            active_candidates = priority + fallback  # 3/3 zuerst, dann 2/3
        else:
            print(f"   Kein 3/3 — alle {len(fallback)}× 2/3 aktiv")

    # Fallback: wenn alle durch S/R blockiert → nimm den mit der größten Distanz
    if not active_candidates and blocked_candidates:
        blocked_candidates.sort(key=lambda x: -x[0])  # größte Distanz zuerst
        fallback = blocked_candidates[0][1]
        print(f"⚠️ Alle Kandidaten S/R-geblockt — Fallback: {fallback['base']} ({blocked_candidates[0][0]:.2f}% Distanz)")
        tg(f"⚠️ [{name}] Alle S/R-geblockt — Fallback {fallback['base']} ({blocked_candidates[0][0]:.2f}%)")
        active_candidates.append(fallback)

    if not active_candidates:
        msg = f"⚠️ [{name}] Alle Kandidaten S/R-geblockt — Session beendet."
        print(msg); tg(msg)
        return

    bases = [c["base"] for c in active_candidates]
    print(f"👁 [{name}] Entry-Polling — {len(active_candidates)} Kandidaten: {', '.join(bases)}")
    print(f"   Rotiere alle 30s — erster mit Pullback gewinnt")

    cycle = 0
    while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
        cycle += 1
        for cand in active_candidates:
            symbol = cand["symbol"]
            bias = cand["bias"]
            base = cand["base"]

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

                if signal.state == EntryState.ENTERED:
                    # ── BTC-Korrelations-Check ──
                    btc_trend = _check_btc_1m_trend()
                    btc_blocked = (bias == "LONG" and btc_trend == "down") or \
                                  (bias == "SHORT" and btc_trend == "up")
                    if btc_blocked:
                        print(f"  [{_ts_str()}] {base}: BTC 1m {'bearish' if bias == 'LONG' else 'bullish'} — skip")
                        continue

                    # ── Trade Log: ENTRY ──
                    _log_trade("entry", symbol=symbol, base=base, bias=bias,
                               price=signal.entry_price, ema20=signal.ema20,
                               stop_loss=signal.stop_loss, mode=mode, session=name)
                    entry_msg = (
                        f"🎯 {mode} ENTRY {bias} {base}\n"
                        f"   Price:  {signal.entry_price:.6f}\n"
                        f"   EMA20:  {signal.ema20:.6f}\n"
                        f"   Dist:   {signal.distance_pct:.2f}%\n"
                        f"   SL:     {signal.stop_loss:.6f}\n"
                        f"   Grund:  {signal.reasoning}"
                    )
                    print(entry_msg); tg(entry_msg)

                    entered = True
                    entry_price = signal.entry_price
                    stop_loss = signal.stop_loss
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
                    break  # inner loop (candidates)

                elif signal.state == EntryState.AT_EMA:
                    now_sec = time.time()
                    if now_sec - last_status_msg > 300:
                        print(f"  [{_ts_str()}] {base} an EMA ({signal.distance_pct:.2f}%)...")
                        last_status_msg = now_sec

                elif signal.state in (EntryState.WAITING, EntryState.APPROACHING):
                    pass  # kein Output — zu viel Spam bei 8 Coins

            except Exception as e:
                print(f"  {base}: Entry-Fehler — {e}")

            if entered:
                break  # outer while loop

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
        msg = f"⏱ [{name}] Kein Entry — {len(active_candidates)} Coins geprüft, {cycle} Cycles\n"
        for r in summary_reasons:
            msg += f"   {r}\n"
        print(msg); tg(msg)
        _log_debug(cycle, "SESSION", "END", "NO_ENTRY", f"{len(active_candidates)} Coins, {cycle} Cycles",
                   candidates=[c["base"] for c in active_candidates])

    # ─── Phase 3: Watcher (mit Trailing-Stop) ────────────────────

    if not entered:
        msg = f"⚠️ [{name}] Kein Entry-Signal — Session beendet."
        print(msg); tg(msg)
        return

    print(f"[{_ts_str()}] Watcher aktiv — überwache {base} {bias}...")

    if DRY_RUN:
        # DRY RUN: kein echter Watcher — nur bis Session-Ende warten
        print(f"   DRY RUN: Position simuliert — Exit/TP/SL werden nicht getrackt")
        print(f"   Nächste Session: ~{close_dt.strftime('%H:%M')} UTC (Session-Ende)")
        last_pnl_msg = 0
        best_pnl = 0.0
        exit_price_actual = entry_price
        while _now_ts() < int(close_dt.timestamp() * 1000) - 30_000:
            try:
                price_data = entry_engine._fetch_1m(symbol, limit=3)
                if len(price_data) > 0:
                    current_px = float(price_data[-1][4])
                    pnl = ((current_px - entry_price) / entry_price * 100) if bias == "LONG" \
                          else ((entry_price - current_px) / entry_price * 100)
                    _log_debug(0, base, bias, "DRY_RUN", f"PnL {pnl:+.2f}% @ {current_px:.4f}",
                               price=current_px, pnl=round(pnl, 2))
                    # Simulierter Profit Lock bei +1% und bestehendem Hoch
                    if pnl >= 1.0 and pnl > best_pnl:
                        best_pnl = pnl
                        exit_price_actual = current_px
                        if time.time() - last_pnl_msg > 300:  # alle 5 Min
                            print(f"   [{_ts_str()}] {base}: PnL {pnl:+.2f}% (best: {best_pnl:+.2f}%)")
                            last_pnl_msg = time.time()
                    # Simulierter SL: echten SL aus entry.py nutzen (0.75× 1H ATR)
                    sl_trigger = (current_px <= stop_loss) if bias == "LONG" else (current_px >= stop_loss)
                    if sl_trigger:
                        exit_price_actual = current_px
                        pnl_icon = "🔴"
                        sl_msg = (
                            f"📤 {mode} EXIT 100% {bias} {base}\n"
                            f"   Level:  2/3 — stop_loss (simuliert)\n"
                            f"   Entry:  ${entry_price:.6f}\n"
                            f"   Exit:   ${current_px:.6f}\n"
                            f"   PnL:    {pnl_icon} {pnl:+.2f}%\n"
                            f"   Info:   DRY RUN SL getriggert"
                        )
                        print(sl_msg); tg(sl_msg)
                        _log_trade("exit", symbol=symbol, base=base, bias=bias,
                                   reason="stop_loss", close_pct=100, pnl_pct=round(pnl, 2),
                                   price=current_px, rsi=0, session=name)
                        entered = False
                        break
            except:
                pass
            time.sleep(60)

        # Session-End: simulierten Exit senden falls noch offen
        if entered:
            try:
                price_data = entry_engine._fetch_1m(symbol, limit=3)
                if len(price_data) > 0:
                    exit_price_actual = float(price_data[-1][4])
                    pnl = ((exit_price_actual - entry_price) / entry_price * 100) if bias == "LONG" \
                          else ((entry_price - exit_price_actual) / entry_price * 100)
            except:
                pass
            pnl_icon = "🟢" if pnl >= 0 else "🔴"
            exit_msg = (
                f"📤 {mode} EXIT 100% {bias} {base}\n"
                f"   Level:  3/3 — session_end\n"
                f"   Entry:  ${entry_price:.6f}\n"
                f"   Exit:   ${exit_price_actual:.6f}\n"
                f"   PnL:    {pnl_icon} {pnl:+.2f}%\n"
                f"   Info:   Session-Ende (DRY RUN)"
            )
            print(exit_msg); tg(exit_msg)
            _log_trade("exit", symbol=symbol, base=base, bias=bias,
                       reason="session_end", close_pct=100, pnl_pct=round(pnl, 2),
                       price=exit_price_actual, rsi=0, session=name)
            entered = False
    else:
        # LIVE: echter Watcher mit Exit-Engine
        from watcher import Watcher, TrackedPosition
        watcher = Watcher()
        tracked = TrackedPosition(
            symbol=symbol, side=bias.lower(),
            entry_price=entry_price, stop_loss=stop_loss,
            size=0,
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
                    _log_trade("exit", symbol=symbol, base=base, bias=bias,
                               reason=sig.reason.value, close_pct=pct, pnl_pct=round(pnl, 2),
                               price=sig.price, rsi=sig.rsi, session=name)

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
                        tracked.stop_loss = entry_price
                        tracked.trailing_active = True
                        tracked.trailing_price = entry_price
                        tracked.pattern_exit_done = True
                        current_step = 2
                        watcher.add_position(tracked)
                        from trader import KrakenTrader
                        trader2 = KrakenTrader()
                        trader2.close_position(symbol, bias.lower(), close_pct=0.5)
                        trader2.set_stop_loss(symbol, bias.lower(), entry_price)
                        print(f"  → 50% geschlossen + SL auf Break-Even + Trailing aktiv: {entry_price:.6f}")

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
                        from trader import KrakenTrader
                        trader2 = KrakenTrader()
                        trader2.close_position(symbol, bias.lower())
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
                            from trader import KrakenTrader
                            trader2 = KrakenTrader()
                            trader2.close_position(symbol, bias.lower())
                            entered = False
                            watcher.remove_position(symbol)
                            break

            except Exception as e:
                print(f"  Watcher-Fehler: {e}")

            time.sleep(30)

        # LIVE Session-Ende
        if entered:
            _log_trade("exit", symbol=symbol, base=base, bias=bias,
                       reason="session_end", close_pct=100, pnl_pct=0,
                       price=0, rsi=0, session=name)
            end_msg = f"📤 {mode} EXIT 100% {bias} {base}\n   Level:  3/3 — session_end\n   Info:   Session-Ende → Zwangsschluss"
            print(end_msg); tg(end_msg)
            results = watcher.close_all_session_end()
            if not results:
                trader = KrakenTrader()
                trader.close_position(symbol, bias.lower())

    # ─── Session End (beide Modi) ────────────────────────────────

    dry_label = " (1 Signal, dry run)" if entered and DRY_RUN else ""
    print(f"✅ [{name}] Session Ende — {_ts_str()}{dry_label}")
    tg(f"✅ [{name}] Session Ende — {_ts_str()}")

    ENTRY_STATE_FILE.unlink(missing_ok=True)
    entry_engine.reset_all()


if __name__ == "__main__":
    main()