from __future__ import annotations

from typing import Iterable, Tuple

import numpy as np


SESSION_OPEN_GRACE_DEFAULT = 1.5


def _session_volume_ratio(
    candles: np.ndarray,
    session_open_ts: int,
    now_ms: int,
    timeframe_ms: int = 15 * 60_000,
    session_lookback: int = 3,
    baseline_lookback: int = 20,
    first_candle_multiplier: float = SESSION_OPEN_GRACE_DEFAULT,
) -> Tuple[list[float], list[float], float]:
    """Return baseline candles, session candles and a robust volume ratio.

    Ratio is median(session) / median(baseline) where both windows are
    restricted to fully closed candles. While there is no closed
    session-candle yet, the ratio is reported as ``first_candle_multiplier``
    so the bias filter does not over-block the first 15 minutes of a
    fresh session.
    """
    if candles.ndim != 2 or candles.shape[1] < 6:
        raise ValueError("candles must have at least 6 columns")

    opens = candles[:, 0].astype(float)
    volumes = candles[:, 5].astype(float)

    closed_mask = opens + timeframe_ms <= now_ms
    closed = candles[closed_mask]

    if closed.shape[0] == 0:
        return [], [], float(first_candle_multiplier)

    pre_session_mask = opens < session_open_ts
    baseline_vols = volumes[pre_session_mask][-baseline_lookback:].tolist()

    session_mask = (opens >= session_open_ts) & ((opens + timeframe_ms) <= now_ms)
    session_vols = volumes[session_mask][-session_lookback:].tolist()

    if not session_vols:
        return baseline_vols, [], float(first_candle_multiplier)

    session_median = float(np.median(session_vols))
    if not baseline_vols:
        baseline_vols = session_vols
        baseline_median = session_median
    else:
        baseline_median = float(np.median(baseline_vols))

    if baseline_median <= 0:
        return baseline_vols, session_vols, float(first_candle_multiplier)

    ratio = session_median / baseline_median
    return baseline_vols, session_vols, ratio
