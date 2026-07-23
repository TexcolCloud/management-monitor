# 数据采集与快照发布

本目录是门户和附件基础设施适配层。时间窗口、工单身份、合并及快照完整性规则位于
`safety_monitor/domain/` 和 `safety_monitor/application/`，可在无真实门户的情况下测试。

## 主流程

生产入口是：

```powershell
npm.cmd run workflow:daily
```

每次启动固定采集最近 30 个自然日，首日从 `00:00:00` 开始，末日到
`23:59:59.999999` 结束。数据库已有数据不会跳过初始化。程序先打开门户供人工登录，
随后通过同一个浏览器会话完成列表分页、详情、附件和后续监听，无需第二次登录。

模块职责：

- `run_daily_acquisition.py`：兼容 CLI 与步骤编排。
- `time_range.py`：30 个自然日默认范围。
- `capture_components.py`：门户页、详情请求和分页适配器。
- `acquisition_steps.py`：登录进程、附件进程、受控路径及结果传递。
- `download-daily-attachments.js`：流式附件下载、哈希、重试和复用。
- `run_summary.py`：批次摘要。
- `record-portal-clicks.js`：Playwright 登录、跳转和本机请求桥。
- `recordings/portal-navigation-script.json`：可分发的点击路径，不含账号或会话凭证。

## 会话与认证

主工作流启动一个仅监听 `127.0.0.1` 的浏览器请求桥。桥使用每次随机生成的 bearer，且只
允许配置的列表、详情和附件 API；采集与监听共享该桥和浏览器内存中的登录状态。桥始终在
实际发出日常管理请求的同源 frame 中执行，读取该 frame 的 local/session storage，并以仅
保存在 Node 进程内存中的最近一次门户 Authorization 作为回退。没有认证证据或同源 frame
时不会发布 ready 文件，也不会向门户发送匿名请求。

新的主流程使用非持久内存 browser context，不生成认证 profile、`auth-token.json` 或
`portal-storage-state.json`；`--profile-dir` / `WORKORDER_LOGIN_PROFILE_DIR` 只保留参数兼容。
保存的请求结构会删除
Authorization、Cookie 等敏感头；URL 日志删除 userinfo、query 和 fragment，诊断不保存远端
响应正文。`WORKORDER_TOKEN` 及旧认证
文件读取仅为旧版调试兼容，不是主流程的凭证存储方案；不要把真实 Token 放进 `.env`、命令行
或项目文件。

门户页面结构变化、既有点击路径无法进入日常管理时，才重新录制：

```powershell
npm.cmd run record:portal-navigation
```

人工登录后依次进入“云网运营管理中台 / 安全管理 / 日常管理”，等待列表请求出现。录制文件
只应包含脱敏点击路径；生成后仍应在提交前检查差异。

## 完整性与发布

默认正式快照为：

```text
capture-output/daily-management/current/
```

采集在同级、由本次运行拥有的暂存目录中完成。发布前统一验证：

- 列表报告总数与按身份去重后的实际总数一致，分页未超限或异常提前结束；
- 每条列表工单都有身份一致且包含关键字段的详情；
- 详情声明的全部附件与 manifest 一一对应；
- manifest 状态成功、路径仍在附件根目录、文件存在且 SHA-256 相符；
- 必需 JSON 可读取，且批次摘要已在发布前生成。

全部满足后，`FilesystemSnapshotPublisher` 才用带事务标记和备份的切换替换 `current/`；
进程中断后下次启动可恢复。任何不完整结果只进入 `partial-attempts/` 或失败诊断目录，不改变
正式快照、不入库、不进入监听。

`--allow-partial`、`--max-detail-failures` 和 `--max-attachment-failures` 只用于保留诊断证据，
不把部分批次升级为可发布、可入库的完整数据。

## 附件

逻辑附件平铺在 `current/daily-attachments/`。下载使用受限并发、超时、重试及流式 SHA-256；
内容存储位于同级受管理的 `attachment-store/<sha256>`，支持按来源索引复用。成功发布后只清理
该受管理存储中不再被当前 manifest 引用的内容，不遍历或删除项目外路径、用户文件、旧正式
快照或非本次运行拥有的目录。

并发、超时、重试和失败阈值默认位于 `config/runtime.json`，可由
`WORKORDER_ATTACHMENT_CONCURRENCY`、`WORKORDER_ATTACHMENT_TIMEOUT_MS`、
`WORKORDER_ATTACHMENT_RETRIES` 和 `WORKORDER_ATTACHMENT_MAX_FAILURES` 覆盖。

## 主要输出

- `management-api-all.json` / `.csv` / `-excel-safe.csv`：列表及统计。
- `management-api-details.json` / `.csv` / `-excel-safe.csv`：成功详情。
- `management-api-detail-failures.*`：详情失败诊断，不参与正式入库。
- `daily-management-request.json`：脱敏请求结构和筛选字段，不含认证头。
- `daily-attachments/`：当前逻辑附件。
- `daily-attachments-manifest.*`：附件来源、状态、路径和哈希。
- `acquisition-run-summary.json`：实际时间范围与批次结果。
- `acquisition.log`、`capture.log`：带运行 ID 的脱敏诊断日志。

普通 CSV 保留原始值供程序读取；`-excel-safe.csv` 转义公式前缀。XLSX 写入保留原始文本，
同时把公式前缀单元格强制设为字符串。

默认测试使用 fake/mock 门户和本地临时目录，不依赖真实登录。真实门户、网络超时和账号权限
只在显式执行 `npm.cmd run workflow:daily` 时验证。
