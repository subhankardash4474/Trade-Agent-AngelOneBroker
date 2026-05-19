<#
.SYNOPSIS
    Cloud-aware battery status for the dedicated backtester VM.

.DESCRIPTION
    Counterpart to tools/battery_status.ps1 (which reads the laptop's
    overnight battery). This script SSHes into the backtester VM and
    answers, in one shot:

      * Is the battery-scheduler systemd unit alive?
      * Is a battery_* container running, and how hot is it (CPU / RAM)?
      * What is the most recent run, and how many of its variants have
        produced result JSONs vs total?
      * What is each active worker doing right now (last log line +
        log file size as a crude progress proxy)?
      * Tail of comparison.md (so you see the markdown table-in-progress).
      * Queue scheduler state (which jobs are done / next / pending).
      * Disk space on the run-dir filesystem.
      * A best-effort ETA estimate from results-completed-per-hour.

    Read-only; freeze-safe (does not modify any code, config, or queue
    on the VM). Sits next to pull_battery_results.ps1 in the operator's
    cloud toolkit.

.PARAMETER VmHost
    Hostname or IP of the backtester VM. Resolution order:
      1. -VmHost arg
      2. $env:BACKTESTER_VM_HOST
      3. fail with an error.

.PARAMETER SshUser
    SSH user. Resolution order:
      1. -SshUser arg
      2. $env:BACKTESTER_SSH_USER
      3. 'opc' (matches pull_battery_results.ps1 default).

.PARAMETER SshKey
    Path to the SSH private key. Defaults to $HOME\.ssh\oci_trader_key
    (same key as the trader VM, by convention).

.PARAMETER RemoteRoot
    Path of the repo on the backtester VM. Defaults to /opt/trading-agent.

.PARAMETER MaxComparisonLines
    How many lines of comparison.md to tail. Default 30.

.EXAMPLE
    # Once: stash the backtester host
    $env:BACKTESTER_VM_HOST = "80.225.197.125"

    # Anywhere, anytime:
    .\tools\battery_status_remote.ps1

.NOTES
    Mirrors the auth + path conventions of pull_battery_results.ps1 so
    the operator only has to learn one SSH-key / host pattern.
    Does NOT pull artefacts to the laptop -- use pull_battery_results.ps1
    when you want to inspect comparison.md or per-variant JSONs locally.
#>

[CmdletBinding()]
param(
    [string] $VmHost = $env:BACKTESTER_VM_HOST,
    [string] $SshUser = $(if ($env:BACKTESTER_SSH_USER) { $env:BACKTESTER_SSH_USER } else { 'opc' }),
    [string] $SshKey = "$HOME\.ssh\oci_trader_key",
    [string] $RemoteRoot = '/opt/trading-agent',
    [int]    $MaxComparisonLines = 30
)

$ErrorActionPreference = 'Stop'

if (-not $VmHost) {
    Write-Host ""
    Write-Host "ERROR: backtester VM host not set." -ForegroundColor Red
    Write-Host "  Pass -VmHost <ip> or set `$env:BACKTESTER_VM_HOST." -ForegroundColor Red
    Write-Host ""
    exit 1
}
if (-not (Test-Path $SshKey)) {
    Write-Host ""
    Write-Host "ERROR: SSH key not found at $SshKey." -ForegroundColor Red
    Write-Host "  Pass -SshKey <path> if your key lives elsewhere." -ForegroundColor Red
    Write-Host ""
    exit 1
}

$startedAt = Get-Date
Write-Host ("=" * 60)
Write-Host " battery_status_remote.ps1"
Write-Host " VM       : $SshUser@$VmHost"
Write-Host " Remote   : $RemoteRoot"
Write-Host " Key      : $SshKey"
Write-Host (" When     : {0:yyyy-MM-dd HH:mm:ss zzz}" -f $startedAt)
Write-Host ("=" * 60)

