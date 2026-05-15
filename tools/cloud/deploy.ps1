<#
.SYNOPSIS
  Cloud deploy -- push latest main to the trader VM and restart container.

.DESCRIPTION
  PowerShell port of tools/cloud/deploy.sh for Windows hosts where bash is
  unavailable or broken (WSL issues, etc.). Performs the same workflow:

    1. Refuse if local working tree is dirty.
    2. Warn (and prompt) if local HEAD != origin/<branch>.
    3. SSH into the VM, fetch + reset --hard origin/<branch>.
    4. docker compose build --pull trader.
    5. docker compose up -d trader.
    6. Sleep, then show status + recent logs + health.json.

  Defaults assume the OCI Mumbai E2.1.Micro setup we ship with: SSH user
  `ubuntu` (only user with installed keys), repo at /opt/trading-agent owned
  by `trader`, `sudo -u trader` for git ops, `sudo docker` for container ops.

.PARAMETER VmHost
  IP or hostname of the trader VM. Required.

.PARAMETER Branch
  Git branch to deploy. Default: main.

.PARAMETER SshUser
  SSH user. Default: ubuntu (override with $env:SSH_USER).

.PARAMETER SshKey
  Path to SSH private key. Default: $HOME\.ssh\oci_trader_key.

.PARAMETER TraderHome
  Repo path on the VM. Default: /opt/trading-agent.

.PARAMETER RunAs
  Linux user that owns the repo on the VM. Default: trader. Used as
  `sudo -u <RunAs> git ...` so the working tree stays owned by trader.

.PARAMETER HealthSleepSec
  Seconds to wait after container restart before pulling health.json.
  Default: 15.

.PARAMETER SkipLocalChecks
  Skip the dirty-tree / unpushed-commits guard. Use with care.

.PARAMETER DryRun
  Print the remote script that WOULD run, then exit. Does not SSH.

.EXAMPLE
  tools\cloud\deploy.ps1 80.225.251.79

.EXAMPLE
  tools\cloud\deploy.ps1 80.225.251.79 feature/circuit-band-clamp -DryRun
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$VmHost,

    [Parameter(Position=1)]
    [string]$Branch = "main",

    [string]$SshUser = $(if ($env:SSH_USER) { $env:SSH_USER } else { "ubuntu" }),
    [string]$SshKey = $(if ($env:SSH_KEY) { $env:SSH_KEY } else { "$HOME\.ssh\oci_trader_key" }),
    [string]$TraderHome = $(if ($env:TRADER_HOME) { $env:TRADER_HOME } else { "/opt/trading-agent" }),
    [string]$RunAs = $(if ($env:DEPLOY_RUN_AS) { $env:DEPLOY_RUN_AS } else { "trader" }),
    [int]$HealthSleepSec = 15,
    [switch]$SkipLocalChecks,
    [switch]$DryRun
)

# Note: deliberately NOT using ErrorActionPreference="Stop". PowerShell 5.1
# treats native-command stderr (e.g. `git fetch`'s progress output) as
# errors under Stop, which would abort the script on benign messages. We
# check $LASTEXITCODE explicitly after each native command instead.
$ErrorActionPreference = "Continue"

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " $Text" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

Write-Section "Cloud deploy"
Write-Host " Target:    $SshUser@$VmHost"
Write-Host " Branch:    $Branch"
Write-Host " Path:      $TraderHome"
Write-Host " Run as:    $RunAs"
Write-Host " SSH key:   $SshKey"
Write-Host " Started:   $(Get-Date -Format 'o')"

# Sanity: SSH key must exist.
if (-not (Test-Path -LiteralPath $SshKey)) {
    Write-Host ""
    Write-Host "[FAIL] SSH key not found: $SshKey" -ForegroundColor Red
    Write-Host "       Set -SshKey or `$env:SSH_KEY to the correct path."
    exit 4
}

# ──────────────────────────────────────────────────────────────────────
# Local safety checks (skipped with -SkipLocalChecks)
# ──────────────────────────────────────────────────────────────────────
if (-not $SkipLocalChecks) {
    Write-Host ""
    Write-Host "[local] checking working tree..." -ForegroundColor Yellow
    & git diff-index --quiet HEAD --
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] local working tree is dirty -- commit or stash before deploying" -ForegroundColor Red
        & git status --short
        exit 2
    }
    Write-Host "[local] working tree is clean."

    Write-Host ""
    Write-Host "[local] fetching origin/$Branch ..." -ForegroundColor Yellow
    # git writes progress to stderr -- redirect to stdout via cmd.exe so
    # PowerShell doesn't tag it as an error record.
    & cmd.exe /c "git fetch origin $Branch 2>&1" | ForEach-Object { "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] git fetch failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 5
    }

    $localHead = (& git rev-parse HEAD).Trim()
    $remoteHead = (& git rev-parse "origin/$Branch").Trim()
    if ($localHead -ne $remoteHead) {
        Write-Host ""
        Write-Host "[WARN] local HEAD differs from origin/$Branch" -ForegroundColor Yellow
        Write-Host "       local : $localHead"
        Write-Host "       remote: $remoteHead"
        Write-Host "       Tip: 'git push origin $Branch' first if you have unpushed commits."
        if (-not $DryRun) {
            $confirm = Read-Host "Continue anyway? (y/N)"
            if ($confirm -ne 'y' -and $confirm -ne 'Y') {
                Write-Host "Aborted." -ForegroundColor Red
                exit 3
            }
        }
    } else {
        Write-Host "[local] local HEAD == origin/$Branch ($localHead)"
    }
}

