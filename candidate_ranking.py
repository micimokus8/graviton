"""Transparent candidate ranking and isolated LLM shadow challenger."""
from __future__ import annotations

import json
import math
import urllib.request
from typing import Any, Callable


def _number(row: dict, key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def rank_score(row: dict) -> tuple[float, list[str]]:
    """Score an already eligible candidate; hard filters happen elsewhere."""
    signals = _number(row, "signal_count")
    volume = max(0.0, min(_number(row, "session_vol_ratio"), 3.0))
    distance = _number(row, "distance_pct", 0.0)
    score = signals * 100.0 + volume * 2.0 - distance * 5.0
    reasons = [f"signale={int(signals)}", f"volumen={volume:.2f}x"]
    if "distance_pct" in row:
        reasons.append(f"ema_distanz={distance:.3f}%")
    return round(score, 6), reasons


def deterministic_rank(candidates: list[dict]) -> list[dict]:
    """Attach explainable score and return candidates in deterministic order."""
    enriched = []
    for index, candidate in enumerate(candidates):
        row = dict(candidate)
        row["rank_score"], row["rank_reasons"] = rank_score(row)
        enriched.append((row, index))
    enriched.sort(key=lambda item: (-item[0]["rank_score"], item[1]))
    return [row for row, _ in enriched]


def parse_ranking(raw: str, symbols: list[str]):
    """Accept only a complete, duplicate-free permutation of symbols."""
    try:
        text = str(raw).strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        value = json.loads(text)
        ranking = value.get("ranking")
        if "confidence" not in value or value["confidence"] is None:
            return None
        confidence = float(value["confidence"])
        reason = str(value.get("reason", ""))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(ranking, list) or ranking != list(dict.fromkeys(ranking)):
        return None
    if set(ranking) != set(symbols) or len(ranking) != len(symbols):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return ranking, confidence, reason


def decide_ranking(
    candidates: list[dict], *, api_key: str, model: str,
    base_url: str = "https://openrouter.ai/api/v1",
    requester: Callable[..., Any] | None = None, timeout: float = 30.0,
) -> dict:
    """Return an LLM ranking for shadow comparison only."""
    symbols = [str(row.get("symbol", "")) for row in candidates]
    result = {"ranking": [], "confidence": 0.0, "reason": "LLM nicht verfügbar", "fallback": True}
    if not api_key or not model or not symbols:
        return result
    _RANKING_SYSTEM = (
        "Du bist ein Krypto-Trading-Analyst. Du erhältst Kandidaten mit technischen "
        "Indikatoren (EMA20/50, RSI, MACD, Stochastic, ATR, Volatilität) auf 15m/30m/1h "
        "sowie BTC-Kontext und Relative-Stärke-Werten. Ordne die Kandidaten nach "
        "Trendstärke und Momentum-Qualität: stärkster, klarster Trend zuerst. "
        "Berücksichtige EMA-Ausrichtung über Timeframes, MACD-Histogramm-Vorzeichen, "
        "RSI-Niveau (nicht überkauft/überverkauft) und Relative Strength vs BTC. "
        "Setze confidence auf 0.5-0.95 je nach Klarheit der Trennung. "
        "Antworte ausschließlich als JSON: "
        '{"ranking":["SYM1/USD:USD","SYM2/USD:USD"],'
        '"confidence":0.8,"reason":"1-2 Sätze"}'
    )
    payload = {"model": model, "temperature": 0.0, "max_tokens": 240,
               "messages": [{"role": "system", "content": _RANKING_SYSTEM},
                 {"role": "user", "content": json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))}]}
    if requester is None:
        def requester(url: str, *, headers: dict, timeout: float, payload: dict):
            request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                headers={**headers, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
    try:
        response = requester(f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout, payload=payload)
        parsed = parse_ranking(response["choices"][0]["message"]["content"], symbols)
    except Exception as exc:
        result["reason"] = f"LLM-Fehler: {type(exc).__name__}"
        return result
    if parsed is None:
        result["reason"] = "LLM-Rangfolge ungültig"
        return result
    ranking, confidence, reason = parsed
    return {"ranking": ranking, "confidence": confidence, "reason": reason, "fallback": False}


__all__ = ["decide_ranking", "deterministic_rank", "parse_ranking", "rank_score"]
