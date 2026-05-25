param(
    [switch]$Persistent,
    [switch]$Once,
    [double]$IntervalSeconds = 30
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
    $IntervalSeconds
)

if ($Persistent) {
    $pollerArgs += "--persistent"
}

if ($Once) {
    $pollerArgs += "--once"
}

"$(Get-Date -Format o) launching raypak poller persistent=$($Persistent.IsPresent) once=$($Once.IsPresent) interval=${IntervalSeconds}s" | Add-Content -Path $LogFile
$ErrorActionPreference = "Continue"
& $Python @pollerArgs *>> $LogFile
exit $LASTEXITCODE
