param(
    [switch]$Persistent,
    [switch]$Once,
    [double]$IntervalSeconds = 30,
    [double]$FaultSampleSeconds = 2,
    [int]$FaultSampleAttempts = 5,
    [Nullable[double]]$WeatherLatitude,
    [Nullable[double]]$WeatherLongitude
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "raypak-poller.log"
$Python = "C:\Python312\python.exe"
$Poller = Join-Path $ProjectRoot "raypak_poller.py"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $ProjectRoot
$env:PYTHONWARNINGS = "ignore"

$pollerArgs = @(
    $Poller,
    "--interval-seconds",
    $IntervalSeconds,
    "--fault-sample-seconds",
    $FaultSampleSeconds,
    "--fault-sample-attempts",
    $FaultSampleAttempts
)

if ($Persistent) {
    $pollerArgs += "--persistent"
}

if ($Once) {
    $pollerArgs += "--once"
}

if ($WeatherLatitude.HasValue) {
    $pollerArgs += @("--weather-latitude", $WeatherLatitude.Value)
}

if ($WeatherLongitude.HasValue) {
    $pollerArgs += @("--weather-longitude", $WeatherLongitude.Value)
}

"$(Get-Date -Format o) launching raypak poller persistent=$($Persistent.IsPresent) once=$($Once.IsPresent) interval=${IntervalSeconds}s fault_sample=${FaultSampleSeconds}s fault_attempts=${FaultSampleAttempts} weather_args=$($WeatherLatitude.HasValue -or $WeatherLongitude.HasValue)" | Add-Content -Path $LogFile
$ErrorActionPreference = "Continue"
& $Python @pollerArgs *>> $LogFile
exit $LASTEXITCODE
