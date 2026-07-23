from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class PortalAcquisitionPort(Protocol):
    def request_page(
        self,
        page_no: int,
        page_size: int,
        base_body: dict[str, Any],
    ) -> dict[str, Any]: ...

    def request_detail(self, record_id: str) -> dict[str, Any]: ...


@dataclass
class CaptureStatistics:
    source_total_reported: int = 0
    source_rows_fetched: int = 0
    unique_rows: int = 0
    duplicate_rows: int = 0
    matched_rows: int = 0
    pages_reported: int = 0
    pages_fetched: int = 0
    empty_page_stopped: bool = False
    total_consistent: bool = True
    invalid_date_rows: int = 0
    details_requested: int = 0
    details_succeeded: int = 0
    details_failed: int = 0
    details_skipped_missing_id: int = 0
    incomplete_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureResult:
    rows: list[dict[str, Any]]
    details: list[dict[str, Any]]
    failed_details: list[dict[str, Any]]
    request_body: dict[str, Any]
    statistics: CaptureStatistics


@dataclass(frozen=True)
class SnapshotCompleteness:
    complete: bool
    reasons: tuple[str, ...]
    expected_attachments: int = 0
    manifest_attachments: int = 0


@dataclass(frozen=True)
class AttachmentFileEvidence:
    saved_path: str
    exists: bool
    inside_attachment_root: bool
    declared_sha256: str
    actual_sha256: str
