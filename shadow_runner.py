"""Out-of-process LLM shadow enrichment for bias_result.json."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from atomic_json import atomic_write_json
from config import (
    LLM_BIAS_API_KEY,
    LLM_BIAS_BASE_URL,
    LLM_BIAS_ENABLED,
    LLM_BIAS_MIN_CONFIDENCE,
    LLM_BIAS_MODEL,
)
from llm_bias import decide_bias
from candidate_ranking import decide_ranking, deterministic_rank


def enrich(path: str | Path, history_path: str | Path = "data/bias_shadow_history.jsonl") -> int:
    """Enrich and archive a snapshot; never changes effective_bias."""
    target = Path(path)
    with target.open() as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        return 2
    enriched = []
    for row in rows:
        deterministic = row.get("deterministic_bias", row.get("bias", "NOISE"))
        if LLM_BIAS_ENABLED:
            result = decide_bias(
                {
                    "symbol": row.get("symbol"),
                    "deterministic_bias": deterministic,
                    "price": row.get("price"),
                    "reason": row.get("reason", ""),
                    "green": row.get("green", 0),
                    "red": row.get("red", 0),
                    "signal_count": row.get("signal_count", 0),
                    "session_vol_ratio": row.get("session_vol_ratio", 0.0),
                    "features": row.get("features", {}),
                },
                api_key=LLM_BIAS_API_KEY,
                model=LLM_BIAS_MODEL,
                base_url=LLM_BIAS_BASE_URL,
                min_confidence=LLM_BIAS_MIN_CONFIDENCE,
            )
        else:
            result = {"llm_bias": "DISABLED", "llm_confidence": 0.0,
                      "fallback": False, "reason": "LLM deaktiviert"}
        row.update(
            {
                "llm_bias": result["llm_bias"],
                "llm_confidence": result["llm_confidence"],
                "llm_fallback": result["fallback"],
                "llm_reason": result["reason"],
                "effective_bias": deterministic,
                "deterministic_bias": deterministic,
                "llm_enabled": True,
            }
        )
        enriched.append(row)

    # Ranking only compares candidates that already passed deterministic bias.
    # It is persisted for analysis and never controls session execution.
    ranked = deterministic_rank([
        row for row in enriched
        if row.get("deterministic_bias", row.get("bias")) in ("LONG", "SHORT")
    ])
    # At this stage the 1m/5m pullback is not known yet. This is deliberately
    # called a bias/SR pre-ranking, not a final entry ranking. The session's
    # live EMA-distance ordering remains authoritative for execution.
    if LLM_BIAS_ENABLED and len(ranked) >= 2:
        ranking_result = decide_ranking(
            ranked,
            api_key=LLM_BIAS_API_KEY,
            model=LLM_BIAS_MODEL,
            base_url=LLM_BIAS_BASE_URL,
            timeout=8.0,
        )
    else:
        ranking_result = {
            "ranking": [], "confidence": 0.0,
            "fallback": True,
            "reason": "LLM deaktiviert oder weniger als 2 Kandidaten",
        }
    ranking_fields = {
        "deterministic_bias_ranking": [row["symbol"] for row in ranked],
        "llm_bias_ranking": ranking_result["ranking"],
        "llm_bias_ranking_confidence": ranking_result["confidence"],
        "llm_bias_ranking_fallback": ranking_result["fallback"],
        "llm_bias_ranking_reason": ranking_result["reason"],
    }
    for row in enriched:
        row.update(ranking_fields)

    # Keep the current snapshot enriched for inspection; the deterministic
    # effective_bias field is preserved above.
    atomic_write_json(target, enriched)
    history = Path(history_path)
    history.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with history.open("a") as handle:
        for row in enriched:
            record = {
                "timestamp": timestamp,
                "symbol": row.get("symbol"),
                "deterministic_bias": row.get("deterministic_bias"),
                "llm_bias": row.get("llm_bias"),
                "llm_confidence": row.get("llm_confidence", 0.0),
                "effective_bias": row.get("effective_bias"),
                "llm_fallback": row.get("llm_fallback", False),
                "llm_reason": row.get("llm_reason", ""),
                "features": row.get("features", {}),
                "deterministic_bias_ranking": row.get("deterministic_bias_ranking", []),
                "llm_bias_ranking": row.get("llm_bias_ranking", []),
                "llm_bias_ranking_confidence": row.get("llm_bias_ranking_confidence", 0.0),
                "llm_bias_ranking_fallback": row.get("llm_bias_ranking_fallback", True),
                "llm_bias_ranking_reason": row.get("llm_bias_ranking_reason", ""),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(enrich(sys.argv[1] if len(sys.argv) > 1 else "data/bias_result.json"))
    except Exception as exc:
        print(f"LLM shadow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
