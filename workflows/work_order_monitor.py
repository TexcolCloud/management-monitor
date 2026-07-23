from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common.feishu_app_bot import FeishuOutboundBot
from common.cli import run_cli_safely
from workflows.feishu_export_service import start_feishu_export_listener
from common.logging_utils import setup_logging
from common.process_runner import run_command
from common.runtime_config import RuntimeConfig, load_runtime_config
from common.workflow_paths import ensure_inside_project, project_path
from data_acquisition.acquisition_steps import resolve_node_executable
from data_acquisition.browser_session import BrowserSession
from data_acquisition.capture_components import (
    ApiClient,
    CaptureResult,
    CaptureService,
    TokenProvider,
    read_captured_headers,
    read_request_config,
)
from safety_monitor.adapters.postgres import (
    PostgresMonitoringUnitOfWork,
    PostgresOutboxRepository,
)
from safety_monitor.adapters.feishu_notifications import (
    DownloadedImage,
    FeishuOutboxDispatcher,
)
from safety_monitor.application.monitoring import DeliverPendingNotifications, MonitorNewOrders
from safety_monitor.domain.monitoring import CapturedWindow, PollingPolicy, PollingWindow
from safety_monitor.domain.work_order import merge_work_order
from safety_monitor.ports.repositories import CheckpointConflictError, MonitoringUnitOfWork
from database.postgres_store import (
    DEFAULT_CONFIG_PATH,
    connect,
    enqueue_feishu_notifications,
    ensure_table,
    existing_safety_codes,
    is_enabled,
    load_config,
    load_env_file,
    upsert_rows,
)


LOGGER = logging.getLogger("workorder_daily_manage")
DEFAULT_STATE_FILE = Path(".temp/work-order-monitor-state.json")
MONITOR_STREAM = "daily-management"
TIME_FIELD_PAIRS = (
    ("createTimeStart", "createTimeEnd"),
    ("createStartTime", "createEndTime"),
    ("startTime", "endTime"),
    ("beginTime", "endTime"),
    ("startDate", "endDate"),
)
DEFAULT_TIME_FIELD_PAIR = ("startTime", "endTime")


@dataclass(frozen=True)
class MonitorState:
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class CycleSummary:
    start: datetime
    end: datetime
    details_seen: int
    new_count: int
    upserted_count: int


def parse_state_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def load_monitor_state(path: Path, logger: logging.Logger = LOGGER) -> MonitorState:
    if not path.exists():
        return MonitorState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("轮询检查点无法读取，将按初始回看窗口执行: %s", exc)
        return MonitorState()
    if not isinstance(payload, dict):
        logger.warning("轮询检查点格式无效，将按初始回看窗口执行: %s", path)
        return MonitorState()
    checkpoint = parse_state_datetime(payload.get("lastSuccessAt"))
    if payload.get("lastSuccessAt") and checkpoint is None:
        logger.warning("轮询检查点时间无效，将按初始回看窗口执行: %s", path)
    return MonitorState(last_success_at=checkpoint)


def write_monitor_state(path: Path, state: MonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "lastSuccessAt": state.last_success_at.isoformat(timespec="seconds")
        if state.last_success_at
        else "",
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def poll_window(
    now: datetime,
    state: MonitorState,
    lookback: timedelta,
    overlap: timedelta,
    initial_lookback: timedelta,
) -> tuple[datetime, datetime]:
    try:
        window = PollingPolicy(lookback, overlap, initial_lookback).window(
            now,
            state.last_success_at,
        )
    except ValueError as exc:
        raise ValueError("轮询时间窗口配置无效") from exc
    return window.start, window.end


def is_authentication_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in ("api code 401", "api code 403", "http 401", "http 403"))


def request_time_fields(
    body: dict[str, Any],
    requested_start: str,
    requested_end: str,
) -> tuple[str, str]:
    if bool(requested_start) != bool(requested_end):
        raise ValueError("--request-start-field 和 --request-end-field 必须同时指定")
    if requested_start:
        return requested_start, requested_end
    for start_field, end_field in TIME_FIELD_PAIRS:
        if start_field in body and end_field in body:
            return start_field, end_field
    return DEFAULT_TIME_FIELD_PAIR


