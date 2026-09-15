# README 工程图

[工作流程](workflow.svg)与[组件架构](architecture.svg)采用可直接编辑的 SVG；README 引用相同图源，不维护重复 Mermaid 或位图实现。

流程图按编号阶段阅读，矩形表示处理或产物，菱形标记判断，圆形连接符接续阶段。架构图表示当前组件与依赖，不是未来规划；连线仅使用水平、垂直线段。调用方向、分支条件及持久化边界以文字明确标注。

## 实现依据

- [workflows/daily_workflow.py](../../workflows/daily_workflow.py)
- [workflows/work_order_monitor.py](../../workflows/work_order_monitor.py)
- [safety_monitor/application/acquire_snapshot.py](../../safety_monitor/application/acquire_snapshot.py)
- [safety_monitor/application/monitoring.py](../../safety_monitor/application/monitoring.py)
- [data_acquisition/browser_session.py](../../data_acquisition/browser_session.py)
- [config/runtime.json](../../config/runtime.json)

修改时先核对上述实现，再更新图源。用浏览器检查全图与 README 栏宽预览，确认没有文字溢出、连线遮挡或失败分支误接成功结果。制图表达参考 [ASQ 流程图约定](https://asq.org/quality-resources/flowchart)与 [C4 图示规范](https://c4model.com/diagrams/notation)。
