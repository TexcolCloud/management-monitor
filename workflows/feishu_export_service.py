from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from common.feishu_app_bot import FeishuOutboundBot, FeishuRetryPolicy
from common.workflow_paths import PROJECT_ROOT
from data_export.daily_management_excel import table_rows, write_xlsx
from database.postgres_store import rows_by_create_time
from safety_monitor.application.export_service import ExportApplicationService
from safety_monitor.domain.exporting import (
    CARD_ACTION,
    CARD_COMMAND,
    FEISHU_DATE_PATTERN,
    ExportPolicy,
    ExportRequest,
    ExportRequestError,
    form_date_text,
    normalize_feishu_date,
)


LOGGER = logging.getLogger("workorder_daily_manage")
DEFAULT_COMMAND = "export"
DEFAULT_FILENAME = "daily-management-export.xlsx"
CARD_DATE_WITH_TIMEZONE = FEISHU_DATE_PATTERN


def _enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("FEISHU_EXPORT_ENABLED 必须为布尔值。")


def _open_ids(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class FeishuExportService:
    app_id: str
    app_secret: str = field(repr=False)
    allowed_open_ids: frozenset[str] = field(repr=False)
    command: str
    timeout_seconds: float
    enabled: bool
    logger: logging.Logger = LOGGER
    retry_policy: FeishuRetryPolicy = field(default_factory=FeishuRetryPolicy)

    @classmethod
    def from_environment(cls, logger: logging.Logger = LOGGER) -> "FeishuExportService":
        enabled = _enabled(os.environ.get("FEISHU_EXPORT_ENABLED", ""))
        command = os.environ.get("FEISHU_EXPORT_COMMAND", DEFAULT_COMMAND).strip()
        timeout_text = os.environ.get("FEISHU_APP_TIMEOUT_SECONDS", "10").strip()
        try:
            timeout_seconds = float(timeout_text)
        except ValueError as exc:
            raise ValueError("FEISHU_APP_TIMEOUT_SECONDS 必须为数字。") from exc
        if timeout_seconds <= 0:
            raise ValueError("FEISHU_APP_TIMEOUT_SECONDS 必须大于 0。")

        service = cls(
            app_id=os.environ.get("FEISHU_APP_ID", "").strip(),
            app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip(),
            allowed_open_ids=_open_ids(os.environ.get("FEISHU_EXPORT_ALLOWED_OPEN_IDS", "")),
            command=command,
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            logger=logger,
            retry_policy=FeishuRetryPolicy.from_environment(),
        )
        if enabled:
            service.validate_configuration()
        return service

    def validate_configuration(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError("Excel 导出需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET。")
        if not self.allowed_open_ids:
            raise ValueError("FEISHU_EXPORT_ALLOWED_OPEN_IDS 至少需要配置一个 open_id。")
        if not self.command:
            raise ValueError("FEISHU_EXPORT_COMMAND 不能为空。")

    def _application(self) -> ExportApplicationService:
        return ExportApplicationService(
            policy=ExportPolicy(self.allowed_open_ids, self.command),
            send_date_selection_card=self.send_export_card,
            submit_export_job=self._schedule_export,
        )

    def _log_safe_failure(self, message: str, error: Exception) -> None:
        self.logger.error("%s；错误类型=%s", message, type(error).__name__)

    def handle_event(self, event: Mapping[str, Any]) -> bool:
        sender = event.get("sender")
        message = event.get("message")
        if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
            return False
        sender_id = sender.get("sender_id")
        open_id = str(sender_id.get("open_id") or "").strip() if isinstance(sender_id, Mapping) else ""
        message_type = str(message.get("message_type") or "")
        try:
            content = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError:
            return False
        command = str(content.get("text") or "").strip() if isinstance(content, Mapping) else ""
        try:
            return self._application().handle_command(open_id, message_type, command)
        except Exception as exc:
            self._log_safe_failure("已授权用户的 Excel 导出卡片发送失败", exc)
            return True

    def _bot_for_open_id(self, open_id: str) -> FeishuOutboundBot:
        return FeishuOutboundBot(
            app_id=self.app_id,
            app_secret=self.app_secret,
            receive_id=open_id,
            timeout_seconds=self.timeout_seconds,
            receive_id_type="open_id",
            retry_policy=self.retry_policy,
        )

    @staticmethod
    def export_card() -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Excel 表格导出"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "请选择按创建时间（createTime）筛选的起止日期，再提交导出。",
                    },
                },
                {
                    "tag": "form",
                    "name": "export_date_range",
                    "elements": [
                        {
                            "tag": "date_picker",
                            "name": "start_date",
                            "required": True,
                            "placeholder": {"tag": "plain_text", "content": "开始日期"},
                        },
                        {
                            "tag": "date_picker",
                            "name": "end_date",
                            "required": True,
                            "placeholder": {"tag": "plain_text", "content": "结束日期"},
                        },
                        {
                            "tag": "button",
                            "name": CARD_ACTION,
                            "text": {"tag": "plain_text", "content": "导出Excel表格"},
                            "type": "primary",
                            "action_type": "form_submit",
                            "value": {"action": CARD_ACTION},
                        },
                    ],
                },
            ],
        }

    def send_export_card(self, open_id: str) -> None:
        self._bot_for_open_id(open_id).send_interactive_card(self.export_card())

    @staticmethod
    def _form_date(value: Any) -> str:
        try:
            return normalize_feishu_date(value).isoformat()
        except ExportRequestError:
            return form_date_text(value)

    def _schedule_export(self, request: ExportRequest) -> None:
        threading.Thread(
            target=self._export_date_range,
            args=(request.requester_open_id, request.date_range.start, request.date_range.end),
            name="feishu-date-export",
            daemon=True,
        ).start()

    def handle_card_action(
        self,
        open_id: str,
        action_value: Mapping[str, Any] | None,
        form_value: Mapping[str, Any] | None,
    ) -> tuple[bool, str]:
        try:
            result = self._application().handle_action(open_id, action_value, form_value)
        except Exception as exc:
            self._log_safe_failure("Excel 导出任务启动失败", exc)
            return False, "Excel 导出任务启动失败，请稍后重试。"
        return result.accepted, result.message

    def _export_date_range(self, open_id: str, start: datetime, end: datetime) -> None:
        try:
            self.export_for_open_id(open_id, start, end)
        except Exception as exc:
            self._log_safe_failure("按日期导出 Excel 失败", exc)
            try:
                self._bot_for_open_id(open_id).send_text("Excel 导出失败，请检查数据库连接和配置后重试。")
            except Exception as notification_error:
                self._log_safe_failure("无法发送 Excel 导出失败通知", notification_error)

    def export_for_open_id(self, open_id: str, start: datetime, end: datetime) -> None:
        if open_id not in self.allowed_open_ids:
            raise ValueError("你没有导出该数据的权限。")
        rows = table_rows(rows_by_create_time(start, end))
        temp_root = PROJECT_ROOT / ".temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="feishu-excel-export-", dir=temp_root) as temp_dir:
            output_path = Path(temp_dir) / f"{start:%Y%m%d}-{end:%Y%m%d}-{DEFAULT_FILENAME}"
            write_xlsx(output_path, "Daily Management", rows)
            self._bot_for_open_id(open_id).send_file(str(output_path))
        self.logger.info("已发送日期范围 Excel 导出文件：%s..%s", start.date(), end.date())

    def run(self) -> None:
        if not self.enabled:
            return
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                "飞书长连接 Excel 导出依赖 lark-oapi，请安装 requirements.txt 中的依赖。"
            ) from exc

        def receive_message(data: Any) -> None:
            try:
                payload = lark.JSON.marshal(data)
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if isinstance(payload, Mapping):
                    event = payload.get("event", payload)
                    if isinstance(event, Mapping):
                        self.handle_event(event)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.logger.warning(
                    "已忽略无法解析的飞书长连接事件；错误类型=%s",
                    type(exc).__name__,
                )

        def receive_card_action(data: Any) -> Any:
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

            event = getattr(data, "event", None)
            operator = getattr(event, "operator", None)
            action = getattr(event, "action", None)
            accepted, message = self.handle_card_action(
                str(getattr(operator, "open_id", "") or ""),
                getattr(action, "value", None),
                getattr(action, "form_value", None),
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "success" if accepted else "error",
                        "content": message,
                    }
                }
            )

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(receive_message)
            .register_p2_card_action_trigger(receive_card_action)
            .build()
        )
        self.logger.info("正在启动飞书长连接 Excel 导出监听器")
        lark.ws.Client(self.app_id, self.app_secret, event_handler=event_handler).start()


