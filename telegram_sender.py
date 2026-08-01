#!/usr/bin/env python3
"""
graviton/telegram_sender.py — Direct Telegram Bot API
=======================================================
Sendet Live-Nachrichten via Telegram Bot API.
Token wird aus Umgebungsvariable TELEGRAM_BOT_TOKEN gelesen.
"""

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger("graviton.telegram")


_TOKEN: str = ""
_CHAT_ID: str = ""


def _is_valid_chat_id(value: str) -> bool:
    """Group IDs sind negativ und bestehen aus Ziffern mit führendem Minus."""
    if not value:
        return False
    stripped = value.removeprefix("-") if value.startswith("-") else value
    return stripped.isdigit()


def _chat_id_from_users_csv(value: str) -> str:
    """Return the first Telegram numeric chat id from TELEGRAM_ALLOWED_USERS."""
    if not value:
        return ""
    candidate = ""
    for raw in value.split(","):
        token = raw.strip()
        if token.lstrip("-").isdigit():
            candidate = token
            break
    return candidate if _is_valid_chat_id(candidate) else ""


def _read_hermes_env() -> tuple[str, str]:
    """Best-effort fallback read of Hermes .env without overriding active ENV."""
    env_path = Path.home() / ".hermes" / ".env"
    token = ""
    users = ""
    if not env_path.exists():
        return token, users
    try:
        with open(env_path) as file_handle:
            for raw_line in file_handle:
                line = raw_line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    _, _, v = line.partition("=")
                    token = v.strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_ALLOWED_USERS="):
                    _, _, v = line.partition("=")
                    users = v.strip().strip('"').strip("'")
    except OSError as exc:
        _LOG.debug("Konnte Hermes-.env nicht lesen: %s", exc)
    return token, users


def _load_config() -> None:
    global _TOKEN, _CHAT_ID

    _TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    _CHAT_ID = (
        os.getenv("TELEGRAM_CHAT_ID", "")
        or _chat_id_from_users_csv(os.getenv("TELEGRAM_ALLOWED_USERS", ""))
        or os.getenv("TELEGRAM_HOME_CHANNEL", "")
    )

    if _TOKEN and _is_valid_chat_id(_CHAT_ID):
        return

    fallback_token, fallback_users = _read_hermes_env()
    if not _TOKEN and fallback_token:
        _TOKEN = fallback_token
    if not _is_valid_chat_id(_CHAT_ID) and fallback_users:
        _CHAT_ID = _chat_id_from_users_csv(fallback_users)
        if not _is_valid_chat_id(_CHAT_ID):
            _CHAT_ID = os.getenv("TELEGRAM_HOME_CHANNEL", "")


_load_config()


def _mask(value: str) -> str:
    """Return a value safe for logging — empty when missing, else masked prefix."""
    if not value:
        return "<unset>"
    if len(value) <= 4:
        return "****"
    return value[:3] + "***"


def send(message: str, chat_id: Optional[str] = None) -> bool:
    """
    Sendet eine Telegram-Nachricht.
    Returns True bei Erfolg, False bei Fehler oder fehlender Konfiguration.
    """
    cid = chat_id or _CHAT_ID
    if not _TOKEN or not _is_valid_chat_id(cid):
        _LOG.warning(
            "Skip Telegram: token=%s chat_id=%s",
            _mask(_TOKEN), _mask(cid),
        )
        return False

    try:
        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML",
        }).encode()

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return True
            _LOG.warning("Telegram API Antwort %s: %s",
                        _mask(cid), result.get("description"))
            return False
    except Exception as exc:
        _LOG.warning("Telegram-Versand fehlgeschlagen: %s", exc)
        return False


if __name__ == "__main__":
    send("🧪 Graviton Telegram Test — funktioniert!")
