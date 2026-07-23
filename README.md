# 工单安全管理监控

本项目采集门户“安全管理 / 日常管理”工单及附件，将完整数据按 `safety_code`
幂等写入 PostgreSQL，并在初始化后持续监听新工单。新增工单通过飞书应用机器人发送
卡片和图片附件；获授权用户可通过飞书 SDK 长连接按日期自助导出数据库中的 Excel。

主入口每次启动都先采集最近 **30 个自然日**，无论数据库是否已有记录。时间范围固定为
首日 `00:00:00` 至末日 `23:59:59.999999`，两端均包含。初始化采集和后续监听复用
同一个已登录浏览器会话。

## 环境要求

- Windows PowerShell
- Conda 环境 `management-monitor`
- Python 3.10+
- Node.js 18+
- PostgreSQL 12+
- 能访问门户和飞书开放平台的网络（仅真实运行需要）

在项目根目录激活指定 Conda 环境，并使用国内镜像安装依赖：

```powershell
conda activate management-monitor
python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
npm.cmd ci
$env:PLAYWRIGHT_DOWNLOAD_HOST = 'https://npmmirror.com/mirrors/playwright'
npx.cmd playwright install chromium
Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST
```

项目级 `.npmrc` 和 `package-lock.json` 已固定使用 npmmirror，`node_modules` 安装在本项目
根目录，不依赖旧项目或全局 Node 包。Python 命令必须在上述 Conda 环境中执行；`--index-url`
只配置清华镜像，不回退到境外 PyPI。

复制配置模板并在本机填写，不要提交 `.env`：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

PostgreSQL 和飞书变量见 `.env.example`；门户地址、网络、分页和输出目录的非敏感默认值
位于 `config/runtime.json`。配置优先级为进程环境变量、项目根目录 `.env`、JSON 默认值；
已有进程变量不会被 `.env` 覆盖。数据库配置细节见
`database/DATABASE_USAGE.md`。

## 四个兼容命令

```powershell
npm.cmd run workflow:daily
npm.cmd run feishu:export-listener
npm.cmd run feishu:simulation
npm.cmd run check
```

| 命令 | 用途 | 是否访问真实外部系统 |
| --- | --- | --- |
| `npm.cmd run workflow:daily` | 30 天初始化采集、原子快照、PostgreSQL 入库、Excel 导出、持续监听；启用时同时启动飞书导出监听 | 是：门户、PostgreSQL；按配置访问飞书 |
| `npm.cmd run feishu:export-listener` | 单独启动飞书 SDK 长连接 Excel 导出服务；不要与同一实例的主工作流重复启动 | 是：PostgreSQL、飞书 |
| `npm.cmd run feishu:simulation` | 显式真实联调；使用随机测试表、模拟工单和模拟图片，输入“结束”或按 `Ctrl+C` 清理 | 是：隔离 PostgreSQL 测试表、飞书 |
| `npm.cmd run check` | 语法检查及 Python/Node 离线测试 | 否；测试使用临时目录、mock/fake，不登录门户、不发真实消息、不修改正式数据库 |

首次部署应先运行离线检查：

```powershell
npm.cmd run check
```

然后启动主流程：

```powershell
npm.cmd run workflow:daily
```

浏览器打开后人工完成门户登录。程序会在同一会话中完成 30 天初始化采集，验证列表、
详情、附件清单、附件文件和 SHA-256 后发布快照，入库并进入常驻监听。使用 `Ctrl+C`
平稳停止。

## 数据与失败语义

- `capture-output/daily-management/current/` 只表示最近一次经过完整性验证的 30 天快照。
- 采集先写暂存目录；只有列表分页总数、全部详情、预期附件及文件哈希均有效时才原子替换
  `current/`。中断时发布器可恢复旧快照。
- 部分抓取或失败批次保存在 `partial-attempts/` 等诊断目录，不覆盖 `current/`，不入库，
  不推进监听检查点。
