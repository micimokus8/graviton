"""Optional LLM interpretation for Graviton bias snapshots.

The LLM is a conservative filter only: it may return LONG, SHORT or NOISE.
Invalid responses and transport errors fail closed to the deterministic bias.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, Any

SUPPORTED = {"LONG", "SHORT", "NOISE"}

_SYSTEM = (
    "Du bist ein vorsichtiger Intraday-Krypto-Analyst. Bewerte ausschließlich die "
    "gelieferten technischen Kennzahlen. Erfinde keine Werte. Berücksichtige "
    "Trend, Momentum, RSI, MACD, Stochastic, Support/Resistance, Überdehnung, "
    "Pullback und BTC-Kontext. Wenn Signale widersprüchlich, zu spät oder noisy "
    "sind, antworte NOISE. Setze confidence auf 0.5-0.95 je nach Klarheit. "
    "Gib ausschließlich valides JSON zurück: "
    '{"decision":"LONG|SHORT|NOISE","confidence":0.8,'
    '"reason":"1-2 Sätze Begründung"}'
)


def parse_decision(raw: str):
    """Parse and validate the small LLM contract.

    OpenRouter-compatible models sometimes wrap valid JSON in a Markdown
    code fence; unwrap that presentation layer before parsing.
    """
    try:
        text = str(raw).strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        value = json.loads(text)
        decision = str(value.get("decision", "")).upper()
        confidence = float(value.get("confidence", 0.0))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    if decision not in SUPPORTED or not 0.0 <= confidence <= 1.0:
        return None
    return decision, confidence


def _default_requester(url: str, *, headers: dict, timeout: float, payload: dict):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def decide_bias(
    snapshot: dict,
    *,
    api_key: str,
    model: str,
    base_url: str = "https://openrouter.ai/api/v1",
    requester: Callable[..., Any] = _default_requester,
    timeout: float = 30.0,
    min_confidence: float = 0.65,
) -> dict:
    """Return LLM result and final bias, falling back safely on errors."""
    deterministic = str(snapshot.get("deterministic_bias", "NOISE")).upper()
    if deterministic not in SUPPORTED:
        deterministic = "NOISE"
    result = {
        "deterministic_bias": deterministic,
        "llm_bias": "ERROR",
        "llm_confidence": 0.0,
        "final_bias": deterministic,
        "fallback": True,
        "reason": "LLM nicht verfügbar",
    }
    if not api_key or not model:
        return result
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 180,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))},
        ],
    }
    try:
        response = requester(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            payload=payload,
        )
        raw = response["choices"][0]["message"]["content"]
        parsed = parse_decision(raw)
    except Exception as exc:
        result["reason"] = f"LLM-Fehler: {type(exc).__name__}"
        return result
    if parsed is None:
        result["reason"] = "LLM-Antwort ungültig"
        return result
    decision, confidence = parsed
    result.update({
        "llm_bias": decision,
        "llm_confidence": confidence,
        "fallback": False,
        "reason": "valid",
    })
    if confidence >= min_confidence:
        result["final_bias"] = decision
    else:
        result["final_bias"] = "NOISE"
        result["reason"] = f"Konfidenz unter Schwelle ({confidence:.2f} < {min_confidence:.2f})"
    return result


def enabled() -> bool:
    return os.getenv("LLM_BIAS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["decide_bias", "parse_decision", "enabled"]
