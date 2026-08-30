import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shadow_runner


class ShadowHistoryTests(unittest.TestCase):
    def test_enrichment_appends_one_historical_record_per_coin(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "bias_result.json"
            history_path = Path(tmp) / "bias_shadow_history.jsonl"
            result_path.write_text(json.dumps([
                {"symbol": "X/USD:USD", "bias": "SHORT", "deterministic_bias": "SHORT", "price": 1.0},
                {"symbol": "Y/USD:USD", "bias": "LONG", "deterministic_bias": "LONG", "price": 2.0},
            ]))
            valid = {"llm_bias": "NOISE", "llm_confidence": 0.8,
                     "fallback": False, "reason": "reversal"}
            ranking = {"ranking": ["X/USD:USD", "Y/USD:USD"], "confidence": 0.8, "fallback": False, "reason": "ok"}
            with patch.object(shadow_runner, "LLM_BIAS_ENABLED", True), \
                 patch.object(shadow_runner, "decide_bias", return_value=valid), \
                 patch.object(shadow_runner, "decide_ranking", return_value=ranking):
                self.assertEqual(shadow_runner.enrich(result_path, history_path), 0)
            records = [json.loads(line) for line in history_path.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual({r["symbol"] for r in records}, {"X/USD:USD", "Y/USD:USD"})
            self.assertTrue(all(r["deterministic_bias"] == r["effective_bias"] for r in records))
            self.assertTrue(all(r["llm_bias"] == "NOISE" for r in records))
            self.assertEqual(records[0]["deterministic_bias_ranking"], ["X/USD:USD", "Y/USD:USD"])
            self.assertEqual(records[0]["llm_bias_ranking"], ["X/USD:USD", "Y/USD:USD"])

    def test_disabled_mode_also_archives_bias_without_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "bias_result.json"
            history_path = Path(tmp) / "bias_shadow_history.jsonl"
            result_path.write_text(json.dumps([{
                "symbol": "X/USD:USD", "bias": "SHORT", "deterministic_bias": "SHORT",
            }]))
            with patch.object(shadow_runner, "LLM_BIAS_ENABLED", False):
                self.assertEqual(shadow_runner.enrich(result_path, history_path), 0)
            record = json.loads(history_path.read_text())
            self.assertEqual(record["llm_bias"], "DISABLED")
            self.assertEqual(record["effective_bias"], "SHORT")


if __name__ == "__main__":
    unittest.main()
