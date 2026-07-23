from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_CRITICAL_FIELDS = ("id", "safetyCode", "createTime")


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def work_order_identity(row: Mapping[str, Any]) -> str:
    safety_code = str(row.get("safetyCode") or "").strip()
    if safety_code:
        return f"safetyCode:{safety_code}"
    source_id = str(row.get("id") or "").strip()
    if source_id:
        return f"id:{source_id}"
    return "row:" + json.dumps(
        dict(row),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def deduplicate_work_orders(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = work_order_identity(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(row))
    return unique


def merge_work_order(
    list_row: Mapping[str, Any],
    detail_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge detail fields without erasing non-empty values from the list row."""
    merged = dict(list_row)
    for key, value in detail_row.items():
        if is_blank(value) and not is_blank(merged.get(key)):
            continue
        merged[key] = value
    return merged


def identity_conflicts(
    list_row: Mapping[str, Any],
    detail_row: Mapping[str, Any],
) -> list[str]:
    conflicts: list[str] = []
    for field in ("id", "safetyCode"):
        list_value = str(list_row.get(field) or "").strip()
        detail_value = str(detail_row.get(field) or "").strip()
        if list_value and detail_value and list_value != detail_value:
            conflicts.append(field)
    return conflicts


def missing_critical_fields(
    row: Mapping[str, Any],
    critical_fields: Sequence[str] = DEFAULT_CRITICAL_FIELDS,
) -> list[str]:
    return [field for field in critical_fields if is_blank(row.get(field))]


def _attachment_files(value: Any) -> tuple[list[Any], str]:
    if value in (None, ""):
        return [], ""
    if isinstance(value, list):
        return value, ""
    if isinstance(value, dict):
        return [value], ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [], "attachment url is not valid JSON"
        if isinstance(parsed, list):
            return parsed, ""
        if isinstance(parsed, dict):
            return [parsed], ""
        return [], "attachment url JSON must be an object or list"
    return [], "attachment url has an unsupported type"


def _normalized_attachment_file(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    file_path = str(value or "")
    return {
        "fileOldName": PurePath(file_path.split("?", 1)[0].split("#", 1)[0]).name,
        "filePath": file_path,
        "type": "",
    }


def attachment_identity(value: Mapping[str, Any]) -> str:
    source = str(value.get("id") or value.get("safetyCode") or "")
    parts = [
        source,
        str(value.get("fileType") or ""),
        str(value.get("fileId") or ""),
        str(value.get("filePath") or ""),
        str(value.get("fileOldName") or ""),
        str(value.get("type") or ""),
    ]
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class AttachmentExpectation:
    identities: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def counts(self) -> Counter[str]:
        return Counter(self.identities)


def expected_attachments(rows: Iterable[Mapping[str, Any]]) -> AttachmentExpectation:
    identities: list[str] = []
    errors: list[str] = []
    for row_index, row in enumerate(rows):
        groups = row.get("dailySafetyFiles") or []
        if not isinstance(groups, list):
            errors.append(f"row {row_index}: dailySafetyFiles must be a list")
            continue
        for group_index, raw_group in enumerate(groups):
            if not isinstance(raw_group, Mapping):
                errors.append(f"row {row_index} group {group_index}: attachment group must be an object")
                continue
            files, error = _attachment_files(raw_group.get("url"))
            if error:
                errors.append(f"row {row_index} group {group_index}: {error}")
                continue
            for raw_file in files:
                file = _normalized_attachment_file(raw_file)
                identities.append(
                    attachment_identity(
                        {
                            "id": row.get("id"),
                            "safetyCode": row.get("safetyCode"),
                            "fileType": raw_group.get("fileType"),
                            "fileId": raw_group.get("fileId"),
                            "filePath": file.get("filePath"),
                            "fileOldName": file.get("fileOldName"),
                            "type": file.get("type"),
                        }
                    )
                )
    return AttachmentExpectation(tuple(identities), tuple(errors))


def manifest_attachment_counts(rows: Iterable[Mapping[str, Any]]) -> Counter[str]:
    return Counter(attachment_identity(row) for row in rows)
