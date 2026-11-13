"""Zustand des Swing-Buchs (eine Position, DRY_RUN)."""
from __future__ import annotations

import json
import os

PAPER_STORE = "data/swing_state.json"


def load(path: str = PAPER_STORE) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save(state: dict, path: str = PAPER_STORE) -> None:
    """Atomar schreiben: temp + rename, nie halbfertig."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def new_state() -> dict:
    return {"position": None, "last_entry_date": None, "closed": []}
