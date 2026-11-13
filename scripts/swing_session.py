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


def apply_decision(st: dict, decision: dict, now: datetime) -> str:
    act = decision["action"]
    if act == "ENTRY":
        pick = decision["pick"]
        st["position"] = {
            "base": pick["base"], "symbol": pick["symbol"], "side": "LONG",
            "entry": pick["entry"], "stop": pick["stop"], "tp": pick["target"],
            "rr": pick["rr"], "units": 1.0, "partial_done": False,
            "opened_at": now.isoformat(),
        }
        st["last_entry_date"] = now.date().isoformat()
        return "ENTRY"
    if act == "PARTIAL" and st.get("position"):
        p = st["position"]
        p["partial_done"] = True
        p["partial_price"] = decision.get("price") or p["tp"]
        p["units"] = 0.5
        p["stop"] = p["entry"]          # Breakeven-Stop für den Rest
        return "PARTIAL"
    if act == "CLOSE" and st.get("position"):
        p = st["position"]
        price = decision.get("price") or p["entry"]
        if p.get("partial_done"):
            half = (p.get("partial_price", p["entry"]) / p["entry"] - 1)
            rest = (price / p["entry"] - 1)
            pnl = 0.5 * half + 0.5 * rest
        else:
            pnl = (price / p["entry"] - 1)
        st.setdefault("closed", []).append({
            "base": p["base"], "side": p["side"], "entry": p["entry"],
            "exit": round(price, 4), "pnl_pct": round(pnl * 100, 2),
            "partial": bool(p.get("partial_done")),
            "reason": decision.get("reason"), "closed_at": now.isoformat(),
        })
        st["position"] = None
        return "CLOSE"
    return "HOLD"


def report(decision: dict, now: datetime) -> str:
    from collections import Counter

    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    lines = [f"GRAVITON SWING — {local:%d.%m.%Y %H:%M} MEZ   (dry run)", ""]

    pos = decision.get("position")
    if pos:
        extra = " | 50% realisiert, Stop auf Einstand" if pos.get("partial_done") else ""
        lines.append(f"POSITION: LONG {pos['base']} @ {pos['entry']:.2f} | "
                     f"Stop {pos['stop']:.2f} | Ziel {pos['tp']:.2f} | R:R {pos['rr']}{extra}")
    else:
        lines.append("POSITION: keine offen")

    act = decision["action"]
    headline = {"ENTRY": f"ENTRY: {decision.get('pick', {}).get('base', '')}",
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
        if engine.USE_H4_TRIGGER:
            lines.append(f"  {f.get('trigger', 0):3d}  davon mit 4H-Pullback-Trigger")
        lines.append(f"  {f.get('rr_ok', 0):3d}  davon mit Chance/Risiko >= 1,5")

    rejects = decision.get("rejects") or {}
    if rejects:
        lines += ["", f"WARUM ABGELEHNT ({len(rejects)} Titel):"]
        for reason, n in Counter(rejects.values()).most_common():
            syms = [b for b, r in rejects.items() if r == reason]
            shown = ", ".join(syms[:5]) + (" …" if len(syms) > 5 else "")
            lines.append(f"  {n:3d}x {reason} — {shown}")

    if decision.get("pick"):
        p = decision["pick"]
        lines += ["", f"  Kauf: {p['base']}  Entry {p['entry']:.2f}  Stop {p['stop']:.2f}  "
                      f"Ziel {p['target']:.2f}  R:R {p['rr']}  Trendstärke ADX {p['adx']}"]

    lines += ["", "R:R = Gewinn/Risiko. Es wird nur gekauft, wenn Ziel und Stop "
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
    print(f"\n(daten: {len(loaded)}/{len(perps)} Symbole geladen)")

    if args.apply:
        applied = apply_decision(st, decision, now)
        state_mod.save(st)
        print(f"Papierbuch: {applied} (dry_run=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
