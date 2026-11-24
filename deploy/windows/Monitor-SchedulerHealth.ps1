$ErrorActionPreference = 'SilentlyContinue'
$serviceName = 'SchedulerService'
$stateRoot = Join-Path $env:ProgramData 'SchedulerService'
$failureFile = Join-Path $stateRoot 'health-failures.txt'
$logFile = Join-Path $stateRoot 'health-monitor.log'
$healthUrl = 'http://127.0.0.1:1000/api/health'

function Write-MonitorLog([string]$Message) {
    if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 1MB) {
        Move-Item -LiteralPath $logFile -Destination ($logFile + '.1') -Force
    }
    Add-Content -LiteralPath $logFile -Value ("{0:o} {1}" -f [DateTime]::Now, $Message)
}

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 8
    if ($health.status -eq 'ok') {
        Set-Content -LiteralPath $failureFile -Value '0'
        exit 0
    }
    throw "health status: $($health.status)"
} catch {
    $failures = 0
    if (Test-Path -LiteralPath $failureFile) {
        [void][int]::TryParse((Get-Content -LiteralPath $failureFile -Raw), [ref]$failures)
    }
    $failures = [Math]::Min($failures + 1, 3)
    Set-Content -LiteralPath $failureFile -Value $failures
    Write-MonitorLog "Health check failed ($failures/3): $($_.Exception.Message)"
    if ($failures -ge 3) {
        try {
            Restart-Service -Name $serviceName -Force -ErrorAction Stop
            Set-Content -LiteralPath $failureFile -Value '0'
            Write-MonitorLog 'Restarted SchedulerService after three consecutive health failures.'
        } catch {
            Write-MonitorLog "Service restart failed: $($_.Exception.Message)"
        }
    }
}
