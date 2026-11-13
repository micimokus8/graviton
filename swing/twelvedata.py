"""TwelveData-Tageskerzen als Proxy für die (jungen) Kraken-Aktien-Perps.

Gemeinsamer Zugang mit Rate-Limit (8 Credits/Minute) und Datei-Cache, damit
Validierungsläufe und der Tages-Scan nicht bei jedem Start neu laden.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

KEY_FILE = "/root/.hermes/workspace/earnings-monitor/TwelveData Key.txt"
SLEEP_S = 8.0
CACHE_TTL_H = 20.0


def fetch_daily(symbol: str, outputsize: int = 5000) -> list[list]:
    """1D-Kerzen [date, open, high, low, close, volume], aufsteigend."""
    key = open(KEY_FILE).read().strip()
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=1day"
           f"&outputsize={outputsize}&apikey={key}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    vals = data.get("values") or []
    if not vals:
        raise RuntimeError(str(data.get("message", "keine Daten"))[:100])
    return [[v["datetime"], float(v["open"]), float(v["high"]), float(v["low"]),
             float(v["close"]), float(v.get("volume") or 0)] for v in reversed(vals)]


def load(symbols: list[str], cache_path: str) -> dict[str, list[list]]:
    """Cache laden oder fehlende Symbole nachholen (rate-limited)."""
    out: dict[str, list[list]] = {}
    if os.path.exists(cache_path):
        out = json.load(open(cache_path))
    missing = [s for s in symbols if s not in out]
    if missing:
        for i, s in enumerate(missing):
            try:
                out[s] = fetch_daily(s)
                print(f"  {s}: {len(out[s])} Bars ({out[s][0][0]}..{out[s][-1][0]})")
            except Exception as exc:
                print(f"  {s}: FEHLER {str(exc)[:80]}")
            if i < len(missing) - 1:
                time.sleep(SLEEP_S)
        json.dump(out, open(cache_path, "w"))
    return out


def to_ms(rows: list[list]) -> list[list]:
    """[date, o, h, l, c, v] -> [ts_ms, o, h, l, c, v] (UTC-Mitternacht)."""
    out = []
    for d, o, h, l, c, v in rows:
        ts = int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000)
        out.append([ts, o, h, l, c, v])
    return out


def daily_ms(symbol: str, cache_path: str = "data/underlying_daily.json",
             outputsize: int = 400) -> list[list] | None:
    """Tageskerzen in ms-Form; pro Symbol gecacht (TTL), damit der Scan nicht bremst."""
    now = datetime.now(timezone.utc)
    cache: dict = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except ValueError:
            cache = {}
    books = cache.get("symbols") or {}
    entry = books.get(symbol)
    if isinstance(entry, dict):
        try:
            age = (now - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
            if age < CACHE_TTL_H * 3600:
                return to_ms(entry["rows"])
        except (KeyError, TypeError, ValueError):
            pass
    try:
        rows = fetch_daily(symbol, outputsize=outputsize)
    except Exception:
        return None
    books[symbol] = {"fetched_at": now.isoformat(), "rows": rows}
    cache["symbols"] = books
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(cache, fh)
    time.sleep(SLEEP_S)  # Rate-Limit respektieren
    return to_ms(rows)
