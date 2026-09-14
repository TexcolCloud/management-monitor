# PostgreSQL 配置、迁移与恢复

PostgreSQL 是工单、监听检查点、飞书待发送任务和 Excel 自助导出的持久化事实来源。
文件快照不能替代数据库事务。

## 配置

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

从 `.env.example` 创建本机 `.env`。不要把密码写入 `config/database.json`，不要提交
`.env`。进程环境变量优先于 `.env`，`.env` 优先于 `config/database.json`。

```dotenv
PG_ENABLED=true
PGHOST=127.0.0.1
PGPORT=5432
PGDATABASE=postgres
PGUSER=postgres
PGPASSWORD=
PGSCHEMA=public
PGTABLE=work_orders
PGCONNECT_TIMEOUT=10
PGSTATEMENT_TIMEOUT_MS=
PGLOCK_TIMEOUT_MS=
```

- `PGTABLE` 是业务表；升级时保持原值即可继续使用已有数据。
- `PGCONNECT_TIMEOUT` 是连接超时秒数。
- `PGSTATEMENT_TIMEOUT_MS`、`PGLOCK_TIMEOUT_MS` 是可选的非负毫秒数；留空时沿用
  PostgreSQL 默认值。
- Schema、表名只接受安全 PostgreSQL 标识符，不接受拼接 SQL。

## 表与语义

迁移在 `PGSCHEMA` 中维护：

- `PGTABLE`：工单事实表，`safety_code` 唯一；`create_time` 用于闭区间导出；
  `raw_data` 保留门户数据语义。
- `workorder_schema_migrations`：按目标业务表记录迁移版本和 SHA-256。
- `workorder_monitor_checkpoints`：按 `target_table + stream_name` 保存最后成功窗口、
  CAS `revision` 和更新时间。
- `workorder_feishu_notification_outbox`：按 `target_table + safety_code` 唯一保存通知、
  卡片/图片发送进度、失败分类、下次重试时间和领取租约。

初始化完整快照按 `safety_code` upsert。监听窗口在一个事务中完成：

1. 锁定并校验期望检查点；
2. 插入或更新完整工单；
3. 只为真正新增的 `safety_code` 创建 outbox 任务；
4. 推进检查点；
5. 提交。任一步骤失败则全部回滚。

outbox worker 通过 `FOR UPDATE SKIP LOCKED` 和到期租约领取任务。临时失败增加
`attempts` 并按指数退避设置 `next_attempt_at`；卡片和每张图片分别确认进度。数据库不会
把失败任务静默删除。

## 可重复迁移与旧数据兼容

应用第一次连接目标表时自动调用 `database/migrations.py`：

1. 获取 Schema 级 PostgreSQL advisory transaction lock；
2. 创建迁移记录表；
3. 按编号执行尚未应用的迁移；
4. 校验已应用迁移的 SHA-256；
5. 整个失败批次回滚。

`001` 至既有版本不可修改。迁移器只接受当前 checksum 和两枚已知历史 `001` checksum；
其他变化会拒绝启动。新结构一律通过更高编号的 additive migration 增加。

迁移 `006` 用于兼容早期业务表：先补缺失列，再验证 `safety_code`、`create_time`、
`raw_data` 类型以及空值和重复 `safety_code`。只有数据满足约束才补齐 NOT NULL、唯一约束、
注释和索引；发现脏数据时明确失败并回滚，不自动删除、合并或改写用户数据。应先人工审计并
修复副本，再重新执行迁移。

不要通过删除业务表或迁移记录来“重新初始化”。升级前使用 `pg_dump` 备份，步骤见
`docs/OPERATIONS.md`。

## 显式连接与迁移验证

以下命令会连接配置的真实 PostgreSQL，并可能应用待执行迁移，因此不属于默认离线测试：

```powershell
python -c "from database.postgres_store import load_config,connect,ensure_table; c=load_config(); db=connect(c); ensure_table(db,c); print('数据库连接和迁移成功'); db.close()"
```

只做离线代码与测试检查：

```powershell
npm.cmd run check
```

本次重构的默认验证没有连接正式 PostgreSQL；生产连接、权限、锁等待、旧表数据质量和备份
可恢复性必须由运维人员在隔离或维护窗口中显式验证。
