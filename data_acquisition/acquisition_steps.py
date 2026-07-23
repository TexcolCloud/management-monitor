from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from common.process_runner import run_command
from common.runtime_config import RuntimeConfig
from common.workflow_paths import ensure_inside_project, project_path
from data_acquisition.time_range import SelectedRange


ATTACHMENT_MANIFEST_NAMES = (
    "daily-attachments-manifest.json",
    "daily-attachments-manifest.csv",
    "daily-attachments-manifest-excel-safe.csv",
)
SHA256_FILE_NAME = re.compile(r"^[a-f0-9]{64}$", flags=re.IGNORECASE)


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _prepare_attachment_staging(
    output_dir: Path,
    staging_dir: Path,
    attachment_dir_name: str,
) -> None:
    shutil.copy2(
        output_dir / "management-api-details.json",
        staging_dir / "management-api-details.json",
    )
    current_attachments = output_dir / attachment_dir_name
    if current_attachments.is_dir():
        shutil.copytree(
            current_attachments,
            staging_dir / attachment_dir_name,
            copy_function=_link_or_copy,
        )
    current_manifest = output_dir / ATTACHMENT_MANIFEST_NAMES[0]
    if current_manifest.is_file():
        shutil.copy2(current_manifest, staging_dir / current_manifest.name)


