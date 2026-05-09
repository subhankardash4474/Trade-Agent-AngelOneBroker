# run_battery_overnight.ps1
# ─────────────────────────────────────────────────────────────────────
# One-click overnight launcher for the backtest battery.
#
# What it does:
#   1. Locks AC power-management to "never sleep" (the laptop must stay
#      plugged in!)
#   2. Stops the live daemon + supervisor (no point in paper trading
#      while you're asleep — saves CPU + lets the battery use full cores)
#   3. Detaches the battery script as a hidden background process,
#      redirected to logs/battery_stdout.log + battery_stderr.log
#   4. Prints the PID + how to monitor + how to resume on failure
#
# Usage:
#   .\tools\run_battery_overnight.ps1
#   .\tools\run_battery_overnight.ps1 -ResumeAuto         # auto-resume incomplete
#   .\tools\run_battery_overnight.ps1 -ResumeRun 20260508T173000
#   .\tools\run_battery_overnight.ps1 -Days 14            # shorter for testing
#
# To check status later (from any new shell):
#   .\tools\battery_status.ps1
# ─────────────────────────────────────────────────────────────────────

[CmdletBinding()]
param(
    [int]$Days = 30,
    [switch]$ResumeAuto,
    [string]$ResumeRun = "",
    [switch]$KeepDaemon  # don't kill the live daemon
)

$ErrorActionPreference = "Stop"

# ── Resolve project root + python ──
$proj   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\subhanda\AppData\Local\Programs\Python\Python314\python.exe"
$venvPy = Join-Path $proj ".venv\Scripts\python.exe"
if (Test-Path $venvPy) { $python = $venvPy }
$script = Join-Path $proj "tools\overnight_backtest_battery.py"

if (-not (Test-Path $script)) {
    Write-Error "Battery script not found: $script"; exit 1
}

# ── 1. Power management: refuse to sleep on AC ──
Write-Host "[1/4] Locking power policy: never sleep on AC..." -ForegroundColor Cyan
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 30   # display can sleep, system stays on
Write-Host "      AC standby timeout: never (display: 30 min)"
Write-Host "      ⚠  Keep the laptop plugged in or it WILL sleep on battery!"

# ── 2. Stop live daemon + supervisor ──
if (-not $KeepDaemon) {
    Write-Host "[2/4] Stopping live daemon + supervisor..." -ForegroundColor Cyan
    $stopped = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "run_daemon\.py" -and $_.CommandLine -notmatch "tail" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match "run_daemon_resilient" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    Write-Host "      stopped $stopped process(es)"
} else {
    Write-Host "[2/4] -KeepDaemon set — leaving live daemon running." -ForegroundColor Yellow
}

# ── 3. Build battery args ──
$argList = @("`"$script`"", "--days", $Days)
if ($ResumeAuto)        { $argList += @("--resume", "auto") }
elseif ($ResumeRun)     { $argList += @("--resume", $ResumeRun) }

# ── 4. Launch detached ──
Write-Host "[3/4] Launching battery as background process..." -ForegroundColor Cyan
$logsDir = Join-Path $proj "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }
$stdout = Join-Path $logsDir "battery_stdout.log"
$stderr = Join-Path $logsDir "battery_stderr.log"

$proc = Start-Process `
    -WorkingDirectory $proj `
    -FilePath $python `
    -ArgumentList $argList `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
    Write-Host "      [OK] Battery launched: PID=$($proc.Id)" -ForegroundColor Green
} else {
    Write-Host "      [FAIL] Process exited within 3s — check $stderr" -ForegroundColor Red
    Get-Content $stderr -Tail 10
    exit 1
}

# ── 5. Print monitor instructions ──
Write-Host ""
Write-Host "[4/4] Battery is running. ──────────────────────────────────────" -ForegroundColor Green
Write-Host "  PID            : $($proc.Id)"
Write-Host "  Stdout log     : $stdout"
Write-Host "  Stderr log     : $stderr"
Write-Host "  Variant results: $proj\logs\backtests\<latest>\results\"
Write-Host "  Comparison     : $proj\logs\backtests\<latest>\comparison.md"
Write-Host ""
Write-Host "MONITOR FROM ANY SHELL:" -ForegroundColor Yellow
Write-Host "  powershell .\tools\battery_status.ps1"
Write-Host ""
Write-Host "RESUME IF IT CRASHES:" -ForegroundColor Yellow
Write-Host "  .\tools\run_battery_overnight.ps1 -ResumeAuto"
Write-Host ""
Write-Host "KILL EARLY:" -ForegroundColor Yellow
Write-Host "  Stop-Process -Id $($proc.Id) -Force"
Write-Host ""
Write-Host "Estimated completion: ~10-12 hours (15 variants x ~45 min)" -ForegroundColor Gray
Write-Host "Started at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
