from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from safety_monitor.domain.time_window import parse_datetime
from safety_monitor.domain.diagnostics import diagnostic_error
from safety_monitor.domain.work_order import (
    attachment_identity,
    deduplicate_work_orders,
    expected_attachments,
    identity_conflicts,
    manifest_attachment_counts,
    merge_work_order,
    missing_critical_fields,
)
from safety_monitor.ports.acquisition import (
    AttachmentFileEvidence,
    CaptureResult,
    CaptureStatistics,
    PortalAcquisitionPort,
    SnapshotCompleteness,
)


ProgressCallback = Callable[[str, dict[str, Any]], None]
SHA256 = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)


def row_datetime(row: dict[str, Any], field: str) -> datetime | None:
    value = row.get(field)
    if not value:
        return None
    try:
        return parse_datetime(str(value))
    except ValueError:
        return None


class AcquisitionService:
    def __init__(
        self,
        portal: PortalAcquisitionPort,
        *,
        max_pages: int = 1000,
        stop_on_empty_page: bool = True,
        validate_total: bool = True,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.portal = portal
        self.max_pages = max_pages
        self.stop_on_empty_page = stop_on_empty_page
        self.validate_total = validate_total
        self.progress = progress or (lambda _event, _context: None)

    @staticmethod
    def _pagination_int(value: Any, *, name: str, fallback: int) -> tuple[int, str]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback, f"invalid {name}: {value!r}"
        if parsed < 0:
            return fallback, f"invalid {name}: {value!r}"
        return parsed, ""

    def capture(
        self,
        *,
        start: datetime,
        end: datetime,
        date_field: str,
        page_size: int,
        base_body: dict[str, Any],
        include_details: bool,
    ) -> CaptureResult:
        first = self.portal.request_page(1, page_size, base_body)
        first_rows = first["rows"]
        page_info = first["pageInfo"]
        total_pages, pages_error = self._pagination_int(
            page_info.get("totalPage"), name="totalPage", fallback=1
        )
        total_pages = total_pages or 1
        source_total, total_error = self._pagination_int(
            page_info.get("totalNumber"), name="totalNumber", fallback=len(first_rows)
        )
        if total_pages > self.max_pages:
            raise RuntimeError(
                f"API reported {total_pages} pages, exceeding configured max_pages={self.max_pages}"
            )

        stats = CaptureStatistics(
            source_total_reported=source_total,
            pages_reported=total_pages,
            pages_fetched=1,
        )
        for reason in (pages_error, total_error):
            if reason:
                stats.incomplete_reasons.append(reason)
        source_rows = list(first_rows)
        self.progress("page", {"page": 1, "totalPages": total_pages, "rows": len(first_rows)})

        if not first_rows and self.stop_on_empty_page and total_pages > 1:
            stats.empty_page_stopped = True
            stats.incomplete_reasons.append("pagination stopped at empty first page")
        pages_to_fetch = 1 if stats.empty_page_stopped else total_pages
        for page_no in range(2, pages_to_fetch + 1):
            page = self.portal.request_page(page_no, page_size, base_body)
            page_rows = page["rows"]
            stats.pages_fetched += 1
            self.progress(
                "page", {"page": page_no, "totalPages": total_pages, "rows": len(page_rows)}
            )
            if not page_rows and self.stop_on_empty_page:
                stats.empty_page_stopped = True
                stats.incomplete_reasons.append(f"pagination stopped at empty page {page_no}")
                break
            source_rows.extend(page_rows)

        stats.source_rows_fetched = len(source_rows)
        unique_rows = deduplicate_work_orders(source_rows)
        stats.unique_rows = len(unique_rows)
        stats.duplicate_rows = len(source_rows) - len(unique_rows)
        stats.total_consistent = stats.unique_rows == source_total and not pages_error and not total_error
        if self.validate_total and not stats.total_consistent:
            stats.incomplete_reasons.append(
                "API total mismatch: "
                f"reported={source_total} fetched={stats.source_rows_fetched} "
                f"unique={stats.unique_rows} duplicates={stats.duplicate_rows}"
            )

        matched_rows: list[dict[str, Any]] = []
        for row in unique_rows:
            value = row_datetime(row, date_field)
            if value is None:
                stats.invalid_date_rows += 1
                stats.incomplete_reasons.append(
                    f"work order {row.get('safetyCode') or row.get('id') or '<unknown>'} "
                    f"has invalid {date_field}"
                )
                continue
            if start <= value <= end:
                matched_rows.append(row)
        stats.matched_rows = len(matched_rows)

        details: list[dict[str, Any]] = []
        failed_details: list[dict[str, Any]] = []
        if include_details:
            stats.details_requested = len(matched_rows)
            for index, row in enumerate(matched_rows, start=1):
                record_id = str(row.get("id") or "").strip()
                if not record_id:
                    stats.details_skipped_missing_id += 1
                    stats.details_failed += 1
                    failed_details.append({**row, "_captureError": "missing record id"})
                    continue
                try:
                    raw_detail = self.portal.request_detail(record_id)
                except RuntimeError as exc:
                    stats.details_failed += 1
                    failed_details.append(
                        {
                            **row,
                            "id": record_id,
                            "_captureError": diagnostic_error(exc),
                        }
                    )
                    continue

                conflicts = identity_conflicts(row, raw_detail)
                merged = merge_work_order(row, raw_detail)
                missing = missing_critical_fields(merged)
                if conflicts or missing:
                    reasons: list[str] = []
                    if conflicts:
                        reasons.append("identity conflict: " + ", ".join(conflicts))
                    if missing:
                        reasons.append("missing critical fields: " + ", ".join(missing))
                    stats.details_failed += 1
                    failed_details.append({**merged, "_captureError": "; ".join(reasons)})
                    continue

                stats.details_succeeded += 1
                details.append(merged)
                self.progress(
                    "detail",
                    {"index": index, "total": len(matched_rows), "recordId": record_id},
                )

        if stats.details_failed:
            stats.incomplete_reasons.append(f"{stats.details_failed} detail records failed")

        return CaptureResult(
            rows=matched_rows,
            details=details,
            failed_details=failed_details,
            request_body=first["requestBody"],
            statistics=stats,
        )


def evaluate_snapshot(
    *,
    list_statistics: Mapping[str, Any],
    list_rows: Sequence[Mapping[str, Any]],
    detail_rows: Sequence[Mapping[str, Any]],
    failed_detail_rows: Sequence[Mapping[str, Any]],
    manifest_rows: Sequence[Mapping[str, Any]],
    attachment_files: Sequence[AttachmentFileEvidence],
    required_files_present: bool = True,
) -> SnapshotCompleteness:
    """Evaluate snapshot evidence without reading files or calling external systems."""
    reasons: list[str] = []
    if not required_files_present:
        reasons.append("one or more required snapshot files are missing or invalid")
    if list_statistics.get("total_consistent") is not True:
        reasons.append("portal list total is not consistent")
    recorded_reasons = list_statistics.get("incomplete_reasons") or []
    if isinstance(recorded_reasons, list) and recorded_reasons:
        reasons.extend(str(reason) for reason in recorded_reasons if str(reason).strip())
    if failed_detail_rows:
        reasons.append(f"{len(failed_detail_rows)} detail records failed")

    detail_by_id = {
        str(row.get("id") or "").strip(): row
        for row in detail_rows
        if str(row.get("id") or "").strip()
    }
    detail_by_code = {
        str(row.get("safetyCode") or "").strip(): row
        for row in detail_rows
        if str(row.get("safetyCode") or "").strip()
    }
    for row in list_rows:
        missing = missing_critical_fields(row)
        identity = str(row.get("safetyCode") or row.get("id") or "<unknown>")
        if missing:
            reasons.append(f"list work order {identity} misses: {', '.join(missing)}")
        detail = detail_by_id.get(str(row.get("id") or "").strip()) or detail_by_code.get(
            str(row.get("safetyCode") or "").strip()
        )
        if detail is None:
            reasons.append(f"work order {identity} has no complete detail")
            continue
        detail_missing = missing_critical_fields(detail)
        if detail_missing:
            reasons.append(f"detail {identity} misses: {', '.join(detail_missing)}")
        conflicts = identity_conflicts(row, detail)
        if conflicts:
            reasons.append(f"detail {identity} conflicts on: {', '.join(conflicts)}")

    if len(detail_rows) != len(list_rows):
        reasons.append(
            f"detail count differs from list count: list={len(list_rows)} details={len(detail_rows)}"
        )

    expectation = expected_attachments(detail_rows)
    reasons.extend(expectation.errors)
    expected_counts = expectation.counts
    manifest_counts = manifest_attachment_counts(manifest_rows)
    if expected_counts != manifest_counts:
        missing_counts = expected_counts - manifest_counts
        unexpected_counts = manifest_counts - expected_counts
        if missing_counts:
            reasons.append(f"attachment manifest misses {sum(missing_counts.values())} entries")
        if unexpected_counts:
            reasons.append(f"attachment manifest has {sum(unexpected_counts.values())} unexpected entries")

    successful_statuses = {"downloaded", "reused", "skipped_duplicate"}
    evidence_by_path = {item.saved_path: item for item in attachment_files}
    for row in manifest_rows:
        identity = attachment_identity(row)
        status = str(row.get("status") or "")
        if status not in successful_statuses:
            reasons.append(f"attachment {identity} has unsuccessful status: {status or '<empty>'}")
        saved_path = str(row.get("savedPath") or "").strip()
        evidence = evidence_by_path.get(saved_path)
        if not saved_path or evidence is None:
            reasons.append(f"attachment {identity} has no file evidence")
            continue
        if not evidence.inside_attachment_root:
            reasons.append(f"attachment path is outside snapshot: {saved_path}")
        if not evidence.exists:
            reasons.append(f"attachment file is missing: {saved_path}")
        declared = evidence.declared_sha256.lower()
        actual = evidence.actual_sha256.lower()
        if not SHA256.fullmatch(declared):
            reasons.append(f"attachment SHA-256 is missing or invalid: {saved_path}")
        elif not actual or actual != declared:
            reasons.append(f"attachment SHA-256 does not match file content: {saved_path}")

    return SnapshotCompleteness(
        complete=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        expected_attachments=sum(expected_counts.values()),
        manifest_attachments=sum(manifest_counts.values()),
    )
