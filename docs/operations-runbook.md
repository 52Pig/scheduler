# 运维运行手册

## 适用范围

本文用于 Windows Service 部署。具体安装/卸载命令、WinSW 校验值和 `\SchedulerService\SchedulerHealthMonitor` 配置见 [`../deploy/windows/SERVICE_OPERATIONS.md`](../deploy/windows/SERVICE_OPERATIONS.md)。其他计划任务不属于调度器，应保持不变。

当前服务监听 `127.0.0.1:1000`，由 LocalService 运行；WinSW 配置意外退出重启，SYSTEM 身份的健康监控任务每分钟检查，并在连续三次健康失败后尝试重启服务。

## 日常检查

```powershell
Get-Service SchedulerService
Invoke-RestMethod http://127.0.0.1:1000/api/health | Format-List
Get-ScheduledTask -TaskPath '\SchedulerService\' -TaskName SchedulerHealthMonitor
```

健康接口应返回 `status = ok`、`scheduler_running = true`、`database = ok` 和 `jobstore = ok`。`scheduled_jobs` 包含系统清理计划，不等于用户任务数。查看用户任务数量请访问 `/api/jobs`。

## 安全重启

重启会中断正在运行的外部命令。执行前查看任务列表/运行日志是否有 `running` 项，确认任务可恢复或业务方接受中断，再由管理员运行：

```powershell
Get-Service SchedulerService
Invoke-RestMethod 'http://127.0.0.1:1000/api/runs?limit=50' |
  Where-Object status -eq 'running' |
  Select-Object id, job_id, started_at
Restart-Service SchedulerService
(Get-Service SchedulerService).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Invoke-RestMethod http://127.0.0.1:1000/api/health
```

重启后检查服务启动时间是否更新、健康接口是否恢复、运行中记录是否变为失败并带有“执行状态未知”说明。核对任务输出/业务数据后再决定是否手动重跑；不要假设退出服务就代表外部操作没有发生。

## 停止单次运行

在任务行点击对应的“停止”，或在运行日志详情中停止当前 run。任务同时有多个活动 run 时，每个 run 有独立停止按钮；停止一个不会自动停止同一工作流中的其他节点。API 调用为 `POST /api/runs/{run_id}/stop`。运行变为 `cancelled` 后检查任务输出和外部业务系统，确认部分副作用后再决定是否重跑。

Windows 执行器调用 `taskkill /T /F` 尝试终止进程树。任务若创建脱离进程树的后台服务/守护进程，不保证被停止；这类任务必须自行提供清理机制。停止请求也不能撤销已提交的数据或外部请求。

## 排障顺序

1. 服务未运行：检查 Windows 服务状态和 WinSW 日志；确认服务启动配置和调度器自己的 Python 环境可访问。
2. 服务运行但端口无响应：检查 `127.0.0.1:1000` 监听者、服务日志、数据库路径和文件权限；不要直接删除 SQLite 文件。
3. 健康接口 `degraded`：检查接口返回的 `database`/`jobstore` 字段、SQLite 文件和磁盘空间。
4. 任务启动失败：检查任务指定的 executable、脚本、working directory、参数、服务账户 ACL、网络资源和任务自身日志。不能通过给调度器安装任务依赖来“修复”单个任务。
5. 任务无输出：打开 `/logs/{run_id}` 查看状态；检查程序是否缓冲 stdout/stderr。Python 默认无缓冲，其他工具需由任务自身刷新输出。
6. 依赖节点被跳过：从同一 workflow run ID 查询父节点，确认父任务成功、节点在所选运行范围内，且定时节点的所有祖先在其各自本地日期成功。

服务和健康监控日志位于 `%ProgramData%\SchedulerService\logs`。任务输出在 `/logs` 查询，默认保留五天且每条最多 20 KB。

## 部署与更新

升级调度器前先备份服务数据库，检查发布变更和数据库兼容性，安排维护窗口，并通知正在运行任务的所有者。使用管理员 PowerShell 运行安装脚本；它管理调度器自身环境，不负责安装或授权某个任务运行时。任务脚本、解释器及数据目录 ACL 由任务部署者管理。

安装后验证服务自动启动、健康接口、网页首页、API 任务列表、健康监控任务及日志路径。恢复机制可在受控维护窗口按 `SERVICE_OPERATIONS.md` 运行 `Test-SchedulerRecovery.ps1`；该测试会主动停止服务，不应在有业务任务运行时执行。

## 事件记录

每次故障记录发生时间、服务状态、健康响应、相关 job ID/run ID/workflow run ID、服务日志文件、采取的动作和验证结果。记录中不得包含密钥或完整个人数据。若需长期审计，当前五天本机运行历史不足以满足要求，应先建立集中日志/审计方案。