def _prune_unreferenced_attachments(staging_dir: Path, attachment_dir_name: str) -> None:
    attachments_dir = (staging_dir / attachment_dir_name).resolve()
    manifest = json.loads(
        (staging_dir / ATTACHMENT_MANIFEST_NAMES[0]).read_text(encoding="utf-8")
    )
    referenced: set[Path] = set()
    for row in manifest.get("rows", []):
        saved_path = str(row.get("savedPath") or "").strip()
        if not saved_path:
            continue
        resolved = (staging_dir / saved_path).resolve()
        try:
            resolved.relative_to(attachments_dir)
        except ValueError as exc:
            raise ValueError(f"Attachment manifest path is outside output directory: {saved_path}") from exc
        if resolved.is_file():
            referenced.add(resolved)

    if not attachments_dir.exists():
        return
    for path in attachments_dir.rglob("*"):
        if path.is_file() and path.resolve() not in referenced:
            path.unlink()
    for path in sorted(attachments_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _prune_attachment_store(output_dir: Path, attachment_dir_name: str) -> int:
    """Compatibility no-op: shared attachment content may belong to an older snapshot."""
    return 0


def _prune_legacy_range_attachments(output_dir: Path, attachment_dir_name: str) -> int:
    """Compatibility no-op: range snapshots and their attachments are immutable history."""
    return 0


def cleanup_published_attachment_outputs(
    output_dir: Path,
    attachment_dir_name: str,
    logger: logging.Logger,
) -> None:
    """Clean attachment caches only after the full capture snapshot is published."""
    try:
        removed = _prune_attachment_store(output_dir, attachment_dir_name)
        if removed:
            logger.info("Removed %s attachments outside the current thirty-day snapshot", removed)
        legacy_removed = _prune_legacy_range_attachments(output_dir, attachment_dir_name)
        if legacy_removed:
            logger.info("Removed attachments from %s legacy range snapshots", legacy_removed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Attachment store cleanup failed and will retry next run: %s", exc)


def _publish_attachment_outputs(
    output_dir: Path,
    staging_dir: Path,
    attachment_dir_name: str,
) -> None:
    names = (attachment_dir_name, *ATTACHMENT_MANIFEST_NAMES)
    missing = [name for name in names if not (staging_dir / name).exists()]
    if missing:
        raise RuntimeError("Attachment staging output is incomplete: " + ", ".join(missing))

    backup_dir = output_dir / f".attachment-backup-{uuid.uuid4().hex}"
    backup_dir.mkdir()
    published: list[str] = []
    backed_up: list[str] = []
    rollback_complete = False
    try:
        for name in names:
            target = output_dir / name
            if target.exists():
                target.replace(backup_dir / name)
                backed_up.append(name)
        for name in names:
            (staging_dir / name).replace(output_dir / name)
            published.append(name)
    except Exception:
        try:
            for name in reversed(published):
                target = output_dir / name
                if target.exists():
                    target.replace(staging_dir / name)
            for name in reversed(backed_up):
                (backup_dir / name).replace(output_dir / name)
            rollback_complete = True
        except OSError:
            # Keep the backup intact for manual or startup recovery.
            rollback_complete = False
        raise
    else:
        rollback_complete = True
    finally:
        if rollback_complete:
            shutil.rmtree(backup_dir, ignore_errors=True)


def resolve_node_executable() -> str:
    configured = os.environ.get("NODE_EXE") or os.environ.get("NODE_PATH")
    candidates = [configured] if configured else []
    located = shutil.which("node") or shutil.which("node.exe")
    if located:
        candidates.append(located)
    if os.name == "nt":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if not base:
                continue
            suffix = Path("Programs/nodejs/node.exe") if env_name == "LOCALAPPDATA" else Path("nodejs/node.exe")
            candidates.append(str(Path(base) / suffix))
        candidates.extend(
            [r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"]
        )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    raise RuntimeError(
        "未找到 Node.js。请先安装 Node.js LTS 并执行 npm install；"
        "也可设置 NODE_EXE 指向 node.exe。"
    )


def capture_range(
    args,
    selected: SelectedRange,
    output_dir: Path,
    logger: logging.Logger,
    browser_bridge_url: str = "",
    browser_bridge_token: str = "",
) -> None:
    command = [
        sys.executable,
        "-m",
        "data_acquisition.capture_daily_range",
        "--details",
        f"--start={selected.start_text}",
        f"--end={selected.end_text}",
        f"--out={output_dir}",
        f"--headers-file={project_path(args.headers_file)}",
        f"--token-source={project_path(args.token_source)}",
        f"--page-size={args.page_size}",
        f"--max-pages={args.max_pages}",
        f"--request-timeout={args.request_timeout}",
        f"--retries={args.retries}",
        f"--max-detail-failures={args.max_detail_failures}",
        f"--date-field={args.date_field}",
    ]
    if args.allow_partial:
        command.append("--allow-partial")
    if args.body_file:
        command.append(f"--body-file={project_path(args.body_file)}")
    if browser_bridge_url:
        command.append(f"--browser-bridge-url={browser_bridge_url}")
    bridge_environment = (
        {"WORKORDER_BROWSER_BRIDGE_TOKEN": browser_bridge_token}
        if browser_bridge_url and browser_bridge_token
        else None
    )
    run_command(
        command,
        "抓取日常管理数据",
        logger,
        env_overrides=bridge_environment,
    )


def download_attachments(
    args,
    runtime: RuntimeConfig,
    output_dir: Path,
    logger: logging.Logger,
    cleanup_after_publish: bool = True,
) -> None:
    output_dir = ensure_inside_project(output_dir)
    attachment_store = ensure_inside_project(output_dir.parent / "attachment-store")
    with TemporaryDirectory(prefix=".attachment-staging-", dir=output_dir) as temp_dir:
        staging_dir = Path(temp_dir)
        _prepare_attachment_staging(output_dir, staging_dir, runtime.attachment_dir_name)
        command = [
            resolve_node_executable(),
            "data_acquisition/download-daily-attachments.js",
            f"--data-dir={staging_dir}",
            f"--token-source={project_path(args.token_source)}",
            f"--headers-file={project_path(args.headers_file)}",
            f"--attachment-dir-name={runtime.attachment_dir_name}",
            f"--attachment-store={attachment_store}",
            f"--timeout-ms={runtime.attachment_timeout_ms}",
            f"--retries={runtime.attachment_retries}",
            f"--max-failures={args.max_attachment_failures}",
            f"--concurrency={args.attachment_concurrency}",
        ]
        browser_bridge_url = str(getattr(args, "browser_bridge_url", "") or "")
        browser_bridge_token = str(getattr(args, "browser_bridge_token", "") or "")
        if browser_bridge_url:
            command.append(f"--browser-bridge-url={browser_bridge_url}")
        bridge_environment = (
            {"WORKORDER_BROWSER_BRIDGE_TOKEN": browser_bridge_token}
            if browser_bridge_url and browser_bridge_token
            else None
        )
        run_command(
            command,
            "下载日常管理附件",
            logger,
            env_overrides=bridge_environment,
        )
        _publish_attachment_outputs(output_dir, staging_dir, runtime.attachment_dir_name)
        if cleanup_after_publish:
            cleanup_published_attachment_outputs(
                output_dir,
                runtime.attachment_dir_name,
                logger,
            )


def write_result_file(result_file: Path, output_dir: Path) -> None:
    resolved = ensure_inside_project(project_path(result_file))
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps({"outputDir": str(output_dir)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
