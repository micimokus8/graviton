import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shadow_runner


class ShadowRunnerTests(unittest.TestCase):
    def test_shadow_enrichment_never_changes_effective_bias(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bias_result.json"
            path.write_text(json.dumps([{
                "symbol": "X/USD:USD",
                "bias": "SHORT",
                "deterministic_bias": "SHORT",
                "price": 1.0,
            }]))
            valid = {"llm_bias": "LONG", "llm_confidence": 0.9,
                     "fallback": False, "reason": "valid"}
            with patch.object(shadow_runner, "LLM_BIAS_ENABLED", True), \
                 patch.object(shadow_runner, "decide_bias", return_value=valid):
                self.assertEqual(shadow_runner.enrich(path), 0)
            row = json.loads(path.read_text())[0]
            self.assertEqual(row["llm_bias"], "LONG")
            self.assertEqual(row["effective_bias"], "SHORT")
            self.assertEqual(row["deterministic_bias"], "SHORT")


if __name__ == "__main__":
    unittest.main()