def start_feishu_export_listener(logger: logging.Logger = LOGGER) -> threading.Thread | None:
    service = FeishuExportService.from_environment(logger)
    if not service.enabled:
        return None
    try:
        import lark_oapi  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "飞书长连接 Excel 导出依赖 lark-oapi，请安装 requirements.txt 中的依赖。"
        ) from exc
    def run_listener() -> None:
        try:
            service.run()
        except Exception:
            logger.exception("飞书长连接 Excel 导出监听器异常退出")

    thread = threading.Thread(target=run_listener, name="feishu-export-listener", daemon=True)
    thread.start()
    return thread


def main() -> None:
    from database.postgres_store import load_env_file
    from common.logging_utils import setup_logging

    logger = setup_logging(PROJECT_ROOT / ".temp" / "feishu-export-listener.log")
    load_env_file()
    service = FeishuExportService.from_environment(logger)
    if not service.enabled:
        raise SystemExit("未启用飞书 Excel 导出。请在 .env 中设置 FEISHU_EXPORT_ENABLED=true 后重试。")
    try:
        service.run()
    except KeyboardInterrupt:
        logger.info("飞书长连接 Excel 导出监听器已停止")


if __name__ == "__main__":
    from common.cli import run_cli_safely

    run_cli_safely(
        main,
        failure_message="飞书 Excel 导出监听器启动失败",
        interrupted_message="飞书 Excel 导出监听器已停止",
    )
