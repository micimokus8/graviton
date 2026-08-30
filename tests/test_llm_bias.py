import json
import unittest

from llm_bias import decide_bias, parse_decision


class LlmBiasTests(unittest.TestCase):
    def test_parse_accepts_only_supported_decision(self):
        self.assertEqual(parse_decision('{"decision":"LONG","confidence":0.8}'), ("LONG", 0.8))
        self.assertEqual(parse_decision('{"decision":"NOISE","confidence":0.55}'), ("NOISE", 0.55))
        self.assertIsNone(parse_decision('{"decision":"BUY","confidence":0.9}'))

    def test_parse_accepts_markdown_json_fence(self):
        raw = '```json\n{"decision":"SHORT","confidence":0.9,"reason":"test"}\n```'
        self.assertEqual(parse_decision(raw), ("SHORT", 0.9))

    def test_invalid_llm_response_keeps_deterministic_bias(self):
        result = decide_bias(
            {"symbol": "X/USD:USD", "deterministic_bias": "SHORT"},
            api_key="key",
            model="model",
            requester=lambda **kwargs: {"choices": [{"message": {"content": "not json"}}]},
        )
        self.assertEqual(result["final_bias"], "SHORT")
        self.assertEqual(result["llm_bias"], "ERROR")
        self.assertEqual(result["fallback"], True)

    def test_valid_noise_can_filter_deterministic_candidate(self):
        result = decide_bias(
            {"symbol": "X/USD:USD", "deterministic_bias": "SHORT", "features": {"btc_30m": 0.4}},
            api_key="key",
            model="model",
            requester=lambda url, **kwargs: {"choices": [{"message": {"content": json.dumps({"decision": "NOISE", "confidence": 0.82, "reason": "reversal risk"})}}]},
        )
        self.assertEqual(result["final_bias"], "NOISE")
        self.assertEqual(result["llm_bias"], "NOISE")
        self.assertFalse(result["fallback"])


if __name__ == "__main__":
    unittest.main()


def _keep_imports_used():
    return json
