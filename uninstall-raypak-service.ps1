param(
    [string]$ServiceName = "RaypakPoller"
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

Assert-Admin

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceExe = Join-Path $ProjectRoot "tools\$ServiceName.exe"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}

if (Test-Path -LiteralPath $ServiceExe) {
    & $ServiceExe uninstall
}
elseif ($service) {
    & sc.exe delete $ServiceName
}
else {
    Write-Host "Service not found: $ServiceName"
    exit 0
}

Write-Host "Uninstalled service: $ServiceName"
