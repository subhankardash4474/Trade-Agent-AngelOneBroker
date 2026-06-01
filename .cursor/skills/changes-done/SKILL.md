---
name: changes-done
description: >-
  Appends a structured change record to docs/changes/changes_done_<date>.md.
  Captures the change, the why, the files touched, the risk, the rollback,
  and the verification step for every code/config/data change shipped that
  day. Use when the user says "record this change", "add to changes-done",
  "log this change", "changes done entry", "what we changed today", or when
  invoked by incident-response after a recovery change.
---

# Changes-Done — Daily Change Record

## Why this skill exists

The repo currently has `changes_done_*.md` files **at both the repo
root and inside `docs/`** with different dates and inconsistent
internal structure. This skill:

1. Forces the canonical path (`docs/changes/` per `repo-conventions`).
2. Forces a consistent template so the file is greppable and the next
   operator can do a clean rollback without re-reading commits.
3. Removes the ambiguity around "did this go into changes-done?".

## When this skill fires

- "record this change", "log this change", "log the change"
- "add to changes-done", "changes-done entry", "changes done"
- "what we changed today"
- Automatic: `incident-response` triggers this skill whenever a
  recovery change (restart, config tweak, manual close, hotfix
  commit) is approved during triage.

Do not fire on:
- "log it" / "journal this" → `daily-log` (journal is narrative;
  changes-done is the change log)
- "write a postmortem" → `postmortem-writer`

## Relationship to `daily-log`

These two are complementary, not redundant:

- `daily-log` (engineering journal) is the **narrative** of the day
  — what you worked on, decisions, blockers. One bullet may
  reference a change.
- `changes-done` (this skill) is the **change manifest** — every
  code/config/data change with its rollback. One row per change.

A single change typically gets one row in `changes-done` *and* one
bullet in `daily-log` cross-linking to it.

## Output path (HARD RULE — owned by `repo-conventions`)

```
docs/changes/changes_done_<YYYY-MM-DD>.md
```

- Date is **IST today**.
- One file per day.
- If the file does not exist, create it (header + first change). If
  it exists, **append** a new change row — do not overwrite.
- If `docs/changes/` does not exist, create it.
- Do **not** write to:
  - the repo root (legacy `changes_done_2026-05-14.md` etc. are
    grandfathered; new files go in canonical path)
  - `docs/changes_done_*.md` (loose at `docs/` root — also legacy,
    do not extend)

## File header (only when the file is first created today)

```
# Changes Done — <YYYY-MM-DD> (IST)

**Author(s):** <handle>
**Branch:** <branch name>
```

After the header, each change is appended below as a `## Change` block.

## Change-block template (every append)

```
---

## Change @ <HH:MM IST> — <one-line title>

- **Type:** code | config | data | infra | runbook | other
- **Files touched:** `path/one.py`, `path/two.yaml` (or "N files in
  `packages/<area>/` — see commit")
- **Commit(s):** <SHA>, <SHA> (or "uncommitted at time of writing")
- **PR / branch:** <link or branch name>
- **Trigger:** routine | incident-response | bug-fix |
  freeze-bypass | scheduled
- **Risk:** low | medium | high — one-line justification
- **Rollback:** <exact steps to undo, including git revert SHA or
  config-key + previous value>
- **Verification:** <how the operator confirmed it works — test
  name, log line to grep, metric to watch>

### Why

<1–3 sentences, business or operational reason. Banned phrases:
"cleanup", "minor fix", "refactor" without saying what behaviour
they change.>

### Cross-links (optional)

- Bug finding (if this change fixes one):
  `docs/bug_found_<date>/<file>.md`
- Postmortem (if this change is incident-driven):
  `docs/postmortems/<file>.md`
- Trade diagnosis (if a trade triggered this):
  `docs/diagnoses/trade_<id>_<date>.md`
- Journal entry: `docs/journal/engineering_journal_<date>.md`
  (the matching bullet)
```

## How to elicit content when the user only said "record this change"

Do not invent content. Ask one consolidated question:

> "What changed (1 line) / type (code/config/data/infra) / files
> touched / commit SHA / risk / rollback step / verification step /
> why?"

Never make up risk or rollback. If the operator can't articulate
rollback, write `Rollback: NOT IDENTIFIED — DO NOT MERGE WITHOUT
ROLLBACK PLAN` and stop.

## Hard rules

- **Append, never overwrite.** Same-day re-runs add a new
  `## Change @ HH:MM IST` block at the bottom.
- **One change per block.** Do not bundle "fixed three bugs" into
  one row — that defeats the rollback purpose.
- **Rollback is mandatory.** Even for a "trivial" change, write the
  revert SHA or the previous config value. "git revert HEAD" counts.
- **`freeze-bypass:` trigger** must be tagged explicitly when the
  change goes against the active FREEZE_v2.1 window. The repo's
  freeze contract caps these at 3 per window — the count is
  computable by grepping the file.
- **Filing location is fixed.** No `docs/changes_done_<date>.md`
  loose, no `changes/` at repo root, no `logs/changes/`. Only
  `docs/changes/changes_done_<date>.md`.

## What this skill must NOT do

- Do not commit the change for the user.
- Do not summarise or invent — record what the operator says
  verbatim.
- Do not move legacy `changes_done_*.md` files; that is a
  `repo-conventions` cleanup task triggered by the user explicitly.
- Do not file the same content as a journal entry — that's
  `daily-log`'s job. Cross-link instead.
