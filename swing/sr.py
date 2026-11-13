"""Bestätigte Swing-Hochs/Tiefs und daraus die nächsten Ziele (S/R).

Ein Pivot gilt erst als bestätigt, wenn `k` Kerzen rechts davon geschlossen sind
— kein Repainting. Widerstände liegen über dem Kurs, Supports darunter.
"""
from __future__ import annotations


def swing_highs(highs: list[float], k: int = 3) -> list[tuple[int, float]]:
    out = []
    for i in range(k, len(highs) - k):
        window = highs[i - k:i + k + 1]
        if highs[i] == max(window) and window.count(highs[i]) == 1:
            out.append((i, highs[i]))
    return out


def swing_lows(lows: list[float], k: int = 3) -> list[tuple[int, float]]:
    out = []
    for i in range(k, len(lows) - k):
        window = lows[i - k:i + k + 1]
        if lows[i] == min(window) and window.count(lows[i]) == 1:
            out.append((i, lows[i]))
    return out


def next_resistance(price: float, levels: list[tuple[int, float]]) -> float | None:
    above = [v for _, v in levels if v > price]
    return min(above) if above else None


def next_support(price: float, levels: list[tuple[int, float]]) -> float | None:
    below = [v for _, v in levels if v < price]
    return max(below) if below else None


def last_swing_low(lows: list[tuple[int, float]], price: float) -> float | None:
    below = [v for _, v in lows if v < price]
    return max(below) if below else None


def last_swing_high(highs: list[tuple[int, float]], price: float) -> float | None:
    above = [v for _, v in highs if v > price]
    return min(above) if above else None


def risk_reward(entry: float, stop: float, target: float | None) -> float | None:
    """R:R für LONG. None, wenn kein Ziel oder Risiko <= 0."""
    if target is None:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    return (target - entry) / risk
