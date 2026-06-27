#!/usr/bin/env python3
"""
scripts/list_kraken_perps.py — Listet alle USD Perpetuals auf Kraken Futures
================================================================================
Nutzt CCXT public endpoint (kein API-Key nötig).
Gibt Symbol, Base, Min Size, Fees aus.

Usage:
  python scripts/list_kraken_perps.py
"""

import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner import list_kraken_perps


def main():
    perps = list_kraken_perps()
    print(f"\nKraken Futures — {len(perps)} USD Linear Perpetuals\n")
    print(f"{'#':<4} {'Base':<10} {'CCXT Symbol':<30} {'Min Size':>10} {'Taker %':>8} {'Maker %':>8}")
    print(f"{'─'*4} {'─'*10} {'─'*30} {'─'*10} {'─'*8} {'─'*8}")

    for i, p in enumerate(perps, 1):
        min_size = p.get("min_contract") or "—"
        taker = f"{p.get('taker_fee', 0) * 100:.2f}%" if p.get("taker_fee") else "—"
        maker = f"{p.get('maker_fee', 0) * 100:.2f}%" if p.get("maker_fee") else "—"
        print(f"{i:<4} {p['base']:<10} {p['symbol']:<30} {str(min_size):>10} {taker:>8} {maker:>8}")

    print(f"\n{len(perps)} Perps insgesamt.")
    print("\nDiese Liste kann als SYMBOLS in config.py verwendet werden.")


if __name__ == "__main__":
    main()