"""OHLCV helpers shared by all signal and monitoring paths."""

from __future__ import annotations

import time
from typing import Iterable

import numpy as np


_TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    "1M": 30 * 24 * 60 * 60_000,
}


def closed_ohlcv(
    candles: Iterable,
    timeframe: str,
    now_ms: int | None = None,
) -> np.ndarray:
    """Return only candles whose interval has fully closed.

    CCXT timestamps are candle-open timestamps in milliseconds. A candle is
    closed only when ``open_timestamp + timeframe`` is no later than the
    reference time. Keeping the reference injectable makes this deterministic
    in tests and avoids accidentally using the current, repainting candle.
    """
    try:
        interval_ms = _TIMEFRAME_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"Unsupported OHLCV timeframe: {timeframe}") from exc

    array = np.asarray(candles, dtype=float)
    if array.size == 0:
        return np.empty((0, 6), dtype=float)
    if array.ndim != 2 or array.shape[1] < 6:
        raise ValueError("OHLCV data must be a 2D array with at least 6 columns")

    reference_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    timestamps = array[:, 0]
    mask = np.isfinite(timestamps) & ((timestamps + interval_ms) <= reference_ms)
    return array[mask]
