# 配置与接入

## 接入配置

```powershell
Copy-Item .env.example .env
Copy-Item config/runtime.json config/runtime.local.json
Copy-Item data_acquisition/recordings/portal-navigation.example.json data_acquisition/recordings/portal-navigation.local.json
npx playwright install chromium
```

在 `.env` 设置 `WORKORDER_CONFIG=config/runtime.local.json`，在本地 JSON 中填写门户及 API 地址。配置优先级：进程环境变量 > `.env` > JSON 默认值。

| 配置 | 用途 |
| --- | --- |
| `WORKORDER_CONFIG` | 运行配置文件 |
| `WORKORDER_LIST_API_URL` / `WORKORDER_DETAIL_API_URL` | 列表、详情地址 |
| `WORKORDER_ATTACHMENT_URL` / `WORKORDER_PORTAL_URL` | 附件、登录地址 |
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` | 数据库连接 |
| `PGSCHEMA` / `PGTABLE` | 模式及目标表，默认表为 `work_orders` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭据 |
| `FEISHU_RECEIVE_ID_TYPE` / `FEISHU_RECEIVE_ID` | 通知目标 |
| `FEISHU_EXPORT_ENABLED` / `FEISHU_EXPORT_ALLOWED_OPEN_IDS` | 导出开关及允许名单 |

完整变量见 [配置模板](../.env.example)，数据库细节见 [数据库说明](../database/DATABASE_USAGE.md)。`config/classification_rules.json` 中的单位前缀、分类和关键词均为可修改的示例规则。

### 门户适配

由使用者在浏览器中完成登录，程序通过带临时凭据的本地请求桥复用会话。请先核对 `data_acquisition/` 中的请求参数与响应字段是否适用于目标系统。

可用 `npm run record:portal-navigation` 录制导航，结果写入被忽略的 `portal-navigation.local.json`；也可通过主流程的 `--no-login-replay` 手动导航。不提交录制结果、认证状态、真实请求和工单附件。

### 飞书接入

通知使用应用机器人。自助导出通过官方 `lark-oapi` SDK 长连接接收消息和卡片事件，不需要公网回调地址，但应用仍需配置相关权限。

导出默认关闭。开启后，允许用户发送“导出Excel表格”或 `FEISHU_EXPORT_COMMAND` 指定的命令，选择日期后私信接收文件。接收目标与用户标识只放在本机配置。

## 运行命令

完成适配和配置后再执行真实业务命令：

| 命令 | 用途 | 外部访问 |
| --- | --- | --- |
| `npm run check` | 语法检查和离线测试 | 无 |
| `npm run workflow:daily -- --no-login-replay` | 手动导航，30 日初始化采集及监听 | 门户、数据库，按配置访问飞书 |
| `npm run workflow:daily` | 使用本地导航录制启动 | 同上 |
| `npm run feishu:export-listener` | 独立 Excel 导出监听 | 数据库、飞书 |
| `npm run feishu:latest-notification` | 从最新完整快照发送一次通知 | 飞书，会实际发送消息 |
| `npm run feishu:simulation` | 合成工单真实联调 | 测试数据库表、飞书，会实际发送消息 |

不要同时启动主流程内的导出监听和独立导出监听。初始化默认不生成本地 Excel；用 `Ctrl+C` 停止。真实联调使用独立测试数据库和测试接收目标。
