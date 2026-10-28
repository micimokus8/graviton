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

    def test_block_depends_on_live_entry_price(self):
        """DASH 28.10.: Snapshot 53.30 -> Block, echter Entry 53.02 -> frei.

        Der S/R-Check darf nur fuer den aktuellen Preis gelten, nicht fuer den
        Preis aus dem Bias-Snapshot. Sonst faellt ein Coin fuer die ganze
        Session aus der Rotation.
        """
        def checker(symbol, price, bias):
            blocked = price > 53.28
            reason = "Support 53.28 zu nah" if blocked else "S/R Abstand OK"
            return blocked, reason, object()

        snapshot = [{"symbol": "DASH/USD:USD", "price": 53.30, "bias": "SHORT"}]
        live = [{"symbol": "DASH/USD:USD", "price": 53.02, "bias": "SHORT"}]

        self.assertEqual(_partition_sr_candidates(snapshot, checker)[0], [])
        self.assertEqual(_partition_sr_candidates(live, checker)[0], live)


if __name__ == "__main__":
    unittest.main()
