# run_daemon_resilient.ps1
# -----------------------------------------------------------------------
# Layer-1 watchdog wrapper around `python run_daemon.py --paper`.
#
# Behaviour:
#   - Launches the trading daemon as a foreground child process.
#   - On any exit (clean or crash), waits 30 seconds and relaunches.
#   - Logs every transition to logs/daemon_supervisor.log so we can audit
#     why the daemon restarted.
#   - Exits only when an "EMERGENCY_STOP" file is present at the project
#     root (matches the operations.emergency_stop_path config).
#
# Usage (manual):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_daemon_resilient.ps1
#
# Usage (Scheduled Task): registered automatically by tools\install_scheduled_task.ps1
# -----------------------------------------------------------------------

$ErrorActionPreference = "Continue"

# Resolve project root (one level up from tools/).
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$SupLog = Join-Path $LogDir "daemon_supervisor.log"

function Write-Sup($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts | $msg"
    Add-Content -Path $SupLog -Value $line
    Write-Host $line
}

# Find the python interpreter that has all our deps installed. Prefer the
# project venv if present, fall back to the system python that's been used
# all session.
$Python = "C:\Users\subhanda\AppData\Local\Programs\Python\Python314\python.exe"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) { $Python = $VenvPython }

$DaemonScript = Join-Path $ProjectRoot "run_daemon.py"
if (-not (Test-Path $DaemonScript)) {
    Write-Sup "[FATAL] $DaemonScript not found - cannot start daemon."
    exit 1
}

$EmergencyStop = Join-Path $ProjectRoot "EMERGENCY_STOP"
$RestartDelaySeconds = 30
$RestartCount = 0
$MaxRestartsPerHour = 10  # safety: stop flapping if we restart > 10x/hr

$RecentRestarts = New-Object 'System.Collections.Generic.Queue[DateTime]'

Write-Sup "[SUPERVISOR-START] PID=$PID. Python=$Python. Daemon=$DaemonScript."

while ($true) {

    # Pre-launch checks
    if (Test-Path $EmergencyStop) {
        Write-Sup "[SUPERVISOR-STOP] EMERGENCY_STOP file present at $EmergencyStop. Exiting supervisor."
        exit 0
    }

    # Restart-rate limiter (safety against flapping)
    $now = Get-Date
    while ($RecentRestarts.Count -gt 0 -and ($now - $RecentRestarts.Peek()).TotalMinutes -gt 60) {
        [void]$RecentRestarts.Dequeue()
    }
    if ($RecentRestarts.Count -ge $MaxRestartsPerHour) {
        Write-Sup "[SUPERVISOR-FLAP] $MaxRestartsPerHour restarts in last hour - backing off for 10 min before retrying."
        Start-Sleep -Seconds 600
        $RecentRestarts.Clear()
    }

    Write-Sup "[DAEMON-LAUNCH] attempt #$($RestartCount + 1)"
    $RestartCount += 1
    $RecentRestarts.Enqueue($now)

    # Launch the daemon as a child process and wait. We capture both
    # stdout/stderr to the same supervisor log so we never lose final
    # bytes from a crashing daemon.
    #
    # IMPORTANT: the daemon script path contains spaces ("OneDrive -
    # AMDOCS"). Passing it via -ArgumentList @($DaemonScript, ...) leaves
    # the array element unquoted on the final command line, causing
    # python to see only "C:\Users\subhanda\OneDrive" and fail with
    # "can't find '__main__' module". We pre-quote the path and pass
    # ArgumentList as a single string so PowerShell preserves the quotes.
    $QuotedDaemon = '"' + $DaemonScript + '"'
    $DaemonArgs   = "$QuotedDaemon --paper --interval 60"
    $StartArgs = @{
        FilePath               = $Python
        ArgumentList           = $DaemonArgs
        WorkingDirectory       = $ProjectRoot
        NoNewWindow            = $true
        Wait                   = $true
        PassThru               = $true
        RedirectStandardError  = (Join-Path $LogDir "daemon_stderr_latest.log")
        RedirectStandardOutput = (Join-Path $LogDir "daemon_stdout_latest.log")
    }

    try {
        $proc = Start-Process @StartArgs
        $exitCode = $proc.ExitCode
        Write-Sup "[DAEMON-EXIT] PID=$($proc.Id) exit_code=$exitCode"
    }
    catch {
        Write-Sup "[DAEMON-LAUNCH-FAIL] $($_.Exception.Message)"
    }

    # Brief cooldown before the next launch - prevents tight-loop on
    # repeatable startup failure (e.g. config file syntax error).
    $cooldown = $RestartDelaySeconds
    Write-Sup "[SUPERVISOR-COOLDOWN] sleeping ${cooldown}s before relaunch"
    Start-Sleep -Seconds $cooldown
}
