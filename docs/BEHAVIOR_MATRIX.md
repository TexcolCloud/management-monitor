# 业务不变量与测试映射

原始需求的“不可改变的业务行为”实际编号为 1 至 22（共 22 条），本文不省略最后一条。
实现列指出承载业务事实的核心模块；测试列是默认 `npm.cmd run check` 中的离线证据。
“mock 边界”表示已验证本地编排和参数，但没有声称真实门户、PostgreSQL 或飞书可用。

| # | 业务不变量 | 主要实现 | 离线特征测试 / 验证边界 |
| --- | --- | --- | --- |
| 1 | 主入口启动后先获取最近 30 个自然日 | `workflows/daily_workflow.py:main/build_capture_command`；`data_acquisition/time_range.py:DEFAULT_CAPTURE_DAYS` | `test_workflow_result.py::test_capture_command_uses_default_thirty_day_acquisition_without_date_arguments`；`test_workflow_always_captures_recent_thirty_days_when_database_has_records`；`test_runtime_and_paths.py::test_default_recent_range_covers_thirty_calendar_days` |
| 2 | 首日 `00:00:00` 至末日 `23:59:59.999999`，闭区间 | `safety_monitor/domain/time_window.py:recent_natural_days/parse_datetime` | `test_capture_components.py::test_parse_datetime_expands_date_end`、`test_recent_window_microseconds_reach_portal_request_body`；`test_export_application.py::test_feishu_timezone_variants_produce_closed_calendar_range`；30 天起止由 `test_default_recent_range_covers_thirty_calendar_days` 覆盖 |
| 3 | 登录、初始化和监听复用同一浏览器会话 | `data_acquisition/browser_session.py:BrowserSession`；组合根持有会话，采集与监听接收同一 browser bridge URL 和随机 token | `test_workflow_result.py::test_initial_capture_and_monitor_reuse_the_same_browser_bridge`；`test_daily_acquisition_snapshot.py::test_direct_acquisition_reuses_one_in_memory_browser_session`；`test_work_order_monitor.py::test_browser_session_keeps_logged_in_browser_and_publishes_bridge_url`；真实 SSO 仅能显式联调 |
| 4 | 获取列表、详情、附件；完整结果才原子替换快照 | `safety_monitor/application/acquire_snapshot.py:evaluate_snapshot`；`safety_monitor/adapters/filesystem_snapshot.py:FilesystemSnapshotPublisher`；`data_acquisition/run_daily_acquisition.py` | `test_daily_acquisition_snapshot.py::test_complete_capture_replaces_current_snapshot`、`test_publish_interruption_restores_previous_snapshot`、`test_snapshot_requires_expected_attachment_file_and_matching_sha256`；`test_attachment_staging.py::test_failed_download_preserves_published_attachments`、`test_successful_download_replaces_outputs_without_deleting_user_files` |
| 5 | 部分结果只用于诊断，不覆盖完整快照 | `run_daily_acquisition.py:_publish_partial_attempt`；工作流在 `capture_data_complete=false` 时停止 | `test_daily_acquisition_snapshot.py::test_partial_capture_is_published_as_diagnostic_attempt`；`test_workflow_result.py::test_workflow_does_not_import_partial_capture`；`test_database_and_export.py::test_data_dir_records_complete_rejects_partial_capture` |
| 6 | 按 `safety_code` 去重并写 PostgreSQL | `safety_monitor/domain/work_order.py:work_order_identity/deduplicate_work_orders`；业务表唯一约束；`PostgresMonitoringUnitOfWork` upsert | `test_capture_components.py::test_deduplicate_uses_safety_code_as_business_identity`、`test_capture_rejects_duplicates_that_mask_missing_rows`；`test_database_persistence.py::test_work_order_outbox_and_checkpoint_commit_together`；真实唯一约束由显式 PG 验证 |
| 7 | 迁移可重复执行并兼容已有数据 | `database/migrations.py` 的编号、checksum 白名单、advisory lock；`001` 至 `006` additive migrations | `test_database_persistence.py::test_original_migration_files_are_byte_for_byte_unchanged`、`test_only_known_001_checksums_are_accepted`、`test_apply_migrations_accepts_known_alias_and_takes_transaction_lock`、`test_apply_migrations_rejects_unknown_checksum_and_rolls_back`、`test_existing_legacy_table_is_reconciled_before_older_index_migration`；正式旧表未默认联调 |
| 8 | 初始化后持续监听；窗口和检查点可重试 | `safety_monitor/domain/monitoring.py:PollingPolicy`；`application/monitoring.py:MonitorNewOrders`；PostgreSQL checkpoint repository | `test_work_order_monitor.py::test_poll_window_retries_since_last_success_after_failure`；`test_monitoring_application.py::test_complete_window_is_persisted_with_checkpoint_in_one_port_call`；`test_database_persistence.py::test_checkpoint_repository_locks_and_updates_by_revision` |
| 9 | 抓取、入库等必要步骤成功后才推进检查点 | `CapturedWindow.complete_records` 拒绝分页、详情或身份不完整；`PostgresMonitoringUnitOfWork.persist_complete_window` 同事务 CAS 提交 | `test_monitoring_application.py::test_incomplete_capture_never_reaches_transaction_port`、`test_missing_safety_code_does_not_advance_checkpoint`；`test_database_persistence.py::test_outbox_failure_rolls_back_work_orders_and_checkpoint`、`test_checkpoint_conflict_rolls_back_before_any_business_write`；`test_work_order_monitor.py::test_failed_detail_does_not_persist_checkpoint` |
| 10 | 新工单入库后发送飞书卡片和图片附件 | UoW 只为新插入 code 入 outbox；通知 dispatcher 调用 `common/feishu_app_bot.py` 并保存卡片/图片进度 | `test_database_persistence.py::test_work_order_outbox_and_checkpoint_commit_together`；`test_feishu_outbound_bot.py::test_send_work_order_uses_outbound_api_without_callback`、`test_send_image_uploads_then_posts_image_key`；mock 边界不发真实消息 |
| 11 | 飞书失败持久排队并自动重试，不静默丢失 | `PostgresOutboxRepository` 租约领取、分段确认和失败记录；迁移 `002/003/005`；有界指数退避 | `test_database_persistence.py::test_pending_uses_relational_code_and_only_returns_due_unleased_rows`、`test_claim_uses_skip_locked_and_persists_a_lease`、`test_failure_is_sanitized_classified_and_exponentially_delayed`；`test_work_order_monitor.py::test_feishu_delivery_sends_only_unfinished_messages_after_image_failure` |
| 12 | 通知目标由 `FEISHU_RECEIVE_ID` 配置 | `FeishuOutboundBot.from_environment`；`FEISHU_RECEIVE_ID_TYPE`；旧 `FEISHU_CHAT_ID` fallback | `test_feishu_outbound_bot.py::test_send_work_order_supports_direct_user_recipient`；`test_work_order_monitor.py::test_monitor_loads_environment_before_initializing_feishu_outbound_bot`；真实目标未默认联调 |
| 13 | Excel 自助导出用官方 SDK 长连接，不依赖公网回调 | `workflows/feishu_export_service.py:run` 使用 `lark.EventDispatcherHandler` 和 `lark.ws.Client` 注册消息及卡片事件 | `test_feishu_export_service.py` 离线覆盖事件处理；SDK 到飞书 WSS 的握手、应用事件配置需执行 `feishu:export-listener` 或 `feishu:simulation` 实测 |
| 14 | 仅允许 `FEISHU_EXPORT_ALLOWED_OPEN_IDS` 中用户导出 | `safety_monitor/domain/exporting.py:ExportPolicy`；消息发送者和卡片操作者均重新授权 | `test_export_application.py::test_chinese_and_custom_commands_are_authorized_offline`、`test_card_operator_is_reauthorized_and_receives_the_job`；`test_feishu_export_service.py::test_unauthorized_or_non_command_event_cannot_export` |
| 15 | 中文或兼容命令触发中文日期选择卡片 | `ExportPolicy.accepts_command`；`FeishuExportService.export_card` | `test_export_application.py::test_chinese_and_custom_commands_are_authorized_offline`；`test_feishu_export_service.py::test_authorized_export_commands_send_date_card_to_sender`、`test_export_card_contains_date_form_and_submit_action` |
| 16 | 飞书日期可含 `+0800` 等时区后缀 | `safety_monitor/domain/exporting.py:normalize_feishu_date/ExportDateRange` | `test_export_application.py::test_feishu_timezone_variants_produce_closed_calendar_range`；`test_feishu_export_service.py::test_date_card_submits_a_valid_date_range_for_export` |
| 17 | 提交日期后按 `create_time` 闭区间查 PostgreSQL，不访问门户 | `database/postgres_store.py:rows_by_create_time`；`FeishuExportService.export_for_open_id` | `test_database_and_export.py::test_rows_by_create_time_loads_incomplete_raw_rows_inclusive`；`test_feishu_export_service.py::test_export_for_open_id_reads_rows_from_database`、`test_export_service_does_not_require_a_capture_directory` |
| 18 | 已入库不完整记录仍导出，缺失字段留空 | `postgres_store._export_row_from_record` 从关系列重建 `raw_data IS NULL` 行；`data_export.daily_management_excel.clean_text/table_rows` | `test_database_persistence.py::test_export_reconstructs_raw_null_row_from_relational_columns`；`test_database_and_export.py::test_rows_by_create_time_loads_incomplete_raw_rows_inclusive` |
| 19 | Excel 私发实际请求用户，不发原群聊 | `ExportRequest.requester_open_id`；`send_export_card`、`export_for_open_id` 均使用发送者/操作者 `open_id` | `test_export_application.py::test_card_operator_is_reauthorized_and_receives_the_job`；`test_feishu_export_service.py::test_authorized_export_commands_send_date_card_to_sender`、`test_export_for_open_id_reads_rows_from_database` |
| 20 | `Ctrl+C` 平稳停止且无无意义 Traceback | `daily_workflow.main`、monitor/export/simulation 入口捕获 `KeyboardInterrupt` 并在 `finally` 清理拥有的资源 | `test_workflow_result.py::test_workflow_handles_keyboard_interrupt_without_traceback`；`test_work_order_monitor.py::test_ctrl_c_during_login_stops_without_traceback`；真实信号传播仍需显式进程级冒烟验证 |
| 21 | 联调使用随机测试表和模拟附件；“结束”或 `Ctrl+C` 清理 | `workflows/feishu_simulation.py` 生成 UUID 表名、模拟工单和 1x1 图片，`finally` 清理 | `test_feishu_simulation.py::test_simulation_names_and_rows_are_isolated_and_exportable`、`test_simulation_table_environment_restores_existing_value_after_failure`；真实清理由显式联调验证 |
| 22 | 不误删正式表、正式快照、正式附件或用户修改 | 模拟表名正则与 run id 双重校验；快照发布只操作事务拥有路径；路径限制和迁移均为 additive | `test_feishu_simulation.py::test_cleanup_refuses_a_non_simulation_table_before_connecting`、`test_cleanup_refuses_a_different_simulation_run`；`test_runtime_and_paths.py::test_project_path_guard_and_slug`；`test_attachment_staging.py::test_attachment_store_preserves_content_owned_by_older_snapshots`、`test_current_snapshot_preserves_legacy_range_attachment_copies`；迁移冻结/回滚见 `test_database_persistence.py` |

