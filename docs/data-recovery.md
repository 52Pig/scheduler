# 数据备份与恢复

## 数据范围

服务状态数据库位于部署配置中的独立状态目录，包括任务定义、定义版本、运行记录、短期输出和 APScheduler 持久化触发器。实际路径以 `SCHEDULER_DATABASE_URL` 和服务 XML 为准。

运行输出保留末尾 20 KB。终态运行记录最多保留五天；`running` 记录不会被定期清理。五天保留是应用历史策略，不是数据库备份或灾难恢复方案。

## 备份

升级前和定期维护时创建带时间戳的新备份文件；目标文件必须不存在。迁移脚本使用 SQLite online backup API，并检查完整性和必要表：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stateRoot = Join-Path $env:ProgramData 'SchedulerService'
$backup = Join-Path $stateRoot "backups\scheduler-$stamp.db"
New-Item -ItemType Directory -Path (Split-Path $backup) -Force | Out-Null
& '.\.venv\Scripts\python.exe' `
  '.\deploy\windows\migrate_database.py' `
  (Join-Path $stateRoot 'scheduler.db') $backup
if ($LASTEXITCODE -ne 0) { throw 'Scheduler database backup failed.' }
```

这是手工备份流程，项目当前没有自动备份计划、异机复制、加密或保留清理策略。运维方应将备份复制到受控的独立存储，限制读取权限，确定频率/保留期/RPO/RTO，并定期检查可恢复性。不要把唯一备份放在源数据库所在磁盘。

## 恢复

恢复会替换任务定义、计划触发器状态和运行历史；必须由管理员执行并安排维护窗口。

1. 停止 `SchedulerService`，确认没有服务进程继续写数据库。
2. 将当前数据库另存为事故现场副本，不覆盖历史备份。
3. 对候选备份执行 `PRAGMA integrity_check`，确认结果为 `ok`，并确认 `job_definitions` 和 `runs` 表存在。
4. 将已验证备份复制为 `SCHEDULER_DATABASE_URL` 指定的主库，保留正确 ACL。
5. 启动服务并检查 `/api/health`、任务定义、触发器计划和近期运行详情。
6. 如备份内含 `running` 记录，服务启动恢复会把它们标记为失败且执行结果未知。检查业务系统副作用，再决定是否重跑。
7. 记录恢复时间、备份来源、完整性结果、恢复点和验证结果。

检查候选文件的完整性和必要表：

```powershell
$candidate = Join-Path $env:ProgramData 'SchedulerService\backups\scheduler-YYYYMMDD-HHMMSS.db'
$verification = @'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print("integrity_check:", result)
    print("scheduler tables present:", {"job_definitions", "runs"} <= tables)
    if result != "ok" or not {"job_definitions", "runs"} <= tables:
        raise SystemExit(1)
'@
$verification | & '.\.venv\Scripts\python.exe' - $candidate
if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed; do not restore this file.' }
```

恢复示例（路径必须先核实，以下命令会覆盖当前主库）：

```powershell
Stop-Service SchedulerService
(Get-Service SchedulerService).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
$stateRoot = Join-Path $env:ProgramData 'SchedulerService'
Copy-Item -LiteralPath (Join-Path $stateRoot 'backups\scheduler-YYYYMMDD-HHMMSS.db') `
  -Destination (Join-Path $stateRoot 'scheduler.db')
Start-Service SchedulerService
Invoke-RestMethod http://127.0.0.1:1000/api/health
```

只能恢复到已经验证的副本；不要在运行中的 SQLite 数据库上使用普通 `Copy-Item` 作为备份方法，也不要把项目目录中的旧副本当成最新状态。

## 升级和迁移

`RunStore` 启动时执行少量兼容性 `ALTER TABLE` 初始化；目前没有完整的版本化迁移框架。升级前必须备份并检查差异。任何新增/重命名/删除持久化字段都需定义旧库升级行为、失败恢复方式和兼容测试；不要以删除数据库作为迁移手段。