- PostgreSQL 以 `safety_code` 为业务唯一键。监听窗口中的工单 upsert、飞书 outbox 入队和
  检查点推进位于同一事务；任何一步失败都回滚并从原窗口重试。
- 飞书 outbox 持久化卡片和图片发送进度，使用租约领取及有界退避。飞书网络失败不会静默
  丢弃通知；由于远端发送和本地确认无法构成同一事务，进程恰在两者之间中断时可能重复发送。
- Excel 自助导出只按 `create_time` 闭区间查询 PostgreSQL，不访问门户。不完整历史记录仍
  保留整行，缺失单元格留空，文件只私发给实际请求且仍在允许名单内的用户。

## 飞书配置

自动通知发送到 `FEISHU_RECEIVE_ID` 指定的目标，类型由
`FEISHU_RECEIVE_ID_TYPE` 指定；未设置时仍兼容旧变量 `FEISHU_CHAT_ID`。出站 HTTP 的超时、
单次调用重试次数和退避分别由 `FEISHU_APP_TIMEOUT_SECONDS`、
`FEISHU_APP_MAX_ATTEMPTS`、`FEISHU_APP_RETRY_BACKOFF_SECONDS` 和
`FEISHU_APP_RETRY_MAX_BACKOFF_SECONDS` 控制。

自助导出需要设置 `FEISHU_EXPORT_ENABLED=true` 和
`FEISHU_EXPORT_ALLOWED_OPEN_IDS`。允许用户发送“导出Excel表格”或
`FEISHU_EXPORT_COMMAND` 配置的兼容命令后，机器人私发中文日期卡片。事件由官方
`lark-oapi` SDK 长连接接收，不需要公网回调地址；飞书应用仍需启用消息接收、卡片交互、
消息发送及文件上传权限。

## 认证与敏感数据

主工作流通过仅监听 `127.0.0.1`、带随机 bearer 且限制目标 API 的浏览器请求桥复用已登录
会话。新的主流程不会生成 `auth-token.json`、`portal-storage-state.json` 或持久认证
profile，也不会把
Authorization、Cookie、响应正文或飞书标识写入正式快照和日志。`WORKORDER_TOKEN` 只作为
旧版兼容的临时调试入口；不要把真实 Token 写入 `.env`、命令行、README 或提交记录。

从旧版本升级时应检查并妥善处理旧目录可能遗留的认证文件，但不要在未备份、未确认路径时
批量删除。完整步骤见 `docs/OPERATIONS.md`。

## 代码边界

```text
safety_monitor/domain/       时间窗口、工单身份、完整性、监听与导出纯规则
safety_monitor/application/  初始化快照、监听、通知、导出用例
safety_monitor/ports/        门户、快照、仓储、检查点、outbox 等接口
safety_monitor/adapters/     PostgreSQL、文件快照与飞书 outbox 投递适配器
common/                      配置、脱敏日志、进程和飞书兼容适配
data_acquisition/            内存浏览器会话、HTTP/附件采集适配器及兼容 CLI
database/                    PostgreSQL 仓储、迁移及兼容 API
data_export/                 Excel 生成
workflows/                   四个稳定命令的组合根
```

领域和应用规则不直接依赖 HTTP、文件系统、PostgreSQL、飞书 SDK 或浏览器，因此默认测试可
完全离线运行。架构与决策见 `docs/ARCHITECTURE.md`，业务不变量与测试映射见
`docs/BEHAVIOR_MATRIX.md`，升级、监控和回滚见 `docs/OPERATIONS.md`。

## 验证范围

本次重构的默认验证是 `npm.cmd run check` 所覆盖的离线检查。真实门户登录、正式
PostgreSQL 迁移和真实飞书消息/长连接均未在默认测试中执行，不能据离线通过推断外部权限、
网络或正式数据配置正确。真实联调只能使用上述显式命令和隔离配置执行。
