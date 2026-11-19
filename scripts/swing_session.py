"""Swing-Session (DRY_RUN): ein Scan, max. 1 Position, Bericht nach Telegram-Art.

Aufruf:
    PYTHONPATH=. .venv/bin/python scripts/swing_session.py            # nur Bericht
    PYTHONPATH=. .venv/bin/python scripts/swing_session.py --apply    # Papierbuch buchen
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from swing import data, engine, state as state_mod
from swing.book import apply_decision


def report(decision: dict, now: datetime) -> str:
    from collections import Counter

    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    lines = [f"GRAVITON SWING — {local:%d.%m.%Y %H:%M} MEZ   (dry run)", ""]

    pos = decision.get("position")
    if pos:
        extra = " | 50% realisiert, Stop auf Einstand" if pos.get("partial_done") else ""
        side = (pos.get("side") or "LONG").upper()
        lines.append(f"POSITION: {side} {pos['base']} @ {pos['entry']:.2f} | "
                     f"Stop {pos['stop']:.2f} | Ziel {pos['tp']:.2f} | R:R {pos['rr']}{extra}")
    else:
        lines.append("POSITION: keine offen")

    act = decision["action"]
    pick = decision.get("pick") or {}
    entry_txt = pick.get("base", "")
    if entry_txt and pick.get("side"):
        entry_txt = f"{entry_txt} ({pick['side']})"
    headline = {"ENTRY": f"ENTRY: {entry_txt}",
                "PARTIAL": "TEILVERKAUF: 50% raus, Stop auf Einstand",
                "CLOSE": "CLOSE", "HOLD": "HALTEN",
                "NONE": "KEINE AKTION"}.get(act, act)
    lines.append(f"ENTSCHEIDUNG: {headline}"
                 + (f" — {decision.get('reason')}" if decision.get("reason") else ""))

    f = decision.get("funnel") or {}
    if f:
        lines += ["", f"WIE VIELE DER {f.get('total', 0)} TITEL KOMMEN DURCH:"]
        lines.append(f"  {f.get('total', 0):3d}  gescannt")
        long_txt = f"  {f.get('long', 0):3d}  im Aufwärtstrend"
        if f.get("long_bases"):
            long_txt += f"  ({', '.join(f['long_bases'])})"
        lines.append(long_txt)
        if engine.config.ALLOW_SHORT:
            short_txt = f"  {f.get('short', 0):3d}  im Abwärtstrend"
            if f.get("short_bases"):
                short_txt += f"  ({', '.join(f['short_bases'])})"
            lines.append(short_txt)
        if engine.USE_H4_TRIGGER:
            lines.append(f"  {f.get('trigger', 0):3d}  davon mit 4H-Pullback-Trigger")
        lines.append(f"  {f.get('rr_ok', 0):3d}  davon mit Chance/Risiko >= 1,5")

    errors = decision.get("data_errors") or {}
    if errors:
        lines += ["", f"DATENFEHLER ({len(errors)} Titel) — kein Setup, Quelle prüfen:"]
        for base in sorted(errors):
            lines.append(f"  {base}: {errors[base]}")

    rejects = decision.get("rejects") or {}
    if rejects:
        lines += ["", f"WARUM ABGELEHNT ({len(rejects)} Titel):"]
        for reason, n in Counter(rejects.values()).most_common():
            syms = [b for b, r in rejects.items() if r == reason]
            shown = ", ".join(syms[:5]) + (" …" if len(syms) > 5 else "")
            lines.append(f"  {n:3d}x {reason} — {shown}")

    shorts = [c for c in (decision.get("candidates") or []) if c.get("side") == "SHORT"]
    if shorts:
        bits = ", ".join(f"{c['base']} (R:R {c['rr']}, ADX {c['adx']})" for c in shorts)
        lines += ["", f"Short-Kandidaten: {bits}"]

    if decision.get("pick"):
        p = decision["pick"]
        verb = "Short" if p.get("side") == "SHORT" else "Kauf"
        lines += ["", f"  {verb}: {p['base']}  Entry {p['entry']:.2f}  Stop {p['stop']:.2f}  "
                      f"Ziel {p['target']:.2f}  R:R {p['rr']}  Trendstärke ADX {p['adx']}"]

    lines += ["", "R:R = Gewinn/Risiko. Gehandelt wird nur, wenn Ziel und Stop "
                  "mindestens 1,5 auseinanderliegen."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Papierbuch schreiben")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    ex = data.exchange()
    perps = data.stock_perps(ex)
    loaded = engine.load_data(ex, perps, now)
    st = state_mod.load() or state_mod.new_state()

    decision = engine.scan_from_data(loaded, now, st)
    print(report(decision, now))
    n_err = sum(1 for v in loaded.values() if v.get("error"))
    print(f"\n(daten: {len(loaded) - n_err}/{len(perps)} Symbole geladen, "
          f"{n_err} Datenfehler)")

    if args.apply:
        applied = apply_decision(st, decision, now)
        state_mod.save(st)
        print(f"Papierbuch: {applied} (dry_run=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
