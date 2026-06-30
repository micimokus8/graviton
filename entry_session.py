#!/usr/bin/env python3
"""
graviton/entry_session.py — Entry + Watcher (liest bias.json, kein Bias)
Läuft als Hintergrundprozess via graviton_session_ny.sh
"""
import sys, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG, SESSIONS, DRY_RUN
from entry import EntryEngine, EntryState
from exit import ExitEngine, ExitReason
from sr_levels import check_sr_for_entry
from watcher import Watcher, TrackedPosition
from telegram_sender import send as tg

DATA_DIR = Path(__file__).parent / "data"
TRADE_LOG = DATA_DIR / "trade_log.jsonl"

def _log(event, **kw):
    e = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    with open(TRADE_LOG, "a") as f: f.write(json.dumps(e) + "\n")

def _ts(): return datetime.now(timezone.utc).strftime("%H:%M UTC")
def _now(): return int(datetime.now(timezone.utc).timestamp() * 1000)

# ─── BTC Trend Check (kopiert aus session.py) ───
def _check_btc_1m_trend():
    import ccxt, numpy as np
    try:
        ex = ccxt.krakenfutures({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        candles = ex.fetch_ohlcv("BTC/USD:USD", timeframe="1m", limit=10)
        if not candles or len(candles) < 6: return "neutral"
        closes = np.array([c[4] for c in candles], dtype=float)
        alpha = 2.0 / 6.0
        emas = [closes[0]]
        for v in closes[1:]: emas.append(alpha * v + (1 - alpha) * emas[-1])
        if emas[-1] > emas[-2] * 1.001: return "up"
        if emas[-1] < emas[-2] * 0.999: return "down"
        return "neutral"
    except: return "neutral"

# ─── Main ───
def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "ny"
    session = SESSIONS[session_key]
    mode = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    name = session_key.upper()

    close_h, close_m = map(int, session["close"].split(":"))
    now = datetime.now(timezone.utc)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    if close_dt <= now: close_dt += timedelta(days=1)

    # Lade Bias-Ergebnisse (vom Bias-Cron)
    bias_file = DATA_DIR / "bias.json"
    if not bias_file.exists():
        print(f"⚠️ [{name}] Kein bias.json — Bias-Cron zuerst nötig"); return
    bias_data = json.load(open(bias_file))
    results = bias_data["results"]
    candidates = [r for r in results if r["bias"] in ("LONG", "SHORT")]

    # Bias-Ergebnisse anzeigen
    bias_lines = [f"🧠 [{name}] Bias:"]
    for r in results:
        icon = {"LONG": "🟢", "SHORT": "🔴", "NOISE": "⚪"}.get(r["bias"], "⚪")
        bias_lines.append(f"  {icon} {r['base']}: {r['bias']} | RSI {r['rsi']}")
    bias_lines.append(f"  → {len(candidates)} Kandidaten, {len(results)-len(candidates)} verworfen")
    bias_msg = "\n".join(bias_lines)
    print(bias_msg); tg(bias_msg)

    if not candidates:
        print(f"⚠️ [{name}] Keine Bias-Kandidaten — Session beendet.")
        tg(f"⚠️ [{name}] Keine Bias-Kandidaten — Session beendet."); return

    # ─── Entry Polling ───
    entry_engine = EntryEngine()
    exit_engine = ExitEngine()
    entered = False
    entry_price = stop_loss = 0.0
    symbol = bias = base = ""
    candidate_idx = 0

    while candidate_idx < len(candidates) and not entered:
        cand = candidates[candidate_idx]
        blocked, reason, _ = check_sr_for_entry(cand["symbol"], cand["price"], cand["bias"])
        if blocked:
            print(f"🚫 {cand['base']}: S/R-Block — {reason}")
            tg(f"🚫 [{name}] {cand['base']}: S/R-Block — {reason}")
            candidate_idx += 1; continue

        symbol = cand["symbol"]; bias = cand["bias"]; base = cand["base"]
        print(f"👁 [{name}] Entry-Polling {base} ({bias})")

        btc_block_count = 0
        while _now() < int(close_dt.timestamp() * 1000) - 30_000:
            try:
                signal = entry_engine.check_entry(symbol, bias, current_step=1)
                if signal.state == EntryState.ENTERED:
                    btc_trend = _check_btc_1m_trend()
                    btc_blocked = (bias == "LONG" and btc_trend == "down") or (bias == "SHORT" and btc_trend == "up")
                    if btc_blocked:
                        btc_block_count += 1
                        print(f"  [{_ts()}] {base}: BTC-Block (×{btc_block_count}/3)")
                        if btc_block_count >= 3:
                            tg(f"🔄 [{name}] {base}: 3× BTC-Block — Candidate-Rotation"); break
                        time.sleep(30); continue
                    btc_block_count = 0

                    entry_msg = f"🎯 {mode} ENTRY {bias} {base}\n   Price: {signal.entry_price:.6f}\n   EMA20: {signal.ema20:.6f}\n   SL: {signal.stop_loss:.6f}"
                    print(entry_msg); tg(entry_msg)
                    _log("entry", symbol=symbol, base=base, bias=bias, price=signal.entry_price, ema20=signal.ema20, stop_loss=signal.stop_loss)

                    entered = True; entry_price = signal.entry_price; stop_loss = signal.stop_loss
                    entry_engine.increment_step(symbol)

                    if not DRY_RUN:
                        from trader import KrakenTrader
                        t = KrakenTrader(); fill = t.open_position(symbol, bias.lower())
                        if fill.success: t.set_stop_loss(symbol, bias.lower(), stop_loss, fill.size); entry_price = fill.price
                        else: print(f"LIVE ERROR: {fill.message}"); entered = False
                    break
            except Exception as e:
                print(f"Entry-Fehler: {e}")
            time.sleep(30)
        if entered: break
        candidate_idx += 1

    if not entered:
        print(f"⏱ [{name}] Kein Entry-Signal"); tg(f"⏱ [{name}] Kein Entry-Signal"); return

    # ─── Watcher ───
    print(f"[{_ts()}] Watcher aktiv — {base} {bias}...")
    watcher = Watcher()
    tracked = TrackedPosition(symbol=symbol, side=bias.lower(), entry_price=entry_price, stop_loss=stop_loss,
                              size=0, steps=1, trailing_active=False, trailing_price=stop_loss,
                              entry_time=now.isoformat(), pattern_exit_done=False)
    watcher.add_position(tracked)
    current_step = 1

    while _now() < int(close_dt.timestamp() * 1000) - 30_000:
        try:
            sig = exit_engine.check(symbol=symbol, side=bias.lower(), entry_price=entry_price,
                                    stop_loss=tracked.stop_loss, current_step=current_step,
                                    trailing_active=tracked.trailing_active, trailing_price=tracked.trailing_price)
            if sig.reason != ExitReason.NONE:
                pct = int(sig.close_pct * 100)
                pnl = ((sig.price - entry_price) / entry_price * 100) if bias == "LONG" else ((entry_price - sig.price) / entry_price * 100)
                _log("exit", symbol=symbol, base=base, bias=bias, reason=sig.reason.value, close_pct=pct, pnl_pct=round(pnl, 2), price=sig.price, rsi=sig.rsi)

                if sig.reason in (ExitReason.PATTERN, ExitReason.PROFIT_LOCK):
                    exit_msg = f"📤 {mode} EXIT {pct}% {bias} {base}\n   Level: 1/3 — {sig.reason.value}\n   Preis: {sig.price:.6f}\n   PnL: {pnl:+.2f}% | RSI: {sig.rsi}\n   → Rest mit Trailing"
                    print(exit_msg); tg(exit_msg)
                    tracked.stop_loss = entry_price; tracked.trailing_active = True
                    tracked.trailing_price = entry_price; tracked.pattern_exit_done = True
                    current_step = 2; watcher.add_position(tracked)
                    if not DRY_RUN:
                        from trader import KrakenTrader
                        t = KrakenTrader(); t.close_position(symbol, bias.lower(), close_pct=0.5)
                        t.set_stop_loss(symbol, bias.lower(), entry_price)
                else:
                    exit_msg = f"📤 {mode} EXIT {pct}% {bias} {base}\n   Level: 2/3 — {sig.reason.value}\n   Preis: {sig.price:.6f}\n   PnL: {pnl:+.2f}% | RSI: {sig.rsi}"
                    print(exit_msg); tg(exit_msg)
                    if not DRY_RUN:
                        from trader import KrakenTrader; KrakenTrader().close_position(symbol, bias.lower())
                    entered = False; watcher.remove_position(symbol); break
            else:
                if tracked.trailing_active:
                    watcher.update_trailing(symbol, sig.price)
                    pos = watcher.get_position(symbol)
                    if pos: tracked.trailing_price = pos.trailing_price
                    if watcher.check_trailing_hit(symbol, sig.price):
                        pnl = ((sig.price - entry_price) / entry_price * 100) if bias == "LONG" else ((entry_price - sig.price) / entry_price * 100)
                        msg = f"📤 {mode} EXIT 100% {bias} {base}\n   Level: 2/3 — Trailing Stop\n   Preis: {sig.price:.6f}\n   PnL: {pnl:+.2f}%"
                        print(msg); tg(msg)
                        _log("exit", symbol=symbol, base=base, bias=bias, reason="trailing_stop", close_pct=100, pnl_pct=round(pnl, 2))
                        if not DRY_RUN:
                            from trader import KrakenTrader; KrakenTrader().close_position(symbol, bias.lower())
                        entered = False; watcher.remove_position(symbol); break
        except Exception as e:
            print(f"Watcher-Fehler: {e}")
        time.sleep(30)

    # Session End
    if entered:
        _log("exit", symbol=symbol, base=base, bias=bias, reason="session_end", close_pct=100, pnl_pct=0)
        tg(f"📤 {mode} EXIT 100% {bias} {base}\n   Level: 3/3 — session_end")
        watcher.close_all_session_end()

    print(f"✅ [{name}] Session Ende — {_ts()}")
    tg(f"✅ [{name}] Session Ende — {_ts()}")

if __name__ == "__main__":
    main()
