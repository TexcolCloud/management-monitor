from __future__ import annotations

from typing import Any


FORMULA_PREFIXES = ("=", "+", "-", "@")


def is_spreadsheet_formula(value: Any) -> bool:
    text = "" if value is None else str(value)
    return text.lstrip().startswith(FORMULA_PREFIXES)


def spreadsheet_safe_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if is_spreadsheet_formula(text) else text
