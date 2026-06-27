#!/usr/bin/env python3
"""
graviton/trader.py — CCXT Kraken Futures Order Execution
==========================================================
Platziert Market-Orders + Stop-Loss auf Kraken Perpetuals via CCXT.

Flow:
  1. open_position()  → Market-Order, wartet auf Fill
  2. set_stop_loss()  → Stop-Order mit reduceOnly (SL = von entry.py berechnet)
  3. close_position() → Schließt Position, cancelt SL vorher

Watcher:
  - has_stop_loss()       → Prüft ob SL existiert
  - get_stop_loss_price() → Liest aktuellen SL-Preis
"""

from __future__ import annotations
import ccxt
import os
import time
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import CFG


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


@dataclass
class FillResult:
    """Ergebnis einer gefillten Order."""
    success: bool
    symbol: str
    side: str
    order_id: str
    price: float
    size: float
    cost: float
    message: str
    timestamp: str


@dataclass
class TradeResult:
    success: bool
    symbol: str
    side: str
    order_id: str
    price: float
    size: float
    cost: float
    message: str
    timestamp: str


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

    def get_size_contracts(self, symbol: str, usd_amount: float) -> float:
        try:
            ex = self._get_exchange()
            ex.load_markets()
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker["last"])
            contracts = usd_amount / price
            market = ex.market(symbol)
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 1) or 1
            return max(contracts, min_amount)
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

    # ─── Open Position ─────────────────────────────────────────

    def open_position(self, symbol: str, side: str,
                      size_usd: Optional[float] = None) -> FillResult:
        """
        Öffnet Position via Market-Order und wartet auf Fill.
        KEIN Stop-Loss — der kommt separat.
        """
        if not KRAKEN_KEY:
            return FillResult(False, symbol, side, "", 0, 0, 0,
                            "DRY RUN", datetime.now(timezone.utc).isoformat())

        try:
            ex = self._get_exchange()
            ex.load_markets()
            self.set_leverage(symbol, CFG.exchange["leverage"])

            if size_usd is None:
                size_usd = self.calc_position_size()

            size = self.get_size_contracts(symbol, size_usd)
            if size <= 0:
                return FillResult(False, symbol, side, "", 0, 0, 0,
                                "Kontraktgröße ≤ 0", datetime.now(timezone.utc).isoformat())

            # Market Order
            if side == "long":
                order = ex.create_market_buy_order(symbol, size)
            else:
                order = ex.create_market_sell_order(symbol, size)

            order_id = str(order.get("id", ""))
            fill_price = float(order.get("average", order.get("price", 0)) or 0)
            fill_size = float(order.get("filled", size) or size)
            fill_cost = float(order.get("cost", 0) or 0)

            print(f"[Trader] OPEN {side.upper()} {symbol}: {fill_size} @ {fill_price:.6f} | ID: {order_id}")

            return FillResult(True, symbol, side, order_id, fill_price,
                            fill_size, fill_cost,
                            f"Filled {side.upper()}", datetime.now(timezone.utc).isoformat())

        except Exception as e:
            print(f"[Trader] Open ERROR: {e}")
            return FillResult(False, symbol, side, "", 0, 0, 0,
                            str(e), datetime.now(timezone.utc).isoformat())

    # ─── Stop Loss ─────────────────────────────────────────────

    def has_stop_loss(self, symbol: str) -> bool:
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    return True
            return False
        except Exception:
            return False

    def get_stop_loss_price(self, symbol: str) -> Optional[float]:
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    # Stop orders: price is trigger, not limit
                    return float(
                        o.get("stopPrice", 0)
                        or o.get("triggerPrice", 0)
                        or o.get("price", 0)
                        or 0
                    )
            return None
        except Exception:
            return None

    def _cancel_stop_orders(self, symbol: str):
        """Entfernt alle existierenden Stop-Orders für Symbol."""
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    ex.cancel_order(o["id"], symbol)
                    print(f"[Trader] Alten SL gecancelled")
        except Exception:
            pass

    def _price_precision(self, symbol: str) -> int:
        """Anzahl Dezimalstellen für Preis-Tick."""
        try:
            ex = self._get_exchange()
            ex.load_markets()
            market = ex.market(symbol)
            prec = market.get("precision", {}).get("price", 0.0001)
            s = f"{prec:.10f}"
            if "e-" in s:
                return abs(int(s.split("e-")[-1]))
            return len(s.split(".")[-1].rstrip("0"))
        except Exception:
            return 4

    def set_stop_loss(self, symbol: str, side: str, stop_price: float,
                      amount: Optional[float] = None) -> bool:
        """
        Setzt Stop-Loss via Stop-Order mit reduceOnly.
        SL-Preis kommt von entry.py (Rejection-Kerze ± 0.2%).
        """
        if not KRAKEN_KEY:
            print(f"[Trader] DRY RUN: SL {symbol} {side} @ {stop_price:.4f}")
            return True

        try:
            ex = self._get_exchange()
            ex.load_markets()

            # Cancel old SL first
            self._cancel_stop_orders(symbol)

            # Round to market precision
            ndigits = self._price_precision(symbol)
            sl_rounded = round(stop_price, ndigits)

            # Get position size if not given
            if amount is None or amount <= 0:
                positions = ex.fetch_positions([symbol])
                for p in positions:
                    c = float(p.get("contracts", 0) or 0)
                    if c > 0:
                        amount = c
                        break

            if not amount or amount <= 0:
                print(f"[Trader] SL: Keine Position für {symbol}")
                return False

            # Stop order: buy for short, sell for long
            stop_side = "buy" if side == "short" else "sell"

            order = ex.create_order(
                symbol, "stop", stop_side, amount, sl_rounded,
                {"stopPrice": sl_rounded, "reduceOnly": True}
            )
            print(f"[Trader] SL gesetzt: {symbol} {amount} @ {sl_rounded} | ID: {order.get('id')}")
            return True

        except Exception as e:
            print(f"[Trader] SL ERROR: {e}")
            return False

    # ─── Close Position ────────────────────────────────────────

    def close_position(self, symbol: str, side: str,
                       amount: Optional[float] = None,
                       close_pct: float = 1.0) -> TradeResult:

        if not KRAKEN_KEY:
            return TradeResult(False, symbol, side, "", 0, 0, 0,
                             "DRY RUN", datetime.now(timezone.utc).isoformat())

        try:
            ex = self._get_exchange()
            ex.load_markets()

            # Cancel SL first
            self._cancel_stop_orders(symbol)

            # Get position
            positions = ex.fetch_positions([symbol])
            pos = None
            for p in positions:
                if float(p.get("contracts", 0) or 0) > 0:
                    pos = p
                    break

            if pos is None:
                return TradeResult(False, symbol, side, "", 0, 0, 0,
                                 "Keine Position", datetime.now(timezone.utc).isoformat())

            total = float(pos.get("contracts", 0))
            close_size = total * close_pct

            if side == "long":
                order = ex.create_market_sell_order(symbol, close_size, {"reduceOnly": True})
            else:
                order = ex.create_market_buy_order(symbol, close_size, {"reduceOnly": True})

            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            pct_text = f"{int(close_pct*100)}%"
            print(f"[Trader] CLOSE {pct_text} {symbol}: {close_size} @ {avg_price:.6f}")

            return TradeResult(True, symbol, side, str(order.get("id", "")),
                             avg_price, close_size, float(order.get("cost", 0) or 0),
                             f"Closed {pct_text}", datetime.now(timezone.utc).isoformat())

        except Exception as e:
            print(f"[Trader] Close ERROR: {e}")
            return TradeResult(False, symbol, side, "", 0, 0, 0,
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