# install_scheduled_task.ps1
# ─────────────────────────────────────────────────────────────────────────
# Layer-2 watchdog: registers a Windows Scheduled Task that launches
# tools\run_daemon_resilient.ps1 (the Layer-1 wrapper) and ensures it
# stays alive across:
#
#   • User logon (auto-starts when you log in)
#   • Scheduled time (daily 09:00 IST as backup trigger)
#   • Wake from sleep (the task itself can wake the computer)
#   • Wrapper crash (Scheduled Task auto-restarts up to 3x in 1-min
#     intervals if the wrapper script fails)
#
# It does NOT prevent Modern Standby on lid-close — that's a Power Plan
# setting we surface in the README. But it ensures the daemon is back
# up within ~1 minute of any sleep/wake transition.
#
# Usage:
#   Run from an elevated PowerShell (admin):
#     powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_scheduled_task.ps1
#
#   To uninstall:
#     Unregister-ScheduledTask -TaskName "TradingAgentDaemon" -Confirm:$false
# ─────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$TaskName = "TradingAgentDaemon"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WrapperScript = Join-Path $ProjectRoot "tools\run_daemon_resilient.ps1"

if (-not (Test-Path $WrapperScript)) {
    Write-Error "Wrapper script missing: $WrapperScript"
    exit 1
}

Write-Host "Installing scheduled task '$TaskName' for: $WrapperScript"

# Action: launch the wrapper in PowerShell, hidden window.
# `New-ScheduledTaskAction -Argument` expects a single string (not an
# array) — passing an array triggers a "Cannot process argument
# transformation on parameter 'Argument'" error. We build one string
# and quote the file path because the project path contains spaces
# ("OneDrive - AMDOCS") which would otherwise tokenise wrong.
$Argument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperScript`""
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $Argument `
    -WorkingDirectory $ProjectRoot

# Triggers
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Daily at 09:00 IST as belt-and-braces (in case user is already logged in
# but the task somehow stopped overnight).
$TriggerDaily = New-ScheduledTaskTrigger -Daily -At 9:00am

# Settings:
#   - StartWhenAvailable: if missed the trigger (e.g. machine off), start
#     as soon as available.
#   - RestartCount/RestartInterval: auto-restart on failure.
#   - DontStopIfGoingOnBatteries / AllowStartIfOnBatteries: don't be cute.
#   - WakeToRun: physically wake the laptop at the daily trigger time.
#   - ExecutionTimeLimit: 0 = run indefinitely.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# Run as the current user, not SYSTEM, so it has access to the user's
# venv, env vars, network, etc.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Build the task object
$Task = New-ScheduledTask -Action $Action -Trigger @($TriggerLogon, $TriggerDaily) -Settings $Settings -Principal $Principal

# Replace any prior version
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "  Removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null

Write-Host ""
Write-Host "[OK] Scheduled task '$TaskName' registered."
Write-Host ""
Write-Host "Verify with:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "Start it now (without rebooting/logging out) with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Watch the supervisor log:"
Write-Host "  Get-Content logs\daemon_supervisor.log -Wait -Tail 10"
Write-Host ""
Write-Host "Disable temporarily:"
Write-Host "  Disable-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Uninstall completely:"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
