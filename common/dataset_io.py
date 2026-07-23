from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_rows(input_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("rows", [])
    else:
        raise ValueError(f"Invalid dataset object in {input_path}")

    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Invalid rows in {input_path}")
    return rows
