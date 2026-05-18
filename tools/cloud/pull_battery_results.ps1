<#
.SYNOPSIS
    Pull backtester battery results from the dedicated backtester VM to the
    local repo, so the comparison report / variant JSONs can be inspected
    in Cursor.

.DESCRIPTION
    Freeze-v2.1 companion. The backtester VM runs one-shot `docker run`
    invocations of `tools/run_battery.py` (see launch_battery.sh). Each
    run writes its artefacts under `logs/backtests/<run_id>/` on the
    backtester VM. This script SCPs that whole directory back to the laptop
    after the run finishes.

    Default behaviour without -RunId: pulls the most recent run's
    directory (alphabetic max under logs/backtests on the VM, which works
    because run_ids are ISO timestamps).

.PARAMETER VmHost
    Hostname or IP of the backtester VM. Resolution order:
      1. -VmHost arg
      2. $env:BACKTESTER_VM_HOST
      3. fail with an error

.PARAMETER RunId
    Specific run_id to pull (e.g. `battery_freeze_v21_20260518T120000`).
    Defaults to the most recent run on the VM.

.PARAMETER RemoteRoot
    Path of the repo on the backtester VM. Defaults to /opt/trading-agent.

.PARAMETER SshUser
    SSH user. Resolution order:
      1. -SshUser arg
      2. $env:BACKTESTER_SSH_USER
      3. 'opc' (OCI Oracle Linux default; Ubuntu uses 'ubuntu' --
                 set the env var once and forget).

.PARAMETER SshKey
    Path to the SSH private key. Defaults to $HOME\.ssh\oci_trader_key
    (same key as the trader VM by convention).

.PARAMETER DryRun
    Print the scp / ssh commands that would run without executing them.

.EXAMPLE
    # Once: stash the backtester IP
    $env:BACKTESTER_VM_HOST = "132.45.67.89"

    # Pull the latest run:
    .\tools\cloud\pull_battery_results.ps1

    # Pull a specific run:
    .\tools\cloud\pull_battery_results.ps1 -RunId battery_freeze_v21_20260518T120000

.NOTES
    Mirrors the auth + path conventions used by pull_logs.ps1 so the
    laptop only has one SSH-key / host pattern to learn.
#>

[CmdletBinding()]
param(
    [string]$VmHost,
    [string]$RunId,
    [string]$RemoteRoot = "/opt/trading-agent",
    [string]$SshUser,
    [string]$SshKey,
    [switch]$DryRun
)

# Match pull_logs.ps1: continue past optional misses; we throw explicitly
# from helpers when we want a hard stop.
$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Resolve-VmHost {
    if ($VmHost)                       { return $VmHost }
    if ($env:BACKTESTER_VM_HOST)       { return $env:BACKTESTER_VM_HOST }
    throw "Backtester VM host not set. Pass -VmHost <ip>, or set `$env:BACKTESTER_VM_HOST."
}

function Resolve-SshUser {
    if ($SshUser)                      { return $SshUser }
    if ($env:BACKTESTER_SSH_USER)      { return $env:BACKTESTER_SSH_USER }
    return "opc"
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

function Get-LatestRunId {
    param(
        [Parameter(Mandatory)] [string]$Key,
        [Parameter(Mandatory)] [string]$User,
        [Parameter(Mandatory)] [string]$RemoteHost
    )

    # `ls -1` is line-per-entry; `tail -1` gets the alphabetically last,
    # which equals the most-recent timestamp because run_ids are ISO-like
    # YYYYMMDDTHHMMSS strings.
    $cmd = @(
        "ssh",
        "-i", $Key,
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "$User@$RemoteHost",
        "ls -1 $RemoteRoot/logs/backtests/ 2>/dev/null | sort | tail -1"
    )
    if ($DryRun) {
        Write-Host "  [DRY] $($cmd -join ' ')"
        return "<dry-run-no-id>"
    }
    $out = & $cmd[0] $cmd[1..($cmd.Length-1)] 2>$null
    if (-not $out) {
        throw "No completed runs found at ${RemoteHost}:${RemoteRoot}/logs/backtests/"
    }
    return $out.Trim()
}

function Invoke-Scp {
    param(
        [Parameter(Mandatory)] [string]$RemotePath,
        [Parameter(Mandatory)] [string]$LocalPath,
        [Parameter(Mandatory)] [string]$Key,
        [Parameter(Mandatory)] [string]$User,
        [Parameter(Mandatory)] [string]$RemoteHost,
        [switch]$Recurse
    )

    # Mirror pull_logs.ps1: for recursive copies, target the PARENT so
    # scp doesn't double-nest when LocalPath already exists.
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
        Write-Host "      [FAIL] scp exit code $LASTEXITCODE" -ForegroundColor Red
    }
    return $ok
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$resolvedHost = Resolve-VmHost
$resolvedKey  = Resolve-SshKey
$resolvedUser = Resolve-SshUser

if (-not $RunId) {
    Write-Host "No -RunId given; querying VM for the most recent run..."
    $RunId = Get-LatestRunId -Key $resolvedKey -User $resolvedUser -RemoteHost $resolvedHost
    Write-Host "  Latest run on VM: $RunId"
}

Write-Host "============================================================"
Write-Host " pull_battery_results.ps1"
Write-Host " VM       : $resolvedUser@$resolvedHost"
Write-Host " Run ID   : $RunId"
Write-Host " Remote   : $RemoteRoot"
Write-Host " Local    : $RepoRoot"
Write-Host " Key      : $(if ($resolvedKey) { $resolvedKey } else { '(system ssh-agent / default key)' })"
if ($DryRun)    { Write-Host " DryRun   : yes" }
Write-Host "============================================================"

$items = @(
    @{
        Desc      = "Full run directory"
        Remote    = "$RemoteRoot/logs/backtests/$RunId"
        Local     = Join-Path $RepoRoot "logs\backtests\$RunId"
        Recurse   = $true
    }
)

$ok = 0; $failed = 0
foreach ($it in $items) {
    Write-Host ""
    Write-Host "[$($it.Desc)]"
    $result = Invoke-Scp -RemotePath $it.Remote -LocalPath $it.Local `
                        -Key $resolvedKey -User $resolvedUser -RemoteHost $resolvedHost `
                        -Recurse:$it.Recurse
    if ($result) { $ok++ } else { $failed++ }
}

Write-Host ""
Write-Host "============================================================"
Write-Host " Done.  ok=$ok  failed=$failed"
if ($ok -gt 0) {
    Write-Host "   Local artefacts: logs\backtests\$RunId"
    Write-Host "   Open comparison.md first; per-variant JSONs are next to it."
}
Write-Host "============================================================"

if ($failed -gt 0) { exit 1 }
exit 0
