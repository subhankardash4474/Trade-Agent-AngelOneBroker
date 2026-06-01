---
name: eod-pipeline
description: >-
  End-of-day orchestration skill. Pulls fresh logs from the cloud VM via
  tools/cloud/pull_logs.ps1, then runs trading-audit, reconcile-positions,
  trade-postmortem (batch), and brutal-review in sequence, conditionally
  invokes postmortem-writer and code-bug-review when an incident or
  suspected bug is surfaced, and writes a single EOD dossier at
  docs/eod/eod_report_<date>.md that indexes every artefact produced.
  Use when the user says "EOD pipeline", "EOD deep dive", "do the EOD
  ritual", "pull and review", "full EOD review", "cloud sync and review",
  "run end of day analysis", or any equivalent "do the whole post-close
  pass" intent.
---

# EOD Pipeline — pull, analyse, dossier

## Why this skill exists

The trader daemon runs on an OCI Mumbai VM. After market close (16:05
IST onward) you need to: pull fresh artefacts from the VM, run the
analytical skills against the fresh data, decide whether anything
needs an incident postmortem or code review, and leave behind a
single dossier doc that links to everything. Doing this manually means
remembering 7+ skill invocations in the right order. This skill is the
ritual.

## When this skill fires

- "EOD pipeline", "EOD deep dive", "EOD ritual", "EOD review"
- "pull and review", "pull logs and review", "full EOD review"
- "cloud sync and review", "run end of day analysis"
- "do the whole post-close pass"
- "nightly review"

