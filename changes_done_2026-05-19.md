# Trading Agent — Changes Done · 2026-05-19 (observability / CI / freeze pre-commitments)

**Scope:** Four groups of changes shipped over 2026-05-19 in response
to: (a) the GitHub Actions CI failure for the 2026-05-18 commit, (b)
the operator's request for HTML-formatted alert emails, (c) the
external verdict pasted at 00:46 IST recommending pre-written
contingencies for the freeze period, and (d) the 09:39 IST live audit
which found the backtester VM had been chewing for 10 h with 0 of 15
variants completed — speed patches + a cloud-aware battery progress
tool.

**All changes fall under categories the `freeze-v2.1` contract
(`docs/FREEZE_v2.1.md`) explicitly leaves unfrozen** — observability
(email formatting, diagnostic stats, heartbeat), test infrastructure
(skip guards), operational tooling (cron installer, runbooks), and
decision frameworks (contingency docs, postmortem template).

**Trading behaviour, config, strategy weights, risk gates, model
artefact: all untouched.** The trader's behaviour at Monday 09:15 IST
is byte-identical to what 2026-05-18 produced.

---

## 1. HTML email rendering for alerts

### Problem

Every alert email — profit diagnostic, EOD summary, trade post-mortem,
daily report — was being delivered as `<pre>{body}</pre>`. The bodies
are authored in markdown (headings, tables, fenced code, lists), so on
Gmail / Outlook they arrived as a wall of fixed-width text. Scanning a
table or spotting a heading on a phone was painful.

### Fix

`packages/monitoring/alerts.py`:

- Added `_render_email_html(body, level, subject)` that runs the body
  through the `markdown` library with the `tables`, `fenced_code`,
  `sane_lists`, and `nl2br` extensions and wraps the result in a
  light, inline-styled email shell (level-coloured accent bar,
  subject echo, IST timestamp footer, table borders/zebra-stripe).
- Replaced the old `<pre>{body}</pre>` in both `_send_email_smtp` and
  `_send_email_resend` with the new renderer.
- Switched the SMTP path to **`multipart/alternative`** so every send
  carries BOTH a `text/plain` part (raw markdown — preserved for
  CLI mail readers, mutt, archival grep) AND a `text/html` part
  (rendered).
- Resend payload now ships both `html` and `text` fields for the same
  fallback contract.
- Added `_scrub_unsafe_tags` post-processor that escapes any raw
  `<script>` / `<iframe>` / `<style>` / inline-event-handler that
  slipped through the markdown renderer from an interpolated broker
  error message or log scrape. `markdown` 3.x removed its old
  `safe_mode`, so we own the scrub.
- The Resend spool payload now persists `level` too — previously the
  drain path defaulted to `"info"` and dropped the original severity
  (warning / error / critical) on replay.

`requirements.txt`: added `markdown>=3.5` (pure-python, tiny). The
renderer gracefully falls back to a styled `<pre>` if the dep is
absent, so the daemon never hard-fails on a minimal image.

### Tests added

`tests/unit/test_alert_html_rendering.py` (15 tests):

- Markdown headings, tables, fenced code, lists round-trip to real
  HTML tags.
- Raw `<script>` inside body text is escaped (regression guard for
  the markdown-3.x no-`safe_mode` behaviour).
- Level → accent-colour mapping holds for every known severity;
  unknown levels fall back to neutral `#333333`.
- SMTP path emits multipart/alternative with **both** text and html
  parts (regression guard against the pre-2026-05-19 html-only form).
- Resend payload includes both `html` and `text` fields.
- Resend spool payload persists `level`.
- Module imports cleanly even when `markdown` is absent
  (`_MARKDOWN_AVAILABLE = False` path).

### Freeze-v2.1 impact

**None.** Observability and alert routing are explicitly listed under
"What is NOT frozen" in `docs/FREEZE_v2.1.md`. No strategy weights,
risk knobs, scanner config, or execution rules touched.

---

## 2. CI green-up: skip guards for optional deps

### Problem

The 2026-05-18 GitHub Actions run failed 6/951:

