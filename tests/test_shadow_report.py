import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shadow_report import build_report


class ShadowReportTests(unittest.TestCase):
    def test_report_counts_deviations_and_entries_in_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            trades = root / "trades.jsonl"
            history.write_text("\n".join(json.dumps(row) for row in [
                {"timestamp": "2026-08-22T12:00:00+00:00", "symbol": "A/USD:USD", "deterministic_bias": "SHORT", "llm_bias": "NOISE", "llm_confidence": 0.8},
                {"timestamp": "2026-08-22T12:01:00+00:00", "symbol": "B/USD:USD", "deterministic_bias": "LONG", "llm_bias": "LONG", "llm_confidence": 0.9},
                {"timestamp": "2026-08-30T12:00:00+00:00", "symbol": "C/USD:USD", "deterministic_bias": "SHORT", "llm_bias": "LONG", "llm_confidence": 0.9},
            ]) + "\n")
            trades.write_text(json.dumps({"timestamp": "2026-08-22T12:02:00+00:00", "event": "entry", "symbol": "A/USD:USD"}) + "\n")
            report = build_report(history, datetime(2026, 8, 22, tzinfo=timezone.utc), datetime(2026, 8, 23, tzinfo=timezone.utc), trades)
            self.assertEqual(report["llm_comparisons"], 2)
            self.assertEqual(report["deviations"], 1)
            self.assertEqual(report["deviation_rate_pct"], 50.0)
            self.assertEqual(report["trade_entries"], 1)
            self.assertEqual(report["deviation_details"][0]["symbol"], "A/USD:USD")


if __name__ == "__main__":
    unittest.main()
