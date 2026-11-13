"""Swing-Datenzugang: dynamisches Aktien-Perp-Universum + 1D-Kerzen.

Das Universum wird NICHT hartkodiert: es kommt aus den Kraken-Markets, gefiltert
nach Kategorie. Nimmt Kraken neue xStocks/Equities auf, sind sie automatisch dabei.
"""
from __future__ import annotations

import ccxt

# Kategorien, die als "Aktien-Perp" gelten. Neue Symbole INNERHALB dieser
# Kategorien kommen automatisch mit; neue Kategorien hier ergänzen.
STOCK_CATEGORIES = ("xStocks", "Equities")


def exchange() -> ccxt.Exchange:
    ex = ccxt.krakenfutures({"enableRateLimit": True})
    ex.load_markets()
    return ex


def stock_perps(ex: ccxt.Exchange | None = None,
                categories: tuple[str, ...] = STOCK_CATEGORIES) -> list[dict]:
    """Alle Perps der gewünschten Kategorien (symbol, base, category)."""
    ex = ex or exchange()
    out = []
    for sym, m in ex.markets.items():
        cat = (m.get("info") or {}).get("category", "")
        if cat in categories:
            out.append({"symbol": sym, "base": m.get("base"), "category": cat})
    return sorted(out, key=lambda r: (r["category"], r["base"] or ""))


def daily_candles(ex: ccxt.Exchange, symbol: str, limit: int = 1000) -> list[list]:
    """1D-OHLCV (UTC) für ein Symbol."""
    return ex.fetch_ohlcv(symbol, "1d", limit=limit)


# Perp-Base -> Underlying-Ticker (TwelveData). None = kein öffentliches Underlying
# (z. B. Pre-IPO). Neue Perps ohne Eintrag fallen auf die Perp-eigene Historie
# zurück; hier nachtragen, sobald ein Underlying bekannt ist.
UNDERLYING = {
    "AAPLX": "AAPL", "AMZNX": "AMZN", "COINX": "COIN", "CRCLX": "CRCL",
    "GLDX": "GLD", "GOOGLX": "GOOGL", "HOODX": "HOOD", "METAX": "META",
    "MSTRX": "MSTR", "NVDAX": "NVDA", "QQQX": "QQQ", "SPYX": "SPY",
    "TSLAX": "TSLA", "SPCXX": None,
    "AMD": "AMD", "EWY": "EWY", "EWZ": "EWZ", "INTC": "INTC", "MRVL": "MRVL",
    "MU": "MU", "NBIS": "NBIS", "PLTR": "PLTR", "SNDK": "SNDK",
    "SOXL": "SOXL", "SOXS": "SOXS",
    "SAMSUNG": "005930.KS", "SKHY": "000660.KS",
}


def underlying_for(base: str) -> str | None:
    return UNDERLYING.get(base)
