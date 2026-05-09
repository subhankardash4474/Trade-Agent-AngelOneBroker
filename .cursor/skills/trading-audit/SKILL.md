---
name: trading-audit
description: >-
  Reads the latest pre-generated audit checkpoint for the Trading Agent and
  summarises what has changed since the previous one. Use whenever the user
  asks for an "audit", "re-audit", "checkpoint", "summary till now", "status",
  "any update", "anything new", "what happened", "how is the agent doing", or
  any similar request to know the current state of the running trading daemon
  without re-deriving everything from raw logs.
---

# Trading Agent Audit Checkpoint Reader

The trading daemon writes a comprehensive audit checkpoint to disk every hour
during market hours (09:00–16:00 IST). This skill teaches the agent how to
read and summarise those checkpoints quickly and consistently.

## Core idea

You do NOT manually re-audit the agent. The daemon has already done that work
on disk. Your job is to:

1. Read the most recent checkpoint file.
2. Compare it against the previous checkpoint (a delta block is included).
3. Surface only what needs the user's attention.
4. Keep the response short and structured.

## Where the checkpoints live

```
logs/audit/<YYYY-MM-DD>/checkpoint_<HHMM>.md   ← human/agent-readable
logs/audit/<YYYY-MM-DD>/checkpoint_<HHMM>.json ← structured fallback
```

Today's date is determined by IST. If the user is asking on a non-trading day
or before the first checkpoint, the most recent checkpoint may be from a
previous day — read whatever is most recent.

## Step-by-step procedure

### Step 1 — Find the latest checkpoint

Use the Glob tool. Sort descending. The newest filename is the latest.

```
Glob: logs/audit/*/checkpoint_*.md
```

If zero results: tell the user the daemon has not produced a checkpoint yet
and suggest running `python -m tools.audit_checkpoint` to generate one
immediately. Do NOT proceed.

### Step 2 — Read the latest checkpoint

Use the Read tool on the latest `.md` file. Read it whole — the file is small
(<10 KB).

### Step 3 — Apply the verdict gate

The checkpoint contains a top-line `**Verdict:**` field. Treat it as
authoritative and act accordingly:

| Verdict prefix | Meaning | Your reaction |
|---|---|---|
| `GREEN` | Daemon healthy, no errors, no anomalies | Brief 4-line summary. Do not pad. |
| `YELLOW` | High warning count or other soft issue | Highlight the specific issue, suggest if it needs action. |
| `RED` | Errors / traceback / DB round-trip failure / dead daemon | Lead with the problem. State the corrective action clearly. |

### Step 4 — Format the response

Use this exact template for every audit response (substitute values from the
checkpoint). Keep it tight: never exceed 25 lines unless the verdict is RED.

```
**[Verdict] — {HH:MM} checkpoint** ({date})

P&L:        Realised ₹{X} ({W}W/{L}L), unrealised ₹{Y} → day ₹{Z}
Positions:  {N} open ({sym1}, {sym2}, ...)
Trades:     {trades_in_window} closed in this window ({names+pnl one-liner})
Errors:     {error_count} errors, {warning_count} warnings (window only)
Pipeline:   {cycles} cycles, {acts} ensemble acts, regime={regime}, threshold={t}

[only if delta exists]
Δ vs prev:  ΔP&L ₹{...}, Δtrades {...}, Δpositions {...}, Δerrors {...}

[only if anything is non-trivial]
Notes:
- {flagged anomaly 1}
- {flagged anomaly 2}
```

### Step 5 — Add a recommendation only if needed

Do NOT recommend changes when the verdict is GREEN and nothing changed.
Only recommend action when:
- Verdict is YELLOW or RED.
- DB round-trip failed.
- A new symbol has been blacklisted today.
- Three or more consecutive losses today.
- Drawdown is approaching the 20% halt threshold.
- A position has been open >2 hours with negative unrealised P&L.

## Trigger phrases

This skill should auto-fire when the user asks any of these (or close
variants — be liberal):

- "audit", "re-audit", "re-aduit", "audit?", "audit please"
- "checkpoint", "latest checkpoint", "last checkpoint"
- "summary till now", "summary so far", "summary"
- "status", "status?", "current status"
- "any update", "any updates", "anything new", "anything to look at"
- "what happened", "how is it going", "how's the agent", "is everything ok"
- "scan logs", "any errors", "anything to fix"

If the user's request is broader (e.g. "do a thorough audit") AND something
in the checkpoint is RED/YELLOW, treat the checkpoint as a starting point and
go deeper using the live agent log + database, but always cite the checkpoint
first.

## Checkpoint freshness check

Each checkpoint embeds a `**Window:** HH:MM:SS → HH:MM:SS` line. If the
`until` time is more than 75 minutes older than the current IST time, warn
the user that the checkpoint is stale (daemon may have stopped) before
summarising. Verify the daemon is alive with:

```
Get-Process | Where-Object { $_.ProcessName -like 'python*' }
```

## What this skill must NOT do

- Do not re-derive metrics from raw logs when a checkpoint exists.
- Do not recompute P&L from the database directly — trust the checkpoint.
- Do not generate a new checkpoint unless the user explicitly asks or no
  checkpoint exists at all.
- Do not paraphrase the verdict — quote it.
- Do not exceed 25 lines on a GREEN response.

## Manual regeneration (rare)

If the user wants a fresh checkpoint right now (rather than the daemon's
hourly one), run:

```bash
python -m tools.audit_checkpoint
```

This writes a checkpoint dated to the current minute. Then read and
summarise it the same way.
