#!/usr/bin/env python3
"""
graviton/trader.py — CCXT Kraken Futures Order Execution
==========================================================
Platziert Market-Orders + Stop-Loss auf Kraken Perpetuals via CCXT.

SL-Setzung:
  - Bei Entry: Market-Order, dann SOFORT Stop-Loss nachlegen
  - Nachträglich: set_stop_loss() prüft ob schon SL existiert
  - Watcher: check_sl_exists() → set_stop_loss() wenn nötig

Kraken Futures API hat kein natives Position-SL. Stattdessen:
Stop-Order mit reduceOnly=True. Funktional identisch.
"""

from __future__ import annotations
import ccxt
import os
import time
from typing import Optional, Tuple, Dict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import CFG


# ═══════════════════════════════════════════════════════════════════
# .env laden
# ═══════════════════════════════════════════════════════════════════

_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)

KRAKEN_KEY = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET = os.getenv("KRAKEN_API_SECRET", "")


# ═══════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TradeResult:
    success: bool
    symbol: str
    side: str
    order_id: str
    price: float
    size: float
    cost: float
    stop_loss: float
    step: int
    message: str
    timestamp: str


# ═══════════════════════════════════════════════════════════════════
# Kraken Trader
# ═══════════════════════════════════════════════════════════════════

