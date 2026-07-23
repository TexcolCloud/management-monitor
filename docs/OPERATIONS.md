# 升级、运行、监控与回滚

本文以旧目录 `D:\CodeProgram\management-monitor` 和新目录
`D:\CodeProgram\management-monitor` 为例。原则是先停旧进程、保留旧目录、完整备份
PostgreSQL，再在新目录离线验证和显式迁移；不要把新文件直接覆盖到仍在运行的旧目录。

## 1. 升级前盘点和停止

1. 记录旧版本正在使用的 `PGHOST/PGPORT/PGDATABASE/PGSCHEMA/PGTABLE`，但不要把密码、
   Token、App Secret、Cookie 或 open_id 粘贴到工单、日志或命令行。
2. 记录旧版进程、调度器和服务管理器配置，确认是否同时运行主工作流和独立飞书监听器。
3. 使用旧服务本身的正常停止方式或 `Ctrl+C` 停止；确认不再有旧 Python/Node 进程写数据库
   或快照。
4. 保留整个旧目录，不执行 `git reset --hard`、`checkout`、递归删除或批量覆盖。

新工作区可能没有 Git 元数据，因此回滚不能只依赖 Git；目录备份和数据库 dump 是必需的。

## 2. 备份

创建只允许运维人员访问的备份目录，备份以下内容：

- 完整 PostgreSQL 数据库（至少包括业务表、迁移表、检查点和 outbox）；
- 旧目录的 `.env`，单独加密或限制 ACL；
- `config/`、`data_acquisition/recordings/` 中的用户修改；
- 旧 `capture-output/`、`excel-output/` 和必要日志；
- 旧 `.temp/work-order-monitor-state.json`（仅时间检查点，不要复制整个 `.temp`）；
- 调度器或服务定义。

推荐使用 PostgreSQL 客户端交互式提示密码，避免密码进入命令历史：

```powershell
pg_dump --format=custom --password --host "<PGHOST>" --port "<PGPORT>" --username "<PGUSER>" --file "<受限备份目录>\before-safety-monitor-upgrade.dump" "<PGDATABASE>"
pg_restore --list "<受限备份目录>\before-safety-monitor-upgrade.dump"
```

`pg_restore --list` 只检查归档目录，不等同于恢复演练。生产切换前应在隔离数据库实际执行一次
恢复并核对行数。

旧版本可能遗留 `auth-token.json`、`portal-storage-state.json`、含认证头的请求文件或浏览器
profile。先停止进程并纳入受限备份，再逐个确认内容和路径；不要使用通配符跨目录删除。新主
流程不依赖这些落盘凭证，也不要把它们复制到新快照目录。

## 3. 安装与离线预检

在新目录执行：

```powershell
Set-Location 'D:\CodeProgram\management-monitor'
conda activate management-monitor
python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
npm.cmd ci
$env:PLAYWRIGHT_DOWNLOAD_HOST = 'https://npmmirror.com/mirrors/playwright'
npx.cmd playwright install chromium
Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm.cmd run check
```

项目级 `.npmrc` 与锁文件固定使用 npmmirror；Python 依赖只从清华 PyPI 镜像解析。不要在
`management-monitor` 之外的 Python 环境安装或运行本项目，也不要通过旧项目的
`NODE_PATH` 复用依赖。

最后一条是离线检查，不登录门户、不连接正式 PostgreSQL、不发飞书。失败时不要进入迁移。

## 4. 迁移配置

1. 将旧 `.env` 中仍兼容的变量逐项填入新 `.env`，不要直接覆盖模板后忽略新增项。
2. 保持原 `PGSCHEMA` 和 `PGTABLE` 才会复用原业务表。确认新增
   `PGCONNECT_TIMEOUT`、可选 `PGSTATEMENT_TIMEOUT_MS/PGLOCK_TIMEOUT_MS` 及飞书重试变量。
3. 对比并手工合并 `config/runtime.json`、`config/classification_rules.json` 和门户录制文件的
   用户修改；不要把旧代码目录整体覆盖到新目录。
