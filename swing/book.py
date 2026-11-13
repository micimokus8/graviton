"""Papierbuch-Buchung: eine Stelle für Tages-Scan und Exit-Wächter."""
from __future__ import annotations

from datetime import datetime


def apply_decision(st: dict, decision: dict, now: datetime) -> str:
    """ENTRY/PARTIAL/CLOSE in das Papierbuch schreiben. Gibt die Aktion zurück."""
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
            half = p.get("partial_price", p["entry"]) / p["entry"] - 1
            rest = price / p["entry"] - 1
            pnl = 0.5 * half + 0.5 * rest
        else:
            pnl = price / p["entry"] - 1
        st.setdefault("closed", []).append({
            "base": p["base"], "side": p["side"], "entry": p["entry"],
            "exit": round(price, 4), "pnl_pct": round(pnl * 100, 2),
            "partial": bool(p.get("partial_done")),
            "reason": decision.get("reason"), "closed_at": now.isoformat(),
        })
        st["position"] = None
        return "CLOSE"

    return "HOLD"