# Build the remote bundle. One SSH round-trip; output is parsed locally.
# Using ASCII-only delimiters so PowerShell's Out-String round-trip
# doesn't mangle anything.
#
# Each section is wrapped in `===SECTION:<name>===` / `===END:<name>===`
# so we can split client-side without fragile regex.
$remoteScript = @"
set +e
cd '$RemoteRoot' 2>/dev/null || { echo "[FATAL] remote root not found: $RemoteRoot"; exit 7; }

emit() { echo "===SECTION:`$1==="; }
end()  { echo "===END:`$1==="; }

emit scheduler
systemctl is-active battery-scheduler 2>/dev/null
echo "---"
sched_since=`$(systemctl show battery-scheduler --property=ActiveEnterTimestamp --value 2>/dev/null)
echo "active_since: `$sched_since"
sched_pid=`$(systemctl show battery-scheduler --property=MainPID --value 2>/dev/null)
echo "main_pid: `$sched_pid"
sudo journalctl -u battery-scheduler --no-pager -n 3 2>/dev/null | tail -3
end scheduler

emit container
sudo docker ps --filter name=battery_ --format '{{.Names}}|{{.Status}}|{{.RunningFor}}|{{.Image}}' 2>/dev/null
echo "---STATS---"
sudo docker stats --no-stream --filter name=battery_ --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>/dev/null
end container

emit run
LATEST=`$(ls -1 logs/backtests/ 2>/dev/null | sort -r | head -1)
echo "run_id: `$LATEST"
if [ -n "`$LATEST" ]; then
    RDIR="logs/backtests/`$LATEST"
    started=`$(stat -c '%y' "`$RDIR" 2>/dev/null | head -1)
    echo "started: `$started"
    last=`$(stat -c '%y' "`$RDIR/comparison.md" 2>/dev/null | head -1)
    echo "comparison_last_modified: `$last"
    res_count=`$(sudo ls "`$RDIR/results/" 2>/dev/null | grep -c '\.json$')
    fail_count=`$(sudo ls "`$RDIR/results/" 2>/dev/null | grep -c '\.failure\.txt$')
    echo "results_done: `$res_count"
    echo "results_failed: `$fail_count"
    pkl_size=`$(sudo stat -c '%s' "`$RDIR/market_data.pkl" 2>/dev/null)
    echo "market_data_pkl_bytes: `$pkl_size"
fi
end run

emit workers
LATEST=`$(ls -1 logs/backtests/ 2>/dev/null | sort -r | head -1)
if [ -n "`$LATEST" ]; then
    RDIR="logs/backtests/`$LATEST"
    if [ -d "`$RDIR/workers" ]; then
        for f in `$(sudo ls "`$RDIR/workers" 2>/dev/null); do
            sz=`$(sudo stat -c '%s' "`$RDIR/workers/`$f" 2>/dev/null)
            mt=`$(sudo stat -c '%Y' "`$RDIR/workers/`$f" 2>/dev/null)
            last=`$(sudo tail -1 "`$RDIR/workers/`$f" 2>/dev/null | cut -c1-220)
            echo "WORKER|`$f|`$sz|`$mt|`$last"
        done
    fi
fi
end workers

emit comparison
LATEST=`$(ls -1 logs/backtests/ 2>/dev/null | sort -r | head -1)
if [ -n "`$LATEST" ]; then
    sudo tail -$MaxComparisonLines "logs/backtests/`$LATEST/comparison.md" 2>/dev/null
    # comparison.md may not end with a trailing newline -- ensure one
    # so the next section marker doesn't get glued to the last line.
    echo
fi
end comparison

emit queue
if [ -f data/battery_queue.yaml ]; then
    grep -E '^\s*-\s*name:' data/battery_queue.yaml | sed 's/^\s*-\s*name:\s*//' | head -20
fi
echo "---STATE---"
if [ -f data/battery_queue_state.json ]; then
    sudo cat data/battery_queue_state.json
