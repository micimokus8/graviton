import json
import unittest

from candidate_ranking import deterministic_rank, parse_ranking


class CandidateRankingTests(unittest.TestCase):
    def test_deterministic_rank_is_transparent_and_prioritizes_strength_then_distance(self):
        candidates = [
            {"symbol": "FAR/USD:USD", "signal_count": 3, "distance_pct": 0.10, "session_vol_ratio": 2.0},
            {"symbol": "NEAR/USD:USD", "signal_count": 3, "distance_pct": 0.30, "session_vol_ratio": 1.0},
            {"symbol": "WEAK/USD:USD", "signal_count": 2, "distance_pct": 0.01, "session_vol_ratio": 3.0},
        ]
        ranked = deterministic_rank(candidates)
        self.assertEqual([row["symbol"] for row in ranked], ["FAR/USD:USD", "NEAR/USD:USD", "WEAK/USD:USD"])
        self.assertIn("rank_score", ranked[0])
        self.assertIn("rank_reasons", ranked[0])

    def test_parse_ranking_requires_exact_candidate_set(self):
        self.assertEqual(
            parse_ranking(json.dumps({"ranking": ["A", "B"], "confidence": 0.8, "reason": "ok"}), ["A", "B"]),
            (["A", "B"], 0.8, "ok"),
        )
        self.assertIsNone(parse_ranking('{"ranking":["A","C"],"confidence":0.8}', ["A", "B"]))
        self.assertIsNone(parse_ranking('{"ranking":["A","A"],"confidence":0.8}', ["A", "B"]))
        self.assertIsNone(parse_ranking('{"ranking":["A","B"]}', ["A", "B"]))
        self.assertIsNone(parse_ranking('{"ranking":["A","B"],"confidence":null}', ["A", "B"]))


if __name__ == "__main__":
    unittest.main()



def _keep_imports_used():
    return json
