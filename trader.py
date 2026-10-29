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
import json
import os
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CFG, DRY_RUN


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


def _log_dry_run_trade(payload: dict) -> None:
    """Schreibt Dry-Run-Events ins Trade-Log des aktiven Modus."""
    try:
        path = Path(getattr(CFG, "data_dir", Path(__file__).parent / "data")) / "trade_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(payload) + "\n")
    except Exception:
        pass


@dataclass
class FillResult:
    """Ergebnis einer gefillten Order. Identisch zu TradeResult — eine reicht."""
    success: bool
    symbol: str
    side: str
    order_id: str
    price: float
    size: float
    cost: float
    message: str
    timestamp: str

# Alias für Kompatibilität
TradeResult = FillResult


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
        except Exception as e:
            print(f"[Trader] EUR/USD fetch: {e} — Fallback 1.08")
            return 1.08

    def get_equity(self) -> float:
        if self._equity is not None:
            return self._equity
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance({"type": "flex"})
            total_dict = balance.get("total", {})
            usd = float(total_dict.get("USD", 0) or 0)
            eur = float(total_dict.get("EUR", 0) or 0)
            # Multi-Collateral: USD und EUR können gleichzeitig im Wallet liegen
            # (z.B. USD-Reste aus Futures-PnL/Gebühren). Summiere beide, statt
            # fälschlich nur USD zu nehmen, sobald ein kleiner USD-Rest > 0 ist.
            if eur > 0:
                usd += eur * self._fetch_eur_usd_rate()
            if usd > 0:
                self._equity = usd
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
        fixed = CFG.position.get("fixed_usd") or 0
        if fixed > 0:
            return float(fixed)
        equity = self.get_equity()
        risk_pct = CFG.position["account_risk_pct_per_coin"] / 100
        return equity * risk_pct

    def get_size_contracts(self, symbol: str, usd_amount: float) -> float:
        try:
            ex = self._get_exchange()
            ex.load_markets()
            market = ex.market(symbol)
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker["last"])
            contract_size = float(market.get("contractSize", 1) or 1)
            raw_contracts = usd_amount / (price * contract_size)
            min_amount = market.get("limits", {}).get("amount", {}).get("min")
            if min_amount is not None and raw_contracts < float(min_amount):
                print(f"[Trader] {symbol}: ${usd_amount:.2f} → {raw_contracts:.6f} Contracts < min {min_amount}. Abbruch.")
                return 0.0
            contracts = float(ex.amount_to_precision(symbol, raw_contracts))
            if contracts <= 0:
                print(f"[Trader] {symbol}: ${usd_amount:.2f} unter Amount-Precision. Abbruch.")
                return 0.0
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

        if DRY_RUN:
            # KEIN API-Call im Dry-Run. Vorher fehlte dieser Guard im Trader
            # (nur die Session prueft DRY_RUN) — ein manueller Test hat am
            # 28.10. eine echte Kraken-Order ausgeloest.
            if size_usd is None:
                size_usd = self.calc_position_size()
            try:
                px = float(self._get_exchange().fetch_ticker(symbol)["last"])
            except Exception:
                px = 0.0
            size = self.get_size_contracts(symbol, size_usd) if px > 0 else 0.0
            ts = datetime.now(timezone.utc).isoformat()
            _log_dry_run_trade({"event": "entry", "timestamp": ts, "symbol": symbol,
                                "bias": side.upper(), "entry": px,
                                "size": size, "size_usd": size_usd, "mode": "DRY_RUN"})
            return FillResult(True, symbol, side, f"dry-{int(time.time())}", px, size,
                              round(size * px, 4), "DRY RUN Fill (simuliert)", ts)

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
        except Exception as e:
            print(f"[Trader] has_stop_loss {symbol}: {e}")
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
        except Exception as e:
            print(f"[Trader] get_stop_loss_price {symbol}: {e}")
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
        except Exception as e:
            print(f"[Trader] _cancel_stop_orders {symbol}: {e}")

    def _current_stop_order_id(self, symbol: str) -> Optional[str]:
        """Return the id of the existing reduceOnly stop, if any."""
        try:
            ex = self._get_exchange()
            orders = ex.fetch_open_orders(symbol)
            for o in orders:
                if o.get("reduceOnly") and "stop" in str(o.get("type", "")).lower():
                    return str(o.get("id", "")) or None
            return None
        except Exception:
            return None

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

            if DRY_RUN:
                _log_dry_run_trade({"event": "sl", "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "symbol": symbol, "side": side, "stop": stop_price,
                                    "mode": "DRY_RUN"})
                print(f"[Trader] DRY RUN SL {symbol} {stop_price}")
                return True

            # Round to market precision
            ndigits = self._price_precision(symbol)
            sl_rounded = round(stop_price, ndigits)

            # ── Safety: stop darf niemals auf der falschen Marktseite landen ──
            # Wurde z.B. vom Close einer veralteten 1m-Kerze abgeleitet, während
            # der Live-Markt schon weiter gelaufen ist → Stop wäre durch den Markt
            # und würde sofort als Market-Order feuern.
            MIN_DIST = 0.003  # 0.3% Mindestabstand vom Mark-Preis
            try:
                ticker = ex.fetch_ticker(symbol)
                mark = float(ticker.get("info", {}).get("markPrice", 0) or 0)
                if mark <= 0:
                    mark = float(ticker.get("last", 0) or 0)
            except Exception:
                mark = 0

            if mark > 0:
                if side == "short":
                    safe_min = mark * (1 + MIN_DIST)
                    if sl_rounded <= safe_min:
                        old_sl = sl_rounded
                        sl_rounded = round(safe_min, ndigits)
                        print(f"[Trader] ⚠ SL {old_sl:.6f} ≤ Mark {mark:.6f} → "
                              f"korrigiert auf {sl_rounded:.6f} (SHORT-Stop muss ABOVE)")
                else:  # long
                    safe_max = mark * (1 - MIN_DIST)
                    if sl_rounded >= safe_max:
                        old_sl = sl_rounded
                        sl_rounded = round(safe_max, ndigits)
                        print(f"[Trader] ⚠ SL {old_sl:.6f} ≥ Mark {mark:.6f} → "
                              f"korrigiert auf {sl_rounded:.6f} (LONG-Stop muss BELOW)")

            # Identify the existing SL so it can be cancelled only after the
            # new SL is accepted. Keeps the previous reduceOnly protection in
            # place if the new submit fails.
            old_stop_id = self._current_stop_order_id(symbol)

            # Get position size — match side
            if amount is None or amount <= 0:
                positions = ex.fetch_positions([symbol])
                for p in positions:
                    c = float(p.get("contracts", 0) or 0)
                    p_side = (p.get("side", "") or "").lower()
                    if c > 0 and p_side == side:
                        amount = c
                        break

            if not amount or amount <= 0:
                print(f"[Trader] SL: Keine Position für {symbol}")
                return False

            # New SL is placed first; only cancelled on success so a failed
            # submit leaves the previous reduceOnly protection active.
            stop_side = "buy" if side == "short" else "sell"

            try:
                order = ex.create_order(
                    symbol, "stop", stop_side, amount, None,
                    {
                        "stopPrice": sl_rounded,
                        "reduceOnly": True,
                        "triggerSignal": "mark",
                    }
                )
            except Exception as exc:
                print(f"[Trader] SL-Fehler bei {symbol}: {exc}")
                return False

            if old_stop_id:
                try:
                    ex.cancel_order(old_stop_id, symbol)
                except Exception as exc:
                    print(f"[Trader] Alten SL nicht abgebrochen, neuer aktiv: {exc}")
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

        if DRY_RUN:
            # Im Dry-Run gibt es keine Exchange-Position. Ein manuelles
            # Schliessen wird trotzdem protokolliert, damit ein Test-Trade
            # (oeffnen -> SL editieren -> schliessen) nachvollziehbar bleibt
            # und nicht spurlos verschwindet.
            ts = datetime.now(timezone.utc).isoformat()
            try:
                path = Path(getattr(CFG, "data_dir", Path(__file__).parent / "data")) / "trade_log.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as fh:
                    fh.write(json.dumps({
                        "event": "exit", "timestamp": ts, "symbol": symbol,
                        "bias": (side or "").upper(), "reason": "manual_close",
                        "close_pct": round(close_pct * 100, 1), "mode": "DRY_RUN",
                    }) + "\n")
            except Exception:
                pass
            return TradeResult(True, symbol, side, f"dry-{int(time.time())}", 0.0,
                               float(amount or 0.0), 0.0,
                               "DRY RUN Close (simuliert, protokolliert)", ts)

        try:
            ex = self._get_exchange()
            ex.load_markets()

            # Der bestehende reduceOnly-SL bleibt bis zum bestätigten Market-
            # Close aktiv. So bleibt die Position auch bei Order-Fehler geschützt.

            # Get position — match side
            positions = ex.fetch_positions([symbol])
            pos = None
            # Die Position hat die GEGEN-Seite des Close-Orders: ein Long
            # wird mit "sell" geschlossen, steht aber als "buy" in der Position.
            # Vorher wurde p_side == side verglichen -> Close fand nie eine
            # Position und meldete "Keine Position".
            want_side = "buy" if str(side).lower() == "sell" else "sell"
            for p in positions:
                c = float(p.get("contracts", 0) or 0)
                p_side = (p.get("side", "") or "").lower()
                if c > 0 and p_side == want_side:
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

            if close_pct >= 0.999:
                self._cancel_stop_orders(symbol)

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

    def get_open_positions(self, strict: bool = False) -> list:
        if not self.has_api_keys():
            if strict:
                raise RuntimeError("Kraken API-Keys fehlen")
            return []
        try:
            return self._get_exchange().fetch_positions()
        except Exception as e:
            print(f"[Trader] fetch_positions error: {e}")
            if strict:
                raise RuntimeError(f"Kraken Positionsabruf fehlgeschlagen: {e}") from e
            return []

    # ─── Echte Positions-/Fill-Abfrage (gegen Fake-Meldungen) ─────

    def get_position_contracts(self, symbol: str, side: str) -> float:
        """Aktuelle offene Contracts für (symbol, side). 0 = Position geschlossen/weg."""
        try:
            positions = self._get_exchange().fetch_positions([symbol])
            for p in positions:
                c = float(p.get("contracts", 0) or 0)
                p_side = (p.get("side", "") or "").lower()
                if c > 0 and p_side == side:
                    return c
            return 0.0
        except Exception as e:
            print(f"[Trader] get_position_contracts {symbol}: {e}")
            return -1.0  # -1 = Abfrage fehlgeschlagen (nicht sicher)

    def get_fills_since(self, symbol: str, since_ms: float) -> list:
        """Echte Kraken-Fills für symbol seit since_ms (aufsteigend sortiert)."""
        try:
            fills = self._get_exchange().fetch_my_trades(symbol, limit=100)
            return sorted(
                [f for f in fills if f.get("timestamp", 0) >= since_ms],
                key=lambda f: f.get("timestamp", 0),
            )
        except Exception as e:
            print(f"[Trader] get_fills_since {symbol}: {e}")
            return []

    def get_account_state(self) -> dict:
        """EUR + USD Equity (Multi-Collateral) für Konto-Stand-Anzeige.

        Returns dict mit eur, usd, usd_total (EUR→USD via aktuellem Kurs),
        oder None wenn Abruf fehlschlägt.
        """
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance({"type": "flex"})
            total = balance.get("total", {})
            usd = float(total.get("USD", 0) or 0)
            eur = float(total.get("EUR", 0) or 0)
            rate = self._fetch_eur_usd_rate()
            usd_total = usd + eur * rate
            return {"eur": eur, "usd": usd, "usd_total": usd_total, "rate": rate}
        except Exception as e:
            print(f"[Trader] get_account_state: {e}")
            return None