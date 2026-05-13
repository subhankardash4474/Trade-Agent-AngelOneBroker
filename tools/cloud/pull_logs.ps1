<#
.SYNOPSIS
    Pull today's trader-pod artefacts from the cloud VM into the local repo
    so Cursor (and you) can audit them after market close.

.DESCRIPTION
    Since 2026-05-11 the trader daemon runs in a container on an OCI Mumbai VM.
    The four daily emails (Trade alerts, EOD Summary, Trade Post-Mortem, Profit
    Diagnostic) are summaries — the full audit checkpoints, signal CSVs, and
    daemon logs stay on the VM. This script SCPs the minimum set needed for
    the `trading-audit` Cursor skill to function and for you to inspect a
    trade in detail.

    This is a stop-gap until `docs/cloud_mvc_runbook.md` §6.2 (nightly rclone
    -> OCI Object Storage) lands.

.PARAMETER VmHost
    Hostname or IP of the cloud VM. Resolution order:
      1. -VmHost arg
      2. $env:TRADER_VM_HOST
      3. fail with an error

.PARAMETER Date
    Trading date in YYYY-MM-DD (IST). Defaults to today in IST.

.PARAMETER RemoteRoot
    Path of the repo on the VM. Defaults to /opt/trading-agent (matches the runbook).

.PARAMETER SshUser
    SSH user. Resolution order:
      1. -SshUser arg
      2. $env:TRADER_SSH_USER
      3. 'trader' (the unprivileged user oci_bootstrap.sh is supposed to create;
                   real-world OCI deployments on the default Ubuntu image use
                   'ubuntu' instead -- set the env var once and forget).

.PARAMETER SshKey
    Path to the SSH private key. Defaults to $HOME\.ssh\oci_trader_key.

.PARAMETER IncludeDb
    Also pull data/trading_agent.db (~MBs). Off by default — only needed when
    you want to re-derive P&L locally.

.PARAMETER DryRun
    Print the scp commands that would run without executing them.

.EXAMPLE
    # Once: stash the VM host so you don't repeat it
    $env:TRADER_VM_HOST = "130.61.42.111"

    # Daily ritual (after 16:05 IST, once the Profit Diagnostic email lands):
    .\tools\cloud\pull_logs.ps1

    # Pull a specific historical day
    .\tools\cloud\pull_logs.ps1 -Date 2026-05-12

    # Also grab the DB for offline P&L recomputation
    .\tools\cloud\pull_logs.ps1 -IncludeDb

.NOTES
    Requires OpenSSH client (bundled with Windows 10/11 by default; verify
    with `Get-Command ssh`). Auth uses key-based login set up per
    docs/cloud_mvc_runbook.md §2.2 / §3.3 — no passwords here.
#>

[CmdletBinding()]
param(
    [string]$VmHost,
    [string]$Date,
    [string]$RemoteRoot = "/opt/trading-agent",
    [string]$SshUser,
    [string]$SshKey,
    [switch]$IncludeDb,
    [switch]$DryRun
)

# Use Continue rather than Stop -- scp writes "No such file" to stderr for
# optional remote paths (e.g. logs/postmortem/<date>.md on zero-trade days),
# which Stop would treat as a terminating error and abort the whole batch.
# We still throw explicitly inside the helpers when we want to halt.
$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Resolve-VmHost {
    if ($VmHost) { return $VmHost }
    if ($env:TRADER_VM_HOST) { return $env:TRADER_VM_HOST }
    throw "VM host not set. Pass -VmHost <ip>, or set `$env:TRADER_VM_HOST."
}

function Resolve-SshUser {
    if ($SshUser) { return $SshUser }
    if ($env:TRADER_SSH_USER) { return $env:TRADER_SSH_USER }
    return "trader"
}

function Resolve-IstDate {
    if ($Date) {
        if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') {
            throw "-Date must be YYYY-MM-DD (got: '$Date')"
        }
        return $Date
    }
    $istNow = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
        [datetime]::UtcNow, "India Standard Time")
    return $istNow.ToString("yyyy-MM-dd")
}

