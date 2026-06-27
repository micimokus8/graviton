#!/usr/bin/env python3
"""
graviton/watcher.py — Position Monitor & Trailing Stop
========================================================
Überwacht aktive Positionen und managed:
  - Trailing Stop (nach Pattern Exit 50%)
  - Exit-Signal Polling (via exit.py)
  - Position PnL Tracking
  - Session-End Check
"""

from __future__ import annotations
import time
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import CFG
from exit import ExitEngine, ExitSignal, ExitReason


@dataclass
class TrackedPosition:
    """Eine überwachte Position."""
    symbol: str
    side: str               # "long" / "short"
    entry_price: float
    stop_loss: float
    size: float             # contracts
    steps: int              # aktuelle Treppenstufe
    trailing_active: bool
    trailing_price: float   # aktueller Trailing Stop
    entry_time: str
    pattern_exit_done: bool  # 50% already closed via pattern
    pnl_pct: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# Watcher
# ═══════════════════════════════════════════════════════════════════

class Watcher:
    """
    Position-Watcher: pollt Exit-Signale und managed Trailing Stops.
    """

    def __init__(self, trader=None):
        self._exit_engine = ExitEngine()
        self._trader = trader  # KrakenTrader instance (für Live-Orders)
        self._positions: Dict[str, TrackedPosition] = {}
        self._trailing_pct = CFG.exit["trailing_pct"]

    # ─── Register ──────────────────────────────────────────────

    def add_position(self, pos: TrackedPosition):
        """Registriert eine neue Position zum Überwachen."""
        self._positions[pos.symbol] = pos
        print(f"[Watcher] Tracking {pos.symbol} {pos.side} "
              f"@ {pos.entry_price:.6f} (Step {pos.steps})")

    def remove_position(self, symbol: str):
        """Entfernt Position aus Tracking."""
        self._positions.pop(symbol, None)

    def get_position(self, symbol: str) -> Optional[TrackedPosition]:
        return self._positions.get(symbol)

    # ─── Trailing Update ───────────────────────────────────────

    def update_trailing(self, symbol: str, current_price: float):
        """
        Updated den Trailing Stop für eine Position.
        Nur relevant wenn trailing_active=True (nach Pattern Exit).
        """
        pos = self._positions.get(symbol)
        if pos is None or not pos.trailing_active:
            return

        if pos.side == "long":
            # Trailing zieht nach oben
            new_trail = current_price * (1 - self._trailing_pct / 100)
            if new_trail > pos.trailing_price:
                pos.trailing_price = new_trail
                print(f"[Watcher] {symbol} Trailing ↑ {new_trail:.6f}")
        else:  # short
            # Trailing zieht nach unten
            new_trail = current_price * (1 + self._trailing_pct / 100)
            if new_trail < pos.trailing_price:
                pos.trailing_price = new_trail
                print(f"[Watcher] {symbol} Trailing ↓ {new_trail:.6f}")

    def check_trailing_hit(self, symbol: str, current_price: float) -> bool:
        """Prüft ob Trailing Stop getriggert wurde."""
        pos = self._positions.get(symbol)
        if pos is None or not pos.trailing_active:
            return False

        if pos.side == "long" and current_price <= pos.trailing_price:
            return True
        if pos.side == "short" and current_price >= pos.trailing_price:
            return True
        return False

    # ─── Main Poll ─────────────────────────────────────────────

    def poll(self) -> List[Tuple[str, ExitSignal]]:
        """
        Pollt alle überwachten Positionen auf Exit-Signale.

        Returns:
            Liste von (symbol, ExitSignal) für Positionen die geschlossen
            werden müssen.
        """
        exits: List[Tuple[str, ExitSignal]] = []

        for symbol, pos in list(self._positions.items()):
            try:
                signal = self._exit_engine.check(
                    symbol=symbol,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    stop_loss=pos.stop_loss,
                    current_step=pos.steps,
                    trailing_active=pos.trailing_active,
                    trailing_price=pos.trailing_price,
                )

                if signal.reason != ExitReason.NONE:
                    exits.append((symbol, signal))
                    continue

                # Update Trailing
                self.update_trailing(symbol, signal.price)

                # Check Trailing Hit
                if self.check_trailing_hit(symbol, signal.price):
                    signal.reason = ExitReason.PATTERN  # Trailing ist Teil von Pattern Exit
                    signal.close_pct = 0.5  # Rest schließen
                    exits.append((symbol, signal))

            except Exception as e:
                print(f"[Watcher] Fehler bei {symbol}: {e}")

        return exits

    # ─── Session End ───────────────────────────────────────────

    def close_all_session_end(self) -> List[Tuple[str, str]]:
        """
        Schließt alle Positionen am Session-Ende.
        Returns Liste von (symbol, side).
        """
        results = []
        for symbol, pos in list(self._positions.items()):
            if self._trader and self._trader.has_api_keys():
                result = self._trader.close_position(pos.symbol, pos.side)
                status = "OK" if result.success else f"FAIL: {result.message}"
            else:
                status = "DRY RUN"
            results.append((symbol, status))
            print(f"[Watcher] Session End: Close {symbol} → {status}")

        self._positions.clear()
        return results

    # ─── Summary ───────────────────────────────────────────────

    def summary(self) -> str:
        """Gibt eine Summary aller überwachten Positionen."""
        if not self._positions:
            return "Keine aktiven Positionen."

        lines = [f"\n{'═'*60}",
                 f"  Position Summary — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
                 f"{'═'*60}"]
        for sym, pos in self._positions.items():
            trail_info = f"Trail:{pos.trailing_price:.4f}" if pos.trailing_active else "SL"
            lines.append(
                f"  {sym:<14} {pos.side.upper():<6} "
                f"Entry:{pos.entry_price:.4f} "
                f"{trail_info}:{pos.stop_loss:.4f} "
                f"Step:{pos.steps} PnL:{pos.pnl_pct:+.2f}%"
            )
        lines.append(f"{'═'*60}")
        return "\n".join(lines)