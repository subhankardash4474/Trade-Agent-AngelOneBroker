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

# F-78: prefer the project venv, then any python on PATH, then the
# historic hardcoded path as a last-resort fallback. Previously the
# hardcoded developer path was tried first, which means any other
# machine (CI, another laptop, the cloud VM) would launch the wrong
# interpreter unless the venv happened to exist.
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Python = $null
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $pythonOnPath = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonOnPath) {
        $Python = $pythonOnPath.Source
    } else {
        # Last-resort fallback to the legacy developer path; warn loudly.
        $LegacyPython = "C:\Users\subhanda\AppData\Local\Programs\Python\Python314\python.exe"
        if (Test-Path $LegacyPython) {
            $Python = $LegacyPython
            Write-Host "[WARN] Using legacy developer python path: $Python"
        } else {
            Write-Host "[FATAL] No python interpreter found (no .venv, no python on PATH)."
            exit 1
        }
    }
}

$DaemonScript = Join-Path $ProjectRoot "run_daemon.py"
if (-not (Test-Path $DaemonScript)) {
    Write-Sup "[FATAL] $DaemonScript not found - cannot start daemon."
    exit 1
}

# F-04: the daemon honours operations.emergency_stop_path (default
# `logs/STOP`). The legacy `EMERGENCY_STOP` at repo root is kept as
# an additional path so historic runbooks continue to work. Supervisor
# halts when EITHER file is present.
$StopFileLogs   = Join-Path $ProjectRoot "logs\STOP"
$StopFileLegacy = Join-Path $ProjectRoot "EMERGENCY_STOP"
$RestartDelaySeconds = 30
$RestartCount = 0
$MaxRestartsPerHour = 10  # safety: stop flapping if we restart > 10x/hr

$RecentRestarts = New-Object 'System.Collections.Generic.Queue[DateTime]'

Write-Sup "[SUPERVISOR-START] PID=$PID. Python=$Python. Daemon=$DaemonScript."

while ($true) {

    # Pre-launch checks (F-04: honour both the daemon's canonical
    # `logs/STOP` and the legacy repo-root `EMERGENCY_STOP`).
    if (Test-Path $StopFileLogs) {
        Write-Sup "[SUPERVISOR-STOP] Kill-switch present at $StopFileLogs (config: operations.emergency_stop_path). Exiting supervisor."
        exit 0
    }
    if (Test-Path $StopFileLegacy) {
        Write-Sup "[SUPERVISOR-STOP] Legacy kill-switch present at $StopFileLegacy. Exiting supervisor."
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
        $exitCode = 1   # treat launch failure as a non-clean exit so the cooldown-and-retry path fires
    }

    # 2026-05-30 brutal review Finding 6 (Session 2 §3): the supervisor
    # was restarting the daemon UNCONDITIONALLY, including after a clean
    # post-close exit (intraday cutoff at 15:15 IST flips _running=False
    # and the daemon returns 0). On 2026-05-30 the local laptop logged
    # 21 restart cycles by 10:10 IST -- a Saturday with the broker WS
    # closed -- generating dozens of failed reconnect attempts that
    # could mark the AngelOne client_id as suspicious in the next live
    # session.
    #
    # Fix: exit_code == 0 means the daemon shut down INTENTIONALLY (clean
    # shutdown via SIGTERM, post-close cutoff, or explicit kill-switch).
    # Don't relaunch -- trust the daemon's exit. Non-zero (crash, OOM,
    # broker-init failure, etc.) still triggers the cooldown-and-retry
    # path so transient failures self-heal.
    #
    # Activation: the env var SUPERVISOR_RESTART_ON_CLEAN_EXIT=1 restores
    # the legacy "always restart" behaviour for operators who had a
    # specific reason to depend on it. Default is "exit on clean".
    $restartOnClean = [bool]([System.Environment]::GetEnvironmentVariable(
        "SUPERVISOR_RESTART_ON_CLEAN_EXIT") -eq "1")
    if ($exitCode -eq 0 -and -not $restartOnClean) {
        Write-Sup "[SUPERVISOR-CLEAN-EXIT] daemon exit_code=0 (intentional). Supervisor exiting -- relaunch only on crash. Set SUPERVISOR_RESTART_ON_CLEAN_EXIT=1 to restore legacy always-restart behaviour."
        exit 0
    }

    # Brief cooldown before the next launch - prevents tight-loop on
    # repeatable startup failure (e.g. config file syntax error).
    $cooldown = $RestartDelaySeconds
    Write-Sup "[SUPERVISOR-COOLDOWN] sleeping ${cooldown}s before relaunch (exit_code=$exitCode)"
    Start-Sleep -Seconds $cooldown
}
