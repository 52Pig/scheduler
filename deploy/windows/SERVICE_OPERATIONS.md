# Windows Service Operations

The scheduler is installed as the delayed automatic `SchedulerService` Windows service. It listens only on `127.0.0.1:1000`, runs as the low-privilege LocalService account, uses the isolated project environment at `.venv`, and stores its live SQLite state under `%ProgramData%\SchedulerService`. The existing project database is copied with SQLite's online backup API; it is not removed or modified.

The `\SchedulerService\SchedulerHealthMonitor` task runs as SYSTEM every minute, including after reboot, and restarts the service after three consecutive failed health checks. WinSW also restarts the process after an unexpected exit. Interrupted run records are marked failed at startup because an external command may have produced partial output; inspect that task's outputs before rerunning it.

## Install or update

Run elevated PowerShell:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\deploy\windows\Install-SchedulerService.ps1
```

The installer requires the verified WinSW 2.12.0 binary at `%TEMP%\winsw-x64-v2.12.0.exe`. SHA-256: `05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA`.

To verify automatic recovery in a controlled test, run this in elevated PowerShell. It briefly stops the service and confirms the SYSTEM health task restarts it after three failed checks:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\deploy\windows\Test-SchedulerRecovery.ps1
```

## Operate

```powershell
Get-Service SchedulerService
Restart-Service SchedulerService
Invoke-RestMethod http://127.0.0.1:1000/api/health
Get-ScheduledTask -TaskPath '\SchedulerService\' -TaskName SchedulerHealthMonitor
```

To restart and recover overdue Cron jobs, double-click `deploy/windows/Restart-SchedulerService.cmd`. It requests UAC elevation, checks for active run records, starts the service when port 1000 is free, and checks health. If another process owns the port while the service is stopped, it reports the PID and exits without terminating that process. Once healthy, it queues today's overdue Cron jobs with no run record through the scheduler API; the scheduler runs them under its service identity in dependency order. It does not rerun failed/skipped jobs or start future jobs. The recovery log is `%ProgramData%\SchedulerService\logs\service-recovery.log`.

Inspect an unexpected listener and the service configuration before taking further action. The recovery script does not terminate processes outside Windows Service Control Manager.

Service wrapper logs and monitor logs are kept under `%ProgramData%\SchedulerService\logs` and `%ProgramData%\SchedulerService\health-monitor.log`. Execution records are retained for five days by the scheduler. The original `scheduler.db` in the project directory is intentionally kept as a pre-deployment copy.

## Uninstall

From elevated PowerShell, stop and uninstall the wrapper without deleting scheduler data:

```powershell
& "$env:ProgramFiles\SchedulerService\SchedulerService.exe" stop
& "$env:ProgramFiles\SchedulerService\SchedulerService.exe" uninstall
Unregister-ScheduledTask -TaskPath '\SchedulerService\' -TaskName SchedulerHealthMonitor -Confirm:$false
```

The `%ProgramData%\SchedulerService` folder contains execution history and task definitions; remove it only when that data is intentionally being retired.
