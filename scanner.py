#!/usr/bin/env python3
"""
graviton/scanner.py — Kraken Perpetual Screener
================================================
Scannt alle USD-denominierten Linear Perpetuals auf Kraken nach:
  - 24h Change: 4% – 18%
  - 24h Volume: > 750.000 EUR
  - Sortiert nach abs(Change) desc
  - Max 8 Coins auf der Watchlist

Nutzt CCXT (public endpoint, kein API-Key nötig).
"""

from __future__ import annotations

import ccxt
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from pathlib import Path

from config import CFG


# ═══════════════════════════════════════════════════════════════════
# Dataclass
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ScanResult:
    """Ein gescannter Coin."""
    symbol: str               # CCXT symbol: "BTC/USD:USD"
    kraken_id: str            # "PF_XBTUSD"
    base: str                 # "BTC"
    quote: str                # "USD"
    price: float = 0.0
    change_24h_pct: float = 0.0
    volume_24h_usd: float = 0.0
    volume_24h_eur: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    scanned_at: str = ""


# ═══════════════════════════════════════════════════════════════════
# Kraken Futures Scanner
# ═══════════════════════════════════════════════════════════════════

class KrakenScanner:
    """
    Scanner für Kraken Perpetuals via CCXT.
    Filter-Pipeline:
      1. Alle USD Linear Perps laden (CCXT load_markets)
      2. 24h Ticker fetchen (fetch_tickers batch)
      3. Filter: 4% ≤ |change| ≤ 18%
      4. Filter: Vol24h ≥ 750.000 EUR
      5. Sort: abs(change) desc, Top 8
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._eur_usd_rate: float = 1.08  # default, wird aktualisiert

    # ─── Exchange ──────────────────────────────────────────────

    def _get_exchange(self) -> ccxt.Exchange:
        """Lazy-init CCXT Kraken Futures."""
        if self._exchange is None:
            self._exchange = ccxt.krakenfutures({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            self._exchange.load_markets()
        return self._exchange

    # ─── EUR/USD Rate ──────────────────────────────────────────

    def _update_eur_usd(self):
        """Holt aktuellen EUR/USD Kurs via Kraken Spot (public)."""
        try:
            spot = ccxt.kraken({"enableRateLimit": True})
            ticker = spot.fetch_ticker("EUR/USD")
            if ticker and ticker.get("last"):
                self._eur_usd_rate = float(ticker["last"])
        except Exception:
            pass  # fallback to default

    # ─── Perp Markets ──────────────────────────────────────────

    def get_perp_markets(self) -> List[Dict]:
        """
        Returns all USD-denominated linear perpetuals on Kraken Futures.
        Filters out inverse contracts and non-USD.
        """
        ex = self._get_exchange()
        perps = []
        for symbol, market in ex.markets.items():
            if not market.get("linear"):
                continue
            if market.get("settle") != "USD":
                continue
            if market.get("type") != "swap":
                continue
            if not market.get("active", True):
                continue
            perps.append(market)
        return perps

    # ─── Main Scan ─────────────────────────────────────────────

    def scan(self) -> List[ScanResult]:
        """
        Führt den vollen Scan durch und gibt Watchlist zurück.
        """
        ex = self._get_exchange()
        self._update_eur_usd()

        markets = self.get_perp_markets()
        symbols = [m["symbol"] for m in markets]

        print(f"[Scanner] {len(symbols)} USD Perps geladen. Hole Ticker...")

        # Batch-Ticker — Kraken Futures unterstützt fetchTickers
        try:
            all_tickers = ex.fetch_tickers(symbols)
        except Exception as e:
            print(f"[Scanner] fetch_tickers fehlgeschlagen ({e}), falle zurück auf Einzelabruf")
            all_tickers = {}
            for sym in symbols:
                try:
                    all_tickers[sym] = ex.fetch_ticker(sym)
                except Exception:
                    continue

        results: List[ScanResult] = []
        cfg_scan = CFG.scan
        min_vol_eur = cfg_scan["min_volume_eur"]
        min_chg = cfg_scan["min_change_pct"]
        max_chg = cfg_scan["max_change_pct"]

        for market in markets:
            sym = market["symbol"]
            ticker = all_tickers.get(sym)
            if ticker is None:
                continue

            change_pct = float(ticker.get("percentage", 0) or 0)
            abs_change = abs(change_pct)

            # Change-Filter: 4% – 18%
            if abs_change < min_chg or abs_change > max_chg:
                continue

            # Volume: quoteVolume oder baseVolume * price
            vol_usd = float(ticker.get("quoteVolume", 0) or 0)
            if vol_usd <= 0:
                vol_usd = float(ticker.get("baseVolume", 0) or 0) * float(ticker.get("last", 0) or 0)

            vol_eur = vol_usd / self._eur_usd_rate
            if vol_eur < min_vol_eur:
                continue

            result = ScanResult(
                symbol=sym,
                kraken_id=market.get("id", ""),
                base=market.get("base", ""),
                quote=market.get("quote", ""),
                price=float(ticker.get("last", 0) or 0),
                change_24h_pct=round(change_pct, 2),
                volume_24h_usd=round(vol_usd, 2),
                volume_24h_eur=round(vol_eur, 2),
                high_24h=float(ticker.get("high", 0) or 0),
                low_24h=float(ticker.get("low", 0) or 0),
                bid=float(ticker.get("bid", 0) or 0),
                ask=float(ticker.get("ask", 0) or 0),
                scanned_at=datetime.now(timezone.utc).isoformat(),
            )
            results.append(result)

        # Sort: abs(change) desc
        results.sort(key=lambda r: abs(r.change_24h_pct), reverse=True)

        # Top N
        watchlist = results[:cfg_scan["max_watchlist"]]

        print(f"[Scanner] {len(results)} Coins nach Filter, {len(watchlist)} auf Watchlist.")
        return watchlist


# ═══════════════════════════════════════════════════════════════════
# Convenience
# ═══════════════════════════════════════════════════════════════════

def list_kraken_perps() -> List[Dict]:
    """
    Listet alle USD Linear Perpetuals auf Kraken — für die initiale Coin-Liste.
    Gibt dicts mit symbol, base, min_contract, etc. zurück.
    """
    scanner = KrakenScanner()
    markets = scanner.get_perp_markets()
    result = []
    for m in sorted(markets, key=lambda x: x["base"]):
        result.append({
            "symbol": m["symbol"],
            "base": m["base"],
            "quote": m["quote"],
            "min_contract": m.get("limits", {}).get("amount", {}).get("min"),
            "contract_size": m.get("contractSize", 1),
            "taker_fee": m.get("taker", 0),
            "maker_fee": m.get("maker", 0),
        })
    return result


def run_scan() -> List[ScanResult]:
    """Führt einen Scan durch und printed die Ergebnisse."""
    scanner = KrakenScanner()
    results = scanner.scan()
    print(f"\n{'═'*70}")
    print(f"  Graviton Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═'*70}")
    if not results:
        print("  Keine Coins gefunden die den Filter passen.")
        # Save empty watchlist
        _save_watchlist(results)
        return results

    print(f"  {'Coin':<10} {'Change':>8} {'Price':>10} {'Vol($)':>14} {'Bid':>10} {'Ask':>10}")
    print(f"  {'─'*10} {'─'*8} {'─'*10} {'─'*14} {'─'*10} {'─'*10}")
    for r in results:
        arrow = "▲" if r.change_24h_pct > 0 else "▼"
        print(f"  {r.base:<10} {arrow}{abs(r.change_24h_pct):>7.2f}% "
              f"${r.price:>9.4f} ${r.volume_24h_usd:>13,.0f} "
              f"${r.bid:>9.4f} ${r.ask:>9.4f}")
    print(f"{'═'*70}")

    # Save for pipeline
    _save_watchlist(results)
    return results


def _save_watchlist(results: List[ScanResult]):
    """Speichert Watchlist als JSON für Bias-Pipeline."""
    import json
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    watchlist = [
        {
            "symbol": r.symbol,
            "base": r.base,
            "price": r.price,
            "change_24h_pct": r.change_24h_pct,
            "volume_24h_usd": r.volume_24h_usd,
            "scanned_at": r.scanned_at,
        }
        for r in results
    ]
    with open(data_dir / "watchlist.json", "w") as f:
        json.dump(watchlist, f, indent=2)


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--list-perps":
        perps = list_kraken_perps()
        print(f"\nKraken Futures — {len(perps)} USD Perpetuals\n")
        print(f"{'Base':<8} {'Symbol':<24} {'Min Size':>10}")
        print(f"{'─'*8} {'─'*24} {'─'*10}")
        for p in perps:
            print(f"{p['base']:<8} {p['symbol']:<24} {str(p['min_contract']):>10}")
    else:
        run_scan()