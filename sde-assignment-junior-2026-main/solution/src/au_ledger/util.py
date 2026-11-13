"""Small shared helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    """Return a sortable UTC timestamp with an explicit offset."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    """Stable compact JSON for persisted metadata and attributes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
