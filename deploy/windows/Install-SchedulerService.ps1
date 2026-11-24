[CmdletBinding()]
param(
    [string]$WrapperSource = (Join-Path $env:TEMP 'winsw-x64-v2.12.0.exe'),
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$PythonExe = (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    [string]$BootstrapPythonExe = (Get-Command python -ErrorAction Stop).Source,
    [string]$StateRoot = (Join-Path $env:ProgramData 'SchedulerService'),
    [switch]$SkipElevation
)

$ErrorActionPreference = 'Stop'
$serviceName = 'SchedulerService'
$serviceHome = Join-Path $env:ProgramFiles 'SchedulerService'
$wrapperPath = Join-Path $serviceHome 'SchedulerService.exe'
$configPath = Join-Path $serviceHome 'SchedulerService.xml'
$databasePath = Join-Path $StateRoot 'scheduler.db'
$sourceDatabase = Join-Path $ProjectRoot 'scheduler.db'
$serviceTemplate = Join-Path $ProjectRoot 'deploy\windows\scheduler-service.xml.template'
$healthScript = Join-Path $ProjectRoot 'deploy\windows\Monitor-SchedulerHealth.ps1'
$expectedHash = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
$installLog = Join-Path $env:TEMP 'scheduler-service-install.log'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if ($SkipElevation) { throw 'Run this installer from an elevated PowerShell window.' }
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
            '-WrapperSource', ('"{0}"' -f $WrapperSource), '-PythonExe', ('"{0}"' -f $PythonExe),
            '-BootstrapPythonExe', ('"{0}"' -f $BootstrapPythonExe),
            '-ProjectRoot', ('"{0}"' -f $ProjectRoot), '-StateRoot', ('"{0}"' -f $StateRoot)
        )
        $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs `
            -ArgumentList ($arguments -join ' ') -Wait -PassThru
        if ($elevated.ExitCode -ne 0 -and (Test-Path -LiteralPath $installLog)) {
            Get-Content -LiteralPath $installLog -Tail 60 | Write-Host
        }
        exit $elevated.ExitCode
    }
}

function Set-DirectoryPermission([string]$Path, [string]$Account, [string]$Rights) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    & icacls.exe $Path '/grant' ("{0}:{1}" -f $Account, $Rights) '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant $Rights to $Account on $Path." }
}

Assert-Administrator
Start-Transcript -Path $installLog -Force | Out-Null
foreach ($requiredPath in @($WrapperSource, $BootstrapPythonExe, $ProjectRoot, $serviceTemplate, $healthScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Required path not found: $requiredPath" }
}
$venvPath = Split-Path -Parent (Split-Path -Parent $PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe)) {
    & $BootstrapPythonExe -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the scheduler virtual environment.' }
}
$venvConfig = Join-Path $venvPath 'pyvenv.cfg'
$pythonHomeEntry = Get-Content -LiteralPath $venvConfig |
    Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
if (-not $pythonHomeEntry) { throw "Could not determine the base Python path from $venvConfig." }
$pythonHome = ($pythonHomeEntry -split '=', 2)[1].Trim()
if (-not (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe'))) {
    throw "The virtual environment base Python is missing: $pythonHome"
}
& $PythonExe -m pip install --disable-pip-version-check greenlet==3.1.1
if ($LASTEXITCODE -ne 0) { throw 'Could not install the Python 3.9 Windows greenlet wheel.' }
& $PythonExe -m pip install --disable-pip-version-check $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not install scheduler dependencies into its virtual environment.' }
& $PythonExe -c 'import apscheduler, fastapi, pydantic, sqlalchemy, uvicorn'
if ($LASTEXITCODE -ne 0) { throw 'The isolated scheduler environment is missing runtime dependencies.' }
$actualHash = (Get-FileHash -LiteralPath $WrapperSource -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) { throw "WinSW SHA-256 mismatch: $actualHash" }

New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StateRoot 'logs') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StateRoot 'logs\service') -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort 1000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($process.CommandLine -notmatch 'scheduler\.cli.*serve|scheduler\.exe.*serve') {
        throw "Port 1000 is occupied by an unrelated process (PID $($listener.OwningProcess)); stop it before installing."
    }
    Stop-Process -Id $listener.OwningProcess -Force
    if ($process.ParentProcessId) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)"
        if ($parent.CommandLine -match 'scheduler\.exe.*serve') {
            Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

if (-not (Test-Path -LiteralPath $databasePath)) {
    & $PythonExe (Join-Path $ProjectRoot 'deploy\windows\migrate_database.py') $sourceDatabase $databasePath
    if ($LASTEXITCODE -ne 0) { throw 'Scheduler database migration failed.' }
} else {
    Write-Host "Existing service database retained: $databasePath"
}

New-Item -ItemType Directory -Path $serviceHome -Force | Out-Null
Copy-Item -LiteralPath $WrapperSource -Destination $wrapperPath -Force
$databaseUrl = 'sqlite:///' + ($databasePath -replace '\\', '/')
$serviceXml = [IO.File]::ReadAllText($serviceTemplate)
$replacements = @{
    '@@PYTHON@@' = [Security.SecurityElement]::Escape($PythonExe)
    '@@PROJECT_ROOT@@' = [Security.SecurityElement]::Escape($ProjectRoot)
    '@@DATABASE_URL@@' = [Security.SecurityElement]::Escape($databaseUrl)
    '@@LOG_DIR@@' = [Security.SecurityElement]::Escape((Join-Path $StateRoot 'logs\service'))
}
foreach ($placeholder in $replacements.Keys) {
    $serviceXml = $serviceXml.Replace($placeholder, $replacements[$placeholder])
}
if ($serviceXml -match '@@[A-Z_]+@@') { throw 'The WinSW service template has unresolved placeholders.' }
Set-Content -LiteralPath $configPath -Value $serviceXml -Encoding UTF8

Set-DirectoryPermission $serviceHome 'NT AUTHORITY\LOCAL SERVICE' '(OI)(CI)RX'
Set-DirectoryPermission $ProjectRoot 'NT AUTHORITY\LOCAL SERVICE' '(OI)(CI)RX'
Set-DirectoryPermission $pythonHome '*S-1-5-19' '(OI)(CI)RX'
Set-DirectoryPermission $StateRoot 'NT AUTHORITY\LOCAL SERVICE' '(OI)(CI)M'
Set-DirectoryPermission $StateRoot 'NT AUTHORITY\SYSTEM' '(OI)(CI)M'

$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -ne 'Stopped') {
        Stop-Service -Name $serviceName -Force
        (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    & $wrapperPath uninstall
    if ($LASTEXITCODE -ne 0) { throw 'Could not update the existing Scheduler Windows service.' }
}
& $wrapperPath install
if ($LASTEXITCODE -ne 0) { throw 'WinSW could not install the Windows service.' }
& sc.exe config $serviceName 'obj=' 'NT AUTHORITY\LocalService'
if ($LASTEXITCODE -ne 0) { throw 'Could not set the service identity to NT AUTHORITY\LocalService.' }

& $wrapperPath start
if ($LASTEXITCODE -ne 0) { throw 'WinSW could not start the Windows service.' }

$taskFolder = '\SchedulerService'
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$rootFolder = $scheduler.GetFolder('\')
try { $null = $scheduler.GetFolder($taskFolder) }
catch { $null = $rootFolder.CreateFolder('SchedulerService', $null) }

$taskXmlPath = Join-Path $serviceHome 'SchedulerHealthMonitor.xml'
$firstCheck = (Get-Date).AddMinutes(1).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$escapedScript = [Security.SecurityElement]::Escape($healthScript)
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Restart the scheduler after three consecutive failed health checks.</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$firstCheck</StartBoundary><Enabled>true</Enabled></TimeTrigger>
  </Triggers>
  <Principals><Principal id="System"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="System"><Exec><Command>powershell.exe</Command><Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -File &quot;$escapedScript&quot;</Arguments></Exec></Actions>
</Task>
"@
Set-Content -LiteralPath $taskXmlPath -Value $taskXml -Encoding Unicode
& schtasks.exe /Create /TN '\SchedulerService\SchedulerHealthMonitor' /XML $taskXmlPath /F
if ($LASTEXITCODE -ne 0) { throw 'Task Scheduler could not register SchedulerHealthMonitor.' }
& schtasks.exe /Query /TN '\SchedulerService\SchedulerHealthMonitor' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Task Scheduler could not verify SchedulerHealthMonitor after registration.' }

$health = $null
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:1000/api/health' -TimeoutSec 5
        if ($health.status -eq 'ok') { break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $health -or $health.status -ne 'ok') { throw 'Service did not become healthy within 60 seconds.' }
Write-Host "Scheduler service is healthy at http://127.0.0.1:1000 (database: $databasePath)."
Write-Host "Health monitor installed at \SchedulerService\SchedulerHealthMonitor."
Stop-Transcript | Out-Null
