param(
    [string]$TaskName = "Raypak Poller",
    [switch]$Persistent,
    [switch]$CurrentUser,
    [double]$IntervalSeconds = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $ProjectRoot "run-raypak-poller.ps1"

if (-not (Test-Path $Runner)) {
    throw "Runner script not found: $Runner"
}

if (-not $CurrentUser) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script as Administrator, or pass -CurrentUser to install a logon-only task."
    }
}

$runnerArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    "`"$Runner`"",
    "-IntervalSeconds",
    $IntervalSeconds
)

if ($Persistent) {
    $runnerArgs += "-Persistent"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($runnerArgs -join " ") `
    -WorkingDirectory $ProjectRoot

if ($CurrentUser) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel LeastPrivilege
} else {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
}

$description = if ($CurrentUser) {
    "Poll Raypak Crosswind heater and write telemetry to InfluxDB at user logon"
} else {
    "Poll Raypak Crosswind heater and write telemetry to InfluxDB at system startup"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description $description `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started scheduled task: $TaskName"
Write-Host "Mode: $(if ($CurrentUser) { 'current-user logon' } else { 'SYSTEM startup' })"
Write-Host "Runner: $Runner"
Write-Host "Log: $ProjectRoot\logs\raypak-poller.log"
