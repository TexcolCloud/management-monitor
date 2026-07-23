from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from common.runtime_config import load_runtime_config
from safety_monitor.adapters.filesystem_snapshot import inspect_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_ROOT = load_runtime_config().capture_root

INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def project_path(path: Path | str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def ensure_inside_project(path: Path | str) -> Path:
    resolved = project_path(path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"Path is outside project root: {resolved}") from exc
    return resolved


def safe_slug(value: str, fallback: str = "dataset") -> str:
    text = INVALID_PATH_CHARS.sub("_", str(value or fallback)).strip(" .")
    return text or fallback


def read_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_exported_at(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def dataset_score(data_dir: Path) -> float:
    summary = read_json_object(data_dir / "acquisition-run-summary.json")
    score = parse_exported_at(str(summary.get("exportedAt", "")))
    if score:
        return score

    capture = read_json_object(data_dir / "management-api-all.json")
    score = parse_exported_at(str(capture.get("exportedAt", "")))
    if score:
        return score

    known_files = [
        data_dir / "acquisition-run-summary.json",
        data_dir / "management-api-all.json",
        data_dir / "management-api-details.json",
        data_dir / "daily-attachments-manifest.json",
    ]
    existing_times = [path.stat().st_mtime for path in known_files if path.exists()]
    return max(existing_times) if existing_times else data_dir.stat().st_mtime


def is_capture_data_dir(path: Path) -> bool:
    return path.is_dir() and (path / "management-api-all.json").exists()


def capture_data_complete(path: Path) -> bool:
    """Return whether list, details and every expected attachment form a proven snapshot."""
    try:
        return inspect_snapshot(path).complete
    except OSError:
        return False


def iter_capture_data_dirs(root: Path | str = DEFAULT_CAPTURE_ROOT) -> list[Path]:
    resolved_root = project_path(root)
    if not resolved_root.exists():
        return []
    return sorted(
        (path for path in resolved_root.iterdir() if is_capture_data_dir(path)),
        key=dataset_score,
        reverse=True,
    )


def latest_capture_data_dir(root: Path | str = DEFAULT_CAPTURE_ROOT) -> Path:
    candidates = iter_capture_data_dirs(root)
    if not candidates:
        raise FileNotFoundError(
            f"No captured daily-management data found under {project_path(root)}"
        )
    return candidates[0].resolve()


def resolve_capture_data_dir(
    data_dir: Path | str | None,
    root: Path | str = DEFAULT_CAPTURE_ROOT,
) -> Path:
    if data_dir:
        resolved = project_path(data_dir).resolve()
        if not is_capture_data_dir(resolved):
            raise FileNotFoundError(f"Invalid capture data directory: {resolved}")
        return resolved
    return latest_capture_data_dir(root)
