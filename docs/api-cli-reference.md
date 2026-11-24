# API 与 CLI 参考

## API 基础

默认地址为 `http://127.0.0.1:1000`。FastAPI 生成的 OpenAPI 文档可从 `/openapi.json` 获取，交互式页面为 `/docs` 与 `/redoc`。CLI 通过 HTTP 调用已运行的服务；设置 `SCHEDULER_API_URL` 可更改目标地址。

当前 API 没有内建身份认证。能访问写 API 的主体等同于能以服务身份注册操作系统命令；访问限制由本机边界或受保护的代理负责。

## 任务定义

创建和更新使用同一任务定义字段：

```json
{
  "id": "weekday-report",
  "name": "工作日报",
  "command": ["D:\\runtime\\python\\python.exe", "D:\\jobs\\report.py", "--format", "csv"],
  "depends_on": ["root"],
  "schedule_type": "cron",
  "cron": "0 18 * * 1-5",
  "timezone": "Asia/Shanghai",
  "working_directory": "D:\\jobs",
  "timeout_seconds": 1800
}
```

`command`、`schedule_type`、计划字段、时区、工作目录均可按需省略；未配置计划的根节点为手动触发，未配置命令的节点为逻辑节点。`depends_on` 默认为空数组，`timeout_seconds` 默认 300 秒。所有字段限制以 `JobInput` 和 `/openapi.json` 为准。

Cron 表达式使用标准五段 crontab 规则：分、时、日、月、周。数字星期中 `0` 和 `7` 都表示周日，`1` 至 `6` 表示周一至周六；例如 `1-5` 表示周一至周五。星期名称（如 `mon-fri`）也可使用。

## 端点

| 方法与路径 | 用途 | 成功结果 |
| --- | --- | --- |
| `GET /api/health` | 服务、数据库和 job store 健康状态 | `status`、启动时间、运行时长、计划项数量 |
| `GET /api/jobs` | 列出任务定义和计算后的计划信息 | 任务数组 |
| `POST /api/jobs` | 注册任务 | `201` 和任务视图 |
| `PUT /api/jobs/{job_id}` | 修改完整任务定义；路径 ID 必须等于 body ID | `200` 和新版本任务视图 |
| `PATCH /api/jobs/{job_id}/dependencies` | 修改直接父任务列表 | `200` 和任务视图 |
| `DELETE /api/jobs/{job_id}` | 移除无子节点任务 | `204`；有下游时 `409` |
| `POST /api/jobs/{job_id}/run?scope=self\|downstream` | 排入手动运行 | `202` 和 `queued` 提交回执 |
| `GET /api/runs?limit=50&offset=0&job_id=...` | 分页读取运行摘要，不含输出 | 运行数组 |
| `GET /api/runs/recent` | 读取五天状态圆点所需摘要 | 按 job ID 分组的运行记录 |
| `POST /api/runs/catch-up-today` | 排入今天漏掉的 Cron 运行 | `status`、按依赖顺序排列的 `jobs` |
| `GET /api/runs/search?job_id=...&date=YYYY-MM-DD&status=...` | 按任务、服务时区日期和状态筛选 | `items`、`total`、`retention_days` |
| `GET /api/runs/{run_id}` | 读取运行详情及当前输出 | 单条完整运行记录 |
| `POST /api/runs/{run_id}/stop` | 请求停止一个活动运行及其进程树 | `202` 和 `stopping`；完成后状态为 `cancelled` |

`scope` 只能是 `self` 或 `downstream`，默认是 `downstream`。搜索 `limit` 默认 50、最大 200；`offset` 必须大于等于 0。日期按服务默认时区解释，格式为 `YYYY-MM-DD`。错误输入通常返回 `422`，任务/运行不存在返回 `404`，依赖冲突、禁止删除或运行已结束返回 `409`。

Catch-up 端点只排入服务本地日期内计划时间已过、且当天没有运行记录的 Cron 任务；按依赖层级执行，并沿用正常调度的当天上游成功检查。它不重试当天已有失败/跳过记录的任务，也不提前运行今天尚未到时间的任务。已排队任务会以调度服务身份运行，结果出现在普通运行历史中。

运行详情在执行期间会返回 `status: "running"` 及已落库的部分 `output`。列表接口不返回输出，客户端应只在用户打开单次详情时调用详情接口。停止接口只接受仍由当前调度器进程管理的运行；它不会回滚外部副作用，也不保证终止逃逸出进程树的后台进程。运行输出限制和保留周期见 [`data-recovery.md`](data-recovery.md)。

## CLI

CLI 命令名为 `scheduler`；也可从源码环境用 `python -m scheduler.cli`。注册语法以 `scheduler add --help` 为准：

```text
scheduler add --id ID --name NAME [--every SECONDS | --cron CRON | --at ISO_TIME]
              [--timezone ZONE] [--cwd DIRECTORY] [--timeout SECONDS]
              [--depends-on JOB_ID ...] -- EXECUTABLE [ARG ...]
scheduler list
scheduler run ID [--scope self|downstream]
scheduler remove ID
```

CLI 没有修改任务子命令；使用 Web 控制台或 `PUT /api/jobs/{id}`。CLI 请求服务失败或收到 API 错误时以非零状态结束并输出错误。命令参数由 `argparse.REMAINDER` 接收，建议始终显式写 `--`。

从 PowerShell 直接调用创建/更新 API 且包含中文名称时，应将 JSON 明确编码为 UTF-8 字节再提交。部分 PowerShell 版本对字符串 `-Body` 使用系统默认编码，可能在发送前把中文替换成 `?`；服务收到后无法还原原文。Web 表单和 `scheduler` CLI 会按 UTF-8 发送。示例：

```powershell
$payload = @{ id = 'example-job'; name = '示例任务'; command = $null } | ConvertTo-Json -Depth 10
$utf8Body = [System.Text.Encoding]::UTF8.GetBytes($payload)
Invoke-RestMethod -Uri 'http://127.0.0.1:1000/api/jobs' -Method Post `
  -ContentType 'application/json; charset=utf-8' -Body $utf8Body
```

## 示例

```powershell
$env:SCHEDULER_API_URL = 'http://127.0.0.1:1000'
scheduler list
scheduler run weekday-report --scope self
scheduler run root --scope downstream
```

`scheduler serve` 用于前台开发启动服务；不要在已安装的 Windows 服务之外再对同一数据库和端口启动第二个生产调度实例。
