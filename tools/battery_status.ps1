# battery_status.ps1
# ---------------------------------------------------------------------
# Quick status check for the overnight backtest battery.
# Shows: alive process, latest run progress, last comparison.md tail.
#
# Usage (from project root):
#   .\tools\battery_status.ps1
# ---------------------------------------------------------------------

$proj = Split-Path -Parent $PSScriptRoot

# 1. Is the battery process alive?
Write-Host "== BATTERY PROCESS =================================" -ForegroundColor Cyan
$bat = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
       Where-Object { $_.CommandLine -match "overnight_backtest_battery" }
if ($bat) {
    $upMin  = [math]::Round((New-TimeSpan -Start $bat.CreationDate -End (Get-Date)).TotalMinutes, 1)
    $ramMB  = [math]::Round($bat.WorkingSetSize / 1MB, 0)
    $cpuMin = [math]::Round($bat.UserModeTime / 600000000, 1)
    Write-Host "  PID     : $($bat.ProcessId)" -ForegroundColor Green
    Write-Host "  Started : $($bat.CreationDate)"
    Write-Host "  Up      : $upMin min"
    Write-Host "  RAM     : $ramMB MB"
    Write-Host "  CPU     : $cpuMin min"
} else {
    Write-Host "  [NOT RUNNING]" -ForegroundColor Yellow
}

# 2. Find latest run dir
Write-Host ""
Write-Host "== LATEST RUN ======================================" -ForegroundColor Cyan
$btDir = Join-Path $proj "logs\backtests"
if (-not (Test-Path $btDir)) {
    Write-Host "  No runs yet." -ForegroundColor Yellow
    return
}
$latest = Get-ChildItem $btDir -Directory | Sort-Object Name -Descending | Select-Object -First 1
if (-not $latest) { Write-Host "  No runs yet." -ForegroundColor Yellow; return }

Write-Host "  Run ID  : $($latest.Name)"
Write-Host "  Path    : $($latest.FullName)"

# 3. Variants completed / failed
$resultsDir = Join-Path $latest.FullName "results"
$done = @(Get-ChildItem $resultsDir -Filter "*.json" -ErrorAction SilentlyContinue)
$fail = @(Get-ChildItem $resultsDir -Filter "*.failure.txt" -ErrorAction SilentlyContinue)
Write-Host "  Variants: $($done.Count) done / 15 total ($($fail.Count) failed)"

if ($done.Count -gt 0) {
    Write-Host ""
    Write-Host "  Completed (chronological):" -ForegroundColor Gray
    $done | Sort-Object LastWriteTime | ForEach-Object {
        $ts = (Get-Date $_.LastWriteTime).ToString('HH:mm')
        Write-Host ("    [{0}] {1}" -f $ts, $_.BaseName)
    }
}

$cache = Join-Path $latest.FullName "market_data.pkl"
if (Test-Path $cache) {
    $sizeMB = [math]::Round((Get-Item $cache).Length / 1MB, 1)
    Write-Host ""
    Write-Host "  Cache   : market_data.pkl present ($sizeMB MB) -- resume-safe"
}

# 4. Comparison.md tail
$comp = Join-Path $latest.FullName "comparison.md"
if (Test-Path $comp) {
    $isComplete = (Get-Content $comp -Raw) -match "\[COMPLETE\]"
    $statusColor = if ($isComplete) { "Green" } else { "Yellow" }
    $statusText  = if ($isComplete) { "COMPLETE" } else { "IN-PROGRESS" }
    Write-Host ""
    Write-Host "  Status  : $statusText" -ForegroundColor $statusColor
    Write-Host ""
    Write-Host "== COMPARISON.MD (last 25 lines) ===================" -ForegroundColor Cyan
    Get-Content $comp -Tail 25
} else {
    Write-Host "  comparison.md not yet written"
}

# 5. Stdout tail
$stdout = Join-Path $proj "logs\battery_stdout.log"
if ((Test-Path $stdout) -and ((Get-Item $stdout).Length -gt 0)) {
    Write-Host ""
    Write-Host "== STDOUT (last 5 lines) ===========================" -ForegroundColor Cyan
    Get-Content $stdout -Tail 5
}

# 6. Stderr (only show if non-empty errors)
$stderr = Join-Path $proj "logs\battery_stderr.log"
if ((Test-Path $stderr) -and ((Get-Item $stderr).Length -gt 0)) {
    $tail = Get-Content $stderr -Tail 3
    if ($tail | Where-Object { $_ -match "Error|Exception|Traceback" }) {
        Write-Host ""
        Write-Host "== STDERR (recent errors) ==========================" -ForegroundColor Red
        $tail | ForEach-Object { Write-Host "  $_" }
    }
}
