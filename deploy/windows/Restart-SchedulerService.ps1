$ErrorActionPreference = 'Stop'

$stateRoot = Join-Path $env:ProgramData 'SchedulerService'
$logDirectory = Join-Path $stateRoot 'logs'
$logPath = Join-Path $logDirectory 'service-recovery.log'
$healthUrl = 'http://127.0.0.1:1000/api/health'
$jobsUrl = 'http://127.0.0.1:1000/api/jobs'
$recentUrl = 'http://127.0.0.1:1000/api/runs/recent'

function Write-RecoveryLog {
    param([Parameter(Mandatory)][string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'), $Message
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Requesting administrator approval through UAC.'
        $arguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-NoExit',
            '-File', ('"{0}"' -f $PSCommandPath)
        )
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
        exit 0
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Write-RecoveryLog 'Running with an elevated administrator token.'
    $service = Get-Service -Name 'SchedulerService'
    if ($service.Status -eq 'Running') {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        if ($health.status -ne 'ok' -or -not $health.scheduler_running) {
            throw 'SchedulerService is running but its health endpoint is not healthy; no process was stopped.'
        }
        $recent = Invoke-RestMethod -Uri $recentUrl -TimeoutSec 5
        $runs = @(
            foreach ($property in $recent.PSObject.Properties) {
                foreach ($run in @($property.Value)) { $run }
            }
        )
        $activeRuns = @($runs | Where-Object { $_.status -eq 'running' })
        Write-RecoveryLog 'SchedulerService is already running and healthy; no restart was needed.'
    } else {
        $recent = Invoke-RestMethod -Uri $recentUrl -TimeoutSec 5
        $runs = @(
            foreach ($property in $recent.PSObject.Properties) {
                foreach ($run in @($property.Value)) { $run }
            }
        )
        $activeRuns = @($runs | Where-Object { $_.status -eq 'running' })
        if ($activeRuns.Count -gt 0) {
            throw "Found $($activeRuns.Count) active run records; no process was stopped."
        }

        $listener = Get-NetTCPConnection -LocalPort 1000 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($listener) {
            throw "Port 1000 is occupied by PID $($listener.OwningProcess) while SchedulerService is stopped. Inspect that process before stopping it; no process was terminated."
        }

        Write-RecoveryLog 'Starting SchedulerService.'
        Start-Service -Name 'SchedulerService'

        $healthy = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ((Get-Service -Name 'SchedulerService').Status -eq 'Running') {
                try {
                    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
                    if ($health.status -eq 'ok' -and $health.scheduler_running) {
                        $healthy = $true
                        break
                    }
                } catch {
                    # The service may still be starting; the bounded loop retries once per second.
                }
            }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) {
            throw "SchedulerService did not become healthy within 60 seconds (state=$((Get-Service -Name 'SchedulerService').Status))."
        }

        $jobs = Invoke-RestMethod -Uri $jobsUrl -TimeoutSec 5
        $root = $jobs | Where-Object { $_.id -eq 'root' } | Select-Object -First 1
        if (-not $root -or -not $root.next_run_time) {
            throw 'The service is healthy, but the root schedule is missing its next run time.'
        }
        Write-RecoveryLog ("SchedulerService is healthy. Root cron: {0}; next run: {1}." -f $root.cron, $root.next_run_time)
    }

    $catchUp = Invoke-RestMethod -Uri 'http://127.0.0.1:1000/api/runs/catch-up-today' -Method Post -TimeoutSec 5
    $queuedJobs = @($catchUp.jobs)
    Write-RecoveryLog ("Today's catch-up result: {0}; jobs queued: {1}. {2}" -f $catchUp.status, $queuedJobs.Count, ($queuedJobs -join ', '))

    $recent = Invoke-RestMethod -Uri $recentUrl -TimeoutSec 5
    $runs = @(
        foreach ($property in $recent.PSObject.Properties) {
            foreach ($run in @($property.Value)) { $run }
        }
    )
    $activeRuns = @($runs | Where-Object { $_.status -eq 'running' })
    $todayRuns = @(
        $runs | Where-Object {
            $_.started_at -and
            [DateTimeOffset]::Parse($_.started_at).ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd') -eq
            (Get-Date).ToString('yyyy-MM-dd')
        }
    )
    Write-RecoveryLog ("Current health: {0}; running records: {1}; today's existing run records: {2}." -f $health.status, $activeRuns.Count, $todayRuns.Count)
    Write-RecoveryLog 'Missing Cron runs are queued through SchedulerService under its service identity; the elevated shell does not run task commands.'
    Write-RecoveryLog "Recovery log: $logPath"
} catch {
    try {
        Write-RecoveryLog ("FAILED: {0}" -f $_.Exception.Message)
        Write-RecoveryLog "Recovery log: $logPath"
    } catch {
        Write-Host ("FAILED: {0}" -f $_.Exception.Message)
    }
    exit 1
}
