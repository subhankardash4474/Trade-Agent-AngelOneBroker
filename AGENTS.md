# Trading Agent — Agent Onboarding

> **You are an AI agent working on this Trading Agent codebase.** This file
> is your mandatory onboarding doc. Read it once per session before
> taking any action. It tells you which project-specific skills exist
> and when to use them.
>
> **Hard contract:** before responding to any user message that maps to
> one of the intents below, you MUST load and follow the corresponding
> skill at `.cursor/skills/<name>/SKILL.md`. Failing to consult the
> skill is failing to do the job correctly.

## What this project is

A live algorithmic trading agent for the Indian stock market (NSE/BSE)
with AngelOne broker integration. It runs as a daemon during market
hours (09:00–16:00 IST), produces hourly audit checkpoints, daily
EOD reports, signal-audit CSVs, and a SQLite trade ledger. It is
currently inside an active freeze window (`docs/FREEZE_v2.1.md`,
expires **2026-06-05**).

Key directories:

- `trading_agent.py` (~340 KB monolith), `run_daemon.py`, `main.py` — entry points
- `packages/{trader,strategies,core,brokers,monitoring,research,training,ui}/` — source
- `tools/` — operator scripts
- `tests/{unit,integration,fixtures}/` — pytest suite (107 files)
- `data/trading_agent.db` — live SQLite store
- `logs/` — daemon output, audit checkpoints, signal audit, trades CSV
- `docs/` — long-lived docs + dated reports (audits, postmortems, etc.)
- `config.yaml` + `config_overlays/` — runtime config
- `.cursor/skills/` — **project-specific agent skills (consult these)**
- `.cursor/rules/` — passive Cursor rules (auto-attach on file globs)

## Skills you MUST consult — intent → skill map

When the user's message maps to one of these intents, load the named
skill before responding. Trigger phrases are illustrative, not
exhaustive — match on intent, not exact wording.

### Read-only / status

| If the user wants to... | Use skill |
|---|---|
| Know current status of the daemon ("audit", "status", "anything new", "how is the agent doing") | `trading-audit` |
| Get a critical brutal review of strategy + P&L ("brutal review", "play adviser", "tear it apart", "what's wrong", "honest review") | `brutal-review` |
| Find code bugs ("code review", "find bugs", "static review", "concurrency review", "think like a staff engineer") | `code-bug-review` |
| **Run the full post-close ritual** — pull cloud logs + audit + reconcile + trade-postmortem + brutal-review + dossier ("EOD pipeline", "EOD deep dive", "do the EOD ritual", "pull and review", "full EOD review", "cloud sync and review") | `eod-pipeline` |

### Investigation / postmortem

| If the user wants to... | Use skill |
|---|---|
| Write an incident postmortem ("write a postmortem", "document the incident", "incident report") | `postmortem-writer` |
| Analyse a specific trade ("analyse trade X", "why did SYMBOL exit at HH:MM", "MFE on today's trades") | `trade-postmortem` |
| Reconcile positions with the broker ("reconcile", "DB vs broker", "are positions in sync") | `reconcile-positions` |
| Triage a live incident ("incident", "daemon down", "broker disconnected", "DB locked", "something is wrong now") | `incident-response` |

### Record-keeping

| If the user wants to... | Use skill |
|---|---|
| Add to the engineering journal ("log it", "journal this", "EOD journal") | `daily-log` |
| Record a shipped change ("record this change", "log this change", "add to changes-done") | `changes-done` |

### Conventions / cleanup

| If the user wants to... | Use skill |
|---|---|
| Know where a file goes / what to call it ("where should this go", "naming convention", "what folder") | `repo-conventions` |
| Clean up haphazard docs/logs/data layout ("clean up docs", "reorganise", "rename per conventions") | `repo-conventions` (dry-run first) |
| Anything about test files (naming, layout, markers, fixtures, cleanup) | `test-conventions` (also enforced via `.cursor/rules/test-conventions.mdc`) |

### Composite workflows (multiple skills cooperate)

- **Live incident:** `incident-response` → snapshots state → calls
  `reconcile-positions` for triage step 5 → stubs a postmortem via
  `postmortem-writer` → any recovery change is recorded via
  `changes-done`.
- **End-of-day (full ritual):** `eod-pipeline` orchestrates the lot —
  pull cloud logs → `trading-audit` → `reconcile-positions` →
  `trade-postmortem` (batch) → `brutal-review` → conditional
  `postmortem-writer` and `code-bug-review` → `daily-log` →
  writes a single dossier index at `docs/eod/eod_report_<date>.md`.
  **Prefer this over running the individual skills by hand** when the
  intent is "do the full post-close pass".
- **End-of-day (light):** if you just want one piece (e.g. just the
  brutal review), invoke that skill directly — `eod-pipeline` is for
  the full ritual.
- **Bug found during review:** `code-bug-review` files under
  `docs/bug_found_<date>/` → each finding's verification cites a test
  function name → tests follow `test-conventions`.

## Output-path discipline (HARD)

Every skill that writes files defers its output paths to
**`repo-conventions`** (or **`test-conventions`** for `tests/`). Do
NOT invent new locations. Specifically:

