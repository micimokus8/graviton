"""Bestätigte Swing-Hochs/Tiefs und daraus die nächsten Ziele (S/R).

Ein Pivot gilt erst als bestätigt, wenn `k` Kerzen rechts davon geschlossen sind
— kein Repainting. Für den Einstieg zählen nur junge, deutliche Pivots
(`meaningful_swings`): kleine Zacken und alte Hochs aus der ganzen Historie
sind kein Widerstand und kein Stop.
"""
from __future__ import annotations

# Enger als k=3, damit eine 3-Kerzen-Zacke kein Niveau wird.
PIVOT_K = 5
# Nur die letzten Handelstage. Ein Hoch von vor einem Jahr ist kein Ziel.
PIVOT_LOOKBACK = 60
# Der Pivot muss sich vom Fenster mindestens so weit abheben (in ATR).
MIN_PIVOT_ATR = 0.4


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


def meaningful_swings(series: list[float], *, kind: str, atr: float,
                      k: int = PIVOT_K, lookback: int = PIVOT_LOOKBACK,
                      min_prominence_atr: float = MIN_PIVOT_ATR
                      ) -> list[tuple[int, float]]:
    """Junge, deutliche Pivots. `kind` ist 'high' oder 'low'.

    Prominenz = Abstand vom Extrem zum anderen Ende des Fensters. Eine Zacke
    von wenigen Zehntel ATR fällt raus, auch wenn sie formal ein k-Pivot ist.
    """
    if kind not in ("high", "low"):
        raise ValueError(kind)
    if atr is None or not atr > 0 or len(series) < 2 * k + 1:
        return []
    pivots = swing_highs(series, k) if kind == "high" else swing_lows(series, k)
    start = max(0, len(series) - lookback)
    need = min_prominence_atr * atr
    out = []
    for i, v in pivots:
        if i < start:
            continue
        window = series[i - k:i + k + 1]
        prom = (v - min(window)) if kind == "high" else (max(window) - v)
        if prom < need:
            continue
        out.append((i, v))
    return out


def latest_level(levels: list[tuple[int, float]]) -> float | None:
    """Letztes bestätigtes Extrem nach Zeit, auch wenn der Kurs schon darüber
    oder darunter liegt. Das ist der Pullback-Anker — nicht das nächste
    Niveau auf der anderen Seite des Kurses."""
    if not levels:
        return None
    return max(levels, key=lambda iv: iv[0])[1]


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


def target_level(price: float, atr: float, levels: list[tuple[int, float]], *,
                 side: str, min_atr: float, max_atr: float) -> float | None:
    """Strukturelles Ziel aus den übergebenen Pivots, oder None.

    None heißt: kein Niveau im Fenster — der Plan nimmt ein gemessenes
    ATR-Ziel. Ein *anderer* Pivot näher als `min_atr` wird zurückgegeben,
    damit der Plan „zu nah“ ablehnt.

    Der Pivot, von dem der Kurs gerade zurücksetzt, ist bei einem flachen
    Pullback weder Ziel noch Blocker. Sonst ist jedes gesunde Pullback
    „Widerstand zu nah“, und ein frisches Hoch (kein Pivot darüber) kommt
    ohne Prüfung durch.
    """
    if side not in ("LONG", "SHORT"):
        raise ValueError(side)
    if atr is None or not atr > 0 or not levels:
        return None
    impulse = latest_level(levels)
    if side == "LONG":
        ordered = sorted(v for _, v in levels if v > price)

        def dist(v: float) -> float:
            return (v - price) / atr

        shallow = (impulse is not None and impulse > price
                   and dist(impulse) < min_atr)
    else:
        ordered = sorted((v for _, v in levels if v < price), reverse=True)

        def dist(v: float) -> float:
            return (price - v) / atr

        shallow = (impulse is not None and impulse < price
                   and dist(impulse) < min_atr)
    skip = impulse if shallow else None
    blockers = [v for v in ordered if v != skip and dist(v) < min_atr]
    if blockers:
        return blockers[0]
    usable = [v for v in ordered
              if v != skip and min_atr <= dist(v) <= max_atr]
    if usable:
        return usable[0]
    return None


def risk_reward(entry: float, stop: float, target: float | None,
                side: str = "LONG") -> float | None:
    """R:R. None, wenn kein Ziel oder Risiko/Chance nicht positiv sind."""
    if target is None:
        return None
    if side == "SHORT":
        risk = stop - entry
        reward = entry - target
    else:
        risk = entry - stop
        reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk
