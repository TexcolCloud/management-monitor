from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from common.logging_utils import setup_logging
from common.process_runner import run_command
from common.runtime_config import load_runtime_config
from common.workflow_paths import (
    DEFAULT_CAPTURE_ROOT,
    PROJECT_ROOT,
    capture_data_complete,
    is_capture_data_dir,
    project_path,
)
from database.postgres_store import data_dir_records_complete
from data_acquisition.browser_session import BrowserSession


LOGGER = logging.getLogger("workorder_daily_manage")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="数据库为空时自动获取最近 30 天日常管理数据并导出 Excel 表格。"
    )
    parser.add_argument("--capture-out", type=Path, default=None, help="数据抓取输出目录。")
    parser.add_argument("--capture-root", type=Path, default=DEFAULT_CAPTURE_ROOT)
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--request-timeout", type=float, default=None)
    parser.add_argument("--retries", type=int, default=None)
    parser.add_argument("--max-detail-failures", type=int, default=None)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--max-attachment-failures", type=int, default=None)
    parser.add_argument("--attachment-concurrency", type=int, default=None)
    parser.add_argument("--date-field", default="")
    parser.add_argument("--request-start-field", default="")
    parser.add_argument("--request-end-field", default="")
    parser.add_argument("--login-wait-ms", type=int, default=None)
    parser.add_argument("--skip-login", action="store_true", help="仅调试使用：跳过登录刷新。")
    parser.add_argument("--no-login-replay", action="store_true")
    parser.add_argument("--skip-attachments", action="store_true")
    parser.add_argument("--keep-existing-attachments", action="store_true")
    parser.add_argument("--export-root", type=Path, default=None)
    parser.add_argument("--excel-out", type=Path, default=None, help="完整 Excel 输出文件路径。")
    parser.add_argument("--sheet-name", default="")
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="只执行数据获取，不导出 Excel。用于排查问题。",
    )
    return parser.parse_args()


def build_capture_command(
    args: argparse.Namespace,
    result_file: Path,
    browser_bridge_url: str = "",
) -> list[str]:
    command = [sys.executable, "-m", "data_acquisition.run_daily_acquisition"]
    command.append(f"--result-file={result_file}")

    if args.capture_out:
        command.append(f"--out={project_path(args.capture_out)}")
    else:
        command.append(f"--capture-root={project_path(args.capture_root)}")
    if args.page_size is not None:
        command.append(f"--page-size={args.page_size}")
    if args.max_pages is not None:
        command.append(f"--max-pages={args.max_pages}")
    if args.request_timeout is not None:
        command.append(f"--request-timeout={args.request_timeout}")
    if args.retries is not None:
        command.append(f"--retries={args.retries}")
    if args.max_detail_failures is not None:
        command.append(f"--max-detail-failures={args.max_detail_failures}")
    if args.max_attachment_failures is not None:
        command.append(f"--max-attachment-failures={args.max_attachment_failures}")
    if args.attachment_concurrency is not None:
        command.append(f"--attachment-concurrency={args.attachment_concurrency}")
    if args.date_field:
        command.append(f"--date-field={args.date_field}")
    if args.login_wait_ms is not None:
        command.append(f"--login-wait-ms={args.login_wait_ms}")

    for flag, enabled in [
        ("--skip-login", args.skip_login or bool(browser_bridge_url)),
        ("--no-login-replay", args.no_login_replay),
        ("--skip-attachments", args.skip_attachments),
        ("--keep-existing-attachments", args.keep_existing_attachments),
        ("--allow-partial", args.allow_partial),
    ]:
        if enabled:
            command.append(flag)
    if browser_bridge_url:
        command.append(f"--browser-bridge-url={browser_bridge_url}")

    return command


def result_file_path() -> Path:
    return PROJECT_ROOT / ".temp" / "workflow-results" / f"daily-{uuid.uuid4().hex}.json"


def acquired_data_dir(result_file: Path) -> Path:
    try:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        data_dir = Path(payload["outputDir"]).resolve()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取本次抓取结果目录：{result_file}") from exc
    if not is_capture_data_dir(data_dir):
        raise RuntimeError(f"本次抓取结果目录无效：{data_dir}")
    return data_dir


