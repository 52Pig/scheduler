# 文档索引

本文档按使用者角色组织。所有文档都描述本仓库调度框架；“当前实现”和“目标/建议”会明确区分。

| 读者 | 从这里开始 | 解决的问题 |
| --- | --- | --- |
| 新接手的 Agent/开发者 | 根目录 [`AGENTS.md`](../AGENTS.md)、[`architecture.md`](architecture.md) | 代码地图、边界、约束、测试与实现路径 |
| 任务所有者 | [`task-authoring.md`](task-authoring.md)、[`dependency-workflows.md`](dependency-workflows.md) | 如何注册命令、解释器、计划和上下游依赖 |
| API/CLI 集成者 | [`api-cli-reference.md`](api-cli-reference.md) | 请求结构、端点、命令行和错误行为 |
| Windows 运维者 | [`operations-runbook.md`](operations-runbook.md)、[`../deploy/windows/SERVICE_OPERATIONS.md`](../deploy/windows/SERVICE_OPERATIONS.md) | 部署、健康检查、重启和恢复服务 |
| 安全/数据管理员 | [`security-model.md`](security-model.md)、[`data-recovery.md`](data-recovery.md) | 执行权限、访问边界、备份与恢复 |
| 贡献者/评审者 | [`../CONTRIBUTING.md`](../CONTRIBUTING.md)、[`testing-release.md`](testing-release.md)、[`decisions/`](decisions/) | 质量门禁、发布和架构决策依据 |

## 关键架构决策

- [ADR 0001：单机单实例调度](decisions/0001-single-host-scheduler.md)：当前部署拓扑及转向多实例的前置条件。
- [ADR 0002：任务自带运行时](decisions/0002-task-owned-runtime.md)：命令数组、解释器归属和不安装任务依赖的边界。
- [ADR 0003：用户定义 DAG](decisions/0003-user-defined-dag.md)：根节点、依赖和运行范围语义。
- [ADR 0004：SQLite 与短期运行历史](decisions/0004-local-sqlite-run-history.md)：当前状态存储、输出上限和留存限制。
- [ADR 0005：按运行 ID 停止任务](decisions/0005-run-scoped-cancellation.md)：单次取消范围、进程树终止和副作用边界。

## 文档规则

- API 的机器可读结构以 FastAPI `/openapi.json` 为准；本参考说明其稳定语义和 CLI 行为。
- 任务执行边界以 `scheduler/models.py` 和 `scheduler/service.py` 为准；文档不能把将来计划当成现有功能。
- Windows 安装细节以部署脚本和服务操作说明为准；改动路径、账户、数据库或计划任务时必须同步更新。
- 关键设计变化新增 ADR。每个 ADR 记录状态、上下文、决定、替代方案和后果。
- 命令示例应在受控测试环境验证。真实任务命令、数据目录和凭据不得作为测试样例。

## 文档维护责任

代码改动者维护与变更行为直接相关的文档；评审者检查文档和实现是否一致。若文档与实现冲突，先以实际代码确定当前行为，再修正文档或提出代码修复，不默默依赖旧文字。
