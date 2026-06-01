---
name: repo-conventions
description: >-
  Canonical reference for file naming and directory placement in the Trading
  Agent repo. This is the single source of truth that the agent MUST consult
  before creating any new file under docs/, logs/, data/, or tools/, and
  before suggesting a rename or move. Use whenever the user asks "where
  should this go?", "what should I name this file?", "is the structure
  right?", "clean up the layout", "where does the EOD report belong?",
  "rename this consistently", or any time the agent itself is about to
  create a markdown, log, or report file and needs to pick a path. Other
  skills (brutal-review, code-bug-review, trading-audit) defer to this skill
  for output paths.
---

# Trading Agent — Repository Conventions

## Why this skill exists

The repo has accumulated parallel ad-hoc paths:

- `changes_done_*.md` lives at BOTH the repo root AND under `docs/`.
- `docs/postmortems/` (empty folder) coexists with
  `docs/postmortem_2026-05-13_morning_losses.md` (loose at top level).
- `docs/audits/` (one file) coexists with
  `docs/audit_2026-05-28_followup.md` (loose at top level).
- `logs/postmortem/` (singular) and `docs/postmortems/` (plural) refer
  to the same concept with different names.
- Dates appear as `2026-05-29`, `20260529`, and `2026-05-29_1140` in
  different places.

Going forward, the agent must:

1. **Consult this skill before creating any file** under `docs/`, `logs/`,
   `data/`, `tools/`, or the repo root.
2. **Cite this skill** in commit messages or chat when justifying a path
   choice.
3. **Suggest a canonical path when the user asks** "where should this
   go?" — never invent ad-hoc locations.
4. **Flag drift** when the user references a non-canonical path, but do
   not move files unless explicitly asked.

## When this skill fires

- "where should this go?" / "what folder?" / "what path?"
- "what should I name this?" / "naming convention?"
- "is the structure right?" / "clean up the layout"
- "where does the {EOD report / postmortem / audit / changes-done / finding}
  belong?"
- "rename this consistently"
- Any time the agent itself is about to call the Write tool with a new
  file path under `docs/`, `logs/`, `data/`, or repo root — read this
  skill *first* and choose the canonical path.

## Universal naming rules

1. **Dates: ISO short, `YYYY-MM-DD`.** Never `YYYYMMDD`, never
   `DD-MM-YYYY`, never `MM-DD`. If a time component is needed, append
   `_HHMM` in IST 24h: `2026-05-30_1430`.
2. **Slugs: snake_case, lowercase, ASCII only.** Two to five words.
   No spaces, no camelCase, no kebab-case in filenames.
3. **Sequence prefixes: 2-digit zero-padded** (`01_`, `02_`, …`99_`).
   Used only where ordering matters (bug-found indexes, multi-part
   migration scripts).
4. **Severity prefix in filenames: `P0`/`P1`/`P2`/`P3`** uppercase.
   Used inside `docs/bug_found_<date>/` only.
