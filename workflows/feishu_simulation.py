from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from common.logging_utils import setup_logging
from common.runtime_config import load_runtime_config
from common.workflow_paths import PROJECT_ROOT
from database.migrations import quote_identifier
from database.postgres_store import (
    DEFAULT_CONFIG_PATH,
    connect,
    ensure_table,
    feishu_outbox_table,
    load_config,
    load_env_file,
    migration_target_table,
    qualified_table,
)
from workflows.feishu_export_service import FeishuExportService, start_feishu_export_listener
from workflows.work_order_monitor import DownloadedImage, WorkOrderMonitor


TEST_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLq7wAAAABJRU5ErkJggg=="
)
SIMULATION_TABLE_PATTERN = re.compile(r"^feishu_sim_[a-z0-9]{16}$", re.IGNORECASE)


def simulation_table_name(run_id: str) -> str:
    return f"feishu_sim_{run_id.replace('-', '')[:16]}"


def require_simulation_table_name(table_name: str) -> str:
    if not SIMULATION_TABLE_PATTERN.fullmatch(table_name):
        raise ValueError("拒绝操作非飞书联调随机测试表。")
    return table_name


@contextmanager
def simulation_table_environment(table_name: str) -> Iterator[None]:
    require_simulation_table_name(table_name)
    had_original = "PGTABLE" in os.environ
    original_table = os.environ.get("PGTABLE")
    os.environ["PGTABLE"] = table_name
    try:
        yield
    finally:
        if had_original and original_table is not None:
            os.environ["PGTABLE"] = original_table
        else:
            os.environ.pop("PGTABLE", None)


def simulated_work_order(run_id: str, created_at: datetime) -> dict[str, str]:
    code_suffix = run_id.replace("-", "")[:8].upper()
    return {
        "id": f"simulation-{run_id}",
        "safetyCode": f"SIM-{code_suffix}",
        "companyName": "飞书联调测试单位",
        "safetyType": "模拟安全管理工单",
        "theme": "飞书卡片、附件和 Excel 导出联调测试",
        "createBy": "飞书联调服务",
        "createTime": created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def simulation_monitor_args(state_file: Path) -> argparse.Namespace:
    return argparse.Namespace(state_file=state_file, database_config=DEFAULT_CONFIG_PATH)


def cleanup_simulation(
    config: Mapping[str, Any],
    expected_table: str | None = None,
) -> None:
    table_name = require_simulation_table_name(str(config.get("table") or ""))
    if expected_table is not None and table_name != require_simulation_table_name(expected_table):
        raise ValueError("拒绝清理非本次运行创建的飞书联调测试表。")
    connection = connect(config)
    schema = quote_identifier(str(config.get("schema", "public")))
    migrations = f'{schema}."workorder_schema_migrations"'
    target_table = migration_target_table(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM %s WHERE target_table = %%s" % feishu_outbox_table(config),
                (target_table,),
            )
            cursor.execute("DROP TABLE IF EXISTS %s" % qualified_table(config))
            cursor.execute("DELETE FROM %s WHERE target_table = %%s" % migrations, (target_table,))
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    load_env_file()
    run_id = uuid.uuid4().hex
    table_name = simulation_table_name(run_id)
    temp_root = PROJECT_ROOT / ".temp" / f"feishu-simulation-{run_id}"
    logger = setup_logging()
    config: Mapping[str, Any] | None = None
    database_ready = False

    with simulation_table_environment(table_name):
        try:
            config = load_config()
            if not bool(config.get("enabled")):
                raise RuntimeError("飞书联调需要启用 PostgreSQL。")
            connection = connect(config)
            try:
                ensure_table(connection, config)
                database_ready = True
            finally:
                connection.close()
            export_service = FeishuExportService.from_environment(logger)
            if not export_service.enabled:
                raise RuntimeError("飞书联调需要设置 FEISHU_EXPORT_ENABLED=true。")

            runtime = load_runtime_config()
            image_path = temp_root / "simulation-attachment.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(TEST_IMAGE)
            row = simulated_work_order(run_id, datetime.now())

            monitor = WorkOrderMonitor(simulation_monitor_args(temp_root / "monitor-state.json"), runtime, logger)
            if not monitor.feishu_bot.enabled:
                raise RuntimeError("飞书联调需要配置 FEISHU_RECEIVE_ID 和飞书应用凭证。")
            monitor._download_image_attachments = lambda _row, _data_dir: [
                DownloadedImage("simulation-attachment.png", image_path)
            ]

            new_codes, upserted = monitor._store_rows([row])
            sent, failed = monitor._deliver_feishu_notifications()
            if row["safetyCode"] not in new_codes or upserted != 1 or sent != 1 or failed:
                raise RuntimeError("模拟工单未能完整发送，请检查日志中的飞书错误。")

            listener = start_feishu_export_listener(logger)
            if listener is None:
                raise RuntimeError("飞书 Excel 导出监听器未启动。")
            logger.info("模拟工单卡片和图片附件已发送至 FEISHU_RECEIVE_ID。")
            logger.info("请用允许名单中的用户发送“导出Excel表格”，并选择今天的日期验证 Excel 私发。")
            while input("输入“结束”后删除模拟数据并退出：").strip() != "结束":
                logger.info("等待“结束”命令；模拟监听仍在运行。")
        except KeyboardInterrupt:
            logger.info("收到停止信号，正在清理飞书联调数据。")
        finally:
            if config is not None and database_ready:
                try:
                    cleanup_simulation(config, expected_table=table_name)
                    logger.info("已删除飞书联调测试表和待发送记录。")
                except Exception as exc:
                    logger.error(
                        "飞书联调数据清理失败；请保留日志并手动处理。错误类型=%s",
                        type(exc).__name__,
                    )
            try:
                shutil.rmtree(temp_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.error("飞书联调临时文件清理失败。错误类型=%s", type(exc).__name__)


if __name__ == "__main__":
    main()
