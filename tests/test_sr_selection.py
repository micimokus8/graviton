import unittest

from session import _partition_sr_candidates


class SRSelectionTests(unittest.TestCase):
    def test_all_blocked_candidates_produce_no_active_fallback(self):
        candidates = [
            {"symbol": "A/USD:USD", "price": 10.0, "bias": "LONG"},
            {"symbol": "B/USD:USD", "price": 20.0, "bias": "SHORT"},
        ]

        def checker(symbol, price, bias):
            return True, "zu nah", object()

        active, blocked = _partition_sr_candidates(candidates, checker)

        self.assertEqual(active, [])
        self.assertEqual(len(blocked), 2)

    def test_unblocked_candidate_remains_active(self):
        candidates = [
            {"symbol": "A/USD:USD", "price": 10.0, "bias": "LONG"},
            {"symbol": "B/USD:USD", "price": 20.0, "bias": "SHORT"},
        ]

        def checker(symbol, price, bias):
            return symbol.startswith("A/"), "zu nah", object()

        active, blocked = _partition_sr_candidates(candidates, checker)

        self.assertEqual(active, [candidates[1]])
        self.assertEqual(blocked[0][0], candidates[0])


if __name__ == "__main__":
    unittest.main()