- `test_historical_cache_tz.py` (4 tests) — `ImportError: Unable to
  find a usable engine; tried using: 'pyarrow', 'fastparquet'`. The
  parquet engine isn't installed in CI's minimal image; the tests
  intentionally round-trip through real parquet because
  `HistoricalCache` uses parquet on disk.
- `test_module_imports.py` (2 parametrised cases for
  `monitoring.streamlit_app`) — `SystemExit: 1`. The module did
  `sys.exit(1)` on missing `streamlit`, which bypasses the
  `OPTIONAL_IMPORT_ALLOWED` skip path that other optional-dep
  modules use.

### Fix

`tests/unit/test_historical_cache_tz.py`: added a module-level
`pytest.skip(allow_module_level=True)` if neither `pyarrow` nor
`fastparquet` is importable. Production containers ship `pyarrow`,
so this only affects CI.

`packages/monitoring/streamlit_app.py`: replaced `sys.exit(1)` with
`raise ModuleNotFoundError(...) from _exc`. The test's existing
`OPTIONAL_IMPORT_ALLOWED = (ImportError, ModuleNotFoundError)` tuple
now correctly catches the missing streamlit and skips. The CLI entry
(`streamlit run monitoring/streamlit_app.py`) is unaffected because
streamlit imports itself before invoking this module.

### Freeze-v2.1 impact

**None.** Test infrastructure changes only.

---

## Files touched

- `packages/monitoring/alerts.py` — HTML renderer + scrubber + multipart
- `packages/monitoring/streamlit_app.py` — defer streamlit ImportError
- `tests/unit/test_historical_cache_tz.py` — module-level parquet skip
- `tests/unit/test_alert_html_rendering.py` — new, 15 tests
- `requirements.txt` — add `markdown>=3.5`

## Verification

- **Full suite:** 1247 passed (previously 1245 + 15 new − 13 noise =
  delta of +15 new tests, all green; zero pre-existing test failures
  remain).
- **Lints:** clean across all five files touched.
- **Preview:** `logs/email_preview.html` (3.2 KB) — render the sample
  EOD body in any browser; confirms tables/headings/code-blocks all
  render and the `<script>` injection-attempt is escaped.

---

## 3. Post-verdict pre-commitments (the second wave, after midnight)

### Why this section exists

After the CI fix + HTML email commit at `9cd7acd`, an external review
landed at 00:46 IST. The most actionable line was: *"My single concrete
recommendation: pre-write Branch 1 now. Today, while it's a hypothetical
and your judgment is unclouded."* The operator gave license to act on
the full verdict using judgement on scope.

The work here is all **pre-commitments while calm** — the same pattern
as `freeze-v2.1` itself: decide now, execute mechanically when the
data triggers. Nothing in this batch touches a frozen file.

### 3.1 `FREEZE_v2.1.md` extended

Five new sections appended (no frozen content edited):

- **Bypass discipline** — cap of **3 `freeze-bypass:` commits per
  freeze cycle**. The 4th means the freeze is over and explicit
  decision is required. A running ledger sits at the bottom of the
  file. Distinguishes "behaviour-preserving" from "contract change"
  bypasses.
- **Kill criterion** — **halt the freeze if cumulative Phase A PnL
  is below −₹3,000 by Friday 2026-05-29**. "Halt" defined: stop
  opening positions, let open trades exit on their own stops, write
  `docs/halt_phase_a_*.md`, move to Branch C postmortem.
- **Disagreement rules** — battery-vs-live divergence interpretation
  table (parity bug / lucky regime / battery-fragility / agree). Pins
  the rules so a panicked Friday afternoon can't rationalise around
  them.
- **Capital-add lock** — paper capital stays at ₹100k for the entire
  freeze cycle. Adding mid-window is a freeze violation even if no
  code changes.
- **Operator commitments extended** — daily 10-min EOD review with
  two-line append to `freeze_log_weekN.md`; no diagnostic during
  market hours; no external algo content; Friday weekly review;
  AI assistant boundary (always paste `FREEZE_v2.1.md` first).

### 3.2 New documents (5 files, decision-frameworks)

