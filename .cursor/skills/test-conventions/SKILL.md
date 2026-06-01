---
name: test-conventions
description: >-
  Canonical reference for pytest test file/function naming, folder layout,
  marker discipline, and fixture organisation in the Trading Agent repo.
  Owns the dry-run cleanup flow for renaming the legacy dated audit-fix
  test files. Consult before creating, renaming, or moving any test file
  under tests/. Use when the user asks "test naming", "what should I call
  this test", "where does this test go", "fix test names", "clean up
  tests/", "add a marker", "shared fixture", "rename test files", or any
  time the agent itself is about to create a new test.
---

# Test Conventions — Trading Agent

## Why this skill exists

`tests/` currently holds **107 test files** across `unit/` and
`integration/`. The folder split (per `restructure_plan.md` Phase B) is
healthy, but four antipatterns have accumulated:

1. **Dated regression file names** like
   `test_audit_2026_05_28_phase5.py` (8+ files) that are opaque a
   month later.
2. **`test_audit_*` prefix is overloaded** across 4 unrelated concepts
   (the tool, audit fixes, audit sections, dated regression batches).
3. **Markers declared but unused** — `slow` / `integration` / `live`
   are in `pyproject.toml` but rarely applied; folder split has
   substituted for marker discipline.
4. **Flat 95-file `tests/unit/` folder** doesn't mirror
   `packages/{trader,strategies,core,brokers,...}`.

This skill codifies what's good, fixes what's drifting, and is invoked
by a thin Cursor rule on every `tests/**/*.py` edit.

## When this skill fires

- "test naming", "what should I call this test"
- "where does this test go", "where do tests live"
- "fix test names", "rename test files", "clean up tests/"
- "add a marker", "should this be slow/integration/live"
- "shared fixture", "where do shared fixtures go"
- Automatic (via the matching Cursor rule): any agent action that
  creates or renames a file under `tests/`.

## Naming rules (file level)

### Allowed file name patterns

| Pattern | When to use | Example |
|---|---|---|
| `test_<module>.py` | One file per module under test | `test_portfolio.py`, `test_execution.py` |
| `test_<module>_<aspect>.py` | Scoped slice of a module | `test_trailing_stop_persistence.py` |
| `test_<module>_regressions.py` | All regression tests for a module accumulate here | `test_market_safety_regressions.py` |
| `test_<feature>_e2e.py` | End-to-end / pipeline tests (inside `integration/` only) | `test_eod_pipeline_e2e.py` |

### Forbidden file name patterns (do not create more of these)

| Pattern | Why forbidden | Replacement |
|---|---|---|
| `test_<topic>_<YYYY>_<MM>_<DD>_phaseN.py` | Date + phase number convey nothing in 3 months | Add tests to `test_<module>_regressions.py`; use finding-id in the function name |
| `test_<topic>_<YYYY>_<MM>_<DD>_fixes.py` | Same problem | Same fix |
| `test_<topic>_quick_wins.py` | "Quick wins" tells the next operator nothing | Name by what's tested |
| `test_audit_*` for non-audit-tool tests | Overloads a real subject prefix | Use `test_<module>_regressions.py` and reference the audit in the docstring |
| `test_basic.py`, `test_misc.py`, `test_other.py`, `test_v2_*.py` | Mystery files | Split into per-module files |

### Dates inside file names — the one allowed exception

Python module names cannot contain dashes, so when a date *must* be in
a test filename (rare — only for an existing convention or a one-off
exploratory probe), use `YYYY_MM_DD` with underscores. This is the
ONLY place the project's universal `YYYY-MM-DD` rule (from
`repo-conventions`) is relaxed, and it's a hard constraint, not
sloppiness. Document the exception with a comment in the file.

## Naming rules (function level)

Pick **one** convention per file; do not mix.

### Behaviour-driven (preferred for new tests)

```
test_<does_what>_when_<condition>
test_<does_what>_if_<condition>
test_<returns_what>_for_<input>
```

Examples:
- `test_blocks_entry_when_drawdown_exceeds_halt`
- `test_returns_none_for_empty_universe`
- `test_does_not_double_register_fill_on_retry`

### Finding-driven (regression files only)

```
test_<finding_id>_<intent>
```

Examples:
- `test_obs06_market_safety_no_bare_pass_in_staleness_or_spike`
- `test_perf02_sma_calculation_caches_per_symbol`

The finding ID matches the ID in `docs/bug_found_<date>/NN_PX_*.md`
or `docs/audits/<date>/...`. This is the bridge between the test and
the finding doc.

### Forbidden function names

`test_basic`, `test_case_1`, `test_works`, `test_true`,
`test_<module>` (same as the module under test — adds no info),
`test_smoke` (too vague — say what it smokes).

## Folder layout

### Current state (preserved)

```
tests/
├── __init__.py
├── unit/             ← fast, no IO, < 50ms per test
├── integration/      ← touches DB, filesystem, or multiple components
└── fixtures/         ← shared fixtures (currently underused)
```