5. **No leading underscores** for new files (`_aggregate.md`, `_now.py`
   are legacy — don't introduce more).
6. **No `_v2`, `_v3`, `_final`, `_final_final`, `_followup` suffixes.**
   Append into the existing dated file or folder instead. If a true new
   version is needed, file it under a fresh date.
7. **One concept per filename, one date per filename.** `eod_2026-05-29.md`
   is fine; `eod_report_and_postmortem_2026-05-29.md` is not.
8. **Singular vs plural folders: plural for collections.** `docs/postmortems/`,
   `docs/audits/`, `docs/journal/` (currently singular — preserved for
   git history but new ones go in the canonical plural). Logs follow
   their own legacy: `logs/postmortem/`, `logs/audit/` — see exceptions
   below.

## Canonical directory map

### Repo root — what is ALLOWED here

Repo root is for **entry points and config only**. Markdown reports do
**not** belong here.

```
README.md
main.py
run_daemon.py
stop_daemon.py
trading_agent.py
config.yaml
conftest.py
pyproject.toml
requirements.txt
Dockerfile
docker-compose.yml
docker-compose.stage3.yml
.env, .env.example, .env.production.example
.gitignore, .dockerignore
```

Anything else at the root is a layout violation. Specifically:

- `changes_done_2026-05-14.md`, `changes_done_2026-05-18.md`,
  `changes_done_2026-05-19.md` at root → **belong in
  `docs/changes/changes_done_<date>.md`**. (Existing files preserved;
  new ones go to canonical path.)

### `docs/` — human-authored prose

```
docs/
├── ARCHITECTURE.md                                ← long-lived system doc
├── README.md                                      ← index of docs/
├── ops_runbook.md                                 ← long-lived runbook
├── stage3_runbook.md                              ← long-lived runbook
├── backtester_vm_runbook.md                       ← long-lived runbook
├── cloud_mvc_runbook.md                           ← long-lived runbook
├── cloud_pod_architecture.md                     ← long-lived design
├── battery_v2_design.md                           ← long-lived design
├── restructure_plan.md                            ← long-lived plan
├── strategy_research_backlog.md                   ← long-lived backlog
├── ampere_capacity_watcher_setup.md               ← long-lived setup doc
│
├── audits/                                        ← all dated audits
│   └── <topic>_audit_<YYYY-MM-DD>.md
│   └── <topic>_audit_<YYYY-MM-DD>_followup.md    ← only if a true followup
│
├── postmortems/                                   ← all dated postmortems
│   └── postmortem_<YYYY-MM-DD>_<short_slug>.md
│   └── postmortem_phase_<X>_template.md           ← templates here too
│
├── journal/                                       ← engineering journal
│   └── engineering_journal_<YYYY-MM-DD>.md
│
├── changes/                                       ← daily change log
│   └── changes_done_<YYYY-MM-DD>.md
│
├── findings/                                      ← findings registers
│   └── findings_log_<YYYY-MM-DD>.md               ← running log
│   └── findings_<YYYY-MM-DD>.md                   ← point-in-time snapshot
│
├── eod/                                           ← end-of-day reports
│   └── eod_report_<YYYY-MM-DD>.md
│
├── reviews/                                       ← named non-EOD reviews
│   └── friday_review_<YYYY-MM-DD>.md
│   └── post_freeze_v<N>_proposal.md               ← proposals also here
│
├── freeze/                                        ← freeze-window docs
│   └── FREEZE_v<X.Y>.md
│   └── FREEZE_v<X.Y>_revision.md
│   └── freeze_log_week<N>.md
│   └── freeze_v<X.Y>_exit_criteria_<YYYY-MM-DD>.md
│   └── freeze_contingencies.md
│   └── freeze_observability_extensions.md
│   └── wind_down_criteria_<YYYY-MM-DD>.md
│
├── diagnoses/                                     ← multi-day investigations
│   └── diagnosis_sprint_<YYYY-MM-DD>.md
│   └── bug_<short_slug>_diff.md                  ← e.g. bug_i_trader_divergence_diff.md
│
├── deferred/                                      ← deferred-work registers
│   └── deferred_items_<YYYY-MM-DD>.md
│
├── e2e/                                           ← e2e test plans/postmortems
│   └── e2e_<topic>_plan.md
│   └── e2e_<stage>_postmortem.md
│
├── phases/                                        ← phase-scoped docs
│   └── phase_<letter>_<short_slug>.md            ← e.g. phase_b_hourly_blackout_candidates.md
│
└── bug_found_<YYYY-MM-DD>/                        ← owned by code-bug-review skill
    ├── INDEX.md
    ├── 01_P0_<short_slug>.md
    ├── 02_P1_<short_slug>.md
    └── ...
```

Rules specific to `docs/`:

- **No dated markdown files at the top of `docs/`.** They belong in the
  appropriate dated subfolder (audits, postmortems, findings, eod,
  changes, journal, etc.).
- **Long-lived docs** (no date in name, single canonical file) stay at
  the top of `docs/`. Runbooks, architecture, design docs, plans,
  backlogs.
- **Templates** live next to the things they template
  (`postmortem_phase_a_template.md` in `docs/postmortems/`).

### `logs/` — machine and operator output

```
logs/
├── daemon_<YYYY-MM-DD>.log                        ← daily daemon stdout/err
├── trading_agent_<YYYY-MM-DD>.log                 ← daily verbose app log
├── signal_audit_<YYYY-MM-DD>.csv                  ← daily signal audit
├── trades.csv                                     ← rolling, current
├── trades_<purpose>_<YYYY-MM-DD>.csv              ← snapshots / archives
├── health.json                                    ← rolling, current
├── learning_journal.md                            ← rolling, append-only
│
├── audit/                                         ← hourly checkpoints
│   └── <YYYY-MM-DD>/checkpoint_<HHMM>.md
│   └── <YYYY-MM-DD>/checkpoint_<HHMM>.json
│
├── diagnostics/                                   ← daemon-emitted diagnostics
│   └── eod_<YYYY-MM-DD>.md                       ← daemon's own EOD
│   └── profit_diagnostic_<YYYYMMDD_HHMMSS>.md    ← legacy timestamp style; OK to keep
│   └── latest.json
│
├── postmortem/                                    ← daemon-emitted postmortems
│   └── <YYYY-MM-DD>.md                           ← short, per-day
│   └── _aggregate.md                             ← legacy; OK to keep
│
├── failed_alerts/                                 ← undelivered alerts queue
├── battery_pulled/                                ← pulled battery runs
├── live_e2e/                                      ← live e2e harness output
├── cloud_pull_<YYYY-MM-DD>_<topic>/               ← cloud sync pulls
├── cloud_sync/
├── backtests/                                     ← per-run backtest output
├── backtest_ensemble/                             ← ensemble backtest output
└── archive/                                       ← anything older than 60 days
    └── <YYYY>/<MM>/...
```

Rules specific to `logs/`:

- **Logs are written by code, not by the agent.** This skill exists so
  the agent does not invent new log paths when adding instrumentation.
  If a new log type is genuinely needed, add it to this map first.
- **EOD has two flavours, kept separate:**
  - `logs/diagnostics/eod_<date>.md` — written by the daemon at EOD,
    machine-generated.
  - `docs/eod/eod_report_<date>.md` — operator-authored narrative.
  Do not mix them.
- **Postmortem has two flavours, kept separate:**
  - `logs/postmortem/<date>.md` — daemon-emitted short summary.
  - `docs/postmortems/postmortem_<date>_<slug>.md` — operator-authored
    narrative.
- **Archive after 60 days.** Daemon logs and audit checkpoints older
  than 60 days move under `logs/archive/<YYYY>/<MM>/`. The daemon
  doesn't do this automatically; operator runs it on request.

### `data/` — durable inputs and stores

```
data/
├── trading_agent.db                               ← live SQLite store
├── trading_agent.db.bak-<YYYYMMDD-HHMMSS>        ← legacy backup naming; preserve
├── trading_agent_pre_<reason>_<YYYYMMDD_HHMMSS>.db ← pre-change snapshots
├── battery_queue.yaml                             ← rolling
├── battery_queue_<purpose>.yaml                   ← variants by purpose
├── event_calendar.csv                             ← reference data
├── slippage_log.csv                               ← rolling
├── trailing_stops.json                            ← rolling state
├── self_sufficiency.json                          ← rolling state
├── training_symbols.txt                           ← reference
├── v<N>_universe_<count>.txt                      ← versioned universe lists
├── train_dataset.csv                              ← regenerable bulk
├── test_dataset.csv                               ← regenerable bulk
├── prepare_dataset_<YYYY-MM-DD>.log               ← dataset prep logs
└── local_cloud_mirror/                            ← cloud mirror cache
```

Rules specific to `data/`:

- **DB backups** continue to use the legacy `db.bak-YYYYMMDD-HHMMSS`
  pattern — too many scripts depend on the format. Do not "modernize"
  this filename style.
- **Universe files** are versioned: `v<N>_universe_<count>.txt`. New
  universe → bump N.
- **Logs do not go in `data/`.** `prepare_dataset_*.log` is grandfathered
  because it pairs with a dataset; do not add new log types here.

### `tools/` and `tools/cloud/` — operator scripts

```
tools/
├── README.md
├── <verb>_<noun>.py                                ← canonical script naming
│   e.g. close_position.py, audit_checkpoint.py, reconcile_trade_book.py
├── <verb>_<noun>.ps1                               ← powershell variant
├── cloud/                                          ← cloud-only tooling
└── _<noun>.py                                      ← legacy "internal" scripts; preserve
```

Rules specific to `tools/`:

- **`<verb>_<noun>.py`** is the canonical naming for new scripts.
- **Leading-underscore scripts** (`_now.py`, `_state_check.py`) are
  legacy; do not introduce new ones. The pending Phase D rename in
  `docs/restructure_plan.md` will eventually move these to
  `scripts/ops/`.
- **Per-script logs** belong in `logs/`, not next to the script.

## Decision tree for "where does this go?"

Use this in order. First match wins.

1. **Is it source code?** → `packages/<area>/` or root entry point.
   Never `tools/`.
2. **Is it an operator/admin script?** → `tools/<verb>_<noun>.py`.
3. **Is it a daemon-written log or CSV?** → `logs/<canonical>` per the
   logs map.
4. **Is it durable input data or a model artifact?** → `data/` or
   `models/`.
5. **Is it human-authored prose with a date in its identity?**
   - Audit → `docs/audits/<topic>_audit_<YYYY-MM-DD>.md`
   - Postmortem → `docs/postmortems/postmortem_<YYYY-MM-DD>_<slug>.md`
   - Engineering journal entry → `docs/journal/engineering_journal_<YYYY-MM-DD>.md`
   - Daily change log → `docs/changes/changes_done_<YYYY-MM-DD>.md`
   - Findings register → `docs/findings/findings_log_<YYYY-MM-DD>.md`
   - Operator EOD report → `docs/eod/eod_report_<YYYY-MM-DD>.md`
   - Named review (friday, weekly, ad-hoc) → `docs/reviews/<name>_<YYYY-MM-DD>.md`
   - Diagnosis / investigation → `docs/diagnoses/diagnosis_<slug>_<YYYY-MM-DD>.md`
   - Bug review batch (owned by `code-bug-review` skill) →
     `docs/bug_found_<YYYY-MM-DD>/<NN>_PX_<slug>.md`
6. **Is it human-authored prose with no date in its identity?**
   - Runbook → `docs/<topic>_runbook.md`
   - Architecture / design → `docs/<topic>_architecture.md` or
     `docs/<topic>_design.md`
   - Plan / backlog → `docs/<topic>_plan.md` /
     `docs/<topic>_backlog.md`
   - Freeze window → `docs/freeze/FREEZE_v<X.Y>.md` and friends
7. **None of the above?** → STOP. Ask the user, or ask the operator
   to extend this skill before creating the file.

## Known legacy violations (two-step cleanup, never auto-move)

The agent **never** moves or renames files on its own. Cleanup is
strictly a two-step process with the user in the loop between them:

**Step 1 — DRY RUN (always, no exceptions).** When the user says
"clean up", "reorganise docs", "rename per conventions", or anything
similar, the agent must:

1. Print the proposed moves as a table in chat (current path →
   canonical path). Use the table below as the starting point, plus
   any new violations discovered since this skill was last updated.
2. Print the proposed `git mv` commands that would execute the batch.
3. Print the commit message that would be used.
4. Print any references that would break (e.g. links inside markdown
   files, imports if a `.py` is moved, hardcoded paths in scripts).
5. **Stop and wait.** Do not execute anything. Ask the user one
   question: *"Approve all moves, a subset, or cancel?"*

**Step 2 — EXECUTE (only after explicit approval).** Only after the
user replies with an unambiguous go-ahead (e.g. "yes do it", "approve
all", "approve 1, 3, 5") does the agent:

1. Run `git mv` for each approved move (preserving git history).
2. Update any references the dry-run flagged (only for approved moves).
3. Commit as a single batch with the message shown in the dry-run.
4. Do **not** push.

Hard rules around cleanup:

- "Clean up" alone is **never** sufficient to start moving files. It
  triggers the dry run, not the execution.
- If the user just says "do it" without a prior dry-run in the
  conversation, the agent must still produce the dry-run first.
- If `git status` is not clean when the user approves, the agent stops
  and asks the user to stash or commit first. Cleanup commits must be
  isolated.
- New files MUST use canonical paths from now on regardless of whether
  any cleanup has run.

### Known violations table (input to the dry run)

| Current path | Canonical path |
|---|---|
| `changes_done_2026-05-14.md` (root) | `docs/changes/changes_done_2026-05-14.md` |
| `changes_done_2026-05-18.md` (root) | `docs/changes/changes_done_2026-05-18.md` |
| `changes_done_2026-05-19.md` (root) | `docs/changes/changes_done_2026-05-19.md` |
| `docs/changes_done_2026-05-25.md` | `docs/changes/changes_done_2026-05-25.md` |
| `docs/changes_done_2026-05-26.md` | `docs/changes/changes_done_2026-05-26.md` |
| `docs/changes_done_2026-05-27.md` | `docs/changes/changes_done_2026-05-27.md` |
| `docs/audit_2026-05-28_followup.md` | `docs/audits/audit_2026-05-28_followup.md` |
| `docs/postmortem_2026-05-13_morning_losses.md` | `docs/postmortems/postmortem_2026-05-13_morning_losses.md` |
| `docs/postmortem_phase_a_template.md` | `docs/postmortems/postmortem_phase_a_template.md` |
| `docs/findings_log_2026-05-25.md` | `docs/findings/findings_log_2026-05-25.md` |
| `docs/findings_log_2026-05-27.md` | `docs/findings/findings_log_2026-05-27.md` |
| `docs/findings_2026-05-27.md` | `docs/findings/findings_2026-05-27.md` |
| `docs/eod_report_2026-05-26.md` | `docs/eod/eod_report_2026-05-26.md` |
| `docs/eod_report_2026-05-27.md` | `docs/eod/eod_report_2026-05-27.md` |
| `docs/friday_review_2026-05-29.md` | `docs/reviews/friday_review_2026-05-29.md` |
| `docs/post_freeze_v4_proposal.md` | `docs/reviews/post_freeze_v4_proposal.md` |
| `docs/FREEZE_v2.1.md` | `docs/freeze/FREEZE_v2.1.md` |
| `docs/FREEZE_v2.1_revision.md` | `docs/freeze/FREEZE_v2.1_revision.md` |
| `docs/freeze_v2.1_exit_criteria_2026-06-05.md` | `docs/freeze/freeze_v2.1_exit_criteria_2026-06-05.md` |
| `docs/freeze_log_week1.md` | `docs/freeze/freeze_log_week1.md` |
| `docs/freeze_contingencies.md` | `docs/freeze/freeze_contingencies.md` |
| `docs/freeze_observability_extensions.md` | `docs/freeze/freeze_observability_extensions.md` |
| `docs/wind_down_criteria_2026-06-05.md` | `docs/freeze/wind_down_criteria_2026-06-05.md` |
| `docs/diagnosis_sprint_2026-05-27.md` | `docs/diagnoses/diagnosis_sprint_2026-05-27.md` |
| `docs/bug_i_trader_divergence_diff.md` | `docs/diagnoses/bug_i_trader_divergence_diff.md` |
| `docs/deferred_items_2026-05-27.md` | `docs/deferred/deferred_items_2026-05-27.md` |
| `docs/e2e_broker_test_plan.md` | `docs/e2e/e2e_broker_test_plan.md` |
| `docs/e2e_stage12_postmortem.md` | `docs/e2e/e2e_stage12_postmortem.md` |
| `docs/e2e_stage21_postmortem.md` | `docs/e2e/e2e_stage21_postmortem.md` |
| `docs/phase_b_hourly_blackout_candidates.md` | `docs/phases/phase_b_hourly_blackout_candidates.md` |
| `docs/audit_2026-05-25/` (folder) | Decide: collapse into individual files under `docs/audits/` |

## How other skills consume this

- `code-bug-review` writes only to `docs/bug_found_<YYYY-MM-DD>/`
  following the exact layout in section "Canonical directory map →
  docs → bug_found_<date>/".
- `brutal-review` writes the same content it shows in chat to
  `docs/reviews/brutal_review_<YYYY-MM-DD>.md` on **every** invocation
  (not opt-in). Second/third invocations on the same day **append** a
  new `## Session @ HH:MM IST` block to the existing file rather than
  creating `_v2.md` or `_evening.md` siblings.
- `trading-audit` reads only from `logs/audit/<date>/checkpoint_*.md`
  and `logs/audit/<date>/checkpoint_*.json` — those paths are owned by
  the daemon and this skill must not move them.

## When this skill must NOT do something

- **Do not move or rename existing files without a two-step dance.**
  Step 1 is always a dry-run printed to chat. Step 2 (actual `git mv`)
  only runs after the user gives explicit approval in the next turn.
  "Clean up" alone triggers only the dry run, never the execution.
- **Do not edit files** outside of `docs/bug_found_<date>/INDEX.md` for
  the bug-review skill. This skill itself is a reference; it does not
  modify content.
- **Do not invent new top-level directories.** If the structure needs
  to grow, add the new directory to this skill first and have the user
  approve, then start using it.
- **Do not contradict `docs/restructure_plan.md`.** That doc tracks a
  larger code-layout migration (Phases D and E). This skill complements
  it for docs/logs/data; the two should agree. If they conflict, flag
  it to the user.

## Adding to this skill

When a new file type appears that isn't covered:

1. Propose where it should live and why, using the decision tree.
2. Edit this skill to add the new path to the canonical directory map
   and (if dated) to the decision tree.
3. Only then create the file.