| Output | Path |
|---|---|
| Brutal reviews | `docs/reviews/brutal_review_<YYYY-MM-DD>.md` (append on same-day repeats) |
| Bug findings | `docs/bug_found_<YYYY-MM-DD>/{INDEX.md, NN_PX_<slug>.md}` |
| Incident postmortems | `docs/postmortems/postmortem_<YYYY-MM-DD>_<slug>.md` |
| Per-trade diagnoses | `docs/diagnoses/trade_<id>_<YYYY-MM-DD>.md` |
| Reconciliation reports | `docs/diagnoses/reconcile_<YYYY-MM-DD>_<HHMM>.md` (on divergence) |
| Engineering journal | `docs/journal/engineering_journal_<YYYY-MM-DD>.md` |
| Changes manifest | `docs/changes/changes_done_<YYYY-MM-DD>.md` |
| EOD dossier (eod-pipeline orchestrator output) | `docs/eod/eod_report_<YYYY-MM-DD>.md` |
| DB snapshots | `data/trading_agent.db.bak-<YYYYMMDD-HHMMSS>` |
| Health snapshots | `logs/diagnostics/health_<YYYYMMDD-HHMMSS>.json` |

Dates are always **IST today** in ISO format (`YYYY-MM-DD`), except
inside Python module names where the underscore form `YYYY_MM_DD` is
the forced exception.

## Cursor rules (passive, auto-attach by glob)

| Rule | Globs | Effect |
|---|---|---|
| `.cursor/rules/test-conventions.mdc` | `tests/**/*.py`, `conftest.py` | Points at `test-conventions` skill on every test edit |
| `.cursor/rules/secret-hygiene.mdc` | `.env*`, `**/credentials*`, `*.pem`, `*.key`, `**/angelone_*token*` | Blocks reading/echoing/committing secrets |

## Hard contract — what you must NEVER do

1. **Never write a file under `docs/`, `logs/`, `data/`, or repo root**
   without consulting `repo-conventions` for the correct path. If a
   path doesn't fit the conventions, either consult the user OR
   extend `repo-conventions` first, never invent an ad-hoc path.
2. **Never rename or move existing files** unless the user explicitly
   says "clean up" or "rename per conventions". Even then, follow the
   two-step dry-run flow in `repo-conventions` / `test-conventions`.
3. **Never display, commit, echo, or persist real secret values** from
   `.env`, broker tokens, API keys. See `.cursor/rules/secret-hygiene.mdc`.
4. **Never restart the daemon, square off positions, or re-auth the
   broker** without explicit operator approval. The
   `incident-response` skill prints the command for you to approve;
   only execute after the user says "approve <verb>".
5. **Never produce a "brutal review" without also persisting it** to
   `docs/reviews/brutal_review_<YYYY-MM-DD>.md` — this is contractual.
6. **Never produce code-bug-review output anywhere except**
   `docs/bug_found_<YYYY-MM-DD>/`.
7. **Never paraphrase log lines or DB query results as evidence.**
   Quote them.
8. **Never bypass the active freeze window** (`docs/FREEZE_v2.1.md`,
   exits 2026-06-05) by silently editing files in `packages/strategies/`,
   `packages/core/`, or threshold keys in `config.yaml`. If asked to
   make such a change, ask the user for explicit `freeze-bypass:
   <reason>` acknowledgement and record it in `changes-done` with the
   `freeze-bypass` trigger flag (cap: 3 per window per the freeze
   contract).

   **Freeze-safe allowlist** (added 2026-06-01 post-CHG sweep, per
   brutal-review Session 3 Finding 2 — `docs/reviews/brutal_review_2026-06-01.md`):
   the following `packages/core/` modules are upstream
   cost/infrastructure code that the canonical `docs/FREEZE_v2.1.md`
   frozen-file list **does NOT enumerate**. Edits to them do NOT
   require a `freeze-bypass:` tag and do NOT consume a freeze slot,
   provided the edit is a **broker-correctness or upstream-refactor
   fix** (not a behavioural threshold tweak that flows into strategy
   logic):

   - `packages/core/charges.py` — broker rate calibration. Calibrate
     to the live broker's actual rate card. (CHG-01..CHG-05 in
     `docs/findings/findings_log_2026-06-01.md` is the precedent.)
   - `packages/core/regime.py` — regime taxonomy plumbing
     (refactor / observability only; the active regime *thresholds*
     in `config.yaml` remain frozen).

   **Explicitly NOT freeze-safe** (still gated by §8 above and require
   a freeze-bypass slot): `packages/core/risk_manager.py`,
   `packages/core/position_sizer.py`, `packages/strategies/*.py`,
   `packages/strategies/ensemble.py`, and any threshold keys in
   `config.yaml` (`risk.*`, `strategies.*`, `regime.*` thresholds).

   When in doubt, ask the operator. The FREEZE_v2.1.md enumeration
   is the operating contract; this allowlist is a clarification of
   what §8's broader "packages/core/" language was always meant to
   carve out.

## When in doubt

1. Check `.cursor/skills/README.md` for the full skills index.
2. Check `docs/ARCHITECTURE.md` for system context.
3. Check `docs/restructure_plan.md` for layout migration status.
4. Ask the user. Asking is always better than guessing on this
   project — money is involved.

## Adding new skills / rules

See the "Adding new skills" section in `.cursor/skills/README.md`.
Same rule as everything else: extend the convention before creating
the file, not after.
