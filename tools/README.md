# Resilient Daemon Operations

Two-layer watchdog for the trading daemon. Designed to survive the failure
mode that killed the daemon on **2026-05-06** at **14:46 IST**: laptop lid
close → Modern Standby → kernel terminated the hung daemon.

## What's installed

| Layer | What | File |
|---|---|---|
| 1 | PowerShell wrapper that loops + relaunches `run_daemon.py` on any exit | `tools/run_daemon_resilient.ps1` |
| 2 | Windows Scheduled Task that ensures the wrapper is always running | `tools/install_scheduled_task.ps1` |

Together they survive:
- Daemon Python crash → Layer 1 relaunches in 30s
- Wrapper itself dies → Layer 2 restarts wrapper (3 attempts, 1-min intervals)
- User log off / log on → Layer 2 re-triggers at next logon
- Sleep / wake / lid close → wrapper inherits new session and continues
- Reboot → Layer 2 re-triggers at next logon (or 09:00 daily, whichever first)

## Install (one time, requires admin)

```powershell
# Open PowerShell as Administrator:
cd "C:\Users\subhanda\OneDrive - AMDOCS\Documents\Trading Agent"
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_scheduled_task.ps1
```

This registers a scheduled task named `TradingAgentDaemon`.

## Start the daemon now (without rebooting)

```powershell
Start-ScheduledTask -TaskName TradingAgentDaemon
```

## Verify it's running

```powershell
Get-ScheduledTask -TaskName TradingAgentDaemon | Get-ScheduledTaskInfo
Get-Content logs\daemon_supervisor.log -Wait -Tail 10
```

## Stop the daemon (clean shutdown)

Two options, depending on intent:

**Pause for a few minutes** (will auto-restart):
```powershell
Stop-ScheduledTask -TaskName TradingAgentDaemon
# A new instance will start at the next trigger (logon / 09:00 daily).
```

**Stop indefinitely** (won't auto-restart):
```powershell
# Create the emergency-stop sentinel file. The Layer-1 wrapper checks for
# this and exits cleanly without restart.
New-Item -Path EMERGENCY_STOP -ItemType File -Force
Stop-ScheduledTask -TaskName TradingAgentDaemon
```

To resume:
```powershell
Remove-Item EMERGENCY_STOP
Start-ScheduledTask -TaskName TradingAgentDaemon
```

## Uninstall

```powershell
Unregister-ScheduledTask -TaskName TradingAgentDaemon -Confirm:$false
```

---

## RECOMMENDED: prevent lid-close standby during market hours

The watchdog above will bring the daemon back within ~1 minute of waking
from sleep, but the cleaner fix is to not sleep at all during 09:15–15:30 IST.

### Option A — Power plan: lid-close = "Do nothing" (plugged in)

```powershell
# As admin:
powercfg -setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg -setactive SCHEME_CURRENT
```
Code 0 = "Do nothing", 1 = Sleep, 2 = Hibernate, 3 = Shut down. The above
sets the AC (plugged-in) lid-close action to "Do nothing".

### Option B — Schedule "Powercfg /requestsoverride" during market hours

Lighter touch: only override sleep during 09:15–15:30 IST. Out of scope
for this skill — file an issue if you want it.

### Option C — Just remember to keep the lid open during market hours

Combined with the watchdog, this is acceptable: the worst case becomes
"~60s gap on accidental lid close" instead of "44 minutes of dead daemon".

---

## Day-of operations checklist

- 09:00 IST: scheduled task auto-fires (or has been running since logon).
- Verify: `Get-Content logs\daemon_supervisor.log -Tail 5`
- Pre-market warm-up runs 09:10 → 09:15.
- First trading cycle 09:15.
- Audit checkpoints auto-write to `logs/audit/<date>/` every hour.
- 15:30: market close, daemon continues running for EOD wrap-up.
- 16:00: optional EOD summary auto-emitted.
- Daemon stays alive between sessions (handles overnight maintenance,
  weekend idle, etc.). It just doesn't trade outside market hours.

## Troubleshooting

**"Daemon won't start after install"** — Check the supervisor log for the
exact Python error:
```powershell
Get-Content logs\daemon_stderr_latest.log -Tail 30
Get-Content logs\daemon_supervisor.log -Tail 30
```

**"Task is running but Python is not"** — Layer-1 is in cooldown sleep
between launches. Wait 30s.

**"Flap detected — backing off 10 min"** — Layer-1 saw 10+ restarts in an
hour, indicating a repeatable startup failure (config bug, missing dep,
DB lock). Fix the underlying issue, then `Start-ScheduledTask` to resume.

**"Multiple instances running"** — `MultipleInstances IgnoreNew` is set, so
this should never happen. If it does, manually kill the duplicates and
report the bug.
