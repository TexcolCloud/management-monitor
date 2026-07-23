from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from safety_monitor.application.acquire_snapshot import evaluate_snapshot
from safety_monitor.ports.acquisition import AttachmentFileEvidence, SnapshotCompleteness


TRANSACTION_MARKER_VERSION = 1
TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _rows(payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if payload is None:
        return None
    value = payload.get("rows")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return None
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_snapshot(path: Path, attachment_dir_name: str = "daily-attachments") -> SnapshotCompleteness:
    list_payload = _json_object(path / "management-api-all.json")
    details_payload = _json_object(path / "management-api-details.json")
    failures_payload = _json_object(path / "management-api-detail-failures.json")
    manifest_payload = _json_object(path / "daily-attachments-manifest.json")
    list_rows = _rows(list_payload)
    detail_rows = _rows(details_payload)
    failed_rows = _rows(failures_payload)
    manifest_rows = _rows(manifest_payload)
    required_present = all(
        value is not None for value in (list_payload, list_rows, detail_rows, failed_rows, manifest_rows)
    )
    list_statistics = list_payload.get("statistics", {}) if list_payload else {}
    if not isinstance(list_statistics, dict):
        list_statistics = {}

    attachment_root = (path / attachment_dir_name).resolve()
    evidence: list[AttachmentFileEvidence] = []
    for row in manifest_rows or []:
        saved_path = str(row.get("savedPath") or "").strip()
        candidate = (path / saved_path).resolve() if saved_path else path.resolve()
        try:
            candidate.relative_to(attachment_root)
            inside = True
        except ValueError:
            inside = False
        exists = inside and candidate.is_file()
        evidence.append(
            AttachmentFileEvidence(
                saved_path=saved_path,
                exists=exists,
                inside_attachment_root=inside,
                declared_sha256=str(row.get("sha256") or "").strip(),
                actual_sha256=_sha256(candidate) if exists else "",
            )
        )

    return evaluate_snapshot(
        list_statistics=list_statistics,
        list_rows=list_rows or [],
        detail_rows=detail_rows or [],
        failed_detail_rows=failed_rows or [],
        manifest_rows=manifest_rows or [],
        attachment_files=evidence,
        required_files_present=required_present,
    )


@dataclass(frozen=True)
class SnapshotTransaction:
    transaction_id: str
    staging: Path
    current: Path
    backup: Path
    marker: Path


class FilesystemSnapshotPublisher:
    """Publish a directory with a crash-recoverable, marker-backed transaction."""

    def __init__(
        self,
        *,
        validator: Callable[[Path], SnapshotCompleteness] = inspect_snapshot,
        phase_hook: Callable[[str, SnapshotTransaction], None] | None = None,
    ) -> None:
        self.validator = validator
        self.phase_hook = phase_hook or (lambda _phase, _transaction: None)

    @staticmethod
    def marker_path(current: Path) -> Path:
        return current.parent / f".{current.name}-snapshot-transaction.json"

    @staticmethod
    def _write_marker(transaction: SnapshotTransaction, phase: str) -> None:
        payload = {
            "version": TRANSACTION_MARKER_VERSION,
            "transactionId": transaction.transaction_id,
            "phase": phase,
            "staging": str(transaction.staging.resolve()),
            "current": str(transaction.current.resolve()),
            "backup": str(transaction.backup.resolve()),
        }
        temporary = transaction.marker.with_name(
            f"{transaction.marker.name}.{transaction.transaction_id}.tmp"
        )
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(transaction.marker)

    @staticmethod
    def _transaction_from_marker(marker: Path, expected_current: Path) -> tuple[SnapshotTransaction, str]:
        payload = _json_object(marker)
        if payload is None or payload.get("version") != TRANSACTION_MARKER_VERSION:
            raise RuntimeError(f"Snapshot transaction marker is invalid: {marker}")
        transaction_id = str(payload.get("transactionId") or "")
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise RuntimeError(f"Snapshot transaction marker has an invalid transaction id: {marker}")
        transaction = SnapshotTransaction(
            transaction_id=transaction_id,
            staging=Path(str(payload.get("staging") or "")).resolve(),
            current=Path(str(payload.get("current") or "")).resolve(),
            backup=Path(str(payload.get("backup") or "")).resolve(),
            marker=marker.resolve(),
        )
        parent = expected_current.parent.resolve()
        if transaction.current != expected_current.resolve():
            raise RuntimeError("Snapshot marker targets a different current directory")
        for candidate in (transaction.staging, transaction.current, transaction.backup):
            if candidate.parent != parent:
                raise RuntimeError("Snapshot marker path is outside the snapshot parent")
        expected_backup = parent / f".{expected_current.name}-backup-{transaction_id}"
        if transaction.backup != expected_backup:
            raise RuntimeError("Snapshot marker backup path is not owned by this transaction")
        if not transaction.staging.name.startswith(f".{expected_current.name}-staging-"):
            raise RuntimeError("Snapshot marker staging path is not owned by this transaction")
        return transaction, str(payload.get("phase") or "")

    def recover(self, current: Path) -> None:
        current = current.resolve()
        marker = self.marker_path(current)
        if not marker.exists():
            return
        transaction, _phase = self._transaction_from_marker(marker, current)
        current_complete = current.is_dir() and self.validator(current).complete
        if current_complete:
            if transaction.backup.exists():
                shutil.rmtree(transaction.backup)
            marker.unlink(missing_ok=True)
            return

        if current.exists():
            recovered = current.parent / f"partial-attempts/recovered-{transaction.transaction_id}"
            recovered.parent.mkdir(parents=True, exist_ok=True)
            if recovered.exists():
                raise RuntimeError(f"Recovered snapshot path already exists: {recovered}")
            current.replace(recovered)
        if transaction.backup.is_dir():
            transaction.backup.replace(current)
            marker.unlink(missing_ok=True)
            return
        if transaction.staging.is_dir() and self.validator(transaction.staging).complete:
            transaction.staging.replace(current)
            marker.unlink(missing_ok=True)
            return
        raise RuntimeError(
            "Snapshot transaction cannot be recovered automatically; marker and candidate were preserved"
        )

    def publish(self, staging: Path, current: Path) -> None:
        staging = staging.resolve()
        current = current.resolve()
        self.recover(current)
        validation = self.validator(staging)
        if not validation.complete:
            raise RuntimeError("Snapshot is incomplete: " + "; ".join(validation.reasons))

        transaction_id = uuid.uuid4().hex
        transaction = SnapshotTransaction(
            transaction_id=transaction_id,
            staging=staging,
            current=current,
            backup=current.parent / f".{current.name}-backup-{transaction_id}",
            marker=self.marker_path(current),
        )
        self._write_marker(transaction, "prepared")
        self.phase_hook("prepared", transaction)
        try:
            if current.exists():
                current.replace(transaction.backup)
            self._write_marker(transaction, "previous_moved")
            self.phase_hook("previous_moved", transaction)
            staging.replace(current)
            self._write_marker(transaction, "published")
            self.phase_hook("published", transaction)
        except Exception:
            if not current.exists() and transaction.backup.is_dir():
                try:
                    transaction.backup.replace(current)
                    transaction.marker.unlink(missing_ok=True)
                except OSError:
                    # The backup and marker are deliberately retained for the next recovery attempt.
                    pass
            raise

        if transaction.backup.exists():
            shutil.rmtree(transaction.backup)
        transaction.marker.unlink(missing_ok=True)
