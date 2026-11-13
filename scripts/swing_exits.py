"""Exit-Wächter (DRY_RUN): prüft NUR die offene Position — kein Scan, keine Entries.

Läuft alle 30 Minuten im US-Fenster, damit Stop und TP nicht erst am nächsten
Tag auffallen. Ist nichts zu tun, gibt das Script NICHTS aus (Watchdog-Prinzip:
leerer Output = keine Telegram-Nachricht).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from swing import data, engine, state as state_mod
from swing.book import apply_decision


def main() -> int:
    now = datetime.now(timezone.utc)
    st = state_mod.load()
    pos = st.get("position")
    if not pos:
        return 0

    ex = data.exchange()
    perps = [p for p in data.stock_perps(ex) if p["base"] == pos["base"]]
    if not perps:
        print(f"GRAVITON EXIT — {pos['base']} ist nicht mehr im Universum. "
              f"Position bleibt offen, bitte manuell prüfen.")
        return 0

    loaded = engine.load_data(ex, perps, now)
    frames = loaded.get(pos["base"])
    if not frames:
        return 0

    view = engine.daily_view(frames["d1"], int(now.timestamp() * 1000), frames.get("last"))
    if view is None:
        return 0

    decision = engine.position_decision(pos, view, now)
    if decision["action"] == "HOLD":
        return 0

    applied = apply_decision(st, decision, now)
    state_mod.save(st)

    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    price = decision.get("price") or view["price"]
    lines = [f"GRAVITON EXIT — {local:%d.%m.%Y %H:%M} MEZ", "",
             f"POSITION: LONG {pos['base']} @ {pos['entry']:.2f} "
             f"(Stop {pos['stop']:.2f} | Ziel {pos['tp']:.2f})",
             f"AKTION: {applied} — {decision.get('reason')} @ {price:.2f}"]
    if applied == "PARTIAL":
        lines.append("50% realisiert. Rest läuft mit Stop auf Einstand weiter.")
    elif applied == "CLOSE":
        last = (st.get("closed") or [{}])[-1]
        lines.append(f"Trade geschlossen: {last.get('pnl_pct'):+.2f}%")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