class KrakenTrader:

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._equity: Optional[float] = None

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            if not KRAKEN_KEY or not KRAKEN_SECRET:
                raise RuntimeError("KRAKEN_API_KEY + KRAKEN_API_SECRET fehlen!")
            self._exchange = ccxt.krakenfutures({
                "apiKey": KRAKEN_KEY,
                "secret": KRAKEN_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
        return self._exchange

    # ─── Account ───────────────────────────────────────────────

    def _fetch_eur_usd_rate(self) -> float:
        try:
            ticker = self._get_exchange().fetch_ticker("EUR/USD:USD")
            return float(ticker.get("last", 1.08) or 1.08)
        except Exception:
            return 1.08

    def get_equity(self) -> float:
        if self._equity is not None:
            return self._equity
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance({"type": "flex"})
            total_dict = balance.get("total", {})
            usd = float(total_dict.get("USD", 0) or 0)
            if usd > 0:
                self._equity = usd
                return self._equity
            eur = float(total_dict.get("EUR", 0) or 0)
            if eur > 0:
                self._equity = eur * self._fetch_eur_usd_rate()
                return self._equity
            self._equity = float(os.getenv("EQUITY_USD", "200"))
            return self._equity
        except Exception as e:
            print(f"[Trader] Balance-Fehler: {e}")
            return float(os.getenv("EQUITY_USD", "200"))

    def refresh_equity(self) -> float:
        self._equity = None
        return self.get_equity()

    # ─── Position Sizing ───────────────────────────────────────

    def calc_position_size(self) -> float:
        equity = self.get_equity()
        risk_pct = CFG.position["account_risk_pct_per_coin"] / 100
        return equity * risk_pct

    def get_position_size_contracts(self, symbol: str, usd_amount: float) -> float:
        try:
            ex = self._get_exchange()
            market = ex.market(symbol)
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker["last"])
            contracts = usd_amount / price
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 1)
            contracts = max(contracts, min_amount or 1)
            return contracts
        except Exception as e:
            print(f"[Trader] Kontraktgröße: {e}")
            return 0

    def set_leverage(self, symbol: str, leverage: int = 1) -> bool:
        try:
            self._get_exchange().set_leverage(leverage, symbol)
            return True
        except Exception as e:
            print(f"[Trader] Leverage: {e}")
            return False

    # ─── Entry Order ───────────────────────────────────────────

    def enter_position(
        self, symbol: str, side: str,
        size_usd: Optional[float] = None,
        stop_loss: float = 0.0,
        step: int = 1,
    ) -> TradeResult:
        """
        Öffnet Position via Market Order + setzt sofort Stop-Loss.
        """
        if not KRAKEN_KEY:
            return TradeResult(False, symbol, side, "", 0, 0, 0, stop_loss, step,
                             "DRY RUN", datetime.now(timezone.utc).isoformat())

        try:
            ex = self._get_exchange()
            ex.load_markets()
            self.set_leverage(symbol, CFG.exchange["leverage"])

            if size_usd is None:
                size_usd = self.calc_position_size()

            size_contracts = self.get_position_size_contracts(symbol, size_usd)
            if size_contracts <= 0:
                return TradeResult(False, symbol, side, "", 0, 0, size_usd, stop_loss, step,
                                 "Kontraktgröße ≤ 0", datetime.now(timezone.utc).isoformat())

            # Market Order
            if side == "long":
                order = ex.create_market_buy_order(symbol, size_contracts)
            else:
                order = ex.create_market_sell_order(symbol, size_contracts)

            order_id = str(order.get("id", ""))
            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            cost = float(order.get("cost", 0) or 0)

            # Auto-SL wenn Stop-Loss mitgegeben
            sl_actual = stop_loss
            if stop_loss <= 0:
                ticker = ex.fetch_ticker(symbol)
                price_now = float(ticker["last"])
                sl_offset = CFG.entry["sl_offset_pct"] / 100
                sl_actual = round(price_now * (1 + sl_offset) if side == "short" else price_now * (1 - sl_offset), 6)

            sl_ok = self.set_stop_loss(symbol, side, sl_actual, size_contracts)
            if sl_ok:
                print(f"[Trader] {side.upper()} {symbol}: {size_contracts} @ {avg_price:.6f} | SL: {sl_actual:.6f}")
            else:
                print(f"[Trader] {side.upper()} {symbol}: {size_contracts} @ {avg_price:.6f} | SL: FEHLER!")

            return TradeResult(True, symbol, side, order_id, avg_price or 0,
                             size_contracts, cost, sl_actual, step,
                             f"Entry {side.upper()}", datetime.now(timezone.utc).isoformat())

        except Exception as e:
            print(f"[Trader] Entry ERROR: {e}")
            return TradeResult(False, symbol, side, "", 0, 0, 0, 0, step,
                             str(e), datetime.now(timezone.utc).isoformat())

    # ─── Stop Loss ─────────────────────────────────────────────

    def has_stop_loss(self, symbol: str) -> bool:
        """Prüft ob Position bereits einen Stop-Loss hat."""
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    return True
            return False
        except Exception:
            return False

    def set_stop_loss(self, symbol: str, side: str, stop_price: float,
                      amount: Optional[float] = None) -> bool:
        """
        Setzt Stop-Loss via Stop-Market-Order mit reduceOnly.
        Entfernt vorherigen SL falls vorhanden.
        """
        if not KRAKEN_KEY:
            print(f"[Trader] DRY RUN: SL {symbol} {side} @ {stop_price}")
            return True

        try:
            ex = self._get_exchange()
            ex.load_markets()

            # Round to market price precision
            market = ex.market(symbol)
            price_precision = market.get("precision", {}).get("price", 0.0001)
            ndigits = abs(int(f"{price_precision:.10f}".split("e-")[-1])) if "e-" in f"{price_precision:.10f}" else len(str(price_precision).split(".")[-1]) if "." in str(price_precision) else 4
            sl_rounded = round(stop_price, ndigits)

            # Cancel existing SL orders
            try:
                open_orders = ex.fetch_open_orders(symbol)
                for o in open_orders:
                    if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                        ex.cancel_order(o["id"], symbol)
                        print(f"[Trader] Alten SL gecancelled: {o['id']}")
            except Exception:
                pass

            # Place new Stop-Market order
            stop_side = "buy" if side == "short" else "sell"

            # Get position size if not given
            if amount is None or amount <= 0:
                positions = ex.fetch_positions([symbol])
                for p in positions:
                    c = float(p.get("contracts", 0) or 0)
                    if c > 0:
                        amount = c
                        break

            if not amount or amount <= 0:
                print(f"[Trader] SL: Keine Position gefunden für {symbol}")
                return False

            order = ex.create_order(
                symbol, "stop", stop_side, amount, sl_rounded,
                {"stopPrice": sl_rounded, "reduceOnly": True}
            )
            print(f"[Trader] SL gesetzt: {symbol} {side} {amount} @ {sl_rounded:.6f} | ID: {order.get('id')}")
            return True

        except Exception as e:
            print(f"[Trader] SL ERROR: {e}")
            return False

    def get_stop_loss_price(self, symbol: str) -> Optional[float]:
        """Liest aktuellen SL-Preis aus offenen Orders."""
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    return float(o.get("price", 0) or o.get("stopPrice", 0) or 0)
            return None
        except Exception:
            return None

    # ─── Close Position ────────────────────────────────────────

    def close_position(self, symbol: str, side: str,
                       amount: Optional[float] = None,
                       close_pct: float = 1.0) -> TradeResult:
        """Schließt Position (teilweise/ganz). Cancelt SL vorher."""
        if not KRAKEN_KEY:
            return TradeResult(False, symbol, side, "", 0, 0, 0, 0, 0,
                             "DRY RUN", datetime.now(timezone.utc).isoformat())

        try:
            ex = self._get_exchange()
            ex.load_markets()

            # Cancel all orders (SL)
            try:
                ex.cancel_all_orders(symbol)
            except Exception:
                pass

            # Get position
            positions = ex.fetch_positions([symbol])
            pos = None
            for p in positions:
                if float(p.get("contracts", 0) or 0) > 0:
                    pos = p
                    break

            if pos is None:
                return TradeResult(False, symbol, side, "", 0, 0, 0, 0, 0,
                                 "Keine Position", datetime.now(timezone.utc).isoformat())

            total = float(pos.get("contracts", 0))
            close_size = total * close_pct

            if side == "long":
                order = ex.create_market_sell_order(symbol, close_size, {"reduceOnly": True})
            else:
                order = ex.create_market_buy_order(symbol, close_size, {"reduceOnly": True})

            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            print(f"[Trader] Close {int(close_pct*100)}% {symbol}: {close_size} @ {avg_price:.6f}")

            return TradeResult(True, symbol, side, str(order.get("id", "")),
                             avg_price, close_size, float(order.get("cost", 0) or 0), 0, 0,
                             f"Closed {int(close_pct*100)}%", datetime.now(timezone.utc).isoformat())

        except Exception as e:
            print(f"[Trader] Close ERROR: {e}")
            return TradeResult(False, symbol, side, "", 0, 0, 0, 0, 0,
                             str(e), datetime.now(timezone.utc).isoformat())

    def has_api_keys(self) -> bool:
        return bool(KRAKEN_KEY and KRAKEN_SECRET)

    def get_open_positions(self) -> list:
        if not self.has_api_keys():
            return []
        try:
            return self._get_exchange().fetch_positions()
        except Exception:
            return []