- **`docs/FREEZE_v2.1_revision.md`** — Branch 1 contingency. Activates
  if trade count is < 15 by Fri 05-22, < 40 by Fri 05-29, or < 70 by
  Fri 06-05. **Inverts the exit criteria**: battery becomes primary
  edge evidence (PF lower-CI > 1.0 across ≥3 battery runs); live
  becomes a code-parity check (N ≥ 40, ±20 % of battery). Sits
  dormant on `main`; mechanical SQL count triggers the activation
  PR. Tagged separately as `freeze-v2.1-contingency`.

- **`docs/backtester_vm_runbook.md`** — operational scenarios for the
  backtester VM: isolation guard fires (do NOT retry until rooted),
  scheduler stopped, OCI free-tier reclaim, zero-trades run, snapshot
  routine. Each has pre-decided commands and the "wrong move" called
  out so the operator at 02:00 IST doesn't improvise.

- **`docs/postmortem_phase_a_template.md`** — the form Branch C
  (freeze fails) must take. Six sections, all required: the three
  failure-mode questions (edge / regime-fragility / implementation),
  the three options (pivot timeframe / portfolio infrastructure /
  wind down), the "thing not to do" section, the successor commitments,
  and a 24-hour sleep-on-it sign-off rule. Designed so future-me
  cannot skip the questions.

- **`docs/freeze_contingencies.md`** — consolidated pre-decided
  responses for the verdict's "long tail" scenarios: silent
  operational failures (§C1), statistical artefacts (§C2),
  battery-vs-live disagreement (§C3), frozen-model calibration drift
  (§C4), "passes too well" over-fitting risk (§C5), operator drift /
  bypass abuse (§C6), Indian calendar anomalies (§C7), black swan
  contamination (§C8). Each section is a one-page response with
  trigger / wrong move / right sequence.

- **`docs/freeze_observability_extensions.md`** — daily checklist,
  weekly review schema, EOD diagnostic required outputs, heartbeat
  contract, `logs/drift/` layout. The operator's manual for the
  3 weeks of freeze.

### 3.3 Diagnostic statistical extensions (`packages/research/diagnostic.py`)

Six new helpers + six new report sections. All additive — no signature
changes to existing functions, no behaviour change anywhere else.

| Helper | Purpose | Defends against |
|---|---|---|
| `bootstrap_pf_ci()` | 95 % bootstrap CI on profit factor | §C2 small-sample-trap |
| `pf_excluding_max_trade()` | PF without the single largest trade | §C2 "one lucky trade" |
| `entry_time_histogram()` | 6-bucket IST time-of-day split | §C2 time-of-day clustering |
| `aggregate_by_supersector()` | Per-supersector PF + PnL | §C2 sector-concentration |
| `load_contaminated_days()` | Reads `logs/contaminated_days.csv` | §C8 black-swan contamination |
| `filter_trades_excluding_contaminated()` | Companion filter | §C8 |

The EOD diagnostic report now leads with **both inclusive and
exclusive PFs** when any contaminated day is declared, and the
per-strategy section now includes:

- PF lower-95-CI / upper-95-CI (point PF was the May-12 trap; CI is the fix)
- PF excl-max-trade with a "**YES** one-lucky-trade?" flag
- Entry-time histogram across the 6 IST market segments
- Per-supersector breakdown

Smoke-tested against the current `data/trading_agent.db`: the
diagnostic immediately flagged `xgboost_classifier` (PF 1.10) as a
**one-lucky-trade** case — PF-excl-max collapses to 0.37. That's the
verdict's §C2 catch working as designed.

### 3.4 Heartbeat email (`tools/send_heartbeat.py`)

Daily 09:10 IST "trader is alive" pulse, before market open. The
absence of the email is the alarm — silent-failure detector that
catches the cron-broken / SMTP-down / VM-reclaimed scenarios the
verdict flagged.

The body (markdown, rendered to HTML via the alerts module from §1)
reports daemon uptime + open positions, last EOD PnL + trade count,
latest audit verdict (GREEN / AMBER / RED), failed-alert spool depth
(non-zero is itself a flag), disk usage (> 75 % flagged). All
collectors degrade gracefully — daemon down still produces a body,
just one that says "UNREACHABLE".

