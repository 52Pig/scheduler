#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'
$taskPath = '\SchedulerService\'
$taskName = 'SchedulerHealthMonitor'
$serviceName = 'SchedulerService'
$healthUrl = 'http://127.0.0.1:1000/api/health'
$stateRoot = Join-Path $env:ProgramData 'SchedulerService'
$stateFile = Join-Path $stateRoot 'health-failures.txt'
$monitorLog = Join-Path $stateRoot 'health-monitor.log'

function Wait-ForHealth([int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
            if ($health.status -eq 'ok') { return $health }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "Scheduler health did not recover within $TimeoutSeconds seconds."
}

try {
    $null = Wait-ForHealth
    Set-Content -LiteralPath $stateFile -Value '0'
    Stop-Service -Name $serviceName -Force
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))

    foreach ($expectedFailures in 1..2) {
        Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
        $deadline = (Get-Date).AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 300
            $current = [int](Get-Content -LiteralPath $stateFile -Raw)
        } while ($current -ne $expectedFailures -and (Get-Date) -lt $deadline)
        if ($current -ne $expectedFailures) {
            throw "Expected health monitor failure count $expectedFailures, observed $current."
        }
    }

    Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
    $null = Wait-ForHealth
    $restartMessage = Get-Content -LiteralPath $monitorLog -Tail 20 |
        Where-Object { $_ -match 'Restarted SchedulerService after three consecutive health failures' }
    if (-not $restartMessage) { throw 'The monitor did not record the expected automatic restart.' }
    Write-Host 'Recovery test passed: three failed checks restarted the service and health returned to ok.'
} finally {
    if ((Get-Service -Name $serviceName).Status -ne 'Running') {
        Start-Service -Name $serviceName
        (Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    }
}