else
    echo "(no state file -- no queued job has launched yet)"
fi
end queue

emit disk
df -h /opt/trading-agent 2>/dev/null | tail -1
echo "---"
nproc
end disk
"@

try {
    # Encode the script as base64 and decode it on the VM. This sidesteps
    # every quoting quirk between PowerShell, ssh's command-arg parser,
    # and bash:
    #   * No need to escape `$`, `"`, `\`, backticks, or newlines.
    #   * Tested across Windows PowerShell 5.1 and PowerShell 7+.
    #   * The b64 blob travels as a single shell-safe word, so nothing
    #     in the heredoc can be re-interpreted on the way through.
    # Normalize CRLF -> LF: PowerShell here-strings written from Windows
    # files have CRLF endings, which when base64-decoded on bash leave a
    # `\r` on every line. bash on Oracle Linux 8 then mis-parses
    # commands like `set +e` because `+e\r` becomes a non-flag token.
    $normalized  = $remoteScript -replace "`r`n", "`n"
    $scriptBytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $scriptB64   = [Convert]::ToBase64String($scriptBytes)
    $remoteCmd   = "echo $scriptB64 | base64 -d | bash"

    $rawOutput = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 `
        -i $SshKey "$SshUser@$VmHost" $remoteCmd 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: ssh exit $LASTEXITCODE -- VM unreachable or remote script crashed." -ForegroundColor Red
        Write-Host "Raw output below:" -ForegroundColor Yellow
        $rawOutput | ForEach-Object { Write-Host "  $_" }
        exit $LASTEXITCODE
    }
} catch {
    Write-Host ""
    Write-Host "ERROR: ssh invocation failed: $_" -ForegroundColor Red
    exit 1
}

# Parse the bundled output by SECTION markers.
function Get-Section {
    param([string[]] $lines, [string] $name)
    $out = New-Object 'System.Collections.Generic.List[string]'
    $inside = $false
    foreach ($ln in $lines) {
        if ($ln -eq "===SECTION:$name===") { $inside = $true;  continue }
        if ($ln -eq "===END:$name===")     { $inside = $false; continue }
        if ($inside) { $out.Add($ln) }
    }
    return $out
}

$lines = $rawOutput -split "`r?`n"
$sched = Get-Section $lines 'scheduler'
$cont  = Get-Section $lines 'container'
$run   = Get-Section $lines 'run'
$work  = Get-Section $lines 'workers'
$cmp   = Get-Section $lines 'comparison'
$queue = Get-Section $lines 'queue'
$disk  = Get-Section $lines 'disk'

# ─────────────────────── render ───────────────────────
Write-Host ""
Write-Host "== SCHEDULER =====================================" -ForegroundColor Cyan
$schedActive = $sched | Select-Object -First 1
if ($schedActive -eq 'active') {
    Write-Host ("  state    : {0}" -f $schedActive) -ForegroundColor Green
} else {
    Write-Host ("  state    : {0}" -f $schedActive) -ForegroundColor Red
}
$sched | Where-Object { $_ -match '^active_since:|^main_pid:' } |
    ForEach-Object { Write-Host "  $_" }
Write-Host "  recent journal:" -ForegroundColor Gray
$sched | Where-Object { $_ -notmatch '^---$|^active_since:|^main_pid:' -and $_ -ne $schedActive -and $_ } |
    Select-Object -Last 3 |
    ForEach-Object { Write-Host "    $_" }

Write-Host ""
Write-Host "== ACTIVE BATTERY CONTAINER ======================" -ForegroundColor Cyan
$psLine = $cont | Where-Object { $_ -and $_ -notmatch '^---' -and $_ -notmatch '\|\s*\d+\.\d+%\|' } | Select-Object -First 1
if (-not $psLine) {
    Write-Host "  [NO BATTERY CONTAINER RUNNING]" -ForegroundColor Yellow
} else {
    $parts = $psLine -split '\|'
    if ($parts.Count -ge 4) {
        Write-Host "  name     : $($parts[0])"
        $statusColor = if ($parts[1] -match 'unhealthy') { 'Yellow' } else { 'Green' }
        Write-Host "  status   : $($parts[1])" -ForegroundColor $statusColor
        Write-Host "  uptime   : $($parts[2])"
        Write-Host "  image    : $($parts[3])"
    }
    $statsLine = $cont | Where-Object { $_ -and $_ -notmatch '^---' -and $_ -ne $psLine } | Select-Object -First 1
    if ($statsLine) {
        $sp = $statsLine -split '\|'
        if ($sp.Count -ge 4) {
            Write-Host "  cpu      : $($sp[1])"
            Write-Host "  mem      : $($sp[2]) ($($sp[3]))"
        }
    }
}

Write-Host ""
Write-Host "== LATEST RUN ====================================" -ForegroundColor Cyan
$runMap = @{}
foreach ($ln in $run) {
    if ($ln -match '^([a-z_]+)\s*:\s*(.*)$') {
        $runMap[$Matches[1]] = $Matches[2]
    }
}
if (-not $runMap.run_id) {
    Write-Host "  [NO RUNS YET]" -ForegroundColor Yellow
} else {
    Write-Host "  run_id              : $($runMap.run_id)"
    if ($runMap.started) { Write-Host "  started             : $($runMap.started)" }
    if ($runMap.comparison_last_modified) {
        Write-Host "  comparison updated  : $($runMap.comparison_last_modified)"
    }
    $done   = if ($runMap.results_done)   { [int]$runMap.results_done }   else { 0 }
    $failed = if ($runMap.results_failed) { [int]$runMap.results_failed } else { 0 }
    $doneColor = if ($done -gt 0) { 'Green' } else { 'Yellow' }
    Write-Host ("  variants done       : {0}{1}" -f $done,
        $(if ($failed -gt 0) { "  (failed: $failed)" } else { '' })) -ForegroundColor $doneColor

    # ETA estimate based on results-per-hour.
    if ($runMap.started) {
        try {
            $startUtc = [DateTime]::Parse($runMap.started)
            $elapsedH = (New-TimeSpan -Start $startUtc -End (Get-Date)).TotalHours
            if ($elapsedH -gt 0) {
                if ($done -gt 0) {
                    $perVariantH = $elapsedH / $done
                    Write-Host ("  per-variant (avg)   : {0:N1}h" -f $perVariantH)
                } else {
                    Write-Host ("  per-variant (lower) : >= {0:N1}h (no completions yet after {0:N1}h)" -f $elapsedH) -ForegroundColor Yellow
                }
                # The "Variants done: X / Y" line lives somewhere in the
                # comparison.md tail — scan all rendered lines, not just
                # the first.
                $totalVar = 0
                foreach ($cl in $cmp) {
                    if ($cl -match 'Variants done:\s*(\d+)\s*/\s*(\d+)') {
                        $totalVar = [int]$Matches[2]
                        break
                    }
                }
                if ($totalVar -gt 0) {
                    $remaining = $totalVar - $done
                    if ($remaining -gt 0 -and $done -gt 0) {
                        # Two workers concurrently => waves of 2.
                        $wavesLeft = [math]::Ceiling($remaining / 2.0)
                        $etaH = $wavesLeft * ($elapsedH / [math]::Ceiling([math]::Max($done,1) / 2.0))
                        Write-Host ("  ETA (this run)      : ~{0:N1}h remaining ({1} variants x workers=2)" -f $etaH, $remaining) -ForegroundColor Gray
                    } elseif ($done -eq 0) {
                        Write-Host ("  ETA (this run)      : unknown ({0} variants pending -- rerun once V1 lands)" -f $totalVar) -ForegroundColor Yellow
                    }
                }
            }
        } catch {
            # silent: best-effort ETA only
        }
    }
}

Write-Host ""
Write-Host "== ACTIVE WORKERS ================================" -ForegroundColor Cyan
$wRows = $work | Where-Object { $_ -match '^WORKER\|' }
if (-not $wRows) {
    Write-Host "  [NO WORKER LOGS]" -ForegroundColor Yellow
} else {
    foreach ($r in $wRows) {
        $p = $r -split '\|', 5
        # 0=WORKER 1=name 2=size_bytes 3=mtime_epoch 4=last_line
        $name  = $p[1]
        $sizeKB = [math]::Round([int64]$p[2] / 1024, 0)
        $mtime  = [DateTimeOffset]::FromUnixTimeSeconds([int64]$p[3]).ToLocalTime()
        $age    = (New-TimeSpan -Start $mtime.DateTime -End (Get-Date))
        $ageStr = if ($age.TotalMinutes -lt 60) {
            "{0:N0}m ago" -f $age.TotalMinutes
        } else {
            "{0:N1}h ago" -f $age.TotalHours
        }
        $last = if ($p.Count -ge 5) { $p[4] } else { '' }
        $color = if ($age.TotalMinutes -lt 5) { 'Green' } elseif ($age.TotalMinutes -lt 30) { 'Yellow' } else { 'Red' }
        Write-Host ("  [{0}]  {1,7} KB  last write: {2}" -f $name, $sizeKB, $ageStr) -ForegroundColor $color
        if ($last) { Write-Host "    last: $last" -ForegroundColor Gray }
    }
}

Write-Host ""
Write-Host "== QUEUE =========================================" -ForegroundColor Cyan
$queueLines = New-Object 'System.Collections.Generic.List[string]'
$stateLines = New-Object 'System.Collections.Generic.List[string]'
$inState = $false
foreach ($q in $queue) {
    if ($q -eq '---STATE---') { $inState = $true; continue }
    if ($inState) { $stateLines.Add($q) } else { $queueLines.Add($q) }
}
if ($queueLines.Count -eq 0) {
    Write-Host "  [NO QUEUE FILE FOUND]" -ForegroundColor Yellow
} else {
    Write-Host "  jobs in queue (in order):"
    $i = 1
    foreach ($jname in $queueLines | Where-Object { $_.Trim() }) {
        # Mark completed jobs based on state file.
        $stateBlob = ($stateLines -join "`n")
        $isDone = $stateBlob -match """$jname""\s*:\s*\{[^}]*""status""\s*:\s*""completed"""
        $marker = if ($isDone) { '✓' } else { ' ' }
        Write-Host ("    {0}  {1}. {2}" -f $marker, $i, $jname)
        $i++
    }
}
Write-Host ""
$stateRaw = ($stateLines -join "`n").Trim()
if ($stateRaw -match '\(no state file') {
    Write-Host "  state    : no queued job has launched yet (scheduler waiting on existing run)" -ForegroundColor Yellow
} elseif ($stateRaw) {
    Write-Host "  state    : present"
}

Write-Host ""
Write-Host "== COMPARISON.MD (last $MaxComparisonLines lines) ============" -ForegroundColor Cyan
if ($cmp.Count -eq 0) {
    Write-Host "  (no comparison.md yet)" -ForegroundColor Yellow
} else {
    $cmp | ForEach-Object { Write-Host "  $_" }
}

Write-Host ""
Write-Host "== HOST ==========================================" -ForegroundColor Cyan
$diskLine = ($disk | Where-Object { $_ -match '^/dev/' } | Select-Object -First 1)
if ($diskLine) { Write-Host "  disk     : $diskLine" }
$np = ($disk | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
if ($np) { Write-Host "  cpu cores: $np" }

Write-Host ""
Write-Host ("=" * 60)
Write-Host (" Done. (queried in {0:N1}s)" -f ((Get-Date) - $startedAt).TotalSeconds)
Write-Host ("=" * 60)