function Resolve-SshKey {
    if ($SshKey) {
        if (-not (Test-Path -LiteralPath $SshKey)) {
            throw "SSH key not found: $SshKey"
        }
        return $SshKey
    }
    $default = Join-Path $HOME ".ssh\oci_trader_key"
    if (Test-Path -LiteralPath $default) { return $default }
    return $null
}

function Invoke-Scp {
    param(
        [Parameter(Mandatory)] [string]$RemotePath,
        [Parameter(Mandatory)] [string]$LocalPath,
        [Parameter(Mandatory)] [string]$Key,
        [Parameter(Mandatory)] [string]$User,
        [Parameter(Mandatory)] [string]$RemoteHost,
        [switch]$Recurse,
        [switch]$Optional
    )

    # For recursive directory copies scp puts the remote folder *inside*
    # the local target. If we passed LocalPath = "...\logs\audit\2026-05-12"
    # and that dir already existed, scp would double-nest into
    # "...\logs\audit\2026-05-12\2026-05-12\". Wipe + use parent dir to
    # guarantee a flat, idempotent layout.
    if ($Recurse) {
        if ((Test-Path -LiteralPath $LocalPath) -and -not $DryRun) {
            Remove-Item -Recurse -Force -LiteralPath $LocalPath
        }
        $localTarget = Split-Path -Parent $LocalPath
        if (-not (Test-Path -LiteralPath $localTarget)) {
            New-Item -ItemType Directory -Path $localTarget -Force | Out-Null
        }
    } else {
        $localTarget = $LocalPath
        $localParent = Split-Path -Parent $localTarget
        if (-not (Test-Path -LiteralPath $localParent)) {
            New-Item -ItemType Directory -Path $localParent -Force | Out-Null
        }
    }

    $remote = "${User}@${RemoteHost}:${RemotePath}"
    $scpArgs = @()
    if ($Key)     { $scpArgs += @("-i", $Key) }
    if ($Recurse) { $scpArgs += "-r" }
    # accept-new = auto-add unknown hosts to known_hosts, reject on mismatch.
    # Matches the convention already used in tools/cloud/deploy.sh and keeps
    # the first-run experience smooth without weakening verification later.
    $scpArgs += @(
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new"
    )
    $scpArgs += @($remote, $localTarget)

    $display = "scp $($scpArgs -join ' ')"
    if ($DryRun) {
        Write-Host "  [DRY] $display"
        return $true
    }

    Write-Host "  -> $RemotePath"
    & scp @scpArgs 2>&1 | ForEach-Object { Write-Host "      $_" }
    $ok = ($LASTEXITCODE -eq 0)
    if (-not $ok) {
        if ($Optional) {
            Write-Host "      (optional - skipping)" -ForegroundColor DarkYellow
            return $false
        }
        Write-Host "      [FAIL] scp exit code $LASTEXITCODE" -ForegroundColor Red
    }
    return $ok
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$resolvedHost = Resolve-VmHost
$resolvedDate = Resolve-IstDate
$resolvedKey  = Resolve-SshKey
$resolvedUser = Resolve-SshUser

Write-Host "============================================================"
Write-Host " pull_logs.ps1"
Write-Host " VM       : $resolvedUser@$resolvedHost"
Write-Host " Date     : $resolvedDate (IST)"
Write-Host " Remote   : $RemoteRoot"
Write-Host " Local    : $RepoRoot"
Write-Host " Key      : $(if ($resolvedKey) { $resolvedKey } else { '(system ssh-agent / default key)' })"
if ($IncludeDb) { Write-Host " IncludeDb: yes" }
if ($DryRun)    { Write-Host " DryRun   : yes" }
Write-Host "============================================================"

# Items to fetch. Order is intentional — audit checkpoints first because
# that's what the `trading-audit` skill reads.
$items = @(
    @{
        Desc      = "Audit checkpoints"
        Remote    = "$RemoteRoot/logs/audit/$resolvedDate"
        Local     = Join-Path $RepoRoot "logs\audit\$resolvedDate"
        Recurse   = $true
        Optional  = $false
    },
    @{
        Desc      = "Daemon supervisor log"
        Remote    = "$RemoteRoot/logs/daemon_$resolvedDate.log"
        Local     = Join-Path $RepoRoot "logs\daemon_$resolvedDate.log"
        Recurse   = $false
        Optional  = $true     # daemon log only exists if daemon was (re)started today
    },
    @{
        Desc      = "Trading agent log (verbose)"
        Remote    = "$RemoteRoot/logs/trading_agent_$resolvedDate.log"
        Local     = Join-Path $RepoRoot "logs\trading_agent_$resolvedDate.log"
        Recurse   = $false
        Optional  = $false
    },
    @{
        Desc      = "Post-mortem markdown"
        Remote    = "$RemoteRoot/logs/postmortem/$resolvedDate.md"
        Local     = Join-Path $RepoRoot "logs\postmortem\$resolvedDate.md"
        Recurse   = $false
        Optional  = $true     # absent on zero-trade days
    },
    @{
        Desc      = "EOD profit diagnostic"
        Remote    = "$RemoteRoot/logs/diagnostics/eod_$resolvedDate.md"
        Local     = Join-Path $RepoRoot "logs\diagnostics\eod_$resolvedDate.md"
        Recurse   = $false
        Optional  = $true     # absent if <10 trades in last 7d
    },
    @{
        Desc      = "Signal audit CSV"
        Remote    = "$RemoteRoot/logs/signal_audit_$resolvedDate.csv"
        Local     = Join-Path $RepoRoot "logs\signal_audit_$resolvedDate.csv"
        Recurse   = $false
        Optional  = $false
    },
    @{
        Desc      = "Trades CSV (append-only)"
        Remote    = "$RemoteRoot/logs/trades.csv"
        Local     = Join-Path $RepoRoot "logs\trades.csv"
        Recurse   = $false
        Optional  = $false
    },
    @{
        Desc      = "Health snapshot"
        Remote    = "$RemoteRoot/logs/health.json"
        Local     = Join-Path $RepoRoot "logs\health.json"
        Recurse   = $false
        Optional  = $false
    },
    @{
        Desc      = "Live e2e stage logs (auth/AMO/round-trip)"
        Remote    = "$RemoteRoot/logs/live_e2e"
        Local     = Join-Path $RepoRoot "logs\live_e2e"
        Recurse   = $true
        Optional  = $true     # absent until first e2e test runs
    }
)

if ($IncludeDb) {
    $items += @{
        Desc     = "SQLite DB (full)"
        Remote   = "$RemoteRoot/data/trading_agent.db"
        Local    = Join-Path $RepoRoot "data\trading_agent.db"
        Recurse  = $false
        Optional = $false
    }
}

$ok = 0
$skipped = 0
$failed = 0
foreach ($it in $items) {
    Write-Host ""
    Write-Host "[$($it.Desc)]"
    $result = Invoke-Scp -RemotePath $it.Remote -LocalPath $it.Local `
                        -Key $resolvedKey -User $resolvedUser -RemoteHost $resolvedHost `
                        -Recurse:$it.Recurse -Optional:$it.Optional
    if ($result)            { $ok++ }
    elseif ($it.Optional)   { $skipped++ }
    else                    { $failed++ }
}

Write-Host ""
Write-Host "============================================================"
Write-Host " Done.  ok=$ok  skipped=$skipped  failed=$failed"
Write-Host "============================================================"

if ($failed -gt 0) {
    Write-Host "Some required items failed. Common causes:"     -ForegroundColor Red
    Write-Host "  - Wrong VM host / IP"                          -ForegroundColor Red
    Write-Host "  - SSH key missing or not added to ssh-agent"   -ForegroundColor Red
    Write-Host "  - Daemon hasn't written anything yet for $resolvedDate" -ForegroundColor Red
    Write-Host "Re-run with -DryRun to inspect the exact scp commands."   -ForegroundColor Red
    exit 1
}
exit 0