# ──────────────────────────────────────────────────────────────────────
# Build the remote bash script. Single quotes around shell-side vars to
# prevent the local PowerShell parser from interpolating them; double
# quotes around values where we DO want PowerShell to expand (Branch etc.).
# ──────────────────────────────────────────────────────────────────────
$remoteScript = @"
set -euo pipefail
echo '[remote] target dir = $TraderHome'
echo '[remote] git head (pre)  = '"`$(sudo -u $RunAs git -C '$TraderHome' rev-parse --short HEAD)"
echo '[remote] fetching origin/$Branch ...'
sudo -u $RunAs git -C '$TraderHome' fetch origin '$Branch'
echo '[remote] resetting to origin/$Branch ...'
sudo -u $RunAs git -C '$TraderHome' reset --hard 'origin/$Branch'
echo '[remote] git head (post) = '"`$(sudo -u $RunAs git -C '$TraderHome' rev-parse --short HEAD)"

echo ''
echo '[remote] rebuilding image (this may take 2-5 min on first build)...'
cd '$TraderHome' && sudo docker compose build --pull trader

echo ''
echo '[remote] restarting container...'
cd '$TraderHome' && sudo docker compose up -d trader

echo ''
echo "[remote] waiting ${HealthSleepSec}s for healthcheck..."
sleep $HealthSleepSec

echo ''
echo '[remote] container status:'
cd '$TraderHome' && sudo docker compose ps trader

echo ''
echo '[remote] last 30 log lines:'
cd '$TraderHome' && sudo docker compose logs --tail 30 trader

echo ''
echo '[remote] health snapshot:'
cd '$TraderHome' && sudo docker compose exec -T trader cat /app/logs/health.json 2>/dev/null || echo '[remote] (health.json not present yet -- container may still be booting)'
"@

if ($DryRun) {
    Write-Section "DRY RUN -- remote script that would be piped over SSH"
    Write-Host $remoteScript
    Write-Host ""
    Write-Host "[dry-run] no changes made." -ForegroundColor Yellow
    exit 0
}

# ──────────────────────────────────────────────────────────────────────
# Execute the remote sequence
# ──────────────────────────────────────────────────────────────────────
# We CANNOT just pipe $remoteScript over SSH stdin -- PowerShell encodes
# the pipe stream with CRLF line endings, which makes bash mangle every
# line (e.g. `set -euo pipefail\r` -> "invalid option name pipefail").
# Workaround: write the script to a local temp file with LF-only endings,
# scp it to /tmp on the VM, then execute it via a single ssh call.
# ──────────────────────────────────────────────────────────────────────
Write-Section "Remote sequence"

$localTemp = Join-Path $env:TEMP "trader_deploy_$([guid]::NewGuid().ToString('N').Substring(0,8)).sh"
$remoteTemp = "/tmp/_trader_deploy_$([guid]::NewGuid().ToString('N').Substring(0,8)).sh"

# Write LF-only.
[IO.File]::WriteAllText($localTemp, ($remoteScript -replace "`r`n", "`n"), [Text.UTF8Encoding]::new($false))
Write-Host "[local] wrote remote script to $localTemp ($(((Get-Item $localTemp).Length)) bytes, LF endings)"

$scpArgs = @(
    "-i", $SshKey,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-q",
    $localTemp,
    "${SshUser}@${VmHost}:${remoteTemp}"
)
Write-Host "[local] uploading script -> ${SshUser}@${VmHost}:${remoteTemp} ..."
& scp @scpArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] scp failed (exit $LASTEXITCODE)" -ForegroundColor Red
    Remove-Item $localTemp -ErrorAction SilentlyContinue
    exit 6
}

$sshArgs = @(
    "-i", $SshKey,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "${SshUser}@${VmHost}",
    "bash $remoteTemp; _rc=`$?; rm -f $remoteTemp; exit `$_rc"
)
Write-Host "[local] executing remote script ..."
Write-Host ""
& ssh @sshArgs
$exit = $LASTEXITCODE

Remove-Item $localTemp -ErrorAction SilentlyContinue

Write-Section "Deploy complete"
Write-Host " Finished:  $(Get-Date -Format 'o')"
if ($exit -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] remote script exited with code $exit" -ForegroundColor Red
    exit $exit
}
Write-Host " Status:    OK" -ForegroundColor Green
