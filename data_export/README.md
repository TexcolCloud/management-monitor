# Excel 查询与生成

`data_export` 负责把工单行转换为带表头、样式、筛选和冻结窗格的 `.xlsx`。纯格式转换不访问
门户、PostgreSQL 或飞书；数据来源由外层工作流决定。

## 两条调用路径

1. `npm.cmd run workflow:daily` 在完整 30 天快照成功入库后，从该批次生成本地 Excel，默认
   路径为 `excel-output/current/daily-management-table-no-operation.xlsx`。
2. 飞书自助导出由 `npm.cmd run workflow:daily` 内置监听器或
   `npm.cmd run feishu:export-listener` 接收事件，从 PostgreSQL 按 `create_time` 闭区间查询，
   生成临时 Excel 并私发给实际请求用户。此路径不读取快照，也不访问门户。

不要同时为同一个飞书应用运行主工作流内置监听器和独立监听器。

## 飞书自助导出语义

- 只接受 `FEISHU_EXPORT_ALLOWED_OPEN_IDS` 中的实际发送者/卡片操作者。
- 接受固定中文命令“导出Excel表格”和 `FEISHU_EXPORT_COMMAND` 配置的兼容命令。
- 机器人向请求用户私发中文起止日期卡片，不向原群聊发送导出文件。
- 日期值支持 `YYYY-MM-DD +0800`、`YYYY-MM-DD+0800`、`+08:00` 等飞书格式。
- 查询范围为开始日 `00:00:00` 到结束日 `23:59:59.999999`，两端包含。
- 查询使用 PostgreSQL 的 `create_time`；数据库中已存在但 `raw_data` 为空或字段不完整的记录
  仍生成一行，缺失单元格为空。
- 临时 Excel 在发送结束后清理。查询或发送失败时向请求用户返回中文提示，不改变业务表、
  outbox 或监听检查点。

事件由官方 `lark-oapi` SDK 的长连接接收，不要求公网 IP、入站端口或 HTTPS 回调地址。真实
飞书应用仍需配置事件、权限和用户可用范围；这些外部设置不在离线测试中验证。

## 表格结构

当前 9 列为：

```text
单据编号, 单位名称, 归属单位, 类型, 归类, 风险等级, 主题, 创建人, 创建时间
```

- 单位名称按现有业务规则标准化，不修改源数据。
- 归属单位、归类和风险等级由 `config/classification_rules.json` 计算。
- 第一行加粗并设底色，冻结第一行，开启自动筛选，使用固定列宽和边框。
- 文本移除无效 XML 控制字符并按 Excel 的 32767 字符上限截断。
- 公式前缀内容保留文本值，但单元格类型强制为字符串，避免打开文件时执行公式。

离线测试会生成并重新打开临时 `.xlsx`，验证行列、工作表名、缺失字段和公式安全；不会发送
真实飞书文件或查询正式 PostgreSQL。