## 失败边界补充

默认检查还覆盖下列交叉风险：

- 分页：空页提前终止、报告页数超限、总数不一致、重复项掩盖缺行；见
  `tests/test_capture_components.py`。
- 详情与附件：详情失败隔离，附件失败保留旧发布，manifest 路径与哈希验证；见
  `tests/test_database_and_export.py`、`tests/test_daily_acquisition_snapshot.py` 和
  `tests/test_attachment_staging.py`。
- 外部错误：门户/飞书临时错误重试，永久 4xx 停止本次 HTTP 重试，错误脱敏且有界；见
  `tests/test_capture_components.py`、`tests/test_feishu_outbound_bot.py`、
  `tests/test_logging_redaction.py`。
- 浏览器认证：本地 Chromium 集成测试覆盖同源 `sessionStorage`、跨源 iframe
  `localStorage`、仅内存 Authorization 回退、伪造调用方认证头覆盖、二进制附件复用以及
  无认证 fail-closed；见 `tests/test-portal-window.js`。该测试不访问真实门户。
- 数据库：迁移 checksum、锁、事务回滚、检查点 CAS、outbox 租约和重试排期；见
  `tests/test_database_persistence.py`。
- Excel：时区、闭区间、不完整行、工作表合法性、公式安全和文件回读；见
  `tests/test_export_application.py` 与 `tests/test_database_and_export.py`。

## 验证声明

`npm.cmd run check` 不依赖真实门户登录，不发送真实飞书消息，不修改正式 PostgreSQL。
因此它证明领域行为、适配器契约和失败编排，而不证明真实账号权限、网络、门户页面结构、
数据库历史数据质量或飞书应用配置。外部验证必须按 `docs/OPERATIONS.md` 使用显式命令执行。