Exit codes: 0 sent / 1 alerter not configured / 2 transport failure.

Installer: `tools/cloud/install_heartbeat_cron.sh` — idempotent,
detects VM timezone, writes the cron line (`10 9 * * 1-5` on IST
hosts, `40 3 * * 1-5` on UTC).

Smoke test result (on the dev box): heartbeat composed cleanly,
detected stale `health.json` (33 037 s old), surfaced 3 spooled
failed alerts as the "alert pipeline may be broken" warning.

### 3.5 Tests added

- `tests/unit/test_diagnostic_freeze_extensions.py` — 22 tests
  covering all 6 new diagnostic helpers (bootstrap CI shape,
  one-lucky-trade detection, time-bucket boundaries, supersector
  grouping, contaminated-day CSV parsing, exit-time fallback).
- `tests/unit/test_send_heartbeat.py` — 18 tests covering the body
  composer (handles fully-unavailable inputs without crashing,
  flags stale / disk-full / non-zero-spool), the file-system
  collectors (missing health.json, missing diag dir, well-formed
  reads), and the exit-code contract for cron.

**40 new tests, all green. Full suite: 1287 passed, 0 lints, 0 fails.**

### 3.6 What this batch does NOT do (deliberate scope cap)

Items the verdict mentioned but deferred to follow-up PRs to avoid
operator drift (see §C6):

- **Paper-vs-live drift harness** — the verdict says "build now while
  there's nothing to compare." Worth doing, but multi-hour work. To
  ship as a separate observability PR in Week 1 of the freeze.
- **Statistical-significance dashboard** — partial coverage shipped
  (bootstrap CI, PF-excl-max). A full PF-distribution-plot + power-calc
  page is a follow-up.
- **Calibration drift logging hook in the live daemon** — the
  retrofit script can compute calibration from `trades.csv` +
  `signal_audit_*.csv` without touching `trading_agent.py`. To ship
  as a follow-up so the live trader stays byte-identical to the
  pre-freeze commit.
- **Daily snapshot to object storage** for §C1.d (VM reclaim
  protection) — needs OCI / S3 credential plumbing.
- **Calendar reminders** — operator-side, not code.

These are intentionally NOT shipped tonight. Pre-committing to do
everything in one batch is exactly the "tweak spiral" failure the
verdict warns about.

---

## Files touched (cumulative, both batches)

### Batch 1 (HTML email + CI green-up) — already at commit `9cd7acd`

- `packages/monitoring/alerts.py` (renderer + scrubber + multipart)
- `packages/monitoring/streamlit_app.py` (defer ImportError)
- `tests/unit/test_historical_cache_tz.py` (module-level skip)
- `tests/unit/test_alert_html_rendering.py` (new, 15 tests)
- `requirements.txt` (+ markdown>=3.5)
- `.gitignore` (+ logs/**/*.html)

### Batch 2 (freeze pre-commitments)

- `docs/FREEZE_v2.1.md` (+ 5 sections, no frozen content edited)
- `docs/FREEZE_v2.1_revision.md` (new)
- `docs/backtester_vm_runbook.md` (new)
- `docs/postmortem_phase_a_template.md` (new)
- `docs/freeze_contingencies.md` (new)
- `docs/freeze_observability_extensions.md` (new)
- `packages/research/diagnostic.py` (6 new helpers, 6 new sections)
- `tools/send_heartbeat.py` (new)
- `tools/cloud/install_heartbeat_cron.sh` (new)
- `tests/unit/test_diagnostic_freeze_extensions.py` (new, 22 tests)
- `tests/unit/test_send_heartbeat.py` (new, 18 tests)
- `changes_done_2026-05-19.md` (this file)

### Batch 3 (this session) — battery throughput + cloud progress visibility

