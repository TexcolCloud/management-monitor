# 管理监控

**从工单采集到消息通知，让重复的数据整理工作自动完成。**

管理监控（Management Monitor）是一个基于 Python 和 Node.js 的工单自动化工具。它复用浏览器登录会话采集工单及附件，在完整性校验后写入 PostgreSQL，持续检查新增工单，并通过飞书发送通知和提供 Excel 自助导出。

[快速开始](#快速开始) · [使用方式](#使用方式) · [工作原理](#工作原理) · [文档](#文档) · [开发](#开发)

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 工单与附件采集 | 初始化获取近 30 个自然日的数据，支持分页及附件下载 |
| 完整快照 | 检查列表、详情、附件和文件哈希，验证后发布完整批次 |
| 持续监控 | 幂等入库，以检查点保存进度，失败后从原窗口重试 |
| 飞书通知 | 发送卡片、图片和文件，保存投递进度并重试失败通知 |
| 自助导出 | 允许名单内用户按日期查询，私信接收 Excel 文件 |

> 当前为脱敏版本：不包含原业务系统地址、导航记录或数据。门户地址为不可访问的示例域名；离线测试可以直接运行，真实接入需要自行配置并调整接口适配器。

## 快速开始

需要 **Python 3.10+** 和 **Node.js 20+**。以下使用 Windows PowerShell：

```powershell
git clone https://github.com/TexcolCloud/management-monitor.git
cd management-monitor

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci
npx playwright install chromium --only-shell

npm run check
```

这会完成语法检查和 Python / Node 离线测试，不连接门户、正式数据库或飞书。依赖安装需要网络；当前 npm 镜像设置见 `.npmrc`。

## 使用方式

真实运行需要一个获准访问的门户、PostgreSQL 和飞书应用。先创建本机配置并安装浏览器：

```powershell
Copy-Item .env.example .env
Copy-Item config/runtime.json config/runtime.local.json
npx playwright install chromium
```

在 `.env` 设置 `WORKORDER_CONFIG=config/runtime.local.json`，填写数据库与飞书凭据；在本地 JSON 中填写门户及 API 地址。核对适配器的请求和响应字段后，使用手动导航启动：

```powershell
npm run workflow:daily -- --no-login-replay
```

在打开的浏览器中登录并进入目标页面，程序随后完成初始化采集和持续监听。用 `Ctrl+C` 停止。导航录制、自助导出及其他命令见 [配置与接入](docs/CONFIGURATION.md)。

`feishu:simulation` 是会访问测试数据库、实际发送飞书消息的联调命令，不是离线演示。

## 工作原理

```text
浏览器登录会话
      │
      ▼
工单列表 → 详情与附件 → 完整性校验 → current 快照
                                      │
                                      ▼
                       PostgreSQL 工单 / 检查点 / Outbox
                                      │
                                      ▼
                               飞书通知与 Excel
```

- **完整后发布：** 采集先写暂存目录；失败批次保留用于排查，不覆盖上一份完整快照。
- **事务内记录：** 新工单入库、通知入队和检查点推进在同一事务内完成。
- **可恢复投递：** Outbox 使用租约和退避重试。远端发送与本地确认之间中断可能产生重复通知，不承诺 exactly-once。
- **按授权导出：** 从数据库按创建时间查询，文件仅发给提出请求且仍在允许名单内的用户。

## 文档

| 文档 | 内容 |
| --- | --- |
| [配置与接入](docs/CONFIGURATION.md) | 本地配置、门户适配、飞书和运行命令 |
| [架构说明](docs/ARCHITECTURE.md) | 领域、应用、接口及适配器职责 |
| [行为与测试映射](docs/BEHAVIOR_MATRIX.md) | 数据完整性与恢复行为 |
| [数据库说明](database/DATABASE_USAGE.md) | 表、配置和迁移 |
| [运行维护](docs/OPERATIONS.md) | 故障检查与运行数据管理 |
| [采集模块](data_acquisition/README.md) | 浏览器会话、导航及附件 |
| [导出模块](data_export/README.md) | Excel 生成 |

本版配置前缀为 `WORKORDER_`，数据库标识已改为通用名称；它不是原业务部署的直接升级版本。请为演示使用独立数据库。

## 开发

```powershell
npm run check
# 单独运行 Python 测试
python -m unittest discover -s tests -v
```

默认测试覆盖快照完整性、附件处理、入库、监听、通知、导出权限和日志脱敏，使用临时目录与替身服务。真实接入的权限和网络需要另行验证。

欢迎通过 Issue 描述问题，或提交包含复现测试的修改。请使用合成工单，勿附带真实凭据、业务地址、附件或用户信息。`.env`、`*.local.json`、运行数据和认证状态已加入忽略规则。