### Target state (migrate opportunistically — see "Migration policy" below)

```
tests/
├── __init__.py
├── conftest.py                       ← root sys.path bootstrap stays
│
├── unit/
│   ├── conftest.py                   ← shared unit fixtures
│   ├── trader/                       ← mirrors packages/trader/
│   ├── strategies/                   ← mirrors packages/strategies/
│   ├── core/                         ← mirrors packages/core/
│   ├── brokers/                      ← mirrors packages/brokers/
│   ├── monitoring/                   ← mirrors packages/monitoring/
│   ├── research/                     ← mirrors packages/research/
│   ├── training/                     ← mirrors packages/training/
│   └── tools/                        ← tests for tools/*.py scripts
│
├── integration/
│   ├── conftest.py                   ← shared integration fixtures
│   ├── pipeline/                     ← end-to-end DB-touching tests
│   ├── broker/                       ← broker adapter integration
│   └── eod/                          ← EOD writer + alerts integration
│
└── fixtures/
    ├── README.md                     ← REQUIRED — what each fixture provides
    ├── db.py                         ← in-memory sqlite fixture
    ├── broker.py                     ← mocked broker fixture
    └── market_data.py                ← canned bars / ticks fixture
```

### Migration policy (no mass rename)

- **Do not migrate all 95 unit files at once.** That generates a
  100-file diff no one can review.
- **Migrate opportunistically:** every time you touch a test in
  `tests/unit/`, move it into the right subfolder as part of the
  same commit. Within ~3 months the migration completes itself.
- **New tests go straight into the target layout.** No new files at
  the flat `tests/unit/` root.

## Marker discipline (HARD RULE going forward)

`pyproject.toml` already declares the markers. Use them.

| Marker | Meaning | Wall-clock | Network/IO | Default behaviour |
|---|---|---|---|---|
| (none) | fast unit test | < 50ms | none | runs always |
| `@pytest.mark.slow` | slow unit/regression | 50ms – few sec | none | runs `pytest -m slow` |
| `@pytest.mark.integration` | DB / filesystem / multi-component | any | local only | runs on `pytest tests/integration/` |
| `@pytest.mark.live` | hits broker / network / external API | any | external | OFF by default; runs only on `pytest -m live` |
| `@pytest.mark.flaky(reason="...")` | known flaky | any | any | runs but does not block CI; reason is mandatory |

Apply markers at the function level (preferred) or module level.
Don't sprinkle markers as decoration — every applied marker must
match the table above.

`tests/integration/` tests should also carry `@pytest.mark.integration`
explicitly (folder + marker is intentional duplication so `pytest -m
"not integration"` works regardless of folder filters).

## Fixture rules

### Where fixtures live

1. **Function/class-local** — keep in the test file, no fixture
   needed: just construct the object inline.
2. **Module-local** — top of the test file, prefixed with `_` if
   private intent (`@pytest.fixture` named `_db`).
3. **Folder-local** — in `tests/unit/<area>/conftest.py` or
   `tests/integration/<area>/conftest.py`. Available to all tests
   under that folder.
4. **Repo-wide shared** — in `tests/fixtures/<topic>.py`, registered
   via `tests/conftest.py`:
   ```python
   pytest_plugins = [
       "fixtures.db",
       "fixtures.broker",
       "fixtures.market_data",
   ]
   ```

### `tests/fixtures/README.md` is mandatory once `fixtures/` is used

Without it, no one knows which shared fixtures exist and nobody reuses
them. The README should list:

```
| Fixture | Module | Scope | Provides | When to use |
|---|---|---|---|---|
| in_memory_db | fixtures.db | function | empty sqlite with the live schema | any test that exercises DB read/write |
| mock_broker_session | fixtures.broker | function | broker stub returning canned tradeBook | broker-call tests without live API |
| five_min_bars_RELIANCE | fixtures.market_data | session | 60-day canned 5-min bars | strategy backtest unit tests |
```

When you add a new shared fixture, add a row to the README in the same
commit.

## File header docstring (mandatory for new files)

Every new test file starts with a 1–3 line docstring stating **what's
under test and which contract is being verified**.

```python
"""Tests for `packages/core/market_safety.py`.

Contract: staleness and price-spike checks must fail-closed (skip the
trade and log a reason) rather than swallow exceptions silently.
"""
```

Forbidden header docstrings:

- "Tests for the 2026-05-28 audit Phase-1 fixes." — describes
  *origin*, not *contract*. Move the date reference into the
  function-name finding-id and the cross-link to the finding doc.
- "Basic tests." — useless.
- No docstring at all.

## Cross-skill links

- **`repo-conventions`** owns the universal naming rules (ISO dates,
  snake_case, no `_v2/_final` siblings). This skill is the test-shaped
  specialisation; if the two ever conflict, `repo-conventions` wins
  and this skill gets edited.
- **`code-bug-review`** files findings at
  `docs/bug_found_<date>/NN_PX_<slug>.md`. Each finding's
  "Verification plan" section should reference an existing test
  function by its full path:
  `tests/unit/core/test_market_safety_regressions.py::test_obs06_market_safety_no_bare_pass`.
  That's why finding-id function naming matters.