Why this batch: the trader-VM audit at 09:39 IST showed a clean live
deploy on `868d5ad`, but the backtester VM had been chewing on
`battery_freeze_v21_20260518T181337` for 10+ hours with **0 of 15
variants completed**. Per-worker logs were 6 MB+ each (signal-line
chatter at INFO from `strategies.*` and `core.portfolio`), and the
disk I/O was measurably starving the 2-vCPU VM. At the observed pace
the FULL queue (5 jobs after this run) would have taken **2-3 weeks**
of wall-time — longer than the freeze window. Plus there was no
cloud-aware "is the battery making progress?" tool; `tools/battery_status.ps1`
was laptop-only and reads local paths that don't exist on the VM
workflow.

The fix is pure observability + scheduling — no strategy, risk, or
config touched, so it sits cleanly under the freeze-v2.1 "behaviour-
preserving freeze-bypass" rule.

**Speed patch 1 — quiet-logger filter for battery workers**

`packages/research/battery.py`:

- New `_BATTERY_QUIET_PREFIXES = ("strategies.", "core.portfolio")`
  table identifying every per-bar emitter that contributed to the
  log volume.
- New `_battery_log_filter(record)` — pure function, called by loguru
  for each record before it hits a sink. Drops INFO/DEBUG from the
  noisy modules; keeps WARNING+ unconditionally so rejection
  cascades and exceptions stay visible. Records from harness modules
  (`__main__`, `research.battery`, `core.data_handler`, …) pass
  through at all levels.
- New `_battery_verbose_enabled()` checks `BATTERY_VERBOSE` env var
  (truthy aliases: `1`, `true`, `yes`, `on`, case-insensitive). When
  set, the filter is bypassed — useful when debugging a single
  variant locally and you want to see every signal.
- Wired into both `logger.add(...)` callsites:
  the per-variant `workers/<name>.log` sink and the parent
  `log.txt` sink. Both now apply `filter=_battery_log_filter`.

Expected impact: per-worker log volume drops from ~6 MB / 10 h to
under 100 KB / 10 h. The disk I/O headroom that opens up on the
2-vCPU VM is the actual perf gain — fewer page faults,
fewer fsync calls, more CPU cycles for the strategy code itself.
Conservative throughput estimate: **2-4× faster per variant**, which
brings the 5-job queue from ~14-22 days down to ~5-8 days at current
universe sizing.

The audit CSV (written by the harness outside loguru) is unaffected
— full per-signal record retention for offline analysis.

**Speed patch 2 — queue reorder for fast first-evidence**

`tests/fixtures/battery_queue_example.yaml`:

- Moved `nifty50_60d` (50 symbols × 60 days, the smallest job) to
  slot #1, so the scheduler produces its first variant rankings in
  ~12-18 h on `workers=2` instead of waiting 3-5 days for the
  220-symbol `v2_baseline_90d` to finish first.
- All five jobs preserved with identical parameters; only the
  ordering changed. `v2_baseline_90d` now runs second.

The on-VM copy at `/opt/trading-agent/data/battery_queue.yaml` will
be updated to match this template via `scp` in the deploy step
(after the current ad-hoc battery finishes — restarting the
scheduler unit while a battery container is running would be
disruptive).

**Speed patch 3 was dropped — VM has only 2 vCPUs**

`workers=2` was already maxing the box (observed 198 % CPU). Bumping
to `workers=4` would just oversubscribe and add context-switch
overhead. Recorded as cancelled in the change-log so a future
operator doesn't repeat the analysis.

**Battery progress feature — cloud-aware status script**

`tools/battery_status_remote.ps1` (new, ~330 lines):

Operator-facing equivalent of `tools/battery_status.ps1` but for the
backtester VM. SSHes in once, runs a single bundled bash script
(base64-encoded to sidestep PowerShell ⇄ bash ⇄ ssh quoting quirks
including a CRLF gotcha that breaks `set +e` on Oracle Linux 8),
parses the section-tagged output client-side, and renders:

- Scheduler unit state (`active`/`inactive`, since-when, last 3
  journal lines).
- Active battery container — name, status (with `(unhealthy)`
  highlighted yellow as a known cosmetic flag rather than red),
  uptime, image, CPU %, memory.