4. 保持 `FEISHU_EXPORT_ENABLED=false`，直到数据库与主采集验证通过。尚未准备发送真实通知时，
   同样暂不配置出站收件人。
5. 主流程不需要复制旧认证文件或旧浏览器 profile；首次启动重新人工登录。
6. 若目标数据库尚无对应 PostgreSQL 检查点，可检查旧
   `.temp/work-order-monitor-state.json` 只含预期时间字段后，将这一文件复制到新目录相同
   相对路径。监听器会一次性导入旧检查点；数据库已有检查点时不要覆盖。不要复制整个旧
   `.temp`，其中可能含过期会话或用户临时文件。

系统环境变量优先于 `.env`，`.env` 优先于 JSON 数据库配置。空值使用代码或 JSON 默认值。

## 5. 数据库预检与迁移

先在旧表副本或恢复演练数据库执行。迁移只增加结构，不自动修复脏数据；`006` 遇到列类型
冲突、空或重复 `safety_code` 时会整批回滚。

以下命令会真实连接并应用待执行迁移，必须显式执行：

```powershell
python -c "from database.postgres_store import load_config,connect,ensure_table; c=load_config(); db=connect(c); ensure_table(db,c); print('数据库连接和迁移成功'); db.close()"
```

迁移失败时保留错误日志和 dump，在副本中审计数据；不要删除迁移记录、约束或业务行来绕过
验证。修复方案需由数据负责人确认后再次执行。

迁移后在 `PGSCHEMA.workorder_schema_migrations` 核对目标表版本连续且 checksum 已记录：

```sql
SELECT target_table, version, name, applied_at
FROM <PGSCHEMA>.workorder_schema_migrations
WHERE target_table = '<PGSCHEMA>.<PGTABLE>'
ORDER BY version;
```

再核对业务数据，不应出现空或重复编号：

```sql
SELECT COUNT(*) AS rows,
       COUNT(DISTINCT safety_code) AS distinct_codes,
       COUNT(*) FILTER (WHERE safety_code IS NULL OR btrim(safety_code) = '') AS blank_codes,
       MIN(create_time) AS earliest_create_time,
       MAX(create_time) AS latest_create_time
FROM <PGSCHEMA>.<PGTABLE>;
```

## 6. 首次启动与验证

在维护窗口启动：

```powershell
npm.cmd run workflow:daily
```

1. 在 Playwright Chromium 中人工登录门户。
2. 确认日志显示最近 30 个自然日，而不是跳过初始化；起止边界应为首日零点和末日最后一微秒。
3. 等待 `capture-output/daily-management/current/acquisition-run-summary.json` 生成，检查实际范围、
   列表、详情和附件统计。
4. 确认 `partial-attempts/` 中的失败批次没有替换 `current/`。
5. 核对业务表总数、`safety_code` 唯一性和最近数据；确认 Excel 可打开且行数合理。
6. 确认 `workorder_monitor_checkpoints` 只在完整监听窗口提交后推进。
7. 使用 `Ctrl+C` 停止一次，确认无无意义 Traceback，再重新启动验证恢复。

不要仅凭日志“启动成功”判断采集成功；快照摘要、数据库行、检查点和 outbox 必须交叉核对。

## 7. outbox 与检查点监控

以下 SQL 中将 `<PGSCHEMA>` 和 `<PGTABLE>` 替换为已核对的安全标识。不要把查询结果中的业务
内容或收件人标识贴入公开渠道。

待发送、到期可领取和租约中的数量：

```sql
SELECT target_table,
       COUNT(*) FILTER (WHERE sent_at IS NULL) AS pending,
       COUNT(*) FILTER (
           WHERE sent_at IS NULL
             AND next_attempt_at <= CURRENT_TIMESTAMP
             AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
       ) AS due,
       COUNT(*) FILTER (
           WHERE sent_at IS NULL AND lease_until > CURRENT_TIMESTAMP
       ) AS leased,
       MIN(next_attempt_at) FILTER (WHERE sent_at IS NULL) AS oldest_next_attempt
FROM <PGSCHEMA>.workorder_feishu_notification_outbox
GROUP BY target_table
ORDER BY target_table;
```

