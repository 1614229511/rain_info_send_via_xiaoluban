# Remove the Windows Scheduled Task created by install_task.ps1.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File uninstall_task.ps1
[CmdletBinding()]
param(
    [string]$TaskName = 'RainMonitor'
)

$ErrorActionPreference = 'Stop'

$output = & schtasks.exe /Delete /TN $TaskName /F 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not delete task '$TaskName' (exit $LASTEXITCODE): $output"
    exit $LASTEXITCODE
}

Write-Host "Deleted scheduled task '$TaskName'."
