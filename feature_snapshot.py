"""Deterministic features from already fetched, closed OHLCV candles."""
from __future__ import annotations

import numpy as np


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return out
    out[period - 1] = np.mean(values[:period])
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(closes: np.ndarray, period: int = 14):
    if len(closes) <= period:
        return None
    changes = np.diff(closes)
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def _atr_pct(data: np.ndarray, period: int = 14):
    if len(data) < period + 1:
        return None
    high, low, close = data[:, 2], data[:, 3], data[:, 4]
    previous_close = close[:-1]
    true_ranges = np.maximum(high[1:] - low[1:], np.maximum(
        np.abs(high[1:] - previous_close), np.abs(low[1:] - previous_close)))
    return float(np.mean(true_ranges[-period:]) / close[-1] * 100) if close[-1] else None


def build_timeframe_features(data: np.ndarray) -> dict:
    """Build a JSON-safe feature dict from closed OHLCV rows."""
    array = np.asarray(data, dtype=float)
    base = {"candles": int(len(array)), "trend": "ERROR", "price": None,
            "ema20": None, "ema50": None, "ema20_slope_pct": None,
            "rsi": None, "macd": None, "macd_signal": None,
            "macd_histogram": None, "stochastic": None, "atr_pct": None,
            "volatility_regime": "UNKNOWN"}
    if array.ndim != 2 or array.shape[1] < 6 or len(array) < 2:
        return base
    closes = array[:, 4]
    base["price"] = float(closes[-1])
    base["rsi"] = _rsi(closes)
    base["atr_pct"] = _atr_pct(array)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    if not np.isnan(ema20[-1]):
        base["ema20"] = float(ema20[-1])
        if len(ema20) >= 4 and not np.isnan(ema20[-4]) and ema20[-4]:
            base["ema20_slope_pct"] = float((ema20[-1] - ema20[-4]) / ema20[-4] * 100)
    if not np.isnan(ema50[-1]):
        base["ema50"] = float(ema50[-1])
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    if not np.isnan(ema26[-1]):
        macd_line = ema12 - ema26
        signal = _ema(macd_line[~np.isnan(macd_line)], 9)
        if len(signal) and not np.isnan(signal[-1]):
            base["macd"] = float(macd_line[-1])
            base["macd_signal"] = float(signal[-1])
            base["macd_histogram"] = float(macd_line[-1] - signal[-1])
    if len(array) >= 14:
        lows, highs = array[-14:, 3], array[-14:, 2]
        span = highs.max() - lows.min()
        base["stochastic"] = float((closes[-1] - lows.min()) / span * 100) if span else 50.0
    if base["atr_pct"] is not None:
        base["volatility_regime"] = ("LOW" if base["atr_pct"] < 0.5 else
                                      "HIGH" if base["atr_pct"] > 2.0 else "NORMAL")
    if base["ema20"] is not None:
        if closes[-1] > base["ema20"] and (base["ema50"] is None or base["ema20"] >= base["ema50"]):
            base["trend"] = "BULLISH"
        elif closes[-1] < base["ema20"] and (base["ema50"] is None or base["ema20"] <= base["ema50"]):
            base["trend"] = "BEARISH"
        else:
            base["trend"] = "NEUTRAL"
    return base


def relative_strength(price: float, reference: float, price_start: float, reference_start: float):
    if not reference or not price_start or not reference_start:
        return None
    return float((price / price_start - 1.0) * 100 - (reference / reference_start - 1.0) * 100)


__all__ = ["build_timeframe_features", "relative_strength"]
