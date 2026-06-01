# Trading Agent — Skills Index

This directory contains the Cursor Skills that customise the AI agent's
behaviour for the Trading Agent project. Each skill auto-fires on
specific trigger phrases (listed below) and enforces a project-specific
persona, workflow, or output convention.

There is also one Cursor Rule (in `.cursor/rules/`) that auto-attaches
on glob matches and points at its sibling skill.

## At a glance

| # | Skill | One-liner | Auto-writes? |
|---|---|---|---|
| 1 | [trading-audit](#1-trading-audit) | Fast hourly status from the daemon's pre-generated checkpoint | No |
| 2 | [brutal-review](#2-brutal-review) | Expert algo trader persona — honest, brutal, evidence-backed strategy/P&L review | Yes → `docs/reviews/` |
| 3 | [code-bug-review](#3-code-bug-review) | Senior staff engineer persona — forensic code review with severity-tagged findings | Yes → `docs/bug_found_<date>/` |
| 4 | [repo-conventions](#4-repo-conventions) | Canonical "where does this go / what do I call it" reference for files | Only after dry-run approval |
| 5 | [postmortem-writer](#5-postmortem-writer) | Structured operational incident postmortems | Yes → `docs/postmortems/` |
| 6 | [trade-postmortem](#6-trade-postmortem) | Per-trade MFE/MAE/capture/lag analysis | Yes → `docs/diagnoses/trade_<id>_<date>.md` |
| 7 | [reconcile-positions](#7-reconcile-positions) | DB ↔ broker ↔ trades.csv reconciliation, divergence reports | Yes → `docs/diagnoses/reconcile_*.md` on divergence |
| 8 | [incident-response](#8-incident-response) | Live triage — snapshot, checklist, recommendation, postmortem stub | Yes → DB snapshot + postmortem stub |
| 9 | [daily-log](#9-daily-log) | Engineering journal entries (what did / in flight / blockers / decisions) | Yes → `docs/journal/` |
| 10 | [changes-done](#10-changes-done) | Daily change manifest (change / risk / rollback / verification) | Yes → `docs/changes/` |
| 11 | [test-conventions](#11-test-conventions) | Test file naming, layout, markers, fixtures. Has a matching Cursor rule. | Only after dry-run approval |
| 12 | [eod-pipeline](#12-eod-pipeline) | Orchestrator — pulls fresh logs from cloud VM, then runs audit + reconcile + trade-postmortem + brutal-review + conditionals, writes single dossier | Yes → `docs/eod/eod_report_<date>.md` (+ inherits all sub-skill outputs) |

Plus:

| | Rule | One-liner |
|---|---|---|
| R1 | `.cursor/rules/test-conventions.mdc` | Auto-attaches on `tests/**/*.py` edits; points at the `test-conventions` skill. |
| R2 | `.cursor/rules/secret-hygiene.mdc` | Auto-attaches on `.env*` and credential files; blocks reading/committing secrets. |

## Decision tree — "which skill should I reach for?"

**You want to know what's happening now / today**
- Quick hourly status → `trading-audit` ("audit", "status", "any update")
- Live incident in progress → `incident-response` ("incident", "daemon down")
- Full critical review of the strategy/P&L → `brutal-review` ("brutal review", "play adviser")
- **Full post-close ritual (pull + analyse + dossier)** → `eod-pipeline` ("EOD pipeline", "EOD deep dive", "do the EOD ritual")

**You want to analyse something specific**
- A single trade → `trade-postmortem` ("analyse trade <id>")
- Positions vs broker → `reconcile-positions` ("reconcile", "DB vs broker")
- A code path for bugs → `code-bug-review` ("find bugs", "code review")
- An incident that already happened → `postmortem-writer` ("write a postmortem")

**You want to record something**
- A change shipped → `changes-done` ("record this change")
- A day's narrative → `daily-log` ("log it", "journal this")

**You want to know "where does this go / what do I name this"**
- For any project file → `repo-conventions` ("where should this go", "naming")
- For a test file specifically → `test-conventions` ("test naming")
  (auto-fires via rule R1 on `tests/**/*.py` edits)

**You want to clean up drift**
- Doc/log/data layout → `repo-conventions` ("clean up docs") — dry-run first
- Test files → `test-conventions` ("clean up tests") — dry-run first

## Skill cross-link graph

```
                          eod-pipeline (orchestrator)
                                 │
        ┌────────────┬───────────┼────────────┬─────────────┐
        ▼            ▼           ▼            ▼             ▼
   trading-audit  reconcile-  trade-      brutal-       daily-log
                  positions   postmortem  review        (always)
                     │           │            │             │
                     │           │            ▼             ▼
                     │           │      docs/reviews/  docs/journal/
                     │           │            │
                     │           │            └─(if RED or bug)─┐
                     │           │                              │
                     │           └─(if P0)─┐                    │
                     │                     ▼                    ▼
                     │              postmortem-writer    code-bug-review
                     │                     │                    │
                     ▼                     ▼                    ▼
              docs/diagnoses/      docs/postmortems/   docs/bug_found_<date>/
                                                              │
                                                              └─► verification
                                                                  test cited
                                                                  per
                                                                  test-conventions

                     │
                     └─► writes index: docs/eod/eod_report_<date>.md

incident-response (live, separate flow, also calls reconcile + postmortem-writer)
changes-done    ──► docs/changes/   (every shipped change)

repo-conventions  ◄── owns all of the above output paths
test-conventions  ◄── owns tests/ paths
trading-audit     ◄── reads only; never writes
```

Every writing-capable skill defers to `repo-conventions` (or
`test-conventions` for `tests/`) for output paths. If the conventions
ever change, edit those two skills — the others inherit.

---

## Detailed reference

### 1. trading-audit

**Purpose:** Read the daemon's pre-generated hourly audit checkpoint
and summarise. Fast, trusts the checkpoint, < 25 lines on a GREEN
verdict.

**Triggers:** "audit", "re-audit", "checkpoint", "summary till now",
"summary", "status", "any update", "anything new", "what happened",
"how is it going", "scan logs", "any errors"

**Reads:** latest `logs/audit/<date>/checkpoint_*.md`

**Writes:** nothing

**When NOT to use:** when you want a critical re-derivation from raw
data → `brutal-review` instead.

---

### 2. brutal-review

**Purpose:** Expert algo trader + adviser persona. Distrusts the
checkpoint, re-derives metrics from raw logs/CSV/DB, ranks findings
by ₹ impact/day. No sugar-coating.

**Triggers:** "brutal review", "honest review", "no sugar-coating",
"play adviser", "play advisor", "act as adviser", "expert review",
"deep review", "full review", "review the system end-to-end",
"EOD review", "what's wrong", "what could be the bug", "where is it
going wrong", "is the strategy actually working", "is the edge real"

**Reads:** every `daemon_*.log`, every `signal_audit_*.csv`
(including rejected rows), `data/trading_agent.db`, postmortems,
diagnostics; code in `packages/` only if a business question can't be
closed from data.

**Writes:** `docs/reviews/brutal_review_<YYYY-MM-DD>.md` (append on
same-day repeats with `## Session @ HH:MM IST` blocks).

**Output structure:** verdict → bottom-line numbers → top suspicions
ranked by ₹ → daemon's self-lies → things that look fine → refused
conclusions → 24h checklist.

---

### 3. code-bug-review

**Purpose:** Senior staff engineer persona. Forensic code-side bug
hunt across `trading_agent.py` and `packages/`. Severity-tagged,
category-tagged, one bug per file, cited line ranges.

**Triggers:** "code bug review", "code review please", "review the
code", "find bugs", "find me bugs", "code audit", "static review",
"concurrency review", "race condition review", "think like a staff
engineer", "review packages/<x>" (with bug-hunting intent)

**Reads:** source code under `trading_agent.py`, `packages/`,
`tools/`, `config.yaml`, `Dockerfile`, `tests/`.

**Writes:** `docs/bug_found_<YYYY-MM-DD>/` folder containing:
- `INDEX.md` with verdict + counts + recommended merge order
- `01_P0_<slug>.md`, `02_P0_<slug>.md`, ... one bug per file
- Appends with continued numbering if folder exists for today

**Bug categories:** CORRECTNESS, CONCURRENCY, STATE, IO_BOUNDARY,
DATA_INTEGRITY, SILENT_FALLBACK, RESOURCE_LEAK, CONFIG,
TEST_INTEGRITY, OBSERVABILITY.

**Severity:** P0 (money loss now), P1 (money loss soon), P2 (quality),
P3 (hygiene).

---

### 4. repo-conventions

**Purpose:** Canonical file naming and directory layout reference for
the whole repo. All other writing skills defer to this for output
paths. Owns a dry-run cleanup flow for legacy violators.

**Triggers:** "where should this go?", "what should I name this?",
"naming convention?", "is the structure right?", "clean up the
layout", "reorganise docs", "where does the {EOD report /
postmortem / audit / changes-done / finding} belong?", "rename
consistently"

**Reads:** the project tree.

**Writes:** nothing autonomously. "Clean up" triggers a **dry-run**
(table of proposed moves, `git mv` commands, commit message, broken
references) and stops. Only after explicit approval ("approve all",
"approve 1,3,5") does it execute moves as a single git commit.

**Owns:** canonical paths for `docs/audits/`, `docs/postmortems/`,
`docs/journal/`, `docs/changes/`, `docs/findings/`, `docs/eod/`,
`docs/reviews/`, `docs/freeze/`, `docs/diagnoses/`, `docs/deferred/`,
`docs/e2e/`, `docs/phases/`, `docs/bug_found_<date>/` plus universal
naming rules (ISO dates, snake_case, no `_v2/_final` siblings).

**Known legacy violators:** 30+ files identified for opt-in cleanup.

---

### 5. postmortem-writer

**Purpose:** Structured operational incident postmortems. Enforces
template, canonical path, cross-links to bug findings / trade
diagnoses / change records.

**Triggers:** "write a postmortem", "file a postmortem", "draft a
postmortem", "document the incident", "incident report", "postmortem
for today", "postmortem for <date>", "record what happened"

**Reads:** daemon log, audit checkpoint nearest the incident, DB,
trades, signal-audit.

**Writes:** `docs/postmortems/postmortem_<YYYY-MM-DD>_<slug>.md`
(slug describes symptom, not cause).

**Severity:** SEV-1 (money loss / position mismatch / DB corruption),
SEV-2 (missed signals / broken alerts / recoverable), SEV-3
(observability gap).

**Auto-invoked by:** `incident-response` at the end of every triage.

**Note:** This is for *operational* postmortems. The *strategy-edge*
postmortem has its own hand-filled template at
`docs/postmortem_phase_a_template.md` per the FREEZE_v2.1 contract.

---

### 6. trade-postmortem

**Purpose:** Per-trade forensic analysis. Wraps
`tools/trade_postmortem.py` for MFE/MAE/capture/lag computation and
overlays signal-audit + regime + threshold context. One file per
trade ID.

**Triggers:** "analyse trade <id>", "trade postmortem <id>", "why did
<symbol> exit at <time>", "what went wrong with that trade", "review
trades for <date>", "MFE on today's trades"

**Reads:** `tools/trade_postmortem.py` output,
`logs/signal_audit_<date>.csv`, DB `trades` table.

**Writes:**
- `docs/diagnoses/trade_<trade_id>_<YYYY-MM-DD>.md` per trade
- `docs/diagnoses/INDEX_<YYYY-MM-DD>.md` for batch runs (sorted by
  capture_pct ascending)

**Tool-flag catalogue (from the wrapped tool):** LATE-ENTRY, LATE
EXIT, TREND MISMATCH, TIGHT TP, NEAR-SL HOLD, CARRYOVER.

---

### 7. reconcile-positions

**Purpose:** Wraps `tools/reconcile_trade_book.py` to diff
`data/trading_agent.db` against AngelOne's broker tradeBook. On
divergence, files a structured report after snapshotting the DB.

**Triggers:** "reconcile", "reconcile trades", "reconcile positions",
"check DB matches broker", "are positions in sync", "trade book
reconciliation", "DB vs broker for <date>", "did everything match
yesterday"

**Reads:** `tools/reconcile_trade_book.py`, DB, broker, `logs/trades.csv`.

**Writes (only on divergence):**
- DB snapshot to `data/trading_agent.db.bak-<ts>` (ALWAYS, before
  the report)
- `docs/diagnoses/reconcile_<YYYY-MM-DD>_<HHMM>.md` for divergence
- `docs/diagnoses/reconcile_<YYYY-MM-DD>_<HHMM>_FAILED.md` for tool
  exit 77 (cannot reconcile)

**Exit code contract (from the tool):** 0 clean, 1 divergence, 77
cannot connect.

**Auto-invoked by:** `incident-response` step 5 of the triage.

---

### 8. incident-response

**Purpose:** Live triage flow for daemon/broker/DB incidents during
market hours. Snapshots state → 7-item checklist → contained
recommendation → auto-stubs a postmortem.

**Triggers:** "incident", "incident response", "trigger incident
playbook", "daemon is down", "daemon crashed", "daemon hung", "no
cycles", "broker disconnected", "broker auth failed", "DB locked",
"database is locked", "positions don't match", "something is wrong
now", "the trading agent is misbehaving"

**Phase 0 (always, no approval needed — evidence preservation only):**
- DB snapshot → `data/trading_agent.db.bak-<YYYYMMDD-HHMMSS>`
- Health snapshot → `logs/diagnostics/health_<ts>.json`
- Last 200 lines of daemon and trading_agent logs
- PowerShell process check
- Latest audit checkpoint

**Phase 1 (triage checklist):** daemon alive → broker session → DB
locked → last cycle → reconciliation → unsent alerts → drawdown vs
halt.

**Phase 2 (recommendation, one of):** HOLD, MONITOR, RESTART DAEMON,
RE-AUTH BROKER, SQUARE OFF [SYMBOLS], HALT TRADING, ESCALATE.
**Does not execute without explicit operator approval.**

**Phase 3 (auto-stub):** creates a `docs/postmortems/...` draft
pre-filled with timeline and Phase 0 snapshot paths.

---

### 9. daily-log

**Purpose:** Append structured entries to the engineering journal.
Bullets only — what I did / in flight / blockers / decisions /
followups.

**Triggers:** "log it", "log this", "log this decision", "journal
this", "add to journal", "engineering journal", "EOD journal"

**Reads:** asks the user.

**Writes:** `docs/journal/engineering_journal_<YYYY-MM-DD>.md`
(append on same-day repeats with `## Entry @ HH:MM IST` blocks).

**Distinct from `changes-done`:** journal is narrative; changes-done
is the change manifest. One change typically has one bullet here
*and* one row in changes-done, cross-linked.

---

### 10. changes-done

**Purpose:** Append structured change records — the change, why,
files touched, risk, **rollback**, verification — to a daily manifest.
Rollback is mandatory.

**Triggers:** "record this change", "log this change", "log the
change", "add to changes-done", "changes-done entry", "changes done",
"what we changed today"

**Reads:** asks the user.

**Writes:** `docs/changes/changes_done_<YYYY-MM-DD>.md` (append on
same-day repeats with `## Change @ HH:MM IST` blocks).

**Auto-invoked by:** `incident-response` after any approved
remediation action.

**Special trigger flag:** `freeze-bypass` — when a change goes against
the active FREEZE_v2.1 window. The FREEZE contract caps these at 3 per
window; the count is computable by grep.

---

### 11. test-conventions

**Purpose:** Owns test file naming, folder layout, markers, and
fixture organisation under `tests/`. Has a matching Cursor rule that
auto-attaches on every `tests/**/*.py` edit.

**Triggers:** "test naming", "what should I call this test", "where
does this test go", "fix test names", "clean up tests/", "add a
marker", "shared fixture", "rename test files"

**Reads:** project tree, `pyproject.toml` markers.

**Writes:** nothing autonomously. "Clean up tests" triggers a
dry-run, then executes renames only on explicit approval.

**Owns:**
- File patterns: `test_<module>.py`, `test_<module>_<aspect>.py`,
  `test_<module>_regressions.py`, `test_<feature>_e2e.py`
- Forbidden: dated `test_audit_YYYY_MM_DD_phaseN.py`, overloaded
  `test_audit_*`, mystery names
- Target folder layout mirroring `packages/`
- Marker discipline (`slow`, `integration`, `live`, `flaky(reason=)`)
- Fixture rules (function-local → module-local → folder conftest →
  `tests/fixtures/<topic>.py` registered in `tests/conftest.py`)
- File header docstring contract

**Known violators:** 12 dated/overloaded test files identified for
opt-in cleanup.

---

### 12. eod-pipeline

**Purpose:** End-of-day orchestrator. Pulls fresh artefacts from the
cloud VM via `tools/cloud/pull_logs.ps1`, then runs `trading-audit`,
`reconcile-positions`, `trade-postmortem` (batch), and `brutal-review`
in sequence. Conditionally invokes `postmortem-writer` if any phase
surfaces an incident, and `code-bug-review` if a code-side suspicion
arises. Closes by writing a single dossier doc at
`docs/eod/eod_report_<date>.md` that indexes every artefact produced.

**Triggers:** "EOD pipeline", "EOD deep dive", "EOD ritual",
"EOD review", "pull and review", "pull logs and review", "full EOD
review", "cloud sync and review", "run end of day analysis",
"do the whole post-close pass", "nightly review"

**Reads (after pull):** `logs/audit/<date>/`,
`logs/trading_agent_<date>.log`, `logs/signal_audit_<date>.csv`,
`logs/trades.csv`, `logs/health.json`, optionally
`data/trading_agent.db`.

**Writes:**
- The dossier itself: `docs/eod/eod_report_<YYYY-MM-DD>.md` (append on
  same-day re-runs with `## Re-run @ HH:MM IST` blocks).
- Inherits all sub-skill outputs: `docs/reviews/`, `docs/diagnoses/`,
  `docs/postmortems/` (conditional), `docs/bug_found_<date>/`
  (conditional), `docs/journal/`.

**Phase 0 is mandatory:** prints the plan, asks for "go" /
"go --include-db" / "go --date <YYYY-MM-DD>" / "skip pull" / "cancel".
Never starts unprompted.

**Conditional fires:**
- `postmortem-writer` if any of: trading-audit RED, reconcile exit 1
  or 77, trade-postmortem P0 with non-trivial blast radius, brutal-
  review RED.
- `code-bug-review` if brutal-review or trade-postmortem identifies a
  code-side root cause. Scope restricted to the implicated area.

**Hard rules:** never runs during market hours unless overridden;
never silently uses stale logs (always tagged in dossier); the dossier
is the *index*, not a copy — links to other artefacts rather than
embedding them; one dossier per day with append semantics.

---

## Adding new skills

When proposing a new skill:

1. Check this index — does an existing skill already cover it?
2. If not, ensure the trigger phrases don't collide with an existing
   skill.
3. Write the skill at `.cursor/skills/<name>/SKILL.md` following the
   structure of the existing ones (frontmatter → persona contract →
   when to fire → mandatory inputs → output format → hard rules →
   what NOT to do → cross-skill links).
4. Update this README with a new row in "At a glance" and a detailed
   section.
5. Defer output paths to `repo-conventions` (or `test-conventions`)
   rather than inventing new ones.

## Adding new rules

Rules live in `.cursor/rules/<name>.mdc` with frontmatter:

```yaml
---
description: <short description>
globs: ["pattern/**/*.ext"]
alwaysApply: false
---
```

Keep rules thin — they should point at a skill for heavy content,
not duplicate it. Rules are for **passive auto-enforcement on edits
to specific file types**; skills are for **explicit workflows**.

## Pending / not built

These were considered but not yet built:

- **freeze-guardrails** — would block edits to frozen files
  (`packages/strategies/`, `packages/core/`, `config.yaml`
  thresholds) during the FREEZE_v2.1 window without an explicit
  `freeze-bypass: <reason>` override. Highest-urgency missing skill
  while the freeze is active (closes 2026-06-05).
- **pre-market-checklist** — 09:00 IST readiness check (daemon alive,
  broker login, universe loaded, no stale data, freeze status).
- **config-change-safety** — snapshot + verify cycle for any
  `config.yaml` / `config_overlays/` edit.
- **db-schema-migration** — backup + migration + rollback flow for
  schema changes.

Add via the "Adding new skills" steps above when needed.
