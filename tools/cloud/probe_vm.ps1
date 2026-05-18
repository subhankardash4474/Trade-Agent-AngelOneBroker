<#
.SYNOPSIS
    Probe a freshly-provisioned OCI VM for which SSH user the operator
    set up (opc / ubuntu / ec2-user / root), then optionally dump the OS
    + disk + RAM facts the operator usually wants before running the
    backtester bootstrap.

.DESCRIPTION
    Useful when the VM was provisioned by an automated script and the
    operator doesn't remember whether the image was Oracle Linux
    (default user: opc) or Ubuntu (default user: ubuntu) or Amazon
    Linux 2023 (default user: ec2-user).

    The probe tries each candidate user in sequence with key auth, then
    on the first successful login runs a quick fact-collection sequence
    (uname, /etc/os-release, df -h /, free -h, nproc, docker version if
    present). Output is human-readable; no environment side-effects.

    Use this BEFORE bootstrap_backtester.sh so the operator can confirm:
      a) ssh actually works
      b) which user to use as the SSH_USER env / arg
      c) the VM is the size they think it is

.PARAMETER VmHost
    Public IP or hostname of the new VM.

.PARAMETER SshKey
    Private key path. Defaults to $HOME\.ssh\oci_trader_key.

.PARAMETER Users
    Override the candidate user list. Default: opc, ubuntu, ec2-user.

.EXAMPLE
    .\tools\cloud\probe_vm.ps1 -VmHost 132.45.67.89

.EXAMPLE
    # Custom key + extra user candidate
    .\tools\cloud\probe_vm.ps1 -VmHost 132.45.67.89 `
        -SshKey C:\keys\backtester.key `
        -Users @('opc','ubuntu','admin')
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$VmHost,
    [string]$SshKey,
    [string[]]$Users = @('opc','ubuntu','ec2-user')
)

$ErrorActionPreference = "Continue"

# Resolve SSH key. Default to the same key the trader VM uses so the
# operator gets a single pattern across hosts.
if (-not $SshKey) {
    $default = Join-Path $HOME ".ssh\oci_trader_key"
    if (Test-Path -LiteralPath $default) { $SshKey = $default }
}
if (-not (Test-Path -LiteralPath $SshKey)) {
    Write-Host "[probe_vm][FATAL] SSH key not found: $SshKey" -ForegroundColor Red
    Write-Host "  Pass -SshKey <path> or place the key at \$HOME\.ssh\oci_trader_key"
    exit 2
}

$sshOpts = @(
    "-i", $SshKey,
    "-o", "ConnectTimeout=8",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "PasswordAuthentication=no"
)

Write-Host "============================================================"
Write-Host " probe_vm.ps1"
Write-Host " VM      : $VmHost"
Write-Host " Key     : $SshKey"
Write-Host " Try     : $($Users -join ', ')"
Write-Host "============================================================"

$liveUser = $null
foreach ($u in $Users) {
    Write-Host ""
    Write-Host "[probe] trying ${u}@${VmHost} ..."
    # `whoami` is the cheapest possible authenticated check. If it
    # comes back with the user we tried, we have a working channel.
    $out = & ssh @sshOpts "${u}@${VmHost}" "whoami" 2>&1
    if ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq $u) {
        Write-Host "  -> OK as '$u'" -ForegroundColor Green
        $liveUser = $u
        break
    } else {
        Write-Host "  -> denied / no route (exit=$LASTEXITCODE)" -ForegroundColor DarkYellow
    }
}

if (-not $liveUser) {
    Write-Host ""
    Write-Host "[probe][FAIL] None of [$($Users -join ', ')] could log in." -ForegroundColor Red
    Write-Host "  Common causes:" -ForegroundColor Red
    Write-Host "    * Public key not in authorized_keys (OCI 'metadata > ssh_authorized_keys' at create time)"
    Write-Host "    * Security list / NSG blocks port 22 from your IP"
    Write-Host "    * Wrong VM IP (Reserved vs Ephemeral can change after a reboot)"
    Write-Host "    * Key file permission wrong on this Windows machine"
    Write-Host "  Try manually:  ssh -i $SshKey -v <user>@$VmHost"
    exit 3
}

Write-Host ""
Write-Host "============================================================"
Write-Host " Working SSH user: $liveUser" -ForegroundColor Green
Write-Host "============================================================"

# Run the fact pack in a single SSH session so we don't pay the
# connect-handshake cost 5 times.
$factScript = @'
echo "----- os -----"
uname -srm
cat /etc/os-release 2>/dev/null | grep -E "^(PRETTY_NAME|VERSION)=" | head -3
echo ""
echo "----- cpu / ram -----"
echo "cores: $(nproc)"
free -h | head -3
echo ""
echo "----- disk (root fs) -----"
df -h / | tail -1
echo ""
echo "----- swap -----"
swapon --show 2>/dev/null || echo "(no swap)"
echo ""
echo "----- docker -----"
if command -v docker >/dev/null 2>&1; then
    docker --version 2>/dev/null || echo "docker installed but not in PATH for this user"
    if sudo -n docker info >/dev/null 2>&1; then
        echo "sudo docker: usable without password"
    else
        echo "sudo docker: NOT passwordless (bootstrap will prompt or auto-fix)"
    fi
else
    echo "(docker NOT installed -- bootstrap will install it)"
fi
echo ""
echo "----- git -----"
command -v git >/dev/null 2>&1 && git --version || echo "(git NOT installed -- bootstrap will install it)"
echo ""
echo "----- public ip (self-reported) -----"
curl -fsSL --max-time 5 https://checkip.amazonaws.com 2>/dev/null || echo "(unable to reach checkip)"
'@

& ssh @sshOpts "${liveUser}@${VmHost}" "bash -lc '$factScript'"

Write-Host ""
Write-Host "============================================================"
Write-Host " Next step: run bootstrap with the user we just confirmed."
Write-Host ""
Write-Host "   # Set the env so launch_battery.sh / pull scripts know the host:"
Write-Host "   `$env:BACKTESTER_VM_HOST = '$VmHost'"
Write-Host "   `$env:BACKTESTER_SSH_USER = '$liveUser'"
Write-Host ""
Write-Host "   # From a bash shell (Git Bash works):"
Write-Host "   SSH_USER=$liveUser bash tools/cloud/bootstrap_backtester.sh \\"
Write-Host "       $VmHost \\"
Write-Host "       https://github.com/subhankardash4474/Trade-Agent-AngelOneBroker.git \\"
Write-Host "       freeze-v2.1"
Write-Host "============================================================"