def monitor_request_body(
    body: dict[str, Any],
    start: datetime,
    end: datetime,
    requested_start: str = "",
    requested_end: str = "",
) -> dict[str, Any]:
    start_field, end_field = request_time_fields(body, requested_start, requested_end)
    return {
        **body,
        start_field: start.strftime("%Y-%m-%d %H:%M:%S"),
        end_field: end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def combine_details(result: CaptureResult) -> list[dict[str, Any]]:
    details_by_id = {
        str(detail.get("id")): detail
        for detail in result.details
        if str(detail.get("id") or "").strip()
    }
    combined: list[dict[str, Any]] = []
    for row in result.rows:
        record_id = str(row.get("id") or "").strip()
        detail = details_by_id.get(record_id)
        if detail is not None:
            combined.append(merge_work_order(row, detail))
    return combined


class _MonitorCaptureSource:
    """Adapter from the existing portal capture client to the pure monitoring port."""

    def __init__(self, monitor: "WorkOrderMonitor") -> None:
        self.monitor = monitor
        self.records: list[dict[str, Any]] = []

    def capture(self, window: PollingWindow) -> CapturedWindow:
        self.monitor.logger.info(
            "轮询工单: createTime=%s 至 %s",
            window.start.isoformat(sep=" "),
            window.end.isoformat(sep=" "),
        )
        result = self.monitor._capture(window.start, window.end)
        self.records = combine_details(result)
        incomplete_reasons = getattr(result.statistics, "incomplete_reasons", ())
        return CapturedWindow(
            records=tuple(self.records),
            failed_details=tuple(result.failed_details),
            total_consistent=(
                bool(result.statistics.total_consistent)
                and not bool(incomplete_reasons)
            ),
        )


class WorkOrderMonitor:
    def __init__(
        self,
        args: argparse.Namespace,
        runtime: RuntimeConfig,
        logger: logging.Logger = LOGGER,
        monitoring_uow: MonitoringUnitOfWork | None = None,
        outbox_repository: PostgresOutboxRepository | None = None,
    ) -> None:
        self.args = args
        self.runtime = runtime
        self.logger = logger
        self.state_path = ensure_inside_project(project_path(args.state_file))
        self.browser_bridge_url = ""
        self.browser_bridge_token = ""
        load_env_file()
        self.feishu_bot = FeishuOutboundBot.from_environment()
        self._injected_monitoring_uow = monitoring_uow
        self._injected_outbox_repository = outbox_repository
        self._outbox_worker_id = f"monitor-{os.getpid()}-{uuid.uuid4().hex[:12]}"

    def set_browser_bridge(self, url: str, token: str = "") -> None:
        self.browser_bridge_url = url.rstrip("/")
        self.browser_bridge_token = token or os.environ.get(
            "WORKORDER_BROWSER_BRIDGE_TOKEN", ""
        )
        if self.browser_bridge_url and not self.browser_bridge_token:
            raise RuntimeError("浏览器会话桥缺少临时访问凭证")

    def _database_config(self) -> dict[str, Any]:
        database_config = load_config(self.args.database_config)
        if not is_enabled(database_config):
            raise RuntimeError("PostgreSQL 入库未启用，轮询服务无法使用 safety_code 去重")
        return database_config

    def _monitoring_unit_of_work(self) -> MonitoringUnitOfWork:
        if self._injected_monitoring_uow is not None:
            return self._injected_monitoring_uow
        return PostgresMonitoringUnitOfWork(self._database_config())

    def _notification_outbox(self) -> PostgresOutboxRepository:
        if self._injected_outbox_repository is not None:
            return self._injected_outbox_repository
        return PostgresOutboxRepository(self._database_config())

    def _migrate_legacy_checkpoint(self, unit_of_work: MonitoringUnitOfWork) -> None:
        if unit_of_work.read_checkpoint(MONITOR_STREAM) is not None:
            return
        legacy = load_monitor_state(self.state_path, self.logger).last_success_at
        if legacy is None:
            return
        try:
            unit_of_work.persist_complete_window(
                stream=MONITOR_STREAM,
                records=(),
                expected_checkpoint=None,
                next_checkpoint=legacy,
            )
        except CheckpointConflictError:
            # Another monitor imported or advanced the checkpoint first.
            return
        self.logger.info("已将旧文件检查点导入 PostgreSQL: %s", legacy.isoformat(sep=" "))

    def _capture(self, start: datetime, end: datetime) -> CaptureResult:
        headers_file = ensure_inside_project(project_path(self.args.headers_file))
        request_file = self.args.body_file or headers_file
        body_from_file, headers_from_body = read_request_config(request_file)
        base_body = monitor_request_body(
            body_from_file,
            start,
            end,
            self.args.request_start_field,
            self.args.request_end_field,
        )
        captured_headers = {
            **headers_from_body,
            **read_captured_headers(headers_file),
        }
        token = ""
        if not self.browser_bridge_url:
            admin_host = urlparse(self.runtime.list_api_url).hostname or "api.example.invalid"
            token = TokenProvider(
                source_dir=ensure_inside_project(project_path(self.args.token_source)),
                output_dir=self.state_path.parent,
                captured_headers=captured_headers,
                environment_token=os.environ.get("WORKORDER_TOKEN", ""),
                admin_host=admin_host,
            ).resolve()
        client = ApiClient(
            list_url=self.runtime.list_api_url,
            detail_url=self.runtime.detail_api_url,
            token=token,
            captured_headers=captured_headers,
            timeout_seconds=self.runtime.request_timeout_seconds,
            retries=self.runtime.request_retries,
            retry_backoff_seconds=self.runtime.retry_backoff_seconds,
            retryable_api_codes=self.runtime.retryable_api_codes,
            browser_bridge_url=self.browser_bridge_url,
            browser_bridge_token=self.browser_bridge_token,
            logger=self.logger,
        )
        service = CaptureService(
            api_client=client,
            max_pages=self.runtime.max_pages,
            stop_on_empty_page=self.runtime.stop_on_empty_page,
            validate_total=self.runtime.validate_total,
            logger=self.logger,
        )
        return service.capture(
            start=start,
            end=end,
            date_field=self.args.date_field,
            page_size=self.runtime.page_size,
            base_body=base_body,
            include_details=True,
        )

    def _store_rows(self, rows: list[dict[str, Any]]) -> tuple[set[str], int]:
        codes = {str(row.get("safetyCode") or "").strip() for row in rows}
        codes.discard("")
        if not codes:
            return set(), 0

        database_config = self._database_config()
        connection = connect(database_config)
        try:
            ensure_table(connection, database_config)
            existing = existing_safety_codes(connection, database_config, codes)
            new_codes = codes - existing
            upserted = upsert_rows(connection, database_config, rows, commit=False)
            enqueue_feishu_notifications(
                connection,
                database_config,
                [
                    row
                    for row in rows
                    if str(row.get("safetyCode") or "").strip() in new_codes
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return new_codes, upserted

    def _download_image_attachments(
        self,
        row: dict[str, Any],
        data_dir: Path,
    ) -> list[DownloadedImage]:
        data_dir.mkdir(parents=True, exist_ok=True)
        details_file = data_dir / "management-api-details.json"
        details_file.write_text(
            json.dumps({"rows": [row]}, ensure_ascii=False),
            encoding="utf-8",
        )
        command = [
            resolve_node_executable(),
            "data_acquisition/download-daily-attachments.js",
            f"--data-dir={data_dir}",
            f"--token-source={ensure_inside_project(project_path(self.args.token_source))}",
            f"--headers-file={ensure_inside_project(project_path(self.args.headers_file))}",
            "--images-only",
            "--max-failures=0",
            f"--timeout-ms={self.runtime.attachment_timeout_ms}",
            f"--retries={self.runtime.attachment_retries}",
            f"--concurrency={self.runtime.attachment_concurrency}",
        ]
        bridge_environment = None
        if self.browser_bridge_url:
            command.append(f"--browser-bridge-url={self.browser_bridge_url}")
            bridge_environment = {
                "WORKORDER_BROWSER_BRIDGE_TOKEN": self.browser_bridge_token
            }
        run_command(
            command,
            "download Feishu image attachments",
            self.logger,
            env_overrides=bridge_environment,
        )

        manifest_file = data_dir / "daily-attachments-manifest.json"
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            entries = manifest.get("rows", []) if isinstance(manifest, dict) else []
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Feishu image attachment manifest is unavailable") from exc
        if not isinstance(entries, list):
            raise RuntimeError("Feishu image attachment manifest rows are invalid")

        attachment_root = (data_dir / self.runtime.attachment_dir_name).resolve()
        images: list[DownloadedImage] = []
        successful_statuses = {"downloaded", "reused", "skipped_duplicate"}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("isImage"):
                continue
            if str(entry.get("status") or "") not in successful_statuses:
                continue
            saved_path = str(entry.get("savedPath") or "").strip()
            candidate = (data_dir / saved_path).resolve()
            try:
                candidate.relative_to(attachment_root)
            except ValueError as exc:
                raise RuntimeError("Feishu image attachment path is outside the download directory") from exc
            if not candidate.is_file():
                raise RuntimeError("Feishu image attachment file is missing")
            images.append(DownloadedImage(saved_path=saved_path, path=candidate))
        return images

    def _deliver_feishu_notifications(self) -> tuple[int, int]:
        dispatcher = FeishuOutboxDispatcher(
            bot=self.feishu_bot,
            outbox=self._notification_outbox(),
            worker_id=self._outbox_worker_id,
            download_images=self._download_image_attachments,
            temp_root=self.state_path.parent,
            logger=self.logger,
        )
        return DeliverPendingNotifications(dispatcher).run()

    def poll_once(self, now: datetime | None = None) -> CycleSummary:
        end = now or datetime.now()
        unit_of_work = self._monitoring_unit_of_work()
        self._migrate_legacy_checkpoint(unit_of_work)
        source = _MonitorCaptureSource(self)
        cycle = MonitorNewOrders(
            stream=MONITOR_STREAM,
            policy=PollingPolicy(
                timedelta(minutes=self.args.lookback_minutes),
                timedelta(seconds=self.args.overlap_seconds),
                timedelta(minutes=self.args.initial_lookback_minutes),
            ),
            source=source,
            unit_of_work=unit_of_work,
        ).run(end)
        start = cycle.window.start
        end = cycle.window.end
        new_codes = set(cycle.inserted_codes)
        upserted = cycle.upserted_count
        try:
            write_monitor_state(self.state_path, MonitorState(last_success_at=end))
        except OSError as exc:
            self.logger.warning(
                "PostgreSQL 检查点已推进，但兼容 JSON 镜像写入失败: %s",
                type(exc).__name__,
            )
        sent, notification_failures = self._deliver_feishu_notifications()

        for row in source.records:
            code = str(row.get("safetyCode") or "").strip()
            if code in new_codes:
                self.logger.info(
                    "发现新工单: code=%s company=%s theme=%s",
                    code,
                    row.get("companyName") or "",
                    row.get("theme") or "",
                )
        self.logger.info(
            "本轮完成: 详情=%s 新工单=%s 入库更新=%s 飞书发送=%s 飞书待重试=%s",
            len(source.records),
            len(new_codes),
            upserted,
            sent,
            notification_failures,
        )
        return CycleSummary(start, end, len(source.records), len(new_codes), upserted)

    def run_cycle(self, now: datetime | None = None) -> CycleSummary:
        """Run portal polling while keeping outbox retry independent of portal success."""
        try:
            return self.poll_once(now)
        except Exception:
            try:
                sent, failed = self._deliver_feishu_notifications()
                if sent or failed:
                    self.logger.info(
                        "门户轮询失败期间仍执行飞书 outbox: 发送=%s 待重试=%s",
                        sent,
                        failed,
                    )
            except Exception:
                self.logger.exception("独立飞书 outbox 重试失败；租约到期后继续")
            raise


def parse_args() -> tuple[argparse.Namespace, RuntimeConfig]:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=None)
    config_args, _ = config_parser.parse_known_args()
    runtime = load_runtime_config(config_args.config)

    parser = argparse.ArgumentParser(
        description="常驻轮询工单日常管理新工单，并按 safety_code 去重入库。",
        parents=[config_parser],
    )
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--lookback-minutes", type=int, default=5)
    parser.add_argument("--initial-lookback-minutes", type=int, default=5)
    parser.add_argument("--overlap-seconds", type=int, default=30)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--database-config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--headers-file", type=Path, default=runtime.headers_file)
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--token-source", type=Path, default=runtime.token_source_dir)
    parser.add_argument("--date-field", default="createTime")
    parser.add_argument(
        "--request-start-field",
        default="",
        help="门户列表接口的开始时间请求字段；未指定时从捕获请求体识别。",
    )
    parser.add_argument(
        "--request-end-field",
        default="",
        help="门户列表接口的结束时间请求字段；未指定时从捕获请求体识别。",
    )
    parser.add_argument("--login-wait-ms", type=int, default=runtime.login_wait_ms)
    parser.add_argument("--login-navigation-file", type=Path, default=runtime.login_navigation_file)
    parser.add_argument("--login-profile-dir", type=Path, default=runtime.login_profile_dir)
    parser.add_argument("--skip-login", action="store_true", help="使用已有登录凭证直接启动。")
    parser.add_argument(
        "--browser-bridge-url",
        default="",
        help="Reuse a persistent logged-in browser request bridge.",
    )
    parser.add_argument("--no-login-replay", action="store_true")
    parser.add_argument("--once", action="store_true", help="只轮询一次后退出。")
    parser.add_argument(
        "--no-refresh-login-on-auth-error",
        action="store_false",
        dest="refresh_login_on_auth_error",
        help="令牌失效时不自动打开登录窗口。",
    )
    parser.set_defaults(refresh_login_on_auth_error=True)
    args = parser.parse_args()
    for name in ("interval_seconds", "lookback_minutes", "initial_lookback_minutes"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须大于 0")
    if args.overlap_seconds < 0:
        parser.error("--overlap-seconds 不能小于 0")
    return args, runtime


def main() -> None:
    args, runtime = parse_args()
    state_path = ensure_inside_project(project_path(args.state_file))
    logger = setup_logging(state_path.parent / "work-order-monitor.log")
    monitor = WorkOrderMonitor(args, runtime, logger)
    session: BrowserSession | None = None
    export_listener: threading.Thread | None = None

    try:
        if args.browser_bridge_url:
            monitor.set_browser_bridge(args.browser_bridge_url)
        elif not args.skip_login:
            logger.info("即将打开登录窗口，请登录门户以启动新工单监听。")
            session = BrowserSession(args, runtime, state_path, logger)
            monitor.set_browser_bridge(session.start(), session.token)

        if not args.once:
            export_listener = start_feishu_export_listener(logger)
        while True:
            try:
                monitor.run_cycle()
            except Exception as exc:
                logger.exception("本轮工单监听失败；未提交事务已回滚，将在下轮重试")
                if isinstance(exc, ValueError):
                    logger.error("监听参数无效，已停止监听服务")
                    raise
                if args.refresh_login_on_auth_error and is_authentication_error(exc):
                    if session:
                        logger.warning("检测到登录凭证失效，即将重新打开登录窗口")
                        monitor.set_browser_bridge(session.start(), session.token)
                    else:
                        logger.warning("请在当前门户浏览器会话中重新登录；本轮检查点未推进")
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        logger.info("已停止新工单监听服务")
    finally:
        if export_listener:
            logger.info("Stopping Feishu long-connection Excel export listener")
        if session:
            session.stop()


if __name__ == "__main__":
    run_cli_safely(
        main,
        failure_message="新工单监听服务失败",
        interrupted_message="已停止新工单监听服务",
    )
