#!/usr/bin/env python3
"""
graviton/telegram_sender.py — Direct Telegram Bot API
=======================================================
Sendet Live-Nachrichten via Telegram Bot API.
Token wird aus Umgebungsvariable TELEGRAM_BOT_TOKEN gelesen.
"""

import os, json, urllib.request
from pathlib import Path
from typing import Optional

# ─── Load from Hermes .env ────────────────────────────────────────

_TOKEN = None
_CHAT_ID = None


def _load_config():
    global _TOKEN, _CHAT_ID

    # Try environment first
    _TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    _CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_ALLOWED_USERS", "")

    if not _TOKEN:
        # Load from Hermes .env
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        _, _, v = line.partition("=")
                        _TOKEN = v.strip().strip('"').strip("'")
                    elif line.startswith("TELEGRAM_ALLOWED_USERS="):
                        _, _, v = line.partition("=")
                        ids = v.strip().strip('"').strip("'")
                        if ids and not _CHAT_ID:
                            _CHAT_ID = ids.split(",")[0].strip()

    if not _CHAT_ID and os.getenv("TELEGRAM_HOME_CHANNEL"):
        _CHAT_ID = os.getenv("TELEGRAM_HOME_CHANNEL")


_load_config()


def send(message: str, chat_id: Optional[str] = None) -> bool:
    """
    Sendet eine Telegram-Nachricht.
    Returns True bei Erfolg.
    """
    cid = chat_id or _CHAT_ID
    if not _TOKEN or not cid:
        return False

    try:
        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML",
        }).encode()

        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception:
        return False


# Test
if __name__ == "__main__":
    send("🧪 Graviton Telegram Test — funktioniert!")