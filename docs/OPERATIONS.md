# 运行维护

本页适用于管理监控的脱敏版本。请使用独立的测试或新建数据库；本版重新命名了环境变量和迁移元数据表，不作为原业务部署的直接升级包。

## 启动前检查

1. 完成 [配置与接入](CONFIGURATION.md)，确认所有示例地址已替换为获准访问的地址。
2. 使用项目虚拟环境安装依赖，并运行 `npm run check`。
3. 检查数据库目标表、飞书测试接收目标和导出允许名单。凭据只保存在本机。
4. 首次使用手动导航启动：`npm run workflow:daily -- --no-login-replay`。
5. 通过日志确认完整快照已发布、入库完成后再检查监听状态。

不要同时启动同一实例的主流程与独立导出监听器。用 `Ctrl+C` 正常停止；不要按进程名称批量结束 Python 或 Node 进程。

## 运行数据

| 路径 | 用途 |
| --- | --- |
| `capture-output/daily-management/current/` | 最近一次通过完整性校验的快照 |
| `capture-output/daily-management/partial-attempts/` | 失败或不完整批次 |
| `excel-output/` | 显式导出产物 |
| `.temp/` | 请求回执和临时状态 |
| 数据库检查点、Outbox | 监听进度和待发送通知 |

这些内容可能包含业务数据。不要作为源码、Issue 附件或 Release 资产上传。

## 常见问题

**登录后未开始采集**

检查列表 API 配置、浏览器是否进入目标页面，以及请求字段是否与适配器匹配。使用本机重新录制的导航，或关闭自动回放。不要把包含 Cookie、Token 的原始请求发到公开讨论区。

**快照没有更新**

检查列表总数、缺失详情和附件校验。程序不会用不完整批次覆盖 `current/`；修正采集问题后重新运行，不要直接将失败批次改名为正式快照。

**工单已入库但没有收到通知**

检查接收目标、应用权限、Outbox 状态、下次重试时间及错误分类。通知使用重试机制；远端成功而本地尚未确认时中断，可能重复发送。

**导出没有响应**

确认只运行一个导出监听器，导出开关已启用，请求用户在允许名单内，应用具备事件及文件发送权限。

## 备份与恢复

停止当前实例后备份数据库、本机配置和必要快照。凭据与业务备份应限制访问，不放入 Git 仓库。

```powershell
pg_dump --format=custom --password --host "<host>" --port "<port>" --username "<user>" --file "<private-backup-directory>/management-monitor.dump" "<database>"
pg_restore --list "<private-backup-directory>/management-monitor.dump"
```

列出归档目录不等于恢复成功。在隔离数据库实际恢复并核对数据后，再按自己的部署流程切换。源码回退不能替代数据库和运行数据恢复。

## 提交前检查

`.env`、`*.local.json`、导航录制、认证状态和运行输出不应被跟踪。检查 `git status` 与暂存差异；忽略规则不会清除已提交内容或旧版本历史。
