import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
import os

from atomic_json import atomic_write_json
from session import _entry_deadline, _state_file_fresh, _state_symbols_match


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

    def test_entry_deadline_is_bias_write_plus_fifteen_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bias_result.json"
            path.write_text("[]")
            bias_ts = datetime(2026, 8, 7, 13, 45, tzinfo=timezone.utc).timestamp()
            os.utime(path, (bias_ts, bias_ts))
            close_dt = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
            self.assertEqual(
                _entry_deadline(path, close_dt),
                datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
            )

    def test_entry_deadline_never_exceeds_session_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bias_result.json"
            path.write_text("[]")
            bias_ts = datetime(2026, 8, 7, 15, 55, tzinfo=timezone.utc).timestamp()
            os.utime(path, (bias_ts, bias_ts))
            close_dt = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
            self.assertEqual(_entry_deadline(path, close_dt), close_dt)


if __name__ == "__main__":
    unittest.main()
