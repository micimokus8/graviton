#!/usr/bin/env python3
"""
graviton/trader.py — CCXT Kraken Futures Order Execution
==========================================================
Platziert Market-Orders + Stop-Loss auf Kraken Perpetuals via CCXT.

API Keys werden aus .env oder Umgebungsvariablen geladen:
  KRAKEN_API_KEY, KRAKEN_API_SECRET

Features:
  - Market-Order Entry
  - Stop-Loss via Limit-Order (Kraken Futures: "stop" order type)
  - Position Sizing: 15% Equity pro Coin, 4 Treppen (equal distribution)
  - Close Position (Market)
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
    """Ergebnis einer Order-Platzierung."""
    success: bool
    symbol: str
    side: str           # "long" oder "short"
    order_id: str
    price: float
    size: float         # Contracts / Menge
    cost: float         # USD Wert
    stop_loss: float
    step: int           # Treppenstufe
    message: str
    timestamp: str


@dataclass
class Position:
    """Aktive Position."""
    symbol: str
    side: str
    size: float
    entry_price: float
    stop_loss: float
    current_price: float
    pnl_pct: float
    steps: int
    opened_at: str


# ═══════════════════════════════════════════════════════════════════
# Kraken Trader
# ═══════════════════════════════════════════════════════════════════

class KrakenTrader:
    """
    Führt Trades auf Kraken Futures aus.
    Nutzt CCXT mit API-Key Authentifizierung.
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._equity: Optional[float] = None
        self._active_positions: Dict[str, dict] = {}

    # ─── Exchange ──────────────────────────────────────────────

    def _get_exchange(self) -> ccxt.Exchange:
        """Lazy-init authenticated CCXT exchange."""
        if self._exchange is None:
            if not KRAKEN_KEY or not KRAKEN_SECRET:
                raise RuntimeError(
                    "KRAKEN_API_KEY und KRAKEN_API_SECRET nicht gesetzt! "
                    "Bitte in .env eintragen."
                )
            self._exchange = ccxt.krakenfutures({
                "apiKey": KRAKEN_KEY,
                "secret": KRAKEN_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            # Kein load_markets() nötig für private endpoints wenn public schon geladen
        return self._exchange

    # ─── Account ───────────────────────────────────────────────

    def get_equity(self) -> float:
        """Holt aktuelles Account-Equity (USD)."""
        if self._equity is not None:
            return self._equity
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance()
            total = float(balance.get("total", {}).get("USD", 0) or balance.get("total", {}).get("USDT", 0) or 0)
            free = float(balance.get("free", {}).get("USD", 0) or balance.get("free", {}).get("USDT", 0) or 0)
            self._equity = max(total, free)
            return self._equity
        except Exception as e:
            print(f"[Trader] Konnte Balance nicht laden: {e}")
            return float(os.getenv("EQUITY_USD", "200"))

    def refresh_equity(self) -> float:
        """Equity neu laden."""
        self._equity = None
        return self.get_equity()

    # ─── Position Sizing ───────────────────────────────────────

    def calc_position_size(self) -> float:
        """
        Berechnet Position Size in USD.
        15% Account Equity, equal distribution über 4 Steps.
        """
        equity = self.get_equity()
        cfg_pos = CFG.position
        risk_pct = cfg_pos["account_risk_pct_per_coin"] / 100  # 0.15
        total_for_coin = equity * risk_pct

        max_steps = CFG.entry["max_stair_steps"]
        per_step = total_for_coin / max_steps  # 3.75% pro Stufe

        return per_step

    def get_position_size_contracts(self, symbol: str, usd_amount: float) -> float:
        """
        Konvertiert USD-Betrag in Kontrakt-Anzahl.
        Kraken Futures: linear contracts, 1 contract = 1 USD notional (typisch).
        """
        try:
            ex = self._get_exchange()
            market = ex.market(symbol)
            contract_size = market.get("contractSize", 1)
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 1)

            # Bei linearen Perps: size ≈ usd_amount / price
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker["last"])

            contracts = usd_amount / (contract_size * price) if contract_size > 0 else usd_amount / price
            contracts = max(contracts, min_amount or 1)

            # Runde auf erlaubte Precision
            amount_precision = market.get("precision", {}).get("amount", 8)
            contracts = round(contracts, amount_precision)

            return contracts
        except Exception as e:
            print(f"[Trader] Konnte Kontraktgröße nicht berechnen: {e}")
            return 0

    # ─── Entry Order ───────────────────────────────────────────

    def enter_position(
        self,
        symbol: str,
        side: str,       # "long" oder "short"
        size_usd: Optional[float] = None,
        step: int = 1,
    ) -> TradeResult:
        """
        Öffnet eine Position via Market Order.

        Args:
            symbol: CCXT Symbol
            side: "long" oder "short"
            size_usd: Positionsgröße in USD (None = auto-calc)
            step: Treppenstufe (1-4)

        Returns:
            TradeResult
        """
        if not KRAKEN_KEY:
            return TradeResult(
                success=False, symbol=symbol, side=side,
                order_id="", price=0, size=0, cost=0, stop_loss=0,
                step=step, message="Keine API Keys konfiguriert (DRY RUN)",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        try:
            ex = self._get_exchange()

            if size_usd is None:
                size_usd = self.calc_position_size()

            size_contracts = self.get_position_size_contracts(symbol, size_usd)
            if size_contracts <= 0:
                return TradeResult(
                    success=False, symbol=symbol, side=side,
                    order_id="", price=0, size=0, cost=size_usd, stop_loss=0,
                    step=step, message="Kontraktgröße ≤ 0",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

            # Market Order
            if side == "long":
                order = ex.create_market_buy_order(symbol, size_contracts)
            else:
                order = ex.create_market_sell_order(symbol, size_contracts)

            order_id = str(order.get("id", "unknown"))
            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            cost = float(order.get("cost", 0) or 0)

            print(f"[Trader] {side.upper()} Entry: {symbol} "
                  f"size={size_contracts} @ ~{avg_price:.6f} (Step {step})")

            return TradeResult(
                success=True,
                symbol=symbol,
                side=side,
                order_id=order_id,
                price=avg_price or 0,
                size=size_contracts,
                cost=cost,
                stop_loss=0,  # wird separat gesetzt
                step=step,
                message=f"Entry {side.upper()} {symbol}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            error_msg = str(e)
            print(f"[Trader] Entry ERROR: {error_msg}")
            return TradeResult(
                success=False, symbol=symbol, side=side,
                order_id="", price=0, size=0, cost=0, stop_loss=0,
                step=step, message=error_msg,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

    # ─── Stop Loss ─────────────────────────────────────────────

    def set_stop_loss(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        amount: Optional[float] = None,
    ) -> bool:
        """
        Setzt einen Stop-Loss via "stop" Limit-Order.

        Kraken Futures: stop-loss order type = "stop"
        """
        if not KRAKEN_KEY:
            print(f"[Trader] DRY RUN: Stop-Loss {symbol} {side} @ {stop_price}")
            return True

        try:
            ex = self._get_exchange()

            if side == "long":
                # Long SL: sell stop unter Entry
                params = {
                    "stopPrice": stop_price,
                    "triggerPrice": stop_price,
                }
                order = ex.create_order(
                    symbol, "stop", "sell", amount or 0,
                    stop_price, params,
                )
            else:
                # Short SL: buy stop über Entry
                params = {
                    "stopPrice": stop_price,
                    "triggerPrice": stop_price,
                }
                order = ex.create_order(
                    symbol, "stop", "buy", amount or 0,
                    stop_price, params,
                )

            print(f"[Trader] Stop-Loss gesetzt: {symbol} {side} @ {stop_price:.6f}")
            return True

        except Exception as e:
            print(f"[Trader] Stop-Loss ERROR: {e}")
            return False

    # ─── Close Position ────────────────────────────────────────

    def close_position(
        self,
        symbol: str,
        side: str,
        amount: Optional[float] = None,
        close_pct: float = 1.0,  # 1.0 = 100%, 0.5 = 50%
    ) -> TradeResult:
        """
        Schließt eine Position (teilweise oder ganz).

        Args:
            symbol: CCXT Symbol
            side: "long" oder "short"
            amount: Menge (None = gesamte Position)
            close_pct: Anteil zum Schließen (0.5 = 50%)
        """
        if not KRAKEN_KEY:
            return TradeResult(
                success=False, symbol=symbol, side=side,
                order_id="", price=0, size=0, cost=0, stop_loss=0,
                step=0, message="DRY RUN — keine Live-Order",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        try:
            ex = self._get_exchange()

            # Position aus fetch_positions holen
            positions = ex.fetch_positions([symbol])
            pos = None
            for p in positions:
                if float(p.get("contracts", 0) or 0) > 0:
                    pos = p
                    break

            if pos is None:
                return TradeResult(
                    success=False, symbol=symbol, side=side,
                    order_id="", price=0, size=0, cost=0, stop_loss=0,
                    step=0, message="Keine offene Position gefunden",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

            total_contracts = float(pos.get("contracts", 0))
            close_contracts = total_contracts * close_pct

            # Runden
            market = ex.market(symbol)
            precision = market.get("precision", {}).get("amount", 8)
            close_contracts = round(close_contracts, precision)

            if close_contracts <= 0:
                return TradeResult(
                    success=False, symbol=symbol, side=side,
                    order_id="", price=0, size=0, cost=0, stop_loss=0,
                    step=0, message="Close amount ≤ 0",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

            # Market close
            if side == "long":
                order = ex.create_market_sell_order(symbol, close_contracts, {
                    "reduceOnly": True,
                })
            else:
                order = ex.create_market_buy_order(symbol, close_contracts, {
                    "reduceOnly": True,
                })

            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            cost = float(order.get("cost", 0) or 0)

            pct_text = f"{int(close_pct * 100)}%"
            print(f"[Trader] Close {pct_text}: {symbol} {side} "
                  f"{close_contracts} @ ~{avg_price:.6f}")

            return TradeResult(
                success=True, symbol=symbol, side=side,
                order_id=str(order.get("id", "")),
                price=avg_price, size=close_contracts, cost=cost,
                stop_loss=0, step=0,
                message=f"Closed {pct_text} of {symbol}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            print(f"[Trader] Close ERROR: {e}")
            return TradeResult(
                success=False, symbol=symbol, side=side,
                order_id="", price=0, size=0, cost=0, stop_loss=0,
                step=0, message=str(e),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

    # ─── Convenience ───────────────────────────────────────────

    def has_api_keys(self) -> bool:
        """Prüft ob API Keys konfiguriert sind."""
        return bool(KRAKEN_KEY and KRAKEN_SECRET)

    def get_open_positions(self) -> list:
        """Listet alle offenen Positionen."""
        if not self.has_api_keys():
            return []
        try:
            ex = self._get_exchange()
            return ex.fetch_positions()
        except Exception:
            return []