Do **not** fire on:
- "audit" / "status" alone → `trading-audit` (fast, doesn't pull)
- "brutal review" alone → `brutal-review` (uses whatever is on disk)
- "reconcile" alone → `reconcile-positions`

This skill is for the *whole* ritual. The individual skills remain
usable independently for everything else.

## Pipeline overview

```
Phase 0  ─ Confirm with user (pull hits cloud)
Phase 1  ─ Pull artefacts via tools/cloud/pull_logs.ps1
Phase 2  ─ Print pull summary
Phase 3  ─ trading-audit            (quick context from fresh checkpoint)
Phase 4  ─ reconcile-positions      (catches divergence early)
Phase 5  ─ trade-postmortem         (batch, every closed trade for the day)
Phase 6  ─ brutal-review            (critical strategy/P&L view)
Phase 7  ─ postmortem-writer        (CONDITIONAL — only if 3/4/5/6 surfaced an incident)
Phase 8  ─ code-bug-review          (CONDITIONAL — only if 6 flagged code-side issues)
Phase 9  ─ daily-log                (append journal entry summarising)
Phase 10 ─ Write EOD dossier at docs/eod/eod_report_<date>.md
```

Phases 0–6, 9, 10 are unconditional. Phases 7 and 8 only fire when
their preconditions are met (defined below).

## Phase 0 — confirm with user (always)

The pull SCPs from the cloud VM. It is fast but not free: it hits the
network, may prompt for SSH credentials, and may be a poor idea if
you're on a flaky connection or the VM is mid-deploy. Always print
the plan and stop for explicit approval:

```
**EOD pipeline — proposed plan for <YYYY-MM-DD> (IST)**

1. Pull logs from cloud VM via tools/cloud/pull_logs.ps1
   (overwrites local copies for today)
2. trading-audit → fast status check from fresh checkpoint
3. reconcile-positions → DB vs broker vs trades.csv
4. trade-postmortem --range <date> <date> → all closed trades today
5. brutal-review → expert critical view, persists to docs/reviews/
6. (conditional) postmortem-writer if incident surfaced
7. (conditional) code-bug-review if bug surfaced
8. daily-log → append a summary entry to engineering journal
9. EOD dossier → docs/eod/eod_report_<date>.md (indexes all artefacts)

Pull options:
- IncludeDb? (default: no — flip to yes if you want offline P&L
  recomputation; pulls trading_agent.db, MBs)
- Date? (default: today IST = <YYYY-MM-DD>)

Reply with one of:
- "go"               → run with defaults
- "go --include-db"  → run, also pull DB
- "go --date 2026-05-28"  → run for a historical date
- "skip pull"        → run phases 3+ on existing local logs
- "cancel"
```

Do **not** start without an explicit response. "Run EOD" alone is
sufficient as a trigger; the prompt for options is still mandatory.

## Phase 1 — pull artefacts

After approval, invoke the pull script:

```powershell
# Default
.\tools\cloud\pull_logs.ps1

# With DB
.\tools\cloud\pull_logs.ps1 -IncludeDb

# Historical date
.\tools\cloud\pull_logs.ps1 -Date <YYYY-MM-DD>

# Both
.\tools\cloud\pull_logs.ps1 -IncludeDb -Date <YYYY-MM-DD>
```

The script pulls (per its source):

| Artefact | Required | Local path |
|---|---|---|
| Audit checkpoints (folder) | yes | `logs/audit/<date>/` |
| Daemon supervisor log | optional | `logs/daemon_<date>.log` |
| Trading agent verbose log | yes | `logs/trading_agent_<date>.log` |
| Post-mortem markdown | optional | `logs/postmortem/<date>.md` |
| EOD profit diagnostic | optional | `logs/diagnostics/eod_<date>.md` |
| Signal audit CSV | yes | `logs/signal_audit_<date>.csv` |
| Trades CSV | yes | `logs/trades.csv` |
| Health snapshot | yes | `logs/health.json` |
| Live e2e logs (folder) | optional | `logs/live_e2e/` |
| SQLite DB | optional (`-IncludeDb`) | `data/trading_agent.db` |

Script exit code:
- `0` — required items pulled successfully (optionals may be missing)
- `1` — at least one required item failed; ABORT the rest of the pipeline

If exit `1`:
- Print the failure to the user.
- Suggest re-running with `-DryRun` (the script's own debug mode).
- Offer to proceed with "skip pull" mode using stale local logs (clearly
  flagged as stale in the dossier).
- Do not silently continue.

## Phase 2 — pull summary

Print a compact summary the user can scan:

```
Pull complete (exit 0)
  ok      : N
  skipped : N (optional artefacts not present on VM)
  failed  : N (if non-zero, ABORT)

Fetched:
  - logs/audit/<date>/  (M checkpoint files)
  - logs/trading_agent_<date>.log  (S MB)
  - logs/signal_audit_<date>.csv  (R rows)
  - ... etc.

Stale (not fetched this run):
  - ... etc.
```

## Phase 3 — `trading-audit`

Read `logs/audit/<date>/checkpoint_<latest>.md` and run the
trading-audit skill in full. Capture its verdict (GREEN/YELLOW/RED)
and the bottom-line numbers. These feed into the dossier and into the
conditional triggers below.

If verdict is **RED**, immediately surface that fact at the top of
the user-facing pipeline progress, but DO NOT abort — continue to
phase 4. The conditional postmortem-writer phase will pick it up.

## Phase 4 — `reconcile-positions`

Run reconcile-positions for the same date. The skill itself decides
whether to file a divergence report based on the tool's exit code.

Capture:
- Tool exit code (0 clean / 1 divergence / 77 cannot run)
- Whether a divergence report was filed at
  `docs/diagnoses/reconcile_<date>_<HHMM>.md`
- DB snapshot path (if divergence)

If exit `1` (divergence): the conditional postmortem-writer phase MUST
fire (P0 by definition).

## Phase 5 — `trade-postmortem` (batch)

Run trade-postmortem in batch mode for the date:

```bash
python tools/trade_postmortem.py <date>
```

Then file per-trade structured diagnoses via the trade-postmortem
skill and create / update the `docs/diagnoses/INDEX_<date>.md`.

Capture:
- N trades analysed
- Worst capture_pct (sort ascending)
- Any P0 trade-level flags surfaced (LATE EXIT, TREND MISMATCH,
  TIGHT TP, NEAR-SL HOLD, CARRYOVER)
- List of trade diagnosis file paths

If any trade carries a P0 flag AND its cause looks like a *bug* (not
a strategy choice), the conditional code-bug-review phase fires.

## Phase 6 — `brutal-review`

Run brutal-review against the fresh local data. The skill itself
persists its output to `docs/reviews/brutal_review_<date>.md` (append
on same-day repeats with a `## Session @ HH:MM IST` header — this
session will produce one such block).

Capture:
- Verdict (GREEN/YELLOW/RED)
- Top suspicions (just the headlines, not the full evidence — that's
  in the brutal_review file already)
- Any "code-bug suspicion" flags the brutal review raises

If verdict is **RED** OR brutal-review identifies a likely bug, the
relevant conditional phases below fire.

## Phase 7 — `postmortem-writer` (CONDITIONAL)

Fires when ANY of:

- Phase 3 (trading-audit) verdict is RED.
- Phase 4 (reconcile-positions) exited 1 (divergence) or 77 (cannot
  reconcile).
- Phase 5 (trade-postmortem) surfaced a P0 trade-level flag with
  blast radius > the daily-VAR rounding threshold.
- Phase 6 (brutal-review) verdict is RED.
- The user explicitly said "treat today as an incident".

When it fires:

- Invoke postmortem-writer to draft an operational postmortem.
- Pre-fill from the captured data above (verdict, divergence report
  path, trade IDs, brutal-review headlines).
- File at `docs/postmortems/postmortem_<date>_<slug>.md`.
- Status: Draft.

When it does NOT fire: explicitly note in the dossier "No incident
postmortem filed — Phase 3/4/5/6 all within tolerance."

## Phase 8 — `code-bug-review` (CONDITIONAL)

Fires when ANY of:

- Phase 6 (brutal-review) raised a code-side suspicion (e.g. "exit
  logic appears to cap winners by code, not by config").
- Phase 5 (trade-postmortem) surfaced a flag whose root cause is
  obviously code (e.g. silent fallback on stale price, lookahead in
  indicator).
- The user explicitly says "do a quick code review of <area>".

Restrict scope to the implicated area only — do NOT do a full code
sweep at EOD. A scoped review takes minutes; a full sweep takes
hours. Findings file at `docs/bug_found_<date>/`.

When it does NOT fire: explicitly note in the dossier "No code-bug
review run — no code-side suspicion surfaced."

## Phase 9 — `daily-log`

Append a journal entry to
`docs/journal/engineering_journal_<date>.md` summarising what the
pipeline produced. Bullets only, cross-linked to every artefact.

## Phase 10 — write the EOD dossier

This is the user's index page for the day. Write to (canonical path,
owned by `repo-conventions`):

```
docs/eod/eod_report_<YYYY-MM-DD>.md
```

If the file already exists for today (you ran the pipeline twice),
**append** a `## Re-run @ HH:MM IST` section — do not overwrite.

### Dossier template (mandatory)

```
# EOD dossier — <YYYY-MM-DD> (IST)

- **Author:** eod-pipeline skill
- **Generated:** <YYYY-MM-DD HH:MM IST>
- **Pipeline status:** Complete | Aborted at Phase <N>
- **Data freshness:** Pulled at <HH:MM IST> from cloud VM | Stale (skip pull)

## Verdict roll-up

| Phase | Skill | Verdict | Notes |
|---|---|---|---|
| 3 | trading-audit | GREEN/YELLOW/RED | <one line> |
| 4 | reconcile-positions | exit 0/1/77 | <one line> |
| 5 | trade-postmortem | <N trades, worst capture %> | <one line> |
| 6 | brutal-review | GREEN/YELLOW/RED | <one line> |
| 7 | postmortem-writer | filed / not filed | <reason> |
| 8 | code-bug-review | run / not run | <scope or reason> |

## Bottom-line numbers (cited from brutal-review)

- Realised P&L (window): ₹<X> (W/L)
- Avg win / avg loss: ₹<...> / ₹<...>
- Avg MFE leakage: ₹<...>
- Slippage actual vs assumed: <bps>
- Drawdown: <%>
- Cycles / signals scored / accepted / trades placed: .../.../.../...

## Artefacts produced (links)

- Brutal review: `docs/reviews/brutal_review_<date>.md`
- Trade diagnoses index: `docs/diagnoses/INDEX_<date>.md`
- Per-trade diagnoses: <list of trade_<id>_<date>.md paths>
- Reconciliation: `docs/diagnoses/reconcile_<date>_<HHMM>.md` (or
  "Clean — no file filed")
- Incident postmortem: `docs/postmortems/...` (or "Not filed")
- Bug findings: `docs/bug_found_<date>/` (or "Not run")
- Journal entry: `docs/journal/engineering_journal_<date>.md`
- DB snapshot (if divergence): `data/trading_agent.db.bak-<ts>`
- Daemon log: `logs/daemon_<date>.log`
- Trading agent log: `logs/trading_agent_<date>.log`
- Signal audit CSV: `logs/signal_audit_<date>.csv`
- Audit checkpoints (folder): `logs/audit/<date>/`

## Top 3 things to look at tomorrow

1. <action item — cite the artefact that justifies it>
2. ...
3. ...

## Pipeline log (timestamps)

| Time (IST) | Phase | Result |
|---|---|---|
| HH:MM | Phase 1 pull | ok / N skipped |
| HH:MM | Phase 3 trading-audit | <verdict> |
| HH:MM | Phase 4 reconcile-positions | exit <code> |
| HH:MM | Phase 5 trade-postmortem | <N trades, worst <pct>%> |
| HH:MM | Phase 6 brutal-review | <verdict> |
| HH:MM | Phase 7 postmortem-writer | <filed: yes/no> |
| HH:MM | Phase 8 code-bug-review | <run: yes/no> |
| HH:MM | Phase 9 daily-log | appended |
| HH:MM | Phase 10 dossier | written |
```

## Failure handling

| Failure | Action |
|---|---|
| Phase 1 pull exits 1 | ABORT pipeline; offer "skip pull" rerun |
| Phase 3 trading-audit can't find checkpoint | Continue (note in dossier); pipeline still valuable |
| Phase 4 reconcile exits 77 | Continue; flag as RED in roll-up; Phase 7 fires |
| Phase 5 tool errors | Continue; note in dossier; skip trade-postmortem skill output for affected trades |
| Phase 6 brutal-review fails to write file | ABORT — brutal-review's own contract forbids silent loss |
| Phase 9 daily-log fails | Continue but warn user in chat |
| Phase 10 dossier write fails | ABORT — the dossier is the deliverable |

In every "Continue" case, mark the phase as `degraded` in the
verdict roll-up. The user must be able to see at a glance which
phases ran cleanly and which didn't.

## Cross-skill discipline

This skill **does not duplicate** the work of the skills it
orchestrates — it invokes them and lets them follow their own
contracts:

- `brutal-review` still appends to `docs/reviews/brutal_review_<date>.md`
  with its own session header — the dossier links to that file rather
  than embedding the content.
- `code-bug-review` still writes per-finding files in
  `docs/bug_found_<date>/` — the dossier links to `INDEX.md` there.
- `reconcile-positions` still snapshots the DB before any divergence
  report.
- `postmortem-writer` still defaults Status: Draft.
- Output paths owned by `repo-conventions`; this skill never invents
  new ones.

## Hard rules

- **Never start without explicit Phase 0 approval.** "Run EOD" is a
  trigger; the plan-and-confirm step is mandatory.
- **Never silently swap in stale local logs.** If pull is skipped or
  fails, the dossier MUST be tagged "Data freshness: Stale".
- **Never embed brutal-review's body in the dossier.** Link to it.
  Two files saying the same thing is exactly the drift `repo-conventions`
  exists to prevent.
- **Conditional phases (7, 8) must be explicit about why they did or
  did not fire.** "Not filed" is a valid outcome but it must be
  justified in the dossier's verdict roll-up.
- **One dossier per day.** Re-runs append; never `_v2.md`.
- **Dossier write is the last step.** Everything else must complete
  (or fail loudly) before the dossier is written, because the dossier
  is the index.

## What this skill must NOT do

- Do not run during market hours (09:00–16:00 IST) unless the user
  explicitly says "even though market is open". The pipeline pulls
  *closed* artefacts (EOD); running mid-day captures a half-day view
  that's worse than waiting.
- Do not run remediation. Any "fix something" action surfaced by the
  skills it invokes is recorded in the dossier as a recommendation;
  the operator decides whether to execute and uses `changes-done` to
  record it.
- Do not modify config, code, the DB, or any log file (snapshots are
  copies, not edits).
- Do not skip Phase 0. Even when the user says "just run it", show
  the plan and require "go".
- Do not parallelise the phases. They have ordering dependencies
  (e.g. Phase 7's conditional depends on Phases 3/4/5/6 results).