def attachments_complete(data_dir: Path) -> bool:
    manifest_path = data_dir / "daily-attachments-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rows = manifest.get("rows")
    if not isinstance(rows, list):
        return False
    attachment_root = (data_dir / "daily-attachments").resolve()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in {
            "downloaded",
            "reused",
            "skipped_duplicate",
        }:
            return False
        saved_path = str(row.get("savedPath") or "").strip()
        candidate = (data_dir / saved_path).resolve()
        try:
            candidate.relative_to(attachment_root)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
    return True


def build_database_command(data_dir: Path) -> list[str]:
    return [sys.executable, "-m", "database.postgres_store", f"--data-dir={data_dir}"]


def build_export_command(args: argparse.Namespace, data_dir: Path) -> list[str]:
    command = [sys.executable, "-m", "data_export", f"--data-dir={data_dir}"]
    if args.export_root:
        command.append(f"--export-root={project_path(args.export_root)}")
    if args.excel_out:
        command.append(f"--out={project_path(args.excel_out)}")
    if args.sheet_name:
        command.append(f"--sheet-name={args.sheet_name}")
    return command


def build_monitor_command(
    args: argparse.Namespace,
    reuse_login: bool,
    browser_bridge_url: str = "",
) -> list[str]:
    command = [sys.executable, "-m", "workflows.work_order_monitor"]
    if args.skip_login or browser_bridge_url:
        command.append("--skip-login")
    if browser_bridge_url:
        command.append(f"--browser-bridge-url={browser_bridge_url}")
    if args.request_start_field:
        command.append(f"--request-start-field={args.request_start_field}")
    if args.request_end_field:
        command.append(f"--request-end-field={args.request_end_field}")
    return command


def main() -> None:
    args = parse_args()
    os.environ.setdefault("WORKORDER_RUN_ID", uuid.uuid4().hex[:12])
    logger = setup_logging()
    capture_result_file = result_file_path()

    logger.info("启动日常管理初始化与新工单监听流程")
    browser_session: BrowserSession | None = None
    browser_bridge_url = ""
    browser_bridge_environment: dict[str, str] | None = None
    try:
        if not args.skip_login:
            runtime = load_runtime_config()
            session_args = argparse.Namespace(
                login_profile_dir=runtime.login_profile_dir,
                login_navigation_file=runtime.login_navigation_file,
                token_source=runtime.token_source_dir,
                headers_file=runtime.headers_file,
                login_wait_ms=args.login_wait_ms or runtime.login_wait_ms,
                no_login_replay=args.no_login_replay,
            )
            browser_session = BrowserSession(
                session_args,
                runtime,
                PROJECT_ROOT / ".temp" / "work-order-monitor-state.json",
                LOGGER,
            )
            logger.info("打开登录窗口；完成登录后将复用该浏览器完成初始化抓取和新工单监听")
            browser_bridge_url = browser_session.start()
            browser_bridge_environment = {
                "WORKORDER_BROWSER_BRIDGE_TOKEN": browser_session.token
            }
        run_command(
            build_capture_command(args, capture_result_file, browser_bridge_url),
            "最近30天数据获取",
            LOGGER,
            env_overrides=browser_bridge_environment,
        )
        data_dir = acquired_data_dir(capture_result_file)
        if not capture_data_complete(data_dir):
            raise RuntimeError("最近三十天列表抓取不完整，已停止入库和进入监听")
        if not attachments_complete(data_dir):
            raise RuntimeError("最近三十天附件不完整，已停止进入监听，请重新运行以补齐附件")
        if not data_dir_records_complete(data_dir):
            logger.info("最近三十天存在缺失表单，开始补充写入 PostgreSQL")
            run_command(build_database_command(data_dir), "PostgreSQL入库", LOGGER)
            if not data_dir_records_complete(data_dir):
                raise RuntimeError("最近三十天表单仍未完整写入 PostgreSQL，已停止进入监听")
        else:
            logger.info("PostgreSQL 已完整包含最近三十天表单，跳过重复入库")

        logger.info("最近三十天表单和附件均完整: %s", data_dir)

        if not args.skip_export:
            run_command(build_export_command(args, data_dir), "表格导出", LOGGER)
        logger.info("初始化完成，开始常驻监听新工单")
        run_command(
            build_monitor_command(args, reuse_login=True, browser_bridge_url=browser_bridge_url),
            "新工单监听",
            LOGGER,
            env_overrides=browser_bridge_environment,
        )
    except KeyboardInterrupt:
        logger.info("已停止日常管理工作流")
    finally:
        capture_result_file.unlink(missing_ok=True)
        if browser_session:
            browser_session.stop()


if __name__ == "__main__":
    main()
