# Register a per-user Windows Scheduled Task that runs rain_monitor.py every N minutes.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_task.ps1 -IntervalMinutes 5 -RunNow
# The task runs as the signed-in user, only while that user is logged on.
# It uses pythonw.exe, so no console window appears; logs go to .\logs\monitor.log.
[CmdletBinding()]
param(
    [string]$TaskName = 'RainMonitor',
    [string]$PythonExe = '',
    [int]$IntervalMinutes = 0,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 decodes native-command output with the console codepage,
# which mangles non-ASCII user names (e.g. C:\Users\<Chinese name>\...). Force UTF-8
# for both the console decoder and Python's stdout before capturing any path.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # Older hosts may not allow changing the console encoding.
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$monitor = Join-Path $root 'rain_monitor.py'
if (-not (Test-Path -LiteralPath $monitor)) {
    throw "rain_monitor.py not found next to this script: $monitor"
}

# Resolve interval from config.json unless explicitly supplied.
if ($IntervalMinutes -le 0) {
    $configPath = Join-Path $root 'config.json'
    if (Test-Path -LiteralPath $configPath) {
        try {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $config.interval_minutes) {
                $IntervalMinutes = [int]$config.interval_minutes
            }
        } catch {
            Write-Warning "Could not read interval_minutes from config.json; using 5."
        }
    }
}
if ($IntervalMinutes -le 0) { $IntervalMinutes = 5 }
if ($IntervalMinutes -lt 1 -or $IntervalMinutes -gt 1439) {
    throw "IntervalMinutes must be between 1 and 1439."
}

# Resolve a Python executable.
function Resolve-PythonExe {
    param([string]$Requested)

    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested)) { throw "Python executable not found: $Requested" }
        return (Resolve-Path -LiteralPath $Requested).Path
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($version in @('-3.14', '-3')) {
            try {
                $candidate = (& $pyLauncher.Source $version -c "import sys; print(sys.executable)" 2>$null)
                if ($candidate) { $candidate = "$candidate".Trim() }
                if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) {
                    return (Resolve-Path -LiteralPath $candidate).Path
                }
            } catch {
                # Try the next interpreter.
            }
        }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $candidate = (& $pythonCmd.Source -c "import sys; print(sys.executable)" 2>$null)
        if ($candidate) { $candidate = "$candidate".Trim() }
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Python 3 not found. Install Python 3.9+ or pass -PythonExe 'C:\path\to\python.exe'."
}

$python = Resolve-PythonExe -Requested $PythonExe
$pythonw = $python
$pythonwCandidate = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonwCandidate) {
    $pythonw = $pythonwCandidate
}

$action = '"' + $pythonw + '" "' + $monitor + '" --once'

Write-Host "Task name : $TaskName"
Write-Host "Python    : $pythonw"
Write-Host "Monitor   : $monitor"
Write-Host "Interval  : every $IntervalMinutes minute(s)"

$arguments = @(
    '/Create',
    '/TN', $TaskName,
    '/SC', 'MINUTE',
    '/MO', "$IntervalMinutes",
    '/TR', $action,
    '/F'
)

$output = & schtasks.exe @arguments 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "schtasks.exe failed (exit $LASTEXITCODE): $output"
}
Write-Host $output

if ($RunNow) {
    $runOutput = & schtasks.exe /Run /TN $TaskName 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Task registered, but the immediate run failed: $runOutput"
    } else {
        Write-Host "Started one run now."
    }
}

Write-Host ""
Write-Host "Registered. Check status with:"
Write-Host "  schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host "Log file:"
Write-Host "  $(Join-Path $root 'logs\monitor.log')"
Write-Host "Remove with:"
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $root 'uninstall_task.ps1')`" -TaskName `"$TaskName`""