只看配置业务表的失败分类，不输出 payload 或 `last_error` 原文：

```sql
SELECT error_kind, attempts, COUNT(*) AS tasks,
       MIN(next_attempt_at) AS next_attempt_at
FROM <PGSCHEMA>.workorder_feishu_notification_outbox
WHERE target_table = '<PGSCHEMA>.<PGTABLE>'
  AND sent_at IS NULL
GROUP BY error_kind, attempts
ORDER BY attempts DESC, error_kind;
```

检查点状态：

```sql
SELECT target_table, stream_name, last_success_at, revision, updated_at
FROM <PGSCHEMA>.workorder_monitor_checkpoints
WHERE target_table = '<PGSCHEMA>.<PGTABLE>'
ORDER BY stream_name;
```

告警建议：`due` 持续增长、最早 `next_attempt_at` 长时间不变化、租约超过正常 worker 周期、
`attempts` 持续升高、检查点长时间不推进，或 `partial-attempts` 连续产生。不要手工把失败任务
标记为已发送；先修复权限/网络/附件问题，让 worker 自动重试。

## 8. 显式真实联调

默认 `npm.cmd run check` 不执行以下操作。

门户与正式工作流联调：

```powershell
npm.cmd run workflow:daily
```

飞书长连接单独验证（仅在主工作流未运行同一监听器时）：

```powershell
npm.cmd run feishu:export-listener
```

飞书端到端模拟会真实连接隔离测试表并向配置目标发送模拟卡片、图片和 Excel：

```powershell
npm.cmd run feishu:simulation
```

确认日志中的表名符合 `feishu_sim_` 加 16 位本次随机标识。只用允许名单用户导出当天数据；
完成后输入“结束”或按 `Ctrl+C`。程序只清理本次随机表、对应迁移/outbox 行和临时附件。
若机器断电或被强制杀进程导致清理失败，先根据日志确认精确表名和 run id，再由 DBA 备份、
复核正则和所属运行后处理；禁止用 `feishu_sim_%` 通配删除，更不能操作正式 `PGTABLE`。

## 9. 回滚

### 仅回滚应用

1. 用 `Ctrl+C` 停止新版本，确认子进程结束。
2. 保存新版本日志、当前 outbox 和检查点状态；飞书已发送的远端消息无法回滚，忽略这一事实会
   在重放时造成重复消息。
3. 保留新目录和新快照，不覆盖旧目录。
4. 若评估确认旧版本可忽略新增的 additive 支持表/列，则恢复旧调度器，使用旧目录原配置启动。
5. 核对旧版不会以更早的文件检查点覆盖数据库中的更新状态；无法确认时使用下述完整回滚。

### 数据库完整回滚

优先恢复到一个新建的隔离数据库，再将旧应用指向它；不要直接覆盖仍在使用的生产库：

```powershell
createdb --password --host "<PGHOST>" --port "<PGPORT>" --username "<PGUSER>" "<新的回滚数据库名>"
pg_restore --password --exit-on-error --host "<PGHOST>" --port "<PGPORT>" --username "<PGUSER>" --dbname "<新的回滚数据库名>" "<受限备份目录>\before-safety-monitor-upgrade.dump"
```

核对业务表行数、唯一编号、迁移版本、outbox 和检查点后，再把旧应用的 `PGDATABASE` 指向该
回滚数据库并启动。必须原地恢复时，需进入维护窗口并由 DBA 按组织恢复流程执行；本文不提供
可能覆盖正式库的 `--clean` 命令。

回滚后再次验证：旧服务只运行一个实例、门户能登录、业务表未减少、快照仍可读取、未发送
outbox 未丢失。新版本投递到飞书但尚未本地确认的任务可能重复，需按至少一次语义处理。

## 10. 外部验证声明

本次重构默认只执行离线测试。真实门户登录、正式 PostgreSQL 迁移/锁行为、飞书应用权限、
真实收件人、WSS 长连接和外部网络超时均未由默认检查验证。上述每个真实步骤都必须由有权限的
运维人员显式启动、观察并记录结果，但记录中不得包含凭证或用户敏感标识。
