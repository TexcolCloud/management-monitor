from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_RULES_FILE = Path("config/classification_rules.json")


@dataclass(frozen=True)
class KeywordRule:
    name: str
    keywords: tuple[str, ...]

    def matches(self, text: str) -> bool:
        lowered = text.casefold()
        return any(keyword.casefold() in lowered for keyword in self.keywords)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _rules(payload: dict[str, Any], key: str) -> list[KeywordRule]:
    result: list[KeywordRule] = []
    for item in payload.get(key, []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        keywords = tuple(str(v).strip() for v in item.get("keywords", []) if str(v).strip())
        if name and keywords:
            result.append(KeywordRule(name=name, keywords=keywords))
    return result


class DailyManagementClassifier:
    """Classify portal rows using editable keyword rules instead of hard-coded branches."""

    def __init__(self, rules_file: Path = DEFAULT_RULES_FILE) -> None:
        payload = _load_json(rules_file)
        self.category_rules = _rules(payload, "categories")
        self.risk_rules = _rules(payload, "risk_levels")
        self.default_category = str(payload.get("default_category", "其他"))
        self.default_risk = str(payload.get("default_risk", "一般"))
        self.company_prefixes = tuple(payload.get("company_prefixes", ["示例分公司"]))

    def normalize_company(self, value: Any) -> str:
        text = str(value or "").strip()
        for prefix in self.company_prefixes:
            text = re.sub(
                rf"^{re.escape(str(prefix))}(?:[/\\>｜|_\-\s]+)?",
                "",
                text,
            ).strip()
        return text or str(value or "").strip()

    @staticmethod
    def _match(rules: list[KeywordRule], text: str, default: str) -> str:
        for rule in rules:
            if rule.matches(text):
                return rule.name
        return default

    def classify(self, row: dict[str, Any]) -> dict[str, str]:
        merged = " ".join(
            str(row.get(field) or "")
            for field in ("safetyType", "theme", "content", "remark", "companyName")
        )
        return {
            "归类": self._match(self.category_rules, merged, self.default_category),
            "风险等级": self._match(self.risk_rules, merged, self.default_risk),
            "归属单位": self.normalize_company(row.get("companyName")),
        }
