"""Append-only decision log for later calibration (Brier, log loss).

Each line is one decision: the inputs that produced fair value, the book,
the action, and the reason. API keys, signatures, and PEM material are
dropped if they ever appear in a nested dict.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_key_id",
        "kalshi_api_key_id",
        "private_key",
        "private_key_pem",
        "pem",
        "signature",
        "authorization",
        "secret",
        "token",
        "password",
    }
)


def _scrub(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                continue
            cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, float):
        return round(value, 8)
    return value


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, record: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = _scrub(dict(record))
        body.setdefault("ts", datetime.now(UTC).isoformat())
        line = json.dumps(body, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return body
