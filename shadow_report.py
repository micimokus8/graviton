"""Read-only weekly report for deterministic vs LLM shadow bias."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_report(history_path: str | Path, start: datetime, end: datetime,
                 trade_path: str | Path | None = None) -> dict:
    records = []
    with Path(history_path).open() as handle:
        for line in handle:
            try:
                row = json.loads(line)
                ts = _parse(row["timestamp"])
                if start <= ts < end:
                    records.append(row)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    compared = [r for r in records if r.get("llm_bias") not in (None, "DISABLED", "ERROR")]
    deviations = [r for r in compared if r.get("llm_bias") != r.get("deterministic_bias")]
    result = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "records": len(records),
        "llm_comparisons": len(compared),
        "deviations": len(deviations),
        "deviation_rate_pct": round(len(deviations) / len(compared) * 100, 2) if compared else 0.0,
        "deterministic_counts": dict(Counter(r.get("deterministic_bias") for r in records)),
        "llm_counts": dict(Counter(r.get("llm_bias") for r in compared)),
        "deviation_details": [
            {"timestamp": r.get("timestamp"), "symbol": r.get("symbol"),
             "deterministic_bias": r.get("deterministic_bias"),
             "llm_bias": r.get("llm_bias"),
             "confidence": r.get("llm_confidence", 0.0),
             "reason": r.get("llm_reason", "")}
            for r in deviations
        ],
    }
    if trade_path:
        entries = []
        with Path(trade_path).open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if row.get("event") == "entry" and start <= _parse(row["timestamp"]) < end:
                        entries.append(row)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        result["trade_entries"] = len(entries)
        result["trade_symbols"] = sorted({r.get("symbol") for r in entries})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default="data/bias_shadow_history.jsonl")
    parser.add_argument("--trades", default="data/trade_log.jsonl")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    print(json.dumps(build_report(args.history, _parse(args.start), _parse(args.end), args.trades),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
