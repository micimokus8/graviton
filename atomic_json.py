"""Small atomic JSON writer for cross-process Graviton state files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """Write a complete JSON document and atomically replace the destination."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")

    try:
        with open(temporary, "w") as handle:
            json.dump(data, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