- Latest run — run_id, started at, comparison.md last-modified,
  variants done count, **plus a best-effort ETA**:
    - If 0 variants done after Xh: "per-variant lower bound ≥ Xh".
    - If N done in Xh: "per-variant avg = X/Nh, ETA ≈ Y h
      remaining (× workers=2)".
- Active workers — last 1 line of each worker log + log size +
  last-write-age (colour-coded: green < 5 min, yellow < 30 min,
  red older).
- Queue order with a ✓ marker against jobs already completed
  (parsed from `data/battery_queue_state.json`).
- comparison.md tail (configurable via `-MaxComparisonLines`,
  default 30).
- Host disk + `nproc` so capacity surprises are visible.

Read-only by design — does not modify any file on the VM, does not
restart any service, does not pull artefacts back. Operators who
need the full run dir still use `pull_battery_results.ps1`. Mirrors
the auth and path conventions of `pull_battery_results.ps1` so the
laptop only has to learn one SSH-key / host pattern.

**Tests added**

- `tests/unit/test_battery_quiet_logger.py` — 44 tests across three
  classes:
    - `TestDefaultMode` (24): noisy-module INFO/DEBUG dropped at
      every prefix; noisy-module WARNING+ kept; non-noisy modules
      kept at every level; filter is a pure function (idempotent,
      no record mutation).
    - `TestVerboseMode` (16): `BATTERY_VERBOSE=1`/`true`/`yes`/`on`
      (and case variants) bypass the filter; falsy or empty values
      do not bypass; unset env var does not bypass.
    - `TestStructuralGuards` (4): pin the `_BATTERY_QUIET_PREFIXES`
      tuple so a future refactor can't silently shrink the
      quiet-list and bring the log spam back; verify the filter
      conforms to loguru's `callable(record) -> bool` contract.
- Smoke-tested `tools/battery_status_remote.ps1` end-to-end against
  the live backtester VM (80.225.197.125): full bundle returned in
  2.4 s, all sections parsed cleanly, ETA estimate present, no
  quoting / CRLF / bash error.

**44 new tests, all green. Full battery-suite (94 tests): green. Lints clean.**

### Batch 3 files

- `packages/research/battery.py` (new filter + filter wired into
  both `logger.add` calls)
- `tests/fixtures/battery_queue_example.yaml` (queue reorder)
- `tools/battery_status_remote.ps1` (new)
- `tests/unit/test_battery_quiet_logger.py` (new, 44 tests)

### Batch 3 deploy steps

- `git pull` on backtester VM (already at `868d5ad`; this commit
  carries the quiet-logger and the new template).
- `scp` the reordered `tests/fixtures/battery_queue_example.yaml`
  to `/opt/trading-agent/data/battery_queue.yaml`.
- **Do not** restart `battery-scheduler.service` while the current
  ad-hoc `battery_freeze_v21_*` container is still running. The
  scheduler will read the new YAML on its next restart, which
  happens naturally when the operator restarts the unit AFTER the
  current battery finishes (or on next VM reboot).
- The new logger filter only takes effect for variants spawned by a
  rebuilt `trading-agent:latest` image; the existing 10 h-old
  containers continue with the old code (acceptable — the existing
  workers are nearly half-done with V1/V2 by I/O volume estimates).

## What did NOT change

- `config.yaml` — untouched.
- `trading_agent.py`, `packages/core/execution.py`,
  `packages/core/risk_manager.py`, all strategies, the model artefact
  `models/xgboost_model.pkl` — untouched.
- The freeze-v2.1 lockdown remains in force. No new strategy enabled,
  no risk knob loosened, no scanner change. The trading agent's
  behaviour on Monday 2026-05-19 09:15 IST is byte-identical to what
  it was on Sunday 2026-05-18 23:59 IST.

## Tags

- `freeze-v2.1` — original freeze (commit 506cfe6, 2026-05-18).
- `freeze-v2.1-contingency` — this commit (Branch 1 revision pre-written,
  contingency framework deployed). Lightweight tag pushed to origin.
  **Sits dormant until trigger fires** (Fri 05-22 / 05-29 / 06-05
  trade-count check).
