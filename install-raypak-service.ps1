param(
    [string]$ServiceName = "RaypakPoller",
    [string]$DisplayName = "Raypak Poller",
    [string]$WinSWExe = "",
    [int]$IntervalSeconds = 30,
    [int]$FaultSampleSeconds = 2,
    [int]$FaultSampleAttempts = 5,
    [int]$WeatherRefreshSeconds = 300,
    [switch]$Persistent,
    [switch]$KeepScheduledTask
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $adminRole = [Security.Principal.WindowsBuiltInRole]::Administrator

    if (-not $principal.IsInRole($adminRole)) {
        throw "Run this script from an elevated PowerShell session."
    }
}

function ConvertTo-XmlValue {
    param([string]$Value)
    return [Security.SecurityElement]::Escape($Value)
}

Assert-Admin

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir = Join-Path $ProjectRoot "tools"
$ServiceExe = Join-Path $ToolsDir "$ServiceName.exe"
$ServiceXml = Join-Path $ToolsDir "$ServiceName.xml"
$PythonExe = "C:\Python312\python.exe"
$PollerScript = Join-Path $ProjectRoot "raypak_poller.py"
$LegacyTaskName = "Raypak Poller"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

if (-not (Test-Path -LiteralPath $PollerScript)) {
    throw "Poller script not found: $PollerScript"
}

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

if ($WinSWExe) {
    if (-not (Test-Path -LiteralPath $WinSWExe)) {
        throw "WinSW executable not found: $WinSWExe"
    }

    Copy-Item -LiteralPath $WinSWExe -Destination $ServiceExe -Force
}
elseif (-not (Test-Path -LiteralPath $ServiceExe)) {
    throw "WinSW executable missing. Download the WinSW x64 executable, then run with -WinSWExe C:\path\to\WinSW-x64.exe, or place it at $ServiceExe."
}

$arguments = @(
    (ConvertTo-XmlValue $PollerScript),
    "--interval-seconds", $IntervalSeconds,
    "--fault-sample-seconds", $FaultSampleSeconds,
    "--fault-sample-attempts", $FaultSampleAttempts,
    "--weather-refresh-seconds", $WeatherRefreshSeconds
)

if ($Persistent) {
    $arguments += "--persistent"
}

$argumentText = $arguments -join " "
$projectRootXml = ConvertTo-XmlValue $ProjectRoot
$pythonExeXml = ConvertTo-XmlValue $PythonExe
$displayNameXml = ConvertTo-XmlValue $DisplayName
$logPathXml = ConvertTo-XmlValue (Join-Path $ProjectRoot "logs")

$serviceConfig = @"
<service>
  <id>$ServiceName</id>
  <name>$displayNameXml</name>
  <description>Poll Raypak Crosswind heater telemetry and write data to InfluxDB.</description>
  <executable>$pythonExeXml</executable>
  <arguments>$argumentText</arguments>
  <workingdirectory>$projectRootXml</workingdirectory>
  <startmode>Automatic</startmode>
  <env name="PYTHONWARNINGS" value="ignore" />
  <env name="PYTHONIOENCODING" value="utf-8" />
  <logpath>$logPathXml</logpath>
  <log mode="append" />
</service>
"@

$serviceConfig | Set-Content -LiteralPath $ServiceXml -Encoding UTF8

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    if ($existingService.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force
        $existingService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
    }

    & $ServiceExe uninstall
}

& $ServiceExe install
& $ServiceExe start

$service = Get-Service -Name $ServiceName
$service.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
Start-Sleep -Seconds 20
$service = Get-Service -Name $ServiceName

if ($service.Status -ne "Running") {
    throw "Service installed but did not reach Running state. Check logs in $logPathXml."
}

if (-not $KeepScheduledTask) {
    $legacyTask = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($legacyTask) {
        Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false
        Write-Host "Removed legacy scheduled task: $LegacyTaskName"
    }
}

Write-Host "Installed and started service: $ServiceName"
Write-Host "Status: $($service.Status)"
Write-Host "Config: $ServiceXml"
Write-Host "Logs: $logPathXml"
Write-Host ""
Write-Host "Manage with:"
Write-Host "  Get-Service $ServiceName"
Write-Host "  Start-Service $ServiceName"
Write-Host "  Stop-Service $ServiceName"
Write-Host "  Restart-Service $ServiceName"
