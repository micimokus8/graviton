import tempfile
import unittest
from pathlib import Path

from atomic_json import atomic_write_json
from session import _state_file_fresh, _state_symbols_match


class StateFileTests(unittest.TestCase):
    def test_atomic_json_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, [{"symbol": "X/USD:USD"}])

            self.assertEqual(path.read_text(), '[\n  {\n    "symbol": "X/USD:USD"\n  }\n]')
            self.assertFalse((Path(tmp) / "state.json.tmp").exists())

    def test_state_file_freshness_uses_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("[]")
            mtime = path.stat().st_mtime

            self.assertTrue(_state_file_fresh(path, 60, now_ts=mtime + 30))
            self.assertFalse(_state_file_fresh(path, 60, now_ts=mtime + 61))

    def test_state_symbols_must_match_exactly(self):
        watchlist = [{"symbol": "A/USD:USD"}, {"symbol": "B/USD:USD"}]
        matching = [{"symbol": "B/USD:USD"}, {"symbol": "A/USD:USD"}]
        stale = [{"symbol": "A/USD:USD"}]

        self.assertTrue(_state_symbols_match(watchlist, matching))
        self.assertFalse(_state_symbols_match(watchlist, stale))


if __name__ == "__main__":
    unittest.main()
