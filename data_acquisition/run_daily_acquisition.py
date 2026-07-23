from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.logging_utils import setup_logging  # noqa: E402
from common.cli import run_cli_safely  # noqa: E402
from common.runtime_config import load_runtime_config  # noqa: E402
from common.workflow_paths import ensure_inside_project, project_path  # noqa: E402
from data_acquisition.acquisition_steps import (  # noqa: E402
    capture_range,
    cleanup_published_attachment_outputs,
    download_attachments,
    write_result_file,
)
from data_acquisition.browser_session import BrowserSession  # noqa: E402
from data_acquisition.run_summary import write_run_summary  # noqa: E402
from data_acquisition.time_range import DEFAULT_CAPTURE_DAYS, recent_range  # noqa: E402
from safety_monitor.adapters.filesystem_snapshot import (  # noqa: E402
    FilesystemSnapshotPublisher,
    inspect_snapshot,
)


RUNTIME_CONFIG = load_runtime_config()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="获取最近 30 天的日常管理数据。"
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--capture-root", type=Path, default=RUNTIME_CONFIG.capture_root)
    parser.add_argument("--headers-file", type=Path, default=RUNTIME_CONFIG.headers_file)
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--token-source", type=Path, default=RUNTIME_CONFIG.token_source_dir)
    parser.add_argument("--page-size", type=int, default=RUNTIME_CONFIG.page_size)
    parser.add_argument("--max-pages", type=int, default=RUNTIME_CONFIG.max_pages)
    parser.add_argument("--request-timeout", type=float, default=RUNTIME_CONFIG.request_timeout_seconds)
    parser.add_argument("--retries", type=int, default=RUNTIME_CONFIG.request_retries)
    parser.add_argument("--max-detail-failures", type=int, default=RUNTIME_CONFIG.detail_max_failures)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument(
        "--max-attachment-failures",
        type=int,
        default=RUNTIME_CONFIG.attachment_max_failures,
    )
    parser.add_argument(
        "--attachment-concurrency",
        type=int,
        default=RUNTIME_CONFIG.attachment_concurrency,
    )
    parser.add_argument("--result-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--date-field", default="createTime")
    parser.add_argument("--login-wait-ms", type=int, default=RUNTIME_CONFIG.login_wait_ms)
    parser.add_argument(
        "--login-navigation-file",
        type=Path,
        default=RUNTIME_CONFIG.login_navigation_file,
    )
    parser.add_argument("--no-login-replay", action="store_true")
    parser.add_argument("--login-profile-dir", type=Path, default=RUNTIME_CONFIG.login_profile_dir)
    parser.add_argument("--skip-login", action="store_true")
    parser.add_argument("--browser-bridge-url", default="")
    parser.add_argument(
        "--browser-bridge-token",
        default=os.environ.get("WORKORDER_BROWSER_BRIDGE_TOKEN", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--skip-attachments", action="store_true")
    parser.add_argument("--keep-existing-attachments", action="store_true")
    return parser.parse_args()


def _close_file_handlers(logger: logging.Logger) -> None:
    handlers = getattr(logger, "handlers", ())
    if not isinstance(handlers, (list, tuple)):
        return
    for handler in list(handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def _publish_current_snapshot(staging_dir: Path, output_dir: Path) -> None:
    """Compatibility wrapper around the crash-recoverable snapshot publisher."""
    FilesystemSnapshotPublisher().publish(staging_dir, output_dir)


def _publish_partial_attempt(staging_dir: Path, output_dir: Path) -> Path:
    attempts_dir = output_dir.parent / "partial-attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempt_dir = attempts_dir / f"partial-{uuid.uuid4().hex}"
    staging_dir.replace(attempt_dir)
    return attempt_dir


def main() -> None:
    args = parse_args()
    selected = recent_range(DEFAULT_CAPTURE_DAYS)
    logger = setup_logging()

    output_dir = (
        project_path(args.out)
        if args.out
        else project_path(args.capture_root) / "current"
    )
    output_dir = ensure_inside_project(output_dir)
    publish_current = args.out is None
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if publish_current:
        FilesystemSnapshotPublisher().recover(output_dir)
    browser_session: BrowserSession | None = None

    with TemporaryDirectory(prefix=".current-staging-", dir=output_dir.parent) if publish_current else nullcontext(output_dir) as staged:
        work_dir = Path(staged)
        if not publish_current:
            work_dir.mkdir(parents=True, exist_ok=True)
        logger = setup_logging(work_dir / "acquisition.log")
        published_dir = output_dir
        try:
            logger.info(
                "启动数据获取: field=%s range=%s..%s output=%s",
                args.date_field,
                selected.start_text,
                selected.end_text,
                output_dir,
            )

            if not args.skip_login and not args.browser_bridge_url:
                logger.info("即将打开登录窗口，请登录门户；本次采集和附件下载将复用该会话。")
                browser_session = BrowserSession(
                    args,
                    RUNTIME_CONFIG,
                    PROJECT_ROOT / ".temp" / "daily-acquisition-state.json",
                    logger,
                )
                args.browser_bridge_url = browser_session.start()
                args.browser_bridge_token = browser_session.token

            capture_range(
                args,
                selected,
                work_dir,
                logger,
                browser_bridge_url=args.browser_bridge_url,
                browser_bridge_token=getattr(args, "browser_bridge_token", ""),
            )
            if not args.skip_attachments:
                download_attachments(
                    args,
                    RUNTIME_CONFIG,
                    work_dir,
                    logger,
                    cleanup_after_publish=not publish_current,
                )
            write_run_summary(work_dir, selected, args.date_field, logger)
            if publish_current:
                _close_file_handlers(logger)
                validation = inspect_snapshot(work_dir)
                if validation.complete:
                    _publish_current_snapshot(work_dir, output_dir)
                    logger = setup_logging(output_dir / "acquisition.log")
                    if not args.skip_attachments:
                        cleanup_published_attachment_outputs(
                            output_dir,
                            RUNTIME_CONFIG.attachment_dir_name,
                            logger,
                        )
                else:
                    published_dir = _publish_partial_attempt(work_dir, output_dir)
                    logger = setup_logging(published_dir / "acquisition.log")
                    logger.warning(
                        "列表总数不一致，诊断结果已保存且未替换当前完整快照: %s",
                        published_dir,
                    )
                    for reason in validation.reasons:
                        logger.warning("批次不完整: %s", reason)
            if args.result_file:
                write_result_file(args.result_file, published_dir)
            logger.info("数据获取流程完成")
        except Exception:
            _close_file_handlers(logger)
            if publish_current and work_dir.exists():
                published_dir = _publish_partial_attempt(work_dir, output_dir)
                logger = setup_logging(published_dir / "acquisition.log")
                logger.exception("采集失败，诊断批次已保留且未替换 current: %s", published_dir)
                if args.result_file:
                    write_result_file(args.result_file, published_dir)
            raise
        finally:
            _close_file_handlers(logger)
            if browser_session:
                browser_session.stop()


if __name__ == "__main__":
    run_cli_safely(
        main,
        failure_message="最近三十天数据获取失败",
        interrupted_message="已停止最近三十天数据获取",
    )