- **`changes-done`** — any test rename/move done via the cleanup
  dry-run below must also produce a `changes-done` entry.

## Known violations and cleanup (two-step, never auto-move)

This skill **never** renames or moves test files on its own. Cleanup
follows the same two-step contract as `repo-conventions`:

**Step 1 — DRY RUN.** When the user says "clean up tests", "rename
test files", "fix test naming", or similar, print:

1. A table of proposed renames (current → canonical).
2. The exact `git mv` commands.
3. The exact commit message.
4. Any references that would break (e.g. a finding doc that cites the
   old test path, an import inside another test).
5. Stop and ask: *"Approve all, a subset, or cancel?"*

**Step 2 — EXECUTE** only after explicit approval. Then run `git mv`
for the approved set, update broken references, commit as one isolated
batch, do not push.

If `git status` is dirty when the user approves, stop and ask them to
stash or commit first.

### Known-violations table (input to the dry run)

| Current path | Canonical path |
|---|---|
| `tests/unit/test_audit_2026_05_25_quick_wins.py` | `tests/unit/<area>/test_<module>_regressions.py` (per-finding) |
| `tests/unit/test_audit_2026_05_26_fixes.py` | `tests/unit/<area>/test_<module>_regressions.py` (per-finding) |
| `tests/unit/test_audit_2026_05_27_fixes.py` | `tests/unit/<area>/test_<module>_regressions.py` (per-finding) |
| `tests/unit/test_audit_2026_05_28_misc.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_audit_2026_05_28_phase1.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_audit_2026_05_28_phase2.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_audit_2026_05_28_phase3.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_audit_2026_05_28_phase4.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_audit_2026_05_28_phase5.py` | split per module → `test_<module>_regressions.py` |
| `tests/unit/test_post_backtest_2026_05_05_fixes.py` (integration) | rename `test_post_backtest_fixes_regressions.py` and move to `tests/integration/` if it touches DB |
| `tests/unit/test_backtester_perf_2026_05_27.py` | `tests/unit/research/test_backtester_perf_regressions.py` |
| `tests/unit/test_audit_per_strategy_section.py` | `tests/unit/tools/test_audit_checkpoint_per_strategy_section.py` (renaming "audit" overload) |

NOTE: "split per module" means the file may produce multiple new
files, one per `packages/<area>/<module>.py` that its tests target.
The dry run will show this expansion explicitly.

DO NOT rename these:

- `test_audit_checkpoint.py` and `test_audit_checkpoint_holiday_detector.py`
  — these actually test the audit-checkpoint tool. They keep the
  `test_audit_*` prefix.

## Template — new test file (use this, do not copy from a dated regression file)

```python
"""Tests for `packages/<area>/<module>.py`.

Contract: <one sentence on the invariant or behaviour being verified>.
"""

from __future__ import annotations

import pytest

from <area>.<module> import <SymbolUnderTest>


# ─────────────────────────── happy path ───────────────────────────


def test_<does_what>_when_<condition>():
    """<one line on why this case matters>."""
    ...


# ─────────────────────────── edge cases ───────────────────────────


def test_<returns_what>_for_<edge_input>():
    ...


# ─────────────────────────── failure modes ────────────────────────


@pytest.mark.slow
def test_<fails_gracefully>_when_<failure_condition>():
    ...
```

## Template — new regressions file

```python
"""Regression tests for `packages/<area>/<module>.py`.

Each test function is keyed to a finding ID in `docs/bug_found_<date>/`
or `docs/audits/`. Add new regressions here rather than creating dated
files.

Cross-links:
- <finding_id>: docs/bug_found_<YYYY-MM-DD>/<NN>_<PX>_<slug>.md
"""

from __future__ import annotations

import pytest


def test_<finding_id>_<intent>():
    """<one-line finding summary>. See cross-link in module docstring."""
    ...
```

## Hard rules

- **Consult this skill before creating a new test file.** The matching
  Cursor rule at `.cursor/rules/test-conventions.mdc` enforces this
  on `tests/**/*.py` edits.
- **No new dated regression files.** Add to
  `test_<module>_regressions.py` instead.
- **No new files at flat `tests/unit/` root** once you've migrated the
  module's existing tests into a subfolder. New tests go straight into
  the target subfolder.
- **Markers are mandatory for slow/integration/live tests.** Folder
  placement alone is not sufficient.
- **Renames are two-step** — dry run first, execute only on explicit
  approval.
- **Never delete a test** as part of cleanup. Renames only.

## What this skill must NOT do

- Do not rewrite test bodies during a rename. Cleanup is filename and
  location only; test content is the operator's call.
- Do not remove a test even if you believe it duplicates another.
  Flag duplicates in the dry-run output and let the operator decide.
- Do not modify `pyproject.toml` `markers` list without the user
  explicitly asking for a new marker.
- Do not migrate the flat `tests/unit/` folder en masse. Opportunistic
  only.
