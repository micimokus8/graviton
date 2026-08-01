import json
import unittest
from unittest.mock import patch

from telegram_sender import (
    _chat_id_from_users_csv,
    _is_valid_chat_id,
    _mask,
    send,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TelegramConfigTests(unittest.TestCase):
    def test_chat_id_valid_accepts_group_id(self):
        self.assertTrue(_is_valid_chat_id("-1001234567890"))

    def test_chat_id_valid_accepts_user_id(self):
        self.assertTrue(_is_valid_chat_id("1234567"))

    def test_chat_id_invalid_rejects_empty(self):
        self.assertFalse(_is_valid_chat_id(""))

    def test_chat_id_invalid_rejects_random_string(self):
        self.assertFalse(_is_valid_chat_id("@botname"))

    def test_chat_id_from_users_csv_picks_numeric_group(self):
        self.assertEqual(
            _chat_id_from_users_csv("@bot,-1009999,other"),
            "-1009999",
        )

    def test_mask_does_not_leak_token(self):
        self.assertEqual(_mask("abcdef"), "abc***")


class TelegramSendTests(unittest.TestCase):
    def test_send_returns_false_when_token_missing(self):
        with patch("telegram_sender._TOKEN", ""), patch("telegram_sender._CHAT_ID", "1"):
            self.assertFalse(send("x"))

    def test_send_returns_false_when_chat_id_invalid(self):
        with patch("telegram_sender._TOKEN", "k"), patch("telegram_sender._CHAT_ID", "@bad"):
            self.assertFalse(send("x"))

    def test_send_returns_true_on_api_ok(self):
        with patch("telegram_sender._TOKEN", "k"), patch("telegram_sender._CHAT_ID", "1"), \
             patch("telegram_sender.urllib.request.urlopen", return_value=_FakeResponse({"ok": True})):
            self.assertTrue(send("hi"))

    def test_send_returns_false_on_api_error(self):
        with patch("telegram_sender._TOKEN", "k"), patch("telegram_sender._CHAT_ID", "1"), \
             patch("telegram_sender.urllib.request.urlopen", return_value=_FakeResponse({"ok": False, "description": "blocked"})):
            self.assertFalse(send("hi"))

    def test_send_returns_false_on_network_failure(self):
        with patch("telegram_sender._TOKEN", "k"), patch("telegram_sender._CHAT_ID", "1"), \
             patch("telegram_sender.urllib.request.urlopen", side_effect=RuntimeError("boom")):
            self.assertFalse(send("hi"))


if __name__ == "__main__":
    unittest.main()
