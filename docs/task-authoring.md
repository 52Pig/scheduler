# 任务编写与注册指南

## 任务运行契约

任务是调度器启动的操作系统进程。任务所有者负责准备脚本、解释器/运行时、库、凭据、输入数据、输出目录和可安全重跑的业务逻辑。调度器不会安装或更新这些内容。

注册命令由可执行文件及参数组成；每个参数单独传递。不要把整条命令包成一个字符串，也不要依赖未配置的交互式 shell、用户 profile 或桌面登录状态。任务运行时必须能被 Windows 服务账户访问。

每个任务可配置：

- 稳定且唯一的任务 ID、展示名称。
- 可选命令数组。省略命令可创建纯逻辑依赖节点。
- 工作目录、超时时间。
- 可选间隔、五段 Cron 或一次性时间计划及 IANA 时区。
- 一个或多个直接父任务 ID。

当前没有每任务 PATH/environment 配置、秘密信息字段、容器镜像或资源配额。命令继承服务进程环境；脚本不可依赖登录用户专属环境。

## 推荐约定

- 任务成功以退出码 `0` 表示；非零退出码记为失败。处理可预期错误时写到 stderr 并返回非零码。
- 将业务日期、输入范围或回刷日期作为明确参数，避免依赖人工临时改脚本。
- 为数据写入设计幂等键、临时文件后原子替换或可重复覆盖策略。调度器不承诺 exactly-once，也不会自动重试。
- 输出关键阶段和业务日期，避免输出密钥、访问令牌、个人数据或完整环境变量。
- 对长任务按阶段 flush 日志。Python 默认以 `PYTHONUNBUFFERED=1` 启动；其他语言/工具需自行启用行缓冲或显式刷新。
- 给任务设合理超时。任务列表和日志详情可停止单次活动运行；停止会终止框架启动的进程及可识别的子进程，但不能撤销已经写入外部系统的数据。
- 服务重启可能中断外部命令；重启后运行记录会标成失败且结果未知。停止和重启都不能替代任务的幂等设计。

## Windows PowerShell 注册示例

Python 脚本：

```powershell
scheduler add `
  --id market-minute-close `
  --name "收盘后分钟线同步" `
  --depends-on root `
  --cron "30 16 * * 1-5" `
  --timezone Asia/Shanghai `
  --cwd "D:\jobs\market" `
  --timeout 3600 `
  -- "D:\runtime\python\python.exe" "D:\jobs\market\sync.py" --latest
```

PowerShell 脚本：

```powershell
scheduler add --id inventory-refresh --name "库存刷新" --cron "0 6 * * 1-5" --timezone Asia/Shanghai --cwd "D:\jobs\inventory" --timeout 900 -- "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -File "D:\jobs\inventory\refresh.ps1"
```

Bash 脚本（需自行安装并维护 Bash）：

```powershell
scheduler add --id report-build --name "报告生成" --depends-on root -- "C:\Program Files\Git\bin\bash.exe" "D:/jobs/report/build.sh" --date latest
```

不带命令的计划根节点：

```powershell
scheduler add --id root --name "每日工作流入口" --cron "30 0 * * 1-5" --timezone Asia/Shanghai --
```

命令和参数中的空格需要由当前终端正确引用。通过 `--` 分隔调度器选项和任务参数；分隔符后的参数逐项传递，不支持直接传入管道、重定向或 shell 组合表达式。

## 依赖和计划

任务 ID 可由业务团队定义，不能依赖框架内置的 extract/transform/report 类型。使用 Web 画布可调整节点位置并增加/删除依赖；也可在 CLI/API 中通过直接父任务 ID 配置。

- 手动运行 scope `self` 只运行选中节点，不自动运行上游。
- 手动运行 scope `downstream` 运行该节点及未单独定时的下游节点。
- 失败、超时或手动停止分支的后代会跳过，独立分支继续执行。
- 一个依赖图当前必须归属一个根节点；跨根汇合不受支持。
- 另有独立计划的下游任务不会被根节点提前执行；它在触发时检查祖先当天是否成功。

详细状态和边界见 [`dependency-workflows.md`](dependency-workflows.md)。

## 修改、日志和回刷

Web 页面支持修改任务。CLI 当前没有 `update` 子命令；可用 Web 或 `PUT /api/jobs/{id}` 更新。修改会提升定义版本，不覆盖既有运行记录。

任务日志从日志页打开单次运行详情。运行中记录会持续刷新；输出合并 stdout/stderr，最多保留末尾 20 KB，运行历史保留五天。回刷前先确认任务可幂等重跑并设置目标日期，再用 `scope=self` 仅重跑该节点，避免无意执行下游。

## 本地验证

首次注册前，用同一服务身份验证解释器路径、工作目录、输入文件、输出目录和网络访问。不要用交互式 PyCharm/PowerShell 会话能成功作为 Windows 服务身份可访问的证明。可先用短时、无副作用的验证脚本确认退出码和日志，再注册正式任务。
